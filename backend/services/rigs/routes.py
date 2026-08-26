"""The rig-facing routes. Nine planned; these are Phase 1's four.

Note what is absent: nothing here computes a rotation, and nothing
accepts a percentage. The server stores the schedule it was pushed and
reads it back; it never derives one.
"""

import logging
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.domains.alerts.repository import AlertRepository
from core.domains.rig_events.repository import RigEventRepository
from core.domains.rig_events.schema import EventBatch, IngestResult
from core.domains.rig_status.repository import RigStatusRepository
from core.domains.schedules.model import Schedule
from core.domains.schedules.repository import ScheduleRepository
from core.infrastructure.config import Settings, get_settings
from core.infrastructure.database import get_session
from core.workflows.floor import floor_state, operator_efficiency
from core.workflows.schedules import in_force as schedules_in_force
from services.rigs.auth import (
    desk_auth, desk_read_auth, rig_auth, rig_auth_for_key, rig_rate_limit,
)
from services.rigs.identity import caller_address, config_js, rig_at
from core.infrastructure.storage import Storage, get_storage
from core.workflows.video import VideoError, backlog, confirm, where_to_put

log = logging.getLogger("rigs.service")

router = APIRouter()


class CursorOut(BaseModel):
    rigId: str
    seq: int


class HeartbeatIn(BaseModel):
    at: datetime


class HeartbeatOut(BaseModel):
    serverTime: datetime
    skewSecs: float


@router.get("/rigs/{rig_id}/cursor", response_model=CursorOut, tags=["ingest"],
            dependencies=[Depends(rig_auth), Depends(rig_rate_limit)],
            summary="How far this rig's events have been accepted")
async def cursor(rig_id: str, session: AsyncSession = Depends(get_session)) -> CursorOut:
    """The highest seq held for this rig, or -1 if none. The uploader asks
    on reconnect and resends only what follows."""
    return CursorOut(rigId=rig_id, seq=await RigEventRepository(session).cursor(rig_id))


@router.post("/rigs/{rig_id}/events", response_model=IngestResult, tags=["ingest"],
             dependencies=[Depends(rig_auth), Depends(rig_rate_limit)],
             summary="File a batch of events. Safe to send twice")
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


@router.post("/rigs/{rig_id}/heartbeat", response_model=HeartbeatOut, tags=["ingest"],
             dependencies=[Depends(rig_auth), Depends(rig_rate_limit)],
             summary="Say the rig is alive, and learn how far its clock has drifted")
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


@router.get("/rigs/{rig_id}/schedule", tags=["schedules"],
            dependencies=[Depends(rig_auth), Depends(rig_rate_limit)],
            summary="The schedule currently in force for this rig")
