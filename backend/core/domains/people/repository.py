"""Reading and writing people.

Two things the desk will lean on later are decided here, because they
are properties of the storage rather than of a screen.

**Search returns everybody who matches, and in a fixed order.** Two real
people called Ben Carter come back as two rows, so the manager
disambiguates rather than the code picking one. And the same three
letters give the same list twice, because a picker that reorders itself
between one glance and the next is one people misclick on.

**A name changes; an id does not.** `rename` touches one column. There
is no method that changes an id, and there should never be one - the id
is what a take carries, and moving it moves the take.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select, update

from core.base.repository import BaseRepository
from core.domains.people.model import Person


class PersonRepository(BaseRepository[Person]):
    model = Person

    async def create(self, name: str, email: str | None = None) -> Person:
        """Mint a person. The one place an id comes from."""
        return await self.add(Person(
            name=name.strip(),
            email=normalise_email(email) if email else None,
        ))

    async def by_email(self, email: str) -> Person | None:
        rows = await self.session.execute(
            select(Person).where(Person.email == normalise_email(email))
        )
        return rows.scalar_one_or_none()

    async def active(self, limit: int = 200) -> Sequence[Person]:
        """Everybody who can currently be assigned. What a picker opens on."""
        rows = await self.session.execute(
            select(Person)
            .where(Person.disabled_at.is_(None))
            .order_by(Person.name, Person.id)
            .limit(limit)
        )
        return rows.scalars().all()

    async def search(self, fragment: str, *, include_disabled: bool = False,
                     limit: int = 200) -> Sequence[Person]:
        """Everybody whose name contains the fragment, case-insensitively.

        An empty fragment is the whole floor, because that is the state a
        picker is in before anybody has typed. `autoescape` so a `%` in
        what somebody typed is a character and not a wildcard.
        """
        fragment = fragment.strip()
        stmt = select(Person)
        if fragment:
            stmt = stmt.where(Person.name.icontains(fragment, autoescape=True))
        if not include_disabled:
            stmt = stmt.where(Person.disabled_at.is_(None))
        rows = await self.session.execute(
            stmt.order_by(Person.name, Person.id).limit(limit)
        )
        return rows.scalars().all()

    async def rename(self, person_id: uuid.UUID, name: str) -> Person | None:
        """Correct a name. Returns the row, or None if there is nobody to
        correct - which a caller should treat as an error rather than a
        no-op, since it means the id in hand names nobody."""
        p = await self.get(person_id)
        if p is None:
            return None
        p.name = name.strip()
        await self.session.flush()
        return p

    async def disable(self, person_id: uuid.UUID,
                      now: datetime | None = None) -> int:
        """Somebody leaves. Returns how many rows it changed, so a second
        press is visibly a no-op and the date of the first one stands:
        the day a person left is a fact, not the last time somebody
        clicked."""
        result = await self.session.execute(
            update(Person)
            .where(Person.id == person_id, Person.disabled_at.is_(None))
            .values(disabled_at=now or datetime.now(timezone.utc))
        )
        changed = result.rowcount or 0
        if changed:
            # A core UPDATE does not tell the identity map, so a caller
            # who loaded this person earlier would still see them enabled
            # - and `session.get()` would hand back that stale object
            # without asking the database. Reload the row over it. Only
            # when something changed: the no-op path changed nothing to
            # reload.
            await self.session.execute(
                select(Person)
                .where(Person.id == person_id)
                .execution_options(populate_existing=True)
            )
        return changed


def normalise_email(email: str) -> str:
    """One spelling of an address, everywhere it is written or read.

    The same rule `accounts` applies, restated here rather than imported:
    domains do not import one another (see `.importlinter`), and the
    rule is one line. If it ever grows, it grows in both places or moves
    below both of them.
    """
    return email.strip().lower()
