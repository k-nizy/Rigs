"""The floor sweep, and what the desk reads.

`sweep()` is the only thing in this system that can see an absence. The
domain bus handles events; nothing publishes "no operator arrived" or
"the machine lost power", because there is nobody there to publish it.
So this runs on a timer, looks at what is true, and reconciles the open
alerts against it.

Reconcile, not append. Every sweep produces the complete set of things
currently wrong; anything open that this sweep did not produce has
stopped being wrong and is closed. That means no rule needs a matching
all-clear, and a sweep that runs every two seconds does not produce a
stream of duplicates.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.domains.alerts.repository import AlertRepository
from core.domains.episodes.model import Episode
from core.domains.rig_downtime_events.model import RigDowntimeEvent
from core.domains.rig_events.model import RigEvent
from core.domains.rig_productivity_blocks.model import RigProductivityBlock
from core.domains.rig_shift_checks.model import RigShiftCheck
from core.domains.rig_status.model import RigStatus
from core.domains.rig_status.repository import RigStatusRepository
from core.domains.schedules.model import Schedule
from core.rules import floor as rules
from core.rules.efficiency import Stint, efficiency
from core.workflows.schedules import in_force


async def _projection_lag(session: AsyncSession) -> tuple[datetime | None, int]:
    """The oldest event still waiting to become a fact, and how many.

    `received_at`, not `at`: the server's own clock. A rig with a wrong
    clock must not be able to invent this or hide it - this is the alert
    that says whether the others can be believed.
    """
    rows = await session.execute(
        select(func.min(RigEvent.received_at), func.count())
        .where(RigEvent.projected_at.is_(None))
    )
    oldest, waiting = rows.one()
    return oldest, int(waiting or 0)


async def _last_event_at(session: AsyncSession, rig_id: str) -> datetime | None:
    rows = await session.execute(
        select(func.max(RigEvent.at)).where(RigEvent.rig_id == rig_id)
    )
    return rows.scalar()


async def _open_downtime(session: AsyncSession) -> list[RigDowntimeEvent]:
    rows = await session.execute(
        select(RigDowntimeEvent).where(RigDowntimeEvent.up_at.is_(None))
    )
    return list(rows.scalars().all())


async def _repeat_faults(session: AsyncSession, since_days: int,
                         now: datetime) -> list[tuple[str, str, int]]:
    """Same subsystem, same rig, counted by distinct shift.

    Counted by shift rather than by occurrence on purpose: a gripper that
    fails four times in one shift is one bad day, and a gripper that fails
    once on each of four shifts is a gripper.
    """
    cutoff = (now - timedelta(days=since_days)).date()
    rows = await session.execute(
        select(
            RigShiftCheck.rig_id,
            RigShiftCheck.subsystem,
            func.count(func.distinct(RigShiftCheck.shift_date)),
        )
        .where(
            RigShiftCheck.event == "fault_opened",
            RigShiftCheck.subsystem.is_not(None),
            RigShiftCheck.shift_date >= cutoff,
        )
        .group_by(RigShiftCheck.rig_id, RigShiftCheck.subsystem)
    )
    return [(r[0], r[1], r[2]) for r in rows.all()]


async def _schedules_for(session: AsyncSession, blocks: list[Any]) -> dict:
    """The schedule in force for each block's shift, in one query.

    Keyed on (rig, shift_date, shift_label) - the same key
    `ScheduleRepository.for_shift` uses one at a time. A shift can have
    been pushed more than once, and the latest push wins, which is why
    this walks in `pushed_at` order and lets later rows overwrite earlier
    ones rather than picking arbitrarily.
    """
    wanted = {(b.rig_id, b.shift_date, b.shift_label) for b in blocks
              if b.turn_from is not None}
    if not wanted:
        return {}

    rows = await session.execute(
        select(Schedule)
        .where(
            tuple_(Schedule.rig_id, Schedule.shift_date, Schedule.shift_label)
            .in_(list(wanted))
        )
        .order_by(Schedule.pushed_at)
    )
    return {(s.rig_id, s.shift_date, s.shift_label): s for s in rows.scalars().all()}


async def _overruns(session: AsyncSession, look_back_hours: int,
                    now: datetime) -> list[Any]:
    """Blocks that ended inside the look-back window.

    `now` is a parameter, not `datetime.now()`. It used to read the wall
    clock while `sweep()` was handed an injected one, which made the sweep
    quietly not a function of its own argument: the same call gave
    different answers on different days. The tests that caught it were
    controls asserting a real overrun still fires, and they passed in the
    morning and failed in the afternoon - which is the worst way for a
    test to be wrong.

    It also matters beyond tests. Sweeping for a historical instant - a
    replay, a backfill - would have silently used today's window.
    """
    cutoff = now - timedelta(hours=look_back_hours)
    rows = await session.execute(
        select(RigProductivityBlock).where(RigProductivityBlock.ended_at >= cutoff)
    )
    return list(rows.scalars().all())


async def sweep(
    session: AsyncSession,
    now: datetime | None = None,
    silent_after_secs: int = 180,
    idle_grace_secs: int = 300,
    clock_tolerance_secs: int = 120,
    projection_behind_after_secs: int = 120,
    repeat_fault_shifts: int = 3,
    repeat_fault_days: int = 7,
    overrun_look_back_hours: int = 24,
) -> dict[str, int]:
    """Look at the floor, reconcile the alerts, report what changed."""
    now = now or datetime.now(timezone.utc)
    found: list[rules.Alert] = []

    statuses = {s.rig_id: s for s in await RigStatusRepository(session).everything()}
    known_rigs = set(statuses)

    # Rigs the desk has pushed to count too, even if they have never once
    # been heard from - a rig missing from the board is the failure this
    # is meant to catch, not an empty row.
    scheduled = await session.execute(select(Schedule.rig_id).distinct())
    known_rigs |= {r[0] for r in scheduled.all()}

    for rig_id in sorted(known_rigs):
        status: RigStatus | None = statuses.get(rig_id)

        silent = rules.rig_silent(
            rig_id, status.last_seen_at if status else None, now, silent_after_secs
        )
        if silent:
            found.append(silent)
            # A rig nobody can hear is not also idle - one cause, one alert.
            continue

        # Checked before idleness is judged, because a wrong clock is one
        # of the things that manufactures a false idle.
        adrift = rules.clock_adrift(
            rig_id, status.skew_secs if status else None, clock_tolerance_secs
        )
        if adrift:
            found.append(adrift)

        # The schedule covering `now`, not the newest row. One push writes
        # all three shifts of the day with a single `pushed_at`, so "newest"
        # is a three-way tie the database breaks however it likes - and an
        # idle check run against the wrong shift finds no turn in progress
        # and says nothing at all. A turn nobody arrived for is one of the
        # two absences this sweep exists to catch; it cannot be looked for
        # against a sheet that is not running.
        sched = await in_force(session, rig_id, now)
        if sched is None:
            continue

        in_progress = rules.turn_in_progress(
            sched.payload, sched.shift_date, now, now.tzinfo
        )
        if in_progress is not None:
            idle = rules.rig_idle(
                rig_id,
                in_progress["turn"]["from"],
                in_progress["start"],
                await _last_event_at(session, rig_id),
                now,
                idle_grace_secs,
            )
            if idle:
                found.append(idle)

    # Ourselves. Everything above asks whether the floor is working; this
    # asks whether the answers can be trusted.
    oldest, waiting = await _projection_lag(session)
    behind = rules.projection_behind(oldest, waiting, now, projection_behind_after_secs)
    if behind:
        found.append(behind)

    for row in await _open_downtime(session):
        found.append(
            rules.rig_down(row.rig_id, row.issue, row.needs_manager, row.down_at, now)
        )

    for rig_id, subsystem, shifts in await _repeat_faults(
            session, repeat_fault_days, now):
        hit = rules.repeat_fault(rig_id, subsystem, shifts, repeat_fault_shifts)
        if hit:
            found.append(hit)

    blocks = await _overruns(session, overrun_look_back_hours, now)
    # One query, not one per block. A day's blocks on a twelve-rig floor
    # is ~400 rows, and looking the schedule up inside the loop made that
    # ~400 sequential round trips every fifteen seconds - measured at
    # ~190ms of a 213ms sweep, which is almost all of it. The distinct
    # shifts behind those blocks number in the single digits.
    block_schedules = await _schedules_for(session, blocks)

    for block in blocks:
        # The schedule that was in force for *this block's* shift, not
        # whatever happens to have been pushed most recently. Turn labels
        # like "01:00" repeat on every shift of every day, so matching a
        # finished block against the current schedule silently measures it
        # against a boundary on another date - which read as a turn that
        # had overrun by twenty-three hours.
        sched = block_schedules.get((block.rig_id, block.shift_date, block.shift_label))
        if sched is None or block.turn_from is None:
            continue
        turn = next(
            (t for t in sched.payload.get("turns", []) if t.get("from") == block.turn_from),
            None,
        )
        if turn is None:
            continue
        # The floor's zone, not the server's - an overrun is measured
        # against a boundary that happened on the floor.
        zone = rules.payload_zone(sched.payload, now.tzinfo)
        _, boundary = rules.turn_bounds(block.shift_date, turn, zone)
        hit = rules.turn_overran(
            block.rig_id, turn["from"], turn["to"], block.ended_at, boundary
        )
        if hit:
            found.append(hit)

    alerts = AlertRepository(session)
    opened = 0
    for a in found:
        if await alerts.raise_(a.key, a.kind, a.rig_id, a.detail, now):
            opened += 1
    resolved = await alerts.resolve({a.key for a in found}, now)

    await session.commit()
    return {"found": len(found), "opened": opened, "resolved": resolved}


async def floor_state(session: AsyncSession, now: datetime | None = None) -> dict:
    """What the desk's Live board reads.

    Per rig: who the schedule says is on it, when it was last heard from,
    how far its clock is out, and whether anything is open against it.
    """
    now = now or datetime.now(timezone.utc)
    statuses = {s.rig_id: s for s in await RigStatusRepository(session).everything()}

    scheduled = await session.execute(select(Schedule.rig_id).distinct())
    rig_ids = sorted(set(statuses) | {r[0] for r in scheduled.all()})

    open_by_rig: dict[str, list] = {}
    for a in await AlertRepository(session).open_alerts():
        open_by_rig.setdefault(a.rig_id, []).append(
            {"kind": a.kind, "detail": a.detail, "openedAt": a.opened_at.isoformat()}
        )

    rigs = []
    for rig_id in rig_ids:
        status = statuses.get(rig_id)
        # The same rule that decides what the rig is served, so the board
        # and the floor cannot disagree about which shift is running.
        sched = await in_force(session, rig_id, now)
        turn = (
            rules.turn_in_progress(sched.payload, sched.shift_date, now, now.tzinfo)
            if sched else None
        )
        rigs.append({
            "rigId": rig_id,
            "task": (sched.payload.get("task") if sched else None),
            "shift": (
                {"date": sched.shift_date.isoformat(), "label": sched.shift_label}
                if sched else None
            ),
            "operator": (turn["turn"].get("operator") if turn else None),
            "turnFrom": (turn["turn"]["from"] if turn else None),
            "secondsLeft": (
                int((turn["end"] - now).total_seconds()) if turn else None
            ),
            "lastSeenAt": status.last_seen_at.isoformat() if status else None,
            "skewSecs": status.skew_secs if status else None,
            "alerts": open_by_rig.get(rig_id, []),
        })

    # Whether this answer can be trusted. Every number above is derived
    # from facts, and facts are produced by a worker that can die without
    # saying so - in which case this whole board is a confident report
    # about a floor that moved on hours ago. The measurements go out
    # beside it rather than a verdict, same as everywhere else here.
    oldest, waiting = await _projection_lag(session)
    backend = {
        "unprojectedEvents": waiting,
        "projectionLagSecs": (
            round((now - oldest).total_seconds(), 1) if oldest else 0.0
        ),
    }

    return {"at": now.isoformat(), "rigs": rigs, "backend": backend}


async def operator_efficiency(session: AsyncSession, shift_date, shift_label: str) -> list[dict]:
    """Efficiency per operator for one shift, computed at read time.

    The four seconds columns are what is stored; this is the only place
    the ratio is made, from one definition, so correcting the formula
    corrects every shift ever recorded.
    """
    rows = await session.execute(
        select(
            RigProductivityBlock.operator_id,
            func.sum(RigProductivityBlock.recorded_secs),
            func.sum(RigProductivityBlock.assigned_secs),
            func.sum(RigProductivityBlock.fault_secs),
            func.sum(RigProductivityBlock.down_secs),
            func.sum(RigProductivityBlock.episodes),
        )
        .where(
            RigProductivityBlock.shift_date == shift_date,
            RigProductivityBlock.shift_label == shift_label,
        )
        .group_by(RigProductivityBlock.operator_id)
        .order_by(RigProductivityBlock.operator_id)
    )
    out = []
    for op, rec, asg, flt, dwn, eps in rows.all():
        stint = Stint(float(rec), float(asg), float(flt), float(dwn))
        out.append({
            "operatorId": op,
            "episodes": int(eps),
            "recordedSecs": float(rec),
            "assignedSecs": float(asg),
            "faultSecs": float(flt),
            "downSecs": float(dwn),
            "efficiency": round(efficiency(stint), 4),
        })
    return out


async def scores_for_shift(session: AsyncSession, shift_date, shift_label: str) -> list[dict]:
    """The scores every person gave their own takes, one row per person,
    for one shift. A manager sees these; nothing here changes one.

    Grouped by person, never by seat. `operator_id` is a chair, and
    grouping by it credits Nadia's takes and Priya's cover day to one
    row with the wrong average for both - the exact merge the person id
    was built to end. A take filed before the rig sent a person has no
    id to group by; it falls back to the seat and name it carries and
    the row says so, rather than being dropped or merged into somebody.

    Counts and a mean, computed here and stored nowhere. A discarded
    take is counted as recorded - a floor where nothing is ever
    discarded is worth asking about - and never enters the average,
    because it was never scored.
    """
    rows = await session.execute(
        select(
            Episode.person_id, Episode.operator_id, Episode.operator_name,
            Episode.outcome, Episode.score, func.count(),
        )
        .where(Episode.shift_date == shift_date, Episode.shift_label == shift_label)
        .group_by(Episode.person_id, Episode.operator_id, Episode.operator_name,
                  Episode.outcome, Episode.score)
    )

    people: dict[tuple, dict] = {}
    for person_id, seat, name, outcome, score, n in rows.all():
        # A person is their id. Without one, the seat and the name
        # together are the best this row can do - both, so two seat-only
        # people who happened to share a chair on different days are not
        # folded into one.
        key = ("person", str(person_id)) if person_id else ("seat", seat, name)
        row = people.setdefault(key, {
            "personId": str(person_id) if person_id else None,
            "seat": seat, "name": name,
            "recorded": 0, "saved": 0, "discarded": 0,
            "scored": {"3": 0, "4": 0, "5": 0},
            "_sum": 0,
        })
        row["recorded"] += n
        if outcome == "saved":
            row["saved"] += n
            if score is not None:
                row["scored"][str(score)] = row["scored"].get(str(score), 0) + n
                row["_sum"] += score * n
        else:
            row["discarded"] += n

    out = []
    for row in people.values():
        scored = sum(row["scored"].values())
        row["average"] = round(row["_sum"] / scored, 2) if scored else None
        del row["_sum"]
        out.append(row)
    out.sort(key=lambda r: (r["name"] or "", r["seat"] or ""))
    return out
