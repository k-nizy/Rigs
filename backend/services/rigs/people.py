"""Who is *using* the desk, as opposed to which machine is calling.

`auth.py` next door answers a different question and answers it well: a
rig presents a bearer token that names one rig, and the operator standing
at it authenticates nothing. That is settled and nothing here changes it.
The two live in separate files because they are separate trust models,
and a single module called "auth" holding both is how they end up sharing
a code path that was only ever correct for one of them.

What this adds is a person. Three properties are worth knowing before
reading it, because none of them is obvious from a route list.

**Login costs the same whether the account exists or not.** Returning
early on an unknown address answers in two milliseconds instead of four
hundred, and that difference is a working account-enumeration oracle -
anyone can learn which addresses are real by timing. So an unknown email
is verified against a dummy hash and thrown away. Same instinct as the
constant-time compare in `auth.py`: the reply should not say which half
of the problem it is, and neither should the clock.

**The cookie is httpOnly; the CSRF token deliberately is not.** They are
a pair. The session cookie must be unreadable by script, or one XSS is a
permanent credential. The CSRF token must be readable by script, because
the page has to echo it back in a header - and that is exactly what makes
it work: another origin can neither read this origin's cookies nor set a
custom header without being allowed to. `SameSite=Lax` is the first lock
and this is the second, for the reason `nginx.conf` already gives about
cache headers - both, because either alone is a single point of failure
for a silent error.

**A session is a row, so it can be ended.** A signed token cannot be
withdrawn before it expires. Being able to end somebody's access on the
day they leave is the whole argument for having people here at all
instead of one shared secret.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.domains.accounts.model import MANAGER, OPERATOR, Account
from core.domains.accounts.passwords import (
    hash_password, password_complaint, verify_password,
)
from core.domains.accounts.repository import (
    AccountRepository, AccountSessionRepository, normalise_email,
)
from core.infrastructure.config import Settings, get_settings
from core.infrastructure.database import get_session
from services.rigs.identity import caller_address
from services.rigs.lockout import Lockout
from services.rigs.ratelimit import RateLimiter

log = logging.getLogger("rigs.people")

SESSION_COOKIE = "rigs_session"
CSRF_COOKIE = "rigs_csrf"
CSRF_HEADER = "x-csrf-token"

# One message for every way of failing to sign in. A reply that
# distinguishes "no such account" from "wrong password" hands over half
# the credential for free.
REFUSED = "email or password is not right"

# Verified against when no account matches, so the wrong-email path costs
# the same as the wrong-password one. Built once at import: it is a real
# scrypt hash of a value nobody has, and deriving it per request would
# double the cost of every failed login for no benefit.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


# --------------------------------------------------------------- cookies


def issue_session_cookies(response: Response, token: str, settings: Settings) -> str:
    """Set both cookies for a browser that has just signed in.

    Returns the CSRF token so the login reply can also carry it in the
    body - a client that reads it there does not have to parse cookies at
    all, and one that prefers the cookie still can.
    """
    csrf = secrets.token_urlsafe(32)
    max_age = settings.session_lifetime_hours * 3600

    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=max_age, httponly=True, secure=settings.session_cookie_secure,
        samesite="lax", path="/",
    )
    # Not httponly, and that is the point - see the module docstring.
    response.set_cookie(
        CSRF_COOKIE, csrf,
        max_age=max_age, httponly=False, secure=settings.session_cookie_secure,
        samesite="lax", path="/",
    )
    return csrf


def clear_session_cookies(response: Response, settings: Settings) -> None:
    """Both of them, with the same attributes they were set with.

    A delete_cookie whose path or samesite differs from the set_cookie
    leaves the original in place in some browsers, and the symptom is
    somebody who cannot sign out.
    """
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(
            name, path="/", httponly=(name == SESSION_COOKIE),
            secure=settings.session_cookie_secure, samesite="lax",
        )


# ------------------------------------------------------------- signing in


# One limiter for the process, built lazily so a test can hand the app
# different settings and get a limiter sized to them. Same shape as the
# rig limiter next door, and a separate instance on purpose: a floor
# emptying its outbox must not spend a manager's login attempts.
_login_limiter: RateLimiter | None = None


def login_limiter(settings: Settings) -> RateLimiter:
    global _login_limiter
    if (_login_limiter is None
            or _login_limiter.per_minute != settings.login_rate_limit_per_min):
        _login_limiter = RateLimiter(per_minute=settings.login_rate_limit_per_min)
    return _login_limiter


def reset_login_limiter() -> None:
    """Tests call this. Nothing in the request path does."""
    global _login_limiter, _lockout
    _login_limiter = None
    _lockout = None


# The other half of the same job. The limiter caps how fast one address
# may guess; this caps how many times it may guess at one account. See
# lockout.py for why it counts per pair rather than per account.
_lockout: Lockout | None = None


def lockout(settings: Settings) -> Lockout:
    global _lockout
    if (_lockout is None
            or _lockout.after != settings.login_lockout_after
            or _lockout.max_wait != settings.login_lockout_max_wait_secs):
        _lockout = Lockout(after=settings.login_lockout_after,
                           max_wait=float(settings.login_lockout_max_wait_secs))
    return _lockout


def lockout_key(request: Request, email: str) -> tuple:
    """What a run of failures is counted against.

    The email as typed, normalised - not the account, and not only when
    the account exists. An unknown address that never locked while a real
    one did would say which addresses are real, which is the enumeration
    leak the dummy hash exists to close.
    """
    return (normalise_email(email), login_key(request))


def login_key(request: Request) -> str:
    """What the login throttle counts against.

    The calling address, resolved by the same rule the rig identity uses:
    a forwarded header is believed only from loopback, because nginx is
    the one hop a client cannot forge. Writing a second answer to "who is
    calling" is how the two drift apart.
    """
    return caller_address(
        request.client.host if request.client else None,
        request.headers.get("x-real-ip"),
    ) or "unknown"


async def sign_in(
    session: AsyncSession, email: str, password: str, settings: Settings
) -> tuple[Account, str] | None:
    """The account and a fresh session token, or None.

    None covers every failure - no such address, wrong password, account
    disabled - because the caller must not be able to tell them apart and
    the easiest way to guarantee that is to not know either.
    """
    accounts = AccountRepository(session)
    account = await accounts.by_email(email)

    # Always do the work. See the module docstring: the branch that skips
    # it is the branch that leaks which addresses are real.
    stored = account.password_hash if account else _DUMMY_HASH
    ok = verify_password(password, stored)

    if account is None or not ok or account.disabled_at is not None:
        if account is not None and ok and account.disabled_at is not None:
            # Worth a line: somebody who has left is still trying, which
            # is either a forgotten bookmark or something to look at.
            log.warning("refused a disabled account: %s", account.email)
        return None

    token = secrets.token_urlsafe(32)
    await AccountSessionRepository(session).open(
        account.id, token, timedelta(hours=settings.session_lifetime_hours)
    )
    account.last_login_at = datetime.now(timezone.utc)
    return account, token


# --------------------------------------------- changing your own password


class PasswordRefused(Exception):
    """Why the change was not made, in words meant for the person.

    Distinct from the login refusal above, and deliberately so: there is
    no account to enumerate here. The caller is already signed in and is
    asking about their own password, so telling them *which* half was
    wrong costs nothing and saves them guessing at both.
    """


async def change_password(
    session: AsyncSession, account: Account,
    current: str, new: str, settings: Settings,
) -> str:
    """Set a new password and return a fresh session token for this browser.

    Three things happen together, and the order matters.

    **The current password is checked even though they are signed in.**
    A cookie proves this browser signed in once, not that the person at
    the keyboard is the account holder. An unattended desk on a floor is
    the case that makes this worth the friction: without it, anybody
    passing an open tab could take the account and lock a manager out of
    their own floor.

    **Every other session is revoked.** The reason to change a password
    is usually that it might be known, and a change that leaves the old
    sessions alive does nothing to whoever already has one.

    **This browser gets a new token rather than being signed out.** They
    have just proved the current password, so ending their own session
    would be friction with nothing bought - and a change-password flow
    that logs you out is one people avoid using.
    """
    if not verify_password(current, account.password_hash):
        raise PasswordRefused("that is not your current password")

    complaint = password_complaint(new)
    if complaint:
        raise PasswordRefused(complaint)

    if verify_password(new, account.password_hash):
        # Not dangerous, just pointless - and silently accepting it would
        # have somebody believe they had changed something.
        raise PasswordRefused("that is already your password")

    account.password_hash = hash_password(new)

    sessions = AccountSessionRepository(session)
    await sessions.revoke_all(account.id)

    token = secrets.token_urlsafe(32)
    await sessions.open(
        account.id, token, timedelta(hours=settings.session_lifetime_hours)
    )
    # No password in the line, present or past. Which account changed and
    # when is the part worth having.
    log.info("password changed: %s (%s)", account.email, account.role)
    return token


# ------------------------------------------------- who is calling, later


async def current_account(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Account | None:
    """The signed-in person, or None.

    Answers None rather than 401 so a route can be open to everyone and
    still know who is reading it - which is what `/auth/session` and the
    desk's own boot check need.

    None means "nobody is signed in" and nothing else. A database that
    cannot be reached raises from `live()` and surfaces as a 500, and
    that distinction is deliberate: a service which quietly reported
    "not signed in" whenever its database blinked would sign people out
    during an outage and give whoever was debugging it entirely the
    wrong thing to look at.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    found = await AccountSessionRepository(session).live(token)
    return found[1] if found else None


