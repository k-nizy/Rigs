"""A person, as distinct from a seat.

`rotation-engine.js` builds an operator id as `"op-" + group + slot`, so
`op-a4` is the fourth chair in group A and names whoever is sitting in it
today. File work under it and two people's takes land in one folder; the
natural report - group by `operator_id` - credits the wrong person, and
nobody finds out until somebody runs it.

This table is the other half. A row here is one person, with an id that
is minted once when a manager adds them and travels with them into every
seat they ever work. The seat keeps its own id, for drawing the sheet and
nothing else.

**Nothing reads this yet.** That is deliberate: the table lands on its
own so the migration can be reverted on its own. The episode row, the
desk's picker and the roster each learn about it in a later phase.

**Not `accounts`.** An account is a way to sign in; a person is who did
the work, and most people here will never sign in to anything - an
operator who never opens My Shift is still created, assigned, recorded
and reported on. The two are linked later, from the account's side.

**An email is optional**, and unique only when it is given. It is what
My Shift signs in with and nothing else, so a person without one is not
missing anything this table cares about.

**Disabled, never deleted.** Somebody leaving is not the same as them
never having been here, and a take filed under this id a year ago still
has to resolve to a name. Somebody who comes back is the same person,
re-enabled - which is why the unique index on email does not care that
the row is disabled, and must not.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import TimestampedBase


class Person(TimestampedBase):
    __tablename__ = "people"

    # Random, so nothing about it can recur. A disabled person's id is
    # never handed to anybody else because no id is ever handed to
    # anybody twice; that is a property of the generator, not a rule
    # something has to remember to enforce.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Editable for as long as the row lives. Correcting a spelling changes
    # this and nothing else, so it never orphans a take.
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # Stored lowercased by the repository, so `Ruth@` and `ruth@` are one
    # mailbox and therefore one person, and the index below can tell.
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Partial: NULL is not a value sixteen operators without an
        # address are fighting over. Same shape as the operator index on
        # accounts, for the same reason.
        Index("uq_people_email", "email", unique=True,
              postgresql_where=text("email IS NOT NULL")),
    )
