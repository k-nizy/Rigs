# The rig backend

The return arrow: episodes, faults, downtime and eventually video coming
back from twelve teleop rigs. `apps/rig/docs/BACKEND-PLAN.md` is the plan
this implements and `apps/rig/docs/BACKEND-ASKS.md` records what the
platform team has settled.

## What lifts, and what does not

The backend was decided to live **inside the platform team's codebase**,
as a service module. We do not have that repository yet, so it is built
here in their exact convention, ready to be copied across.

| | |
|---|---|
| `core/` | **Lifts as-is.** Their layering, their domain shape. |
| `services/rigs/` | **Lifts as-is.** A FastAPI sub-app their gateway mounts. |
| `alembic/` | Lifts, once we know where migrations live for a new service |
| `local_gateway.py` | **Does not lift.** Their gateway already exists. |
| `tests/` | Lifts; `conftest.py` will want their fixtures instead |

Keeping that line visible is the point. A handover that includes a second
gateway is a handover that starts with an argument.

## Layout

```
core/
  base/            DeclarativeBase and BaseRepository[T] - the
                   circular-import firewall models and repos hang off
  infrastructure/  settings, engine, session
  domains/
    schedules/     the pushed payload, stored whole
    rig_events/    the append-only ledger
  rules/           pure functions - Phase 2
services/rigs/     app.py, routes.py
alembic/           migrations
tests/             pytest-asyncio, against rigs_test
```

## Two decisions worth knowing before reading the code

**The ledger comes before the projections.** Events are written verbatim
to `rig_events` first and derived into fact tables afterwards. Writing
straight into fact tables works and is quietly lossy: the moment your
reading of an event is wrong, the original is gone. It also collapses
idempotency into one constraint - `UNIQUE (rig_id, event_id)` - which is
what lets the uploader on the rig retry blindly, forever, without ever
asking whether its last attempt landed.

**The rotation is never computed here.** `packages/engine/rotation-engine.js`
is one file that both the desk and the rig load, because the rig must
never compute a different answer from the desk that scheduled it. A
Python re-implementation would be a third answer and the first one that
could silently disagree. This service stores the pushed payload and reads
it back; it never derives one. If you can delete rotation logic from this
backend and no test fails, the boundary is drawn correctly - there is
none to delete.

## Running it

```sh
cp .env.example .env          # then fill in, and create the databases
uv venv && uv pip install -e ".[dev]"
python -m alembic upgrade head
python -m pytest              # against rigs_test
lint-imports                  # the layering contract
python -m uvicorn local_gateway:app --reload --port 8000
```

## Phases

- **0 — done.** The event contract in `packages/schema`, and the rig
  emitting real envelopes instead of prose.
- **1 — done.** `schedules` and `rig_events`, ingest, cursor, heartbeat,
  idempotency proven by replaying a whole shift.
- **2 — done.** The five fact domains, `core/rules`, the projection
  worker. Replay proven by wiping the facts and rebuilding them.
- **3 — done.** `sweep_floor`, `/floor/state`, `/floor/alerts`. An
  absence has no event to subscribe to, so it has to be swept for.
- **4 — done.** Video, end to end: the rig asks where to put a take, puts
  it, reports the checksum, and lets go of its own copy only when the
  service has verified what landed. Both upload models work - a presigned
  PUT straight to the store, and a stream through the service for the
  local stand-in that has no presigning. Which one a floor uses is still
  the platform team's question, and it is the only thing behind
  `core/infrastructure/storage.py`.

What is **not** done is the Tauri shell (`MockRoda`, the real camera, the
synchronous disk journal). The rig is still a browser page; it journals to
IndexedDB, which survives a reload, a crash and a closed tab but not a
power cut in the millisecond before the write commits. Closing that last
window needs a synchronous write, which needs Tauri.

## Before it reaches a floor

Four things this service does that a deployment has to get right.

**Auth is off until it is configured.** `RIG_TOKENS` is a token per rig,
placed by Ansible. Unset, any caller may file events for any rig - right
for a laptop demo, wrong for a floor. It is not silent: startup logs it
and `/api/health` answers `{"rigAuth": "off"}`, which is what a deploy
check should fail on. Never share one token across rigs; a token names a
rig, and one that speaks for all twelve is one compromised machine away
from unattributable work.

