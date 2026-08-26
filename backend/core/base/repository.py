"""BaseRepository[T] - the generic every domain repository extends.

Deliberately thin. A repository owns how one aggregate is read and
written and nothing else; anything that spans two of them is a workflow,
which lives in the service layer.
"""

from typing import Generic, Sequence, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.base.model import Base

T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    model: type[T]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, pk) -> T | None:
        return await self.session.get(self.model, pk)

    async def all(self, limit: int = 100) -> Sequence[T]:
        rows = await self.session.execute(select(self.model).limit(limit))
        return rows.scalars().all()

    async def add(self, obj: T) -> T:
        self.session.add(obj)
        await self.session.flush()
        return obj
