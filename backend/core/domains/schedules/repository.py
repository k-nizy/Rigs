from datetime import date

from sqlalchemy import desc, select

from core.base.repository import BaseRepository
from core.domains.schedules.model import Schedule


class ScheduleRepository(BaseRepository[Schedule]):
    model = Schedule

    async def current_for_rig(self, rig_id: str) -> Schedule | None:
        """The most recently pushed schedule for one rig. A rig asks for
        this on boot and, once it polls, at every turn boundary."""
        rows = await self.session.execute(
            select(Schedule)
            .where(Schedule.rig_id == rig_id)
            .order_by(desc(Schedule.pushed_at))
            .limit(1)
        )
        return rows.scalar_one_or_none()

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
