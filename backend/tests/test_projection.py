"""The ledger becoming facts, and the property that makes it worth doing.

`test_replay_rebuilds_identical_facts` is the reason the ledger exists.
If it holds, a projection bug found in six months is an afternoon's work
rather than a year of lost data.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from core.domains.episodes.model import Episode
from core.domains.rig_downtime_events.model import RigDowntimeEvent
from core.domains.rig_events.model import RigEvent
from core.domains.rig_productivity_blocks.model import RigProductivityBlock
from core.domains.rig_shift_checks.model import RigShiftCheck
from core.domains.sessions.model import Session
from core.rules.efficiency import Stint, chargeable_secs, efficiency, unaccounted_secs
from core.workflows.projection import project_batch, reset_projections

RIG = "RIG-03"
T0 = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)


def env(seq, event, bucket, data, turn="09:00", op="op-a4"):
    return {
        "eventId": str(uuid.uuid4()),
        "seq": seq,
        "at": (T0 + timedelta(seconds=seq * 30)).isoformat(),
        "rigId": RIG,
        "shiftDate": "2026-08-24",
        "shiftLabel": "Morning",
        "turnFrom": turn,
        "operatorId": op,
        "bucket": bucket,
        "event": event,
        "data": data,
    }


def a_stint() -> list[dict]:
    """One operator's turn, as the rig would actually file it."""
    ep1, ep2, ep3 = (str(uuid.uuid4()) for _ in range(3))
    return [
        env(0, "shift_check", "rig_shift_checks", {"outcome": "passed"}),
        env(1, "episode_saved", "episodes", {"episodeId": ep1, "durationSecs": 92, "score": 4}),
        env(2, "episode_discarded", "episodes", {"episodeId": ep2, "durationSecs": 11}),
        env(3, "episode_saved", "episodes", {"episodeId": ep3, "durationSecs": 105, "score": 5}),
        env(4, "rig_down", "rig_downtime_events",
            {"issue": "Gripper broken", "issuePath": "Gripper broken",
             "needsManager": False, "chargedTo": "previous_operator"}),
        env(5, "rig_up", "rig_downtime_events", {"downSecs": 180}),
        env(6, "stint_ended", "rig_productivity_blocks",
            {"episodes": 2, "recordedSecs": 197, "assignedSecs": 2700,
             "faultSecs": 0, "downSecs": 180}),
    ]


async def ingest(client, events):
    r = await client.post(f"/api/rigs/{RIG}/events", json={"events": events})
    assert r.status_code == 200, r.text
    return r.json()


async def counts(session) -> dict[str, int]:
    out = {}
    for name, model in (
        ("episodes", Episode), ("checks", RigShiftCheck),
        ("downtime", RigDowntimeEvent), ("blocks", RigProductivityBlock),
        ("sessions", Session),
    ):
        rows = await session.execute(select(func.count()).select_from(model))
        out[name] = rows.scalar() or 0
    return out


# ------------------------------------------------------------ projecting

async def test_a_stint_becomes_the_five_facts(client, session):
    await ingest(client, a_stint())
    count, _ = await project_batch(session)
    assert count == 7

    c = await counts(session)
    assert c == {"episodes": 3, "checks": 1, "downtime": 1, "blocks": 1, "sessions": 1}


async def test_a_discarded_take_is_a_row_not_an_absence(client, session):
    """A floor where nothing is ever discarded is a floor worth asking
    about, so the discard has to be visible."""
    await ingest(client, a_stint())
    await project_batch(session)

    rows = await session.execute(select(Episode).where(Episode.outcome == "discarded"))
    discarded = rows.scalars().all()
    assert len(discarded) == 1
    assert discarded[0].score is None, "a thrown-away take was never scored"


async def test_downtime_closes_and_keeps_who_it_was_charged_to(client, session):
    await ingest(client, a_stint())
    await project_batch(session)

    row = (await session.execute(select(RigDowntimeEvent))).scalar_one()
    assert row.up_at is not None, "the outage never closed"
    assert row.down_secs == 180, "the rig measured this against a monotonic clock; prefer it"
    assert row.charged_to == "previous_operator", (
        "the app says this on the wall - it has to survive into the data"
    )


