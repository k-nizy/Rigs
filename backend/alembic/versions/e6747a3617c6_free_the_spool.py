"""free the spool

A spool that never frees is not a spool. The drain used to copy to the
archive and stop, so every byte existed twice for ever and the on-prem
disk filled at exactly the rate video arrived - about 2.7 TB a day across
twelve rigs.

`video_freed_at` records the second fact: not "this reached the cold
tier" but "we have stopped keeping our own copy of it". They are two
different moments and a crash can land between them, so the drain needs
to be able to find rows in between.

Existing rows are left with a NULL `video_freed_at`, including ones
already marked archived. That is deliberate and is not a backfill
oversight: those are exactly the spool copies the old drain left behind,
and the release pass will collect them. It re-asks the archive what it
holds before deleting anything, so a row archived months ago under the
old code is verified again rather than trusted.

The index is partial and is empty in the steady state - it only holds
rows that are archived and not yet freed. Built CONCURRENTLY for the same
reason as the last one: `episodes` is written on every projection, and a
lock here stalls the pipeline behind twelve rigs.

Revision ID: e6747a3617c6
Revises: 5dedb9132a9b
"""
import sqlalchemy as sa
from alembic import op

revision = 'e6747a3617c6'
down_revision = '5dedb9132a9b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no default: instant, no table rewrite.
    op.add_column(
        'episodes',
        sa.Column('video_freed_at', sa.DateTime(timezone=True), nullable=True),
    )
    with op.get_context().autocommit_block():
        op.create_index(
            'ix_episodes_unfreed', 'episodes', ['video_archived_at'],
            unique=False,
            postgresql_where=sa.text('video_freed_at IS NULL'),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            'ix_episodes_unfreed', table_name='episodes',
            postgresql_concurrently=True, if_exists=True,
        )
    op.drop_column('episodes', 'video_freed_at')
