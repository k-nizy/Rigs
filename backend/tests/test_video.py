"""The video path, minus the transport.

The one thing worth proving here is the rule that lets a rig delete its
own copy: the server says yes only when what landed matches what the rig
says it sent. Everything else in this path is a retry; that step is the
one that must never be optimistic, because past it a byte is gone.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from core.domains.episode_videos.model import EpisodeVideo
from core.domains.episodes.model import Episode
from core.infrastructure import storage as storage_mod
from core.infrastructure.storage import (
    LocalStorage, Stored, object_key, sha256_of,
)
from core.workflows.video import (
    ARCHIVED, MISSING, ON_PREM, PENDING,
    EXPIRED, VideoError, backlog, confirm, drain_batch, expire_archive,
    mark_missing, release_spool,
    where_to_put,
)

RIG = "RIG-03"
VIDEO = b"not really an mp4, but it has bytes and a checksum" * 100


@pytest.fixture
def store(tmp_path: Path) -> LocalStorage:
    s = LocalStorage(tmp_path / "spool", tmp_path / "archive")
    storage_mod.configure(s)
    return s


async def an_episode(session, episode_id=None, duration=92.0) -> Episode:
    ep = Episode(
        episode_id=episode_id or uuid.uuid4(),
        rig_id=RIG, shift_date=date(2026, 8, 24), shift_label="Morning",
        turn_from="09:00", operator_id="op-a4",
        at=datetime(2026, 8, 24, 9, 5, tzinfo=timezone.utc),
        duration_secs=duration, outcome="saved", score=4, source_event=1,
    )
    session.add(ep)
    await session.commit()
    return ep


async def cam(session, ep, camera="front") -> EpisodeVideo | None:
    """The row for one camera of one take. Video is per camera now, so
    almost every assertion below is about one of these rather than about
    the episode."""
    rows = await session.execute(
        select(EpisodeVideo).where(
            EpisodeVideo.episode_id == ep.episode_id,
            EpisodeVideo.camera == camera,
        )
    )
    return rows.scalar_one_or_none()


# ------------------------------------------------------------- the key

def test_the_key_joins_the_row_to_the_bytes_without_a_lookup():
    """The episode id is minted on the rig at pedal-press and names the
    directory there too."""
    k = object_key("RIG-03", "abc", "front")
    assert k == "RIG-03/abc/front.mp4"


def test_a_key_cannot_escape_the_storage_root(tmp_path):
    s = LocalStorage(tmp_path)
    with pytest.raises(ValueError):
        s._path("../../etc/passwd")


# ---------------------------------------------------------- the sequence

async def test_a_new_episode_has_no_video_yet(client, session, store):
    """No rows at all, not three pending ones. A camera becomes a row when
    the rig asks where to put it, which is what keeps `pending` meaning
    something somebody is actually waiting for."""
    ep = await an_episode(session)
    assert await cam(session, ep) is None
    assert (await backlog(session))["byState"] == {}


async def test_the_rig_is_told_where_to_put_it(client, session, store):
    ep = await an_episode(session)
    where = await where_to_put(session, str(ep.episode_id), "front")
    assert where["key"] == object_key(RIG, str(ep.episode_id), "front")
    assert where["method"] == "PUT"

    v = await cam(session, ep)
    assert v.state == PENDING, "asking where to put it is not the same as having it"


async def test_confirming_a_matching_upload_permits_deletion(client, session, store):
    """The only step that matters. Past this the rig deletes its copy."""
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")
    await store.put(key, VIDEO)

    result = await confirm(session, str(ep.episode_id), "front", sha256_of(VIDEO), len(VIDEO))
    assert result["safeToDelete"] is True
    assert result["bytes"] == len(VIDEO)

    v = await cam(session, ep)
    assert v.state == ON_PREM
    assert v.sha256 == sha256_of(VIDEO)
    assert v.stored_at is not None


async def test_a_wrong_checksum_is_refused_and_the_rig_keeps_its_copy(client, session, store):
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")
    await where_to_put(session, str(ep.episode_id), "front")
    await store.put(key, VIDEO)

    with pytest.raises(VideoError, match="checksum"):
        await confirm(session, str(ep.episode_id), "front", "0" * 64, len(VIDEO))

    v = await cam(session, ep)
    assert v.state == PENDING, (
        "a mismatch must leave the episode pending - the rig has the only good copy"
    )


async def test_a_truncated_upload_is_refused(client, session, store):
    """The commonest real failure: a connection dropped mid-transfer."""
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")
    await where_to_put(session, str(ep.episode_id), "front")
    await store.put(key, VIDEO[: len(VIDEO) // 2])

    with pytest.raises(VideoError, match="bytes"):
        await confirm(session, str(ep.episode_id), "front", sha256_of(VIDEO), len(VIDEO))

    v = await cam(session, ep)
    assert v.state == PENDING


async def test_confirming_nothing_that_arrived_is_refused(client, session, store):
    ep = await an_episode(session)
    with pytest.raises(VideoError, match="nothing at"):
        await confirm(session, str(ep.episode_id), "front", sha256_of(VIDEO), len(VIDEO))


async def test_an_unknown_episode_is_refused(client, session, store):
    """Inventing a row would create an episode nobody recorded."""
    with pytest.raises(VideoError, match="no episode"):
        await where_to_put(session, str(uuid.uuid4()), "front")


async def test_a_discarded_take_is_marked_missing_not_left_pending(client, session, store):
    """So `pending` keeps meaning "we are waiting for this" and the
    backlog is real rather than full of takes nobody will ever send."""
    ep = await an_episode(session)
    # The rig asked where to put all three before the operator discarded
    # it, so there are three rows waiting on bytes that will never come.
    for c in ("front", "wrist-l", "overhead"):
        await where_to_put(session, str(ep.episode_id), c)

    await mark_missing(session, str(ep.episode_id), "discarded")

    for c in ("front", "wrist-l", "overhead"):
        row = await cam(session, ep, c)
        assert row.state == MISSING, c + " was left pending"
        assert row.key is None


# ---------------------------------------------------------------- drain

async def test_the_drain_moves_landed_video_to_the_archive(client, session, store):
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")
    await store.put(key, VIDEO)
    await confirm(session, str(ep.episode_id), "front", sha256_of(VIDEO), len(VIDEO))

    count, keys = await drain_batch(session)
    assert count == 1 and keys == [key]

    v = await cam(session, ep)
    assert v.state == ARCHIVED
    assert v.archived_at is not None
    assert (store.archive / key).exists(), "the bytes never reached the archive"


async def test_the_drain_ignores_what_has_not_landed(client, session, store):
    await an_episode(session)                     # pending, no bytes
    count, _ = await drain_batch(session)
    assert count == 0


async def test_draining_twice_archives_once(client, session, store):
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")
    await store.put(key, VIDEO)
    await confirm(session, str(ep.episode_id), "front", sha256_of(VIDEO), len(VIDEO))

    assert (await drain_batch(session))[0] == 1
    assert (await drain_batch(session))[0] == 0, "an archived object was claimed again"


# -------------------------------------------------------------- backlog

async def test_the_backlog_reports_what_is_waiting(client, session, store):
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")
    await store.put(key, VIDEO)
    await confirm(session, str(ep.episode_id), "front", sha256_of(VIDEO), len(VIDEO))

    b = await backlog(session)
    assert b["byState"][ON_PREM]["cameras"] == 1
    assert b["byState"][ON_PREM]["bytes"] == len(VIDEO)


async def test_the_backlog_measures_what_a_video_actually_costs(client, session, store):
    """Every sizing number in the plan comes from an assumption I wrote
    down. This is the measurement that replaces it, and it fills itself in
    the moment a real episode lands."""
    ep = await an_episode(session, duration=100.0)
    key = object_key(RIG, str(ep.episode_id), "front")
    await store.put(key, VIDEO)
    await confirm(session, str(ep.episode_id), "front", sha256_of(VIDEO), len(VIDEO))

    m = (await backlog(session))["measured"]
    assert m["takes"] == 1
    assert m["avgDurationSecs"] == 100.0
    assert m["bytesPerSecond"] == pytest.approx(len(VIDEO) / 100.0)
    assert m["planAssumedBytesPerSecond"] == 3 * 7_000_000 / 8, (
        "the assumption has to stay visible beside the measurement"
    )


# --------------------------------------------------------------- routes

async def test_the_presign_route_serves(client, session, store):
    ep = await an_episode(session)
    r = await client.post(
        f"/api/rigs/{RIG}/episodes/{ep.episode_id}/video:presign",
        json={"camera": "front"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["key"].endswith("front.mp4")


async def test_the_complete_route_refuses_a_mismatch_with_409(client, session, store):
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")
    await store.put(key, VIDEO)

    r = await client.post(
        f"/api/rigs/{RIG}/episodes/{ep.episode_id}/video:complete",
        json={"camera": "front", "sha256": "0" * 64, "bytes": len(VIDEO)},
    )
    assert r.status_code == 409, "a checksum mismatch is a conflict, not a 404"


async def test_the_complete_route_accepts_a_match(client, session, store):
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")
    await store.put(key, VIDEO)

    r = await client.post(
        f"/api/rigs/{RIG}/episodes/{ep.episode_id}/video:complete",
        json={"camera": "front", "sha256": sha256_of(VIDEO), "bytes": len(VIDEO)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["safeToDelete"] is True


async def test_an_unknown_episode_gets_a_404_not_a_409(client, session, store):
    r = await client.post(
        f"/api/rigs/{RIG}/episodes/{uuid.uuid4()}/video:complete",
        json={"camera": "front", "sha256": "0" * 64, "bytes": 1},
    )
    assert r.status_code == 404


async def test_the_video_backlog_route_serves(client, session, store):
    r = await client.get("/api/floor/video")
    assert r.status_code == 200
    assert "measured" in r.json()


# ------------------------------------------------- through the service
#
# The other upload model. LocalStorage has no presigning and points its
# uploads at /api/storage/{key}; until this route existed that path was a
# promise nothing kept, and every video test had to write to the store
# behind the service's back.


async def test_the_rig_can_put_bytes_through_the_service(client, session, store):
    ep = await an_episode(session)
    where = await where_to_put(session, str(ep.episode_id), "front")
    assert where["url"] == "/api" + where["url"].split("/api", 1)[1]

    r = await client.put(where["url"], content=VIDEO)
    assert r.status_code == 200, r.text
    assert r.json()["bytes"] == len(VIDEO)

    landed = await store.head(where["key"])
    assert landed is not None and landed.bytes == len(VIDEO)


async def test_the_whole_path_runs_without_reaching_behind_the_service(client, session, store):
    """Presign, put, confirm - every step over HTTP, nothing written to the
    store directly. This is the sequence a rig actually performs."""
    ep = await an_episode(session)

    where = (await client.post(
        f"/api/rigs/{RIG}/episodes/{ep.episode_id}/video:presign",
        json={"camera": "front"},
    )).json()

    put = await client.put(where["url"], content=VIDEO)
    assert put.status_code == 200

    done = await client.post(
        f"/api/rigs/{RIG}/episodes/{ep.episode_id}/video:complete",
        json={"camera": "front", "sha256": sha256_of(VIDEO), "bytes": len(VIDEO)},
    )
    assert done.status_code == 200, done.text
    assert done.json()["safeToDelete"] is True

    v = await cam(session, ep)
    assert v.state == ON_PREM


async def test_an_empty_body_is_refused(client, session, store):
    """A zero-byte PUT is a dropped connection, not a video."""
    r = await client.put("/api/storage/RIG-03/abc/front.mp4", content=b"")
    assert r.status_code == 400


async def test_a_key_that_climbs_out_of_the_root_is_refused(client, session, store):
    """Percent-encoded, because a plain "../.." is normalised away by the
    client before it is ever sent - which made the first version of this
    test pass against a routing 404 while proving nothing about the guard.
    Encoded, it survives and arrives as a key with ".." in it.
    """
    r = await client.put("/api/storage/%2e%2e%2f%2e%2e%2fetc%2fpasswd", content=b"x")
    assert r.status_code == 400, (
        "a key that climbs out of the storage root was accepted: %s" % r.status_code
    )
    assert "escapes" in r.json()["detail"]


async def test_bytes_that_arrived_are_not_bytes_that_are_confirmed(client, session, store):
    """Storing is not confirming. A PUT of the wrong bytes still lands -
    and confirm is what refuses it, which is the whole point of having a
    step that reads back out of the store."""
    ep = await an_episode(session)
    where = await where_to_put(session, str(ep.episode_id), "front")
    await client.put(where["url"], content=b"the wrong video entirely")

    r = await client.post(
        f"/api/rigs/{RIG}/episodes/{ep.episode_id}/video:complete",
        json={"camera": "front", "sha256": sha256_of(VIDEO), "bytes": len(VIDEO)},
    )
    assert r.status_code == 409
    v = await cam(session, ep)
    assert v.state == PENDING, "the rig must keep its copy"


# --------------------------------------------------------- three cameras
#
# The bug none of the tests above could see. A take is three cameras and
# the bookkeeping was one `video_key` on the episode, so `confirm()`
# overwrote it with whichever camera landed last. Everything passed:
# something was archived, something was freed, the backlog reported a
# number. It was a third of the truth and the other two thirds sat on the
# spool for ever.

CAMERAS = ("front", "wrist-l", "overhead")


async def _whole_take(session, store, duration=92.0):
    """All three cameras of one take, uploaded and confirmed."""
    ep = await an_episode(session, duration=duration)
    for c in CAMERAS:
        key = object_key(RIG, str(ep.episode_id), c)
        await store.put(key, VIDEO)
        await confirm(session, str(ep.episode_id), c, sha256_of(VIDEO), len(VIDEO))
    return ep


async def test_a_take_is_three_rows_not_one(client, session, store):
    ep = await _whole_take(session, store)
    for c in CAMERAS:
        row = await cam(session, ep, c)
        assert row is not None, c + " was not recorded at all"
        assert row.state == ON_PREM
        assert row.bytes == len(VIDEO)


async def test_confirming_one_camera_does_not_overwrite_another(client, session, store):
    """The mechanism of the bug, written down."""
    ep = await an_episode(session)
    for c in ("front", "overhead"):
        await store.put(object_key(RIG, str(ep.episode_id), c), VIDEO)
        await confirm(session, str(ep.episode_id), c, sha256_of(VIDEO), len(VIDEO))

    front = await cam(session, ep, "front")
    overhead = await cam(session, ep, "overhead")
    assert front.key != overhead.key
    assert front.key.endswith("front.mp4")
    assert overhead.key.endswith("overhead.mp4")


async def test_every_camera_of_a_take_reaches_the_archive_and_is_freed(client, session, store):
    """The consequence. One camera archived and two orphaned is what a
    filling disk looks like from the database's point of view: empty."""
    ep = await _whole_take(session, store)

    count, keys = await drain_batch(session)
    assert count == 3, "only %d of three cameras were archived" % count

    for c in CAMERAS:
        row = await cam(session, ep, c)
        assert row.state == ARCHIVED, c + " never reached the archive"
        assert row.freed_at is not None, c + " was left on the spool for ever"
        assert (store.archive / row.key).exists()
        assert await store.head(row.key) is None, c + " is still on the spool"


