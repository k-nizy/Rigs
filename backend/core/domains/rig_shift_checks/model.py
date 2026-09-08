"""Checklist passes and the fault reports raised against them."""

import uuid
from datetime import date, datetime

from sqlalchemy import UniqueConstraint, BigInteger, Date, DateTime, Float, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import TimestampedBase


class RigShiftCheck(TimestampedBase):
    __tablename__ = "rig_shift_checks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    rig_id: Mapped[str] = mapped_column(String(16), nullable=False)
    shift_date: Mapped[date] = mapped_column(Date, nullable=False)
    shift_label: Mapped[str] = mapped_column(String(16), nullable=False)
    turn_from: Mapped[str | None] = mapped_column(String(5), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Who was at the rig when the check was filed. operator_id names the seat the
    # schedule put them in, and a seat is held by different people on
    # different days. Nullable: events filed before the field existed
    # carry no name, and they are not to be refused.
    operator_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The person in that seat, as an id that is theirs. See episodes.
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # shift_check | fault_opened | fault_reclassified | fault_closed | fault_cancelled
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    # raised | passed | passed_early, for a shift_check; null for a fault.
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # Gripper | Camera | CAN bus, for a fault; null for a check.
    subsystem: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Seconds charged to the operator. A fault fixed honestly costs them
    # nothing; a report withdrawn charges the time back, which is the
    # distinction the app already draws on the wall.
    seconds_charged: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    source_event: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_shift_checks_rig_shift", "rig_id", "shift_date", "shift_label"),
        # One row per ledger row. Replaying the ledger must not double
        # a fact, and this is what makes that true at the database
        # rather than in the worker - the same guard the productivity
        # blocks have always had, which was only ever applied to one
        # of the three tables that needed it.
        UniqueConstraint("source_event", name="uq_shift_checks_source_event"),
    )
