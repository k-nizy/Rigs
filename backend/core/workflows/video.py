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

Everything here is **per camera**, not per take. A take is three cameras
and `object_key()` has always produced three keys; the bookkeeping used
to be six columns on `episodes` with a single `video_key`, so `confirm()`
overwrote it with whichever camera landed last. One camera per take got
archived and freed and the other two sat on the spool for ever,
uncounted. `core/domains/episode_videos/model.py` carries the whole story
of that. The rig had it right first: a camera that fails to upload should
cost that camera, not the take.

How the bytes actually travel is behind `core.infrastructure.storage`
and settled nowhere in this file.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domains.episode_videos.model import EpisodeVideo
from core.domains.episodes.model import Episode
from core.infrastructure.storage import Storage, get_storage, object_key

log = logging.getLogger("rigs.video")

PENDING = "pending"
ON_PREM = "on_prem"
ARCHIVED = "archived"
EXPIRED = "expired"
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


async def _camera_row(session: AsyncSession, ep: Episode, camera: str,
                      create: bool = False) -> EpisodeVideo | None:
    rows = await session.execute(
        select(EpisodeVideo).where(
            EpisodeVideo.episode_id == ep.episode_id,
            EpisodeVideo.camera == camera,
        )
    )
    found = rows.scalar_one_or_none()
    if found is not None or not create:
        return found
    made = EpisodeVideo(
        episode_id=ep.episode_id, camera=camera, rig_id=ep.rig_id, state=PENDING
    )
    session.add(made)
    await session.flush()
    return made


# --------------------------------------------------------------- upload

async def where_to_put(session: AsyncSession, episode_id: str, camera: str,
                       storage: Storage | None = None) -> dict:
    """Step 2. Returns where the rig should put this camera's video."""
    ep = await _episode(session, episode_id)
    store = storage or get_storage()
    key = object_key(ep.rig_id, str(ep.episode_id), camera)
    target = store.upload_target(key)

    row = await _camera_row(session, ep, camera, create=True)
    # Recorded as intent, not as fact. The bytes are not here yet.
    if row.state == PENDING:
        row.key = key
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

    row = await _camera_row(session, ep, camera, create=True)
    row.state = ON_PREM
    row.key = key
    row.bytes = landed.bytes
    row.sha256 = landed.sha256
    row.stored_at = datetime.now(timezone.utc)
    await session.commit()

    return {
        "episodeId": str(ep.episode_id),
        "camera": camera,
        "key": key,
        "bytes": landed.bytes,
        "state": row.state,
        # The rig asked one question and this is the answer to it.
        "safeToDelete": True,
    }


async def expire_pending(session: AsyncSession, after_days: int,
                         limit: int = 500) -> int:
    """Stop waiting for takes that are never coming. Returns how many.

    A camera becomes `pending` when the rig asks where to put it. If the
    upload never completes - the rig was replaced, its disk was wiped, it
    went for repair and came back empty - that row waits for ever, and the
    backlog it sits in is the number somebody is supposed to look at to
    decide whether the spool is healthy. A backlog full of takes nobody
    will ever send stops being a signal.

    Seven days: long enough to cover a weekend plus a rig away being
    fixed, short enough that the number still means something.

    Being wrong here is cheap, and that is deliberate. Nothing is
    deleted - the bytes were never here, they are on the rig - and this
    only changes a label. If the take does turn up afterwards, `confirm()`
    sets the row straight back to `on_prem` without caring what it said
    before, so a rig returning from three weeks in a workshop uploads its
    backlog and the rows heal themselves.

    Measured from `received_at`, the server's own clock, for the same
    reason the projection lag is: a rig with a wrong clock must not be
    able to age its own rows out early or keep them alive for ever.
    """
    if after_days <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=after_days)
    rows = await session.execute(
        select(EpisodeVideo)
        .where(
            EpisodeVideo.state == PENDING,
            EpisodeVideo.received_at < cutoff,
        )
        .order_by(EpisodeVideo.received_at)
        .limit(limit)
    )
    given_up = list(rows.scalars().all())
    for row in given_up:
        row.state = MISSING
        # The key is where it would have gone. object_key() is
        # deterministic, so a late arrival recomputes the same one.
        row.key = None

    if given_up:
        await session.commit()
        log.info("stopped waiting for %d takes not uploaded within %d days",
                 len(given_up), after_days)
    return len(given_up)


# ---------------------------------------------------------------- drain

