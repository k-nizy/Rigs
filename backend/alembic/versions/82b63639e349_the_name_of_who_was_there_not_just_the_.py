"""the name of who was there, not just the seat

The reason an operator has an identity at all is so that any take can be
traced to the person who recorded it, and until now the ledger could not
answer that on its own.

`operator_id` is a seat, not a person. `rotation-engine.js` builds it as
"op-" + group + slot, so `op-a4` is whoever the schedule put in group A's
fourth slot that day - a different human on a cover day. Nothing stored a
name, so "who recorded this" meant joining the episode back to the
schedule payload pushed for that rig, date and shift. That join is
correct but nobody had written it, and there is a plausible wrong version
beside it: `accounts.operator_id` has the same name and shape, and
joining through it returns whoever holds the seat *now*.

Every column is nullable and stays that way. Two reasons, and the second
is the load-bearing one:

  - a rig on standby files a shift check with no operator at all;
  - every event already sitting in a rig's journal was written before
    this field existed. A rig does not retry a refused batch - it drops
    it from the outbox and forgets it from the journal, because a batch
    the server refuses would be refused again on every boot. Demanding
    this field would destroy that work rather than delay it.

`rig_events` gets the column alongside the five fact tables, because the
ledger is the system and every fact is derived from it: a name that is
not in `rig_events` would not survive a replay.

Revision ID: 82b63639e349
Revises: 7d8934168889
"""
from alembic import op
import sqlalchemy as sa


revision = '82b63639e349'
down_revision = '7d8934168889'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('episodes', sa.Column('operator_name', sa.String(length=128), nullable=True))
    op.add_column('rig_downtime_events', sa.Column('operator_name', sa.String(length=128), nullable=True))
    op.add_column('rig_events', sa.Column('operator_name', sa.String(length=128), nullable=True))
    op.add_column('rig_productivity_blocks', sa.Column('operator_name', sa.String(length=128), nullable=True))
    op.add_column('rig_shift_checks', sa.Column('operator_name', sa.String(length=128), nullable=True))
    op.add_column('sessions', sa.Column('operator_name', sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column('sessions', 'operator_name')
    op.drop_column('rig_shift_checks', 'operator_name')
    op.drop_column('rig_productivity_blocks', 'operator_name')
    op.drop_column('rig_events', 'operator_name')
    op.drop_column('rig_downtime_events', 'operator_name')
    op.drop_column('episodes', 'operator_name')
