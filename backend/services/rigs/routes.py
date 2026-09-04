"""The rig-facing routes. Nine planned; these are Phase 1's four.

Note what is absent: nothing here computes a rotation, and nothing
accepts a percentage. The server stores the schedule it was pushed and
reads it back; it never derives one.
"""

import logging
import uuid
from datetime import date, datetime, timezone

from fastapi import (
    APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response,
)
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.domains.alerts.repository import AlertRepository
from core.domains.rig_events.repository import RigEventRepository
from core.domains.rig_events.schema import EventBatch, IngestResult
from core.domains.rig_status.repository import RigStatusRepository
from core.domains.schedules.model import Schedule, SchedulePush
from core.domains.schedules.repository import (
    ScheduleRepository, SchedulePushRepository,
)
from core.infrastructure.config import Settings, get_settings
from core.infrastructure.database import get_session
from core.workflows.floor import floor_state, operator_efficiency
from core.workflows.schedules import in_force as schedules_in_force
from core.workflows.schedules import turns_for_operator
from core.domains.accounts.repository import (
    AccountRepository, AccountSessionRepository,
)
from services.rigs.auth import (
    desk_auth, desk_read_auth, rig_auth, rig_auth_for_key, rig_rate_limit,
)
from services.rigs.people import (
    REFUSED, RESET_SENT, SESSION_COOKIE, PasswordRefused, deliver_reset,
    change_password, clear_session_cookies, current_account, finish_reset,
    issue_session_cookies, lockout, lockout_key, login_key, login_limiter,
    require_account, require_csrf, require_manager, require_operator,
    reset_limiter, sign_in,
)
from services.rigs import mail
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
                operator_name=ev.operator_name,
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


# HEAD as well as GET, and spelled out because `@router.get` will not do
# it for you. Starlette's plain Route adds HEAD when GET is registered;
# FastAPI's APIRoute does not. So HEAD used to fall past this route - on
# local_gateway to the static mount, which serves the placeholder that
# names nobody, and behind nginx, where `location =` matches every
# method, to a 405. Two deployments, two different wrong answers to the
# one question this route exists to answer.
@router.api_route("/rigs/config.js", methods=["GET", "HEAD"], tags=["schedules"],
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
            dependencies=[Depends(desk_read_auth), Depends(require_manager)],
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
        # No connection string. It used to name the host, the port, the
        # database and the user here, masked only in the password - and
        # this route answers anybody who can reach the port. Masking the
        # password never made the rest useful to a stranger.
        #
        # Nothing consumed it. The load balancer reads `ok` and the
        # status; the rig reads `rigIdentity`; `tools.preflight` reads
        # the settings directly and prints it on the box, which is where
        # it belongs. The switches below stay: a caller can learn each of
        # them by probing anyway - try a push, count a few refusals - and
        # the rig depends on reading one of them.
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
        # Whether the session cookie is HTTPS-only, and how fast a
        # password may be guessed. Both are things a deploy check should
        # be able to fail on rather than things to remember.
        "sessionCookie": "secure" if s.session_cookie_secure else "INSECURE",
        "loginRateLimit": (
            f"{s.login_rate_limit_per_min}/min"
            if s.login_rate_limit_per_min else "off"
        ),
        "loginLockout": (
            f"after {s.login_lockout_after}, up to {s.login_lockout_max_wait_secs}s"
            if s.login_lockout_after else "off"
        ),
    }
    try:
        await session.execute(text("SELECT 1"))
        # Person auth is on when there is somebody to sign in as. It
        # cannot be read from a setting - the accounts are in the
        # database - which is why this is here and not in `announce`.
        body["personAuth"] = (
            "on" if await AccountRepository(session).count() else "off"
        )
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


def _actor(account, request: Request, settings: Settings) -> dict:
    """Who to record for a push, and how they got in.

    Three ways through the door and the history should say which. A
    signed-in manager is named. The shared desk token is not a person and
    must not be recorded as one. A deployment with neither configured is
    "open", written down rather than left looking like a manager whose
    name nobody captured.
    """
    address = caller_address(
        request.client.host if request.client else None,
        request.headers.get("x-real-ip"),
    )
    if account is not None:
        return {"actor_kind": "manager", "account_id": account.id,
                "actor_email": account.email, "actor_name": account.name,
                "address": address}
    kind = "token" if settings.desk_token else "open"
    return {"actor_kind": kind, "account_id": None,
            "actor_email": None, "actor_name": None, "address": address}