async def require_account(
    account: Account | None = Depends(current_account),
) -> Account:
    """401 when nobody is signed in."""
    if account is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return account


async def require_csrf(request: Request) -> None:
    """Every state-changing request from a browser session.

    Double submit: the header must equal the cookie. An attacker on
    another origin can do neither half - they cannot read this origin's
    cookies, and they cannot set a custom header without CORS permitting
    it.

    Only enforced when a session cookie is actually present. A caller
    authenticating some other way - a rig with a bearer token, a script
    with the desk token - is not subject to CSRF, because CSRF is
    specifically the browser attaching a credential the user did not
    choose to send.
    """
    if not request.cookies.get(SESSION_COOKIE):
        return
    sent = request.headers.get(CSRF_HEADER, "")
    held = request.cookies.get(CSRF_COOKIE, "")
    if not sent or not held or not hmac.compare_digest(sent, held):
        raise HTTPException(status_code=403, detail="missing or wrong CSRF token")


def announce(settings: Settings) -> None:
    """What is open, said where somebody will see it, at startup.

    Whether any accounts exist cannot be answered from here - that needs
    the database - so `/api/health` reports it and this reports the two
    settings that protect the cookie.
    """
    if settings.session_cookie_secure:
        log.info("person auth: session cookie is Secure")
    else:
        log.warning(
            "person auth: session cookie is NOT Secure - it will travel over "
            "plain http. Right for local development, wrong for a floor."
        )
    if settings.login_rate_limit_per_min > 0:
        log.info("login rate limit: %d attempts/min per address",
                 settings.login_rate_limit_per_min)
    else:
        log.warning("login rate limit: OFF - passwords may be guessed at "
                    "whatever rate the network allows.")
    if settings.login_lockout_after > 0:
        log.info("login lockout: after %d failures, waits double to %ds",
                 settings.login_lockout_after,
                 settings.login_lockout_max_wait_secs)
    else:
        log.warning("login lockout: OFF - a weak password can be found by "
                    "working through a list at the rate limit.")