async def schedule(rig_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """The payload in force for this rig right now, returned verbatim.

    Whichever pushed payload covers this instant, not whichever was
    pushed last. A payload covers one shift, so serving the newest is
    what left a rig holding a finished Morning schedule at 16:00 and
    sitting on Standby until somebody reloaded it.

    The choice is a comparison against the window the desk wrote into
    the payload, never a calculation - stored opaque, read opaque, the
    rotation is computed in exactly one place and this is not it.

    Served at both `/schedule` and `/schedule.json`. The rig asks for the
    second because that is what the existing static server answers to,
    and a rig on the floor should not have to know which server it is
    talking to. The end-to-end test caught this: the rig fell through to
    generating its own schedule and said so on the wall, which is exactly
    what that badge is for.
    """
    sched = await schedules_in_force(session, rig_id)
    if sched is None:
        raise HTTPException(status_code=404, detail=f"nothing pushed to {rig_id}")
    return sched.payload



@router.get("/rigs/{rig_id}/schedule.json", tags=["schedules"],
            dependencies=[Depends(rig_auth), Depends(rig_rate_limit)],
            summary="The same schedule, at the path the rig already fetches")
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


@router.get("/rigs/config.js", tags=["schedules"],
            response_class=Response,
            summary="Which rig this machine is, decided by where it called from")
async def rig_config_js(
    request: Request, s: Settings = Depends(get_settings)
) -> Response:
    """The `rig-config.js` the rig's page loads, rendered for the caller.

    Deliberately unauthenticated, and that is not an oversight: this is
    the route that hands a rig its token, so requiring one would be
    circular. What stands in for auth is that the caller cannot ask for
    anything - there is no rig id in the path, no query parameter, and no
    header that changes the answer. The address decides, and a caller at
    an address we do not know is told it is nobody.

    Served through nginx at `/apps/rig/rig-config.js`, which is where the
    page actually asks for it. That mapping lives in `deploy/nginx.conf`
    rather than here so the service keeps one prefix.
    """
    seen = caller_address(
        request.client.host if request.client else None,
        request.headers.get("x-real-ip"),
    )
    rig_id = rig_at(seen, s.rig_addresses)

    if rig_id is None and s.rig_addresses:
        # Worth a line in the log: on a floor this is a rig that has moved,
        # been re-imaged onto a new address, or a machine that should not
        # be asking. All three want somebody to look.
        log.warning("rig config requested from an address no rig is configured at: %s", seen)

    body = config_js(rig_id, s.rig_tokens.get(rig_id or ""), seen)
    return Response(
        content=body,
        media_type="text/javascript; charset=utf-8",
        # The one file that must never be cached. A stale copy is a rig
        # filing every episode under another rig's name, and nginx says
        # the same thing for the same reason.
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


# ------------------------------------------------------------- the video


class PresignIn(BaseModel):
    camera: str


class ConfirmIn(BaseModel):
    camera: str
    sha256: str = Field(min_length=64, max_length=64)
    bytes: int = Field(ge=0)


@router.post("/rigs/{rig_id}/episodes/{episode_id}/video:presign", tags=["video"],
             dependencies=[Depends(rig_auth), Depends(rig_rate_limit)],
             summary="Ask where to put one camera's video")
async def video_presign(
    rig_id: str, episode_id: str, body: PresignIn,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Where the rig should put this camera's video.

    The bytes do not come back through here. At roughly 2.6 TB a day,
    streaming video through application workers is the difference between
    a storage system and an outage.
    """
    try:
        return await where_to_put(session, episode_id, body.camera)
    except VideoError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/rigs/{rig_id}/episodes/{episode_id}/video:complete", tags=["video"],
             dependencies=[Depends(rig_auth), Depends(rig_rate_limit)],
             summary="Confirm what landed. Only a yes here releases the rig's copy")
async def video_complete(
    rig_id: str, episode_id: str, body: ConfirmIn,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The rig says what it sent; the server checks what landed.

    Answering yes here is what permits the rig to delete its own copy, so
    it is the one place in the video path that must not be optimistic. A
    mismatch is a 409 and the rig keeps its bytes.
    """
    try:
        return await confirm(session, episode_id, body.camera, body.sha256, body.bytes)
    except VideoError as e:
        detail = str(e)
        missing = detail.startswith("no episode")
        if not missing:
            # The rig is about to keep bytes it hoped to be rid of, and
            # will try again. If this line repeats for one episode, the
            # take is not going to make it and somebody should look.
            log.warning("video refused for %s episode %s camera %s: %s",
                        rig_id, episode_id, body.camera, detail)
        raise HTTPException(status_code=404 if missing else 409, detail=detail)


@router.put("/storage/{key:path}", tags=["video"],
            dependencies=[Depends(rig_auth_for_key)],
            summary="Take one camera's video, when the store cannot be written to directly")
async def storage_put(
    key: str, request: Request,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict:
    """The other upload model: bytes through the service.

    Which of the two the floor actually uses is the open question - a
    presigned PUT straight to the object store, or a stream through the
    gateway - and it is not answered here. `upload_target()` decides, and
    the rig does whatever it is told. This route exists because the local
    stand-in has no presigning and points its uploads at this path, so
    without it the whole non-S3 path is unreachable and the video code can
    only ever be tested against a bucket.

    It reads the body into memory, which is honest for a stand-in and is
    exactly why streaming every rig's video through application workers is
    not the default. At roughly 2.6 TB a day it would not survive.

    Storing is not confirming. This says only that bytes arrived; whether
    they are the right bytes is `video:complete`, which reads them back
    out of the store and checks.
    """
    limit = settings.max_video_bytes

    # Content-Length first, so an oversized upload is refused before a
    # byte of it is read rather than after all of it is in memory.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"larger than {limit} bytes")

    # Then the same ceiling again while streaming, because Content-Length
    # is a claim by the caller and a chunked request makes no claim at all.
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=f"larger than {limit} bytes")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=400, detail="no bytes")
    try:
        stored = await storage.put(key, data)
    except ValueError as e:
        # A key that tries to climb out of the storage root.
        raise HTTPException(status_code=400, detail=str(e))
    return {"key": stored.key, "bytes": stored.bytes}


@router.get("/floor/video", tags=["video"],
            dependencies=[Depends(desk_read_auth)],
            summary="What video is waiting, and what a real episode actually costs")
async def video_backlog(session: AsyncSession = Depends(get_session)) -> dict:
    """What is waiting to reach the archive, and what a video actually
    costs - measured, rather than the estimate the plan was sized on."""
    return await backlog(session)


@router.get("/health", tags=["service"], summary="Liveness, and what is open")
async def health(
    session: AsyncSession = Depends(get_session),
    s: Settings = Depends(get_settings),
) -> dict:
    """Reachability, not a greeting.

    This used to answer `ok: true` without touching anything, which means
    it answered `ok: true` with the database on fire - and a load balancer
    reading it would have kept sending a broken instance traffic. It runs
    a real query now, and says 503 when that fails.

    It also reports every cross-cutting thing that can be off: rig auth,
    desk auth, whether floor reads are protected, and the rate limit. A
    service that is quietly open looks exactly like a correctly
    configured one until the day it does not, so all of it is something a
    deploy check can fail on rather than something to remember.
    """
    body = {
        "ok": True,
        "database": s.safe_url(),
        "rigAuth": "on" if s.auth_is_on else "off",
        # Whether this service tells rigs apart at all. The rig reads it:
        # a floor that identifies its rigs and could not identify *this*
        # machine is a misprovisioned rig, and it stops rather than
        # falling back to the default and filing every take under it.
        #
        # Separate from rigAuth on purpose. The worst version of that
        # failure is a floor running with auth off, where twelve rigs
        # reporting as one is accepted rather than refused - so the rig
        # cannot use rigAuth to decide whether it is on a floor.
        "rigIdentity": "on" if s.rig_addresses else "off",
        "deskAuth": "on" if s.desk_token else "off",
        # Reading the floor and writing to it are separate questions, so
        # a deploy check can tell which of them is actually closed.
        "floorReads": (
            "protected" if (s.protect_floor_reads and s.desk_token) else "open"
        ),
        "rigRateLimit": (
            f"{s.rig_rate_limit_per_min}/min" if s.rig_rate_limit_per_min else "off"
        ),
    }
    try:
        await session.execute(text("SELECT 1"))
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail={**body, "ok": False, "error": type(e).__name__},
        )
    return body


# ------------------------------------------------------ the schedule in


class PushIn(BaseModel):
    """Twelve payloads in one request, as the desk already sends them."""

    payloads: list[dict] = Field(min_length=1, max_length=64)


class PushOut(BaseModel):
    pushId: uuid.UUID
    pushedAt: datetime
    count: int


@router.post("/schedules/push", response_model=PushOut, tags=["schedules"],
             dependencies=[Depends(desk_auth)],
             summary="Push the desk's payloads to the floor, all or none")
async def push(body: PushIn, session: AsyncSession = Depends(get_session)) -> PushOut:
    """Store what the desk pushed, whole and unexamined.

    Validated for the handful of fields this service indexes on and
    nothing more. The payload is a contract between the desk and the rig,
    both of which run the same engine; this server is a courier and a
    filing cabinet, and the moment it starts having opinions about turns
    it becomes a third answer that can disagree.

    All-or-nothing, like the ingest route and for the same reason: a
    floor running half a schedule is worse than a floor running none.

    A push carries a whole day - every rig, every shift - because a
    payload covers one shift, and pushing only the current one is what
    left a rig holding a finished schedule at the boundary with nothing
    newer to pick up.
    """
    push_id = uuid.uuid4()
    pushed_at = datetime.now(timezone.utc)
    rows = []
    # One version of a shift per rig, per push. The database enforces it
    # too, but reaching it means an IntegrityError surfacing as a 500 -
    # a malformed push deserves to be told what is wrong with it.
    seen: set[tuple[str, date, str]] = set()

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
        key = (rig_id, shift_date, shift_label)
        if key in seen:
            raise HTTPException(
                status_code=422,
                detail=(f"payloads[{i}] repeats {rig_id} {shift_label} on "
                        f"{shift_date}; one push may carry one version of a shift"),
            )
        seen.add(key)

        rows.append(
            Schedule(
                push_id=push_id, pushed_at=pushed_at, rig_id=rig_id,
                shift_date=shift_date, shift_label=shift_label, payload=payload,
            )
        )

    session.add_all(rows)
    await session.commit()
    return PushOut(pushId=push_id, pushedAt=pushed_at, count=len(rows))


@router.post("/push", response_model=PushOut, tags=["schedules"],
             dependencies=[Depends(desk_auth)],
             summary="Push, at the path the deployed desk already posts to")
async def push_alias(body: PushIn, session: AsyncSession = Depends(get_session)) -> PushOut:
    """What the desk's "Push to floor" button already posts to.

    The desk is deployed and speaks to the static server today. Rather
    than make it learn which backend it is talking to, this service
    answers to the same path.
    """
    return await push(body, session)


@router.get("/state", tags=["schedules"],
            dependencies=[Depends(desk_read_auth)],
            summary="What the floor is currently running, as the desk asks for it")
async def state(session: AsyncSession = Depends(get_session)) -> dict:
    """What the floor is currently running, as the desk asks for it.

    The desk shows "On the floor" or "showing this screen's plan" based
    on this, which is the difference between a manager reading what the
    rigs actually have and reading what they are typing.
    """
    rows = await session.execute(
        select(Schedule.rig_id, Schedule.pushed_at)
        .order_by(Schedule.pushed_at.desc())
    )
    seen: dict[str, datetime] = {}
    for rig_id, pushed_at in rows.all():
        seen.setdefault(rig_id, pushed_at)
    if not seen:
        return {"pushedAt": None, "rigs": []}
    return {
        "pushedAt": max(seen.values()).isoformat().replace("+00:00", "Z"),
        "rigs": sorted(seen),
    }


# ------------------------------------------------------------- the floor


@router.get("/floor/state", tags=["floor"],
            dependencies=[Depends(desk_read_auth)],
            summary="The desk's live board: who is on, what is open, what is quiet")
async def floor(session: AsyncSession = Depends(get_session)) -> dict:
    """The Live board: every rig, who is on it, when it was last heard
    from, and anything open against it."""
    return await floor_state(session)


@router.get("/floor/alerts", tags=["floor"],
            dependencies=[Depends(desk_read_auth)],
            summary="What is open against the floor")
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


@router.get("/floor/efficiency", tags=["floor"],
            dependencies=[Depends(desk_read_auth)],
            summary="Efficiency per operator for one shift, computed at read time")
async def efficiency_for_shift(
    shift_date: date, shift_label: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """Efficiency per operator, computed here and stored nowhere."""
    return {
        "shiftDate": shift_date.isoformat(),
        "shiftLabel": shift_label,
        "operators": await operator_efficiency(session, shift_date, shift_label),
    }
