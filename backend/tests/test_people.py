"""A person is not a seat.

Phase A of "Who a person is, and who is on the floor" in CLAUDE.md: the
table and the ids, and nothing that reads them yet. `op-a4` names the
fourth chair in group A; these rows name whoever sits in it, and keep
naming them when they move to a different chair tomorrow.

Nothing here touches a route or a screen. This is the storage layer on
its own - the one thing phase A adds - and the invariants that are the
database's job rather than the application's: an id that is minted once
and never reused, an email that belongs to one person when it is given
at all, and a row that is disabled rather than deleted so that a take
filed years ago still resolves to a name.

`accounts` is deliberately untouched. Linking a sign-in to a person is a
later phase, and changing both tables at once would mean neither could
be reverted on its own.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from core.domains.people.model import Person
from core.domains.people.repository import PersonRepository, normalise_email


async def person(session, name="Ben Carter", email=None) -> Person:
    return await PersonRepository(session).create(name, email=email)


# ------------------------------------------------- one name, two people


class TestTwoPeopleCanShareAName:
    async def test_two_people_with_one_name_are_two_people(self, session):
        """The whole reason for the table. A seat id merges them; a person
        id keeps them apart, and search brings both back so the manager
        disambiguates rather than the code guessing."""
        a = await person(session, "Ben Carter")
        b = await person(session, "Ben Carter")
        assert a.id != b.id

        found = await PersonRepository(session).search("Ben Carter")
        assert {p.id for p in found} == {a.id, b.id}

    async def test_a_person_with_no_email_is_whole(self, session):
        """An operator who never opens My Shift is still created, found
        and listed. An email is what signing in uses and nothing else."""
        p = await person(session, "Mei Chen")
        assert p.email is None

        repo = PersonRepository(session)
        assert (await repo.get(p.id)) is not None
        assert p.id in {x.id for x in await repo.active()}
        assert p.id in {x.id for x in await repo.search("chen")}

    async def test_many_people_may_have_no_email(self, session):
        """The unique index is partial. NULL is not a value sixteen
        operators are fighting over."""
        for name in ("A", "B", "C"):
            await person(session, name)
        await session.commit()
        assert len(await PersonRepository(session).active()) == 3


# ------------------------------------------------- one email, one person


class TestAnEmailBelongsToOnePerson:
    async def test_an_email_belongs_to_one_person(self, session):
        await person(session, "Ruth Osei", email="r.osei@verlet.co")
        await session.commit()
        with pytest.raises(IntegrityError):
            await person(session, "Somebody Else", email="r.osei@verlet.co")

    async def test_an_email_is_one_spelling(self, session):
        """`Ruth@` and `ruth@` are the same mailbox, so they are the same
        person's - stored one way, so the index can see it."""
        await person(session, "Ruth Osei", email="Ruth.Osei@Verlet.co")
        await session.commit()
        with pytest.raises(IntegrityError):
            await person(session, "Somebody Else", email="ruth.osei@verlet.co")

    async def test_the_stored_spelling_is_the_normalised_one(self, session):
        p = await person(session, "Ruth Osei", email="  Ruth.Osei@Verlet.co ")
        assert p.email == "ruth.osei@verlet.co" == normalise_email("  Ruth.Osei@Verlet.co ")
        found = await PersonRepository(session).by_email("RUTH.OSEI@verlet.co")
        assert found is not None and found.id == p.id


# ------------------------------------------- names change, ids do not


class TestNamesChangeAndIdsDoNot:
    async def test_renaming_keeps_the_id(self, session):
        """Correcting a spelling must never orphan a take. The id is what
        the take will carry; the name is what people read."""
        p = await person(session, "Tomas Rivera")
        await session.commit()
        before = p.id

        repo = PersonRepository(session)
        renamed = await repo.rename(before, "Tomás Rivera")
        assert renamed is not None and renamed.id == before
        assert renamed.name == "Tomás Rivera"
        assert not await repo.search("Tomas Rivera")
        assert [x.id for x in await repo.search("Tomás")] == [before]

    async def test_renaming_nobody_says_so(self, session):
        import uuid
        assert await PersonRepository(session).rename(uuid.uuid4(), "X") is None

    async def test_search_is_forgiving_and_ordered(self, session):
        """Case-insensitive and partial, because a manager picking from a
        list types three letters and expects the right people. Ordered,
        because two runs of the same picker must show the same list."""
        for name in ("Mei Chen", "Kai Nakamura", "Lena Vogel", "Ben Carter"):
            await person(session, name)
        await session.commit()

        repo = PersonRepository(session)
        assert [p.name for p in await repo.search("EN")] == ["Ben Carter", "Lena Vogel", "Mei Chen"]
        assert [p.name for p in await repo.search("nakam")] == ["Kai Nakamura"]
        assert [p.name for p in await repo.active()] == [
            "Ben Carter", "Kai Nakamura", "Lena Vogel", "Mei Chen",
        ]

    async def test_an_empty_search_is_the_whole_floor(self, session):
        """Three letters narrows; none is the picker's opening state."""
        await person(session, "Mei Chen")
        await person(session, "Ben Carter")
        await session.commit()
        repo = PersonRepository(session)
        assert [p.name for p in await repo.search("   ")] == ["Ben Carter", "Mei Chen"]


# ------------------------------------------------ disabled, never deleted


class TestDisabledNeverDeleted:
    async def test_a_disabled_person_still_resolves(self, session):
        """Somebody leaving is not the same as never having been here. A
        take filed under this id last year still has to name them."""
        p = await person(session, "Nadia Haddad", email="n.haddad@verlet.co")
        await session.commit()

        repo = PersonRepository(session)
        when = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
        assert await repo.disable(p.id, now=when) == 1

        still = await repo.get(p.id)
        assert still is not None
        assert still.name == "Nadia Haddad"
        assert still.disabled_at == when
        assert p.id not in {x.id for x in await repo.active()}
        assert p.id not in {x.id for x in await repo.search("nadia")}
        assert p.id in {x.id for x in await repo.search("nadia", include_disabled=True)}

    async def test_disabling_twice_is_once(self, session):
        """Idempotent, and it does not move the date: the day somebody
        left is a fact, not the last time somebody pressed the button."""
        p = await person(session, "Nadia Haddad")
        await session.commit()
        repo = PersonRepository(session)
        first = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
        later = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)
        assert await repo.disable(p.id, now=first) == 1
        assert await repo.disable(p.id, now=later) == 0
        assert (await repo.get(p.id)).disabled_at == first

    async def test_a_disabled_persons_email_stays_theirs(self, session):
        """Somebody who comes back is the same person, re-enabled - not a
        second row that happens to share their address. The index does
        not care that the first row is disabled, and must not."""
        p = await person(session, "Nadia Haddad", email="n.haddad@verlet.co")
        await session.commit()
        await PersonRepository(session).disable(p.id)
        await session.commit()
        with pytest.raises(IntegrityError):
            await person(session, "Nadia Haddad", email="n.haddad@verlet.co")

    async def test_ids_are_never_reused(self, session):
        """Disable one Ben, create another with the same name: two ids,
        and the first still resolves to the first. Nothing about a
        person's id is derived from anything that could recur."""
        first = await person(session, "Ben Carter")
        await session.commit()
        repo = PersonRepository(session)
        await repo.disable(first.id)
        await session.commit()

        second = await person(session, "Ben Carter")
        assert second.id != first.id
        assert (await repo.get(first.id)).disabled_at is not None
        assert (await repo.get(second.id)).disabled_at is None