# ------------------------------------------------------------- the gate


async def _person_auth_is_on(session: AsyncSession) -> bool:
    """Whether this deployment has anybody to sign in as.

    Read from the database rather than a setting, because that is where
    accounts are, and only on the path where nobody is signed in - a
    request that already resolved to a person never runs this query.
    """
    return await AccountRepository(session).count() > 0


async def require_manager(
    account: Account | None = Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> Account | None:
    """A manager - or nobody at all, on a deployment with no accounts.

    The fallback is the same off-until-configured rule rig auth and the
    rate limiter already follow, and it is what keeps `npm run serve` and
    the laptop demo working with no setup. The moment one account exists
    the fallback is gone and this is a real gate.

    It is asymmetric with `require_operator` below on purpose. This one
    guards routes that already existed and must keep working; that one
    guards routes that are meaningless without a person.
    """
    if account is not None:
        if account.role != MANAGER:
            # 403, not 401: signing in again will not help, and telling
            # somebody to re-authenticate when the answer is "not you" is
            # how people end up trying three passwords.
            raise HTTPException(
                status_code=403,
                detail="the desk is for managers; your shift is on My Shift",
            )
        return account

    if await _person_auth_is_on(session):
        raise HTTPException(status_code=401, detail="not signed in")
    return None


async def require_operator(
    account: Account | None = Depends(current_account),
) -> Account:
    """An operator, always, with no off-until-configured fallback.

    These routes answer "what is *my* day" and "how did *I* do". Without
    somebody signed in there is no my, so there is nothing to fall back
    to - unlike the manager gate, where falling back means behaving as
    the service did before accounts existed.
    """
    if account is None:
        raise HTTPException(status_code=401, detail="not signed in")
    if account.role != OPERATOR:
        raise HTTPException(
            status_code=403,
            detail="this shows one operator's own shift, and you are not one",
        )
    return account
