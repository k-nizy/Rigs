"""The rig-facing routes. Nine planned; these are Phase 1's four.

Note what is absent: nothing here computes a rotation, and nothing
accepts a percentage. The server stores the schedule it was pushed and
reads it back; it never derives one.
"""

import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.domains.alerts.repository import AlertRepository
from core.domains.rig_events.repository import RigEventRepository
from core.domains.rig_events.schema import EventBatch, IngestResult
from core.domains.rig_status.repository import RigStatusRepository
from core.domains.schedules.model import Schedule
from core.domains.schedules.repository import ScheduleRepository
from core.infrastructure.config import get_settings
from core.infrastructure.database import get_session
from core.workflows.floor import floor_state, operator_efficiency

router = APIRouter()


class CursorOut(BaseModel):
    rigId: str
    seq: int


class HeartbeatIn(BaseModel):
    at: datetime


class HeartbeatOut(BaseModel):
    serverTime: datetime
    skewSecs: float


@router.get("/rigs/{rig_id}/cursor", response_model=CursorOut)
async def cursor(rig_id: str, session: AsyncSession = Depends(get_session)) -> CursorOut:
    """The highest seq held for this rig, or -1 if none. The uploader asks
    on reconnect and resends only what follows."""
    return CursorOut(rigId=rig_id, seq=await RigEventRepository(session).cursor(rig_id))


@router.post("/rigs/{rig_id}/events", response_model=IngestResult)
async def ingest(
    rig_id: str, batch: EventBatch, session: AsyncSession = Depends(get_session)
) -> IngestResult:
    """Append a batch to the ledger. Idempotent on (rig_id, event_id), so a
    rig may resend the same batch any number of times and land it once.

    Validation is all-or-nothing: one bad envelope rejects the batch, the
    same rule the schedule push already follows. A half-accepted batch is
    worse than a rejected one.
    """
    for ev in batch.events:
        if ev.rig_id != rig_id:
            raise HTTPException(
                status_code=422,
                detail=f"event {ev.event_id} says rigId={ev.rig_id}, posted to {rig_id}",
            )

    schedules = ScheduleRepository(session)
    events = RigEventRepository(session)

    # Resolve which schedule version was in force, so every row can be
    # joined back to it. A missing schedule is not a rejection - the event
    # is the fact, the link can be filled in later.
    push_cache: dict[tuple[str, str], object] = {}
    rows = []
    for ev in batch.events:
        key = (ev.shift_date, ev.shift_label)
        if key not in push_cache:
            sched = await schedules.for_shift(
                rig_id, date.fromisoformat(ev.shift_date), ev.shift_label
            )
            push_cache[key] = sched.push_id if sched else None
        rows.append(
            dict(
                rig_id=rig_id,
                event_id=ev.event_id,
                seq=ev.seq,
                at=ev.at,
                shift_date=date.fromisoformat(ev.shift_date),
                shift_label=ev.shift_label,
                turn_from=ev.turn_from,
                operator_id=ev.operator_id,
                bucket=ev.bucket,
                event=ev.event,
                envelope=ev.model_dump(by_alias=True, mode="json"),
                push_id=push_cache[key],
            )
        )

    accepted = await events.append(rows)
    await session.commit()
    return IngestResult(
        accepted=accepted,
        duplicates=len(rows) - accepted,
        cursor=await events.cursor(rig_id),
    )


@router.post("/rigs/{rig_id}/heartbeat", response_model=HeartbeatOut)
async def heartbeat(
    rig_id: str, beat: HeartbeatIn, session: AsyncSession = Depends(get_session)
) -> HeartbeatOut:
    """Proof of life, and a free clock-skew measurement.

    The two most valuable alerts on this floor are both absences - a rig
    idle at handover, a rig that lost power - and an event bus is
    structurally blind to them. There is no message to subscribe to. This
    is what the floor sweep reads instead.
    """
    now = datetime.now(timezone.utc)
    skew = (now - beat.at).total_seconds()
    # Recorded, not just answered. A heartbeat nobody stored cannot be
    # missed later, and being missed is the entire point of it.
    await RigStatusRepository(session).beat(rig_id, now, beat.at, skew)
    await session.commit()
    return HeartbeatOut(serverTime=now, skewSecs=skew)


