"""The suite stays inside its own schema.

Each run gets a private schema in `rigs_test` so two runs at once cannot
demolish each other's tables. That part worked. What did not is the
direction nobody checked: `public`.

SQLAlchemy emits unqualified table names, so Postgres resolves each one
by walking `search_path` in order. With the path set to `run_X,public`,
`CREATE TABLE accounts` lands in `run_X` - first on the path - but
`DROP TABLE accounts` walks the same path, and on the first test of a
run, while `run_X` is still empty, it walks straight past and finds
`public.accounts` instead. The suite drops a table it never created.

This is not hypothetical and it is not cheap. Running a service against
`rigs_test` is the documented way to do local end-to-end work without
minting an account, and a suite started beside it silently deletes the
tables out from under it. The failure looks like the service breaking.

Everything here goes through the fixtures the rest of the suite uses,
never through a connection this file builds for itself. An earlier
version of this file asked a *helper* what the path should be, which is
a question the helper always answers correctly - it passed against the
unfixed code, which is the only thing a regression test must not do.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import RUN_SCHEMA, _test_url

CANARY = "isolation_canary"


async def _plain(sql: str, params: dict | None = None):
    """A connection with the *default* path, the way a service running
    beside the suite would have one. Deliberately not the fixture.

    `returns_rows` because most of these are DDL, and asking a DDL
    result for a value raises rather than giving None.
    """
    eng = create_async_engine(_test_url(), future=True)
    try:
        async with eng.begin() as c:
            result = await c.execute(text(sql), params or {})
            return result.scalar() if result.returns_rows else None
    finally:
        await eng.dispose()


@pytest.fixture
async def canary_in_public():
    await _plain(f"DROP TABLE IF EXISTS public.{CANARY} CASCADE")
    await _plain(f"CREATE TABLE public.{CANARY} (id int)")
    await _plain(f"INSERT INTO public.{CANARY} VALUES (1)")
    yield
    await _plain(f"DROP TABLE IF EXISTS public.{CANARY} CASCADE")


async def _public_still_has_canary() -> bool:
    return bool(await _plain(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = :t", {"t": CANARY}))


class TestTheRunStaysInItsOwnSchema:
    async def test_the_connection_can_see_only_this_runs_schema(self, engine):
        """Asked of the live connection the suite actually uses, not of a
        helper that would answer correctly either way."""
        async with engine.begin() as c:
            path = (await c.execute(text("SHOW search_path"))).scalar()

        names = [n.strip().strip('"') for n in path.split(",")]
        assert names == [RUN_SCHEMA], (
            f"the connection's search_path is {path!r}. Anything after the run "
            f"schema is another place an unqualified DROP TABLE can resolve to, "
            f"which is how this suite came to drop tables in `public`")

    async def test_a_table_in_public_survives_this_suites_drop_and_recreate(
            self, engine, canary_in_public):
        """The behaviour, through the fixture's own engine.

        `drop_all` here is the same call the `engine` fixture makes
        before every single test, with the same connection settings. On
        the unfixed code it walks past the empty run schema and takes
        the table in `public` instead.
        """
        assert await _public_still_has_canary(), "the fixture did not set up"

        md = MetaData()
        Table(CANARY, md, Column("id", Integer))
        async with engine.begin() as c:
            await c.run_sync(md.drop_all)
            await c.run_sync(md.create_all)

        assert await _public_still_has_canary(), (
            "the run reached into `public` and dropped a table it never "
            "created - which is a service running against rigs_test losing "
            "its tables the moment somebody starts the suite")

    async def test_the_run_still_creates_its_own_tables_where_it_should(
            self, engine, session):
        """The other half. A fix that confined the run by breaking it
        would satisfy the test above and be worse than the bug."""
        from core.domains.accounts.model import Account
        from core.domains.accounts.passwords import hash_password

        session.add(Account(email="x@verlet.co", name="X", role="manager",
                            password_hash=hash_password("a-real-password-12")))
        await session.commit()
        assert len((await session.execute(select(Account))).scalars().all()) == 1

        async with engine.begin() as c:
            where = (await c.execute(text(
                "SELECT table_schema FROM information_schema.tables "
                "WHERE table_name = 'accounts'"))).scalars().all()
        assert where == [RUN_SCHEMA], (
            f"accounts should exist only in this run's schema, found in {where}")

    async def test_nothing_needed_public_to_resolve(self, engine, session):
        """Dropping `public` from the path would break the suite if any
        column default or type resolved through it. Nothing does - the
        UUID defaults come from `pg_catalog`, which is always on the path
        - and this is what says so rather than a comment claiming it.
        """
        from core.domains.accounts.model import Account
        from core.domains.accounts.passwords import hash_password

        row = Account(email="y@verlet.co", name="Y", role="operator",
                      operator_id="op-a9",
                      password_hash=hash_password("a-real-password-12"))
        session.add(row)
        await session.commit()
        assert row.id is not None, "a server-side default did not resolve"
        assert row.received_at is not None

    async def test_the_app_under_test_is_confined_too(self, engine, client):
        """`database.configure` builds the same path for the app the
        tests drive, from its own copy of the same line. Fixing one and
        not the other leaves half the suite outside its schema.
        """
        from core.infrastructure.database import sessionmaker

        async with sessionmaker()() as s:
            path = (await s.execute(text("SHOW search_path"))).scalar()

        names = [n.strip().strip('"') for n in path.split(",")]
        assert names == [RUN_SCHEMA], (
            f"the app's own sessions run with search_path {path!r}, so the "
            f"service under test can still reach into `public`")
