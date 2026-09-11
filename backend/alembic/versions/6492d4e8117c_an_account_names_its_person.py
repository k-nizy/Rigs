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

Four things about the shape, and the last two were learned the hard way:

  - `operator_id` stays as a column, nullable and unconstrained. Rows
    minted under the old rule keep the value they had, so nothing is
    destroyed; nothing reads it any more.
  - No foreign key from `accounts` to `people`, for the reason the
    ledger gives: `people` is a different domain, the layering forbids
    the import, and a restore that re-creates people after accounts
    must not fail on an ordering the schema invented. Uniqueness is
    enforced - two accounts cannot be the same person - because that is
    the invariant "my day" depends on.
  - **The new rule is added NOT VALID.** A floor that has been running
    has operator accounts with a seat and no person, and a CHECK added
    the ordinary way is checked against every existing row and refuses
    the whole migration: "violated by some row". The first version of
    this file did exactly that, passed CI on an empty database, and
    could not be deployed to any floor with an operator on it. NOT VALID
    is the Postgres answer: enforced on every row written from now on,
    tolerant of the rows that predate it. Those rows are linked to
    their person by a manager - `mint_account link` - never re-minted
    (a re-mint is a new password) and never invented from the account's
    name (a person is created deliberately, and a picker-made "Mei
    Chen" would become a duplicate).
  - **The downgrade restores the old rule, also NOT VALID.** The first
    version dropped the new rule and did not put the old one back,
    reasoning that rows minted without a seat could not satisfy it.
    That left a database claiming this revision's parent while missing
    its constraint, so `downgrade -1` followed by `upgrade head` - the
    rollback DEPLOY.md documents - failed on the drop. NOT VALID lets
    the old rule come back over rows that cannot satisfy it, and the
    round-trip holds. CI now rehearses both: the upgrade over a
    populated floor, and the one-step rollback.

Revision ID: 6492d4e8117c
Revises: 231c498cc2f2
"""
from alembic import op
import sqlalchemy as sa


revision = '6492d4e8117c'
down_revision = '231c498cc2f2'
branch_labels = None
depends_on = None

OLD_RULE = ("(role = 'manager' AND operator_id IS NULL) OR "
            "(role = 'operator' AND operator_id IS NOT NULL)")
NEW_RULE = ("(role = 'manager' AND person_id IS NULL) OR "
            "(role = 'operator' AND person_id IS NOT NULL)")


def upgrade() -> None:
    op.add_column('accounts', sa.Column('person_id', sa.UUID(), nullable=True))
    op.create_index('uq_accounts_person', 'accounts', ['person_id'], unique=True,
                    postgresql_where=sa.text('person_id IS NOT NULL'))
    op.drop_constraint('ck_accounts_operator_id_matches_role', 'accounts', type_='check')
    # op.execute rather than op.create_check_constraint: the NOT VALID
    # clause is the whole point, and it is written out so nobody has to
    # know which Alembic version grew a keyword for it.
    op.execute(
        "ALTER TABLE accounts ADD CONSTRAINT ck_accounts_person_id_matches_role "
        f"CHECK ({NEW_RULE}) NOT VALID"
    )


def downgrade() -> None:
    op.drop_constraint('ck_accounts_person_id_matches_role', 'accounts', type_='check')
    op.execute(
        "ALTER TABLE accounts ADD CONSTRAINT ck_accounts_operator_id_matches_role "
        f"CHECK ({OLD_RULE}) NOT VALID"
    )
    op.drop_index('uq_accounts_person', table_name='accounts',
                  postgresql_where=sa.text('person_id IS NOT NULL'))
    op.drop_column('accounts', 'person_id')
