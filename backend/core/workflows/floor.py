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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domains.alerts.repository import AlertRepository
from core.domains.rig_downtime_events.model import RigDowntimeEvent
from core.domains.rig_events.model import RigEvent
from core.domains.rig_productivity_blocks.model import RigProductivityBlock
from core.domains.rig_shift_checks.model import RigShiftCheck
from core.domains.rig_status.model import RigStatus
from core.domains.rig_status.repository import RigStatusRepository
from core.domains.schedules.model import Schedule
from core.domains.schedules.repository import ScheduleRepository
from core.rules import floor as rules
from core.rules.efficiency import Stint, efficiency


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


async def _current_schedule(session: AsyncSession, rig_id: str) -> Schedule | None:
    rows = await session.execute(
        select(Schedule).where(Schedule.rig_id == rig_id)
        .order_by(Schedule.pushed_at.desc()).limit(1)
    )
    return rows.scalar_one_or_none()


async def _open_downtime(session: AsyncSession) -> list[RigDowntimeEvent]:
    rows = await session.execute(
        select(RigDowntimeEvent).where(RigDowntimeEvent.up_at.is_(None))
    )
    return list(rows.scalars().all())


async def _repeat_faults(session: AsyncSession, since_days: int) -> list[tuple[str, str, int]]:
    """Same subsystem, same rig, counted by distinct shift.

    Counted by shift rather than by occurrence on purpose: a gripper that
    fails four times in one shift is one bad day, and a gripper that fails
    once on each of four shifts is a gripper.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).date()
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


async def _overruns(session: AsyncSession, look_back_hours: int) -> list[Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=look_back_hours)
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

        sched = await _current_schedule(session, rig_id)
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

    for rig_id, subsystem, shifts in await _repeat_faults(session, repeat_fault_days):
        hit = rules.repeat_fault(rig_id, subsystem, shifts, repeat_fault_shifts)
        if hit:
            found.append(hit)

    schedules = ScheduleRepository(session)
    for block in await _overruns(session, overrun_look_back_hours):
        # The schedule that was in force for *this block's* shift, not
        # whatever happens to have been pushed most recently. Turn labels
        # like "01:00" repeat on every shift of every day, so matching a
        # finished block against the current schedule silently measures it
        # against a boundary on another date - which read as a turn that
        # had overrun by twenty-three hours.
        sched = await schedules.for_shift(
            block.rig_id, block.shift_date, block.shift_label
        )
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
        sched = await _current_schedule(session, rig_id)
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
