"""Ledger -> fact tables.

A cross-domain orchestration: it reads `rig_events` and writes the five
fact domains, so it lives here rather than inside any one of them.

The property this file exists to protect is **replay**. Wipe every fact
table, run the whole ledger through again, and the result must be
identical. That is what makes a projection bug survivable: the events are
the truth, the tables are a reading of them, and a reading can be
corrected six months later.

Two things follow from that and shape every function below:

  Projection is idempotent per ledger row. `source_event` carries the
  ledger id and the unique constraints are on it, so processing a row
  twice cannot double a fact.

  Nothing here derives a rotation. Who was on a rig comes from the
  envelope, which got it from the pushed payload. This module has no
  opinion about schedules and could not compute one if asked.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.domains.episodes.model import Episode
from core.domains.rig_downtime_events import lifecycle as downtime_lifecycle
from core.domains.rig_downtime_events.model import RigDowntimeEvent
from core.domains.rig_events.model import RigEvent
from core.domains.rig_productivity_blocks.model import RigProductivityBlock
from core.domains.rig_shift_checks.model import RigShiftCheck
from core.domains.sessions import lifecycle as session_lifecycle
from core.domains.sessions.model import Session


def _key(ev: RigEvent) -> dict[str, Any]:
    """The fields every fact row carries, all of them from the schedule
    the desk pushed.

    operator_id is the seat; operator_name is the person who sat in it.
    Both, because the seat is what the sheet is written in and the person
    is what a take has to be traced to. None on a row projected from an
    event filed before the name travelled.
    """
    return dict(
        rig_id=ev.rig_id,
        shift_date=ev.shift_date,
        shift_label=ev.shift_label,
        turn_from=ev.turn_from,
        operator_id=ev.operator_id,
        operator_name=ev.operator_name,
    )


async def _open_session(session: AsyncSession, ev: RigEvent) -> Session:
    """Find or open the session this event belongs to.

    Opened by the first event under a turn, because the schedule already
    says when a turn starts - there is no session_started event and there
    should not be one.
    """
    rows = await session.execute(
        select(Session).where(
            Session.rig_id == ev.rig_id,
            Session.shift_date == ev.shift_date,
            Session.shift_label == ev.shift_label,
            Session.turn_from.is_(ev.turn_from) if ev.turn_from is None else Session.turn_from == ev.turn_from,
            Session.operator_id.is_(ev.operator_id) if ev.operator_id is None else Session.operator_id == ev.operator_id,
        )
    )
    found = rows.scalar_one_or_none()
    if found is not None:
        return found
    opened = Session(**_key(ev), started_at=ev.at)
    session.add(opened)
    await session.flush()
    return opened


async def _stint_totals(
    session: AsyncSession, ev: RigEvent, sess: Session
) -> tuple[int, float, float]:
    """What the operator actually did this turn, from facts already held.

    `stint_ended` carries the rig's own running counters, and those live
    in `S`, which `boot()` rebuilds from zero on every start. A reload, a
    crash or a kiosk restart therefore files a block describing only the
    work done *since* the restart. The episodes before it are not lost -
    the rig journals every event before it touches the network, so they
    are in this ledger - but the block that judges the operator does not
    count them, and there is no correction mechanism downstream.

    So it is derived rather than transcribed, which is the rule every
    other fact table here already follows: the ledger is the system, and
    everything else is rebuilt from it. That also makes existing wrong
    blocks correctable by replay, which transcribed ones never were.

    Both numbers have to move together. `assigned_secs` is the
    denominator, and it resets with everything else; deriving the
    numerator alone would let a restarted stint report a full turn's work
    against a few minutes of assigned time, and `efficiency()` clamps to
    1.0 - so the bug would be rounded away into a perfect score.

    - episodes and recorded_secs come from the Episode rows for this
      turn, projected in ledger order before this event. Only saved
      takes: a discarded one recorded nothing.
    - assigned_secs is wall clock, `started_at` to now. `_open_session`
      finds-or-opens by the turn key rather than per boot, so the start
      is the first event of the turn and survives a restart.

    The turn's scheduled length would be the more literal reading of
    "assigned", and `floor.py` already looks turns up that way. It is
    deliberately not used here: schedules are pushed, not filed, so they
    are not in the ledger. A database restored from envelopes alone comes
    back with every fact and no schedules at all, and `test_recovery.py`
    asserts the facts come back *identical*. Reading the length from a
    schedule would make this one column unreproducible from the ledger,
    which is the property the whole design is arranged around. Every
    input here is an event timestamp or an episode row, so replay stays
    self-contained.

    `fault_secs` and `down_secs` still come from the payload and reset
    the same way. Left alone deliberately: fault time has no table of its
    own - `rig_downtime_events` holds only `rig_down`/`rig_up`, while the
    rig accrues `faultSecs` separately in `fault_fixing` - so deriving
    one and not the other would be inconsistent. They fail in the safe
    direction: too small a subtrahend makes chargeable time larger and
    efficiency lower, so a restart cannot flatter anybody.
    """
    key = [
        Episode.rig_id == ev.rig_id,
        Episode.shift_date == ev.shift_date,
        Episode.shift_label == ev.shift_label,
        Episode.turn_from.is_(None) if ev.turn_from is None
        else Episode.turn_from == ev.turn_from,
        Episode.operator_id.is_(None) if ev.operator_id is None
        else Episode.operator_id == ev.operator_id,
        Episode.outcome == "saved",
    ]
    # Episodes for this turn were added to this same unit of work a moment
    # ago and may still be pending. Flush so the aggregate sees them.
    await session.flush()
    rows = await session.execute(
        select(func.count(), func.coalesce(func.sum(Episode.duration_secs), 0.0))
        .where(*key)
    )
    episodes, recorded = rows.one()

    assigned = (ev.at - sess.started_at).total_seconds()
    # A stint whose first event is the one ending it. Not an error; the
    # clamp in efficiency() already treats no chargeable time as zero.
    return int(episodes), float(recorded), max(0.0, assigned)


async def _open_downtime(session: AsyncSession, ev: RigEvent) -> RigDowntimeEvent | None:
    rows = await session.execute(
        select(RigDowntimeEvent)
        .where(RigDowntimeEvent.rig_id == ev.rig_id, RigDowntimeEvent.up_at.is_(None))
        .order_by(RigDowntimeEvent.down_at.desc())
        .limit(1)
    )
    return rows.scalar_one_or_none()


# Events that prove a rig is working, for closing an outage nobody
# closed. Deliberately short.
#
# A passed shift check means somebody stood at the rig and confirmed it.
# An episode means somebody recorded with it. Both are impossible on a
# broken rig.
#
# What is NOT here matters as much. A heartbeat proves the software is
# running and says nothing about the gripper. `shift_check` with outcome
# "raised" is only the checklist appearing on boot, not passing it. A
# `stint_ended` can describe a turn that was entirely downtime. And
# `fault_opened` is the opposite of evidence.
PROVES_IT_WORKS = {"episode_saved", "episode_discarded"}
PASSED_CHECK = {"passed", "passed_early"}


def _is_working(ev: RigEvent, data: dict) -> bool:
    if ev.event in PROVES_IT_WORKS:
        return True
    return ev.event == "shift_check" and data.get("outcome") in PASSED_CHECK


async def project_one(session: AsyncSession, ev: RigEvent) -> str:
    """Apply one ledger row. Returns what it did, for the worker's log."""
    data = (ev.envelope or {}).get("data", {})

    # Every event belongs to a turn, so every event can open the session
    # for it. A rig on standby has no turn and no operator, and its events
    # still land - the key is simply (rig, shift, null, null).
    sess = await _open_session(session, ev)

    # An outage nobody closed. Only rig_up used to close one, and there
    # are ordinary ways for it never to arrive - the operator ends their
    # session with the rig down, a technician fixes it, and the rig comes
    # back with no memory of the outage. The row then stayed open for
    # ever and the floor board showed a working rig as down.
    if _is_working(ev, data):
        stuck = await _open_downtime(session, ev)
        if stuck is not None:
            downtime_lifecycle.resume(stuck, ev.at)

    if ev.event in ("episode_saved", "episode_discarded"):
        saved = ev.event == "episode_saved"
        session.add(
            Episode(
                episode_id=data["episodeId"],
                **_key(ev),
                at=ev.at,
                duration_secs=float(data.get("durationSecs", 0)),
                outcome="saved" if saved else "discarded",
                score=int(data["score"]) if saved and data.get("score") is not None else None,
                source_event=ev.id,
            )
        )
        return f"episode {'saved' if saved else 'discarded'}"

    if ev.event in (
        "shift_check", "fault_opened", "fault_reclassified",
        "fault_closed", "fault_cancelled",
    ):
        session.add(
            RigShiftCheck(
                **_key(ev),
                at=ev.at,
                event=ev.event,
                outcome=data.get("outcome"),
                subsystem=data.get("subsystem"),
                seconds_charged=float(data.get("secondsCharged", 0)),
                source_event=ev.id,
            )
        )
        return ev.event

    if ev.event == "rig_down":
        # A rig cannot be doubly down. Pressing through the issue tree
        # twice is one outage.
        if await _open_downtime(session, ev) is not None:
            return "rig_down ignored, one already open"
        session.add(
            RigDowntimeEvent(
                **_key(ev),
                down_at=ev.at,
                issue=data.get("issue", "unknown"),
                issue_path=data.get("issuePath"),
                needs_manager=bool(data.get("needsManager", False)),
                charged_to=data.get("chargedTo", "rig"),
                source_event=ev.id,
            )
        )
        return "rig_down"

    if ev.event == "rig_up":
        open_row = await _open_downtime(session, ev)
        if open_row is None:
            # Replay can start mid-ledger; the matching rig_down may
            # simply not be in range. Not an error.
            return "rig_up with nothing open, ignored"
        downtime_lifecycle.close(open_row, ev.at, data.get("downSecs"),
                                 ended_by=downtime_lifecycle.OPERATOR)
        return "rig_up"

    if ev.event == "stint_ended":
        episodes, recorded_secs, assigned_secs = await _stint_totals(session, ev, sess)
        session.add(
            RigProductivityBlock(
                **_key(ev),
                ended_at=ev.at,
                episodes=episodes,
                recorded_secs=recorded_secs,
                assigned_secs=assigned_secs,
                fault_secs=float(data.get("faultSecs", 0)),
                down_secs=float(data.get("downSecs", 0)),
                source_event=ev.id,
            )
        )
        # A stint reaching its boundary is the ordinary way a turn ends.
        session_lifecycle.close(sess, ev.at, session_lifecycle.HANDOVER)
        return "stint_ended"

    if ev.event == "session_ended":
        session_lifecycle.close(
            sess, ev.at, data.get("endedBy", session_lifecycle.OPERATOR)
        )
        return "session_ended"

    # An event the projection has no reading for yet is left unprojected
    # rather than dropped. The ledger keeps it; a later version can claim
    # it. This is the whole reason the ledger exists.
    return f"no projection for {ev.event}"


