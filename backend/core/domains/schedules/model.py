"""The pushed schedule, stored whole.

Every event a rig sends back is stamped with rigId, shiftDate, turnFrom
and operatorId - all of them fields from the payload the desk pushed.
That makes the schedule the dimension table for the entire event stream,
which is why it is a domain and not a config blob.

The payload is stored as an opaque validated document on purpose. The
rotation lives in one JavaScript file that both the desk and the rig
load, and the founding invariant of this system is that the rig must
never compute a different answer from the desk that scheduled it. A
Python re-implementation would be a third answer and the first one that
could silently disagree, so this table reads what was pushed and never
derives it.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date, DateTime, ForeignKey, Index, String, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import Base, TimestampedBase


class Schedule(TimestampedBase):
    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # One push covers the whole floor; every rig's row shares its push_id,
    # so an event can name the exact schedule version in force when it
    # happened and attribution stays auditable after a re-push.
    push_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    pushed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    rig_id: Mapped[str] = mapped_column(String(16), nullable=False)
    shift_date: Mapped[date] = mapped_column(Date, nullable=False)
    shift_label: Mapped[str] = mapped_column(String(16), nullable=False)

    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        # One row per rig per shift, per push.
        #
        # This was (push_id, rig_id), which assumed a push covered a
        # single shift. It does not any more: the desk sends the whole
        # day in one press, because a payload covers one shift and
        # pushing only the current one is what left a rig holding a
        # finished schedule at the boundary with nothing newer to pick
        # up. Under the old key that push failed outright, since each
        # rig appeared three times beneath one push id.
        #
        # The intent is unchanged and still holds: a rig cannot be given
        # two versions of the same shift in one push, and every row of a
        # push still shares its id, so an event can still name the exact
        # schedule version in force when it happened.
        UniqueConstraint("push_id", "rig_id", "shift_date", "shift_label",
                         name="uq_schedules_push_rig_shift"),
        Index("ix_schedules_rig_shift", "rig_id", "shift_date", "shift_label"),
    )


class SchedulePush(Base):
    """Who changed the floor's day, and when.

    The ledger records everything a *rig* does and nothing a *manager*
    does. A push rewrites what twelve machines run for a whole calendar
    day, and until this table existed the only trace was thirty-six
    schedule rows that say what was pushed and not by whom. "Who put the
    floor on this schedule" had no answer.

    One row per accepted push, sharing its `push_id` with the schedule
    rows it produced, so the two join without either owning the other.

    **The name is a snapshot, not a lookup.** `account_id` is kept for
    joining, but the email and name are written down as they were at the
    time. An audit line that renders through a live join changes when
    somebody is renamed, and a record of what happened that rewrites
    itself is not a record. It is the same instinct as the ledger keeping
    the envelope a rig actually sent.

    **Accepted pushes only.** A rejected one never reaches here, because
    validation raises before the transaction. A refused attempt is worth
    a log line and is not worth a row that says the floor changed when it
    did not.
    """

    __tablename__ = "schedule_pushes"

    push_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    pushed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # How the caller got in: a signed-in manager, the shared desk token,
    # or a deployment with neither configured. Recorded rather than
    # inferred, so "open" is visible in the history instead of looking
    # the same as a manager whose name was not captured.
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Where from, by the same rule the rig identity uses - a forwarded
    # header believed only from loopback.
    address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # What it covered: how many payloads, how many rigs, which dates and
    # shifts. Counts and lists, not a verdict - the same reason no
    # percentage is stored anywhere in this service.
    covered: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index("ix_schedule_pushes_at", "pushed_at"),
    )
