"""A ceiling per rig, and who may read the floor.

Both are off by default, because the platform team's gateway may already
own them and two implementations disagreeing is worse than one. What is
tested here is that they are off, that turning them on works, and - the
part that actually matters - that turning them on cannot lose an event.

The limiter is a token bucket rather than a fixed window, and the reason
is the shape of the real traffic. The floor's sustained rate is about
0.01 requests per second per rig; what actually happens is a rig coming
back from an outage and emptying its outbox as fast as it can. That is
correct behaviour and a limit sized for the average would punish it.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from core.infrastructure.config import Settings, get_settings
from services.rigs.auth import reset_limiter
from services.rigs.ratelimit import MAX_TRACKED, RateLimiter

RIG = "RIG-03"
OTHER = "RIG-01"
TOKEN = "a-token-placed-by-ansible"
DESK = "desk-secret"


class FakeClock:
    """Time that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


@asynccontextmanager
async def serving(**overrides):
    from services.rigs.app import create_app

    reset_limiter()
    base = get_settings()
    settings = Settings(**{**base.model_dump(), **overrides})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c
    reset_limiter()


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


# ------------------------------------------------------------ the bucket

def test_a_limit_of_zero_is_no_limit():
    """Off is off, so callers never have to check twice."""
    r = RateLimiter(per_minute=0)
    assert not r.enabled
    for _ in range(1000):
        assert r.allow(RIG) == 0.0


def test_the_burst_goes_through_and_then_the_average_holds():
    """The shape of a rig emptying its outbox after an outage."""
    clock = FakeClock()
    r = RateLimiter(per_minute=60, clock=clock)

    for i in range(60):
        assert r.allow(RIG) == 0.0, f"the burst was cut short at {i}"
    assert r.allow(RIG) > 0, "the bucket never ran out"


def test_it_refills_over_time():
    clock = FakeClock()
    r = RateLimiter(per_minute=60, clock=clock)      # one per second
    for _ in range(60):
        r.allow(RIG)
    assert r.allow(RIG) > 0

    clock.advance(1.0)
    assert r.allow(RIG) == 0.0, "a second should buy one request at 60/min"
    assert r.allow(RIG) > 0, "and only one"


def test_it_says_how_long_to_wait():
    """A 429 with no idea when to come back invites the tight retry loop
    the limit exists to stop."""
    clock = FakeClock()
    r = RateLimiter(per_minute=60, clock=clock)
    for _ in range(60):
        r.allow(RIG)

    wait = r.allow(RIG)
    assert 0 < wait <= 1.0, wait


def test_it_never_refills_past_the_burst():
    """A rig quiet for a week does not get a week's worth of requests."""
    clock = FakeClock()
    r = RateLimiter(per_minute=60, clock=clock)
    r.allow(RIG)
    clock.advance(86400)

    for _ in range(60):
        assert r.allow(RIG) == 0.0
    assert r.allow(RIG) > 0, "a long silence bought more than one burst"


def test_one_rig_running_hot_does_not_limit_another():
    """The one that would be a real incident: eleven rigs going quiet
    because the twelfth is in a retry loop."""
    clock = FakeClock()
    r = RateLimiter(per_minute=10, clock=clock)
    for _ in range(20):
        r.allow(RIG)
    assert r.allow(RIG) > 0

    assert r.allow(OTHER) == 0.0, "a busy rig took another rig's budget"


def test_it_will_not_track_unbounded_keys():
    """Only reachable with auth off, but that is a configuration somebody
    will run, and a limiter that can be made to allocate for ever is not
    much of a limiter."""
    r = RateLimiter(per_minute=60)
    for i in range(MAX_TRACKED):
        r.allow(f"rig-{i}")
    assert r.tracked() == MAX_TRACKED
    assert r.allow("one-too-many") > 0
    assert r.tracked() == MAX_TRACKED


def test_a_full_table_does_not_evict_an_existing_rig():
    """Evicting on churn is how a limiter gets bypassed."""
    r = RateLimiter(per_minute=1, clock=FakeClock())
    r.allow(RIG)
    assert r.allow(RIG) > 0                       # RIG is now at its limit
    for i in range(MAX_TRACKED + 10):
        r.allow(f"rig-{i}")
    assert r.allow(RIG) > 0, "a real rig's bucket was thrown away by churn"


# ------------------------------------------------------------- the route

async def test_off_by_default(engine):
    async with serving() as c:
        for _ in range(30):
            r = await c.post(f"/api/rigs/{RIG}/events", json={"events": [an_event()]})
            assert r.status_code == 200, r.text


async def test_over_the_limit_is_429_with_a_retry_after(engine):
    async with serving(rig_rate_limit_per_min=5) as c:
        codes = []
        for _ in range(10):
            r = await c.post(f"/api/rigs/{RIG}/events", json={"events": [an_event()]})
            codes.append(r.status_code)
            last = r
        assert 429 in codes, codes
        assert last.status_code == 429
        assert int(last.headers["Retry-After"]) >= 1


