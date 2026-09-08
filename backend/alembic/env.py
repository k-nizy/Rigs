"""Alembic, pointed at the same settings the app uses.

The URL is never written into alembic.ini - it carries a password and
lives in backend/.env, which is gitignored.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from core.base.model import Base
from core.infrastructure.config import get_settings

# Imported for their side effect: registering tables on Base.metadata.
from core.domains.accounts import model as _accounts  # noqa: F401
from core.domains.alerts import model as _alerts  # noqa: F401
from core.domains.episode_videos import model as _episode_videos  # noqa: F401
from core.domains.episodes import model as _episodes  # noqa: F401
from core.domains.people import model as _people  # noqa: F401
from core.domains.rig_status import model as _status  # noqa: F401
from core.domains.rig_downtime_events import model as _downtime  # noqa: F401
from core.domains.rig_events import model as _rig_events  # noqa: F401
from core.domains.rig_productivity_blocks import model as _blocks  # noqa: F401
from core.domains.rig_shift_checks import model as _checks  # noqa: F401
from core.domains.sessions import model as _sessions  # noqa: F401
from core.domains.schedules import model as _schedules  # noqa: F401

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return context.get_x_argument(as_dictionary=True).get("url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_online())
