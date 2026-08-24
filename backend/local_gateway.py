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

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from local_dev_routes import dev
from services.rigs.app import create_app

REPO = Path(__file__).resolve().parent.parent

app = FastAPI(title="rigs (local dev gateway)")

# The static server on 8765 is still a valid way to run the apps, so a
# rig served from there can still reach this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8765", "http://localhost:8765"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