async def test_being_limited_loses_nothing(engine):
    """The property that makes a limiter safe here at all. A refused batch
    is kept by the rig and resent, and ingest dedupes it - so the events
    arrive late rather than not at all."""
    async with serving(rig_rate_limit_per_min=2) as c:
        events = [an_event(seq=i) for i in range(4)]

        sent = []
        for e in events:
            r = await c.post(f"/api/rigs/{RIG}/events", json={"events": [e]})
            if r.status_code == 200:
                sent.append(e)
        assert len(sent) < len(events), "nothing was actually limited"

    # The rig backs off and tries again. Same events, no limit this time.
    async with serving(rig_rate_limit_per_min=0) as c:
        r = await c.post(f"/api/rigs/{RIG}/events", json={"events": events})
        assert r.status_code == 200
        body = r.json()
        assert body["accepted"] + body["duplicates"] == len(events)
        assert body["duplicates"] == len(sent), (
            "the ones that got through the limit were not recognised on retry"
        )


async def test_the_limit_is_per_rig_over_http(engine):
    async with serving(rig_rate_limit_per_min=3) as c:
        for _ in range(8):
            await c.post(f"/api/rigs/{RIG}/events", json={"events": [an_event()]})
        blocked = await c.post(f"/api/rigs/{RIG}/events", json={"events": [an_event()]})
        assert blocked.status_code == 429

        other = await c.post(f"/api/rigs/{OTHER}/events",
                             json={"events": [an_event(rig_id=OTHER)]})
        assert other.status_code == 200, "one rig's limit stopped another rig"


@pytest.mark.parametrize("method,path", [
    ("GET", f"/api/rigs/{RIG}/cursor"),
    ("POST", f"/api/rigs/{RIG}/heartbeat"),
    ("GET", f"/api/rigs/{RIG}/schedule"),
])
async def test_every_rig_facing_route_shares_the_ceiling(engine, method, path):
    """One rig, one budget - not one budget per route, which would be
    twelve times the ceiling somebody thought they set."""
    async with serving(rig_rate_limit_per_min=3) as c:
        for _ in range(6):
            await c.post(f"/api/rigs/{RIG}/events", json={"events": [an_event()]})
        r = await c.request(method, path, json={"at": "2026-08-24T09:00:00Z"})
        assert r.status_code == 429, f"{method} {path} had a budget of its own"


async def test_the_limit_applies_after_auth(engine):
    """An unauthorised caller should not be able to fill a real rig's
    bucket on its behalf."""
    async with serving(rig_rate_limit_per_min=2, rig_tokens={RIG: TOKEN}) as c:
        for _ in range(10):
            r = await c.post(f"/api/rigs/{RIG}/events", json={"events": [an_event()]})
            assert r.status_code == 401

        ok = await c.post(f"/api/rigs/{RIG}/events",
                          json={"events": [an_event()]}, headers=bearer(TOKEN))
        assert ok.status_code == 200, "a stranger spent the rig's budget"


# --------------------------------------------------------- reading the floor

async def test_floor_reads_are_open_by_default(engine):
    """Even with a desk token set. Reading and writing are different
    questions, and a wall display should not need a secret unless somebody
    decides it does."""
    async with serving(desk_token=DESK) as c:
        assert (await c.get("/api/floor/state")).status_code == 200
        assert (await c.get("/api/floor/alerts")).status_code == 200
        assert (await c.post("/api/schedules/push",
                             json={"payloads": [{}]})).status_code == 401


@pytest.mark.parametrize("path", [
    "/api/floor/state", "/api/floor/alerts", "/api/floor/video", "/api/state",
])
async def test_floor_reads_can_be_protected(engine, path):
    async with serving(desk_token=DESK, protect_floor_reads=True) as c:
        assert (await c.get(path)).status_code == 401, path
        assert (await c.get(path, headers=bearer(DESK))).status_code == 200, path
        assert (await c.get(path, headers=bearer("wrong"))).status_code == 401, path


async def test_protecting_reads_without_a_token_protects_nothing(engine):
    """There is nothing to check against, so it stays open rather than
    locking everyone out of a board with a secret nobody was given."""
    async with serving(desk_token="", protect_floor_reads=True) as c:
        assert (await c.get("/api/floor/state")).status_code == 200


async def test_health_says_which_of_the_four_are_off(engine):
    async with serving() as c:
        body = (await c.get("/api/health")).json()
        assert body["rigAuth"] == "off"
        assert body["deskAuth"] == "off"
        assert body["floorReads"] == "open"
        assert body["rigRateLimit"] == "off"


async def test_health_says_which_of_the_four_are_on(engine):
    async with serving(rig_tokens={RIG: TOKEN}, desk_token=DESK,
                       protect_floor_reads=True, rig_rate_limit_per_min=120) as c:
        body = (await c.get("/api/health")).json()
        assert body["rigAuth"] == "on"
        assert body["deskAuth"] == "on"
        assert body["floorReads"] == "protected"
        assert body["rigRateLimit"] == "120/min"


async def test_health_itself_is_never_gated(engine):
    """A health check that needs a credential is a health check that
    reports the load balancer's configuration, not the service's."""
    async with serving(rig_tokens={RIG: TOKEN}, desk_token=DESK,
                       protect_floor_reads=True, rig_rate_limit_per_min=1) as c:
        for _ in range(20):
            assert (await c.get("/api/health")).status_code == 200
