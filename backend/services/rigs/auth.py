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

from fastapi import Depends, Header, HTTPException, Path

from core.infrastructure.config import Settings, get_settings

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
        raise HTTPException(status_code=401, detail=REFUSED)

    # compare_digest, not ==: an early return on the first wrong byte
    # leaks how much of the token the caller already has.
    if not hmac.compare_digest(presented, expected):
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
        log.info("desk auth: on")
    else:
        log.warning("desk auth: OFF - anyone who can reach this may push a schedule.")
