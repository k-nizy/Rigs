"""The append-only ledger. The source of truth for everything a rig sent.

The obvious design writes each incoming event straight into whichever
fact table it belongs to. That works, and it is quietly lossy: the moment
your reading of an event is wrong, the original is already gone.

Writing the envelope verbatim first means a projection bug found in six
months can be fixed and replayed. It also collapses three problems into
one constraint - UNIQUE (rig_id, event_id) makes ingest idempotent, which
is what lets the uploader on the rig retry blindly, forever, without ever
asking whether its last attempt landed.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import TimestampedBase


class RigEvent(TimestampedBase):
    __tablename__ = "rig_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    rig_id: Mapped[str] = mapped_column(String(16), nullable=False)

    # Minted on the rig at pedal-press, before anything downstream is told.
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # Per-rig and monotonic. max(seq) is the uploader's cursor.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # The rig's wall clock. received_at comes from TimestampedBase, and the
    # difference between the two is measurable clock skew.
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    shift_date: Mapped[date] = mapped_column(Date, nullable=False)
    shift_label: Mapped[str] = mapped_column(String(16), nullable=False)
    turn_from: Mapped[str | None] = mapped_column(String(5), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Accepted, never demanded. The ledger is the system and every fact
    # table derives from it, so a name that is not here cannot survive a
    # replay - but events written before the field existed have none, and
    # a rig discards a refused batch rather than retrying it.
    operator_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The person, so it survives a replay. Opaque - no key to `people`,
    # see the envelope for why.
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    bucket: Mapped[str] = mapped_column(String(32), nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)

    # The whole envelope, exactly as it arrived. Nothing is discarded at
    # ingest, so a projection can always be rebuilt from here.
    envelope: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Which schedule version was in force. Null if the event arrived
    # before its schedule did, which the projection worker can resolve
    # later rather than rejecting the event now.
    push_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Set when a projection has consumed this row. Null means outstanding,
    # which is the whole of the projection worker's claim query.
    projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The one constraint that makes blind retry safe.
        UniqueConstraint("rig_id", "event_id", name="uq_rig_events_rig_event"),
        Index("ix_rig_events_rig_seq", "rig_id", "seq"),
        Index("ix_rig_events_unprojected", "projected_at", postgresql_where=(projected_at.is_(None))),
        # The floor sweep asks "when did this rig last say anything?" for
        # every rig, every fifteen seconds, forever. Without this it is a
        # scan of the ledger - the one table that only ever grows.
        Index("ix_rig_events_rig_at", "rig_id", "at"),
    )
