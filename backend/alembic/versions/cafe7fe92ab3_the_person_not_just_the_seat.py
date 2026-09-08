"""the person, not just the seat

One column, `person_id`, on the ledger and on all five fact tables it
feeds - the same six tables `operator_name` went onto, for the same
reason: the ledger is the system and every fact is derived from it, so a
person that is not in `rig_events` would not survive a replay.

It is the id from `people`, minted once when a manager adds somebody and
carried into every seat they ever work. `operator_id` is a seat and
`operator_name` is a string; neither gives "who recorded this" one answer
across months. This does.

Nullable, and it stays nullable. No rig sends it until the release after
this one - the server learns a shape first, because a rig drops a refused
batch rather than retrying it - and every event already queued was
written without it.

**No foreign key, deliberately.** A restore re-POSTs the ledger through
ingest, and `people` is outside the ledger the way schedules are. A key
would make a restore order-dependent, and a person row that was not there
yet would turn events into refusals - which on this floor means lost, not
delayed. The id is stored opaque, the way `operator_id` already is.

Indexed on `episodes` only: "everything this person recorded" is the
question the column exists to answer. The other four tables carry it so a
replay keeps it; nothing groups by it there yet.

Revision ID: cafe7fe92ab3
Revises: d137b5150ea3
"""
from alembic import op
import sqlalchemy as sa


revision = 'cafe7fe92ab3'
down_revision = 'd137b5150ea3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('episodes', sa.Column('person_id', sa.UUID(), nullable=True))
    op.add_column('rig_downtime_events', sa.Column('person_id', sa.UUID(), nullable=True))
    op.add_column('rig_events', sa.Column('person_id', sa.UUID(), nullable=True))
    op.add_column('rig_productivity_blocks', sa.Column('person_id', sa.UUID(), nullable=True))
    op.add_column('rig_shift_checks', sa.Column('person_id', sa.UUID(), nullable=True))
    op.add_column('sessions', sa.Column('person_id', sa.UUID(), nullable=True))
    op.create_index('ix_episodes_person', 'episodes', ['person_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_episodes_person', table_name='episodes')
    op.drop_column('sessions', 'person_id')
    op.drop_column('rig_shift_checks', 'person_id')
    op.drop_column('rig_productivity_blocks', 'person_id')
    op.drop_column('rig_events', 'person_id')
    op.drop_column('rig_downtime_events', 'person_id')
    op.drop_column('episodes', 'person_id')
