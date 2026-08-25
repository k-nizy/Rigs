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
from core.domains.rig_shift_checks.model import RigShiftCheck
from core.domains.rig_productivity_blocks.model import RigProductivityBlock
from core.domains.rig_status.repository import RigStatusRepository
from core.domains.schedules.model import Schedule
from core.rules import floor as rules
from core.workflows.floor import (
    _overruns, _repeat_faults, floor_state, operator_efficiency, sweep,
)
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


# --------------------------------------------------------------- the zone
#
# Added after /api/floor/state reported no turn in progress on any of the
# twelve rigs while both apps showed a live turn with forty minutes on it.
# The payload's "00:15" is floor wall-clock; the server read it as UTC.


class TestTheZoneTravelsWithTheSchedule:

    PAYLOAD_TZ = {
        "shift": {"label": "Night", "date": "2026-08-24", "start": "00:00",
                  "end": "08:00", "tz": "Africa/Nairobi"},
        "turns": [{"from": "00:15", "to": "01:00", "minutes": 45,
                   "operator": {"id": "op-a4", "name": "Nadia Haddad"}}],
    }

    def test_the_floors_zone_is_read_from_the_payload(self):
        zone = rules.payload_zone(self.PAYLOAD_TZ, UTC)
        assert str(zone) == "Africa/Nairobi"

    def test_a_payload_without_a_zone_falls_back_rather_than_crashing(self):
        """Schedules pushed before the field existed are still readable."""
        assert rules.payload_zone({"shift": {"label": "M"}}, UTC) is UTC

    def test_an_unknown_zone_falls_back(self):
        assert rules.payload_zone({"shift": {"tz": "Mars/Olympus"}}, UTC) is UTC

    def test_a_turn_is_found_from_a_utc_clock_on_a_floor_that_is_not_utc(self):
        """The bug, written down.

        The floor keeps UTC+3, so its 00:15-01:00 turn on the 24th runs
        from 21:15Z to 22:00Z on the 23rd. At 21:30Z an operator is at the
        rig. Read in the server's own zone that instant falls outside every
        turn in the payload, and all twelve rigs look unscheduled.
        """
        now = datetime(2026, 8, 23, 21, 30, tzinfo=UTC)
        found = rules.turn_in_progress(
            self.PAYLOAD_TZ, date(2026, 8, 24), now, UTC
        )
        assert found is not None, "the floor's turn was invisible to a UTC server"
        assert found["turn"]["from"] == "00:15"

    def test_the_same_clock_reads_as_no_turn_if_the_zone_is_ignored(self):
        """The control: without the zone, this is what used to happen."""
        no_tz = {"shift": {k: v for k, v in self.PAYLOAD_TZ["shift"].items() if k != "tz"},
                 "turns": self.PAYLOAD_TZ["turns"]}
        now = datetime(2026, 8, 23, 21, 30, tzinfo=UTC)
        assert rules.turn_in_progress(no_tz, date(2026, 8, 24), now, UTC) is None

    def test_idle_can_fire_on_a_non_utc_floor(self):
        """`rig_idle` needs a turn in progress. While the zone was assumed
        it could not fire at all, which is the alert for nobody arriving."""
        now = datetime(2026, 8, 23, 21, 30, tzinfo=UTC)
        found = rules.turn_in_progress(self.PAYLOAD_TZ, date(2026, 8, 24), now, UTC)
        alert = rules.rig_idle(
            "RIG-03", found["turn"]["from"], found["start"],
            None, now, grace_secs=300,
        )
        assert alert is not None
        assert alert.kind == rules.RIG_IDLE


