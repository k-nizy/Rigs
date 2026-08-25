"""Losing the whole database, and getting it back.

`test_replay_rebuilds_identical_facts` proves the facts can be rebuilt
from the ledger. It assumes the ledger survives. This file assumes it
does not, because that is the failure that ends the company rather than
the afternoon.

The claim being tested is stronger than "we take backups":

    the backup format is the public contract, and restoring is
    re-sending it

Every ledger row keeps the envelope the rig actually sent. Those
envelopes are exactly what `POST /rigs/{id}/events` accepts, so a backup
is a file of envelopes and a restore is a replay of the same API a rig
uses - with the same idempotency, so it can be interrupted, resumed, or
run twice by two frightened people at once.

Two things this establishes that a `pg_dump` alone does not:

  * the ledger is genuinely sufficient. Every fact comes back from the
    envelopes and nothing else.
  * the restore path is one that gets exercised. A backup restored only
    during a disaster is a backup nobody has tested.

And one thing it establishes that is easy to get wrong: the schedules
are **not** in the ledger. They are pushed by the desk, and a restore
that only replays events comes back with correct facts and an empty
floor board. That is written down here rather than discovered at the
worst possible moment.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from core.domains.episodes.model import Episode
from core.domains.rig_downtime_events.model import RigDowntimeEvent
from core.domains.rig_events.model import RigEvent
from core.domains.rig_productivity_blocks.model import RigProductivityBlock
from core.domains.rig_shift_checks.model import RigShiftCheck
from core.domains.schedules.model import Schedule
from core.domains.sessions.model import Session
from core.workflows.projection import project_batch

RIG = "RIG-03"
T0 = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)

# Columns that are this database's own bookkeeping rather than facts about
# the floor. A restored row is a different row in the storage sense and
# the same row in every sense that matters, and conflating the two would
# make this test assert something untrue.
NOT_A_FACT = {"id", "source_event", "received_at", "push_id"}

FACT_MODELS = (
    ("episodes", Episode),
    ("checks", RigShiftCheck),
    ("downtime", RigDowntimeEvent),
    ("blocks", RigProductivityBlock),
    ("sessions", Session),
)


def env(seq, event, bucket, data, rig=RIG, turn="09:00", op="op-a4"):
    return {
        "eventId": str(uuid.uuid4()),
        "seq": seq,
        "at": (T0 + timedelta(seconds=seq * 30)).isoformat(),
        "rigId": rig,
        "shiftDate": "2026-08-24",
        "shiftLabel": "Morning",
        "turnFrom": turn,
        "operatorId": op,
        "bucket": bucket,
        "event": event,
        "data": data,
    }


def a_shift(rig=RIG, op="op-a4", base=0):
    """One operator's turn, as the rig would actually file it."""
    ep1, ep2, ep3 = (str(uuid.uuid4()) for _ in range(3))
    return [
        env(base + 0, "shift_check", "rig_shift_checks", {"outcome": "passed"}, rig, op=op),
        env(base + 1, "episode_saved", "episodes",
            {"episodeId": ep1, "durationSecs": 92, "score": 4}, rig, op=op),
        env(base + 2, "episode_discarded", "episodes",
            {"episodeId": ep2, "durationSecs": 11}, rig, op=op),
        env(base + 3, "fault_opened", "rig_shift_checks",
            {"outcome": "failed", "subsystem": "Gripper"}, rig, op=op),
        env(base + 4, "fault_closed", "rig_shift_checks", {"outcome": "fixed"}, rig, op=op),
        env(base + 5, "rig_down", "rig_downtime_events",
            {"issue": "Gripper broken", "issuePath": "Gripper broken",
             "needsManager": False, "chargedTo": "previous_operator"}, rig, op=op),
        env(base + 6, "rig_up", "rig_downtime_events", {"downSecs": 180}, rig, op=op),
        env(base + 7, "episode_saved", "episodes",
            {"episodeId": ep3, "durationSecs": 105, "score": 5}, rig, op=op),
        env(base + 8, "stint_ended", "rig_productivity_blocks",
            {"episodes": 2, "recordedSecs": 197, "assignedSecs": 2700,
             "faultSecs": 60, "downSecs": 180}, rig, op=op),
        env(base + 9, "session_ended", "sessions",
            {"endedBy": "handover", "recordedSecs": 197}, rig, op=op),
    ]


async def ingest(client, events, rig=RIG):
    r = await client.post(f"/api/rigs/{rig}/events", json={"events": events})
    assert r.status_code == 200, r.text
    return r.json()


async def snapshot(session) -> dict:
    """Every fact on the floor, in a form two databases can be compared in."""
    out = {}
    for name, model in FACT_MODELS:
        cols = [c for c in model.__table__.columns if c.name not in NOT_A_FACT]
        rows = await session.execute(select(*cols))
        out[name] = sorted(
            [tuple(str(v) for v in row) for row in rows.all()]
        )
    return out


async def the_backup(session) -> list[dict]:
    """What a backup of this system actually is: the envelopes, in order.

    Ordered by (rig, seq) rather than by row id, because row ids are this
    database's bookkeeping and a restore must not depend on them.
    """
    rows = await session.execute(
        select(RigEvent.envelope).order_by(RigEvent.rig_id, RigEvent.seq)
    )
    return [r[0] for r in rows.all()]