async def test_the_stint_carries_measurements_and_no_ratio(client, session):
    await ingest(client, a_stint())
    await project_batch(session)

    block = (await session.execute(select(RigProductivityBlock))).scalar_one()
    assert block.recorded_secs == 197
    assert block.assigned_secs == 2700
    assert block.down_secs == 180
    assert not hasattr(block, "efficiency"), (
        "a stored percentage cannot be corrected without re-running the floor"
    )

    # The ratio is a read-time computation, from one definition.
    s = Stint(block.recorded_secs, block.assigned_secs, block.fault_secs, block.down_secs)
    assert chargeable_secs(s) == 2520
    assert efficiency(s) == pytest.approx(197 / 2520)


async def test_a_session_opens_on_the_first_event_and_closes_at_the_handover(client, session):
    await ingest(client, a_stint())
    await project_batch(session)

    sess = (await session.execute(select(Session))).scalar_one()
    assert sess.started_at == T0, "opened by the first event under the turn"
    assert sess.ended_by == "handover", "a stint reaching its boundary is the ordinary end"
    assert sess.operator_id == "op-a4"


async def test_two_operators_get_two_sessions(client, session):
    first = a_stint()
    second = [env(10 + i, e["event"], e["bucket"], e["data"], turn="09:45", op="op-a1")
              for i, e in enumerate(a_stint())]
    await ingest(client, first + second)
    await project_batch(session)

    rows = (await session.execute(select(Session))).scalars().all()
    assert len(rows) == 2
    assert {r.operator_id for r in rows} == {"op-a4", "op-a1"}


async def test_an_operator_ending_early_is_recorded_as_such(client, session):
    events = a_stint()[:5] + [env(6, "session_ended", "sessions", {"endedBy": "operator"})]
    await ingest(client, events)
    await project_batch(session)

    sess = (await session.execute(select(Session))).scalar_one()
    assert sess.ended_by == "operator", "ending with the rig down is not a handover"


# ------------------------------------------------------------ idempotency

async def test_projecting_twice_does_not_double_anything(client, session):
    await ingest(client, a_stint())
    first, _ = await project_batch(session)
    before = await counts(session)

    again, _ = await project_batch(session)
    assert again == 0, "already-projected rows were claimed a second time"
    assert await counts(session) == before


async def test_a_rig_cannot_be_doubly_down(client, session):
    """Pressing through the issue tree twice is one outage."""
    events = [
        env(0, "rig_down", "rig_downtime_events",
            {"issue": "Gripper broken", "needsManager": False, "chargedTo": "rig"}),
        env(1, "rig_down", "rig_downtime_events",
            {"issue": "Camera mount", "needsManager": False, "chargedTo": "rig"}),
        env(2, "rig_up", "rig_downtime_events", {"downSecs": 60}),
    ]
    await ingest(client, events)
    await project_batch(session)

    rows = (await session.execute(select(RigDowntimeEvent))).scalars().all()
    assert len(rows) == 1, "the second rig_down opened a second outage"
    assert rows[0].issue == "Gripper broken"


async def test_a_rig_up_with_nothing_open_is_ignored(client, session):
    """Replay can start mid-ledger; the matching rig_down may not be in
    range. That is not an error."""
    await ingest(client, [env(0, "rig_up", "rig_downtime_events", {"downSecs": 60})])
    count, done = await project_batch(session)
    assert count == 1
    assert "ignored" in done[0]
    assert (await counts(session))["downtime"] == 0


# ---------------------------------------------------------------- replay

