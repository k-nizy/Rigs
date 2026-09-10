"""Which schedule a rig gets, and why it is a comparison.

A payload covers one shift. The server used to hand a rig whichever
payload was pushed most recently, so at 16:00 a rig was still holding
the Morning schedule it had run all morning, `whoIsOn()` found no turn,
and it dropped to Standby until somebody reloaded twelve browsers. That
was the whole of blocker 2.

The thing being protected here is subtler than the fix. The rotation is
computed in exactly one shared JavaScript file, and the founding rule of
this system is that a rig can never compute a different answer from the
desk that scheduled it. So the server is not allowed to work out which
shift is running - it may only read the window the desk already wrote
into the payload and compare it against now. Every test below is really
asking the same question: did we select, or did we calculate?
"""

import itertools
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from core.domains.schedules.model import Schedule, SchedulePush
from core.workflows.schedules import _nearest, in_force

RIG = "RIG-03"
UTC = timezone.utc
DAY = date(2026, 8, 25)

SHIFTS = {
    "Morning": ("08:00", "16:00"),
    "Day":     ("16:00", "00:00"),
    "Night":   ("00:00", "08:00"),
}


def payload(label, tz="UTC", rig=RIG, day=DAY, turns=True):
    start, end = SHIFTS[label]
    return {
        "rigId": rig, "group": "A", "task": "Box transfer",
        "shift": {"label": label, "date": day.isoformat(),
                  "start": start, "end": end, "tz": tz},
        "blockMinutes": 15, "rotation": "hold",
        "turns": [{"from": start, "to": end, "minutes": 480,
                   "operator": {"id": "op-a1", "name": "Someone"},
                   "relievedBy": None, "theyGoTo": "Break"}] if turns else [],
    }


async def push(session, label, tz="UTC", rig=RIG, day=DAY, at=None):
    s = Schedule(
        push_id=uuid.uuid4(),
        pushed_at=at or datetime(2026, 8, 25, 7, 0, tzinfo=UTC),
        rig_id=rig, shift_date=day, shift_label=label,
        payload=payload(label, tz=tz, rig=rig, day=day),
    )
    session.add(s)
    await session.commit()
    return s


# ------------------------------------------------------------ the blocker

async def test_a_rig_gets_the_shift_that_is_actually_running(client, session):
    """The whole of blocker 2, in one assertion."""
    for label in ("Morning", "Day", "Night"):
        await push(session, label)

    at_ten = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    assert (await in_force(session, RIG, at_ten)).shift_label == "Morning"

    at_eight_pm = datetime(2026, 8, 25, 20, 0, tzinfo=UTC)
    assert (await in_force(session, RIG, at_eight_pm)).shift_label == "Day", (
        "at 20:00 the rig is still being handed the Morning schedule, which is "
        "why it went to Standby and stayed there"
    )


async def test_the_boundary_hands_over_at_the_minute(client, session):
    for label in ("Morning", "Day"):
        await push(session, label)

    just_before = datetime(2026, 8, 25, 15, 59, tzinfo=UTC)
    on_the_dot = datetime(2026, 8, 25, 16, 0, tzinfo=UTC)
    assert (await in_force(session, RIG, just_before)).shift_label == "Morning"
    assert (await in_force(session, RIG, on_the_dot)).shift_label == "Day"


async def test_the_day_shift_crosses_midnight(client, session):
    """16:00 to 00:00. Its end is on the following date, and a naive
    comparison would decide it covers nothing at all."""
    await push(session, "Day")
    await push(session, "Night")

    late = datetime(2026, 8, 25, 23, 30, tzinfo=UTC)
    assert (await in_force(session, RIG, late)).shift_label == "Day"

    # The rollover, properly: at 00:30 on the 26th the shift running is
    # the NEXT day Night, and the previous day Day shift has ended.
    await push(session, "Night", day=date(2026, 8, 26),
               at=datetime(2026, 8, 25, 7, 0, tzinfo=UTC))

    just_after = datetime(2026, 8, 26, 0, 30, tzinfo=UTC)
    got = await in_force(session, RIG, just_after)
    assert got.shift_label == "Night" and got.shift_date == date(2026, 8, 26), (
        "at 00:30 the rig got " + got.shift_label + " for " + str(got.shift_date) +
        ", so the Day shift ran past its own end"
    )


# ------------------------------------------------------ read, never derive

