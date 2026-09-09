"""People, from the desk's side: created deliberately, found by picking.

Phase C's first half. The picker on the desk needs somebody to pick, and
until now the only way anybody came to exist was a terminal command that
makes accounts, not people. These are the routes a manager uses instead.

Who may call them is asserted in test_roles.py, in the same sweep as the
push and the board. This file is what they do once let through, so it
runs on a deployment with no accounts - the open-until-configured path -
except where a signed-in manager is the point.

The rules under test are the repository's, reached over HTTP: two people
with one name are two rows and both come back; an email belongs to one
person, spelt one way; a name changes and an id does not; disabled is
not deleted.
"""

from __future__ import annotations

import uuid

from services.rigs.people import CSRF_HEADER
from tests.test_roles import MANAGER, accounts, serving, signed_in

NOBODY = "00000000-0000-4000-8000-000000000000"


async def add(c, name, email=None, **headers):
    r = await c.post("/api/people", json={"name": name, "email": email}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------- creating


class TestCreatedDeliberately:
    async def test_a_person_comes_back_with_an_id_that_is_theirs(self, engine):
        async with serving() as c:
            p = await add(c, "Ben Carter")
        uuid.UUID(p["id"])
        assert p == {"id": p["id"], "name": "Ben Carter", "email": None, "disabledAt": None}

    async def test_two_people_with_one_name_are_two_people(self, engine):
        async with serving() as c:
            a = await add(c, "Ben Carter")
            b = await add(c, "Ben Carter")
            assert a["id"] != b["id"]
            found = (await c.get("/api/people?q=ben")).json()["people"]
        assert {p["id"] for p in found} == {a["id"], b["id"]}

    async def test_an_email_belongs_to_one_person(self, engine):
        async with serving() as c:
            await add(c, "Ruth Osei", "r.osei@verlet.co")
            r = await c.post("/api/people", json={"name": "Somebody", "email": "R.Osei@Verlet.co"})
        assert r.status_code == 409
        assert "address" in r.json()["detail"]

    async def test_the_address_is_stored_one_way(self, engine):
        async with serving() as c:
            p = await add(c, "Ruth Osei", "  R.Osei@Verlet.co ")
        assert p["email"] == "r.osei@verlet.co"

    async def test_a_blank_name_is_refused(self, engine):
        async with serving() as c:
            r = await c.post("/api/people", json={"name": "   "})
        assert r.status_code == 422


# ---------------------------------------------------------------- picking


class TestFoundByPicking:
    async def test_search_is_forgiving_and_ordered(self, engine):
        async with serving() as c:
            for name in ("Mei Chen", "Kai Nakamura", "Lena Vogel", "Ben Carter"):
                await add(c, name)
            got = (await c.get("/api/people?q=EN")).json()["people"]
        assert [p["name"] for p in got] == ["Ben Carter", "Lena Vogel", "Mei Chen"]

    async def test_no_query_is_the_whole_floor(self, engine):
        async with serving() as c:
            await add(c, "Mei Chen")
            await add(c, "Ben Carter")
            got = (await c.get("/api/people")).json()["people"]
        assert [p["name"] for p in got] == ["Ben Carter", "Mei Chen"]

    async def test_one_person_by_id(self, engine):
        async with serving() as c:
            p = await add(c, "Mei Chen", "m.chen@verlet.co")
            r = await c.get(f"/api/people/{p['id']}")
        assert r.status_code == 200
        assert r.json() == p

    async def test_nobody_by_that_id(self, engine):
        async with serving() as c:
            r = await c.get(f"/api/people/{NOBODY}")
        assert r.status_code == 404


# --------------------------------------------------- names change, ids do not


class TestNamesChangeAndIdsDoNot:
    async def test_renaming_keeps_the_id(self, engine):
        async with serving() as c:
            p = await add(c, "Tomas Rivera")
            r = await c.patch(f"/api/people/{p['id']}", json={"name": "Tomás Rivera"})
            assert r.status_code == 200, r.text
            assert r.json()["id"] == p["id"]
            assert r.json()["name"] == "Tomás Rivera"
            assert (await c.get("/api/people?q=Tomas")).json()["people"] == []
            assert [x["id"] for x in (await c.get("/api/people?q=Tomás")).json()["people"]] == [p["id"]]

    async def test_renaming_nobody_says_so(self, engine):
        async with serving() as c:
            r = await c.patch(f"/api/people/{NOBODY}", json={"name": "X"})
        assert r.status_code == 404

    async def test_the_id_is_not_a_field_anybody_can_send(self, engine):
        """There is no route that changes an id, and a body that tries is
        refused rather than ignored - ignored is how a client comes to
        believe it worked."""
        async with serving() as c:
            p = await add(c, "Tomas Rivera")
            r = await c.patch(f"/api/people/{p['id']}", json={"name": "T", "id": str(uuid.uuid4())})
        assert r.status_code == 422


# ------------------------------------------------ disabled, never deleted


class TestDisabledNeverDeleted:
    async def test_disabling_hides_from_the_picker_but_still_resolves(self, engine):
        async with serving() as c:
            p = await add(c, "Nadia Haddad", "n.haddad@verlet.co")
            r = await c.post(f"/api/people/{p['id']}/disable")
            assert r.status_code == 200, r.text
            assert r.json() == {"ok": True, "changed": 1}

            assert (await c.get("/api/people?q=nadia")).json()["people"] == []
            withheld = (await c.get("/api/people?q=nadia&includeDisabled=true")).json()["people"]
            assert [x["id"] for x in withheld] == [p["id"]]

            still = (await c.get(f"/api/people/{p['id']}")).json()
            assert still["name"] == "Nadia Haddad"
            assert still["disabledAt"] is not None

    async def test_disabling_twice_is_once(self, engine):
        async with serving() as c:
            p = await add(c, "Nadia Haddad")
            first = (await c.post(f"/api/people/{p['id']}/disable")).json()
            second = (await c.post(f"/api/people/{p['id']}/disable")).json()
        assert (first["changed"], second["changed"]) == (1, 0)

    async def test_disabling_nobody_says_so(self, engine):
        async with serving() as c:
            r = await c.post(f"/api/people/{NOBODY}/disable")
        assert r.status_code == 404

    async def test_a_disabled_persons_address_stays_theirs(self, engine):
        async with serving() as c:
            p = await add(c, "Nadia Haddad", "n.haddad@verlet.co")
            await c.post(f"/api/people/{p['id']}/disable")
            r = await c.post("/api/people", json={"name": "Nadia Haddad", "email": "n.haddad@verlet.co"})
        assert r.status_code == 409


# ------------------------------------------------ as a signed-in manager


class TestAsASignedInManager:
    """The same acts, through a real session with the CSRF token the desk
    echoes back. Everything above ran on the open path; this is the
    floor's path."""

    async def test_the_whole_flow(self, engine, session):
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c, MANAGER)
            h = {CSRF_HEADER: csrf}
            p = await add(c, "Ben Carter", "b.carter@verlet.co", **h)
            assert (await c.patch(f"/api/people/{p['id']}", json={"name": "Benjamin Carter"}, headers=h)).status_code == 200
            assert (await c.get("/api/people?q=benj")).json()["people"][0]["id"] == p["id"]
            assert (await c.post(f"/api/people/{p['id']}/disable", headers=h)).json()["changed"] == 1
            # Reads need the session but not the token - a GET changes nothing.
            assert (await c.get(f"/api/people/{p['id']}")).status_code == 200
