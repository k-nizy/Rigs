"""Slowing down a password list, without handing anybody a way to lock
a manager out of the desk.

The rate limiter caps how *fast* one address may call; this caps how
*many* times it may guess at one account. Ten a minute is fourteen
thousand a day, which is longer than a list of common passwords, so the
limiter alone protects a strong password and not a weak one.

Two properties matter more than the counting, and both are here:

**An attacker locks themselves out, not the manager.** Counting per
(account, address) rather than per account is the whole reason this
feature is safe to add. A shift with no pushed schedule leaves twelve
rigs in Standby, so locking the desk is a better attack than reading it.

**An unknown address behaves exactly like a real one.** If only real
accounts locked, the difference would say which addresses exist - the
same leak the dummy hash closes, reopened from another side.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from core.domains.accounts.model import Account
from core.domains.accounts.passwords import hash_password
from core.infrastructure.config import Settings, get_settings
from services.rigs.lockout import Lockout
from services.rigs.people import reset_login_limiter

PASSWORD = "a-real-password-12"
MANAGER = "r.osei@verlet.co"


@pytest.fixture(autouse=True)
def _fresh():
    reset_login_limiter()
    yield
    reset_login_limiter()


class FakeClock:
    """Time that only moves when a test says so."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def tick(self, secs: float) -> None:
        self.t += secs


# ------------------------------------------------------- the mechanism


class TestTheCounter:
    def _lockout(self):
        clock = FakeClock()
        return Lockout(after=3, first_wait=60, max_wait=900, clock=clock), clock

    def test_it_allows_the_first_few_and_then_makes_you_wait(self):
        """`after=3` means three attempts are allowed. The third failure
        is what starts the wait, so the fourth attempt is the one
        refused."""
        lk, _ = self._lockout()
        key = ("a@b.co", "10.0.0.9")

        for _ in range(2):
            assert lk.check(key) == 0.0
            assert lk.failed(key) == 0.0, "should not be waiting yet"

        assert lk.failed(key) == 60.0, "the third failure starts the wait"
        assert lk.check(key) == 60.0

    def test_the_wait_doubles_and_then_stops(self):
        lk, _ = self._lockout()
        key = ("a@b.co", "10.0.0.9")
        for _ in range(2):
            lk.failed(key)

        assert [lk.failed(key) for _ in range(6)] == [60, 120, 240, 480, 900, 900]

    def test_the_wait_runs_out_on_its_own(self):
        """Nothing here needs an administrator to undo it. At three in
        the morning on a floor there is not one."""
        lk, clock = self._lockout()
        key = ("a@b.co", "10.0.0.9")
        for _ in range(3):
            lk.failed(key)

        assert lk.check(key) == 60.0
        clock.tick(59)
        assert lk.check(key) == pytest.approx(1.0)
        clock.tick(2)
        assert lk.check(key) == 0.0

    def test_a_right_password_ends_it_at_once(self):
        lk, _ = self._lockout()
        key = ("a@b.co", "10.0.0.9")
        for _ in range(5):
            lk.failed(key)
        assert lk.check(key) > 0

        lk.succeeded(key)
        assert lk.check(key) == 0.0
        assert lk.failed(key) == 0.0, "the count should have started over"

    def test_two_addresses_guessing_one_account_do_not_share_a_count(self):
        """The property that makes this safe to ship. An attacker locks
        out themselves; the manager at the desk is on another address."""
        lk, _ = self._lockout()
        attacker = ("r.osei@verlet.co", "203.0.113.7")
        the_desk = ("r.osei@verlet.co", "10.0.0.2")

        for _ in range(20):
            lk.failed(attacker)

        assert lk.check(attacker) > 0
        assert lk.check(the_desk) == 0.0, \
            "an attacker elsewhere locked the desk out of its own account"

    def test_one_address_guessing_two_accounts_does_not_share_a_count(self):
        lk, _ = self._lockout()
        for _ in range(20):
            lk.failed(("a@b.co", "203.0.113.7"))
        assert lk.check(("c@d.co", "203.0.113.7")) == 0.0

    def test_it_can_be_turned_off(self):
        lk = Lockout(after=0)
        key = ("a@b.co", "10.0.0.9")
        for _ in range(50):
            assert lk.failed(key) == 0.0
        assert lk.check(key) == 0.0

    def test_it_will_not_grow_without_bound(self):
        """An unauthenticated caller must not be able to make us allocate
        for ever by inventing addresses."""
        from services.rigs.lockout import MAX_TRACKED

        lk = Lockout(after=3, clock=FakeClock())
        for i in range(MAX_TRACKED + 200):
            lk.failed((f"{i}@b.co", "10.0.0.9"))
        assert lk.tracked() <= MAX_TRACKED


# ------------------------------------------------------- over the route


@asynccontextmanager
async def serving(caller="10.0.0.2", **overrides):
    """A client that appears to call from `caller`.

    The address matters: the lockout counts per (account, address), and
    a test that cannot put two clients at two addresses cannot tell that
    design from the dangerous one.
    """
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