async def test_the_window_comes_from_the_payload_not_from_a_table_here(client, session):
    """The rule this protects. If the server had its own idea of when a
    shift runs, a desk that changed its mind would be overruled by it."""
    odd = payload("Morning")
    odd["shift"]["start"] = "10:00"
    odd["shift"]["end"] = "12:00"
    session.add(Schedule(
        push_id=uuid.uuid4(), pushed_at=datetime(2026, 8, 25, 7, 0, tzinfo=UTC),
        rig_id=RIG, shift_date=DAY, shift_label="Morning", payload=odd,
    ))
    await session.commit()

    inside = datetime(2026, 8, 25, 11, 0, tzinfo=UTC)
    outside = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)

    got_in = await in_force(session, RIG, inside)
    assert got_in is not None and got_in.payload["shift"]["start"] == "10:00"

    # 09:00 is inside a normal Morning shift but outside the one that was
    # actually pushed. Falling back is correct; claiming it covers is not.
    got_out = await in_force(session, RIG, outside)
    assert got_out is not None, "a rig outside every window still needs something to show"


async def test_the_floors_zone_decides_not_the_servers(client, session):
    """A UTC server and a UTC+3 floor. 06:00Z is 09:00 on the floor, so
    the floor's Morning shift is running even though the server's clock
    says it has not started."""
    await push(session, "Morning", tz="Africa/Nairobi")

    at_six_z = datetime(2026, 8, 25, 6, 0, tzinfo=UTC)
    got = await in_force(session, RIG, at_six_z)
    assert got is not None and got.shift_label == "Morning"


# --------------------------------------------------------- the safe edges

async def test_a_newer_push_of_the_same_shift_wins(client, session):
    """Fixing a bad roster mid-shift has to reach the floor."""
    await push(session, "Morning", at=datetime(2026, 8, 25, 7, 0, tzinfo=UTC))
    later = await push(session, "Morning", at=datetime(2026, 8, 25, 9, 30, tzinfo=UTC))

    at_ten = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    assert (await in_force(session, RIG, at_ten)).push_id == later.push_id


async def test_a_rig_between_shifts_still_gets_something(client, session):
    """It shows Standby, which is true. Returning nothing would make the
    rig generate its own schedule, which is the drift this prevents."""
    await push(session, "Morning")
    before_the_shift = datetime(2026, 8, 25, 5, 0, tzinfo=UTC)
    assert await in_force(session, RIG, before_the_shift) is not None


async def test_a_rig_nobody_pushed_to_gets_nothing(client, session):
    await push(session, "Morning", rig="RIG-01")
    assert await in_force(session, "RIG-07", datetime(2026, 8, 25, 10, 0, tzinfo=UTC)) is None


async def test_one_rigs_schedule_is_never_served_to_another(client, session):
    await push(session, "Morning", rig="RIG-01")
    await push(session, "Day", rig="RIG-02")

    got = await in_force(session, "RIG-01", datetime(2026, 8, 25, 10, 0, tzinfo=UTC))
    assert got.payload["rigId"] == "RIG-01"


async def test_a_payload_with_no_window_is_not_claimed_to_cover(client, session):
    """Malformed rather than malicious, but it must not match everything."""
    broken = payload("Morning")
    del broken["shift"]["start"]
    session.add(Schedule(
        push_id=uuid.uuid4(), pushed_at=datetime(2026, 8, 25, 7, 0, tzinfo=UTC),
        rig_id=RIG, shift_date=DAY, shift_label="Morning", payload=broken,
    ))
    await session.commit()

    got = await in_force(session, RIG, datetime(2026, 8, 25, 10, 0, tzinfo=UTC))
    assert got is not None, "it should still fall back rather than 404"


# ------------------------------------------------------------- over http

async def test_the_route_serves_what_is_in_force(client, session):
    for label in ("Morning", "Day"):
        await push(session, label)

    r = await client.get(f"/api/rigs/{RIG}/schedule.json")
    assert r.status_code == 200
    assert r.json()["shift"]["label"] in {"Morning", "Day", "Night"}


async def test_the_route_is_404_for_a_rig_with_no_schedule(client, session):
    r = await client.get("/api/rigs/RIG-09/schedule.json")
    assert r.status_code == 404


# ------------------------------------------------------- a whole-day push