def _archive_matches(row: EpisodeVideo, landed) -> bool:
    """Is what the archive holds the same take we are about to stop keeping?

    Deliberately strict in the same way `confirm()` is. Past this point
    the on-prem copy is gone, so "probably fine" is not an answer.
    """
    if landed is None:
        return False
    if row.bytes is not None and landed.bytes != row.bytes:
        return False
    if row.sha256:
        # An archive that cannot say what it holds has not proved anything.
        if not landed.sha256 or landed.sha256 != row.sha256:
            return False
    return True


async def drain_batch(session: AsyncSession, limit: int = 50,
                      storage: Storage | None = None) -> tuple[int, list[str]]:
    """Move what has landed to the cold tier, and free the spool.

    The one slow conversation in the system. Everything upstream of it
    runs at wire speed on the local switch; this can be hours behind
    without any rig noticing, which is the entire argument for a spool.

    A spool that never frees is not a spool. This used to copy and stop,
    so every byte existed twice for ever and the on-prem disk filled at
    exactly the rate video arrived. That fails worse than it sounds: once
    the spool is full `confirm()` starts refusing, so twelve rigs
    correctly keep their own copies, and the failure walks backwards onto
    the floor.

    Two passes, because they are two different facts and a crash between
    them must not lose either:

      1. copy to the archive, verify what landed, record it as archived
      2. release the spool copy of anything recorded as archived

    Pass 2 is idempotent and self-healing. A crash after the copy but
    before the release leaves a row that pass 2 collects next time; a
    crash after the release but before its commit deletes an object that
    is already gone, which is a no-op. Nothing is ever freed on the
    strength of a copy call returning - only on the strength of the
    archive being asked what it holds.
    """
    store = storage or get_storage()
    now = datetime.now(timezone.utc)

    rows = await session.execute(
        select(EpisodeVideo)
        .where(EpisodeVideo.state == ON_PREM)
        .order_by(EpisodeVideo.stored_at)
        .limit(limit)
    )
    done: list[str] = []

    for row in list(rows.scalars().all()):
        if not row.key:
            continue
        await store.copy_to_archive(row.key)

        if not _archive_matches(row, await store.head_archive(row.key)):
            # Left on_prem deliberately. The next run copies again, which
            # is safe, and the spool copy stays until somebody can prove
            # the archive has it.
            continue

        row.state = ARCHIVED
        row.archived_at = now
        done.append(row.key)

    await session.commit()

    await release_spool(session, limit=limit, storage=store)
    return len(done), done


async def release_spool(session: AsyncSession, limit: int = 50,
                        storage: Storage | None = None) -> tuple[int, int]:
    """Delete spool copies the archive already holds.

    Returns (cameras freed, bytes freed). Separate from the copy so it can
    be run on its own, and so a leak left by a crash is collected rather
    than sitting on the disk for ever.
    """
    store = storage or get_storage()
    rows = await session.execute(
        select(EpisodeVideo)
        .where(EpisodeVideo.state == ARCHIVED, EpisodeVideo.freed_at.is_(None))
        .order_by(EpisodeVideo.archived_at)
        .limit(limit)
    )
    freed = 0
    freed_bytes = 0
    now = datetime.now(timezone.utc)

    for row in list(rows.scalars().all()):
        if not row.key:
            row.freed_at = now
            continue
        # Asked again rather than trusted from pass 1: this may be a row
        # left behind by a crash, minutes or days later.
        if not _archive_matches(row, await store.head_archive(row.key)):
            # Persistent lines here mean the spool is not draining, which
            # ends with a full disk and rigs that cannot hand over takes.
            log.warning("spool not released for %s: the archive cannot account for it",
                        row.key)
            continue
        await store.delete(row.key)
        row.freed_at = now
        freed += 1
        freed_bytes += row.bytes or 0

    await session.commit()
    return freed, freed_bytes


