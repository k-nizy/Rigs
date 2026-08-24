from datetime import datetime

from sqlalchemy import select, update

from core.base.repository import BaseRepository
from core.domains.alerts.model import Alert


class AlertRepository(BaseRepository[Alert]):
    model = Alert

    async def open_alerts(self) -> list[Alert]:
        rows = await self.session.execute(
            select(Alert).where(Alert.resolved_at.is_(None)).order_by(Alert.opened_at)
        )
        return list(rows.scalars().all())

    async def open_keys(self) -> dict[str, Alert]:
        return {a.key: a for a in await self.open_alerts()}

    async def raise_(self, key: str, kind: str, rig_id: str, detail: str,
                     now: datetime) -> Alert | None:
        """Open an alert, unless one is already open for this key.

        Returns the new row, or None if it was already raised. The detail
        of an open alert is refreshed - "down 3 min" becomes "down 40 min"
        - because the situation is the same one and a stale number on the
        board is worse than none.
        """
        existing = (await self.open_keys()).get(key)
        if existing is not None:
            existing.detail = detail
            return None
        row = Alert(key=key, kind=kind, rig_id=rig_id, detail=detail, opened_at=now)
        self.session.add(row)
        await self.session.flush()
        return row

    async def resolve(self, keys: set[str], now: datetime) -> int:
        """Close every open alert whose key is not in `keys`.

        The sweep reports what is true now; anything open that it did not
        report has stopped being true. Closing by omission is what keeps
        the board honest without every rule needing a matching
        all-clear.
        """
        rows = await self.session.execute(
            select(Alert).where(Alert.resolved_at.is_(None))
        )
        stale = [a.id for a in rows.scalars().all() if a.key not in keys]
        if not stale:
            return 0
        await self.session.execute(
            update(Alert).where(Alert.id.in_(stale)).values(resolved_at=now)
        )
        return len(stale)
