"""Open alerts on the floor.

An alert is a *state*, not a message. It opens when a condition becomes
true and closes when it stops being true, which is why the sweep can run
every few seconds without producing a stream of duplicates: the same
situation yields the same `key`, and a key that is already open is left
alone.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import TimestampedBase


class Alert(TimestampedBase):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Stable per situation. Sweeping twice must not open two alerts.
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    rig_id: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Only one alert may be open per key at a time. A resolved one may
        # sit alongside it, which is how the same rig going silent twice
        # in a shift reads as two outages rather than one long one.
        Index("uq_alerts_open_key", "key", unique=True,
              postgresql_where=(resolved_at.is_(None))),
        Index("ix_alerts_open", "resolved_at"),
        UniqueConstraint("key", "opened_at", name="uq_alerts_key_opened"),
    )
