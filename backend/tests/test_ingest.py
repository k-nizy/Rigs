"""Ingest, the cursor, and the property the whole design rests on.

The headline test is `test_replaying_a_whole_shift_changes_nothing`. If
that holds, the uploader on the rig can retry blindly, forever, without
ever asking whether its last attempt landed - which is what keeps its
retry logic twenty lines instead of two hundred.
"""

import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.conftest import FIXTURES

RIG = "RIG-03"


def envelope(seq: int, event: str = "episode_saved", bucket: str = "episodes", **over) -> dict:
    """One valid envelope, in the shape packages/schema defines."""
    base = {
        "eventId": str(uuid.uuid4()),
        "seq": seq,
        "at": (datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc) + timedelta(seconds=seq)).isoformat(),
        "rigId": RIG,
        "shiftDate": "2026-08-24",
        "shiftLabel": "Morning",
        "turnFrom": "09:00",
        "operatorId": "op-a4",
        "bucket": bucket,
        "event": event,
        "data": {
            "episodeId": str(uuid.uuid4()),
            "durationSecs": 92,
            "score": 4,
        },
    }
    base.update(over)
    return base


def a_shift(n: int = 40) -> list[dict]:
    """A plausible run of events, the sort one operator produces."""
    out = []
    for i in range(n):
        if i % 10 == 9:
            out.append(envelope(i, "stint_ended", "rig_productivity_blocks", data={
                "episodes": 9, "recordedSecs": 2140, "assignedSecs": 2700,
                "faultSecs": 0, "downSecs": 180,
            }))
        else:
            out.append(envelope(i))
    return out


# --------------------------------------------------------------- cursor

async def test_cursor_of_an_unknown_rig_is_minus_one(client):
    r = await client.get(f"/api/rigs/{RIG}/cursor")
    assert r.status_code == 200
    assert r.json() == {"rigId": RIG, "seq": -1}


async def test_cursor_follows_the_highest_seq_accepted(client):
    await client.post(f"/api/rigs/{RIG}/events", json={"events": a_shift(5)})
    r = await client.get(f"/api/rigs/{RIG}/cursor")
    assert r.json()["seq"] == 4


# --------------------------------------------------------------- ingest

