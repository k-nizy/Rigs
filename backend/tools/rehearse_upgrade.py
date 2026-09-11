"""Rehearse the upgrade a floor would actually go through.

    python -m tools.rehearse_upgrade            # against DATABASE_URL
    python -m tools.rehearse_upgrade --test     # against TEST_DATABASE_URL

CI applies the migrations to an empty database and round-trips them
base-to-head. A green run of that proves the migrations are consistent
with themselves. It proved nothing about two things a floor does:

  - **upgrade over rows that already exist.** A CHECK added the ordinary
    way is checked against every existing row and refuses the whole
    migration. `6492d4e8117c` did exactly that: green on an empty
    database, undeployable to any floor with an operator account.
  - **roll back one step, then go forward again.** DEPLOY.md's rollback
    is `downgrade -1`. A downgrade that does not restore what the
    upgrade removed leaves a database claiming one revision with the
    schema of another, and the next `upgrade` fails on the object it
    expects to find. Base-to-head never sees it, because going to base
    drops the tables.

So this rehearses both, the way the `episodes` model's own comment says
that bug was caught: by running the real thing first.

    1. downgrade to FLOOR_AS_OF - a pinned revision, the floor as it
       was before an account could name a person
    2. seed what such a floor holds: a manager, an operator with a seat
       and no person, a pushed schedule
    3. upgrade head              - must carry that floor forward
    4. alembic check             - models and migrations still agree
    5. downgrade -1, upgrade head - the documented rollback round-trips

The pin does not move. Every migration written after it has to carry
this floor forward, which is the guarantee: not that the newest change
tolerates old rows, but that all of them do. Seeding is raw SQL against
the pinned schema rather than the models, because the models are at
head and this database, at step 2, deliberately is not.

The database is left at head with the seed rows removed. Never point
this at a floor's live database: it downgrades it.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import uuid

import asyncpg

from core.infrastructure.config import get_settings

# The floor as it was before an account could name a person: the
# revision that added `schedule_pushes.roster`, immediately before
# `6492d4e8117c`. Pinned; do not advance it.
FLOOR_AS_OF = "231c498cc2f2"

SEED_EMAILS = ("rehearsal.manager@verlet.co", "rehearsal.operator@verlet.co")


def _alembic(url: str, *args: str) -> None:
    cmd = [sys.executable, "-m", "alembic", "-x", f"url={url}", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-12:])
        raise SystemExit(f"alembic {' '.join(args)} failed:\n{tail}")


def _pg(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _seed(url: str) -> None:
    c = await asyncpg.connect(_pg(url))
    try:
        await c.execute("DELETE FROM accounts WHERE email = ANY($1::text[])", list(SEED_EMAILS))
        await c.execute(
            """INSERT INTO accounts (id, email, name, role, operator_id, password_hash)
               VALUES ($1, $2, 'Rehearsal Manager', 'manager', NULL, 'not-a-hash'),
                      ($3, $4, 'Rehearsal Operator', 'operator', 'op-a2', 'not-a-hash')""",
            uuid.uuid4(), SEED_EMAILS[0], uuid.uuid4(), SEED_EMAILS[1],
        )
        await c.execute(
            """INSERT INTO schedules (id, push_id, pushed_at, rig_id, shift_date, shift_label, payload)
               VALUES ($1, $2, now(), 'RIG-REHEARSAL', current_date, 'Morning', '{"rigId":"RIG-REHEARSAL","turns":[]}'::jsonb)""",
            uuid.uuid4(), uuid.uuid4(),
        )
    finally:
        await c.close()


async def _unseed(url: str) -> None:
    c = await asyncpg.connect(_pg(url))
    try:
        await c.execute("DELETE FROM accounts WHERE email = ANY($1::text[])", list(SEED_EMAILS))
        await c.execute("DELETE FROM schedules WHERE rig_id = 'RIG-REHEARSAL'")
    finally:
        await c.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--test", action="store_true",
                   help="rehearse against TEST_DATABASE_URL instead of DATABASE_URL")
    args = p.parse_args()
    s = get_settings()
    url = s.test_database_url if args.test else s.database_url
    if not url:
        raise SystemExit("no database URL configured for this rehearsal")

    print(f"1. downgrade to {FLOOR_AS_OF} (the floor before accounts named people)")
    _alembic(url, "downgrade", FLOOR_AS_OF)
    print("2. seed a manager, an operator with a seat and no person, and a push")
    asyncio.run(_seed(url))
    print("3. upgrade head over that floor")
    _alembic(url, "upgrade", "head")
    print("4. the models and the migrations still agree")
    _alembic(url, "check")
    print("5. the documented rollback: downgrade -1, then upgrade head")
    _alembic(url, "downgrade", "-1")
    _alembic(url, "upgrade", "head")
    asyncio.run(_unseed(url))
    print("rehearsal passed: this floor upgrades, and rolls back one step and forward again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
