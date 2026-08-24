"""The video path, minus the transport.

Four steps and one rule:

  1. the rig finishes a take and journals it to its own disk
  2. it asks where to put the bytes
  3. it puts them
  4. it reports the checksum, and the server verifies against what landed

  Only then may the rig delete its local copy.

That last line is the whole design. Everything else here is a retry -
safe to repeat, safe to interrupt, safe to run twice. Step 4 is the one
thing that must be true before a byte is lost, and it is the reason the
rig SSD manages itself instead of needing a cleanup job that has to be
right.

How the bytes actually travel is behind `core.infrastructure.storage`
and settled nowhere in this file.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domains.episodes.model import Episode
from core.infrastructure.storage import Storage, get_storage, object_key

PENDING = "pending"
ON_PREM = "on_prem"
ARCHIVED = "archived"
MISSING = "missing"


class VideoError(Exception):
    """Something the caller has to be told about, not retried past."""


async def _episode(session: AsyncSession, episode_id: str) -> Episode:
    row = await session.get(Episode, episode_id)
    if row is None:
        # The event has not been projected yet, or never arrived. Either
        # way there is nothing to attach bytes to, and inventing a row
        # would create an episode nobody recorded.
        raise VideoError(f"no episode {episode_id}")
    return row


async def where_to_put(session: AsyncSession, episode_id: str, camera: str,
                       storage: Storage | None = None) -> dict:
    """Step 2. Returns where the rig should put this camera's video."""
    ep = await _episode(session, episode_id)
    store = storage or get_storage()
    key = object_key(ep.rig_id, str(ep.episode_id), camera)
    target = store.upload_target(key)

    # Recorded as intent, not as fact. The bytes are not here yet.
    if ep.video_state == PENDING:
        ep.video_key = key
    await session.commit()

    return {
        "episodeId": str(ep.episode_id),
        "camera": camera,
        "key": target.key,
        "url": target.url,
        "method": target.method,
        "expiresInSecs": target.expires_in_secs,
    }


async def confirm(session: AsyncSession, episode_id: str, camera: str,
                  sha256: str, bytes_: int,
                  storage: Storage | None = None) -> dict:
    """Step 4, and the only step that matters.

    The rig says what it sent. The server checks what actually landed. If
    they disagree, the answer is no - and the rig keeps its copy and tries
    again, which is exactly what should happen.
    """
    ep = await _episode(session, episode_id)
    store = storage or get_storage()
    key = object_key(ep.rig_id, str(ep.episode_id), camera)

    landed = await store.head(key)
    if landed is None:
        raise VideoError(f"nothing at {key}")
    if landed.bytes != bytes_:
        raise VideoError(
            f"{key}: rig sent {bytes_} bytes, {landed.bytes} landed"
        )
    if not landed.sha256:
        # The store cannot tell us what it holds. Saying yes here would
        # be taking the rig's word for its own upload and then telling it
        # to delete the only other copy.
        raise VideoError(
            f"{key}: the store returned no checksum, so this upload cannot be verified"
        )
    if landed.sha256 != sha256:
        raise VideoError(f"{key}: checksum does not match what landed")

    ep.video_state = ON_PREM
    ep.video_key = key
    ep.video_bytes = landed.bytes
    ep.video_sha256 = landed.sha256
    ep.video_stored_at = datetime.now(timezone.utc)
    await session.commit()

    return {
        "episodeId": str(ep.episode_id),
        "key": key,
        "bytes": landed.bytes,
        "state": ep.video_state,
        # The rig asked one question and this is the answer to it.
        "safeToDelete": True,
    }


async def mark_missing(session: AsyncSession, episode_id: str, why: str) -> None:
    """A discarded take, or one the recorder never started.

    Recorded rather than left pending, so `pending` keeps meaning "we are
    waiting for this" and the drain worker's backlog is real.
    """
    ep = await _episode(session, episode_id)
    ep.video_state = MISSING
    ep.video_key = None
    await session.commit()


async def drain_batch(session: AsyncSession, limit: int = 50,
                      storage: Storage | None = None) -> tuple[int, list[str]]:
    """Copy what has landed to the cold tier.

    The one slow conversation in the system. Everything upstream of it
    runs at wire speed on the local switch; this can be hours behind
    without any rig noticing, which is the entire argument for a spool.
    """
    store = storage or get_storage()
    rows = await session.execute(
        select(Episode)
        .where(Episode.video_state == ON_PREM)
        .order_by(Episode.video_stored_at)
        .limit(limit)
    )
    episodes = list(rows.scalars().all())
    done: list[str] = []

    for ep in episodes:
        if not ep.video_key:
            continue
        await store.copy_to_archive(ep.video_key)
        ep.video_state = ARCHIVED
        ep.video_archived_at = datetime.now(timezone.utc)
        done.append(ep.video_key)

    await session.commit()
    return len(done), done


async def backlog(session: AsyncSession) -> dict:
    """What is waiting, and how much of it there is.

    Also the only honest source of sizing. Every number in the plan comes
    from "three 1080p30 cameras at ~7 Mbps", which is a guess; these are
    measurements, and they correct it the moment a real episode lands.
    """
    from sqlalchemy import func

    rows = await session.execute(
        select(
            Episode.video_state,
            func.count(),
            func.coalesce(func.sum(Episode.video_bytes), 0),
        ).group_by(Episode.video_state)
    )
    by_state = {
        state: {"episodes": count, "bytes": int(total)}
        for state, count, total in rows.all()
    }

    measured = await session.execute(
        select(
            func.count(),
            func.avg(Episode.video_bytes),
            func.avg(Episode.duration_secs),
        ).where(Episode.video_bytes.is_not(None), Episode.duration_secs > 0)
    )
    n, avg_bytes, avg_secs = measured.one()
    per_second = (float(avg_bytes) / float(avg_secs)) if n and avg_secs else None

    return {
        "byState": by_state,
        "measured": {
            "episodes": int(n or 0),
            "avgBytes": float(avg_bytes) if avg_bytes else None,
            "avgDurationSecs": float(avg_secs) if avg_secs else None,
            "bytesPerSecond": per_second,
            # The plan assumes three 1080p30 cameras at ~7 Mbps, which is
            # ~2.6 MB/s of video per rig. This is what is actually true.
            "planAssumedBytesPerSecond": 3 * 7_000_000 / 8,
        },
    }
