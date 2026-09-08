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


_NO_NAME = object()


def env(seq, event, bucket, data, turn="09:00", op="op-a4", secs=None,
        name="Nadia Haddad", date="2026-08-24", person=None):
    """One envelope. `secs` overrides the default 30-seconds-per-seq
    spacing, for the events whose real place in the turn matters - a
    stint's length is now read from its own timestamps rather than taken
    from the payload."""
    ev = {
        "eventId": str(uuid.uuid4()),
        "seq": seq,
        "at": (T0 + timedelta(seconds=seq * 30 if secs is None else secs)).isoformat(),
        "rigId": RIG,
        "shiftDate": date,
        "shiftLabel": "Morning",
        "turnFrom": turn,
        "operatorId": op,
        "bucket": bucket,
        "event": event,
        "data": data,
    }
    # name=_NO_NAME leaves the key out altogether, which is what every
    # event already in a rig's journal looks like.
    if name is not _NO_NAME:
        ev["operatorName"] = name
    # person=None leaves the key out, which is what every rig sends until
    # the release after this one - the server learns the shape first.
    if person is not None:
        ev["personId"] = person
    return ev


def a_stint(person=None) -> list[dict]:
    """One operator's turn, as the rig would actually file it."""
    ep1, ep2, ep3 = (str(uuid.uuid4()) for _ in range(3))
    events = [
        env(0, "shift_check", "rig_shift_checks", {"outcome": "passed"}),
        env(1, "episode_saved", "episodes", {"episodeId": ep1, "durationSecs": 92, "score": 4}),
        env(2, "episode_discarded", "episodes", {"episodeId": ep2, "durationSecs": 11}),
        env(3, "episode_saved", "episodes", {"episodeId": ep3, "durationSecs": 105, "score": 5}),
        env(4, "rig_down", "rig_downtime_events",
            {"issue": "Gripper broken", "issuePath": "Gripper broken",
             "needsManager": False, "chargedTo": "previous_operator"}),
        env(5, "rig_up", "rig_downtime_events", {"downSecs": 180}),
        # Forty-five minutes after the first event, because that is what a
        # turn is. The payload still carries the rig's own counters; they
        # are no longer what the block is built from, so the timeline has
        # to be the honest one.
        env(6, "stint_ended", "rig_productivity_blocks",
            {"episodes": 2, "recordedSecs": 197, "assignedSecs": 2700,
             "faultSecs": 0, "downSecs": 180}, secs=2700),
    ]
    if person is not None:
        for ev in events:
            ev["personId"] = person
    return events


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


async def test_an_event_with_no_operator_name_at_all_is_accepted_and_projects(client, session):
    """The shape of every event already queued on a rig the day this ships.

    `operatorName` is accepted, never demanded. A rig that is refused does
    not retry: rig.js takes a 422 batch out of the outbox and calls
    forgetEvents on the journal, because a batch the server refuses would
    be refused again on every boot for ever. The uploader advances its
    mark past a refused batch for the same reason. So a required field
    would not delay the events written before it existed - it would
    destroy them, on every rig with a queue, with certainty rather than
    risk.
    """
    ev = env(0, "episode_saved", "episodes",
             {"episodeId": str(uuid.uuid4()), "durationSecs": 92, "score": 4},
             name=_NO_NAME)
    assert "operatorName" not in ev, "this test is pointless if the key is present"

    await ingest(client, [ev])
    await project_batch(session)

    rows = await session.execute(select(Episode))
    ep = rows.scalars().one()
    assert ep.operator_name is None, "an absent name must read as no name, not fail"
    assert ep.operator_id == "op-a4", "the rest of the event still lands"


