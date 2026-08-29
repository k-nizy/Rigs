from datetime import date
from typing import Sequence

from sqlalchemy import desc, select

from core.base.repository import BaseRepository
from core.domains.schedules.model import Schedule, SchedulePush


class ScheduleRepository(BaseRepository[Schedule]):
    model = Schedule

    async def for_shift(self, rig_id: str, shift_date: date, shift_label: str) -> Schedule | None:
        """The schedule an event belongs under, used to resolve push_id at
        ingest so every row can be joined back to what was in force."""
        rows = await self.session.execute(
            select(Schedule)
            .where(
                Schedule.rig_id == rig_id,
                Schedule.shift_date == shift_date,
                Schedule.shift_label == shift_label,
            )
            .order_by(desc(Schedule.pushed_at))
            .limit(1)
        )
        return rows.scalar_one_or_none()


class SchedulePushRepository(BaseRepository[SchedulePush]):
    """Who pushed, read back newest first."""

    model = SchedulePush

    async def recent(self, limit: int = 50) -> Sequence[SchedulePush]:
        rows = await self.session.execute(
            select(SchedulePush)
            .order_by(desc(SchedulePush.pushed_at))
            .limit(limit)
        )
        return rows.scalars().all()

    async def for_push(self, push_id) -> SchedulePush | None:
        """The audit row belonging to one push, so a schedule row can be
        traced back to the person who put it there."""
        rows = await self.session.execute(
            select(SchedulePush).where(SchedulePush.push_id == push_id)
        )
        return rows.scalar_one_or_none()
