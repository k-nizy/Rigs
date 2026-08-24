"""A gateway for local development only. NOT part of the handover.

The platform team's tree already has a gateway that mounts every service,
handles CORS and rate limiting. Shipping a second one would be inventing
a thing they already own. This exists so `core/` and `services/rigs/` can
be run and tested here before they are lifted across.

    python -m uvicorn local_gateway:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from local_dev_routes import dev
from services.rigs.app import create_app

app = FastAPI(title="rigs (local dev gateway)")

# The desk and the rig are served from the static tree on 8765.
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
app.mount("", service)