async def test_the_spool_is_actually_empty_when_it_says_it_is(client, session, store):
    """The check that would have caught it: ask the disk, not the table."""
    ep = await _whole_take(session, store)
    await drain_batch(session)

    b = await backlog(session)
    assert b["spool"]["cameras"] == 0 and b["spool"]["bytes"] == 0

    left = list(store.root.rglob("*.mp4"))
    left = [f for f in left if store.archive not in f.parents]
    assert left == [], "the spool reports empty while holding %d files" % len(left)


async def test_one_camera_failing_costs_that_camera_and_not_the_take(client, session, store):
    """The rig has always said so: queued per camera, because that is how
    a partial upload stays partial rather than costing the whole take."""
    ep = await an_episode(session)
    for c in ("front", "overhead"):
        await store.put(object_key(RIG, str(ep.episode_id), c), VIDEO)
        await confirm(session, str(ep.episode_id), c, sha256_of(VIDEO), len(VIDEO))
    # wrist-l never arrives
    await where_to_put(session, str(ep.episode_id), "wrist-l")

    count, _ = await drain_batch(session)
    assert count == 2, "a missing camera stopped the two that did arrive"
    assert (await cam(session, ep, "wrist-l")).state == PENDING
    assert (await cam(session, ep, "front")).state == ARCHIVED


