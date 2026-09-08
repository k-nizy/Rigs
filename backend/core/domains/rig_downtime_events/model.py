"""One row per down -> up. A rig going down opens it; the rig coming back
closes it, which is why this domain has a lifecycle and most do not.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import UniqueConstraint, BigInteger, Boolean, Date, DateTime, Float, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import TimestampedBase


class RigDowntimeEvent(TimestampedBase):
    __tablename__ = "rig_downtime_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    rig_id: Mapped[str] = mapped_column(String(16), nullable=False)
    shift_date: Mapped[date] = mapped_column(Date, nullable=False)
    shift_label: Mapped[str] = mapped_column(String(16), nullable=False)
    turn_from: Mapped[str | None] = mapped_column(String(5), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Who was at the rig when it went down. operator_id names the seat the
    # schedule put them in, and a seat is held by different people on
    # different days. Nullable: events filed before the field existed
    # carry no name, and they are not to be refused.
    operator_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The person in that seat, as an id that is theirs. See episodes.
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    down_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    down_secs: Mapped[float | None] = mapped_column(Float, nullable=True)

    # How it ended, in the same vocabulary sessions use.
    #
    #   "operator"  the person at the rig pressed Problem solved. down_secs
    #               is what the rig counted, frame by frame.
    #   "resumed"   nobody ever said it came back, but the rig filed work
    #               afterwards, so it demonstrably did. down_secs here is
    #               an upper bound, not a measurement: it was fixed at some
    #               point at or before that work happened.
    #
    # Null on rows that are still open, and on rows projected before this
    # column existed.
    ended_by: Mapped[str | None] = mapped_column(String(16), nullable=True)

    issue: Mapped[str] = mapped_column(String(64), nullable=False)
    # The whole path taken through the issue tree, not just the leaf.
    # "Other > Other hardware > Cable" says more about a recurring fault
    # than "Cable" does alone.
    issue_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_manager: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # "rig" or "previous_operator". The app already draws this on the wall
    # - "found at handover, charged to the previous operator, not you" -
    # and it is a fact about a person's shift, so it has to survive here
    # or the desk can never show it.
    charged_to: Mapped[str] = mapped_column(String(24), nullable=False)

    source_event: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_downtime_rig_shift", "rig_id", "shift_date", "shift_label"),
        Index("ix_downtime_open", "rig_id", "up_at"),
        # One row per ledger row. Replaying the ledger must not double
        # a fact, and this is what makes that true at the database
        # rather than in the worker - the same guard the productivity
        # blocks have always had, which was only ever applied to one
        # of the three tables that needed it.
        UniqueConstraint("source_event", name="uq_downtime_source_event"),
    )