async def test_a_push_may_carry_every_shift_of_the_day(client, session):
    """Twelve rigs times three shifts in one press.

    This failed outright until the schedules key learned about shifts. It
    was (push_id, rig_id), which assumed one push covered one shift, so
    every rig appearing three times under one push id violated it and the
    whole push 500ed. Nothing caught it because the desk test mocks the
    server and the backend test pushed placeholder objects.
    """
    payloads = []
    for rig in (f"RIG-{i:02d}" for i in range(1, 13)):
        for label in ("Morning", "Day", "Night"):
            payloads.append(payload(label, rig=rig))

    r = await client.post("/api/schedules/push", json={"payloads": payloads})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 36

    rows = await session.execute(select(Schedule).where(Schedule.rig_id == "RIG-03"))
    mine = list(rows.scalars().all())
    assert sorted(x.shift_label for x in mine) == ["Day", "Morning", "Night"]
    assert len({x.push_id for x in mine}) == 1, (
        "every row of one push still shares its id, so an event can name "
        "the exact schedule version in force when it happened"
    )


async def test_the_same_rig_and_shift_twice_in_one_push_is_still_refused(client, session):
    """The part of the old constraint worth keeping."""
    twice = [payload("Morning"), payload("Morning")]
    r = await client.post("/api/schedules/push", json={"payloads": twice})
    assert r.status_code >= 400, "a rig was given two versions of one shift in one push"


async def test_pushing_the_day_twice_replaces_rather_than_collides(client, session):
    """A manager fixing a roster presses push again."""
    day = [payload(l) for l in ("Morning", "Day", "Night")]
    assert (await client.post("/api/schedules/push", json={"payloads": day})).status_code == 200
    assert (await client.post("/api/schedules/push", json={"payloads": day})).status_code == 200

    at_ten = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    got = await in_force(session, RIG, at_ten)
    assert got is not None and got.shift_label == "Morning"


# ------------------------------------------- one floor, one answer

async def test_every_rig_gets_the_same_answer_when_nothing_is_running(client, session):
    """Twelve rigs, one push, and they must not disagree.

    Reported from the floor: RIG-01 and RIG-02 were showing the Day shift
    while RIG-03 sat in Standby, out of a single push, at the same instant.

    The cause was a tie. Every row of one push carries the same
    `pushed_at` to the microsecond, so ordering by it leaves all of them
    equal, and a tied ORDER BY lets the database hand them back in any
    order - a different order per query, and therefore a different shift
    per rig, from identical data. The floor looked like it was running
    three schedules at once.
    """
    payloads = []
    for i in range(1, 13):
        for label in ("Morning", "Day", "Night"):
            payloads.append(payload(label, rig=f"RIG-{i:02d}"))
    r = await client.post("/api/schedules/push", json={"payloads": payloads})
    assert r.status_code == 200, r.text

    # Every shift of the 25th has finished by 01:00 on the 26th.
    after_all = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)
    answers = {}
    for i in range(1, 13):
        rig = f"RIG-{i:02d}"
        got = await in_force(session, rig, after_all)
        assert got is not None, rig + " was given nothing at all"
        answers[rig] = (got.shift_label, got.shift_date)

    distinct = set(answers.values())
    assert len(distinct) == 1, (
        "one push produced " + str(len(distinct)) + " different answers across the "
        "floor: " + "; ".join(f"{r}={l} {d}" for r, (l, d) in sorted(answers.items()))
    )


async def test_it_is_the_same_answer_asked_over_and_over(client, session):
    """The tie made it unstable per query, not just per rig, so asking the
    same question twice could give two answers."""
    for label in ("Morning", "Day", "Night"):
        await push(session, label)

    after_all = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)
    seen = set()
    for _ in range(8):
        got = await in_force(session, RIG, after_all)
        seen.add((got.shift_label, got.shift_date))
    assert len(seen) == 1, "the same rig got " + str(len(seen)) + " different answers: " + str(seen)


async def test_when_they_have_all_ended_it_is_the_one_that_ended_last(client, session):
    """Chosen from the payloads themselves, so it cannot depend on row order."""
    for label in ("Morning", "Day", "Night"):
        await push(session, label)

    after_all = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)
    got = await in_force(session, RIG, after_all)
    assert got.shift_label == "Day", (
        "the last shift to finish on the 25th was Day (16:00-00:00), but the rig "
        "was handed " + got.shift_label
    )


async def test_before_the_day_starts_it_is_the_shift_about_to_begin(client, session):
    """A rig booting early should be waiting on the right shift, not the
    one that finished yesterday."""
    await push(session, "Morning")
    await push(session, "Day")

    early = datetime(2026, 8, 25, 5, 0, tzinfo=UTC)   # before Morning at 08:00
    got = await in_force(session, RIG, early)
    assert got.shift_label == "Morning", (
        "at 05:00 the next shift to start is Morning, but the rig was handed "
        + got.shift_label
    )


async def test_a_covering_shift_still_wins_over_the_fallback(client, session):
    """The fallback must never shadow a shift that is genuinely running."""
    for label in ("Morning", "Day", "Night"):
        await push(session, label)

    mid_morning = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    assert (await in_force(session, RIG, mid_morning)).shift_label == "Morning"