@router.get("/rigs/{rig_id}/schedule")
async def schedule(rig_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """The payload this rig was pushed, returned verbatim. Stored opaque,
    read opaque - the rotation is computed in exactly one place and this
    is not it.

    Served at both `/schedule` and `/schedule.json`. The rig asks for the
    second because that is what the existing static server answers to,
    and a rig on the floor should not have to know which server it is
    talking to. The end-to-end test caught this: the rig fell through to
    generating its own schedule and said so on the wall, which is exactly
    what that badge is for.
    """
    sched = await ScheduleRepository(session).current_for_rig(rig_id)
    if sched is None:
        raise HTTPException(status_code=404, detail=f"nothing pushed to {rig_id}")
    return sched.payload



@router.get("/rigs/{rig_id}/schedule.json")
async def schedule_json(
    rig_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """The same payload, at the path the rig actually asks for.

    The existing static server answers `schedule.json`, and a rig on the
    floor should not have to know which server it is talking to. Declared
    as its own route rather than a second decorator on the one above:
    stacking registers in the OpenAPI schema but does not route.
    """
    return await schedule(rig_id, session)


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {"ok": True, "database": s.safe_url()}


# ------------------------------------------------------ the schedule in


class PushIn(BaseModel):
    """Twelve payloads in one request, as the desk already sends them."""

    payloads: list[dict] = Field(min_length=1, max_length=64)


class PushOut(BaseModel):
    pushId: uuid.UUID
    pushedAt: datetime
    count: int


@router.post("/schedules/push", response_model=PushOut)
async def push(body: PushIn, session: AsyncSession = Depends(get_session)) -> PushOut:
    """Store what the desk pushed, whole and unexamined.

    Validated for the handful of fields this service indexes on and
    nothing more. The payload is a contract between the desk and the rig,
    both of which run the same engine; this server is a courier and a
    filing cabinet, and the moment it starts having opinions about turns
    it becomes a third answer that can disagree.

    All-or-nothing, like the ingest route and for the same reason: a
    floor running half a schedule is worse than a floor running none.
    """
    push_id = uuid.uuid4()
    pushed_at = datetime.now(timezone.utc)
    rows = []

    for i, payload in enumerate(body.payloads):
        try:
            rig_id = payload["rigId"]
            shift = payload["shift"]
            shift_date = date.fromisoformat(shift["date"])
            shift_label = shift["label"]
            if not isinstance(payload.get("turns"), list):
                raise KeyError("turns")
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(
                status_code=422,
                detail=f"payloads[{i}] is not a schedule this service can file: {e}",
            )
        rows.append(
            Schedule(
                push_id=push_id, pushed_at=pushed_at, rig_id=rig_id,
                shift_date=shift_date, shift_label=shift_label, payload=payload,
            )
        )

    session.add_all(rows)
    await session.commit()
    return PushOut(pushId=push_id, pushedAt=pushed_at, count=len(rows))


# ------------------------------------------------------------- the floor


@router.get("/floor/state")
async def floor(session: AsyncSession = Depends(get_session)) -> dict:
    """The Live board: every rig, who is on it, when it was last heard
    from, and anything open against it."""
    return await floor_state(session)


@router.get("/floor/alerts")
async def alerts(session: AsyncSession = Depends(get_session)) -> dict:
    """Everything currently wrong on the floor.

    An alert is a state, not a message: it is here while the condition
    holds and gone when it clears.
    """
    rows = await AlertRepository(session).open_alerts()
    return {
        "open": [
            {
                "kind": a.kind, "rigId": a.rig_id, "detail": a.detail,
                "openedAt": a.opened_at.isoformat(),
            }
            for a in rows
        ]
    }


@router.get("/floor/efficiency")
async def efficiency_for_shift(
    shift_date: date, shift_label: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """Efficiency per operator, computed here and stored nowhere."""
    return {
        "shiftDate": shift_date.isoformat(),
        "shiftLabel": shift_label,
        "operators": await operator_efficiency(session, shift_date, shift_label),
    }
