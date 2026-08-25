"""A gateway for local development only. NOT part of the handover.

The platform team's tree already has a gateway that mounts every service,
handles CORS and rate limiting. Shipping a second one would be inventing
a thing they already own. This exists so `core/` and `services/rigs/` can
be run and tested here before they are lifted across.

It also serves the two static apps, which their gateway will not: on the
floor the desk and the rig are static files on a web server and the
service is behind it. Serving both from one origin here means the rig's
relative fetches reach the real API, so a pedal press travels the whole
way rather than the two halves being demonstrated separately.

    python -m uvicorn local_gateway:app --reload --port 8000
"""

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from core.infrastructure.config import get_settings
from local_dev_routes import dev
from services.rigs.app import DESCRIPTION, TAGS, create_app
from services.rigs.observability import configure as configure_logging
from services.rigs.observability import install as install_observability

from workers.drain_to_archive import run as drain_run
from workers.project_events import run as project_run
from workers.sweep_floor import run as sweep_run

REPO = Path(__file__).resolve().parent.parent
log = logging.getLogger("local_gateway")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    """Run the three workers beside the service, for local development.

    On the floor these are separate processes - they already take
    `--every-secs`, handle their own backoff and shut down on a signal,
    and the platform team runs long-lived work that way. Nothing here
    changes that.

    But a developer who has to remember four terminals will forget one,
    and the failure is silent and confusing: events land in the ledger and
    never become facts, alerts never open, and the floor board looks
    broken for no visible reason. That happened often enough to be worth
    removing. This is the dev harness, so it is the right place for it.
    """
    # Logging that says which request a line belongs to. In their tree the
    # gateway owns this; here nothing else would set it up.
    configure_logging()
    tasks = [
        asyncio.create_task(project_run(poll_secs=2.0, batch=500,
                                        exit_after_empty=0, replay=False),
                            name="project_events"),
        asyncio.create_task(sweep_run(every_secs=15.0, once=False,
                                      silent_after=180, idle_grace=300),
                            name="sweep_floor"),
        asyncio.create_task(drain_run(poll_secs=30.0, batch=50,
                                      exit_after_empty=0,
                                      keep_days=get_settings().video_keep_days,
                                      pending_days=get_settings().video_pending_after_days),
                            name="drain_to_archive"),
    ]
    log.info("workers running: %s", ", ".join(t.get_name() for t in tasks))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        # return_exceptions: a cancelled task raises, and one worker
        # refusing to stop must not hold the others open.
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("workers stopped")


# The published contract is the service's, not this harness's. Mirrored
# rather than reinvented: include_router copies routes and leaves the
# OpenAPI metadata behind, so /docs here would otherwise describe a dev
# gateway that does not lift instead of the thing being handed over.
app = FastAPI(
    title="Rigs",
    version="0.1.0",
    summary="Episodes, faults and downtime from twelve teleop rigs.",
    description=DESCRIPTION,
    openapi_tags=TAGS,
    lifespan=lifespan,
)

# The static server on 8765 is still a valid way to run the apps, so a
# rig served from there can still reach this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8765", "http://localhost:8765"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# include_router carries routes and nothing else. That has now cost two
# things: the OpenAPI metadata below, and the request id and exception
# handlers here - both of which are correct on the service app that
# actually lifts, and both of which silently were not on this harness.
# Anything that is not a route has to be repeated on purpose.
install_observability(app)

service = create_app()
# Dev-only helpers, on the gateway rather than in the service, so the
# thing that lifts into their tree carries none of them.
service.include_router(dev)
# Included, not mounted: the service's routes already carry their own
# /api prefix, and mounting them under /api again gives /api/api.
app.include_router(service.router)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/rotation-desk-v1/")


# Mounted after the API, so nothing static can shadow a route.
for folder in ("apps", "packages", "rotation-desk-v1"):
    app.mount(
        f"/{folder}",
        StaticFiles(directory=REPO / folder, html=True),
        name=folder,
    )
