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

    # Who this account is, for operator accounts: the row in `people`.
    # The seat is deliberately not here. Where a person sits is the
    # roster the manager pushed that morning, read by every screen; an
    # account that also held a seat would be two answers to one question
    # that can disagree. No foreign key across domains - see the
    # migration - but unique, because "my day" depends on one account
    # being one person.
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # The chair this account was minted with, under the rule this
    # replaced. Kept so old rows are not destroyed and the downgrade is
    # clean; nothing reads it. New accounts never set it.
    operator_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # When a password was last chosen - by the person, or by whoever
    # minted the account. Null on an account that was *invited*: it has
    # a hash nobody knows, and stays "invited" until the link is
    # followed. The desk reads this to say who signs in yet.
    password_set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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

        # Two accounts claiming to be the same person would make "my
        # shift" ambiguous, and the resolution must not be whichever row
        # comes back first - the same tie `rig_at()` refuses to break.
        # Partial, so the many managers with no person do not collide
        # with each other on NULL.
        Index("uq_accounts_person", "person_id", unique=True,
              postgresql_where=text("person_id IS NOT NULL")),
        Index("uq_accounts_operator", "operator_id", unique=True,
              postgresql_where=text("operator_id IS NOT NULL")),

        CheckConstraint("role IN ('manager', 'operator')",
                        name="ck_accounts_role"),

        # The invariant that keeps /api/me/shift honest: an operator has
        # somebody to be, a manager does not.
        CheckConstraint(
            "(role = 'manager' AND person_id IS NULL) OR "
            "(role = 'operator' AND person_id IS NOT NULL)",
            name="ck_accounts_person_id_matches_role",
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


class PasswordReset(TimestampedBase):
    """One outstanding "I have forgotten my password", until it is used.

    The same shape as `AccountSession` above and for the same reasons: a
    fingerprint rather than the token, so a dump of this table is a list
    of expiry dates instead of a set of working ways in; and a row rather
    than a signed value, so it can be withdrawn.

    Withdrawal is not a nicety here. A reset token *is* the account for
    as long as it lives, so it has to be single-use and short-lived, and
    both of those are `used_at` and `expires_at` on a row.

    `requested_from` is the calling address, kept because a run of resets
    against one account is the shape of somebody working through a list,
    and the log line alone is not queryable.

    There is no column for the address that was *typed*. It would only
    ever equal the account's own - a row exists only when the address
    matched an account - so it would be a second copy of something the
    join already gives, and the requests that name nobody, which are the
    interesting ones, write no row at all. Those are in the log.
    """

    __tablename__ = "password_resets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )

    # SHA-256 hex, never the token. Same argument as the session table.
    token_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Set the moment it is spent. A reset link that works twice is a link
    # that still works after it has been read out of a mailbox once.
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    requested_from: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("uq_password_resets_fingerprint", "token_fingerprint", unique=True),
        Index("ix_password_resets_expires", "expires_at"),
        # Every live reset for one account, which is what issuing a new
        # one has to void and what a "who is being targeted" question asks.
        Index("ix_password_resets_account", "account_id"),
    )
