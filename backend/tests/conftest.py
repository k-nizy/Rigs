"""Test fixtures. Everything runs against rigs_test, never rigs_dev.

The schema is created from the models rather than by running Alembic, so
a test run cannot leave a half-migrated database behind. The migration
itself is checked separately, by applying it to rigs_dev.
"""

import os
import re
import secrets
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.base.model import Base  # noqa: E402

# Every domain, imported for the side effect of registering its table on
# Base.metadata. All of them, not the two this file happens to use: a
# subset left `create_all` building a schema with a foreign key pointing
# at a table that was not there, so running one test file on its own
# failed while the whole suite passed - because some other module had
# imported the missing model first. The same list as alembic/env.py.
from core.domains.accounts import model as _accounts  # noqa: E402,F401
from core.domains.alerts import model as _alerts  # noqa: E402,F401
from core.domains.episode_videos import model as _episode_videos  # noqa: E402,F401
from core.domains.episodes import model as _episodes  # noqa: E402,F401
from core.domains.rig_downtime_events import model as _downtime  # noqa: E402,F401
from core.domains.rig_events import model as _rig_events  # noqa: E402,F401
from core.domains.rig_productivity_blocks import model as _blocks  # noqa: E402,F401
from core.domains.rig_shift_checks import model as _checks  # noqa: E402,F401
from core.domains.rig_status import model as _status  # noqa: E402,F401
from core.domains.schedules import model as _schedules  # noqa: E402,F401
from core.domains.sessions import model as _sessions  # noqa: E402,F401
from core.infrastructure import database  # noqa: E402
from core.infrastructure.config import get_settings  # noqa: E402

REPO = ROOT.parent
FIXTURES = REPO / "packages" / "schema" / "fixtures"


def _test_url() -> str:
    s = get_settings()
    if not s.test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set in backend/.env")
    return s.test_database_url


# This run's own schema inside rigs_test, so two suites running at once
# cannot demolish each other's tables.
#
# The suite drops and recreates every table it can see, which is fine
# until a second run is doing the same thing in the same place. That has
# now cost real time twice: the failures wander between files, look
# exactly like product bugs, and are not - a gate that "refuses with no
# accounts" turned out to be another run inserting accounts mid-test.
#
# A database each would be tidier, but this role cannot create databases.
# It owns rigs_test and may create schemas in it, so that is the isolation
# actually on offer. The pid keeps concurrent runs apart; the random tail
# keeps a reused pid from inheriting a schema a killed run left behind.
RUN_SCHEMA = f"run_{os.getpid()}_{secrets.token_hex(3)}"


@pytest.fixture(autouse=True)
def _no_state_between_tests():
    """Three process-globals outlive a test, and nothing used to clear them.

    The rig limiter, the login limiter and the lockout are all module
    globals built lazily on first use. `_run_schema` gives each run its
    own database schema, so *rows* cannot leak - but none of this is
    rows, and six test files touch it.

    The lockout is the one that bites, because it is keyed on
    `time.monotonic` rather than on a count. Whether a sign-in is refused
    then depends on how many failures preceded it *and how much wall time
    passed while they did*, so the same command gives different answers
    on a loaded machine - which is what a suite run beside three other
    sessions is. The symptom is a 401 in a test that never asked for one,
    because a correct lockout deliberately does not announce itself.

    Reset before, not only after: a test that fails part-way still leaves
    the global dirty, and the next test is the one that pays.
    """
    from services.rigs import auth, people
    auth.reset_limiter()
    people.reset_login_limiter()
    yield
    auth.reset_limiter()
    people.reset_login_limiter()


def _process_is_alive(pid: int) -> bool:
    """Whether that process still exists. Answers True when unsure.

    Unsure has to mean alive: the only thing this decides is whether a
    schema may be dropped, and dropping a live run's tables is the bug
    the schema-per-run split exists to prevent. A stale schema left
    behind costs a few megabytes.

    `os.kill(pid, 0)` is the usual way and is only half portable. On
    Windows CPython maps every signal except the console ones onto
    TerminateProcess, so asking "are you alive" with it would kill the
    process being asked about - which here is somebody else's test run.
    """
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # Access denied means it is there and not ours to look at.
        return kernel32.GetLastError() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by somebody else
    except OSError:
        return True          # could not tell, so assume alive
    return True


async def _sweep_dead_run_schemas(conn) -> list[str]:
    """Drop the schemas left by runs that are no longer running.

    A run creates its schema at the start and drops it at the end. One
    that is killed - Ctrl-C, a CI timeout, a crash - never reaches the
    end, and its schema stays with thirteen tables in it. They accumulate
    quietly, and the first sign is a database that is bigger than anybody
    expects.

    Only the dead ones. A schema whose process is still running belongs
    to a suite in progress, and taking its tables away is precisely the
    failure the schema-per-run split was introduced to stop.
    """
    rows = (await conn.execute(text(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'run/_%' ESCAPE '/'"
    ))).scalars().all()

    dropped = []
    for name in rows:
        if name == RUN_SCHEMA:
            continue
        # The exact shape RUN_SCHEMA is built from, anchored at both
        # ends. A loose prefix match would put whatever `pg_namespace`
        # returned inside a quoted identifier below - which needs
        # database privileges to exploit and is still not a thing to
        # write. It also means this only ever touches schemas of its own
        # making.
        m = re.fullmatch(r"run_(\d+)_[0-9a-f]{6}", name)
        if not m or _process_is_alive(int(m.group(1))):
            continue
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        dropped.append(name)
    return dropped


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _run_schema():
    """Create this run's schema up front and take it away afterwards.

    Session-scoped: one schema for the whole run, not one per test. The
    per-test cleanliness below is unchanged - it still drops and recreates
    the tables - it just does it somewhere no other run is looking.
    """
    eng = create_async_engine(_test_url(), future=True)
    async with eng.begin() as conn:
        # Tidy up after runs that were killed before they could. Failing
        # to sweep must never fail the suite - it is housekeeping, and a
        # test run that refuses to start because it could not tidy is
        # worse than the mess.
        try:
            gone = await _sweep_dead_run_schemas(conn)
            if gone:
                print(f"\nconftest: cleared {len(gone)} schema(s) left by "
                      f"runs that were killed: {', '.join(gone)}")
        except Exception as e:          # noqa: BLE001 - see above
            print(f"\nconftest: could not sweep old run schemas ({e})")

        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{RUN_SCHEMA}"'))
    await eng.dispose()

    yield RUN_SCHEMA

    # CASCADE because the tables are in it. A run that dies without
    # reaching here leaves one behind; they are cheap, and `run_` plus a
    # dead pid says plainly what it was.
    eng = create_async_engine(_test_url(), future=True)
    async with eng.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{RUN_SCHEMA}" CASCADE'))
    await eng.dispose()


@pytest_asyncio.fixture
async def engine(_run_schema):
    """A clean schema per test. Dropping and recreating is fast at this
    size and means no test can inherit another's rows."""
    eng = create_async_engine(
        _test_url(), future=True,
        connect_args={"server_settings": {"search_path": f"{RUN_SCHEMA},public"}},
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    database.configure(_test_url(), schema=RUN_SCHEMA)
    yield eng
    # Both of them. `eng` is this fixture's own engine; `database` holds a
    # second one that `configure` just built for the app under test, and
    # leaving that one open every test is how a suite runs out of
    # connections somewhere unrelated.
    await database.dispose()
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest_asyncio.fixture
async def client(engine):
    """The service over ASGI - real routing, real validation, no socket."""
    from services.rigs.app import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