async def test_one_seat_two_people_and_each_take_keeps_the_right_one(client, session):
    """The reason an operator has an identity at all: any take can be
    traced to who recorded it.

    `operator_id` is a seat, not a person - rotation-engine builds it as
    "op-" + group + slot - so the same string is a different human on a
    cover day. Nadia holds op-a4 on the 24th; Priya covers the same seat
    on the 25th. Answering "who recorded this" by the id alone credits
    one of them for both, and the ledger stores no name anywhere else to
    correct it with.
    """
    ep_nadia, ep_priya = str(uuid.uuid4()), str(uuid.uuid4())
    await ingest(client, [
        env(0, "episode_saved", "episodes",
            {"episodeId": ep_nadia, "durationSecs": 92, "score": 4},
            op="op-a4", name="Nadia Haddad", date="2026-08-24"),
        env(1, "episode_saved", "episodes",
            {"episodeId": ep_priya, "durationSecs": 88, "score": 5},
            op="op-a4", name="Priya Anand", date="2026-08-25"),
    ])
    await project_batch(session)

    rows = await session.execute(select(Episode))
    by_id = {str(e.episode_id): e for e in rows.scalars().all()}
    assert len(by_id) == 2
    assert by_id[ep_nadia].operator_name == "Nadia Haddad"
    assert by_id[ep_priya].operator_name == "Priya Anand"

    # The seat really is shared - which is why the id could not have
    # answered this on its own.
    assert by_id[ep_nadia].operator_id == by_id[ep_priya].operator_id == "op-a4"
    assert by_id[ep_nadia].shift_date != by_id[ep_priya].shift_date


async def test_the_name_reaches_every_fact_table_the_key_feeds(client, session):
    """operator_name rides in _key(), so all five fact rows carry it and a
    replay rebuilds every one of them with the person still on it."""
    await ingest(client, a_stint())
    await project_batch(session)

    for model in (Episode, RigShiftCheck, RigDowntimeEvent,
                  RigProductivityBlock, Session):
        rows = await session.execute(select(model))
        got = rows.scalars().all()
        assert got, f"{model.__tablename__} projected nothing"
        assert all(r.operator_name == "Nadia Haddad" for r in got),             f"{model.__tablename__} lost the name"


async def test_an_event_with_no_person_id_at_all_is_accepted_and_projects(client, session):
    """The shape of every event every rig sends the day this ships, and of
    every event already in a journal. `personId` is accepted, never
    demanded, for exactly the reason `operatorName` is: a refused batch
    is dropped and forgotten, not retried."""
    ev = env(0, "episode_saved", "episodes",
             {"episodeId": str(uuid.uuid4()), "durationSecs": 92, "score": 4})
    assert "personId" not in ev, "this test is pointless if the key is present"

    await ingest(client, [ev])
    await project_batch(session)

    ep = (await session.execute(select(Episode))).scalars().one()
    assert ep.person_id is None, "an absent person must read as no person, not fail"
    assert ep.operator_id == "op-a4", "the rest of the event still lands"


async def test_one_seat_two_people_and_each_take_keeps_its_person(client, session):
    """What the QC platform reads. It asks "who recorded this" of the
    episode row it is looking at, across months, and a seat cannot answer
    - `op-a4` is the same string for Nadia on the 24th and Priya covering
    on the 25th. The person id can, and it is on the row itself rather
    than in a projection that could be rebuilt differently later."""
    nadia, priya = str(uuid.uuid4()), str(uuid.uuid4())
    ep_nadia, ep_priya = str(uuid.uuid4()), str(uuid.uuid4())
    await ingest(client, [
        env(0, "episode_saved", "episodes",
            {"episodeId": ep_nadia, "durationSecs": 92, "score": 4},
            op="op-a4", name="Nadia Haddad", date="2026-08-24", person=nadia),
        env(1, "episode_saved", "episodes",
            {"episodeId": ep_priya, "durationSecs": 88, "score": 5},
            op="op-a4", name="Priya Anand", date="2026-08-25", person=priya),
    ])
    await project_batch(session)

    by_id = {str(e.episode_id): e for e in (await session.execute(select(Episode))).scalars()}
    assert str(by_id[ep_nadia].person_id) == nadia
    assert str(by_id[ep_priya].person_id) == priya
    assert by_id[ep_nadia].person_id != by_id[ep_priya].person_id
    # The seat really is shared, and the name alone is only a string.
    assert by_id[ep_nadia].operator_id == by_id[ep_priya].operator_id == "op-a4"


