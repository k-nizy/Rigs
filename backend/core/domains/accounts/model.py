"""People. The other half of an authentication story that so far only has
machines in it.

A rig authenticates as a machine and the operator standing at it
authenticates nothing - that is settled, and nothing here changes it. What
had no answer at all was the desk: it is served as static files to anyone
who can reach the origin, and the one control that guards the push is a
shared secret which cannot say who used it, cannot be revoked for one
person, and cannot tell a manager from an operator.

So: `accounts`, and `account_sessions`.

**Not `sessions`.** That table already exists and means something else
entirely - one operator at one rig, turn start to turn end, derived from
the ledger. Two unrelated ideas under one name in a schema this size is a
bug waiting for somebody in a hurry.

**Two roles, and the database enforces which fields go with which.** An
operator account names the roster operator it belongs to (`op-a2`) or
`/api/me/shift` has nothing to look itself up by; a manager names none,
because a manager is not on the sheet. Leaving that to application code
means the first row that breaks it is found by a screen showing the wrong
person's day.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, Index, String, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import TimestampedBase

MANAGER = "manager"
OPERATOR = "operator"
ROLES = (MANAGER, OPERATOR)


class Account(TimestampedBase):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Stored lowercased by the repository, so two people cannot register
    # the same address in different capitals and then both be refused by
    # a lookup that picks one.
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    # Which operator on the sheet this account is, for operator accounts.
    # `op-a2`, the id the engine mints and the payload carries.
    operator_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Disabled, never deleted. Somebody leaving is not the same as them
    # never having been here, and a later audit row naming who pushed a
    # schedule should still be able to find the name.
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("uq_accounts_email", "email", unique=True),

        # Two accounts claiming to be op-a2 would make "my shift"
        # ambiguous, and the resolution must not be whichever row comes
        # back first - that is the same tie `rig_at()` refuses to break.
        # Partial, so the many managers with no operator_id do not
        # collide with each other on NULL.
        Index("uq_accounts_operator", "operator_id", unique=True,
              postgresql_where=text("operator_id IS NOT NULL")),

        CheckConstraint("role IN ('manager', 'operator')",
                        name="ck_accounts_role"),

        # The invariant that keeps /api/me/shift honest: an operator has
        # somebody on the sheet to be, a manager does not.
        CheckConstraint(
            "(role = 'manager' AND operator_id IS NULL) OR "
            "(role = 'operator' AND operator_id IS NOT NULL)",
            name="ck_accounts_operator_id_matches_role",
        ),
    )


class AccountSession(TimestampedBase):
    """One signed-in browser, until it expires or is signed out.

    The row holds a *fingerprint* of the session token, never the token.
    A stolen dump of this table is then a list of expiry dates rather
    than a set of working credentials.

    It is a row and not a JWT for one reason: revocation. A signed token
    cannot be withdrawn before it expires, and being able to end somebody's
    access on the day they leave is the entire argument for having people
    here instead of one shared secret.
    """

    __tablename__ = "account_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )

    # SHA-256 hex of the token. Fast on purpose - the token is 256 bits of
    # randomness, so there is nothing to brute force, and this is read on
    # every authenticated request. See passwords.token_fingerprint.
    token_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("uq_account_sessions_fingerprint", "token_fingerprint", unique=True),
        # The sweep that clears expired rows, and nothing else reads this.
        Index("ix_account_sessions_expires", "expires_at"),
    )
