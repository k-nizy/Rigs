"""A password nobody remembers.

Different from `test_password_change.py` next door, and the difference is
the whole problem: this has to work for somebody who *cannot sign in*.
There is no session to lean on, so the only thing a reset can ever prove
is that whoever is asking can read the account's mailbox.

That makes the address a credential, and it puts three properties on this
flow that the signed-in change does not need.

**The request route answers the same way for an address that does not
exist.** It is reachable by anybody who can load the sign-in page and it
takes an email, so a version that said "no such account" would be a
faster way to enumerate the floor's staff than the login route the dummy
hash was built to protect.

**The token works once.** It is spent by an UPDATE whose WHERE says
`used_at IS NULL`, so two racing requests are decided by the database
rather than by an if-statement with a gap in the middle.

**Off means refused, not open.** With no relay configured the routes
answer 404. Every failure this repo has had in this area was something
unverifiable being read as permission, and a reset flow that shrugged and
carried on with no way to deliver a token would be the next one.

No mail leaves this machine: `mail.send` is replaced throughout, and one
test asserts that the replacement is actually reached rather than quietly
skipped.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from core.domains.accounts.model import Account, AccountSession, PasswordReset
from core.domains.accounts.passwords import hash_password, verify_password
from core.domains.accounts.repository import PasswordResetRepository
from core.infrastructure.config import Settings, get_settings
from services.rigs import mail, people
from services.rigs.people import (
    RESET_SENT as RESET_SENT_TEXT, SESSION_COOKIE, reset_login_limiter,
)

PASSWORD = "a-real-password-12"
NEW = "a-brand-new-password-34"
MANAGER = "r.osei@verlet.co"
OPERATOR = "m.chen@verlet.co"
GONE = "left@verlet.co"

ON = dict(smtp_host="mail.test", public_base_url="https://floor.test",
          session_cookie_secure=False)


@pytest.fixture(autouse=True)
def _fresh():
    reset_login_limiter()
    yield
    reset_login_limiter()


class Outbox(list):
    """Every message the service tried to send, and whether it 'worked'."""

    deliver = True

    def sender(self):
        async def send(to, subject, body, settings):
            self.append({"to": to, "subject": subject, "body": body})
            return self.deliver
        return send

    @property
    def links(self):
        out = []
        for m in self:
            for word in m["body"].split():
                if word.startswith("https://") or word.startswith("http://"):
                    out.append(word)
        return out

    def token_for(self, index=-1):
        """The token out of a link, and only from a *fragment* link.

        Splitting on "reset=" alone matches `?reset=` and `#reset=`
        identically, so every test that pulls a token this way would keep
        passing whichever form the service emitted - including the
        half-done state where the link is a fragment but the page still
        reads the query string, which breaks every real link while CI
        stays green. So the shape is asserted here, once, where no test
        that uses a token can get past it.
        """
        link = self.links[index]
        assert "#reset=" in link, (
            f"the reset link is not a fragment: {link}. As a query "
            f"parameter the token goes up in the request line and into "
            f"nginx's access log, still valid for half an hour")
        assert "?reset=" not in link, f"the token is in the query string: {link}"
        return link.split("#reset=")[1]


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    # Patched where it is *used*, not only where it is defined - people.py
    # holds its own reference to the module.
    monkeypatch.setattr(mail, "send", box.sender())
    monkeypatch.setattr(people.mail, "send", box.sender())
    return box


@asynccontextmanager
async def serving(caller="10.0.0.2", **overrides):
    from services.rigs.app import create_app

    base = get_settings()
    settings = Settings(**{**base.model_dump(), **ON, **overrides})
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
                operator_id="op-a2", password_hash=hash_password(PASSWORD)),
        Account(email=GONE, name="Someone Left", role="operator",
                operator_id="op-a7", password_hash=hash_password(PASSWORD),
                disabled_at=datetime.now(timezone.utc)),
    ]
    session.add_all(rows)
    await session.commit()
    return rows


async def ask(client, email=MANAGER):
    return await client.post("/api/auth/reset/request", json={"email": email})


async def use(client, token, new=NEW):
    return await client.post("/api/auth/reset",
                             json={"token": token, "newPassword": new})


# ------------------------------------------------------- the happy path


class TestGettingBackIn:
    async def test_a_link_is_sent_and_sets_a_new_password(
            self, engine, session, outbox):
        who = (await accounts(session))[0]
        async with serving() as c:
            r = await ask(c)
            assert r.status_code == 200, r.text
            assert len(outbox) == 1, "no mail was sent"

            done = await use(c, outbox.token_for())
        assert done.status_code == 200, done.text

        await session.refresh(who)
        assert verify_password(NEW, who.password_hash)
        assert not verify_password(PASSWORD, who.password_hash)

    async def test_it_signs_them_in_rather_than_sending_them_to_the_login_box(
            self, engine, session, outbox):
        """They have just proved the mailbox and chosen a password. Making
        them type it again into a sign-in form is a step with nothing
        behind it."""
        await accounts(session)
        async with serving() as c:
            await ask(c)
            done = await use(c, outbox.token_for())
            assert done.json()["csrfToken"], "no session came back"
            me = await c.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["name"] == "Ruth Osei"

    async def test_an_operator_gets_a_link_to_my_shift_and_a_manager_to_the_desk(
            self, engine, session, outbox):
        """Being sent to the screen that will refuse you is a poor way to
        end a password reset."""
        await accounts(session)
        async with serving() as c:
            await ask(c, MANAGER)
            await ask(c, OPERATOR)

        assert "/rotation-desk-v1/" in outbox.links[0], outbox.links[0]
        assert "/apps/my-shift/" in outbox.links[1], outbox.links[1]
        assert all(l.startswith("https://floor.test") for l in outbox.links)

    async def test_the_mail_says_how_long_it_lasts_and_what_to_do_if_it_was_not_you(
            self, engine, session, outbox):
        await accounts(session)
        async with serving(password_reset_minutes=45) as c:
            await ask(c)
        body = outbox[0]["body"]
        assert "45 minutes" in body
        assert "not you" in body.lower()
        assert outbox[0]["to"] == MANAGER

    async def test_the_token_is_not_stored_in_the_clear(
            self, engine, session, outbox):
        await accounts(session)
        async with serving() as c:
            await ask(c)
        token = outbox.token_for()
        row = (await session.execute(select(PasswordReset))).scalars().one()
        assert token not in row.token_fingerprint
        assert len(row.token_fingerprint) == 64


# ------------------------------------------------ saying nothing about who


class TestItNeverSaysWhoExists:
    async def test_an_unknown_address_gets_the_same_reply(
            self, engine, session, outbox):
        await accounts(session)
        async with serving() as c:
            real = await ask(c, MANAGER)
            fake = await ask(c, "nobody-at-all@verlet.co")

        assert real.status_code == fake.status_code == 200
        assert real.json() == fake.json(), (
            "the reply distinguishes a real address from an invented one")
        assert len(outbox) == 1, "mail went to an address with no account"

    async def test_a_disabled_account_gets_the_same_reply_and_no_link(
            self, engine, session, outbox):
        """Somebody who has left must not be able to walk back in, and
        their address may since have been given to somebody else."""
        await accounts(session)
        async with serving() as c:
            r = await ask(c, GONE)
        assert r.status_code == 200
        assert outbox == [], "a disabled account was sent a way back in"

    async def test_a_relay_that_refuses_does_not_change_the_reply(
            self, engine, session, outbox):
        """The reply cannot depend on delivery either - a 500 when the
        relay rejects an address is the same oracle by another route."""
        await accounts(session)
        outbox.deliver = False
        async with serving() as c:
            good = await ask(c, MANAGER)
        assert good.status_code == 200
        assert good.json()["detail"]


# ------------------------------------------------------------ single use


class TestTheLinkWorksOnce:
    async def test_a_second_use_is_refused(self, engine, session, outbox):
        await accounts(session)
        async with serving() as c:
            await ask(c)
            token = outbox.token_for()
            first = await use(c, token)
            second = await use(c, token, "a-third-password-56")

        assert first.status_code == 200
        assert second.status_code == 400
        assert "used" in second.json()["detail"].lower()

    async def test_asking_again_voids_the_first_link(
            self, engine, session, outbox):
        """Somebody who asks three times because nothing seemed to arrive
        would otherwise have three working ways in, all sitting in a
        mailbox. Only the newest should open the door."""
        await accounts(session)
        async with serving() as c:
            await ask(c)
            first = outbox.token_for(0)
            await ask(c)
            second = outbox.token_for(1)
            assert first != second

            stale = await use(c, first)
            fresh = await use(c, second)

        assert stale.status_code == 400, "an older link still worked"
        assert fresh.status_code == 200

    async def test_an_expired_link_is_refused(self, engine, session, outbox):
        await accounts(session)
        async with serving(password_reset_minutes=30) as c:
            await ask(c)
            token = outbox.token_for()

            row = (await session.execute(select(PasswordReset))).scalars().one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await session.commit()

            r = await use(c, token)
        assert r.status_code == 400
        assert "expired" in r.json()["detail"].lower()

    async def test_an_invented_token_is_refused(self, engine, session, outbox):
        await accounts(session)
        async with serving() as c:
            r = await use(c, "not-a-real-token-at-all")
        assert r.status_code == 400

    async def test_a_short_password_does_not_burn_the_link(
            self, engine, session, outbox):
        """Getting the rule wrong should not send somebody back to their
        mailbox for a fresh link."""
        await accounts(session)
        async with serving() as c:
            await ask(c)
            token = outbox.token_for()
            bad = await use(c, token, "short")
            good = await use(c, token, NEW)

        assert bad.status_code == 400
        assert good.status_code == 200, "a rejected password spent the link"


# ----------------------------------------------------- what a reset ends


class TestAResetEndsWhatCameBefore:
    async def test_every_session_is_revoked(self, engine, session, outbox):
        """If the reason for the reset is that somebody else knew the
        password, a session they already hold is the thing that matters."""
        await accounts(session)
        async with serving() as intruder:
            r = await intruder.post("/api/auth/login",
                                    json={"email": MANAGER, "password": PASSWORD})
            assert r.status_code == 200
            held = intruder.cookies.get(SESSION_COOKIE)

            async with serving() as owner:
                await ask(owner)
                assert (await use(owner, outbox.token_for())).status_code == 200

            intruder.cookies.set(SESSION_COOKIE, held)
            after = await intruder.get("/api/auth/me")
        assert after.status_code == 401, "a session from before the reset survived"

    async def test_changing_the_password_voids_an_outstanding_link(
            self, engine, session, outbox):
        """The other direction, and easy to miss. A link still lying in a
        mailbox after the password has changed is a way back in for
        whoever else can read that mailbox."""
        who = (await accounts(session))[0]
        async with serving() as c:
            await ask(c)
            token = outbox.token_for()

            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
            csrf = r.json()["csrfToken"]
            changed = await c.post(
                "/api/auth/password",
                json={"currentPassword": PASSWORD, "newPassword": NEW},
                headers={"x-csrf-token": csrf})
            assert changed.status_code == 200, changed.text

            stale = await use(c, token, "a-third-password-56")

        assert stale.status_code == 400, (
            "a reset link issued before the password changed still worked")
        await session.refresh(who)
        assert verify_password(NEW, who.password_hash)

    async def test_using_a_link_leaves_exactly_one_live_session(
            self, engine, session, outbox):
        await accounts(session)
        async with serving() as a:
            await a.post("/api/auth/login",
                         json={"email": MANAGER, "password": PASSWORD})
            async with serving() as b:
                await ask(b)
                assert (await use(b, outbox.token_for())).status_code == 200

        rows = (await session.execute(select(AccountSession))).scalars().all()
        live = [r for r in rows if r.revoked_at is None]
        assert len(live) == 1, f"expected one live session, found {len(live)}"


# --------------------------------------------------- off until configured


class TestWithNoRelayConfigured:
    async def test_both_routes_refuse(self, engine, session, outbox):
        """Not a cheerful 200. A deployment that cannot deliver a token
        must not leave somebody waiting at a screen for mail that was
        never going to be sent."""
        await accounts(session)
        async with serving(smtp_host="") as c:
            asked = await ask(c)
            used = await use(c, "anything-at-all")

        assert asked.status_code == 404
        assert used.status_code == 404
        assert outbox == []

    async def test_it_says_what_to_do_instead(self, engine, session, outbox):
        await accounts(session)
        async with serving(smtp_host="") as c:
            r = await ask(c)
        assert "manager" in r.json()["detail"].lower()

    async def test_nothing_else_about_signing_in_is_affected(
            self, engine, session, outbox):
        await accounts(session)
        async with serving(smtp_host="") as c:
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
        assert r.status_code == 200

    async def test_health_reports_it_and_never_disagrees_with_the_routes(
            self, engine, session, outbox):
        """A deploy check reads this, so the two are asserted together.

        A switch that could say "on" while the routes answer 404 - or
        the reverse - would be believed, and a floor would be told it
        had a way back in that it does not have. Reporting it through
        the same `mail.is_configured` the routes ask is what makes them
        agree; this is the test that would notice if they stopped.
        """
        await accounts(session)

        async with serving(smtp_host="") as c:
            assert (await c.get("/api/health")).json()["passwordReset"] == "off"
            assert (await ask(c)).status_code == 404

        async with serving() as c:
            assert (await c.get("/api/health")).json()["passwordReset"] == "on"
            assert (await ask(c)).status_code != 404


# ------------------------------------------------------------ throttling


class TestItCannotBeUsedToSpamSomebody:
    async def test_a_run_of_requests_is_held(self, engine, session, outbox):
        """This route sends mail to somebody else's inbox. Unbounded, it
        is a way to have the floor deliver a hundred messages to a person
        who asked for none."""
        await accounts(session)
        async with serving(password_reset_per_hour=3) as c:
            codes = [(await ask(c)).status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 429, codes
        assert len(outbox) == 3, f"{len(outbox)} messages sent past the limit"

    async def test_it_says_how_long_to_wait(self, engine, session, outbox):
        await accounts(session)
        async with serving(password_reset_per_hour=2) as c:
            for _ in range(3):
                r = await ask(c)
        assert r.status_code == 429
        assert r.headers.get("retry-after")


# ------------------------------------------------------- the mail itself


class TestTheMailModule:
    def test_it_is_off_without_a_host(self):
        assert not mail.is_configured(Settings(
            database_url="postgresql+asyncpg://x@y/z"))
        assert mail.is_configured(Settings(
            database_url="postgresql+asyncpg://x@y/z", smtp_host="mail.test"))

    async def test_sending_with_no_host_is_false_rather_than_a_crash(self):
        """The caller answers identically whatever happens here, so this
        must never raise - an exception on a real address and a clean
        return on an invented one is the same oracle by another route."""
        s = Settings(database_url="postgresql+asyncpg://x@y/z")
        assert await mail.send("a@b.c", "s", "b", s) is False

    async def test_a_relay_that_is_not_there_is_false_rather_than_a_crash(self):
        s = Settings(database_url="postgresql+asyncpg://x@y/z",
                     smtp_host="127.0.0.1", smtp_port=1)
        assert await mail.send("a@b.c", "s", "b", s) is False

    def test_the_message_is_addressed_and_has_a_from(self):
        s = Settings(database_url="postgresql+asyncpg://x@y/z",
                     smtp_host="mail.test", smtp_from="floor@verlet.co")
        msg = mail._build("m.chen@verlet.co", "Subject here", "Body here", s)
        assert msg["To"] == "m.chen@verlet.co"
        assert msg["From"] == "floor@verlet.co"
        assert msg["Subject"] == "Subject here"
        assert "Body here" in msg.get_content()

    async def test_the_outbox_fixture_is_actually_reached(
            self, engine, session, outbox):
        """The test above this file's own machinery. If `mail.send` were
        patched somewhere the service does not look, every test here
        would pass while sending real mail - or none at all - and none of
        them would notice.
        """
        await accounts(session)
        async with serving() as c:
            await ask(c)
        assert len(outbox) == 1, (
            "the service did not go through the patched sender, so these "
            "tests are not asserting what they appear to")


class TestTheTokenIsNotInTheQueryString:
    """A browser never sends the fragment to the server. That is the only
    reason the token is in one.

    `deploy/nginx.conf` sets `access_log off` on `/healthz` and nothing
    else, so `/apps/my-shift/` and `/rotation-desk-v1/` are logged with
    the full request line. As `?reset=TOKEN` every reset link would have
    been written into the access log in the clear, still good for
    `password_reset_minutes` - a live credential in a file that gets
    shipped wherever logs get shipped.

    Taking it out of the address bar afterwards, which both pages do,
    does not help with that: it covers history and referrers, and the GET
    has already happened.
    """

    async def test_the_link_puts_the_token_in_the_fragment(
            self, engine, session, outbox):
        await accounts(session)
        async with serving() as c:
            await ask(c, MANAGER)
        link = outbox.links[0]
        assert "#reset=" in link, link
        assert "?reset=" not in link, link
        assert "?" not in link, f"nothing should be in the query string: {link}"

    async def test_it_holds_for_an_operator_too(self, engine, session, outbox):
        await accounts(session)
        async with serving() as c:
            await ask(c, OPERATOR)
        assert "#reset=" in outbox.links[0]
        assert "?" not in outbox.links[0]

    def test_the_builder_itself_emits_a_fragment(self):
        """Asserted on the function, not only through a sent mail, so it
        cannot be satisfied by a test helper that rewrites the link."""
        from core.domains.accounts.model import Account
        from services.rigs.people import reset_link

        s = Settings(database_url="postgresql+asyncpg://x@y/z",
                     public_base_url="https://floor.test")
        who = Account(email=MANAGER, name="R", role="manager",
                      password_hash="x")
        link = reset_link("a-token", who, s)
        assert link == "https://floor.test/rotation-desk-v1/#reset=a-token", link


# ------------------------------------------------------------- the clock


class TestTheClockSaysNothingEither:
    """The reply is identical by construction. The time to produce it was
    not, and that is the half easy to write down and then not check.

    Measured on a live service before this was fixed: an address with no
    account came back in 16ms, one with an account in 977ms - a token
    minted, two rows written, committed, and an entire SMTP conversation
    held open inside the request. Every real request was slower than
    every invented one. Identical wording and a sixty-fold difference in
    latency is an oracle with a polite error message, and a better one
    than the login route's, because it needs no credential and no
    guessing.

    Timing is not asserted here - a clock test in CI is a flaky test.
    What is asserted is the structure that makes the property hold, which
    is deterministic: the route does no work, so there is no work to
    measure.
    """

    def test_the_route_does_no_database_work(self):
        """No session dependency at all. Acquiring one is work, and work
        is what leaks - so the absence is the property, not an oversight."""
        import inspect

        from services.rigs.routes import request_password_reset

        params = inspect.signature(request_password_reset).parameters
        assert "session" not in params, (
            "the reset request route took a database session again. Whatever "
            "it does with one happens on the request path, and the request "
            "path is what an attacker times")

    def test_the_route_hands_the_work_to_a_background_task(self):
        source = inspect_source("request_password_reset")
        assert "background.add_task" in source, (
            "the work is back on the request path; the reply now takes as "
            "long as the mail does, which says which addresses are real")
        assert "await begin_reset" not in source, (
            "begin_reset is being awaited inline again - that is the leak")

    def test_the_throttle_is_checked_before_anything_is_scheduled(self):
        """So a refusal costs no more than an acceptance."""
        source = inspect_source("request_password_reset")
        assert source.index("reset_limiter") < source.index("background.add_task")

    async def test_the_work_still_happens(self, engine, session, outbox):
        """The obvious way to make the timings match is to stop sending
        the mail, so this is here to make that not count as a fix."""
        await accounts(session)
        async with serving() as c:
            r = await ask(c, MANAGER)
        assert r.status_code == 200
        assert len(outbox) == 1, "backgrounding the work stopped it happening"
        assert outbox.links[0].startswith("https://floor.test")

    async def test_a_background_failure_does_not_reach_the_caller(
            self, engine, session, monkeypatch):
        """The reply must not depend on how the work went, including when
        the work throws. A 500 on a real address and a 200 on an invented
        one is the same oracle by another route."""
        await accounts(session)

        async def explode(*a, **kw):
            raise RuntimeError("the relay fell over")

        monkeypatch.setattr(people.mail, "send", explode)
        async with serving() as c:
            r = await ask(c, MANAGER)
        assert r.status_code == 200
        assert r.json()["detail"] == RESET_SENT_TEXT


def inspect_source(name: str) -> str:
    import inspect

    from services.rigs import routes

    return inspect.getsource(getattr(routes, name))


# ------------------------------------------- the link is not caller-supplied


class TestTheLinkIsNotBuiltFromTheRequest:
    async def test_a_forged_host_header_does_not_reach_the_email(
            self, engine, session, outbox):
        """The service sits behind nginx and `Host` is a header the caller
        writes. A link derived from it would let somebody ask for a reset
        with a host of their choosing and have the floor mail the victim
        a link pointing at them.
        """
        await accounts(session)
        async with serving() as c:
            await c.post("/api/auth/reset/request",
                         json={"email": MANAGER},
                         headers={"host": "evil.example",
                                  "x-forwarded-host": "evil.example"})

        assert len(outbox) == 1
        assert "evil.example" not in outbox[0]["body"], (
            "a caller-supplied host reached the reset link")
        assert outbox.links[0].startswith("https://floor.test")
