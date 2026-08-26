"""Who is allowed to file events for a rig.

The plan settles this one: *a per-rig token placed by Ansible, checked on
ingest. The rig still has no operator login; the machine authenticates,
the person never does.* That is the whole idea, and it is why the rig can
keep being a screen with three pedals and nothing to sign into.

Two things follow from it that are easy to get wrong:

**The token names a rig, not a session.** RIG-03's token may only speak
for RIG-03. A rig with a valid token for a different rig is refused, not
waved through - otherwise one compromised machine can file events, and
therefore attribute work, for the whole floor.

**Comparison is constant-time.** A token check that returns early on the
first wrong byte tells an attacker how much of the token they have. It is
one function call to not do that.

With no tokens configured every rig is trusted. That is correct for a
laptop demo and wrong for a floor, so it is not silent: the service logs
what is open at startup and `/api/health` reports it, which is a thing a
deploy check can fail on.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Depends, Header, HTTPException, Path, Response

from core.infrastructure.config import Settings, get_settings
from services.rigs.ratelimit import RateLimiter

log = logging.getLogger("rigs.auth")

# A rig that presents no credentials at all and one that presents the
# wrong ones get the same answer, for the same reason as the constant-time
# compare: the reply should not say which half of the problem it is.
REFUSED = "this rig is not authorised"


def _presented(authorization: str | None) -> str | None:
    """The bearer token, or None. Tolerant of case, strict about shape."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _check(rig_id: str, authorization: str | None, settings: Settings) -> None:
    if not settings.rig_tokens:
        return                      # open by configuration, and said out loud

    expected = settings.rig_tokens.get(rig_id)
    presented = _presented(authorization)
    if expected is None or presented is None:
        # Which rig and which half, in the log only. The reply still says
        # neither - the log is for whoever is fixing it, the reply is for
        # whoever might be probing it.
        log.warning("refused %s: %s", rig_id,
                    "no rig by that name" if expected is None else "no bearer token")
        raise HTTPException(status_code=401, detail=REFUSED)

    # compare_digest, not ==: an early return on the first wrong byte
    # leaks how much of the token the caller already has.
    if not hmac.compare_digest(presented, expected):
        # Never the token, presented or expected. A log line is
        # world-readable, because eventually it is.
        log.warning("refused %s: token did not match", rig_id)
        raise HTTPException(status_code=401, detail=REFUSED)


async def rig_auth(
    rig_id: str = Path(...),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """For every route that carries {rig_id} in its path."""
    _check(rig_id, authorization, settings)


async def rig_auth_for_key(
    key: str = Path(...),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """For the storage PUT, whose path is a key rather than a rig.

    Object keys begin with the rig that owns them - `RIG-03/<episode>/
    front.mp4` - so a rig may write under its own prefix and nowhere else.
    That is the same rule as everywhere above, read from the only place
    this route is told who is calling.
    """
    rig_id = key.split("/", 1)[0] if "/" in key else ""
    if not rig_id:
        raise HTTPException(status_code=400, detail="key does not name a rig")
    _check(rig_id, authorization, settings)


async def desk_auth(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """The desk's write path.

    Deliberately separate from the rig's, and deliberately optional: the
    plan does not decide desk auth, because the desk is expected to sit
    behind the platform team's gateway, which already handles CORS, rate
    limiting and who is allowed in. This exists so a deployment without
    that in front of it is not obliged to leave the floor's schedule
    writable by anyone who can reach the port.
    """
    if not settings.desk_token:
        return
    presented = _presented(authorization)
    if presented is None or not hmac.compare_digest(presented, settings.desk_token):
        raise HTTPException(status_code=401, detail="the desk is not authorised")


# One limiter for the process. Built lazily so a test can hand the app
# different settings and get a limiter sized to them.
_limiter: RateLimiter | None = None


def limiter(settings: Settings) -> RateLimiter:
    global _limiter
    if _limiter is None or _limiter.per_minute != settings.rig_rate_limit_per_min:
        _limiter = RateLimiter(per_minute=settings.rig_rate_limit_per_min)
    return _limiter


def reset_limiter() -> None:
    """Tests call this. Nothing in the request path does."""
    global _limiter
    _limiter = None


async def rig_rate_limit(
    response: Response,
    rig_id: str = Path(...),
    settings: Settings = Depends(get_settings),
) -> None:
    """A ceiling per rig, applied after auth so the key is a rig somebody
    owns rather than one a stranger named.

    Refusing is safe: the rig keeps its events, backs off, and ingest
    dedupes the retry, so nothing is lost by being told to wait. That is
    the property that makes a limiter appropriate here at all.
    """
    wait = limiter(settings).allow(rig_id)
    if wait <= 0:
        return
    # A 429 with no idea when to come back invites the tight retry loop
    # the limit exists to stop.
    raise HTTPException(
        status_code=429,
        detail=f"{rig_id} is over its rate limit",
        headers={"Retry-After": str(max(1, int(wait + 0.5)))},
    )


async def desk_read_auth(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reading the floor.

    A separate switch from writing to it, because they are separate
    questions. `/floor/state` is what a wall display shows and what the
    desk polls; requiring a secret for it is a product decision, not a
    security default. Pushing a schedule changes what twelve rigs do, and
    that is gated by `desk_token` on its own.

    With no desk token set there is nothing to check against, so this
    stays open rather than locking everyone out of a board with a secret
    nobody was given.
    """
    if not settings.protect_floor_reads or not settings.desk_token:
        return
    presented = _presented(authorization)
    if presented is None or not hmac.compare_digest(presented, settings.desk_token):
        log.warning("refused a floor read: no valid desk token")
        raise HTTPException(status_code=401, detail="the desk is not authorised")


def announce(settings: Settings) -> None:
    """Say what is open, at startup, where somebody will see it.

    A service that is quietly unauthenticated looks exactly like one that
    is correctly configured until the day it does not.
    """
    if settings.rig_tokens:
        log.info("rig auth: on (%d rigs configured)", len(settings.rig_tokens))
    else:
        log.warning(
            "rig auth: OFF - any caller may file events for any rig. "
            "Set RIG_TOKENS before this reaches a floor."
        )
    if settings.desk_token:
        log.info("desk auth: on (writes%s)",
                 ", reads" if settings.protect_floor_reads else "")
    else:
        log.warning("desk auth: OFF - anyone who can reach this may push a schedule.")

    if settings.rig_rate_limit_per_min > 0:
        log.info("rig rate limit: %d requests/min per rig",
                 settings.rig_rate_limit_per_min)
    else:
        log.warning("rig rate limit: OFF - one rig in a retry loop is unbounded.")
