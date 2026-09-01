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


def load_every_domain() -> list[str]:
    """Import every domain's models, so `Base.metadata` knows what to look
    for. Returns the domains it loaded.

    Found on disk rather than listed here, and that is the fix for a bug
    this function has now had twice. The first version imported nothing,
    so metadata was empty and it reported "0 present, ok" against a
    database with no tables at all. The second listed ten domains by
    hand, `accounts` was added without anyone knowing this third list
    existed, and it reported "11 present" while the models defined
    thirteen - so it would have passed a database with no `accounts`
    table, on a service whose every login needs one.

    A list that has to be remembered in three places is a list that will
    be wrong in one of them. There is nothing to remember now: a new
    directory under `core/domains/` with a `model.py` in it is found.
    """
    import importlib

    root = Path(__file__).resolve().parents[1] / "core" / "domains"
    loaded = []
    for path in sorted(root.glob("*/model.py")):
        name = path.parent.name
        importlib.import_module(f"core.domains.{name}.model")
        loaded.append(name)
    return loaded


async def check_tables() -> bool:
    """Every table the models expect. Catches a migration that ran against
    a different database than the one configured here."""
    from sqlalchemy import inspect

    from core.base.model import Base
    from core.infrastructure.database import engine

    load_every_domain()

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


def check_posture(s, accounts: int | None = None) -> None:
    """Things that work perfectly and should not see a floor."""
    if s.rig_tokens:
        record(OK, "rig auth", f"{len(s.rig_tokens)} rigs provisioned")
    else:
        record(WARN, "rig auth",
               "RIG_TOKENS is empty - any caller may file events for any rig")

    # Identity and the token that goes with it are set in two places and
    # must name the same twelve rigs. A rig with a token and no address
    # can never fetch it, so it never starts; a rig with an address and no
    # token is handed one the service will refuse on every call. Both fail
    # somewhere else entirely - a rig stuck on its own screen, or a floor
    # whose events all 401 - so they are compared here, where the answer
    # is a line of output rather than an afternoon.
    if s.rig_addresses:
        no_token = sorted(set(s.rig_addresses) - set(s.rig_tokens))
        no_address = sorted(set(s.rig_tokens) - set(s.rig_addresses))
        dupes = sorted(
            a for a in set(s.rig_addresses.values())
            if list(s.rig_addresses.values()).count(a) > 1
        )
        if no_token or no_address or dupes:
            parts = []
            if no_address:
                parts.append("no address, so they can never be identified: "
                             + ", ".join(no_address))
            if no_token:
                parts.append("no token, so every call they make is refused: "
                             + ", ".join(no_token))
            if dupes:
                parts.append("addresses used by more than one rig, which "
                             "identifies neither: " + ", ".join(dupes))
            record(FAIL, "rig identity", "; ".join(parts))
        else:
            record(OK, "rig identity",
                   f"{len(s.rig_addresses)} rigs, each with an address and a token")
    elif s.rig_tokens:
        record(FAIL, "rig identity",
               "RIG_TOKENS is set but RIG_ADDRESSES is empty - no rig can be told "
               "which rig it is, so every one of them refuses to start")
    else:
        record(WARN, "rig identity",
               "RIG_ADDRESSES is empty - rigs are not told apart, which is a demo")

    if s.desk_token:
        record(OK, "desk auth", "push requires a token")
    else:
        record(WARN, "desk auth",
               "DESK_TOKEN is empty - anyone who can reach this may push a schedule")

    # The desk's other door. A switch is announced in three places in this
    # service - `announce()` at startup, `/api/health` for a load balancer,
    # and here for the unit that refuses to come up - and the person-auth
    # ones were wired into the first two and not this one. So a deployment
    # was told the *rig* door was open and nothing about the *desk* door.
    if accounts is None:
        record(WARN, "person auth",
               "could not be read - the accounts table was not reachable")
    elif accounts:
        record(OK, "person auth", f"{accounts} accounts; the desk needs a sign-in")
    else:
        record(WARN, "person auth",
               "no accounts - the desk is open to anyone who can reach it, and "
               "nobody can sign in to it either. Make one with "
               "`python -m tools.mint_account manager`")

    # The one switch in this service whose default is the safe setting.
    # Everything else is off until somebody turns it on; this is on until
    # somebody turns it off, so it being off is always a decision and
    # always worth saying out loud.
    if s.session_cookie_secure:
        record(OK, "session cookie", "Secure - HTTPS only")
    else:
        record(WARN, "session cookie",
               "SESSION_COOKIE_SECURE is false - the session cookie will "
               "travel in cleartext. Right for local http, wrong for a floor")

    if s.login_rate_limit_per_min:
        record(OK, "login rate limit",
               f"{s.login_rate_limit_per_min}/min per address")
    else:
        record(WARN, "login rate limit",
               "LOGIN_RATE_LIMIT_PER_MIN is 0 - passwords may be guessed at "
               "whatever rate the network allows")

    if s.login_lockout_after:
        record(OK, "login lockout",
               f"after {s.login_lockout_after} failures, up to "
               f"{s.login_lockout_max_wait_secs}s")
    else:
        record(WARN, "login lockout",
               "LOGIN_LOCKOUT_AFTER is 0 - a weak password can be found by "
               "working through a list at the rate limit")

    # Password reset by email. Off until configured, and unlike most of
    # what is checked here, being *on* is the state that needs saying.
    if not s.smtp_host:
        record(OK, "password reset",
               "off - no SMTP_HOST, so the reset routes refuse. A manager "
               "sets passwords with tools.mint_account passwd")
    else:
        if not s.public_base_url:
            record(FAIL, "password reset",
                   "SMTP_HOST is set but PUBLIC_BASE_URL is not, so the link "
                   "in the email would have nowhere to point")
        else:
            record(OK, "password reset",
                   f"on - links to {s.public_base_url}, good for "
                   f"{s.password_reset_minutes} minutes, "
                   f"{s.password_reset_per_hour}/hour per address")

        # The part nobody thinks about when they turn it on.
        record(WARN, "reset addresses",
               "password reset is on, which makes the email on an account a "
               "credential: whoever reads that mailbox can take the account. "
               "Those addresses are typed once at mint_account time and "
               "nothing has ever verified one, so a typo is a reset link "
               "posted to a stranger. Check them with "
               "`python -m tools.mint_account list`")

        if not s.smtp_starttls:
            record(WARN, "reset transport",
                   "SMTP_STARTTLS is false - the reset link travels to the "
                   "relay in cleartext, and that link is the account")

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


async def count_accounts() -> int | None:
    """How many people can sign in. None if the question cannot be asked.

    Read rather than assumed: person auth is on when there is somebody to
    be, and that fact lives in the database rather than in a setting.
    """
    from sqlalchemy import func, select

    from core.domains.accounts.model import Account
    from core.infrastructure.database import engine

    try:
        async with engine().connect() as conn:
            return int((await conn.execute(
                select(func.count()).select_from(Account))).scalar_one())
    except Exception:
        # A missing table is already reported by check_tables; saying it
        # twice in different words helps nobody.
        return None


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
    check_posture(s, await count_accounts() if live else None)

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
