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
        s = get_settings()
        _engine = create_async_engine(
            s.database_url,
            future=True,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            # A connection idle longer than this is replaced rather than
            # handed out. Anything between here and Postgres - a pooler, a
            # firewall - may drop an idle socket without telling either
            # end, and the first request to reuse it is the one that fails.
            pool_recycle=s.db_pool_recycle_secs,
            # Costs one round trip on checkout and turns "the connection
            # died while idle" from a 500 into nothing at all.
            pool_pre_ping=True,
        )
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


async def dispose() -> None:
    """Close the pool this module is holding, and forget it.

    The pair to `configure`. Without it every call to `configure` leaves
    the previous engine - and its pooled connections - open, because
    nothing else holds a reference and closing a connection is not
    something garbage collection does promptly or predictably.

    One process configuring once does not care. A test suite configures
    once per test, so a few hundred tests leave a few hundred pools
    behind, and Postgres has a `max_connections`. The failure that
    produces is the worst kind: it lands on whichever test happens to be
    running when the ceiling is reached, so it looks like a bug in
    something unrelated and moves each time the suite grows.
    """
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
