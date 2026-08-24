"""Who may speak for a rig, and what the service says about itself.

The plan settles the rule: a per-rig token placed by Ansible, checked on
ingest; the machine authenticates and the operator never does. What is
worth testing is not that a good token works - it is the two ways this
goes quietly wrong:

  * one rig's token speaking for another rig, which would let a single
    compromised machine attribute work across the whole floor
  * auth being off with nothing saying so, which looks exactly like auth
    being on until the day somebody checks
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from core.infrastructure.config import Settings, get_settings
from core.infrastructure.database import get_session

RIG = "RIG-03"
OTHER = "RIG-01"
TOKEN = "a-token-placed-by-ansible"
OTHER_TOKEN = "a-different-token-entirely"
TOKENS = {RIG: TOKEN, OTHER: OTHER_TOKEN}


@asynccontextmanager
async def serving(**overrides):
    """The service, with settings of our choosing. Yields (client, app)."""
    from services.rigs.app import create_app

    base = get_settings()
    settings = Settings(**{**base.model_dump(), **overrides})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c, app


def an_event(rig_id=RIG, seq=0):
    return {
        "eventId": str(uuid.uuid4()), "seq": seq,
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "rigId": rig_id, "shiftDate": "2026-08-24", "shiftLabel": "Morning",
        "turnFrom": "09:00", "operatorId": "op-a4",
        "bucket": "episodes", "event": "episode_saved",
        "data": {"episodeId": str(uuid.uuid4()), "durationSecs": 92, "score": 4},
    }


def bearer(token):
    return {"Authorization": "Bearer " + token}


# ------------------------------------------------------------ off, and loud

async def test_with_no_tokens_configured_a_rig_is_trusted(engine):
    """Right for a laptop demo. The point is that it is a configuration
    rather than an accident."""
    async with serving(rig_tokens={}) as (c, _):
        r = await c.post("/api/rigs/" + RIG + "/events", json={"events": [an_event()]})
        assert r.status_code == 200, r.text


async def test_health_says_when_auth_is_off(engine):
    async with serving(rig_tokens={}, desk_token="") as (c, _):
        body = (await c.get("/api/health")).json()
        assert body["rigAuth"] == "off"
        assert body["deskAuth"] == "off"


async def test_health_says_when_auth_is_on(engine):
    async with serving(rig_tokens=TOKENS, desk_token="d") as (c, _):
        body = (await c.get("/api/health")).json()
        assert body["rigAuth"] == "on"
        assert body["deskAuth"] == "on"


# ------------------------------------------------------------------ the rule

async def test_a_rig_with_its_own_token_may_file(engine):
    async with serving(rig_tokens=TOKENS) as (c, _):
        r = await c.post("/api/rigs/" + RIG + "/events",
                         json={"events": [an_event()]}, headers=bearer(TOKEN))
        assert r.status_code == 200, r.text


async def test_no_token_is_refused(engine):
    async with serving(rig_tokens=TOKENS) as (c, _):
        r = await c.post("/api/rigs/" + RIG + "/events", json={"events": [an_event()]})
        assert r.status_code == 401


async def test_a_wrong_token_is_refused(engine):
    async with serving(rig_tokens=TOKENS) as (c, _):
        r = await c.post("/api/rigs/" + RIG + "/events",
                         json={"events": [an_event()]}, headers=bearer("nope"))
        assert r.status_code == 401


async def test_one_rigs_token_cannot_speak_for_another(engine):
    """The one that matters. A valid token is not a floor-wide pass, or one
    compromised machine could attribute work for all twelve."""
    async with serving(rig_tokens=TOKENS) as (c, _):
        r = await c.post("/api/rigs/" + OTHER + "/events",
                         json={"events": [an_event(rig_id=OTHER)]},
                         headers=bearer(TOKEN))
        assert r.status_code == 401, "one rig's token filed events for another"


async def test_a_rig_with_no_configured_token_is_refused(engine):
    """A rig nobody provisioned is not a rig that may file."""
    async with serving(rig_tokens={RIG: TOKEN}) as (c, _):
        r = await c.post("/api/rigs/RIG-07/events",
                         json={"events": [an_event(rig_id="RIG-07")]},
                         headers=bearer(TOKEN))
        assert r.status_code == 401


@pytest.mark.parametrize("header", [
    "",                       # empty
    TOKEN,                    # no scheme
    "Basic " + TOKEN,         # wrong scheme
    "Bearer",                 # scheme, no token
    "Bearer   ",              # scheme, blank token
])
async def test_a_malformed_authorization_header_is_refused(engine, header):
    async with serving(rig_tokens=TOKENS) as (c, _):
        r = await c.post("/api/rigs/" + RIG + "/events", json={"events": [an_event()]},
                         headers={"Authorization": header})
        assert r.status_code == 401, "accepted %r" % header


async def test_the_scheme_is_read_case_insensitively(engine):
    """RFC 7235 makes the scheme case-insensitive, and clients differ."""
    async with serving(rig_tokens=TOKENS) as (c, _):
        r = await c.post("/api/rigs/" + RIG + "/events", json={"events": [an_event()]},
                         headers={"Authorization": "bearer " + TOKEN})
        assert r.status_code == 200, r.text


async def test_the_refusal_does_not_say_which_half_was_wrong(engine):
    """An unknown rig and a wrong token get the same answer. Anything else
    is an oracle for which rigs exist."""
    async with serving(rig_tokens=TOKENS) as (c, _):
        wrong_token = await c.post("/api/rigs/" + RIG + "/events",
                                   json={"events": [an_event()]}, headers=bearer("x"))
        unknown_rig = await c.post("/api/rigs/RIG-99/events",
                                   json={"events": [an_event(rig_id="RIG-99")]},
                                   headers=bearer("x"))
        assert wrong_token.json()["detail"] == unknown_rig.json()["detail"]


# ------------------------------------------------- every rig-facing route

@pytest.mark.parametrize("method,path", [
    ("GET", "/api/rigs/" + RIG + "/cursor"),
    ("POST", "/api/rigs/" + RIG + "/heartbeat"),
    ("GET", "/api/rigs/" + RIG + "/schedule"),
    ("GET", "/api/rigs/" + RIG + "/schedule.json"),
    ("POST", "/api/rigs/" + RIG + "/episodes/" + str(uuid.uuid4()) + "/video:presign"),
    ("POST", "/api/rigs/" + RIG + "/episodes/" + str(uuid.uuid4()) + "/video:complete"),
])
async def test_every_rig_facing_route_is_guarded(engine, method, path):
    """Guarding ingest and forgetting the rest would leave the schedule and
    the video path open on the same machine."""
    async with serving(rig_tokens=TOKENS) as (c, _):
        r = await c.request(method, path, json={})
        assert r.status_code == 401, method + " " + path + " was not guarded"


async def test_the_storage_put_is_guarded_by_the_rig_in_its_key(engine):
    """That route's path is an object key rather than a rig - and keys begin
    with the rig that owns them, so a rig may write under its own prefix and
    nowhere else."""
    async with serving(rig_tokens=TOKENS) as (c, _):
        mine = "/api/storage/" + RIG + "/" + str(uuid.uuid4()) + "/front.mp4"
        theirs = "/api/storage/" + OTHER + "/" + str(uuid.uuid4()) + "/front.mp4"

        assert (await c.put(mine, content=b"x")).status_code == 401, "no header"
        r = await c.put(theirs, content=b"x", headers=bearer(TOKEN))
        assert r.status_code == 401, "a rig wrote under another rig's prefix"
        good = await c.put(mine, content=b"x", headers=bearer(TOKEN))
        assert good.status_code != 401


async def test_a_key_that_names_no_rig_is_refused(engine):
    async with serving(rig_tokens=TOKENS) as (c, _):
        r = await c.put("/api/storage/loose-file.mp4", content=b"x",
                        headers=bearer(TOKEN))
        assert r.status_code == 400


# ------------------------------------------------------------------ the desk

async def test_a_push_is_open_when_no_desk_token_is_set(engine):
    async with serving(desk_token="") as (c, _):
        r = await c.post("/api/schedules/push", json={"payloads": [{}]})
        assert r.status_code != 401


async def test_a_push_needs_the_desk_token_when_one_is_set(engine):
    async with serving(desk_token="desk-secret") as (c, _):
        assert (await c.post("/api/schedules/push",
                             json={"payloads": [{}]})).status_code == 401
        assert (await c.post("/api/push", json={"payloads": [{}]},
                             headers=bearer("wrong"))).status_code == 401
        ok = await c.post("/api/schedules/push", json={"payloads": [{}]},
                          headers=bearer("desk-secret"))
        assert ok.status_code != 401


# ------------------------------------------------------------------- health

async def test_health_is_503_when_the_database_is_unreachable(engine):
    """It used to answer ok:true without touching anything, which means it
    answered ok:true with the database on fire."""

    class Dead:
        async def execute(self, *a, **k):
            raise OSError("connection refused")

    async with serving() as (c, app):
        app.dependency_overrides[get_session] = lambda: Dead()
        r = await c.get("/api/health")
        assert r.status_code == 503
        assert r.json()["detail"]["ok"] is False


async def test_health_is_200_when_it_can_reach_the_database(engine):
    async with serving() as (c, _):
        r = await c.get("/api/health")
        assert r.status_code == 200 and r.json()["ok"] is True