async def test_replay_rebuilds_identical_facts(client, session):
    """The reason the ledger exists.

    Wipe every fact table, run the whole ledger through again, and the
    result must be identical. This is what makes a projection bug found in
    six months survivable rather than a year of lost data.
    """
    shift = []
    for turn, op in (("09:00", "op-a4"), ("09:45", "op-a1"), ("10:30", "op-a2")):
        for i, e in enumerate(a_stint()):
            shift.append(env(len(shift), e["event"], e["bucket"], e["data"], turn=turn, op=op))
    await ingest(client, shift)

    projected, _ = await project_batch(session, limit=1000)
    assert projected == len(shift)

    def snapshot(rows):
        return sorted(rows)

    before_counts = await counts(session)
    before_eps = snapshot([
        (str(e.episode_id), e.outcome, e.score, e.duration_secs, e.operator_id)
        for e in (await session.execute(select(Episode))).scalars().all()
    ])
    before_blocks = snapshot([
        (b.operator_id, b.turn_from, b.recorded_secs, b.assigned_secs, b.fault_secs, b.down_secs)
        for b in (await session.execute(select(RigProductivityBlock))).scalars().all()
    ])
    before_sessions = snapshot([
        (s.operator_id, s.turn_from, s.ended_by) for s in
        (await session.execute(select(Session))).scalars().all()
    ])

    # --- correct the reading, wipe, rebuild
    await reset_projections(session)
    assert await counts(session) == {"episodes": 0, "checks": 0, "downtime": 0, "blocks": 0, "sessions": 0}

    again, _ = await project_batch(session, limit=1000)
    assert again == len(shift), "the whole ledger should be outstanding again"

    assert await counts(session) == before_counts
    assert snapshot([
        (str(e.episode_id), e.outcome, e.score, e.duration_secs, e.operator_id)
        for e in (await session.execute(select(Episode))).scalars().all()
    ]) == before_eps
    assert snapshot([
        (b.operator_id, b.turn_from, b.recorded_secs, b.assigned_secs, b.fault_secs, b.down_secs)
        for b in (await session.execute(select(RigProductivityBlock))).scalars().all()
    ]) == before_blocks
    assert snapshot([
        (s.operator_id, s.turn_from, s.ended_by) for s in
        (await session.execute(select(Session))).scalars().all()
    ]) == before_sessions


async def test_the_ledger_is_untouched_by_a_replay(client, session):
    """Replay rebuilds the reading, never the events."""
    await ingest(client, a_stint())
    await project_batch(session)
    before = (await session.execute(select(func.count()).select_from(RigEvent))).scalar()

    await reset_projections(session)
    await project_batch(session)

    after = (await session.execute(select(func.count()).select_from(RigEvent))).scalar()
    assert after == before == 7


# ----------------------------------------------------------- the ratio

@pytest.mark.parametrize(
    "stint,expected,why",
    [
        (Stint(0, 0, 0, 0), 0.0, "no chargeable time is not a divide by zero"),
        (Stint(100, 100, 0, 0), 1.0, "every chargeable second recorded"),
        (Stint(50, 100, 0, 0), 0.5, "half"),
        (Stint(50, 100, 20, 30), 1.0, "fault and downtime come out of the denominator"),
        (Stint(10, 100, 60, 40), 0.0, "a stint that was entirely fault and downtime"),
        (Stint(999, 100, 0, 0), 1.0, "clamped - never above one"),
    ],
)
def test_efficiency_matches_the_definition_on_the_wall(stint, expected, why):
    assert efficiency(stint) == pytest.approx(expected), why


def test_unaccounted_seconds_are_visible():
    """Invariant I2: every second lands in exactly one bucket. Reset and
    idle have no column, so they are the remainder - and a negative
    remainder means two buckets overlapped and a second was counted
    twice."""
    s = Stint(recorded_secs=100, assigned_secs=300, fault_secs=50, down_secs=50)
    assert unaccounted_secs(s) == 100      # reset, review and idle
    overlapping = Stint(recorded_secs=300, assigned_secs=300, fault_secs=50, down_secs=50)
    assert unaccounted_secs(overlapping) < 0, "an impossible stint should read as impossible"
