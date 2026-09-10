"""Changing your own password.

Until this existed, nobody could. A manager re-ran `mint_account passwd`
and told the person their new password out of band, which means every
password on the floor travelled through a chat message at least once.

Four properties are being protected, and each is here because the
obvious implementation gets it wrong.

**The current password is required despite the session.** A cookie
proves this browser signed in once, not that the person at the keyboard
is the account holder. Screens on a floor are left open.

**A change ends every other session.** The reason to change a password
is usually that it might be known. A change that leaves the old sessions
alive does nothing at all to whoever already has one.

**But not this one.** They just proved the current password, and a flow
that signs you out for using it is one people stop using.

**One rule about what a password may be.** It used to live inside
`mint_account`'s interactive prompt, so `--password` set anything at all
- the check was on how the password was typed rather than on the
password. Two ways to set one now, so the rule moved somewhere both can
reach.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from core.domains.accounts.model import Account, AccountSession
from core.domains.accounts.passwords import (
    MIN_PASSWORD_LENGTH, hash_password, password_complaint, verify_password,
)
from core.infrastructure.config import Settings, get_settings
from services.rigs.people import CSRF_HEADER, SESSION_COOKIE, reset_login_limiter

PASSWORD = "a-real-password-12"
NEW = "a-different-password-34"
MANAGER = "r.osei@verlet.co"
OPERATOR = "m.chen@verlet.co"


@pytest.fixture(autouse=True)
def _fresh():
    reset_login_limiter()
    yield
    reset_login_limiter()


@asynccontextmanager
async def serving(caller="10.0.0.2", **overrides):
    """`session_cookie_secure` off for the same reason as everywhere else
    here: httpx will not send a Secure cookie over http."""
    from services.rigs.app import create_app

    base = get_settings()
    settings = Settings(**{**base.model_dump(),
                           "session_cookie_secure": False, **overrides})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(
        transport=ASGITransport(app=app, client=(caller, 51234)),
        base_url="http://test",
    ) as c:
        yield c


async def accounts(session):
    rows = [
        Account(email=MANAGER, name="Ruth Osei", role="manager",
                password_hash=hash_password(PASSWORD)),
        Account(email=OPERATOR, name="Mei Chen", role="operator",
                person_id=uuid.UUID("bbbbbbbb-0000-4000-8000-000000000001"), password_hash=hash_password(PASSWORD)),
    ]
    session.add_all(rows)
    await session.commit()
    return rows


async def signed_in(client, email=MANAGER, password=PASSWORD):
    r = await client.post("/api/auth/login",
                          json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["csrfToken"]


async def change(client, csrf, current=PASSWORD, new=NEW):
    return await client.post(
        "/api/auth/password",
        json={"currentPassword": current, "newPassword": new},
        headers={CSRF_HEADER: csrf})


# ------------------------------------------------------ it actually works


class TestChangingIt:
    async def test_a_manager_can_change_their_own_password(self, engine, session):
        who = (await accounts(session))[0]
        async with serving() as c:
            csrf = await signed_in(c)
            r = await change(c, csrf)
        assert r.status_code == 200, r.text

        await session.refresh(who)
        assert verify_password(NEW, who.password_hash), "the new password does not verify"
        assert not verify_password(PASSWORD, who.password_hash), "the old one still works"

    async def test_an_operator_can_too(self, engine, session):
        """Both roles have accounts. A screen an operator could only get
        fixed by asking a manager is the thing this route removes."""
        who = (await accounts(session))[1]
        async with serving() as c:
            csrf = await signed_in(c, OPERATOR)
            r = await change(c, csrf)
        assert r.status_code == 200, r.text
        await session.refresh(who)
        assert verify_password(NEW, who.password_hash)

    async def test_the_new_password_then_signs_in(self, engine, session):
        """The property that matters to a person: it works next time."""
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            assert (await change(c, csrf)).status_code == 200
            await c.post("/api/auth/logout")

            refused = await c.post("/api/auth/login",
                                   json={"email": MANAGER, "password": PASSWORD})
            accepted = await c.post("/api/auth/login",
                                    json={"email": MANAGER, "password": NEW})
        assert refused.status_code == 401, "the old password still signs in"
        assert accepted.status_code == 200, "the new password does not sign in"

    async def test_the_hash_is_stored_not_the_password(self, engine, session):
        who = (await accounts(session))[0]
        async with serving() as c:
            csrf = await signed_in(c)
            await change(c, csrf)
        await session.refresh(who)
        assert NEW not in who.password_hash
        assert who.password_hash.startswith("scrypt$")


# --------------------------------------------------- what it refuses to do


class TestWhatItRefuses:
    async def test_the_wrong_current_password_is_refused(self, engine, session):
        who = (await accounts(session))[0]
        async with serving() as c:
            csrf = await signed_in(c)
            r = await change(c, csrf, current="not-my-password")
        assert r.status_code == 400
        await session.refresh(who)
        assert verify_password(PASSWORD, who.password_hash), "it changed anyway"

    async def test_a_session_alone_is_not_enough(self, engine, session):
        """The one worth stating plainly.

        Without this, anybody passing an unattended desk with an open tab
        could take the account - set a new password, and lock the manager
        out of their own floor. The cookie proves the browser signed in
        once, not who is at the keyboard now.
        """
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            r = await c.post("/api/auth/password",
                             json={"currentPassword": "", "newPassword": NEW},
                             headers={CSRF_HEADER: csrf})
        assert r.status_code in (400, 422), (
            "a signed-in browser changed the password without proving the old one")

    async def test_a_short_password_is_refused(self, engine, session):
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            r = await change(c, csrf, new="short")
        assert r.status_code == 400
        assert "short" in r.json()["detail"].lower()

    async def test_the_same_password_again_is_refused(self, engine, session):
        """Not dangerous, just pointless - and accepting it silently
        would have somebody believe they had changed something."""
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            r = await change(c, csrf, new=PASSWORD)
        assert r.status_code == 400
        assert "already" in r.json()["detail"].lower()

    async def test_nobody_signed_in_is_refused(self, engine, session):
        await accounts(session)
        async with serving() as c:
            r = await c.post("/api/auth/password",
                             json={"currentPassword": PASSWORD, "newPassword": NEW})
        assert r.status_code == 401

    async def test_csrf_is_required(self, engine, session):
        """It is a state-changing POST driven from a page, so it needs the
        same second lock as the push."""
        who = (await accounts(session))[0]
        async with serving() as c:
            await signed_in(c)
            r = await c.post("/api/auth/password",
                             json={"currentPassword": PASSWORD, "newPassword": NEW})
        assert r.status_code == 403
        await session.refresh(who)
        assert verify_password(PASSWORD, who.password_hash)

    async def test_another_sessions_csrf_token_does_not_work(self, engine, session):
        await accounts(session)
        async with serving() as c:
            other = await signed_in(c, OPERATOR)
            await c.post("/api/auth/logout")
            mine = await signed_in(c, MANAGER)
            assert other != mine
            r = await change(c, other)
        assert r.status_code == 403

    async def test_a_rig_token_cannot_change_anybodys_password(self, engine, session):
        """The two trust models stay apart. A machine credential is not a
        person and has no password to change."""
        await accounts(session)
        async with serving(rig_tokens={"RIG-03": "a-token"}) as c:
            r = await c.post("/api/auth/password",
                             json={"currentPassword": PASSWORD, "newPassword": NEW},
                             headers={"Authorization": "Bearer a-token"})
        assert r.status_code in (401, 403)


# ------------------------------------------------------- the other sessions


class TestTheOtherSessionsEnd:
    async def test_a_session_elsewhere_is_revoked(self, engine, session):
        """The reason to change a password is usually that it might be
        known. Leaving the other sessions alive does nothing to whoever
        already has one."""
        await accounts(session)
        async with serving() as elsewhere:
            await signed_in(elsewhere)
            assert (await elsewhere.get("/api/auth/me")).status_code == 200
            stolen = elsewhere.cookies.get(SESSION_COOKIE)

            async with serving() as here:
                csrf = await signed_in(here)
                assert (await change(here, csrf)).status_code == 200

            elsewhere.cookies.set(SESSION_COOKIE, stolen)
            after = await elsewhere.get("/api/auth/me")

        assert after.status_code == 401, (
            "a session opened before the change still works after it")

    async def test_this_browser_keeps_working(self, engine, session):
        """They just proved the current password. Signing them out for it
        is friction with nothing bought, and a flow that does it is one
        people avoid."""
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            r = await change(c, csrf)
            assert r.status_code == 200
            after = await c.get("/api/auth/me")
        assert after.status_code == 200, "the change signed them out of their own tab"

    async def test_the_reply_carries_a_fresh_csrf_token(self, engine, session):
        """The old session row is gone, so the page needs the new pair or
        its next write fails for no reason it can explain."""
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            r = await change(c, csrf)
            fresh = r.json()["csrfToken"]
            assert fresh and fresh != csrf, "the page was left holding a dead token"
            again = await c.post("/api/auth/password",
                                 json={"currentPassword": NEW,
                                       "newPassword": "a-third-password-56"},
                                 headers={CSRF_HEADER: fresh})
        assert again.status_code == 200, "the token the reply handed back did not work"

    async def test_exactly_one_session_row_survives(self, engine, session):
        await accounts(session)
        async with serving() as a:
            await signed_in(a)
            async with serving() as b:
                await signed_in(b)
                async with serving() as c:
                    csrf = await signed_in(c)
                    assert (await change(c, csrf)).status_code == 200

        rows = (await session.execute(select(AccountSession))).scalars().all()
        live = [r for r in rows if r.revoked_at is None]
        assert len(live) == 1, f"expected one live session, found {len(live)}"


# ------------------------------------------------------------ the throttle


class TestGuessingIsThrottled:
    async def test_repeated_wrong_current_passwords_lock_out(self, engine, session):
        """An open tab would otherwise let somebody work through a list of
        likely passwords at the speed of the network."""
        await accounts(session)
        async with serving(login_lockout_after=3) as c:
            csrf = await signed_in(c)
            for _ in range(4):
                await change(c, csrf, current="wrong")
            r = await change(c, csrf, current="wrong")
        assert r.status_code == 429
        assert r.headers.get("retry-after")

    async def test_the_lockout_does_not_punish_a_rejected_new_password(
            self, engine, session):
        """Getting the rule wrong is not guessing. Locking somebody out
        for choosing a short password would punish the one thing this
        route exists to let them do."""
        await accounts(session)
        async with serving(login_lockout_after=3) as c:
            csrf = await signed_in(c)
            for _ in range(6):
                r = await change(c, csrf, new="short")
                assert r.status_code == 400, r.text
            good = await change(c, csrf)
        assert good.status_code == 200, (
            "choosing a short password several times locked the account")

    async def test_getting_it_right_clears_the_count(self, engine, session):
        await accounts(session)
        async with serving(login_lockout_after=5) as c:
            csrf = await signed_in(c)
            await change(c, csrf, current="wrong")
            await change(c, csrf, current="wrong")
            r = await change(c, csrf)
            assert r.status_code == 200
            fresh = r.json()["csrfToken"]
            for _ in range(2):
                await c.post("/api/auth/password",
                             json={"currentPassword": "wrong", "newPassword": "x" * 20},
                             headers={CSRF_HEADER: fresh})
            again = await c.post("/api/auth/password",
                                 json={"currentPassword": NEW,
                                       "newPassword": "a-third-password-56"},
                                 headers={CSRF_HEADER: fresh})
        assert again.status_code == 200, "a success did not clear the failure count"


# -------------------------------------------------------- one rule, one place


class TestOnePasswordRule:
    def test_the_rule_is_a_sentence_or_nothing(self):
        assert password_complaint("x" * MIN_PASSWORD_LENGTH) is None
        assert password_complaint("x" * (MIN_PASSWORD_LENGTH - 1))
        assert isinstance(password_complaint("short"), str)

    def test_mint_account_uses_the_same_rule_on_the_flag_path(self):
        """The bug this consolidation fixes.

        `--password` skipped the length check entirely: it sat inside the
        interactive branch, so the check was on how the password was
        typed rather than on the password. The one path a script would
        use was the one with no rule at all.
        """
        import tools.mint_account as mint

        with pytest.raises(SystemExit):
            mint._password("short", prompt=False)

        ok, shown = mint._password("x" * MIN_PASSWORD_LENGTH, prompt=False)
        assert ok == "x" * MIN_PASSWORD_LENGTH and shown is False

    def test_a_generated_password_satisfies_it(self):
        """Raising the minimum past what the generator makes would
        otherwise mint accounts that breach the rule."""
        made, shown = __import__("tools.mint_account", fromlist=["x"])._password(
            None, prompt=False)
        assert shown is True
        assert password_complaint(made) is None
