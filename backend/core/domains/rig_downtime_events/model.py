"""One row per down -> up. A rig going down opens it; the rig coming back
closes it, which is why this domain has a lifecycle and most do not.
"""

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Index, String, Text
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

    down_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    down_secs: Mapped[float | None] = mapped_column(Float, nullable=True)

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
    )
