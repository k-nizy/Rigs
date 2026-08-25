"""One row per camera per take.

This used to be six columns on `episodes`, and that was wrong in a way
that hid itself for as long as nothing ever deleted anything.

A take is three cameras. `object_key()` has always produced three keys -
`RIG-03/<episode>/front.mp4`, `wrist-l.mp4`, `overhead.mp4` - but the
episode row had a single `video_key`, so `confirm()` overwrote it with
whichever camera happened to land last. The consequence, once the drain
started freeing the spool: one camera per take was archived and released
and the other two sat on the disk for ever, uncounted, unarchived, and
invisible to the very backlog that was supposed to notice a spool
filling up. `/floor/video` reported a third of the truth, and it is the
number that exists to replace the plan's sizing guess.

The rig had it right first. Its comment reads: *queued per camera,
because that is how the store keys them and how a partial upload stays
partial rather than costing the whole episode.* A camera that fails to
upload should cost that camera, not the take - and that is only
expressible if each camera is a row.

The lifecycle is per camera and the states are the ones the workflow has
always used:

    pending -> on_prem -> archived -> expired
                  \\
                   -> missing        (discarded, or never recorded)

`freed_at` is separate from `archived_at` on purpose: reaching the cold
tier and no longer keeping our own copy are two different facts, and a
crash can land between them.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, DateTime, ForeignKey, Index, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import TimestampedBase


class EpisodeVideo(TimestampedBase):
    __tablename__ = "episode_videos"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # The episode the rig minted at pedal-press. Not a composite primary
    # key with `camera`, because a surrogate id keeps the drain's ordering
    # cheap; the pair is unique below, which is what actually matters.
    episode_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("episodes.episode_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Slugged by the rig: "front", "wrist-l", "overhead". It is part of a
    # path in an object store, so it has to survive being a filename.
    camera: Mapped[str] = mapped_column(String(32), nullable=False)

    # Denormalised from the episode so the drain and the backlog never
    # have to join to answer "whose spool is this filling".
    rig_id: Mapped[str] = mapped_column(String(16), nullable=False)

    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    key: Mapped[str | None] = mapped_column(Text, nullable=True)
    bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    stored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    freed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # One row per camera per take. This is the constraint whose
        # absence let a second camera overwrite the first.
        UniqueConstraint("episode_id", "camera", name="uq_episode_videos_camera"),
        # The drain's claim query.
        Index("ix_episode_videos_state", "state"),
        # Its second pass: archived, spool copy not yet released. Partial,
        # so it is empty in the steady state.
        Index("ix_episode_videos_unfreed", "archived_at",
              postgresql_where=(freed_at.is_(None))),
        Index("ix_episode_videos_rig", "rig_id"),
    )