async def test_the_measurement_counts_the_whole_take(client, session, store):
    """The number that exists to replace the plan's sizing guess. Counting
    one camera and calling it the episode made it a third of the truth -
    which would have under-sized the floor by 3x."""
    ep = await _whole_take(session, store, duration=100.0)

    m = (await backlog(session))["measured"]
    assert m["takes"] == 1
    assert m["avgBytesPerTake"] == 3 * len(VIDEO), (
        "a take is three cameras, and the sizing number has to say so"
    )
    assert m["bytesPerSecond"] == pytest.approx(3 * len(VIDEO) / 100.0)
    assert m["planAssumedBytesPerSecond"] == 3 * 7_000_000 / 8, (
        "the assumption has to stay visible beside the measurement"
    )


# ----------------------------------------------------------- the spool
#
# A spool that never frees is not a spool. The drain used to copy to the
# archive and stop, so every byte existed twice for ever and the on-prem
# disk filled at exactly the rate video arrived - ~2.7 TB a day across
# twelve rigs. Worse than it sounds: a full spool makes confirm() refuse,
# so twelve rigs correctly keep their own copies and the failure walks
# backwards onto the floor.
#
# Freeing is the one step here that destroys something, so it is held to
# the same standard as confirm(): never on the strength of a copy call
# returning, only on the strength of asking the archive what it holds.


