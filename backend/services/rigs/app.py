"""The rigs service as a FastAPI sub-app, mounted by a gateway.

This module and everything under `core/` are written to be copied into
the platform team's tree untouched. `local_gateway.py` at the root of
this folder is NOT part of that - their gateway already exists.
"""

from fastapi import FastAPI

from core.infrastructure.config import get_settings
from services.rigs.auth import announce
from services.rigs.observability import install
from services.rigs.routes import router


DESCRIPTION = """The desk pushes a schedule out to the floor. This service is what comes
back. Three properties are worth knowing before reading the routes,
because none of them is visible in a list of endpoints:

**The ledger is the system.** `POST /rigs/{rig_id}/events` appends to an
append-only table and nothing else is authoritative. Every other table
here - episodes, shift checks, downtime, productivity blocks, sessions -
is *derived* from it and can be dropped and rebuilt. Replay is the
property the whole design is arranged around.

**Sending the same events twice is safe.** Events are unique on
`(rigId, eventId)`, so a rig that loses its connection mid-batch retries
the whole thing blind. A resend reports `accepted: 0`. `GET /cursor`
tells a rig where it got to without it having to remember.

**Measurements, not conclusions.** No percentage is ever stored. A
productivity block keeps four seconds columns - recorded, assigned,
fault, down - and efficiency is computed at read time from one
definition, so correcting the formula corrects every shift ever
recorded.

Times in a pushed schedule are floor wall-clock, and the zone they
belong to travels with them in `shift.tz`. The rotation itself is
computed in exactly one shared JavaScript file that is not this service;
here a schedule is stored as it arrived and read back, never derived.
"""

TAGS = [
    {"name": "ingest", "description":
     "The return arrow. What a rig sends: events, and a heartbeat so "
     "silence can be noticed."},
    {"name": "schedules", "description":
     "What the desk pushes out, stored as it arrived and read back."},
    {"name": "floor", "description":
     "What a manager reads. Includes the things nothing publishes - a rig "
     "nobody can hear, a turn nobody arrived for."},
    {"name": "video", "description":
     "Where the bytes go. The rule that matters: a rig may delete its own "
     "copy only after this service has verified what landed."},
    {"name": "service", "description": "Liveness."},
]


def create_app() -> FastAPI:
    app = FastAPI(
        title="Rigs",
        version="0.1.0",
        summary="Episodes, faults and downtime from twelve teleop rigs.",
        description=DESCRIPTION,
        openapi_tags=TAGS,
    )
    install(app)
    app.include_router(router, prefix="/api")
    # Before the first request, not after the first incident.
    announce(get_settings())
    return app


app = create_app()
