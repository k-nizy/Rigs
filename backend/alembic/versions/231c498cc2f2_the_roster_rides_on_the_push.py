"""the roster rides on the push

One nullable JSONB column on `schedule_pushes`: the roster the desk had
on screen when the button was pressed - four groups, each with a task,
three rigs and four operators in slot order.

The assignment had no home. It lived only inside the pushed payloads,
and the desk recovered it by reading twelve of them back and proving the
reconstruction turn by turn. That was the right fix for a compiled-in
file going stale, but its reason to exist was that there was no list of
people anywhere. There is one now, and the desk reads who is on the
floor instead of reconstructing it.

On the push row, not a table of its own, because the roster and the
payloads are one act: the payloads were built from exactly this roster,
in the same transaction, so the two cannot disagree - and who pushed it
and when is already on this row. Stored opaque, like the payload it
came with: this service is a courier and a filing cabinet, and the
moment it has opinions about slots it is a third answer.

Nullable, and it stays nullable: every desk that predates this field,
and the laptop demo, push without one.

Revision ID: 231c498cc2f2
Revises: cafe7fe92ab3
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '231c498cc2f2'
down_revision = 'cafe7fe92ab3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('schedule_pushes',
                  sa.Column('roster', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('schedule_pushes', 'roster')
