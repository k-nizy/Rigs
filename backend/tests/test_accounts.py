"""People: how a password is stored, and what the database refuses.

Phase 1 of authentication. Nothing here touches a route - there are no
auth routes yet - so this is the storage layer on its own: the hash, the
two tables, and the invariants that are the database's job rather than
the application's.

The rig's own auth is deliberately untouched by all of it. That is
asserted in test_auth.py, which still passes unmodified, and it is the
regression that matters most in this whole piece of work.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from core.domains.accounts.model import Account, AccountSession
from core.domains.accounts.passwords import (
    SCRYPT_N, SCRYPT_P, SCRYPT_R, hash_password, needs_rehash,
    token_fingerprint, verify_password,
)
from core.domains.accounts.repository import (
    AccountRepository, AccountSessionRepository, normalise_email,
)


def manager(email="r.osei@verlet.co", name="Ruth Osei", password="a-real-password"):
    return Account(email=normalise_email(email), name=name, role="manager",
                   password_hash=hash_password(password))


def operator(email="m.chen@verlet.co", name="Mei Chen", operator_id="op-a2",
             password="a-real-password"):
    return Account(email=normalise_email(email), name=name, role="operator",
                   operator_id=operator_id, password_hash=hash_password(password))


# ------------------------------------------------------------ the hash


class TestThePassword:
    def test_the_right_password_verifies_and_a_wrong_one_does_not(self):
        stored = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", stored)
        assert not verify_password("correct horse battery stapl", stored)
        assert not verify_password("Correct horse battery staple", stored)
        assert not verify_password("", stored)

    def test_the_hash_is_salted(self):
        """Two people with the same password must not have the same row,
        or one cracked hash cracks every account that shares it."""
        assert hash_password("same") != hash_password("same")

    def test_the_plaintext_is_nowhere_in_the_stored_string(self):
        assert "hunter2" not in hash_password("hunter2")

    def test_the_hash_says_what_made_it(self):
        algo, params, salt, key = hash_password("x").split("$")
        assert algo == "scrypt"
        assert params == "n={},r={},p={}".format(SCRYPT_N, SCRYPT_R, SCRYPT_P)
        assert salt and key

    def test_a_hash_made_at_an_older_cost_still_verifies(self):
        """The reason the parameters travel inside the hash.

        Raising the work factor must not lock out every existing account.
        This builds a real hash at a weaker setting - not a rewritten
        string - and asks today's code to verify it.
        """
        salt = secrets.token_bytes(16)
        weak_n, weak_r, weak_p = 2 ** 12, 8, 1
        key = hashlib.scrypt(b"old-password", salt=salt, n=weak_n, r=weak_r,
                             p=weak_p, dklen=32,
                             maxmem=128 * weak_n * weak_r * weak_p + (1 << 20))
        old = "scrypt$n={},r={},p={}${}${}".format(
            weak_n, weak_r, weak_p,
            base64.b64encode(salt).decode(), base64.b64encode(key).decode())

        assert verify_password("old-password", old), "an old hash stopped verifying"
        assert not verify_password("wrong", old)
        assert needs_rehash(old), "an old hash should be flagged for upgrade"
        assert not needs_rehash(hash_password("new-password"))

    def test_a_malformed_hash_is_false_rather_than_an_exception(self):
        """The login route must answer the same way for a broken row as
        for a wrong password. A 500 tells a prober the account exists."""
        for junk in ["", "not-a-hash", "scrypt$$$", "scrypt$n=x,r=8,p=1$a$b",
                     "bcrypt$n=1,r=8,p=1$YQ==$Yg==", "a$b$c", "$$$$$$"]:
            assert verify_password("x", junk) is False

    def test_an_unknown_algorithm_is_refused_and_flagged(self):
        assert verify_password("x", "argon2id$n=1,r=8,p=1$YQ==$Yg==") is False
        assert needs_rehash("argon2id$n=1,r=8,p=1$YQ==$Yg==")

    def test_the_session_fingerprint_is_stable_and_not_the_token(self):
        token = "a-session-token"
        assert token_fingerprint(token) == token_fingerprint(token)
        assert token_fingerprint(token) != token_fingerprint(token + "x")
        assert token not in token_fingerprint(token)
        assert len(token_fingerprint(token)) == 64


# ------------------------------------------------- what the schema refuses


class TestTheDatabaseHoldsTheLine:
    async def test_two_accounts_cannot_share_an_email(self, session):
        session.add(manager())
        await session.commit()
        session.add(manager(name="Someone Else"))
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_two_accounts_cannot_be_the_same_operator(self, session):
        """The tie `rig_at()` refuses to break, in another place: two rows
        claiming op-a2 makes "my shift" resolve to whichever came back
        first."""
        session.add(operator())
        await session.commit()
        session.add(operator(email="other@verlet.co", name="Not Mei"))
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_many_managers_may_share_having_no_operator_id(self, session):
        """The unique index is partial for exactly this reason - NULL is
        not a value two managers are fighting over."""
        session.add_all([manager(), manager(email="b@verlet.co", name="B")])
        await session.commit()
        assert await AccountRepository(session).count() == 2

    async def test_an_operator_without_an_operator_id_is_refused(self, session):
        session.add(Account(email="x@verlet.co", name="X", role="operator",
                            password_hash=hash_password("pw")))
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.commit()

    async def test_a_manager_with_an_operator_id_is_refused(self, session):
        session.add(Account(email="x@verlet.co", name="X", role="manager",
                            operator_id="op-a2", password_hash=hash_password("pw")))
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.commit()

    async def test_a_role_that_is_not_one_of_the_two_is_refused(self, session):
        session.add(Account(email="x@verlet.co", name="X", role="admin",
                            password_hash=hash_password("pw")))
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.commit()


# ---------------------------------------------------------- the repository


class TestFindingAnAccount:
    async def test_email_is_matched_regardless_of_capitals(self, session):
        session.add(manager(email="R.Osei@Verlet.co"))
        await session.commit()
        found = await AccountRepository(session).by_email("  r.OSEI@verlet.CO ")
        assert found is not None and found.name == "Ruth Osei"

    async def test_an_unknown_email_is_none_rather_than_an_error(self, session):
        assert await AccountRepository(session).by_email("nobody@verlet.co") is None

    async def test_an_account_is_found_by_the_operator_it_is(self, session):
        session.add(operator())
        await session.commit()
        found = await AccountRepository(session).by_operator_id("op-a2")
        assert found is not None and found.email == "m.chen@verlet.co"

    async def test_no_accounts_means_person_auth_is_off(self, session):
        """The off-until-configured rule rig auth already follows."""
        assert await AccountRepository(session).count() == 0


# ------------------------------------------------------------- sessions


class TestASignedInBrowser:
    async def _account(self, session):
        a = manager()
        session.add(a)
        await session.commit()
        return a

    async def test_a_fresh_session_resolves_to_its_account(self, session):
        account = await self._account(session)
        sessions = AccountSessionRepository(session)
        await sessions.open(account.id, "the-token", timedelta(hours=12))
        await session.commit()

        found = await sessions.live("the-token")
        assert found is not None
        _, who = found
        assert who.email == "r.osei@verlet.co" and who.role == "manager"

    async def test_the_raw_token_is_never_stored(self, session):
        """A dump of this table must be a list of expiry dates, not a set
        of working credentials."""
        account = await self._account(session)
        await AccountSessionRepository(session).open(
            account.id, "the-token", timedelta(hours=12))
        await session.commit()

        row = (await session.execute(select(AccountSession))).scalars().one()
        assert row.token_fingerprint != "the-token"
        assert row.token_fingerprint == token_fingerprint("the-token")

    async def test_a_token_nobody_issued_resolves_to_nobody(self, session):
        await self._account(session)
        assert await AccountSessionRepository(session).live("invented") is None

    async def test_an_expired_session_is_refused(self, session):
        account = await self._account(session)
        sessions = AccountSessionRepository(session)
        long_ago = datetime.now(timezone.utc) - timedelta(days=2)
        await sessions.open(account.id, "stale", timedelta(hours=12), now=long_ago)
        await session.commit()
        assert await sessions.live("stale") is None

    async def test_signing_out_ends_it_immediately(self, session):
        account = await self._account(session)
        sessions = AccountSessionRepository(session)
        await sessions.open(account.id, "live-one", timedelta(hours=12))
        await session.commit()
        assert await sessions.live("live-one") is not None

        assert await sessions.revoke("live-one") == 1
        await session.commit()
        assert await sessions.live("live-one") is None

    async def test_signing_out_twice_reports_nothing_the_second_time(self, session):
        account = await self._account(session)
        sessions = AccountSessionRepository(session)
        await sessions.open(account.id, "t", timedelta(hours=12))
        await session.commit()
        assert await sessions.revoke("t") == 1
        assert await sessions.revoke("t") == 0

    async def test_disabling_an_account_kills_every_session_it_has(self, session):
        """What `mint_account disable` relies on. A person who has left
        keeps working until their cookie expires, otherwise."""
        account = await self._account(session)
        sessions = AccountSessionRepository(session)
        for token in ("desk", "phone", "laptop"):
            await sessions.open(account.id, token, timedelta(hours=12))
        await session.commit()

        assert await sessions.revoke_all(account.id) == 3
        await session.commit()
        for token in ("desk", "phone", "laptop"):
            assert await sessions.live(token) is None

    async def test_a_disabled_account_cannot_be_resolved_even_with_a_live_token(
            self, session):
        """Belt and braces: revoke_all is the mechanism, but a session
        that survived somehow must still not resolve to a disabled
        person."""
        account = await self._account(session)
        sessions = AccountSessionRepository(session)
        await sessions.open(account.id, "survivor", timedelta(hours=12))
        await session.commit()

        account.disabled_at = datetime.now(timezone.utc)
        await session.commit()
        assert await sessions.live("survivor") is None

    async def test_two_browsers_are_two_sessions(self, session):
        """Signing out at the desk must not sign the same person out on
        their phone."""
        account = await self._account(session)
        sessions = AccountSessionRepository(session)
        await sessions.open(account.id, "at-the-desk", timedelta(hours=12))
        await sessions.open(account.id, "on-a-phone", timedelta(hours=12))
        await session.commit()

        await sessions.revoke("at-the-desk")
        await session.commit()
        assert await sessions.live("at-the-desk") is None
        assert await sessions.live("on-a-phone") is not None

    async def test_two_sessions_cannot_share_a_fingerprint(self, session):
        account = await self._account(session)
        now = datetime.now(timezone.utc)
        session.add_all([
            AccountSession(account_id=account.id,
                           token_fingerprint=token_fingerprint("same"),
                           issued_at=now, expires_at=now + timedelta(hours=1))
            for _ in range(2)
        ])
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_deleting_an_account_takes_its_sessions_with_it(self, session):
        account = await self._account(session)
        sessions = AccountSessionRepository(session)
        await sessions.open(account.id, "t", timedelta(hours=12))
        await session.commit()

        await session.delete(account)
        await session.commit()
        assert await sessions.live("t") is None

    async def test_purging_clears_expired_rows_and_leaves_live_ones(self, session):
        account = await self._account(session)
        sessions = AccountSessionRepository(session)
        long_ago = datetime.now(timezone.utc) - timedelta(days=2)
        await sessions.open(account.id, "old", timedelta(hours=1), now=long_ago)
        await sessions.open(account.id, "new", timedelta(hours=12))
        await session.commit()

        assert await sessions.purge_expired() == 1
        await session.commit()
        assert await sessions.live("new") is not None


# ------------------------------------------------ housekeeping that runs


class TestExpiredSessionsAreActuallyCleared:
    """`purge_expired` was written, tested, and called by nothing. A
    tested function with no caller is the kind that rots - it keeps
    passing while the rows it was meant to remove pile up."""

    async def test_the_sweep_worker_clears_them(self, engine, session):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from workers.sweep_floor import clear_dead_sessions

        account = manager()
        session.add(account)
        await session.commit()

        sessions = AccountSessionRepository(session)
        long_ago = datetime.now(timezone.utc) - timedelta(days=2)
        await sessions.open(account.id, "dead", timedelta(hours=1), now=long_ago)
        await sessions.open(account.id, "alive", timedelta(hours=12))
        await session.commit()

        maker = async_sessionmaker(engine, expire_on_commit=False)
        assert await clear_dead_sessions(maker) == 1

        assert await sessions.live("alive") is not None, "it took a live one"

    async def test_a_failure_to_tidy_up_does_not_raise(self, engine, session):
        """It rides on the worker that watches the floor. Housekeeping
        must not be able to stop absence detection."""
        import logging

        from workers.sweep_floor import clear_dead_sessions

        class Broken:
            async def __aenter__(self): raise RuntimeError("database gone")
            async def __aexit__(self, *a): return False

        quiet = logging.getLogger("test.quiet")
        quiet.disabled = True
        assert await clear_dead_sessions(lambda: Broken(), log_to=quiet) == 0