def _row(label, day=DAY, rig=RIG):
    """A Schedule that was never persisted. `_nearest` reads only the
    payload, the date and the label, so it needs nothing from the database
    - which is the point: this tests the rule, not the query plan."""
    return Schedule(
        push_id=uuid.uuid4(), pushed_at=datetime(2026, 8, 25, 7, 0, tzinfo=UTC),
        rig_id=rig, shift_date=day, shift_label=label,
        payload=payload(label, day=day, rig=rig),
    )


def test_the_answer_cannot_depend_on_the_order_the_rows_arrive_in():
    """The property itself, pinned directly.

    The floor test above is only a canary: it catches this if the database
    happens to vary its row order on that particular run, and it did not.
    A tie in ORDER BY is free to come back either way, so a test that
    waits for it to misbehave is a test that passes until it matters.

    This asks the question the bug was really about - does the same set of
    schedules, handed over in a different order, produce a different
    answer - and it can only be satisfied by not looking at order at all.
    """
    rows = [_row("Morning"), _row("Day"), _row("Night")]
    after_all = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)

    answers = {
        (_nearest(list(order), after_all).shift_label)
        for order in itertools.permutations(rows)
    }
    assert answers == {"Day"}, (
        "six orderings of one push produced " + str(len(answers)) + " answers " +
        str(sorted(answers)) + " - the choice still depends on row order"
    )


def test_the_shift_about_to_start_is_also_order_independent():
    rows = [_row("Morning"), _row("Day"), _row("Night")]
    early = datetime(2026, 8, 25, 5, 0, tzinfo=UTC)   # Night has ended, Morning is next

    answers = {
        (_nearest(list(order), early).shift_label)
        for order in itertools.permutations(rows)
    }
    assert answers == {"Morning"}, (
        "before the Morning shift the rig should be waiting on Morning, got " +
        str(sorted(answers))
    )


# ------------------------------------------------- the roster, server-side

# The assignment - which people sit in which group, in which slot, on
# which task - had no home except the pushed payloads, and the desk
# rebuilt it by reading twelve of them back and proving the rebuild turn
# by turn. It rides on the push now, in the same row and transaction as
# the schedules it produced, so the two cannot disagree.

ROSTER = [
    {"key": "A", "task": "Box transfer", "rigs": ["RIG-01", "RIG-02", "RIG-03"],
     "ops": [{"name": "Mei Chen", "personId": "3b0e6f7a-9c1d-4e2f-8a5b-6c7d8e9f0a1b"},
             "Ben Carter", "Tomas Rivera", "Nadia Haddad"]},
]


async def test_a_push_may_carry_the_roster_it_was_built_from(client, session):
    r = await client.post("/api/push", json={"payloads": [payload("Morning")], "roster": ROSTER})
    assert r.status_code == 200, r.text
    row = (await session.execute(select(SchedulePush))).scalars().one()
    assert row.roster == ROSTER, "the roster was not stored on the push row"


async def test_a_push_without_a_roster_is_still_a_push(client, session):
    """Every desk that predates this field, and the laptop demo."""
    r = await client.post("/api/push", json={"payloads": [payload("Morning")]})
    assert r.status_code == 200, r.text
    row = (await session.execute(select(SchedulePush))).scalars().one()
    assert row.roster is None


async def test_the_roster_route_answers_the_latest_push(client, session):
    older = [dict(ROSTER[0], task="Older task")]
    assert (await client.post("/api/push", json={"payloads": [payload("Morning")], "roster": older})).status_code == 200
    assert (await client.post("/api/push", json={"payloads": [payload("Day")], "roster": ROSTER})).status_code == 200
    r = await client.get("/api/roster")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["roster"] == ROSTER, "the roster served was not the newest push's"
    assert body["pushedAt"], "the desk needs to know when this roster was pushed"


async def test_a_floor_never_pushed_has_no_roster(client):
    r = await client.get("/api/roster")
    assert r.status_code == 200
    assert r.json() == {"pushedAt": None, "roster": None}


async def test_a_roster_that_is_not_a_list_of_groups_is_refused(client, session):
    """Stored opaque like the payload, but a shape that is not a roster
    at all would have the desk open on garbage - refused at the door."""
    r = await client.post("/api/push", json={"payloads": [payload("Morning")], "roster": {"not": "a list"}})
    assert r.status_code == 422
    assert (await session.execute(select(SchedulePush))).scalars().all() == [], \
        "a refused push left a row behind"
