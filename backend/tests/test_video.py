"""The video path, minus the transport.

The one thing worth proving here is the rule that lets a rig delete its
own copy: the server says yes only when what landed matches what the rig
says it sent. Everything else in this path is a retry; that step is the
one that must never be optimistic, because past it a byte is gone.
"""

import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from core.domains.episodes.model import Episode
from core.infrastructure import storage as storage_mod
from core.infrastructure.storage import LocalStorage, object_key, sha256_of
from core.workflows.video import (
    ARCHIVED, MISSING, ON_PREM, PENDING,
    VideoError, backlog, confirm, drain_batch, mark_missing, where_to_put,
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
    ep = await an_episode(session)
    assert ep.video_state == PENDING
    assert ep.video_bytes is None


async def test_the_rig_is_told_where_to_put_it(client, session, store):
    ep = await an_episode(session)
    where = await where_to_put(session, str(ep.episode_id), "front")
    assert where["key"] == object_key(RIG, str(ep.episode_id), "front")
    assert where["method"] == "PUT"

    await session.refresh(ep)
    assert ep.video_state == PENDING, "asking where to put it is not the same as having it"


async def test_confirming_a_matching_upload_permits_deletion(client, session, store):
    """The only step that matters. Past this the rig deletes its copy."""
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")
    await store.put(key, VIDEO)

    result = await confirm(session, str(ep.episode_id), "front", sha256_of(VIDEO), len(VIDEO))
    assert result["safeToDelete"] is True
    assert result["bytes"] == len(VIDEO)

    await session.refresh(ep)
    assert ep.video_state == ON_PREM
    assert ep.video_sha256 == sha256_of(VIDEO)
    assert ep.video_stored_at is not None


async def test_a_wrong_checksum_is_refused_and_the_rig_keeps_its_copy(client, session, store):
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")
    await store.put(key, VIDEO)

    with pytest.raises(VideoError, match="checksum"):
        await confirm(session, str(ep.episode_id), "front", "0" * 64, len(VIDEO))

    await session.refresh(ep)
    assert ep.video_state == PENDING, (
        "a mismatch must leave the episode pending - the rig has the only good copy"
    )


async def test_a_truncated_upload_is_refused(client, session, store):
    """The commonest real failure: a connection dropped mid-transfer."""
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")
    await store.put(key, VIDEO[: len(VIDEO) // 2])

    with pytest.raises(VideoError, match="bytes"):
        await confirm(session, str(ep.episode_id), "front", sha256_of(VIDEO), len(VIDEO))

    await session.refresh(ep)
    assert ep.video_state == PENDING


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
    await mark_missing(session, str(ep.episode_id), "discarded")
    await session.refresh(ep)
    assert ep.video_state == MISSING


# ---------------------------------------------------------------- drain

async def test_the_drain_moves_landed_video_to_the_archive(client, session, store):
    ep = await an_episode(session)
    key = object_key(RIG, str(ep.episode_id), "front")
    await store.put(key, VIDEO)
    await confirm(session, str(ep.episode_id), "front", sha256_of(VIDEO), len(VIDEO))

    count, keys = await drain_batch(session)
    assert count == 1 and keys == [key]

    await session.refresh(ep)
    assert ep.video_state == ARCHIVED
    assert ep.video_archived_at is not None
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
    assert b["byState"][ON_PREM]["episodes"] == 1
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
    assert m["episodes"] == 1
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

    await session.refresh(ep)
    assert ep.video_state == ON_PREM


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
    await session.refresh(ep)
    assert ep.video_state == PENDING, "the rig must keep its copy"


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
    await session.refresh(ep)
    assert ep.video_state == ARCHIVED

    await minio.delete(key)


@minio_only
def test_real_s3_issues_an_absolute_presigned_url(minio):
    """The local stand-in has no presigning, which is exactly the sort of
    difference worth catching before it is a production assumption."""
    t = minio.upload_target(object_key(RIG, "abc", "front"))
    assert t.url.startswith("http"), "bulk bytes must not come through the application"
    assert t.expires_in_secs and t.expires_in_secs > 0
