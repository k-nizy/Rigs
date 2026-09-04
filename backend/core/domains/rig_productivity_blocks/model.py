"""One row per stint - one operator's turn at one rig.

Stores the four seconds columns and no efficiency figure. The ratio is
computed at read time by core.rules.efficiency, from one definition, so
it can be corrected later and every shift already recorded recomputes
correctly. A stored percentage cannot be.
"""

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import TimestampedBase


class RigProductivityBlock(TimestampedBase):
    __tablename__ = "rig_productivity_blocks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    rig_id: Mapped[str] = mapped_column(String(16), nullable=False)
    shift_date: Mapped[date] = mapped_column(Date, nullable=False)
    shift_label: Mapped[str] = mapped_column(String(16), nullable=False)
    turn_from: Mapped[str | None] = mapped_column(String(5), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Whose stint these measurements are of. operator_id names the seat the
    # schedule put them in, and a seat is held by different people on
    # different days. Nullable: events filed before the field existed
    # carry no name, and they are not to be refused.
    operator_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    episodes: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_secs: Mapped[float] = mapped_column(Float, nullable=False)
    assigned_secs: Mapped[float] = mapped_column(Float, nullable=False)
    fault_secs: Mapped[float] = mapped_column(Float, nullable=False)
    down_secs: Mapped[float] = mapped_column(Float, nullable=False)

    source_event: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        # One stint per ledger row. Replaying the ledger must not double a
        # stint, and this is what makes that true at the database rather
        # than in the worker.
        UniqueConstraint("source_event", name="uq_blocks_source_event"),
        Index("ix_blocks_operator_shift", "operator_id", "shift_date", "shift_label"),
        # The overrun check reads the last day of blocks on every sweep.
        Index("ix_blocks_ended_at", "ended_at"),
    )
