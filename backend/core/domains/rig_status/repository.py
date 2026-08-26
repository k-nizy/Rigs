from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from core.base.repository import BaseRepository
from core.domains.rig_status.model import RigStatus


class RigStatusRepository(BaseRepository[RigStatus]):
    model = RigStatus

    async def beat(self, rig_id: str, seen_at: datetime, rig_clock: datetime,
                   skew_secs: float) -> None:
        """Record a heartbeat. Upsert, because a rig has exactly one status
        and a restart must not create a second."""
        stmt = insert(RigStatus).values(
            rig_id=rig_id, last_seen_at=seen_at,
            rig_clock_at=rig_clock, skew_secs=skew_secs,
        ).on_conflict_do_update(
            index_elements=[RigStatus.rig_id],
            set_=dict(last_seen_at=seen_at, rig_clock_at=rig_clock, skew_secs=skew_secs),
        )
        await self.session.execute(stmt)

    async def everything(self) -> list[RigStatus]:
        rows = await self.session.execute(select(RigStatus).order_by(RigStatus.rig_id))
        return list(rows.scalars().all())