class TestAnOverrunIsMeasuredAgainstTheRightShift:
    """A finished block is judged by the schedule that was in force for
    *its own* shift, not by whatever was pushed most recently.

    The two lookups only disagree when a later push reuses a turn label
    with a different boundary - which is exactly what changing the block
    size does. Then "the most recent schedule" moves the finish line under
    a block that already ended, and an on-time stint is reported as an
    overrun.
    """

    # Pushed later, and re-cut into shorter turns. Same "08:00" label.
    RECUT = {
        "shift": {"label": "Morning", "date": "2026-08-25", "start": "08:00",
                  "end": "16:00", "tz": "UTC"},
        "turns": [{"from": "08:00", "to": "08:15", "minutes": 15,
                   "operator": {"id": "op-a1", "name": "Aleksandr Petrov"},
                   "relievedBy": "Mei Chen", "theyGoTo": "Break"}],
    }

    async def _block(self, session, ended_at):
        session.add(RigProductivityBlock(
            rig_id=RIG, shift_date=DAY, shift_label="Morning",
            turn_from="08:00", operator_id="op-a1", ended_at=ended_at,
            episodes=1, recorded_secs=100.0, assigned_secs=2700.0,
            fault_secs=0, down_secs=0, source_event=987001,
        ))
        await session.commit()

    async def _overruns(self, session):
        return [a for a in await AlertRepository(session).open_alerts()
                if a.kind == rules.TURN_OVERRAN]

    async def test_a_later_re_cut_schedule_does_not_move_a_finished_boundary(
            self, client, session):
        """The block ran 08:00-08:44 and its own turn ended at 08:45. A
        schedule pushed the next day cuts that turn at 08:15; judged by it,
        an on-time stint looks 29 minutes late."""
        await push_schedule(session)                        # Morning, DAY, 08:00-08:45
        session.add(Schedule(
            push_id=uuid.uuid4(), pushed_at=at(23, 0), rig_id=RIG,
            shift_date=date(2026, 8, 25), shift_label="Morning", payload=self.RECUT,
        ))
        await session.commit()
        await self._block(session, at(8, 44))               # inside its own turn

        await sweep(session, now=at(9))
        found = await self._overruns(session)
        assert found == [], (
            "an on-time block was judged against a later schedule: "
            + "; ".join(a.detail for a in found)
        )

    async def test_a_real_overrun_on_its_own_shift_still_raises(self, client, session):
        """The control: the rule still fires when the lookup is right."""
        await push_schedule(session)
        await self._block(session, at(8, 50))               # 5 min past 08:45

        await sweep(session, now=at(9))
        found = await self._overruns(session)
        assert len(found) == 1, "a genuine overrun stopped being reported"
        assert "300s" in found[0].detail, found[0].detail


# ------------------------------------------------------------ the clocks
#
# Skew was measured on every heartbeat and then never looked at, which is
# the same as not measuring it. It is not cosmetic: rig_idle compares the
# newest event's timestamp - the rig's own - against a boundary computed
# here, so a rig running slow looks like a rig where nothing is happening.
# A wrong clock manufactures the very alert this system exists to make
# trustworthy.


class TestAClockThatDisagrees:

    def test_a_clock_within_tolerance_is_not_worth_saying(self):
        assert rules.clock_adrift(RIG, 12.0, 120) is None

    def test_a_rig_never_heard_from_has_no_clock_to_judge(self):
        assert rules.clock_adrift(RIG, None, 120) is None

    def test_a_clock_running_behind_is_reported_as_behind(self):
        a = rules.clock_adrift(RIG, 600.0, 120)
        assert a and a.kind == rules.CLOCK_ADRIFT
        assert "600s behind" in a.detail

    def test_a_clock_running_ahead_is_reported_as_ahead(self):
        """Direction matters to whoever has to go and fix it."""
        a = rules.clock_adrift(RIG, -600.0, 120)
        assert a and "600s ahead of" in a.detail

    def test_it_is_keyed_per_rig_so_sweeping_twice_opens_one(self):
        a = rules.clock_adrift(RIG, 600.0, 120)
        b = rules.clock_adrift(RIG, 640.0, 120)
        assert a.key == b.key

    async def test_the_sweep_raises_it_for_a_rig_whose_clock_is_out(self, client, session):
        await push_schedule(session)
        await RigStatusRepository(session).beat(
            RIG, at(9), at(9) - timedelta(seconds=900), 900.0)
        await session.commit()

        await sweep(session, now=at(9), clock_tolerance_secs=120)
        kinds = [a.kind for a in await AlertRepository(session).open_alerts()]
        assert rules.CLOCK_ADRIFT in kinds

    async def test_a_good_clock_raises_nothing(self, client, session):
        await push_schedule(session)
        await RigStatusRepository(session).beat(RIG, at(9), at(9), 0.4)
        await session.commit()

        await sweep(session, now=at(9), clock_tolerance_secs=120)
        kinds = [a.kind for a in await AlertRepository(session).open_alerts()]
        assert rules.CLOCK_ADRIFT not in kinds


# ----------------------------------------------------------- ourselves
#
# The whole design is about noticing absences that nothing publishes. A
# projection worker that has died is the same failure pointed at us: it
# emits nothing, the ledger keeps accepting, and every board goes on
# answering with yesterday's episodes. Nobody finds out by being told.


