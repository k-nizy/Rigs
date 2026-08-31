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


def schema_connect_args(schema: str) -> dict:
    """How a connection is confined to one schema, in the one place that
    decides it. `configure` below and the test fixtures both use this.

    The path names that schema and nothing else. It used to end in
    `,public`, and that one word was the difference between a run being
    isolated and a run being *mostly* isolated.

    SQLAlchemy emits unqualified table names, so Postgres resolves each
    one by walking this path in order. `CREATE TABLE accounts` landed in
    the run schema, first on the path - but `DROP TABLE accounts` walked
    the same path, and on the first test of a run, while that schema is
    still empty, it walked straight past and found `public.accounts`.
    The suite dropped a table it had never created.

    Not hypothetical: running a service against `rigs_test` is the
    documented way to do local end-to-end work without minting an
    account, and a suite started beside one deleted its tables out from
    under it. It looked like the service breaking.

    With one name on the path there is nowhere else for an unqualified
    name to go, so confinement is a constraint rather than a habit.
    Nothing needed `public` - the UUID defaults resolve from
    `pg_catalog`, which is always searched - and if something ever does,
    it fails loudly here rather than quietly somewhere else.
    """
    # Set on the connection rather than per statement: SQLAlchemy emits
    # unqualified names, so the search path is what decides where CREATE
    # TABLE and every later query land.
    return {"server_settings": {"search_path": schema}}


def configure(url: str, schema: str | None = None) -> None:
    """Point every future session at a different database. Tests call this;
    nothing in the request path does.

    `schema` puts every unqualified table in one named schema rather than
    `public`, which is how a test run gets a database to itself without
    needing a database of its own. The role here owns `rigs_test` and may
    create schemas in it; it may not create databases, so per-run schemas
    are the isolation that is actually available.

    It matters because the suite drops and recreates every table it can
    see. Two runs sharing `public` demolish each other's fixtures
    mid-test, and the failures that produces are the worst kind - they
    move around, they look like real bugs, and they are not.
    """
    global _engine, _sessionmaker
    kw = {}
    if schema:
        kw["connect_args"] = schema_connect_args(schema)
    _engine = create_async_engine(url, future=True, **kw)
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