async def burn_it_down(session):
    """Everything. The ledger, the facts, the schedules - the whole thing."""
    for _, model in FACT_MODELS:
        await session.execute(model.__table__.delete())
    await session.execute(RigEvent.__table__.delete())
    await session.execute(Schedule.__table__.delete())
    await session.commit()


async def restore(client, backup):
    """Re-send the backup through the same route a rig uses.

    In batches, because that is how it would really be done and because a
    restore that only works as one enormous request is a restore that
    fails on a big enough floor.
    """
    by_rig: dict[str, list] = {}
    for e in backup:
        by_rig.setdefault(e["rigId"], []).append(e)
    for rig, events in by_rig.items():
        for i in range(0, len(events), 50):
            await ingest(client, events[i:i + 50], rig=rig)


# ------------------------------------------------------------- the drill

async def test_the_whole_database_can_be_lost_and_rebuilt_from_envelopes(client, session):
    """The drill. Nothing survives except a file of envelopes."""
    await ingest(client, a_shift())
    await ingest(client, a_shift(rig="RIG-01", op="op-a1"), rig="RIG-01")
    await project_batch(session)

    before = await snapshot(session)
    backup = await the_backup(session)
    assert len(backup) == 20, "the backup does not hold every event"
    assert all(before[name] for name, _ in FACT_MODELS), "nothing was projected to lose"

    await burn_it_down(session)
    assert await snapshot(session) == {name: [] for name, _ in FACT_MODELS}
    assert (await session.execute(select(func.count()).select_from(RigEvent))).scalar() == 0

    await restore(client, backup)
    await project_batch(session)
    await project_batch(session)      # more events than one batch, on a big floor

    assert await snapshot(session) == before, (
        "the floor came back different from the one that was lost"
    )


async def test_the_backup_is_the_public_contract(client, session):
    """A backup that needs a private tool to read is a backup nobody can
    restore under pressure. These are the envelopes the rig sent, and the
    route that accepts them is the one a rig uses every day."""
    await ingest(client, a_shift())
    backup = await the_backup(session)

    for e in backup:
        assert set(e) >= {
            "eventId", "seq", "at", "rigId", "shiftDate", "shiftLabel",
            "bucket", "event", "data",
        }, "an envelope came back missing fields the ingest route requires"

    await burn_it_down(session)
    r = await client.post(f"/api/rigs/{RIG}/events", json={"events": backup})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == len(backup)


async def test_restoring_twice_is_safe(client, session):
    """Because it will happen. Two people, one outage, and nobody sure
    whether the first attempt finished."""
    await ingest(client, a_shift())
    await project_batch(session)
    before = await snapshot(session)
    backup = await the_backup(session)

    await burn_it_down(session)
    await restore(client, backup)
    await restore(client, backup)          # again, in a panic
    await project_batch(session)

    assert await snapshot(session) == before
    assert (await session.execute(
        select(func.count()).select_from(RigEvent))).scalar() == len(backup), (
        "a second restore duplicated the ledger"
    )


async def test_an_interrupted_restore_can_be_resumed(client, session):
    """It stops half way. The answer is to run it again from the top,
    which is only true because ingest dedupes on (rigId, eventId)."""
    await ingest(client, a_shift())
    await project_batch(session)
    before = await snapshot(session)
    backup = await the_backup(session)

    await burn_it_down(session)
    await ingest(client, backup[:4])       # the connection dies here
    await restore(client, backup)          # start again from the beginning
    await project_batch(session)

    assert await snapshot(session) == before
    assert (await session.execute(
        select(func.count()).select_from(RigEvent))).scalar() == len(backup)


async def test_a_restore_brings_back_facts_but_not_the_schedule(client, session):
    """The thing that is easy to get wrong.

    Schedules are pushed by the desk, not filed by rigs, so they are not
    in the ledger and a restore does not bring them back. The facts are
    correct and complete; the floor board is empty until the desk pushes
    again. Better written down here than discovered at 3am.
    """
    await ingest(client, a_shift())
    await project_batch(session)
    backup = await the_backup(session)

    await burn_it_down(session)
    await restore(client, backup)
    await project_batch(session)

    assert (await session.execute(
        select(func.count()).select_from(Episode))).scalar() == 3
    assert (await session.execute(
        select(func.count()).select_from(Schedule))).scalar() == 0, (
        "if this ever passes, schedules have moved into the ledger and the "
        "runbook in the README needs to stop saying re-push"
    )


async def test_the_ledger_alone_is_enough_for_every_fact(client, session):
    """No fact depends on anything that is not in an envelope.

    The five fact tables are derived, and this is what "derived" has to
    mean: given the envelopes and nothing else - no schedules, no prior
    facts, no row ids - every one of them comes back.
    """
    await ingest(client, a_shift())
    await project_batch(session)
    before = await snapshot(session)

    for name, _ in FACT_MODELS:
        assert before[name], f"{name} was empty, so this proves nothing about it"

    backup = await the_backup(session)
    await burn_it_down(session)
    await restore(client, backup)
    await project_batch(session)

    after = await snapshot(session)
    for name, _ in FACT_MODELS:
        assert after[name] == before[name], f"{name} did not come back"
