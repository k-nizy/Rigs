"""One row per rig: when it was last heard from.

This exists because the event ledger cannot answer the question. A rig
can be alive, correct, and silent for a long stretch - on standby, or
mid-take - so an absence of events proves nothing. An absence of
heartbeats does.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import Base


class RigStatus(Base):
    __tablename__ = "rig_status"

    rig_id: Mapped[str] = mapped_column(String(16), primary_key=True)

    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # The rig's own clock at that moment, and how far it was out. Twelve
    # rigs disagreeing about the time makes the event stream unsortable
    # and mis-attributes every handover, so it is measured continuously
    # rather than discovered later.
    rig_clock_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    skew_secs: Mapped[float] = mapped_column(Float, nullable=False, default=0)
