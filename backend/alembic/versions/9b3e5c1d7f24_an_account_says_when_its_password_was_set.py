"""an account says when its password was set

`accounts.password_set_at`: when a password was last chosen, by the
person or by whoever minted the account. Null on an account that was
*invited* - minted with a hash of a secret nobody is told, behind a link
to set a real one - and set the moment the link is followed. The desk
reads it to say who signs in yet: none, invited, active.

Backfilled with now() for every row that exists, because every account
that exists was minted with a password. Left null, the desk would offer
to invite people who already sign in.

**Filled as the column is added, not by an UPDATE afterwards.** The
first version of this file did `UPDATE accounts SET password_set_at =
now()`, and the rehearsal refused it: Postgres checks a NOT VALID
constraint on every row an UPDATE touches, and a floor upgraded from
the seat-only days holds an operator row with no person, which
`ck_accounts_person_id_matches_role` refuses. Adding the column with a
default fills every existing row without being an UPDATE, so nothing is
checked; the default is then dropped so new rows - invited accounts -
start null, as the model says.

Revision ID: 9b3e5c1d7f24
Revises: 6492d4e8117c
"""
from alembic import op
import sqlalchemy as sa


revision = '9b3e5c1d7f24'
down_revision = '6492d4e8117c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('accounts', sa.Column('password_set_at', sa.DateTime(timezone=True),
                                        nullable=True, server_default=sa.text('now()')))
    op.alter_column('accounts', 'password_set_at', server_default=None)


def downgrade() -> None:
    op.drop_column('accounts', 'password_set_at')
