"""sweep indexes

Two queries the floor sweep runs every fifteen seconds, forever, with
nothing to support them:

  max(at) per rig          - "when did this rig last say anything?", asked
                             once per rig per sweep against the ledger,
                             which is the one table that only ever grows
  blocks by ended_at       - the overrun check reads the last day of them

Created CONCURRENTLY. A plain CREATE INDEX takes a lock that blocks
writes for as long as it runs, and on rig_events that means twelve rigs
cannot file events for the duration - on a floor mid-shift, that is the
outage this migration was supposed to prevent. Concurrent creation is
slower and needs two table scans, which is the right trade for a table
nobody may stop writing to.

Concurrent index builds cannot run inside a transaction, so this
migration takes autocommit. If it fails part-way, Postgres leaves an
INVALID index behind: drop it and run again rather than assuming the
index is usable.

Revision ID: 5dedb9132a9b
Revises: 2ad33b63b7b7
"""
from alembic import op

revision = '5dedb9132a9b'
down_revision = '2ad33b63b7b7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            'ix_rig_events_rig_at', 'rig_events', ['rig_id', 'at'],
            unique=False, postgresql_concurrently=True, if_not_exists=True,
        )
        op.create_index(
            'ix_blocks_ended_at', 'rig_productivity_blocks', ['ended_at'],
            unique=False, postgresql_concurrently=True, if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            'ix_blocks_ended_at', table_name='rig_productivity_blocks',
            postgresql_concurrently=True, if_exists=True,
        )
        op.drop_index(
            'ix_rig_events_rig_at', table_name='rig_events',
            postgresql_concurrently=True, if_exists=True,
        )