async def a_manager(session):
    session.add(Account(email=MANAGER, name="Ruth Osei", role="manager",
                        password_hash=hash_password(PASSWORD)))
    await session.commit()


async def guess(client, times, email=MANAGER, password="wrong"):
    return [
        (await client.post("/api/auth/login",
                           json={"email": email, "password": password})).status_code
        for _ in range(times)
    ]


class TestOverTheRoute:
    async def test_guessing_is_refused_once_the_run_is_long_enough(
            self, engine, session):
        await a_manager(session)
        async with serving(login_lockout_after=3,
                           login_rate_limit_per_min=0) as c:
            codes = await guess(c, 6)
        assert codes[:3] == [401, 401, 401]
        assert codes[3:] == [429, 429, 429], codes

    async def test_it_says_how_long_to_wait(self, engine, session):
        await a_manager(session)
        async with serving(login_lockout_after=1,
                           login_rate_limit_per_min=0) as c:
            await guess(c, 2)
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": "wrong"})
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1
        assert "failed" in r.json()["detail"]

    async def test_the_right_password_during_a_wait_is_still_refused(
            self, engine, session):
        """Otherwise the lockout is only a lockout until the attacker
        guesses right, which is the moment it needed to hold."""
        await a_manager(session)
        async with serving(login_lockout_after=2,
                           login_rate_limit_per_min=0) as c:
            await guess(c, 3)
            r = await c.post("/api/auth/login",
                             json={"email": MANAGER, "password": PASSWORD})
        assert r.status_code == 429

    async def test_a_right_password_before_the_run_is_long_enough_clears_it(
            self, engine, session):
        await a_manager(session)
        async with serving(login_lockout_after=5,
                           login_rate_limit_per_min=0) as c:
            await guess(c, 4)
            good = await c.post("/api/auth/login",
                                json={"email": MANAGER, "password": PASSWORD})
            assert good.status_code == 200

            # the count started over, so four more are still allowed
            codes = await guess(c, 4)
        assert codes == [401, 401, 401, 401], codes

    async def test_an_address_nobody_has_locks_out_the_same_way(
            self, engine, session):
        """A real account and an invented one must be indistinguishable.
        If only real ones locked, the lockout would answer 'does this
        person work here' for anybody who asked."""
        await a_manager(session)
        async with serving(login_lockout_after=3,
                           login_rate_limit_per_min=0) as c:
            real = await guess(c, 5, email=MANAGER)
            fake = await guess(c, 5, email="nobody-at-all@verlet.co")
        assert real == fake == [401, 401, 401, 429, 429]

    async def test_it_is_off_when_configured_off(self, engine, session):
        await a_manager(session)
        async with serving(login_lockout_after=0,
                           login_rate_limit_per_min=0) as c:
            codes = await guess(c, 12)
        assert set(codes) == {401}

    async def test_health_reports_it(self, engine, session):
        async with serving(login_lockout_after=5,
                           login_lockout_max_wait_secs=900) as c:
            assert (await c.get("/api/health")).json()["loginLockout"] \
                == "after 5, up to 900s"
        async with serving(login_lockout_after=0) as c:
            assert (await c.get("/api/health")).json()["loginLockout"] == "off"

    async def test_a_rig_is_not_affected_by_any_of_it(self, engine, session):
        """The rig authenticates as a machine and never sees this path."""
        await a_manager(session)
        async with serving(login_lockout_after=1, login_rate_limit_per_min=0,
                           rig_tokens={"RIG-03": "a-token"}) as c:
            await guess(c, 5)
            r = await c.get("/api/rigs/RIG-03/cursor",
                            headers={"Authorization": "Bearer a-token"})
        assert r.status_code == 200

    async def test_an_attacker_cannot_lock_the_desk_out_of_its_own_account(
            self, engine, session):
        """The property the whole design turns on, tested where it lives.

        `lockout_key` is what makes the count per (account, address). A
        unit test that builds a Lockout and hands it tuples cannot see
        that function at all - it passes just as happily against the
        textbook version that locks the account, which lets anybody who
        knows a manager's email deny them the desk. This one goes through
        the route from two different addresses, so it fails if the key
        ever stops carrying the caller.
        """
        await a_manager(session)

        async with serving(caller="203.0.113.7", login_lockout_after=2,
                           login_rate_limit_per_min=0) as attacker:
            assert await guess(attacker, 4) == [401, 401, 429, 429]

        async with serving(caller="10.0.0.2", login_lockout_after=2,
                           login_rate_limit_per_min=0) as the_desk:
            r = await the_desk.post("/api/auth/login",
                                    json={"email": MANAGER, "password": PASSWORD})

        assert r.status_code == 200, (
            "a manager at the desk was refused because somebody elsewhere "
            "had been guessing at their account - the lockout is lockable")
