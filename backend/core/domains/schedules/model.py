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

from sqlalchemy import Date, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import TimestampedBase


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
        UniqueConstraint("push_id", "rig_id", name="uq_schedules_push_rig"),
        Index("ix_schedules_rig_shift", "rig_id", "shift_date", "shift_label"),
    )