def _covered(payloads: list[dict]) -> dict:
    """What this push changed, as counts and lists rather than a verdict.

    Enough to answer "was that the push that moved Tuesday night" without
    reading thirty-six payloads back out.
    """
    rigs, dates, shifts = set(), set(), set()
    for p in payloads:
        rigs.add(str(p.get("rigId")))
        shift = p.get("shift") or {}
        dates.add(str(shift.get("date")))
        shifts.add(str(shift.get("label")))
    return {"payloads": len(payloads), "rigs": len(rigs),
            "dates": sorted(dates), "shifts": sorted(shifts)}


@router.post("/schedules/push", response_model=PushOut, tags=["schedules"],
             dependencies=[Depends(desk_auth), Depends(require_csrf)],
             summary="Push the desk's payloads to the floor, all or none")
async def push(
    body: PushIn, request: Request,
    account=Depends(require_manager),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PushOut:
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

    # In the same transaction as the schedules it describes. A push that
    # rolled back must not leave a row saying the floor changed, and a
    # push that landed must not be missing one.
    session.add(SchedulePush(
        push_id=push_id, pushed_at=pushed_at,
        covered=_covered(body.payloads),
        **_actor(account, request, settings),
    ))
    await session.commit()

    who = account.email if account is not None else "no signed-in person"
    log.info("floor pushed by %s: %d payloads across %d rigs",
             who, len(rows), len({r.rig_id for r in rows}))
    return PushOut(pushId=push_id, pushedAt=pushed_at, count=len(rows))


@router.post("/push", response_model=PushOut, tags=["schedules"],
             dependencies=[Depends(desk_auth), Depends(require_csrf)],
             summary="Push, at the path the deployed desk already posts to")
async def push_alias(
    body: PushIn, request: Request,
    account=Depends(require_manager),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PushOut:
    """What the desk's "Push to floor" button already posts to.

    The desk is deployed and speaks to the static server today. Rather
    than make it learn which backend it is talking to, this service
    answers to the same path.
    """
    return await push(body, request, account, session, settings)


@router.get("/state", tags=["schedules"],
            dependencies=[Depends(desk_read_auth), Depends(require_manager)],
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
            dependencies=[Depends(desk_read_auth), Depends(require_manager)],
            summary="The desk's live board: who is on, what is open, what is quiet")
async def floor(session: AsyncSession = Depends(get_session)) -> dict:
    """The Live board: every rig, who is on it, when it was last heard
    from, and anything open against it."""
    return await floor_state(session)


@router.get("/floor/alerts", tags=["floor"],
            dependencies=[Depends(desk_read_auth), Depends(require_manager)],
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
            dependencies=[Depends(desk_read_auth), Depends(require_manager)],
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


# --------------------------------------------------------------- people
#
# Signing in is about a *person* using the desk. It is deliberately
# unrelated to the rig routes above, which authenticate a machine and
# never a person - a rig has no login and is not getting one.


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class WhoOut(BaseModel):
    """What a signed-in caller is told about themselves.

    No id, and no more of the account than a screen needs to greet
    somebody and decide what to draw. A reply that carries more than that
    is a reply that leaks more than that from a page left open.
    """

    name: str
    role: str
    operatorId: str | None = None
    csrfToken: str | None = None


def _who(account, csrf: str | None = None) -> "WhoOut":
    return WhoOut(name=account.name, role=account.role,
                  operatorId=account.operator_id, csrfToken=csrf)


@router.post("/auth/login", response_model=WhoOut, tags=["people"],
             summary="Sign in a manager or an operator")
async def login(
    body: LoginIn, request: Request, response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WhoOut:
    """Exchange an email and password for a session cookie.

    Every way of failing gives the same 401 with the same wording, and
    takes the same time: the wrong-email path is verified against a dummy
    hash rather than returning early, because a route that answers faster
    for unknown addresses tells anyone who asks which addresses are real.

    Throttled per calling address, and on by default - unlike the rig
    limiter, for the reason written beside `login_rate_limit_per_min`.
    """
    # Two throttles, and they answer different questions. The limiter
    # caps how fast this address may call at all; the lockout caps how
    # many times it may guess at one account. Checked before any hashing
    # is done, so a caller already waiting cannot spend our CPU.
    key = lockout_key(request, body.email)
    wait = lockout(settings).check(key)
    if wait > 0:
        raise HTTPException(
            status_code=429, detail="too many failed sign-in attempts",
            headers={"Retry-After": str(max(1, int(wait + 0.5)))},
        )

    wait = login_limiter(settings).allow(login_key(request))
    if wait > 0:
        raise HTTPException(
            status_code=429, detail="too many sign-in attempts",
            headers={"Retry-After": str(max(1, int(wait + 0.5)))},
        )

    signed = await sign_in(session, body.email, body.password, settings)
    if signed is None:
        owed = lockout(settings).failed(key)
        if owed > 0:
            # Worth a line. A run of failures against a manager is either
            # somebody locked out of their own desk or somebody working
            # through a list, and both want a person to look.
            log.warning("sign-in for %s from %s is now waiting %ds after "
                        "repeated failures", body.email, login_key(request),
                        int(owed))
        raise HTTPException(status_code=401, detail=REFUSED)

    lockout(settings).succeeded(key)
    account, token = signed
    await session.commit()
    csrf = issue_session_cookies(response, token, settings)
    log.info("signed in: %s (%s)", account.email, account.role)
    return _who(account, csrf)


@router.post("/auth/logout", tags=["people"],
             summary="End this session. Safe to call when not signed in")
async def logout(
    request: Request, response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """End the session this cookie names, and clear it.

    Always 200, including with no cookie or a stale one. A sign-out that
    can fail is one somebody gives up on, and there is nothing here to
    protect: the only thing it does is end a session, and being asked to
    end one that is already over is not an error.

    No CSRF requirement, deliberately. Forcing a sign-out on somebody is
    a nuisance, not a compromise, and refusing a logout because a token
    did not travel leaves a live session open - which is the worse of
    the two.
    """
    token = request.cookies.get(SESSION_COOKIE)
    ended = 0
    if token:
        ended = await AccountSessionRepository(session).revoke(token)
        await session.commit()
    clear_session_cookies(response, settings)
    return {"signedOut": bool(ended)}


class ResetRequestIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)


class ResetFinishIn(BaseModel):
    token: str = Field(min_length=8, max_length=512)
    newPassword: str = Field(min_length=1, max_length=1024)


def _reset_is_on(settings: Settings) -> None:
    """Refuse both reset routes when there is no way to deliver a token.

    404, not 503 and not a cheerful 200. A deployment with no relay
    cannot complete this flow, and a route that accepted the request
    anyway would leave somebody waiting at a screen for mail that was
    never going to be sent - which is worse than being told plainly that
    the floor does not do this.

    Off until configured, and off meaning *refused* rather than open, for
    the reason CLAUDE.md gives twice: the failures in this area have both
    been something unverifiable read as permission.
    """
    if not mail.is_configured(settings):
        raise HTTPException(
            status_code=404,
            detail="this floor has no mail relay configured, so it cannot send "
                   "a reset link. Ask a manager to set your password.",
        )


@router.post("/auth/reset/request", tags=["people"],
             summary="Ask for a link to set a new password")
async def request_password_reset(
    body: ResetRequestIn, request: Request, background: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Send a reset link to that address, if it belongs to an account.

    **The reply is the same either way, and so is the time it takes.**
    That is the whole design of this route. It is reached by anybody who
    can load the sign-in page and it takes an email address, so a version
    that said "no such account" would be a faster way to enumerate the
    floor's staff than the login route the dummy hash exists to protect.

    The clock is the half that is easy to write down and then not check,
    and this route failed it. Looking the address up, minting a token,
    writing two rows and holding the request open for an SMTP
    conversation cost 977ms, against 16ms for an address with no account
    - measured, with every real request slower than every invented one.
    Identical wording and a sixty-fold difference in latency is not a
    protected route, it is an oracle with a polite error message.

    So nothing happens here. The work is handed to `deliver_reset` and
    done once the reply has gone, which makes both paths cost the same
    because both now do the same thing: schedule and return. There is
    deliberately no database session on this route at all - acquiring one
    is work, and work is what leaks.

    What that costs is a person who mistypes their address waiting for
    something that will never arrive, which is the accepted price and is
    why the message says *if*.

    Throttled per calling address and per hour rather than per minute.
    This route sends mail to somebody else's inbox, so an unbounded one
    is a way to have the floor deliver a hundred messages to a person who
    asked for none. The throttle is checked before scheduling, so a
    refusal costs no more than an acceptance.
    """
    _reset_is_on(settings)

    wait = reset_limiter(settings).allow(login_key(request))
    if wait > 0:
        raise HTTPException(
            status_code=429, detail="too many reset requests",
            headers={"Retry-After": str(max(1, int(wait + 0.5)))},
        )

    background.add_task(deliver_reset, body.email, settings, login_key(request))
    return {"ok": True, "detail": RESET_SENT}


@router.post("/auth/reset", response_model=WhoOut, tags=["people"],
             summary="Set a new password using a reset link")
async def finish_password_reset(
    body: ResetFinishIn, response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WhoOut:
    """Spend the token and set the password.

    No cookie needed and no CSRF token: whoever holds this has proved
    they can read the account's mailbox, which is the only thing a reset
    can ever prove. The token is the credential, it works once, and using
    it ends every session the account had - including whoever was signed
    in with the password that was just replaced, which is the point if
    the reason for the reset was that somebody else knew it.

    They are signed in on the way out. Making somebody who has just
    proved the mailbox and set a password then type that password into a
    login box is a step with nothing behind it.
    """
    _reset_is_on(settings)

    try:
        account, token = await finish_reset(
            session, body.token, body.newPassword, settings)
    except PasswordRefused as refused:
        raise HTTPException(status_code=400, detail=str(refused))

    await session.commit()
    csrf = issue_session_cookies(response, token, settings)
    return _who(account, csrf)


class ChangePasswordIn(BaseModel):
    currentPassword: str = Field(min_length=1, max_length=1024)
    newPassword: str = Field(min_length=1, max_length=1024)


@router.post("/auth/password", response_model=WhoOut, tags=["people"],
             summary="Change your own password")
async def change_own_password(
    body: ChangePasswordIn, request: Request, response: Response,
    account=Depends(require_account),
    _csrf: None = Depends(require_csrf),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WhoOut:
    """Set a new password for the account this cookie names.

    Either role. An operator has an account and a password like anybody
    else, and a screen that could only be fixed by asking a manager is
    the thing this route exists to remove.

    The current password is required despite the session - see
    `change_password` for why a cookie is not enough on a floor where
    screens are left open.

    Throttled with the same lockout as login, keyed the same way. This is
    a guessing oracle against a known account: without it, an open tab
    would let somebody work through a list of likely passwords at the
    speed of the network, and the one route that already knows who you
    are is a poor place to leave that open. A success clears the count,
    so somebody who mistypes their old password twice and then gets it
    right is not left waiting.
    """
    key = lockout_key(request, account.email)
    wait = lockout(settings).check(key)
    if wait > 0:
        raise HTTPException(
            status_code=429, detail="too many attempts",
            headers={"Retry-After": str(max(1, int(wait + 0.5)))},
        )

    try:
        token = await change_password(
            session, account, body.currentPassword, body.newPassword, settings)
    except PasswordRefused as refused:
        # Only a wrong *current* password counts towards the lockout. A
        # rejected new one is the person getting the rule wrong, not
        # somebody guessing, and locking them out for it would punish the
        # one thing this route is for.
        if "current password" in str(refused):
            lockout(settings).failed(key)
        raise HTTPException(status_code=400, detail=str(refused))

    lockout(settings).succeeded(key)
    await session.commit()
    # A new cookie, because every session including this one was just
    # revoked. Without this the person is signed out by their own
    # password change, which is how a flow stops being used.
    csrf = issue_session_cookies(response, token, settings)
    return _who(account, csrf)


@router.get("/auth/me", response_model=WhoOut, tags=["people"],
            summary="Who this browser is signed in as")
async def me(account=Depends(require_account)) -> WhoOut:
    """The identity behind the cookie, or 401.

    This is what the desk asks before it renders anything: the screen a
    manager sees and the screen an operator sees are different screens,
    and this is the only thing that decides which.
    """
    return _who(account)


# ------------------------------------------------------- an operator's own


@router.get("/me/shift", tags=["people"],
            summary="My turns in the shift running now, across every rig")
async def my_shift(
    account=Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One operator's own day, and nobody else's.

    Scoped here rather than in the screen that draws it. A page can hide
    a row; only the route can decline to send it, and the difference
    between those two is the whole of the role split.

    A rig cannot answer this - it knows only its own turns, and this
    person's day walks across all three rigs in their group. That is the
    same fact that put `theyGoTo` in the payload.
    """
    return await turns_for_operator(session, account.operator_id)


@router.get("/me/efficiency", tags=["people"],
            summary="My own efficiency for one shift")
async def my_efficiency(
    shift_date: date, shift_label: str,
    account=Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The same numbers `/floor/efficiency` computes, filtered to one
    person - and filtered *before* they are sent.

    The floor-wide route stays manager-only. Whether an operator should
    see how they compare to the person next to them is a question about
    how this floor is run, and CLAUDE.md already records it as open;
    answering it by accident, in an API that returns everyone, is the one
    way it must not be settled.
    """
    everyone = await operator_efficiency(session, shift_date, shift_label)
    mine = [row for row in everyone if row["operatorId"] == account.operator_id]
    return {
        "shiftDate": shift_date.isoformat(),
        "shiftLabel": shift_label,
        # A list of nought or one, not a bare object: an operator who has
        # not worked that shift has no row, and inventing a zeroed one
        # would read as "you recorded nothing" rather than "you were not
        # here".
        "operators": mine,
    }


@router.get("/auth/session", tags=["people"],
            summary="What a screen needs to know before it draws anything")
async def auth_session(
    request: Request,
    account=Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Whether anybody can sign in here, and whether anybody has.

    `/auth/me` cannot answer this. It says 401 both for "you are not
    signed in" and for "this deployment has no accounts at all", and a
    screen that cannot tell those apart either shows a login box nobody
    has a password for, or opens the desk to anyone the moment a
    deployment has not been configured yet.

    Unauthenticated and always 200, because it is the question asked
    *before* there is a credential to present. It gives away only whether
    the door is locked, which is a thing anyone standing at a door can
    already see.

    The CSRF token comes back here so a page that has just been reloaded
    can make a write without re-reading its own cookies. It is the value
    of the cookie the browser already holds, so this hands out nothing
    the caller did not arrive with.
    """
    from services.rigs.people import CSRF_COOKIE

    on = await AccountRepository(session).count() > 0
    return {
        "personAuth": "on" if on else "off",
        "account": (
            {"name": account.name, "role": account.role,
             "operatorId": account.operator_id}
            if account else None
        ),
        "csrfToken": request.cookies.get(CSRF_COOKIE) if account else None,
    }


class PushedBy(BaseModel):
    pushId: uuid.UUID
    pushedAt: datetime
    by: str | None = None
    email: str | None = None
    how: str
    address: str | None = None
    covered: dict


@router.get("/schedules/pushes", tags=["schedules"],
            dependencies=[Depends(desk_read_auth), Depends(require_manager)],
            summary="Who changed the floor's day, newest first")
async def pushes(
    limit: int = 50, session: AsyncSession = Depends(get_session)
) -> dict:
    """The history the ledger does not keep.

    Every event a rig files is attributable; nothing a manager does was,
    until this. A push rewrites what twelve machines run for a whole day,
    and "who put the floor on this schedule" is a question somebody asks
    only after something has gone wrong - which is exactly when it must
    already have been recorded.

    Manager-only, because it names people and where they were.
    """
    rows = await SchedulePushRepository(session).recent(
        limit=max(1, min(limit, 500))
    )
    return {
        "pushes": [
            PushedBy(
                pushId=r.push_id, pushedAt=r.pushed_at,
                by=r.actor_name, email=r.actor_email,
                how=r.actor_kind, address=r.address, covered=r.covered,
            ).model_dump(mode="json")
            for r in rows
        ]
    }