async def test_the_person_reaches_every_fact_table_the_key_feeds(client, session):
    """The decision named the episode. The precedent says every fact row,
    and there is no reason the episode should be the only one that knows:
    "Ben's efficiency" grouped by seat merges people exactly as "Ben's
    takes" did. person_id rides in _key(), so a replay rebuilds all five
    with the person still on them."""
    who = str(uuid.uuid4())
    await ingest(client, a_stint(person=who))
    await project_batch(session)

    for model in (Episode, RigShiftCheck, RigDowntimeEvent,
                  RigProductivityBlock, Session):
        got = (await session.execute(select(model))).scalars().all()
        assert got, f"{model.__tablename__} projected nothing"
        assert all(str(r.person_id) == who for r in got),             f"{model.__tablename__} lost the person"


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


# ------------------------------------------------------- more than one worker
#
# Running one projection worker is a single point of failure, and the
# first thing anyone does about that is run two. Until the claim took a
# row lock it was a plain SELECT, so both read the same tail and both
# projected it.
#
# Most of that collided and rolled back harmlessly - the episode primary
# key, the sessions constraint, the blocks constraint. A batch of nothing
# but shift checks against a session that already exists had nothing to
# collide with. Doubled shift checks inflate fault counts, and fault
# counts are what repeat_fault alerts on.


async def test_two_workers_claiming_at_once_project_each_row_once(client, session, engine):
    """The claim is a claim, not a look."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    await ingest(client, a_stint())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def worker():
        async with maker() as s:
            return await project_batch(s)

    a, b = await asyncio.gather(worker(), worker())

    # One of them takes the tail; the other finds it locked and skips.
    assert {a[0], b[0]} == {7, 0}, (
        "both workers claimed the same rows: %d and %d" % (a[0], b[0])
    )
    assert await counts(session) == {
        "episodes": 3, "checks": 1, "downtime": 1, "blocks": 1, "sessions": 1,
    }


async def test_a_batch_of_only_shift_checks_cannot_double(client, session, engine):
    """The case with nothing to collide with, which is the one that used to
    get through. The session already exists, so nothing in this batch
    touches a unique constraint except the one added for exactly this."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    await ingest(client, a_stint())
    await project_batch(session)          # the session row now exists

    await ingest(client, [
        env(7, "shift_check", "rig_shift_checks", {"outcome": "passed"}),
        env(8, "shift_check", "rig_shift_checks", {"outcome": "passed"}),
    ])

    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def worker():
        async with maker() as s:
            try:
                return await project_batch(s)
            except Exception:
                return (0, [])            # a collision is an acceptable outcome

    await asyncio.gather(worker(), worker())

    rows = await session.execute(select(func.count()).select_from(RigShiftCheck))
    assert rows.scalar() == 3, "a shift check was projected twice"


async def test_one_fact_per_ledger_row_is_enforced_by_the_database(client, session):
    """Not by the worker being careful. The worker can be replaced, run
    twice, or replayed; the constraint cannot be talked out of it."""
    from sqlalchemy.exc import IntegrityError

    await ingest(client, a_stint())
    await project_batch(session)

    existing = (await session.execute(select(RigShiftCheck))).scalars().first()
    session.add(RigShiftCheck(
        rig_id=RIG, shift_date=existing.shift_date, shift_label=existing.shift_label,
        turn_from=existing.turn_from, operator_id=existing.operator_id,
        at=existing.at, event=existing.event, outcome=existing.outcome,
        source_event=existing.source_event,
    ))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


# ------------------------------------------------- an outage nobody closed
#
# Only rig_up closed a downtime, and there are ordinary ways for it never
# to arrive. The operator ends their session with the rig still down, a
# technician fixes it, and the rig comes back with no memory of the
# outage. Nothing closed the row, so the floor board showed a working rig
# as down with the duration climbing for ever - the alert a manager is
# most likely to act on, stuck permanently on a rig that is fine.


