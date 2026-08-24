"""One row per take, saved or discarded.

A discarded take is a row, not an absence. The plan is explicit that the
bucket carries takes "saved **or** discarded" - an operator throwing away
a bad demonstration is a real event, and a floor where nothing is ever
discarded is a floor worth asking about.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import TimestampedBase


class Episode(TimestampedBase):
    __tablename__ = "episodes"

    # The id the rig minted at pedal-press. It also names the video
    # directory on the rig's disk, which is what makes the join possible
    # without a lookup table.
    episode_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    rig_id: Mapped[str] = mapped_column(String(16), nullable=False)
    shift_date: Mapped[date] = mapped_column(Date, nullable=False)
    shift_label: Mapped[str] = mapped_column(String(16), nullable=False)
    turn_from: Mapped[str | None] = mapped_column(String(5), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_secs: Mapped[float] = mapped_column(Float, nullable=False)

    # "saved" or "discarded".
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    # 3, 4 or 5 when saved; null when discarded, because a thrown-away
    # take was never scored.
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Which ledger row produced this, so a projection can be traced back.
    source_event: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # ---- the video
    #
    # A state machine, and the states are the point:
    #
    #   pending    the rig has it; nobody else does
    #   on_prem    landed and the checksum matched. Only now may the rig
    #              delete its own copy - that one rule is what makes the
    #              rig SSD self-managing, and everything else in the video
    #              path is a retry.
    #   archived   copied to the cold tier
    #   missing    the take was discarded, or the rig never recorded it
    # server_default, not default. A Python-side default produces no DDL,
    # so ALTER TABLE ADD COLUMN NOT NULL fails the moment the table has
    # rows - which is every table in production and no table in a fresh
    # test database. This one was caught by having run the end-to-end
    # script first.
    video_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    video_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    video_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    video_stored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    video_archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_episodes_rig_shift", "rig_id", "shift_date", "shift_label"),
        # The drain worker's claim query: everything landed but not yet
        # archived.
        Index("ix_episodes_video_state", "video_state"),
        Index("ix_episodes_operator", "operator_id"),
    )