**`/api/health` is a real check.** It runs a query and answers 503 when
the database is unreachable, so a load balancer will take a broken
instance out rather than keep feeding it.

**The workers are separate processes.** `local_gateway.py` runs them in
its own lifespan because a developer with four terminals forgets one, and
the failure is silent - events land in the ledger and never become facts.
That convenience does not lift. In their tree these are three long-lived
processes:

```sh
python -m workers.project_events        # ledger -> facts
python -m workers.sweep_floor           # absence detection, every 15s
python -m workers.drain_to_archive      # on-prem spool -> cold tier
```

**The spool has to drain.** Video is copied to the cold tier and the
on-prem copy is then released - but only after the archive is asked what
it holds, the same rule that lets a rig delete its own copy. Watch
`spool.bytes` on `/api/floor/video`: it should oscillate, not climb. A
climbing spool ends with a full disk, and that fails backwards - once the
spool is full `confirm()` refuses, so twelve rigs correctly keep their
own copies and the rig SSDs fill too.

**Archived video is deleted after 90 days.** `VIDEO_KEEP_DAYS=90`, and
that default is in `config.py`, not only in `.env`. Provision the cold
tier for roughly **245 TB** - the steady state at this plan's sizing,
reached after ninety days and flat from then on.

One consequence worth knowing before the first run: pointing a fresh
deployment at a *restored* database with episodes older than ninety days
will expire them on the first drain cycle. That is the policy doing
exactly what it says, but it is not usually what somebody restoring a
backup expects. Set `VIDEO_KEEP_DAYS=0` first if you are restoring.

**Watch `backend.projectionLagSecs` on `/api/floor/state`.** A projection
worker that has died publishes nothing - the ledger keeps accepting, the
facts stop, and every board goes on answering with yesterday's episodes.
The sweep raises `projection_behind` for it, and that is the alert that
says whether the other alerts can be believed.

**Migrations build indexes concurrently.** A plain `CREATE INDEX` locks
`rig_events` against writes, and twelve rigs cannot file events while it
runs. If one fails part-way, Postgres leaves an INVALID index behind:
drop it and run again rather than assuming it is usable.

## If the database is lost

The ledger is the system. Every other table is derived from it and can be
dropped and rebuilt, which means a backup of this service is a backup of
one table - and the format is not a private one:

**A backup is a file of envelopes.** Every ledger row keeps the envelope
the rig actually sent, and those envelopes are exactly what
`POST /api/rigs/{rig_id}/events` accepts. So a restore is a replay of the
same route a rig uses, with the same idempotency: it can be interrupted,
resumed, or run twice by two people at once.

```sql
-- the backup
COPY (SELECT envelope FROM rig_events ORDER BY rig_id, seq)
  TO '/backup/rig_events.jsonl';
```

To restore, POST them back in batches, grouped by rig, then let the
projection worker rebuild the facts. `tests/test_recovery.py` runs this
whole drill - lose everything, restore from envelopes alone, assert every
fact comes back identical - so it is a path that gets exercised rather
than one first attempted during an outage.

**Two things to know before you need this.**

Schedules are *not* in the ledger. They are pushed by the desk, so a
restore comes back with correct facts and an empty floor board. Have the
desk push again; `/api/state` will say `pushedAt: null` until it does.

Restored rows are new rows. `id`, `source_event` and `received_at` differ
from the originals - they are this database's bookkeeping, not facts
about the floor - and every fact that describes what happened on the
floor comes back identical. The test asserts exactly that distinction.

## Still open with the platform team

Seven domains or one `rigs` domain with seven models (their question
1.2); repo access and the contribution route; where Alembic migrations
live for a new service; and how 2.6 TB/day of video actually reaches R2 -
presigned straight to the store, or streamed through the gateway, and
whose checksum is believed.

That last one is the only one still shaping code, and it is held open
deliberately behind `core/infrastructure/storage.py`. Both models are
implemented and tested; `upload_target()` decides, and nothing above it
changes when the answer arrives. Every object that lands is measured, so
`/api/floor/video` replaces the plan's sizing guess with what a real
episode actually costs the moment one exists.