class TestAnOutageNobodyClosed:

    def _down(self, seq):
        return env(seq, "rig_down", "rig_downtime_events",
                   {"issue": "Gripper broken", "issuePath": "Gripper broken",
                    "needsManager": False, "chargedTo": "rig"})

    async def _rows(self, session):
        rows = await session.execute(select(RigDowntimeEvent).order_by(RigDowntimeEvent.down_at))
        return list(rows.scalars().all())

    async def test_the_sequence_that_left_it_open(self, client, session):
        """Down, session ended, rig restarts, work resumes. No rig_up ever."""
        await ingest(client, [
            self._down(0),
            env(1, "session_ended", "sessions", {"endedBy": "operator"}),
            # the rig restarts: a fresh boot raises its checklist, then passes it
            env(2, "shift_check", "rig_shift_checks", {"outcome": "raised"}),
            env(3, "shift_check", "rig_shift_checks", {"outcome": "passed"}),
        ])
        await project_batch(session)

        rows = await self._rows(session)
        assert len(rows) == 1
        assert rows[0].up_at is not None, (
            "the outage is still open, so the board will show this rig as down for ever"
        )
        assert rows[0].ended_by == "resumed"

    async def test_a_recorded_take_also_proves_it(self, client, session):
        await ingest(client, [
            self._down(0),
            env(1, "episode_saved", "episodes",
                {"episodeId": str(uuid.uuid4()), "durationSecs": 92, "score": 4}),
        ])
        await project_batch(session)

        rows = await self._rows(session)
        assert rows[0].up_at is not None
        assert rows[0].ended_by == "resumed"

    async def test_an_operator_saying_so_is_recorded_as_such(self, client, session):
        """The two closes are not the same number and must be tellable apart."""
        await ingest(client, [
            self._down(0),
            env(1, "rig_up", "rig_downtime_events", {"downSecs": 180}),
        ])
        await project_batch(session)

        rows = await self._rows(session)
        assert rows[0].ended_by == "operator"
        assert rows[0].down_secs == 180, "the rig counted this; it must not be recomputed"

    async def test_a_heartbeat_is_not_evidence(self, client, session):
        """It proves the software is running. It says nothing about the
        gripper, and closing an outage on it would invent a repair."""
        await ingest(client, [self._down(0)])
        await project_batch(session)
        r = await client.post(f"/api/rigs/{RIG}/heartbeat",
                              json={"at": "2026-08-24T09:30:00Z"})
        assert r.status_code == 200
        await project_batch(session)

        rows = await self._rows(session)
        assert rows[0].up_at is None, "a heartbeat closed an outage"

    async def test_raising_the_checklist_is_not_passing_it(self, client, session):
        """`raised` is only the checklist appearing on boot."""
        await ingest(client, [
            self._down(0),
            env(1, "shift_check", "rig_shift_checks", {"outcome": "raised"}),
        ])
        await project_batch(session)

        rows = await self._rows(session)
        assert rows[0].up_at is None

    async def test_reporting_a_fault_is_not_evidence_either(self, client, session):
        await ingest(client, [
            self._down(0),
            env(1, "fault_opened", "rig_shift_checks",
                {"outcome": "failed", "subsystem": "Gripper"}),
        ])
        await project_batch(session)

        rows = await self._rows(session)
        assert rows[0].up_at is None, "a fault report is the opposite of working"

    async def test_the_alert_clears(self, client, session):
        """The whole point. A stuck alert teaches people to ignore the rest."""
        from core.domains.alerts.repository import AlertRepository
        from core.rules import floor as rules
        from core.workflows.floor import sweep

        await ingest(client, [self._down(0)])
        await project_batch(session)
        await sweep(session, now=T0 + timedelta(minutes=30))
        open_kinds = [a.kind for a in await AlertRepository(session).open_alerts()]
        assert rules.RIG_DOWN in open_kinds, "the outage should raise an alert"

        await ingest(client, [
            env(1, "shift_check", "rig_shift_checks", {"outcome": "passed"}),
        ])
        await project_batch(session)
        await sweep(session, now=T0 + timedelta(minutes=40))

        open_kinds = [a.kind for a in await AlertRepository(session).open_alerts()]
        assert rules.RIG_DOWN not in open_kinds, (
            "the rig is working and the board still says it is down"
        )

    async def test_work_with_no_outage_open_changes_nothing(self, client, session):
        await ingest(client, a_stint())
        await project_batch(session)
        rows = await self._rows(session)
        assert all(r.ended_by == "operator" for r in rows if r.up_at), (
            "a normally closed outage was relabelled"
        )


# ----------------------------------------------- a rig that restarts mid-turn


