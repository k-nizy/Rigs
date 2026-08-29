"""Signing in: the three routes, the cookie, and the two throttles.

Phase 2. What is worth testing here is not that a correct password works
- it is the handful of ways a login goes quietly wrong:

  * telling a prober which email addresses are real, by answering
    differently or faster for one that is not
  * a cookie a script can read, or one that travels over plain http
  * a sign-out that leaves the session alive on the server
  * the two trust models bleeding into each other - a browser cookie
    authenticating a rig route, or a rig suddenly needing one

The last is the one this whole piece of work must not break, so it is
asserted here as well as by test_auth.py passing unmodified.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient

from core.domains.accounts.model import Account, AccountSession
from core.domains.accounts.passwords import hash_password, token_fingerprint
from core.domains.accounts.repository import AccountSessionRepository
from core.infrastructure.config import Settings, get_settings
from services.rigs.people import (
    CSRF_COOKIE, SESSION_COOKIE, require_csrf, reset_login_limiter,
)

PASSWORD = "not-a-real-password-12"
MANAGER = "r.osei@verlet.co"
OPERATOR = "m.chen@verlet.co"


@pytest.fixture(autouse=True)
def _fresh_limiter():
    """The limiter is a process global. One test's attempts must not be
    counted against the next."""
    reset_login_limiter()
    yield
    reset_login_limiter()


@asynccontextmanager
async def serving(**overrides):
    """The service, with settings of our choosing.

    `session_cookie_secure` defaults to False here and only here. These
    clients talk to `http://test`, and httpx correctly refuses to *send*
    a Secure cookie over plain http - so leaving the production default
    on would make every follow-up request in this file arrive with no
    cookie and 401. That is the flag working. The tests that check the
    flag itself set it explicitly.
    """
    from services.rigs.app import create_app

    base = get_settings()
    settings = Settings(**{**base.model_dump(),
                           "session_cookie_secure": False, **overrides})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def people(session, manager=True, operator=False, disabled=False):
    """Put the accounts a test needs into the database."""
    rows = []
    if manager:
        rows.append(Account(
            email=MANAGER, name="Ruth Osei", role="manager",
            password_hash=hash_password(PASSWORD),
            disabled_at=datetime.now(timezone.utc) if disabled else None))
    if operator:
        rows.append(Account(
            email=OPERATOR, name="Mei Chen", role="operator",
            operator_id="op-a2", password_hash=hash_password(PASSWORD)))
    session.add_all(rows)
    await session.commit()
    return rows


def cookie_attrs(response, name):
    """The Set-Cookie attributes for one cookie, lowercased."""
    for raw in response.headers.get_list("set-cookie"):
        if raw.startswith(name + "="):
            return raw.lower()
    return ""


# ------------------------------------------------------------ signing in


class TestSigningIn:
    async def test_the_right_password_signs_a_manager_in(self, engine, session):
        await people(session)
        async with serving() as c:
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Ruth Osei"
        assert body["role"] == "manager"
        assert body["operatorId"] is None
        assert body["csrfToken"]
        assert r.cookies.get(SESSION_COOKIE)

    async def test_an_operator_is_told_which_operator_they_are(self, engine, session):
        """`/api/me/shift` in phase 3 has nothing to look itself up by
        without this."""
        await people(session, manager=False, operator=True)
        async with serving() as c:
            r = await c.post("/api/auth/login",
                             json={"email": OPERATOR, "password": PASSWORD})
        assert r.status_code == 200
        assert r.json()["role"] == "operator"
        assert r.json()["operatorId"] == "op-a2"

    async def test_the_email_is_matched_regardless_of_capitals(self, engine, session):
        await people(session)
        async with serving() as c:
            r = await c.post("/api/auth/login",
                             json={"email": "R.Osei@VERLET.co", "password": PASSWORD})
        assert r.status_code == 200

    async def test_a_wrong_password_is_refused_with_no_cookie(self, engine, session):
        await people(session)
        async with serving() as c:
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": "not-it"})
        assert r.status_code == 401
        assert not r.cookies.get(SESSION_COOKIE)

    async def test_an_unknown_email_is_refused_exactly_as_a_wrong_password_is(
            self, engine, session):
        """The account-enumeration guard. Two different failures that
        answer differently are one failure that answers the question
        'does this person work here'."""
        await people(session)
        async with serving() as c:
            wrong_pw = await c.post("/api/auth/login",
                                    json={"email": MANAGER, "password": "not-it"})
            no_such = await c.post("/api/auth/login",
                                   json={"email": "nobody@verlet.co",
                                         "password": PASSWORD})
        assert wrong_pw.status_code == no_such.status_code == 401
        assert wrong_pw.json()["detail"] == no_such.json()["detail"]
        assert "password" in no_such.json()["detail"]

    async def test_an_unknown_email_still_does_the_hashing_work(
            self, engine, session):
        """The timing half of the same guard, asserted structurally
        rather than with a stopwatch: a returning-early implementation
        cannot survive this, because verify_password is what makes it
        slow and this proves it was called."""
        import services.rigs.people as people_mod

        calls = []
        real = people_mod.verify_password

        def counting(password, stored):
            calls.append(stored)
            return real(password, stored)

        people_mod.verify_password = counting
        try:
            await people(session)
            async with serving() as c:
                await c.post("/api/auth/login",
                             json={"email": "nobody@verlet.co", "password": "x"})
        finally:
            people_mod.verify_password = real

        assert len(calls) == 1, "the unknown-email path skipped the hash"
        assert calls[0] == people_mod._DUMMY_HASH

    async def test_a_disabled_account_cannot_sign_in(self, engine, session):
        await people(session, disabled=True)
        async with serving() as c:
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
        assert r.status_code == 401
        assert not r.cookies.get(SESSION_COOKIE)

    async def test_the_raw_token_is_never_written_down(self, engine, session):
        """A dump of account_sessions must not be a set of live logins."""
        from sqlalchemy import select

        await people(session)
        async with serving() as c:
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
        token = r.cookies.get(SESSION_COOKIE)

        rows = (await session.execute(select(AccountSession))).scalars().all()
        assert len(rows) == 1
        assert rows[0].token_fingerprint != token
        assert rows[0].token_fingerprint == token_fingerprint(token)

    async def test_two_sign_ins_are_two_sessions(self, engine, session):
        await people(session)
        async with serving() as c1, serving() as c2:
            a = await c1.post("/api/auth/login",
                              json={"email": MANAGER, "password": PASSWORD})
            b = await c2.post("/api/auth/login",
                              json={"email": MANAGER, "password": PASSWORD})
        assert a.cookies.get(SESSION_COOKIE) != b.cookies.get(SESSION_COOKIE)


# --------------------------------------------------------------- cookies


class TestTheCookie:
    async def test_the_session_cookie_cannot_be_read_by_script(
            self, engine, session):
        """One XSS on the desk must not be a permanent credential."""
        await people(session)
        async with serving() as c:
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
        assert "httponly" in cookie_attrs(r, SESSION_COOKIE)

    async def test_the_csrf_cookie_deliberately_can_be(self, engine, session):
        """The page has to echo it back in a header, so it must be
        readable. That is what makes double-submit work at all."""
        await people(session)
        async with serving() as c:
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
        assert "httponly" not in cookie_attrs(r, CSRF_COOKIE)

    async def test_both_cookies_are_samesite_and_rooted(self, engine, session):
        await people(session)
        async with serving() as c:
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            attrs = cookie_attrs(r, name)
            assert "samesite=lax" in attrs, name
            assert "path=/" in attrs, name

    async def test_secure_is_the_shipped_default(self, engine, session):
        """A security control whose default is the unsafe setting is one
        that ships unsafe. Asserted on the setting *and* on the header,
        because this file's harness turns it off to be able to test
        anything else at all."""
        assert Settings.model_fields["session_cookie_secure"].default is True

        await people(session)
        async with serving(session_cookie_secure=True) as c:
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
        assert "secure" in cookie_attrs(r, SESSION_COOKIE)

    async def test_secure_can_be_turned_off_for_local_http(
            self, engine, session):
        await people(session)
        async with serving(session_cookie_secure=False) as c:
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
        assert "secure" not in cookie_attrs(r, SESSION_COOKIE)


# ------------------------------------------------------- who am I, still


class TestStayingSignedIn:
    async def test_me_names_the_person_behind_the_cookie(self, engine, session):
        await people(session)
        async with serving() as c:
            await c.post("/api/auth/login",
                         json={"email": MANAGER, "password": PASSWORD})
            r = await c.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["name"] == "Ruth Osei"
        assert r.json()["role"] == "manager"

    async def test_me_with_no_cookie_is_401(self, engine, session):
        async with serving() as c:
            r = await c.get("/api/auth/me")
        assert r.status_code == 401

    async def test_me_with_an_invented_cookie_is_401(self, engine, session):
        await people(session)
        async with serving() as c:
            c.cookies.set(SESSION_COOKIE, "a-token-nobody-issued")
            r = await c.get("/api/auth/me")
        assert r.status_code == 401

    async def test_an_expired_session_stops_working(self, engine, session):
        await people(session)
        async with serving() as c:
            await c.post("/api/auth/login",
                         json={"email": MANAGER, "password": PASSWORD})
            token = c.cookies.get(SESSION_COOKIE)
            assert (await c.get("/api/auth/me")).status_code == 200

            # Age the session past its expiry, in the database, the way
            # time would.
            row = await AccountSessionRepository(session).live(token)
            assert row is not None
            row[0].expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await session.commit()

            assert (await c.get("/api/auth/me")).status_code == 401

    async def test_disabling_the_account_ends_the_session_it_is_using(
            self, engine, session):
        """A person who has left keeps working until their cookie
        expires, otherwise."""
        (account,) = await people(session)
        async with serving() as c:
            await c.post("/api/auth/login",
                         json={"email": MANAGER, "password": PASSWORD})
            assert (await c.get("/api/auth/me")).status_code == 200

            account.disabled_at = datetime.now(timezone.utc)
            await session.commit()

            assert (await c.get("/api/auth/me")).status_code == 401


# ------------------------------------------------------------ signing out


class TestSigningOut:
    async def test_signing_out_ends_the_session_on_the_server(
            self, engine, session):
        """Not just in the browser. A cookie thrown away by a client is
        still a working credential for whoever has a copy."""
        await people(session)
        async with serving() as c:
            await c.post("/api/auth/login",
                         json={"email": MANAGER, "password": PASSWORD})
            token = c.cookies.get(SESSION_COOKIE)

            out = await c.post("/api/auth/logout")
            assert out.status_code == 200
            assert out.json()["signedOut"] is True

            assert await AccountSessionRepository(session).live(token) is None

    async def test_signing_out_clears_both_cookies(self, engine, session):
        await people(session)
        async with serving() as c:
            await c.post("/api/auth/login",
                         json={"email": MANAGER, "password": PASSWORD})
            await c.post("/api/auth/logout")
            assert not c.cookies.get(SESSION_COOKIE)
            assert not c.cookies.get(CSRF_COOKIE)
            assert (await c.get("/api/auth/me")).status_code == 401

    async def test_signing_out_when_not_signed_in_is_still_fine(
            self, engine, session):
        """A sign-out that can fail is one somebody gives up on."""
        async with serving() as c:
            r = await c.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json()["signedOut"] is False

    async def test_signing_out_twice_is_fine(self, engine, session):
        await people(session)
        async with serving() as c:
            await c.post("/api/auth/login",
                         json={"email": MANAGER, "password": PASSWORD})
            assert (await c.post("/api/auth/logout")).json()["signedOut"] is True
            assert (await c.post("/api/auth/logout")).json()["signedOut"] is False

    async def test_signing_out_here_does_not_sign_out_there(
            self, engine, session):
        """Two browsers are two sessions. Signing out at the desk must
        not sign the same person out on their phone."""
        await people(session)
        async with serving() as desk, serving() as phone:
            await desk.post("/api/auth/login",
                            json={"email": MANAGER, "password": PASSWORD})
            await phone.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
            await desk.post("/api/auth/logout")

            assert (await desk.get("/api/auth/me")).status_code == 401
            assert (await phone.get("/api/auth/me")).status_code == 200


# ------------------------------------------------------------- the throttle


class TestTheLoginThrottle:
    async def test_guessing_is_refused_after_the_limit(self, engine, session):
        await people(session)
        async with serving(login_rate_limit_per_min=5) as c:
            codes = [
                (await c.post("/api/auth/login",
                              json={"email": MANAGER, "password": "guess"})).status_code
                for _ in range(8)
            ]
        assert codes[:5] == [401] * 5, codes
        assert codes[5:] == [429] * 3, codes

    async def test_being_refused_says_when_to_come_back(self, engine, session):
        await people(session)
        async with serving(login_rate_limit_per_min=1) as c:
            await c.post("/api/auth/login",
                         json={"email": MANAGER, "password": "guess"})
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": "guess"})
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1

    async def test_the_throttle_can_be_turned_off(self, engine, session):
        """Both of them, because there are two now.

        This turned off the rate limiter alone and expected twelve
        unthrottled attempts. It stopped at five once the lockout
        existed - correctly, since the lockout is a second throttle with
        its own switch, and turning one off does not turn off the other.
        Off means off, so this asks for both.
        """
        await people(session)
        async with serving(login_rate_limit_per_min=0,
                           login_lockout_after=0) as c:
            codes = [
                (await c.post("/api/auth/login",
                              json={"email": MANAGER, "password": "guess"})).status_code
                for _ in range(12)
            ]
        assert set(codes) == {401}

    async def test_the_lockout_still_holds_when_the_rate_limit_is_off(
            self, engine, session):
        """They are separate switches guarding separate things - how fast
        one address may call, and how many times it may guess at one
        account. Turning the first off must not quietly disarm the
        second."""
        await people(session)
        async with serving(login_rate_limit_per_min=0,
                           login_lockout_after=3) as c:
            codes = [
                (await c.post("/api/auth/login",
                              json={"email": MANAGER, "password": "guess"})).status_code
                for _ in range(6)
            ]
        assert codes == [401, 401, 401, 429, 429, 429], codes

    async def test_a_throttled_caller_cannot_sign_in_with_the_right_password(
            self, engine, session):
        """The limit is on the route, not on failures. A correct password
        that arrives during a lockout still waits - otherwise the limit
        is only a limit until the attacker guesses right."""
        await people(session)
        async with serving(login_rate_limit_per_min=2) as c:
            for _ in range(2):
                await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": "guess"})
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
        assert r.status_code == 429


# ----------------------------------------------------------------- CSRF


def a_request(cookies: dict, headers: dict) -> Request:
    """A bare ASGI request carrying the cookies and headers given."""
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    if cookies:
        jar = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw.append((b"cookie", jar.encode()))
    return Request({"type": "http", "method": "POST", "path": "/api/push",
                    "headers": raw, "query_string": b""})


class TestCrossSiteRequests:
    """`require_csrf` is applied to the write routes in phase 3. It is
    tested here, where it is written, rather than left unexercised."""

    async def test_a_session_cookie_without_the_header_is_refused(self):
        req = a_request({SESSION_COOKIE: "s", CSRF_COOKIE: "c"}, {})
        with pytest.raises(HTTPException) as e:
            await require_csrf(req)
        assert e.value.status_code == 403

    async def test_a_header_that_does_not_match_the_cookie_is_refused(self):
        req = a_request({SESSION_COOKIE: "s", CSRF_COOKIE: "c"},
                        {"x-csrf-token": "something-else"})
        with pytest.raises(HTTPException) as e:
            await require_csrf(req)
        assert e.value.status_code == 403

    async def test_a_matching_header_is_allowed(self):
        req = a_request({SESSION_COOKIE: "s", CSRF_COOKIE: "the-token"},
                        {"x-csrf-token": "the-token"})
        assert await require_csrf(req) is None

    async def test_a_caller_with_no_session_cookie_is_not_subject_to_it(self):
        """A rig with a bearer token is not a browser and has no cookie
        the user did not choose to send. CSRF does not apply to it, and
        requiring a token there would break every rig on the floor."""
        req = a_request({}, {"authorization": "Bearer a-rig-token"})
        assert await require_csrf(req) is None


# ------------------------------------- the two trust models stay separate


class TestTheRigIsUnaffected:
    """The regression that matters most in this whole piece of work."""

    RIG = "RIG-03"
    RIG_TOKEN = "a-token-placed-by-ansible"

    async def test_a_signed_in_manager_cannot_speak_for_a_rig(
            self, engine, session):
        """A browser cookie must not open the ingest path. If it did,
        anyone who could sign in could file episodes under any rig."""
        await people(session)
        async with serving(rig_tokens={self.RIG: self.RIG_TOKEN}) as c:
            await c.post("/api/auth/login",
                         json={"email": MANAGER, "password": PASSWORD})
            assert (await c.get("/api/auth/me")).status_code == 200

            r = await c.get(f"/api/rigs/{self.RIG}/cursor")
        assert r.status_code == 401

    async def test_a_rig_still_needs_only_its_bearer_token(
            self, engine, session):
        """And no cookie, and no account. The rig has no login and is not
        getting one."""
        async with serving(rig_tokens={self.RIG: self.RIG_TOKEN}) as c:
            r = await c.get(f"/api/rigs/{self.RIG}/cursor",
                            headers={"Authorization": "Bearer " + self.RIG_TOKEN})
        assert r.status_code == 200
        assert r.json()["rigId"] == self.RIG

    async def test_a_rig_token_does_not_sign_anybody_in(self, engine, session):
        """The other direction. A machine credential must not resolve to
        a person."""
        await people(session)
        async with serving(rig_tokens={self.RIG: self.RIG_TOKEN}) as c:
            r = await c.get("/api/auth/me",
                            headers={"Authorization": "Bearer " + self.RIG_TOKEN})
        assert r.status_code == 401


# ------------------------------------------------ what the service admits


class TestHealthSaysWhatIsOpen:
    """A service that is quietly unauthenticated looks exactly like one
    that is correctly configured, until the day it does not. Every switch
    here is something a deploy check can fail on."""

    async def test_person_auth_is_off_until_somebody_has_an_account(
            self, engine, session):
        async with serving() as c:
            r = await c.get("/api/health")
        assert r.status_code == 200
        assert r.json()["personAuth"] == "off"

    async def test_person_auth_is_on_once_somebody_does(self, engine, session):
        await people(session)
        async with serving() as c:
            r = await c.get("/api/health")
        assert r.json()["personAuth"] == "on"

    async def test_an_insecure_cookie_is_reported_in_capitals(
            self, engine, session):
        """Hard to read past in a deploy log."""
        async with serving(session_cookie_secure=False) as c:
            r = await c.get("/api/health")
        assert r.json()["sessionCookie"] == "INSECURE"

        async with serving(session_cookie_secure=True) as c:
            r = await c.get("/api/health")
        assert r.json()["sessionCookie"] == "secure"

    async def test_the_login_throttle_is_reported(self, engine, session):
        async with serving(login_rate_limit_per_min=10) as c:
            assert (await c.get("/api/health")).json()["loginRateLimit"] == "10/min"
        async with serving(login_rate_limit_per_min=0) as c:
            assert (await c.get("/api/health")).json()["loginRateLimit"] == "off"

    async def test_the_rig_switches_are_still_reported_unchanged(
            self, engine, session):
        """Adding person auth must not have quietly dropped any of the
        four things health already answered."""
        async with serving() as c:
            body = (await c.get("/api/health")).json()
        for key in ("rigAuth", "rigIdentity", "deskAuth", "floorReads",
                    "rigRateLimit"):
            assert key in body, key


# --------------------------------------- what a screen asks before drawing


class TestTheBootProbe:
    """`/auth/me` answers 401 both for "you are not signed in" and for
    "this deployment has no accounts", and a screen that cannot tell
    those apart either shows a login box nobody has a password for, or
    opens the desk on a deployment nobody has configured yet."""

    async def test_it_says_off_where_nobody_can_sign_in(self, engine, session):
        async with serving() as c:
            r = await c.get("/api/auth/session")
        assert r.status_code == 200
        assert r.json()["personAuth"] == "off"
        assert r.json()["account"] is None

    async def test_it_says_on_but_nobody_when_accounts_exist(
            self, engine, session):
        await people(session)
        async with serving() as c:
            r = await c.get("/api/auth/session")
        assert r.status_code == 200
        assert r.json()["personAuth"] == "on"
        assert r.json()["account"] is None

    async def test_it_names_whoever_is_signed_in(self, engine, session):
        await people(session)
        async with serving() as c:
            await c.post("/api/auth/login",
                         json={"email": MANAGER, "password": PASSWORD})
            r = await c.get("/api/auth/session")
        body = r.json()
        assert body["personAuth"] == "on"
        assert body["account"]["name"] == "Ruth Osei"
        assert body["account"]["role"] == "manager"

    async def test_it_names_an_operator_as_an_operator(self, engine, session):
        await people(session, manager=False, operator=True)
        async with serving() as c:
            await c.post("/api/auth/login",
                         json={"email": OPERATOR, "password": PASSWORD})
            body = (await c.get("/api/auth/session")).json()
        assert body["account"]["role"] == "operator"
        assert body["account"]["operatorId"] == "op-a2"

    async def test_it_hands_back_the_csrf_token_after_a_reload(
            self, engine, session):
        """A page that has just been reloaded has to be able to write
        without re-reading its own cookies."""
        await people(session)
        async with serving() as c:
            login = await c.post("/api/auth/login",
                                 json={"email": MANAGER, "password": PASSWORD})
            probe = await c.get("/api/auth/session")
        assert probe.json()["csrfToken"] == login.json()["csrfToken"]

    async def test_it_never_answers_401(self, engine, session):
        """It is the question asked before there is a credential to
        present, so refusing it would make the screen unable to ask."""
        await people(session)
        async with serving() as c:
            assert (await c.get("/api/auth/session")).status_code == 200
            c.cookies.set(SESSION_COOKIE, "a-token-nobody-issued")
            assert (await c.get("/api/auth/session")).status_code == 200

    async def test_it_gives_nothing_away_to_a_stranger(self, engine, session):
        """Only whether the door is locked, which anyone standing at a
        door can already see."""
        await people(session)
        async with serving() as c:
            body = (await c.get("/api/auth/session")).json()
        assert set(body) == {"personAuth", "account", "csrfToken"}
        assert body["account"] is None and body["csrfToken"] is None
        assert MANAGER not in r_text(body)


def r_text(body) -> str:
    import json
    return json.dumps(body)