async def _landed(session, store, duration=92.0):
    """An episode whose video is on the spool and confirmed."""
    ep = await an_episode(session, duration=duration)
    key = object_key(RIG, str(ep.episode_id), "front")
    await store.put(key, VIDEO)
    await confirm(session, str(ep.episode_id), "front", sha256_of(VIDEO), len(VIDEO))
    return ep, key


async def test_the_drain_frees_the_spool_copy(client, session, store):
    ep, key = await _landed(session, store)
    assert await store.head(key) is not None

    await drain_batch(session)

    assert (store.archive / key).exists(), "the bytes never reached the archive"
    assert await store.head(key) is None, (
        "the spool still holds a take the archive already has - "
        "the disk fills at the rate video arrives"
    )
    v = await cam(session, ep)
    assert v.state == ARCHIVED
    assert v.freed_at is not None


async def test_nothing_is_freed_if_the_archive_did_not_take_it(client, session, store):
    """The copy silently doing nothing must not cost the only other copy."""
    ep, key = await _landed(session, store)

    class Deaf(type(store)):
        async def copy_to_archive(self, k):    # pretends to work
            return "nowhere"

    deaf = Deaf(store.root, store.archive)
    count, _ = await drain_batch(session, storage=deaf)

    assert count == 0, "an episode was recorded as archived on a copy that did nothing"
    assert await deaf.head(key) is not None, "the spool copy was destroyed"
    v = await cam(session, ep)
    assert v.state == ON_PREM, "it must be retried, not written off"