class TestTheBackendFallingBehind:

    def test_an_empty_queue_is_not_a_stall(self):
        assert rules.projection_behind(None, 0, at(9), 120) is None

    def test_a_recent_queue_is_ordinary_depth(self):
        assert rules.projection_behind(at(8, 59), 40, at(9), 120) is None

    def test_a_queue_that_has_stopped_moving_is_reported(self):
        a = rules.projection_behind(at(8, 30), 1204, at(9), 120)
        assert a and a.kind == rules.PROJECTION_BEHIND
        assert "1204 events" in a.detail
        assert "stale" in a.detail

    def test_it_is_not_attributed_to_a_rig(self):
        """No rig did this, and blaming one would send somebody to a
        machine that is working perfectly."""
        a = rules.projection_behind(at(8, 30), 5, at(9), 120)
        assert a.rig_id == rules.FLOOR

    async def _stale_ledger_row(self, session, received_at):
        session.add(RigEvent(
            rig_id=RIG, event_id=uuid.uuid4(), seq=1, at=received_at,
            shift_date=DAY, shift_label="Morning", turn_from="08:00",
            operator_id="op-a1", bucket="episodes", event="episode_saved",
            envelope={}, received_at=received_at, projected_at=None,
        ))
        await session.commit()

    async def test_the_sweep_notices_that_it_has_stopped_keeping_up(self, client, session):
        await push_schedule(session)
        await self._stale_ledger_row(session, at(8, 30))

        await sweep(session, now=at(9), projection_behind_after_secs=120)
        kinds = [a.kind for a in await AlertRepository(session).open_alerts()]
        assert rules.PROJECTION_BEHIND in kinds, "a dead projection worker went unnoticed"

    async def test_it_closes_once_the_backlog_is_projected(self, client, session):
        """Reconciled like every other alert: it stops being true, so it
        stops being open. No all-clear event required."""
        await push_schedule(session)
        await self._stale_ledger_row(session, at(8, 30))
        await sweep(session, now=at(9), projection_behind_after_secs=120)

        rows = await session.execute(select(RigEvent))
        for row in rows.scalars().all():
            row.projected_at = at(9)
        await session.commit()

        await sweep(session, now=at(9), projection_behind_after_secs=120)
        kinds = [a.kind for a in await AlertRepository(session).open_alerts()]
        assert rules.PROJECTION_BEHIND not in kinds

    async def test_the_board_says_how_far_behind_it_is(self, client, session):
        """A verdict is an alert; this is the measurement beside it."""
        await push_schedule(session)
        await self._stale_ledger_row(session, at(8, 30))

        state = await floor_state(session, now=at(9))
        assert state["backend"]["unprojectedEvents"] == 1
        assert state["backend"]["projectionLagSecs"] == 1800.0

    async def test_a_board_that_is_keeping_up_says_zero(self, client, session):
        await push_schedule(session)
        state = await floor_state(session, now=at(9))
        assert state["backend"] == {"unprojectedEvents": 0, "projectionLagSecs": 0.0}


# ------------------------------------------------------- what the sweep costs
#
# The sweep runs every fifteen seconds for ever, so its cost is not a
# micro-optimisation - it is a thing that either stays flat or eats the
# floor. It was looking the schedule up once per productivity block, and a
# day of blocks on a twelve-rig floor is ~400 rows, so ~400 sequential
# round trips every sweep. Measured at ~190ms of a 213ms sweep.
#
# Counting queries rather than milliseconds on purpose. A timing assertion
# on a developer laptop is a flake; "does this scale with the number of
# blocks" is the actual question and it has a yes/no answer.


