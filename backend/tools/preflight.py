"""Is this box actually ready to run a floor? One command, honest answers.

Deployment failures are rarely interesting. They are a migration that was
not run, a token list nobody set, a spool directory the service cannot
write to, a database the app can reach and the workers cannot. Each is
five minutes to fix and an hour to find, because the symptom appears
somewhere else entirely - rigs refused, a board that never updates, a
disk that fills.

So this asks all of it up front and says which.

    python -m tools.preflight              # check, and say what is wrong
    python -m tools.preflight --strict     # also fail on things that are
                                           # merely unwise on a floor

Exit codes, because this belongs in a deploy script:

    0   ready
    1   something is broken and the service will not work
    2   --strict, and something is open that should not be on a floor

The distinction matters. A service with no rig tokens works perfectly and
should never see a floor; that is a 2, not a 1. Nothing here changes
anything - it only looks.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "  [ ok ]", WARN: "  [warn]", FAIL: "  [FAIL]"}

results: list[tuple[str, str, str]] = []


def record(state: str, name: str, detail: str = "") -> None:
    results.append((state, name, detail))
    print(f"{MARK[state]} {name}" + (f" - {detail}" if detail else ""))


async def check_settings() -> object:
    from core.infrastructure.config import get_settings

    try:
        s = get_settings()
    except Exception as e:
        record(FAIL, "settings", f"{type(e).__name__}: {e}")
        return None

    if not s.database_url:
        record(FAIL, "settings", "DATABASE_URL is not set")
        return s
    record(OK, "settings", s.safe_url())
    return s


async def check_database(s) -> bool:
    """Reachable, and the version, because asyncpg against an old server
    fails in ways that do not name the version."""
    from sqlalchemy import text

    from core.infrastructure.database import engine

    try:
        async with engine().connect() as conn:
            version = (await conn.execute(text("SHOW server_version"))).scalar()
        record(OK, "database", f"reachable, PostgreSQL {version}")
        return True
    except Exception as e:
        record(FAIL, "database", f"{type(e).__name__}: {e}")
        return False


async def check_migrations() -> bool:
    """At head. A service running against a schema behind its models fails
    at the first request that touches the missing column, which is a long
    way from here."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    from core.infrastructure.database import engine

    try:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        head = ScriptDirectory.from_config(cfg).get_current_head()
        async with engine().connect() as conn:
            current = (await conn.execute(
                text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception as e:
        record(FAIL, "migrations", f"cannot read alembic_version: {e} "
                                   f"(run: python -m alembic upgrade head)")
        return False

    if current == head:
        record(OK, "migrations", f"at head ({head})")
        return True
    record(FAIL, "migrations",
           f"database at {current}, code expects {head} "
           f"(run: python -m alembic upgrade head)")
    return False


async def check_tables() -> bool:
    """Every table the models expect. Catches a migration that ran against
    a different database than the one configured here."""
    from sqlalchemy import inspect

    from core.base.model import Base
    from core.infrastructure.database import engine

    # Every domain, imported so Base.metadata knows what to expect. The
    # first version of this check did not, so metadata was empty and it
    # cheerfully reported "0 present, ok" against a database with no
    # tables at all - a preflight that passes when nothing is there is
    # worse than no preflight.
    from core.domains.alerts import model as _a  # noqa: F401
    from core.domains.episode_videos import model as _ev  # noqa: F401
    from core.domains.episodes import model as _e  # noqa: F401
    from core.domains.rig_downtime_events import model as _d  # noqa: F401
    from core.domains.rig_events import model as _re  # noqa: F401
    from core.domains.rig_productivity_blocks import model as _b  # noqa: F401
    from core.domains.rig_shift_checks import model as _c  # noqa: F401
    from core.domains.rig_status import model as _s  # noqa: F401
    from core.domains.schedules import model as _sc  # noqa: F401
    from core.domains.sessions import model as _se  # noqa: F401

    try:
        async with engine().connect() as conn:
            found = set(await conn.run_sync(
                lambda c: inspect(c).get_table_names()))
    except Exception as e:
        record(FAIL, "tables", str(e))
        return False

    expected = set(Base.metadata.tables)
    if not expected:
        # The guard for the bug above. If this check does not know what to
        # look for, it must not report success.
        record(FAIL, "tables", "no models registered - this check is broken, "
                               "not the database")
        return False

    missing = expected - found
    if missing:
        record(FAIL, "tables", f"missing: {', '.join(sorted(missing))}")
        return False
    record(OK, "tables", f"{len(expected)} present")
    return True


async def check_storage(s) -> bool:
    """Writable, and by this process. A spool the service cannot write to
    turns into rigs that cannot hand over their takes, which looks like a
    rig problem from every angle except this one."""
    from core.infrastructure.storage import get_storage

    try:
        store = get_storage()
        kind = type(store).__name__
        probe = "_preflight/probe.bin"
        payload = b"preflight"
        await store.put(probe, payload)
        landed = await store.head(probe)
        await store.delete(probe)
    except Exception as e:
        record(FAIL, "storage", f"{type(e).__name__}: {e}")
        return False

    if landed is None or landed.bytes != len(payload):
        record(FAIL, "storage", f"{kind}: wrote a probe and could not read it back")
        return False
    if not landed.sha256:
        record(FAIL, "storage",
               f"{kind}: returns no checksum, so no upload could ever be "
               f"confirmed and no rig could ever delete its copy")
        return False
    record(OK, "storage", f"{kind}: write, read and checksum all work")
    return True


async def check_workers() -> bool:
    """Importable in this interpreter. They run as separate processes, so
    a missing dependency shows up when the first one starts rather than
    when the service does."""
    names = ("project_events", "sweep_floor", "drain_to_archive")
    for n in names:
        try:
            __import__(f"workers.{n}")
        except Exception as e:
            record(FAIL, "workers", f"{n}: {type(e).__name__}: {e}")
            return False
    record(OK, "workers", ", ".join(names))
    return True


def check_posture(s) -> None:
    """Things that work perfectly and should not see a floor."""
    if s.rig_tokens:
        record(OK, "rig auth", f"{len(s.rig_tokens)} rigs provisioned")
    else:
        record(WARN, "rig auth",
               "RIG_TOKENS is empty - any caller may file events for any rig")

    if s.desk_token:
        record(OK, "desk auth", "push requires a token")
    else:
        record(WARN, "desk auth",
               "DESK_TOKEN is empty - anyone who can reach this may push a schedule")

    if s.rig_rate_limit_per_min:
        record(OK, "rate limit", f"{s.rig_rate_limit_per_min}/min per rig")
    else:
        record(WARN, "rate limit",
               "RIG_RATE_LIMIT_PER_MIN is 0 - one rig in a retry loop is unbounded")

    if s.video_keep_days:
        record(OK, "retention", f"archived video kept {s.video_keep_days} days")
    else:
        record(WARN, "retention",
               "VIDEO_KEEP_DAYS is 0 - nothing is ever deleted")

    if s.test_database_url:
        # Compared by host, port and database name, not by the whole URL.
        # Two URLs differing only in the password point at exactly the same
        # database, and this check exists to stop the test suite - which
        # drops and recreates every table it can see - being aimed at the
        # floor's data. Matching on the string would have missed that.
        from urllib.parse import urlsplit

        def target(url):
            u = urlsplit(url)
            return (u.hostname, u.port, u.path)

        if target(s.test_database_url) == target(s.database_url):
            record(FAIL, "databases",
                   "TEST_DATABASE_URL and DATABASE_URL point at the same "
                   "database - the test suite drops every table it can see")
        else:
            record(OK, "databases", "test and live are different databases")


async def run(strict: bool) -> int:
    print("preflight\n")

    s = await check_settings()
    if s is None or not s.database_url:
        return 1

    live = await check_database(s)
    if live:
        await check_migrations()
        await check_tables()
    await check_storage(s)
    await check_workers()
    check_posture(s)

    from core.infrastructure.database import engine
    await engine().dispose()

    fails = [r for r in results if r[0] == FAIL]
    warns = [r for r in results if r[0] == WARN]

    print()
    if fails:
        print(f"NOT READY - {len(fails)} broken, {len(warns)} open")
        return 1
    if warns and strict:
        print(f"READY, but {len(warns)} things are open that should not be "
              f"on a floor (--strict)")
        return 2
    if warns:
        print(f"ready - {len(warns)} warnings, none fatal")
        return 0
    print("ready")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strict", action="store_true",
                   help="exit 2 if anything is open that should not be on a floor")
    raise SystemExit(asyncio.run(run(p.parse_args().strict)))
