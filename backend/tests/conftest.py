"""Test fixtures. Everything runs against rigs_test, never rigs_dev.

The schema is created from the models rather than by running Alembic, so
a test run cannot leave a half-migrated database behind. The migration
itself is checked separately, by applying it to rigs_dev.
"""

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
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


@pytest_asyncio.fixture
async def engine():
    """A clean schema per test. Dropping and recreating is fast at this
    size and means no test can inherit another's rows."""
    eng = create_async_engine(_test_url(), future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    database.configure(_test_url())
    yield eng
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
