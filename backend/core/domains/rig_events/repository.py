"""Ingest and the cursor. Both are one query each, which is the payoff of
writing a ledger instead of routing straight into fact tables."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from core.base.repository import BaseRepository
from core.domains.rig_events.model import RigEvent


class RigEventRepository(BaseRepository[RigEvent]):
    model = RigEvent

    async def cursor(self, rig_id: str) -> int:
        """The highest seq this server holds for a rig. On reconnect the
        uploader asks for this and resends only what follows - no
        bookkeeping negotiation, no lost tail."""
        rows = await self.session.execute(
            select(func.max(RigEvent.seq)).where(RigEvent.rig_id == rig_id)
        )
        return rows.scalar() or -1

    async def append(self, rows: list[dict]) -> int:
        """Insert a batch, ignoring anything already held.

        ON CONFLICT DO NOTHING against UNIQUE (rig_id, event_id) is the
        whole of the idempotency story. A rig may resend the same batch
        any number of times; the second and later attempts change nothing
        and still report success, which is what keeps the uploader's retry
        logic twenty lines instead of two hundred.

        Returns how many rows were genuinely new.
        """
        if not rows:
            return 0
        stmt = (
            insert(RigEvent)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_rig_events_rig_event")
            .returning(RigEvent.id)
        )
        result = await self.session.execute(stmt)
        return len(result.scalars().all())

    async def count_for_rig(self, rig_id: str) -> int:
        rows = await self.session.execute(
            select(func.count()).select_from(RigEvent).where(RigEvent.rig_id == rig_id)
        )
        return rows.scalar() or 0

    async def since(self, rig_id: str, after_seq: int, limit: int = 1000) -> list[RigEvent]:
        rows = await self.session.execute(
            select(RigEvent)
            .where(RigEvent.rig_id == rig_id, RigEvent.seq > after_seq)
            .order_by(RigEvent.seq)
            .limit(limit)
        )
        return list(rows.scalars().all())
