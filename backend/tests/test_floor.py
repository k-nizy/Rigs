"""The floor sweep: noticing what did not happen.

Two halves. The pure rules are tested by writing down a situation, with
no database at all. The sweep is tested against real rows, because the
thing worth proving about it is reconciliation - that an alert opens
once, refreshes while it holds, and closes by itself when it stops being
true.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from core.domains.alerts.model import Alert
from core.domains.alerts.repository import AlertRepository
from core.domains.rig_events.model import RigEvent
from core.domains.rig_status.repository import RigStatusRepository
from core.domains.schedules.model import Schedule
from core.rules import floor as rules
from core.workflows.floor import floor_state, operator_efficiency, sweep
from core.workflows.projection import project_batch

RIG = "RIG-03"
DAY = date(2026, 8, 24)
UTC = timezone.utc


def at(h, m=0, s=0):
    return datetime(2026, 8, 24, h, m, s, tzinfo=UTC)


PAYLOAD = {
    "rigId": RIG,
    "group": "A",
    "task": "Box transfer - bin to conveyor",
    "shift": {"label": "Morning", "date": "2026-08-24", "start": "08:00", "end": "16:00"},
    "turns": [
        {"from": "08:00", "to": "08:45", "minutes": 45,
         "operator": {"id": "op-a1", "name": "Aleksandr Petrov"},
         "relievedBy": "Mei Chen", "theyGoTo": "Break"},
        {"from": "08:45", "to": "09:30", "minutes": 45,
         "operator": {"id": "op-a2", "name": "Mei Chen"},
         "relievedBy": "Tomas Rivera", "theyGoTo": "Think"},
    ],
}


async def push_schedule(session, payload=None):
    session.add(Schedule(
        push_id=uuid.uuid4(), pushed_at=at(7, 31), rig_id=RIG,
        shift_date=DAY, shift_label="Morning", payload=payload or PAYLOAD,
    ))
    await session.commit()


# ==================================================== the rules, no database

class TestSilence:
    def test_a_rig_never_heard_from_is_reported(self):
        a = rules.rig_silent(RIG, None, at(9), 180)
        assert a and a.kind == rules.RIG_SILENT
        assert "never" in a.detail

    def test_a_rig_heard_from_recently_is_fine(self):
        assert rules.rig_silent(RIG, at(9, 0, 0), at(9, 1, 0), 180) is None

    def test_a_rig_past_the_threshold_is_reported(self):
        a = rules.rig_silent(RIG, at(9, 0, 0), at(9, 5, 0), 180)
        assert a is not None
        assert "300s" in a.detail

    def test_the_key_is_stable_so_sweeping_twice_opens_one_alert(self):
        first = rules.rig_silent(RIG, at(9), at(9, 10), 180)
        later = rules.rig_silent(RIG, at(9), at(9, 20), 180)
        assert first.key == later.key
        assert first.detail != later.detail, "the number should move even though the key does not"


class TestIdle:
    def test_standby_is_not_idleness(self):
        """Nothing scheduled means nobody is missing."""
        assert rules.rig_idle(RIG, None, None, None, at(3), 300) is None

    def test_a_turn_just_started_is_within_grace(self):
        assert rules.rig_idle(RIG, "08:00", at(8), None, at(8, 2), 300) is None

    def test_a_turn_with_nothing_happening_is_reported(self):
        a = rules.rig_idle(RIG, "08:00", at(8), None, at(8, 10), 300)
        assert a and a.kind == rules.RIG_IDLE
        assert "08:00" in a.key, "keyed on the turn, so one alert per turn"

    def test_recent_activity_clears_it(self):
        assert rules.rig_idle(RIG, "08:00", at(8), at(8, 9), at(8, 10), 300) is None

    def test_activity_before_the_turn_does_not_count(self):
        """The previous operator's last episode says nothing about whether
        this one turned up."""
        a = rules.rig_idle(RIG, "08:45", at(8, 45), at(8, 40), at(8, 55), 300)
        assert a is not None


class TestRepeatFault:
    def test_one_bad_shift_is_not_a_pattern(self):
        assert rules.repeat_fault(RIG, "Camera", 2, 3) is None

    def test_the_same_subsystem_across_shifts_is(self):
        a = rules.repeat_fault(RIG, "Camera", 3, 3)
        assert a and "Camera" in a.detail and a.kind == rules.REPEAT_FAULT


class TestOverran:
    def test_a_stint_inside_its_boundary_is_not_an_overrun(self):
        assert rules.turn_overran(RIG, "08:00", "08:45", at(8, 44), at(8, 45)) is None

    def test_a_stint_past_its_boundary_is(self):
        a = rules.turn_overran(RIG, "08:00", "08:45", at(8, 46, 30), at(8, 45))
        assert a and "90s" in a.detail


class TestTurnBounds:
    def test_an_ordinary_turn(self):
        start, end = rules.turn_bounds(DAY, {"from": "08:00", "to": "08:45"}, UTC)
        assert (start, end) == (at(8), at(8, 45))

    def test_a_turn_that_crosses_midnight(self):
        """The Night shift does this every single day."""
        start, end = rules.turn_bounds(DAY, {"from": "23:30", "to": "00:15"}, UTC)
        assert start == at(23, 30)
        assert end == at(23, 30) + timedelta(minutes=45)
        assert end.day == DAY.day + 1

    def test_the_turn_in_progress_is_found(self):
        found = rules.turn_in_progress(PAYLOAD, DAY, at(9), UTC)
        assert found and found["turn"]["from"] == "08:45"

    def test_outside_the_shift_there_is_no_turn(self):
        assert rules.turn_in_progress(PAYLOAD, DAY, at(3), UTC) is None

    def test_the_boundary_belongs_to_the_turn_starting(self):
        """08:45 exactly is the second turn, not the first. Both would be
        defensible; picking one and testing it is what matters."""
        found = rules.turn_in_progress(PAYLOAD, DAY, at(8, 45), UTC)
        assert found["turn"]["from"] == "08:45"


# ======================================================= the sweep, with rows

async def test_a_rig_pushed_to_but_never_heard_from_is_flagged(client, session):
    await push_schedule(session)
    result = await sweep(session, now=at(9))
    assert result["opened"] >= 1

    rows = await AlertRepository(session).open_alerts()
    kinds = {a.kind for a in rows}
    assert rules.RIG_SILENT in kinds, "a rig missing from the board is the failure this catches"


async def test_a_heartbeat_clears_the_silence(client, session):
    await push_schedule(session)
    await sweep(session, now=at(9))
    assert any(a.kind == rules.RIG_SILENT for a in await AlertRepository(session).open_alerts())

    await RigStatusRepository(session).beat(RIG, at(9, 1), at(9, 1), 0.0)
    await session.commit()

    await sweep(session, now=at(9, 2))
    assert not any(
        a.kind == rules.RIG_SILENT for a in await AlertRepository(session).open_alerts()
    ), "the alert should close by itself once the condition stops holding"


async def test_the_heartbeat_route_records_it(client, session):
    """A heartbeat nobody stored cannot be missed later, and being missed
    is the entire point of it."""
    # Measured against the real clock, not the fixture's: skew is the
    # difference between the rig's clock and the server's, and a rig
    # running ahead gives a negative one, which is a real thing to report.
    behind = datetime.now(UTC) - timedelta(seconds=90)
    r = await client.post(f"/api/rigs/{RIG}/heartbeat", json={"at": behind.isoformat()})
    assert r.status_code == 200
    assert 85 < r.json()["skewSecs"] < 95

    statuses = await RigStatusRepository(session).everything()
    assert [s.rig_id for s in statuses] == [RIG]
    assert 85 < statuses[0].skew_secs < 95, "the skew has to be stored, not just answered"
    assert statuses[0].rig_clock_at is not None


async def test_sweeping_twice_does_not_open_two_alerts(client, session):
    await push_schedule(session)
    first = await sweep(session, now=at(9))
    second = await sweep(session, now=at(9, 0, 30))

    assert second["opened"] == 0, "the same situation opened a second alert"
    rows = await session.execute(select(Alert).where(Alert.resolved_at.is_(None)))
    keys = [a.key for a in rows.scalars().all()]
    assert len(keys) == len(set(keys)), "duplicate open alerts for one key"


async def test_an_open_alert_keeps_its_detail_current(client, session):
    await push_schedule(session)
    await RigStatusRepository(session).beat(RIG, at(9), at(9), 0.0)
    await session.commit()

    await sweep(session, now=at(9, 10))
    first = (await AlertRepository(session).open_keys())[f"{rules.RIG_SILENT}:{RIG}"].detail

    await sweep(session, now=at(9, 30))
    later = (await AlertRepository(session).open_keys())[f"{rules.RIG_SILENT}:{RIG}"].detail

    assert first != later, "a stale number on the board is worse than none"


async def test_a_silent_rig_is_not_also_reported_idle(client, session):
    """One cause, one alert. A rig nobody can hear is not separately
    guilty of doing nothing."""
    await push_schedule(session)
    await sweep(session, now=at(9))
    kinds = {a.kind for a in await AlertRepository(session).open_alerts()}
    assert rules.RIG_SILENT in kinds
    assert rules.RIG_IDLE not in kinds


async def test_a_live_rig_doing_nothing_in_its_turn_is_idle(client, session):
    await push_schedule(session)
    await RigStatusRepository(session).beat(RIG, at(9, 9), at(9, 9), 0.0)
    await session.commit()

    await sweep(session, now=at(9, 10), idle_grace_secs=300)
    kinds = {a.kind for a in await AlertRepository(session).open_alerts()}
    assert rules.RIG_IDLE in kinds, "the 08:45 turn has been running with nothing happening"


async def test_an_open_outage_shows_on_the_board_and_closes_with_the_rig(client, session):
    await push_schedule(session)

    def env(seq, event, data):
        return {
            "eventId": str(uuid.uuid4()), "seq": seq, "at": at(9, seq).isoformat(),
            "rigId": RIG, "shiftDate": "2026-08-24", "shiftLabel": "Morning",
            "turnFrom": "08:45", "operatorId": "op-a2",
            "bucket": "rig_downtime_events", "event": event, "data": data,
        }

    await client.post(f"/api/rigs/{RIG}/events", json={"events": [
        env(0, "rig_down", {"issue": "Gripper broken", "needsManager": True, "chargedTo": "rig"}),
    ]})
    await project_batch(session)
    await sweep(session, now=at(9, 20))

    down = [a for a in await AlertRepository(session).open_alerts() if a.kind == rules.RIG_DOWN]
    assert len(down) == 1
    assert "manager needed" in down[0].detail

    await client.post(f"/api/rigs/{RIG}/events", json={"events": [
        env(1, "rig_up", {"downSecs": 600}),
    ]})
    await project_batch(session)
    await sweep(session, now=at(9, 30))

    assert not [a for a in await AlertRepository(session).open_alerts() if a.kind == rules.RIG_DOWN]


# ============================================================= floor state

async def test_the_board_shows_every_rig_the_desk_pushed_to(client, session):
    await push_schedule(session)
    state = await floor_state(session, now=at(9))

    assert [r["rigId"] for r in state["rigs"]] == [RIG]
    rig = state["rigs"][0]
    assert rig["task"] == "Box transfer - bin to conveyor"
    assert rig["operator"]["name"] == "Mei Chen", "read from the payload, never derived"
    assert rig["turnFrom"] == "08:45"
    assert rig["secondsLeft"] == 30 * 60


async def test_the_board_reports_last_seen_and_clock_skew(client, session):
    await push_schedule(session)
    await RigStatusRepository(session).beat(RIG, at(9), at(8, 59, 0), 60.0)
    await session.commit()

    rig = (await floor_state(session, now=at(9, 1)))["rigs"][0]
    assert rig["lastSeenAt"] is not None
    assert rig["skewSecs"] == 60.0


async def test_the_board_carries_open_alerts_per_rig(client, session):
    await push_schedule(session)
    await sweep(session, now=at(9))
    rig = (await floor_state(session, now=at(9)))["rigs"][0]
    assert rig["alerts"] and rig["alerts"][0]["kind"] == rules.RIG_SILENT


async def test_the_alerts_route_reports_what_is_open(client, session):
    await push_schedule(session)
    await sweep(session, now=at(9))

    r = await client.get("/api/floor/alerts")
    assert r.status_code == 200
    body = r.json()
    assert body["open"], "nothing reported when a rig has never been heard from"
    assert body["open"][0]["rigId"] == RIG


async def test_the_state_route_serves(client, session):
    await push_schedule(session)
    r = await client.get("/api/floor/state")
    assert r.status_code == 200
    assert r.json()["rigs"][0]["rigId"] == RIG


# ============================================================= efficiency

async def test_efficiency_is_computed_at_read_time_per_operator(client, session):
    """Nothing stores a percentage. Two stints for one operator sum their
    seconds first, then the ratio is taken once - averaging two
    percentages would weight a short stint the same as a long one."""
    from core.domains.rig_productivity_blocks.model import RigProductivityBlock

    for turn, rec, asg in (("08:00", 900, 2700), ("08:45", 1800, 2700)):
        session.add(RigProductivityBlock(
            rig_id=RIG, shift_date=DAY, shift_label="Morning",
            turn_from=turn, operator_id="op-a1", ended_at=at(9),
            episodes=5, recorded_secs=rec, assigned_secs=asg,
            fault_secs=0, down_secs=0, source_event=hash(turn) % 100000,
        ))
    await session.commit()

    rows = await operator_efficiency(session, DAY, "Morning")
    assert len(rows) == 1
    row = rows[0]
    assert row["recordedSecs"] == 2700
    assert row["assignedSecs"] == 5400
    assert row["efficiency"] == pytest.approx(0.5)


async def test_the_efficiency_route_serves(client, session):
    await push_schedule(session)
    r = await client.get("/api/floor/efficiency", params={
        "shift_date": "2026-08-24", "shift_label": "Morning",
    })
    assert r.status_code == 200
    assert r.json()["shiftLabel"] == "Morning"
