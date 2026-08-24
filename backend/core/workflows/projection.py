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

from sqlalchemy import select
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
    """The five fields every fact row carries, all of them from the
    schedule the desk pushed."""
    return dict(
        rig_id=ev.rig_id,
        shift_date=ev.shift_date,
        shift_label=ev.shift_label,
        turn_from=ev.turn_from,
        operator_id=ev.operator_id,
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


async def _open_downtime(session: AsyncSession, ev: RigEvent) -> RigDowntimeEvent | None:
    rows = await session.execute(
        select(RigDowntimeEvent)
        .where(RigDowntimeEvent.rig_id == ev.rig_id, RigDowntimeEvent.up_at.is_(None))
        .order_by(RigDowntimeEvent.down_at.desc())
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def project_one(session: AsyncSession, ev: RigEvent) -> str:
    """Apply one ledger row. Returns what it did, for the worker's log."""
    data = (ev.envelope or {}).get("data", {})

    # Every event belongs to a turn, so every event can open the session
    # for it. A rig on standby has no turn and no operator, and its events
    # still land - the key is simply (rig, shift, null, null).
    sess = await _open_session(session, ev)

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
        downtime_lifecycle.close(open_row, ev.at, data.get("downSecs"))
        return "rig_up"

    if ev.event == "stint_ended":
        session.add(
            RigProductivityBlock(
                **_key(ev),
                ended_at=ev.at,
                episodes=int(data.get("episodes", 0)),
                recorded_secs=float(data.get("recordedSecs", 0)),
                assigned_secs=float(data.get("assignedSecs", 0)),
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
    """
    rows = await session.execute(
        select(RigEvent)
        .where(RigEvent.projected_at.is_(None))
        .order_by(RigEvent.id)
        .limit(limit)
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