def a_stint_with_a_restart() -> list[dict]:
    """The same turn, with a reload in the middle of it.

    `boot()` on the rig rebuilds its whole session from zero - `S.episode`,
    `S.recordedSecs`, `S.stintAt`, all of it - because every boot is
    treated as the start of a shift. So the `stint_ended` that eventually
    arrives describes only what happened *after* the restart.

    The episodes themselves are not lost. The rig journals every event
    before it touches the network, so the two takes from before the reload
    are in this ledger; it is only the rig's summary of them that reset.
    That is exactly why the block can be rebuilt.
    """
    ep1, ep2, ep3 = (str(uuid.uuid4()) for _ in range(3))
    return [
        env(0, "shift_check", "rig_shift_checks", {"outcome": "passed"}),
        # Before the reload: two saved takes, 600 seconds of work.
        env(1, "episode_saved", "episodes",
            {"episodeId": ep1, "durationSecs": 400, "score": 4}, secs=300),
        env(2, "episode_saved", "episodes",
            {"episodeId": ep2, "durationSecs": 200, "score": 4}, secs=900),
        # --- the operator reloads the page here, twenty minutes in ---
        # After it: one take, and counters that start again from nothing.
        env(3, "episode_saved", "episodes",
            {"episodeId": ep3, "durationSecs": 300, "score": 5}, secs=1800),
        env(4, "stint_ended", "rig_productivity_blocks",
            {"episodes": 1, "recordedSecs": 300, "assignedSecs": 900,
             "faultSecs": 0, "downSecs": 0}, secs=2700),
    ]


async def test_a_restart_mid_turn_does_not_erase_the_work_before_it(client, session):
    """The block counts the whole turn, not the fragment the rig remembers.

    Every episode is in the ledger. A block that believed the rig's
    counters would drop two of the three takes and eleven minutes of
    work, silently, in a table with no correction mechanism.
    """
    await ingest(client, a_stint_with_a_restart())
    await project_batch(session)

    block = (await session.execute(select(RigProductivityBlock))).scalar_one()
    assert block.episodes == 3, "the two takes before the reload still happened"
    assert block.recorded_secs == 900, "400 + 200 + 300, not the 300 the rig remembered"


async def test_a_restart_cannot_flatter_the_operator(client, session):
    """The denominator has to move with the numerator.

    This is the reason `assigned_secs` is derived too. The rig reported
    900 seconds assigned - the time since it restarted - against a turn
    that really ran 2700. Score the real work over the fragment's
    denominator and it comes to 1.0, which `efficiency()` clamps to a
    perfect stint. An operator having a bad turn could reload and be
    judged only on what came after.
    """
    await ingest(client, a_stint_with_a_restart())
    await project_batch(session)

    block = (await session.execute(select(RigProductivityBlock))).scalar_one()
    assert block.assigned_secs == 2700, "first event to stint_ended, by the clock"

    s = Stint(block.recorded_secs, block.assigned_secs, block.fault_secs, block.down_secs)
    assert efficiency(s) == pytest.approx(900 / 2700)

    # What it would have been on the rig's own numbers, had the numerator
    # been fixed and the denominator left alone.
    flattered = Stint(900.0, 900.0, 0.0, 0.0)
    assert efficiency(flattered) == 1.0, (
        "the clamp is what would have hidden this - a half-fix reads as perfect"
    )


async def test_replay_corrects_a_block_that_was_wrong(client, session):
    """The property that makes deriving worth doing.

    A transcribed block is frozen at whatever the device believed. A
    derived one is rebuilt from the ledger, so wiping the facts and
    running the projection again repairs history - which is the same
    argument the backend already makes for not storing a percentage.
    """
    await ingest(client, a_stint_with_a_restart())
    await project_batch(session)

    block = (await session.execute(select(RigProductivityBlock))).scalar_one()
    # Corrupt it the way a device-supplied number could be wrong.
    block.episodes = 1
    block.recorded_secs = 300.0
    block.assigned_secs = 900.0
    await session.commit()

    await reset_projections(session)
    await project_batch(session)

    rebuilt = (await session.execute(select(RigProductivityBlock))).scalar_one()
    assert rebuilt.episodes == 3
    assert rebuilt.recorded_secs == 900
    assert rebuilt.assigned_secs == 2700
