"""Reading and writing accounts and their sessions.

Two habits here are deliberate and worth not undoing.

**Lookups never leak existence through timing or through exceptions.**
`by_email` returns None for an unknown address; the route above it still
does the scrypt work, so a wrong email and a wrong password cost the same.

**A session is only ever resolved by fingerprint.** Nothing takes a raw
token and searches for it, because nothing stores one.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from core.base.repository import BaseRepository
from core.domains.accounts.model import Account, AccountSession
from core.domains.accounts.passwords import token_fingerprint


class AccountRepository(BaseRepository[Account]):
    model = Account

    async def by_email(self, email: str) -> Account | None:
        rows = await self.session.execute(
            select(Account).where(Account.email == normalise_email(email))
        )
        return rows.scalar_one_or_none()

    async def by_operator_id(self, operator_id: str) -> Account | None:
        rows = await self.session.execute(
            select(Account).where(Account.operator_id == operator_id)
        )
        return rows.scalar_one_or_none()

    async def count(self) -> int:
        """Whether this deployment has any people in it at all.

        `/api/health` reports it and the gate reads it: with no accounts
        configured the service behaves exactly as it did before there was
        such a thing, which is the same off-until-configured rule rig auth
        and the rate limiter already follow.
        """
        rows = await self.session.execute(select(Account.id))
        return len(rows.scalars().all())


class AccountSessionRepository(BaseRepository[AccountSession]):
    model = AccountSession

    async def open(self, account_id: uuid.UUID, token: str,
                   lifetime: timedelta, now: datetime | None = None) -> AccountSession:
        now = now or datetime.now(timezone.utc)
        return await self.add(AccountSession(
            account_id=account_id,
            token_fingerprint=token_fingerprint(token),
            issued_at=now,
            expires_at=now + lifetime,
        ))

    async def live(self, token: str, now: datetime | None = None
                   ) -> tuple[AccountSession, Account] | None:
        """The session this token names, with its account, or None.

        Expiry and revocation are conditions of the query rather than
        checks after it. A `WHERE` a caller cannot forget is worth more
        than an `if` a caller can.
        """
        now = now or datetime.now(timezone.utc)
        rows = await self.session.execute(
            select(AccountSession, Account)
            .join(Account, Account.id == AccountSession.account_id)
            .where(
                AccountSession.token_fingerprint == token_fingerprint(token),
                AccountSession.revoked_at.is_(None),
                AccountSession.expires_at > now,
                Account.disabled_at.is_(None),
            )
        )
        return rows.first()

    async def revoke(self, token: str, now: datetime | None = None) -> int:
        """Sign out. Returns how many rows it ended, so a caller can tell
        a real sign-out from a stale cookie without a second query."""
        result = await self.session.execute(
            update(AccountSession)
            .where(
                AccountSession.token_fingerprint == token_fingerprint(token),
                AccountSession.revoked_at.is_(None),
            )
            .values(revoked_at=now or datetime.now(timezone.utc))
        )
        return result.rowcount or 0

    async def revoke_all(self, account_id: uuid.UUID,
                         now: datetime | None = None) -> int:
        """Every session this person has anywhere. What a disabled account
        or a changed password has to do, or the old cookie outlives it."""
        result = await self.session.execute(
            update(AccountSession)
            .where(
                AccountSession.account_id == account_id,
                AccountSession.revoked_at.is_(None),
            )
            .values(revoked_at=now or datetime.now(timezone.utc))
        )
        return result.rowcount or 0

    async def purge_expired(self, now: datetime | None = None) -> int:
        """Rows that can no longer authenticate anybody. Housekeeping, not
        security - `live()` already refuses them."""
        result = await self.session.execute(
            delete(AccountSession).where(
                AccountSession.expires_at <= (now or datetime.now(timezone.utc))
            )
        )
        return result.rowcount or 0


def normalise_email(email: str) -> str:
    """One spelling of an address, everywhere it is written or read.

    Addresses are case-insensitive in practice, so storing what somebody
    typed means `Ruth@` and `ruth@` are two accounts that can each be
    created and neither reliably found.
    """
    return email.strip().lower()
