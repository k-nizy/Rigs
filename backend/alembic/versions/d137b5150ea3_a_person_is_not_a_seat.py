"""a person is not a seat

One table, `people`, and one partial unique index on its email. Nothing
else changes and nothing reads it yet - that is the point of landing it
alone. The episode row, the desk's picker and the roster each learn
about it in a later migration, so each of those can come back down
without taking this with it.

The id is random and minted once. `op-a4` names a chair; this names the
person in it, and keeps naming them in every other chair they ever work.

The index is partial on `email IS NOT NULL`, same shape as
`uq_accounts_operator`: an address is optional, and NULL is not a value
sixteen operators are fighting over. It does not exclude disabled rows,
and must not - somebody who comes back is the same person re-enabled,
not a second row with the same address.

New and empty, so the index is built inline. The README's warning about
`CREATE INDEX` locking `rig_events` against twelve writing rigs does not
apply to a table that has never had a row in it.

Revision ID: d137b5150ea3
Revises: 73909a57ffda
"""
from alembic import op
import sqlalchemy as sa


revision = 'd137b5150ea3'
down_revision = '73909a57ffda'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('people',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('uq_people_email', 'people', ['email'], unique=True,
                    postgresql_where=sa.text('email IS NOT NULL'))


def downgrade() -> None:
    op.drop_index('uq_people_email', table_name='people',
                  postgresql_where=sa.text('email IS NOT NULL'))
    op.drop_table('people')
