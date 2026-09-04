"""One operator at one rig, turn start to turn end.

There is no session_started event and there should not be: the schedule
already says when a turn begins, so a session is opened by the first
event seen under a (rig, shift, turn, operator) key and closed by
whatever ends it. Adding an event for something the payload already
knows would be the rig asking a question it has the answer to.

It closes in one of two ways, and which one matters:
  handover      - the stint ran to its boundary, the ordinary case
  operator      - the operator ended the session with the rig down
"""

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.base.model import TimestampedBase


class Session(TimestampedBase):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    rig_id: Mapped[str] = mapped_column(String(16), nullable=False)
    shift_date: Mapped[date] = mapped_column(Date, nullable=False)
    shift_label: Mapped[str] = mapped_column(String(16), nullable=False)
    turn_from: Mapped[str | None] = mapped_column(String(5), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Who held the turn this session covers. operator_id names the seat the
    # schedule put them in, and a seat is held by different people on
    # different days. Nullable: events filed before the field existed
    # carry no name, and they are not to be refused.
    operator_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_by: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        # The key a session is identified by. Replay must find the same
        # session again rather than opening a second one.
        UniqueConstraint(
            "rig_id", "shift_date", "shift_label", "turn_from", "operator_id",
            name="uq_sessions_turn",
        ),
        Index("ix_sessions_operator", "operator_id", "shift_date"),
    )
