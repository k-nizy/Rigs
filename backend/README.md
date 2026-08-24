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
- **2** — the five fact domains, `core/rules`, the projection worker.
  Replay proven by wiping the facts and rebuilding them.
- **3** — `sweep_floor`, `/floor/state`, `/floor/alerts`. An absence has
  no event to subscribe to, so it has to be swept for.
- **4** — video: presigned upload straight to MinIO, checksum, then the
  drain to R2. Bulk bytes never pass through an application worker.

## Still open with the platform team

Seven domains or one `rigs` domain with seven models (their question
1.2); repo access and the contribution route; where Alembic migrations
live for a new service; and how 2.6 TB/day of video actually reaches R2.
