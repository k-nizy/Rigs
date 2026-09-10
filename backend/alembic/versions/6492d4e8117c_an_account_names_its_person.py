"""an account names its person

`accounts.person_id`, and the rule that an operator account must name a
seat is retired.

An operator account used to hold `op-a2` - a chair, set once when the
account was minted - and "how did I do" was answered by asking what
that chair did. On a cover day that showed one person another's
numbers as their own, on their own screen: the seat-is-not-a-person
failure arriving through the door marked authentication.

The seat is never on the account now. Where a person sits is the roster
the manager pushed that morning, and every screen reads that. The
account says who; the push says where.

Three things about the shape:

  - `operator_id` stays as a column, nullable and unconstrained. Rows
    minted under the old rule keep the value they had, so nothing is
    destroyed and the downgrade is clean; nothing reads it any more.
  - No foreign key from `accounts` to `people`, for the reason the
    ledger gives: `people` is a different domain, the layering forbids
    the import, and a restore that re-creates people after accounts
    must not fail on an ordering the schema invented. Uniqueness is
    enforced - two accounts cannot be the same person - because that is
    the invariant "my day" depends on, the same tie `rig_at()` refuses
    to break.
  - The role check is the same shape as before, pointed at the right
    thing: an operator has somebody to be, a manager does not.

Revision ID: 6492d4e8117c
Revises: 231c498cc2f2
"""
from alembic import op
import sqlalchemy as sa


revision = '6492d4e8117c'
down_revision = '231c498cc2f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('accounts', sa.Column('person_id', sa.UUID(), nullable=True))
    op.create_index('uq_accounts_person', 'accounts', ['person_id'], unique=True,
                    postgresql_where=sa.text('person_id IS NOT NULL'))
    op.drop_constraint('ck_accounts_operator_id_matches_role', 'accounts', type_='check')
    op.create_check_constraint(
        'ck_accounts_person_id_matches_role', 'accounts',
        "(role = 'manager' AND person_id IS NULL) OR "
        "(role = 'operator' AND person_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint('ck_accounts_person_id_matches_role', 'accounts', type_='check')
    # Rows minted after this migration have no seat to restore, so the old
    # rule cannot be re-imposed truthfully on them. Restored without the
    # role check rather than with one that would refuse the downgrade.
    op.drop_index('uq_accounts_person', table_name='accounts',
                  postgresql_where=sa.text('person_id IS NOT NULL'))
    op.drop_column('accounts', 'person_id')