class TestTheSweepDoesNotScaleWithTheFloor:

    async def _blocks(self, session, n, turn="08:00", offset=0):
        for i in range(n):
            session.add(RigProductivityBlock(
                rig_id=RIG, shift_date=DAY, shift_label="Morning",
                turn_from=turn, operator_id="op-a1", ended_at=at(8, 44),
                episodes=1, recorded_secs=100.0, assigned_secs=2700.0,
                fault_secs=0, down_secs=0, source_event=500000 + offset + i,
            ))
        await session.commit()

    async def _queries_during_sweep(self, engine, session):
        from sqlalchemy import event

        seen = []

        def count(conn, cursor, statement, params, context, many):
            seen.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", count)
        try:
            await sweep(session, now=at(9))
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count)
        return seen

    async def test_ten_times_the_blocks_does_not_mean_ten_times_the_queries(
            self, client, session, engine):
        await push_schedule(session)

        await self._blocks(session, 5)
        few = len(await self._queries_during_sweep(engine, session))

        await self._blocks(session, 50, offset=1000)
        many = len(await self._queries_during_sweep(engine, session))

        assert many <= few + 2, (
            "the sweep issued %d queries for 55 blocks against %d for 5 - it is "
            "looking something up inside the loop again" % (many, few)
        )

    async def test_the_schedules_behind_a_day_of_blocks_are_read_once(
            self, client, session, engine):
        """Not once per block. The distinct shifts behind four hundred
        blocks number in the single digits."""
        await push_schedule(session)
        await self._blocks(session, 40)

        statements = await self._queries_during_sweep(engine, session)
        schedule_reads = [q for q in statements if "FROM schedules" in q]
        assert len(schedule_reads) <= 3, (
            "%d reads of the schedules table for one shift's blocks: %s"
            % (len(schedule_reads), schedule_reads[:2])
        )

    async def test_a_real_overrun_is_still_found_after_the_batching(
            self, client, session, engine):
        """The control. Making it fast is worthless if it stopped working."""
        await push_schedule(session)
        session.add(RigProductivityBlock(
            rig_id=RIG, shift_date=DAY, shift_label="Morning",
            turn_from="08:00", operator_id="op-a1", ended_at=at(8, 50),
            episodes=1, recorded_secs=100.0, assigned_secs=2700.0,
            fault_secs=0, down_secs=0, source_event=600001,
        ))
        await session.commit()

        await sweep(session, now=at(9))
        overruns = [a for a in await AlertRepository(session).open_alerts()
                    if a.kind == rules.TURN_OVERRAN]
        assert len(overruns) == 1
        assert "300s" in overruns[0].detail


# ------------------------------------------------- the sweep and the clock
#
# Found by two control tests becoming a time bomb: they asserted a real
# overrun still fires, passed in the morning and failed in the afternoon.
# `_overruns` and `_repeat_faults` were reading the wall clock while
# sweep() was handed an injected one, so the sweep was quietly not a
# function of its own argument.


class TestTheSweepUsesTheClockItWasGiven:

    async def _block(self, session, ended_at, source=770001):
        session.add(RigProductivityBlock(
            rig_id=RIG, shift_date=DAY, shift_label="Morning",
            turn_from="08:00", operator_id="op-a1", ended_at=ended_at,
            episodes=1, recorded_secs=100.0, assigned_secs=2700.0,
            fault_secs=0, down_secs=0, source_event=source,
        ))
        await session.commit()

    async def test_an_overrun_is_found_however_long_ago_the_shift_was(
            self, client, session):
        """The same situation, swept from an instant an hour later, must
        give the same answer whatever today's date happens to be."""
        await push_schedule(session)
        await self._block(session, at(8, 50))

        await sweep(session, now=at(9))
        found = [a for a in await AlertRepository(session).open_alerts()
                 if a.kind == rules.TURN_OVERRAN]
        assert len(found) == 1, (
            "the overrun vanished - the look-back is reading a clock the "
            "sweep was not given"
        )

    async def test_the_look_back_window_is_measured_from_that_instant(
            self, client, session):
        """Inside the window from one `now`, outside it from another. If
        the wall clock were involved, both would answer the same."""
        await push_schedule(session)
        await self._block(session, at(8, 50))

        inside = await _overruns(session, 24, at(9))
        outside = await _overruns(session, 24, at(9) + timedelta(days=3))
        assert len(inside) == 1
        assert outside == [], "the look-back ignored the instant it was given"

    async def test_repeat_faults_are_counted_from_that_instant_too(
            self, client, session):
        """Same defect, same fix, different rule."""
        for i, day in enumerate((DAY, DAY - timedelta(days=1))):
            session.add(RigShiftCheck(
                rig_id=RIG, shift_date=day, shift_label="Morning",
                turn_from="08:00", operator_id="op-a1", at=at(9),
                event="fault_opened", outcome="failed", subsystem="Gripper",
                source_event=780000 + i,
            ))
        await session.commit()

        near = await _repeat_faults(session, 7, at(9))
        far = await _repeat_faults(session, 7, at(9) + timedelta(days=30))
        assert near and near[0][2] == 2
        assert far == [], "the fault window ignored the instant it was given"

    async def test_two_sweeps_at_the_same_instant_agree(self, client, session):
        """The property in one line: the sweep is a function of its now."""
        await push_schedule(session)
        await self._block(session, at(8, 50))

        first = await sweep(session, now=at(9))
        second = await sweep(session, now=at(9))
        assert first["found"] == second["found"]
        assert second["opened"] == 0, "the same situation opened a second alert"