async def expire_archive(session: AsyncSession, keep_days: int, limit: int = 50,
                         storage: Storage | None = None) -> tuple[int, int]:
    """Delete archived video older than `keep_days`. Returns (cameras, bytes).

    The mechanism, not the policy. `keep_days = 0` does nothing at all and
    is the default, because how long this floor keeps its footage is a
    cost decision measured in petabytes a year and is not one this file
    gets to make. When somebody picks a number, this is where it goes.

    Two things it will not do:

    **It never touches the episode.** The row, the four seconds columns,
    the score, the attribution - all of it stays. Only the bytes go. The
    facts are the point of the system and they cost nothing to keep; the
    video is what costs 2.7 TB a day.

    **It only considers cameras that reached the cold tier and were
    released.** Anything still on the spool, still pending, or archived
    but not yet freed is not old enough to be anybody's idea of expired,
    whatever the date says.
    """
    if keep_days <= 0:
        return 0, 0

    store = storage or get_storage()
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    rows = await session.execute(
        select(EpisodeVideo)
        .where(
            EpisodeVideo.state == ARCHIVED,
            EpisodeVideo.freed_at.is_not(None),
            EpisodeVideo.archived_at < cutoff,
        )
        .order_by(EpisodeVideo.archived_at)
        .limit(limit)
    )
    gone = 0
    gone_bytes = 0
    for row in list(rows.scalars().all()):
        if row.key:
            await store.delete_archived(row.key)
        row.state = EXPIRED
        gone += 1
        gone_bytes += row.bytes or 0
        # Said out loud. This is the only irreversible thing the service
        # does, and "we deleted 400 takes last night" should be findable.
        log.info("expired %s of episode %s", row.camera, row.episode_id)

    await session.commit()
    return gone, gone_bytes


# -------------------------------------------------------------- backlog

async def backlog(session: AsyncSession) -> dict:
    """What is waiting, and how much of it there is.

    Also the only honest source of sizing. Every number in the plan comes
    from "three 1080p30 cameras at ~7 Mbps", which is a guess; these are
    measurements, and they correct it the moment a real episode lands.

    Counted per camera and summed per take. The old version counted one
    camera per episode and called it the episode, which made every number
    here a third of the truth - including the one that exists to replace
    the guess.
    """
    rows = await session.execute(
        select(
            EpisodeVideo.state,
            func.count(),
            func.coalesce(func.sum(EpisodeVideo.bytes), 0),
        ).group_by(EpisodeVideo.state)
    )
    by_state = {
        state: {"cameras": count, "bytes": int(total)}
        for state, count, total in rows.all()
    }

    # What the on-prem disk is actually holding right now: everything that
    # has landed and not yet been freed. This is the number that decides
    # whether the spool is a buffer or a slowly filling disk.
    spool = await session.execute(
        select(func.count(), func.coalesce(func.sum(EpisodeVideo.bytes), 0))
        .where(
            EpisodeVideo.bytes.is_not(None),
            EpisodeVideo.state.in_([ON_PREM, ARCHIVED]),
            EpisodeVideo.freed_at.is_(None),
        )
    )
    spool_cameras, spool_bytes = spool.one()

    # Archived but not yet released. Zero for a moment in the ordinary
    # case, and persistently non-zero when the release is failing - which
    # is the difference worth being able to see before the disk fills.
    awaiting = await session.execute(
        select(func.count()).where(
            EpisodeVideo.state == ARCHIVED, EpisodeVideo.freed_at.is_(None)
        )
    )

    # Per take: all cameras summed, against the take's own duration. A
    # per-camera average would answer a question nobody is asking - what a
    # floor costs is what a whole take costs.
    per_take = (
        select(
            EpisodeVideo.episode_id.label("eid"),
            func.sum(EpisodeVideo.bytes).label("total"),
        )
        .where(EpisodeVideo.bytes.is_not(None))
        .group_by(EpisodeVideo.episode_id)
        .subquery()
    )
    measured = await session.execute(
        select(
            func.count(),
            func.avg(per_take.c.total),
            func.avg(Episode.duration_secs),
        )
        .select_from(per_take)
        .join(Episode, Episode.episode_id == per_take.c.eid)
        .where(Episode.duration_secs > 0)
    )
    n, avg_bytes, avg_secs = measured.one()
    per_second = (float(avg_bytes) / float(avg_secs)) if n and avg_secs else None

    return {
        "byState": by_state,
        "spool": {
            "cameras": int(spool_cameras or 0),
            "bytes": int(spool_bytes or 0),
            "awaitingRelease": int(awaiting.scalar() or 0),
        },
        "measured": {
            "takes": int(n or 0),
            "avgBytesPerTake": float(avg_bytes) if avg_bytes else None,
            "avgDurationSecs": float(avg_secs) if avg_secs else None,
            "bytesPerSecond": per_second,
            # The plan assumes three 1080p30 cameras at ~7 Mbps, which is
            # ~2.6 MB/s of video per rig. This is what is actually true.
            "planAssumedBytesPerSecond": 3 * 7_000_000 / 8,
        },
    }
