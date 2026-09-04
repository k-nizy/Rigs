"""Helpers for driving the service by hand and from the end-to-end script.

NOT part of the handover. Mounted only by local_gateway.py, which does
not lift either. Running a projection on demand and reading fact rows
back are things a test harness wants and a floor does not.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domains.episodes.model import Episode
from core.infrastructure.database import get_session
from core.workflows.floor import sweep
from core.workflows.projection import project_batch, reset_projections

# include_in_schema=False: these are a test harness, and a published
# contract that lists them invites somebody to build on one.
dev = APIRouter(prefix="/api/dev", tags=["dev"], include_in_schema=False)


@dev.post("/project")
async def project(session: AsyncSession = Depends(get_session)) -> dict:
    count, done = await project_batch(session, limit=1000)
    return {"projected": count, "actions": done}


@dev.post("/replay")
async def replay(session: AsyncSession = Depends(get_session)) -> dict:
    await reset_projections(session)
    count, _ = await project_batch(session, limit=5000)
    return {"rebuilt": count}


@dev.post("/sweep")
async def sweep_now(session: AsyncSession = Depends(get_session)) -> dict:
    return await sweep(session)


@dev.get("/episodes")
async def episodes(rig_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    rows = await session.execute(
        select(Episode).where(Episode.rig_id == rig_id).order_by(Episode.at)
    )
    return {
        "episodes": [
            {
                "episodeId": str(e.episode_id), "outcome": e.outcome, "score": e.score,
                "durationSecs": e.duration_secs, "operatorId": e.operator_id,
                "operatorName": e.operator_name,
                "turnFrom": e.turn_from, "at": e.at.isoformat(),
            }
            for e in rows.scalars().all()
        ]
    }