async def test_a_batch_is_accepted_and_counted(client):
    r = await client.post(f"/api/rigs/{RIG}/events", json={"events": a_shift(12)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 12
    assert body["duplicates"] == 0
    assert body["cursor"] == 11


async def test_replaying_a_whole_shift_changes_nothing(client, session):
    """The property the uploader depends on.

    Send a shift, send it again, send it a third time. The rows must not
    move. This is what makes retry-forever safe.
    """
    from core.domains.rig_events.repository import RigEventRepository

    shift = a_shift(40)

    first = (await client.post(f"/api/rigs/{RIG}/events", json={"events": shift})).json()
    assert first["accepted"] == 40 and first["duplicates"] == 0

    after_first = await RigEventRepository(session).count_for_rig(RIG)
    assert after_first == 40

    for attempt in range(2, 4):
        again = (await client.post(f"/api/rigs/{RIG}/events", json={"events": shift})).json()
        assert again["accepted"] == 0, f"attempt {attempt} inserted rows it should have skipped"
        assert again["duplicates"] == 40
        assert again["cursor"] == first["cursor"]

    assert await RigEventRepository(session).count_for_rig(RIG) == 40, (
        "resending a shift changed the ledger"
    )


async def test_a_partial_resend_lands_only_the_new_tail(client, session):
    """What actually happens after a network drop: the rig re-sends from
    its cursor, overlapping whatever the server already holds."""
    from core.domains.rig_events.repository import RigEventRepository

    shift = a_shift(20)
    await client.post(f"/api/rigs/{RIG}/events", json={"events": shift[:12]})

    overlap = (await client.post(f"/api/rigs/{RIG}/events", json={"events": shift[8:]})).json()
    assert overlap["accepted"] == 8, "only the tail should be new"
    assert overlap["duplicates"] == 4
    assert await RigEventRepository(session).count_for_rig(RIG) == 20


async def test_events_survive_verbatim(client, session):
    """Nothing is discarded at ingest. A projection built later has to be
    able to read exactly what the rig sent."""
    from core.domains.rig_events.repository import RigEventRepository

    sent = envelope(0, "stint_ended", "rig_productivity_blocks", data={
        "episodes": 14, "recordedSecs": 2140, "assignedSecs": 2700,
        "faultSecs": 0, "downSecs": 180,
    })
    await client.post(f"/api/rigs/{RIG}/events", json={"events": [sent]})

    rows = await RigEventRepository(session).since(RIG, -1)
    assert len(rows) == 1
    assert rows[0].envelope["data"] == sent["data"]
    assert rows[0].envelope["eventId"] == sent["eventId"]


# ------------------------------------------------------------ rejection

async def test_a_batch_posted_to_the_wrong_rig_is_rejected_whole(client, session):
    """One bad envelope rejects the batch. A half-accepted batch is worse
    than a rejected one - the same rule the schedule push already keeps."""
    from core.domains.rig_events.repository import RigEventRepository

    batch = a_shift(5)
    batch[3]["rigId"] = "RIG-07"

    r = await client.post(f"/api/rigs/{RIG}/events", json={"events": batch})
    assert r.status_code == 422
    assert await RigEventRepository(session).count_for_rig(RIG) == 0, (
        "a rejected batch left rows behind"
    )


@pytest.mark.parametrize(
    "bad,why",
    [
        ({"eventId": "not-a-uuid"}, "ingest dedupes on eventId"),
        ({"seq": -1}, "seq is the cursor"),
        ({"at": "2026-08-24T09:14:02"}, "a timestamp with no zone is unsortable"),
        ({"bucket": "sessions"}, "episode_saved does not live in sessions"),
        ({"event": "episode_uploaded"}, "unknown event"),
        ({"shiftDate": "24-08-2026"}, "shiftDate must be YYYY-MM-DD"),
    ],
)
async def test_malformed_envelopes_are_refused(client, bad, why):
    ev = envelope(0)
    ev.update(bad)
    r = await client.post(f"/api/rigs/{RIG}/events", json={"events": [ev]})
    assert r.status_code == 422, f"{why}: should have been refused, got {r.status_code}"


async def test_an_empty_batch_is_refused(client):
    r = await client.post(f"/api/rigs/{RIG}/events", json={"events": []})
    assert r.status_code == 422


# ------------------------------------------------------- the contract

def _fixture_files():
    return sorted(FIXTURES.glob("*.json"))


def test_there_are_fixtures_to_check_against():
    assert _fixture_files(), f"no fixtures found at {FIXTURES}"


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.stem)
def test_the_python_side_accepts_every_fixture(path):
    """The cross-language guarantee.

    packages/schema/event.js validates these in JavaScript and this model
    validates them in Python. If either stops accepting one, the two
    halves of the contract have parted company and CI says so on the side
    that broke.
    """
    from core.domains.rig_events.schema import EventEnvelope

    EventEnvelope.model_validate(json.loads(path.read_text(encoding="utf-8")))


async def test_a_standby_event_is_accepted_with_no_turn_or_operator(client):
    """A rig with nothing scheduled still reports its shift check."""
    ev = envelope(0, "shift_check", "rig_shift_checks",
                  turnFrom=None, operatorId=None, data={"outcome": "passed_early"})
    r = await client.post(f"/api/rigs/{RIG}/events", json={"events": [ev]})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1


# ------------------------------------------------------------ schedules

async def test_an_event_is_linked_to_the_schedule_in_force(client, session):
    """Every row can be joined back to the exact schedule version that was
    pushed when it happened, so attribution survives a re-push."""
    from core.domains.rig_events.repository import RigEventRepository
    from core.domains.schedules.model import Schedule

    push_id = uuid.uuid4()
    session.add(Schedule(
        push_id=push_id,
        pushed_at=datetime(2026, 8, 24, 7, 31, tzinfo=timezone.utc),
        rig_id=RIG,
        shift_date=date(2026, 8, 24),
        shift_label="Morning",
        payload={"rigId": RIG, "turns": []},
    ))
    await session.commit()

    await client.post(f"/api/rigs/{RIG}/events", json={"events": [envelope(0)]})

    rows = await RigEventRepository(session).since(RIG, -1)
    assert rows[0].push_id == push_id


async def test_an_event_without_a_schedule_is_still_kept(client, session):
    """The event is the fact; the link can be filled in later. Rejecting it
    would lose a real thing that happened because a push was late."""
    from core.domains.rig_events.repository import RigEventRepository

    r = await client.post(f"/api/rigs/{RIG}/events", json={"events": [envelope(0)]})
    assert r.status_code == 200
    rows = await RigEventRepository(session).since(RIG, -1)
    assert rows[0].push_id is None


# ------------------------------------------------------------ heartbeat

async def test_heartbeat_reports_clock_skew(client):
    """Skew is measurable for free by storing the rig's clock beside the
    server's. Twelve rigs disagreeing about the time makes the event
    stream unsortable and mis-attributes every handover."""
    behind = datetime.now(timezone.utc) - timedelta(seconds=90)
    r = await client.post(f"/api/rigs/{RIG}/heartbeat", json={"at": behind.isoformat()})
    assert r.status_code == 200
    assert 85 < r.json()["skewSecs"] < 95


# -------------------------------------------------------- schedule read

async def test_the_schedule_is_returned_verbatim(client, session):
    """Stored opaque, read opaque. The rotation is computed in one shared
    JavaScript file and this server is not it."""
    from core.domains.schedules.model import Schedule

    payload = {"rigId": RIG, "group": "A", "task": "Box transfer", "turns": [{"from": "08:00"}]}
    session.add(Schedule(
        push_id=uuid.uuid4(),
        pushed_at=datetime(2026, 8, 24, 7, 31, tzinfo=timezone.utc),
        rig_id=RIG, shift_date=date(2026, 8, 24), shift_label="Morning",
        payload=payload,
    ))
    await session.commit()

    r = await client.get(f"/api/rigs/{RIG}/schedule")
    assert r.status_code == 200
    assert r.json() == payload


async def test_a_rig_nothing_was_pushed_to_gets_a_404(client):
    r = await client.get("/api/rigs/RIG-11/schedule")
    assert r.status_code == 404


async def test_a_cursor_of_zero_is_a_real_cursor(client):
    """seq 0 is the first event of a rig's life.

    Treating it as "nothing held" - which `or -1` does, because 0 is
    falsy - would tell a rig that had sent exactly one event that the
    server had none, and it would resend that event forever.
    """
    ev = envelope(0)
    r = await client.post(f"/api/rigs/{RIG}/events", json={"events": [ev]})
    assert r.status_code == 200
    assert r.json()["cursor"] == 0, "the cursor collapsed to -1 on a falsy seq"

    c = await client.get(f"/api/rigs/{RIG}/cursor")
    assert c.json()["seq"] == 0
