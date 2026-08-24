"""One engine, one session factory, one dependency.

The engine is created lazily so importing this module never opens a
socket - tests point it at rigs_test before anything connects.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.infrastructure.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, future=True)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(engine(), expire_on_commit=False)
    return _sessionmaker


def configure(url: str) -> None:
    """Point every future session at a different database. Tests call this;
    nothing in the request path does."""
    global _engine, _sessionmaker
    _engine = create_async_engine(url, future=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session
