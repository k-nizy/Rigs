"""The rigs service as a FastAPI sub-app, mounted by a gateway.

This module and everything under `core/` are written to be copied into
the platform team's tree untouched. `local_gateway.py` at the root of
this folder is NOT part of that - their gateway already exists.
"""

from fastapi import FastAPI

from services.rigs.routes import router


def create_app() -> FastAPI:
    app = FastAPI(
        title="rigs",
        description="The return arrow: episodes, faults and downtime from twelve teleop rigs.",
    )
    app.include_router(router, prefix="/api")
    return app


app = create_app()