async def project_batch(session: AsyncSession, limit: int = 500) -> tuple[int, list[str]]:
    """Claim and project the outstanding tail of the ledger.

    Ordered by id, which is insertion order, so a rig_down is always seen
    before the rig_up that closes it.

    The rows are locked as they are claimed, so more than one worker can
    run this safely. That matters because running one is a single point of
    failure and the first thing anyone does about that is run two.
    """
    rows = await session.execute(
        select(RigEvent)
        .where(RigEvent.projected_at.is_(None))
        .order_by(RigEvent.id)
        .limit(limit)
        # An actual claim, not a look. Without the lock this was a plain
        # SELECT, so two workers - which is what anyone running this for
        # availability will start - both read the same tail and both
        # project it. Most of it collides on a unique constraint and rolls
        # back harmlessly, but a batch of nothing but shift checks against
        # an existing session has nothing to collide with, and quietly
        # doubles. SKIP LOCKED rather than waiting: the second worker
        # should take the next batch, not queue behind this one.
        .with_for_update(skip_locked=True)
    )
    events = list(rows.scalars().all())
    if not events:
        return 0, []

    done: list[str] = []
    now = datetime.now(tz=events[0].at.tzinfo)
    for ev in events:
        done.append(await project_one(session, ev))
        ev.projected_at = now

    await session.commit()
    return len(events), done


async def reset_projections(session: AsyncSession) -> None:
    """Wipe every fact table and mark the whole ledger outstanding again.

    This is what makes a projection bug survivable, and it is exercised by
    the test suite rather than being a claim in a comment: correct the
    reading, wipe, replay, and every shift ever recorded comes back right.
    """
    for model in (Episode, RigShiftCheck, RigDowntimeEvent, RigProductivityBlock, Session):
        await session.execute(model.__table__.delete())
    await session.execute(RigEvent.__table__.update().values(projected_at=None))
    await session.commit()