async def test_a_truncated_archive_copy_keeps_the_spool_copy(client, session, store):
    ep, key = await _landed(session, store)

    class Truncating(type(store)):
        async def copy_to_archive(self, k):
            dst = self.archive / k
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(VIDEO[: len(VIDEO) // 3])
            return str(dst)

    bad = Truncating(store.root, store.archive)
    count, _ = await drain_batch(session, storage=bad)

    assert count == 0
    assert await bad.head(key) is not None, "half a take in the archive freed the whole one"
    v = await cam(session, ep)
    assert v.state == ON_PREM


async def test_an_archive_that_cannot_prove_what_it_holds_frees_nothing(client, session, store):
    """Same rule as confirm(): a checksum the store did not compute is not
    a checksum, and this step is destructive."""
    ep, key = await _landed(session, store)

    class Mute(type(store)):
        async def head_archive(self, k):
            landed = await super().head_archive(k)
            return None if landed is None else Stored(key=k, bytes=landed.bytes, sha256="")

    mute = Mute(store.root, store.archive)
    count, _ = await drain_batch(session, storage=mute)

    assert count == 0
    assert await mute.head(key) is not None
    v = await cam(session, ep)
    assert v.state == ON_PREM


async def test_a_row_the_old_drain_left_behind_is_collected(client, session, store):
    """Before this, `archived` meant copied and the spool copy stayed. Those
    rows are the leak, and the release pass is what collects them - after
    asking the archive again rather than trusting a months-old copy call."""
    ep, key = await _landed(session, store)
    await store.copy_to_archive(key)
    v = await cam(session, ep)
    v.state = ARCHIVED
    v.archived_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    v.freed_at = None
    await session.commit()

    freed, freed_bytes = await release_spool(session)

    assert freed == 1
    assert freed_bytes == len(VIDEO)
    assert await store.head(key) is None
    v = await cam(session, ep)
    assert v.freed_at is not None


async def test_releasing_twice_frees_once(client, session, store):
    ep, key = await _landed(session, store)
    await drain_batch(session)
    freed, _ = await release_spool(session)
    assert freed == 0, "a released spool copy was claimed again"


async def test_the_backlog_says_what_the_spool_is_holding(client, session, store):
    ep, key = await _landed(session, store)

    before = await backlog(session)
    assert before["spool"]["cameras"] == 1
    assert before["spool"]["bytes"] == len(VIDEO)

    await drain_batch(session)

    after = await backlog(session)
    assert after["spool"]["cameras"] == 0, (
        "the spool reports as full after everything in it was archived and freed"
    )
    assert after["spool"]["bytes"] == 0


# -------------------------------------------------------- the retention
#
# The only irreversible thing this service does. It is off by default and
# these tests are mostly about what it refuses to touch.


async def _archived(session, store, archived_at):
    ep, key = await _landed(session, store)
    await drain_batch(session)
    v = await cam(session, ep)
    v.archived_at = archived_at
    await session.commit()
    return ep, key


def test_the_policy_is_ninety_days_and_lives_in_the_code():
    """Decided, and pinned here so it cannot drift quietly.

    It lives in config.py rather than only in .env because .env does not
    travel: when core/ and services/rigs/ lift into the platform team's
    tree, this default goes with them and the environment file does not.
    A retention policy that only exists in an env var is one that silently
    becomes "keep everything" at the handover.
    """
    from core.infrastructure.config import Settings

    assert Settings.model_fields["video_keep_days"].default == 90


async def test_setting_zero_still_means_keep_everything(client, session, store):
    """The way back. Whatever the default is, 0 must always mean never."""
    ep, key = await _archived(session, store, datetime(2020, 1, 1, tzinfo=timezone.utc))

    gone, _ = await expire_archive(session, keep_days=0)

    assert gone == 0
    assert (store.archive / key).exists(), "footage was deleted with no policy set"
    v = await cam(session, ep)
    assert v.state == ARCHIVED


async def test_video_older_than_the_policy_is_deleted(client, session, store):
    ep, key = await _archived(session, store, datetime(2020, 1, 1, tzinfo=timezone.utc))

    gone, gone_bytes = await expire_archive(session, keep_days=90)

    assert gone == 1 and gone_bytes == len(VIDEO)
    assert not (store.archive / key).exists()
    v = await cam(session, ep)
    assert v.state == EXPIRED


async def test_video_inside_the_window_is_kept(client, session, store):
    recent = datetime.now(timezone.utc) - timedelta(days=3)
    ep, key = await _archived(session, store, recent)

    gone, _ = await expire_archive(session, keep_days=90)

    assert gone == 0
    assert (store.archive / key).exists()


async def test_the_episode_itself_survives_being_expired(client, session, store):
    """The facts are the point of the system and cost nothing to keep. The
    video is what costs 2.7 TB a day."""
    ep, _ = await _archived(session, store, datetime(2020, 1, 1, tzinfo=timezone.utc))
    await expire_archive(session, keep_days=90)
    v = await cam(session, ep)

    assert ep.outcome == "saved"
    assert ep.score == 4
    assert ep.duration_secs == 92.0
    assert ep.operator_id == "op-a4"
    assert v.bytes == len(VIDEO), "the measurement should outlive the bytes"


async def test_a_take_still_on_the_spool_is_never_expired(client, session, store):
    """However old the date says it is. Anything not yet archived and
    released is not anybody's idea of expired."""
    ep, key = await _landed(session, store)
    v = await cam(session, ep)
    v.stored_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await session.commit()

    gone, _ = await expire_archive(session, keep_days=1)

    assert gone == 0
    assert await store.head(key) is not None
    v = await cam(session, ep)
    assert v.state == ON_PREM


async def test_expiring_does_not_touch_the_spool_delete(client, session, store):
    """delete() and delete_archived() are two names so that no caller can
    do the irreversible one while meaning the routine one."""
    ep, key = await _archived(session, store, datetime(2020, 1, 1, tzinfo=timezone.utc))
    # The spool copy is already gone - the drain released it - so the only
    # thing left to delete is the archive, which is the point.
    assert await store.head(key) is None
    await expire_archive(session, keep_days=90)
    assert not (store.archive / key).exists()


# --------------------------------------------------------- the ceiling
#
# Only the gateway-upload model reads a body into this process, and until
# it was bounded a single request could ask for as much memory as it liked.
# A presigned PUT never touches this process and is not bounded here.


@asynccontextmanager
async def serving_with(**overrides):
    from httpx import ASGITransport, AsyncClient

    from core.infrastructure.config import Settings, get_settings
    from services.rigs.app import create_app

    base = get_settings()
    settings = Settings(**{**base.model_dump(), **overrides})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def test_an_upload_over_the_ceiling_is_refused_on_its_own_claim(engine, store):
    """Content-Length first, so an oversized upload is refused before a
    byte of it is read rather than after all of it is in memory."""
    async with serving_with(max_video_bytes=1024) as c:
        r = await c.put("/api/storage/RIG-03/ep/front.mp4", content=b"x" * 4096)
        assert r.status_code == 413, r.status_code


async def test_an_upload_over_the_ceiling_is_refused_when_it_claims_nothing(engine, store):
    """A chunked request makes no Content-Length claim at all, so the same
    ceiling has to hold while the body is being read."""
    async def chunks():
        for _ in range(8):
            yield b"x" * 512

    async with serving_with(max_video_bytes=1024) as c:
        r = await c.put("/api/storage/RIG-03/ep/front.mp4", content=chunks())
        assert r.status_code == 413, r.status_code


async def test_an_upload_at_the_ceiling_is_accepted(engine, store):
    """The boundary is a limit, not an off-by-one."""
    async with serving_with(max_video_bytes=1024) as c:
        r = await c.put("/api/storage/RIG-03/ep/front.mp4", content=b"x" * 1024)
        assert r.status_code == 200, r.text
        assert r.json()["bytes"] == 1024


# ------------------------------------------------------- against real S3
#
# Skipped unless MinIO is running. The point of these is not to test
# MinIO - it is to prove the adapter boundary holds, so that swapping
# LocalStorage for S3 changes nothing above core/infrastructure/storage.py.
#
#   minio server .minio-data --address 127.0.0.1:9000

MINIO_EP = "http://127.0.0.1:9000"
MINIO_KEY = "rigsdev"
MINIO_SECRET = "rigsdev12345"


def _minio_up() -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(MINIO_EP + "/minio/health/live", timeout=1) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


minio_only = pytest.mark.skipif(not _minio_up(), reason="MinIO is not running")


@pytest.fixture
def minio():
    import boto3
    from botocore.exceptions import ClientError

    from core.infrastructure.storage import MinioStorage

    c = boto3.client("s3", endpoint_url=MINIO_EP,
                     aws_access_key_id=MINIO_KEY, aws_secret_access_key=MINIO_SECRET)
    for b in ("rigs-video", "rigs-video-archive"):
        try:
            c.create_bucket(Bucket=b)
        except ClientError:
            pass
    s = MinioStorage(MINIO_EP, "rigs-video", MINIO_KEY, MINIO_SECRET)
    storage_mod.configure(s)
    return s


@minio_only
async def test_the_whole_video_path_works_against_real_s3(client, session, minio):
    """The same sequence as the local test, with nothing above the
    storage module changed. If this needs a single conditional anywhere
    else, the boundary is in the wrong place."""
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")

    await minio.put(key, VIDEO)
    result = await confirm(session, str(ep.episode_id), "front", sha256_of(VIDEO), len(VIDEO))
    assert result["safeToDelete"] is True

    count, _ = await drain_batch(session)
    assert count == 1
    v = await cam(session, ep)
    assert v.state == ARCHIVED

    await minio.delete(key)


@minio_only
def test_real_s3_issues_an_absolute_presigned_url(minio):
    """The local stand-in has no presigning, which is exactly the sort of
    difference worth catching before it is a production assumption."""
    t = minio.upload_target(object_key(RIG, "abc", "front"))
    assert t.url.startswith("http"), "bulk bytes must not come through the application"
    assert t.expires_in_secs and t.expires_in_secs > 0
