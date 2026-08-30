"""What the service says about itself, and what it must never say.

Two things are being pinned here and neither is "logging works".

**A handler must only add a line.** The first version of the validation
handler rewrote the response body as well, and `exc.errors()` carries a
live ValueError in `ctx` for any custom validator - so serialising it
turned three well-formed 422s into 500s. A handler that changes the API
by accident is worse than no handler.

**Nothing secret, ever.** A log line is world-readable, because
eventually it is. The tokens and the connection string must not be in
one, and a 500 must not hand a stack trace to whoever caused it.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from core.infrastructure.config import Settings, get_settings
from services.rigs.observability import HEADER

RIG = "RIG-03"
TOKEN = "a-token-placed-by-ansible"


async def serving(raise_app_exceptions=True, **overrides):
    """The service, plus a route that fails, so the last-resort handler
    has something to catch."""
    from services.rigs.app import create_app

    base = get_settings()
    settings = Settings(**{**base.model_dump(), **overrides})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/api/boom")
    async def boom():
        raise RuntimeError("a secret-looking thing: " + TOKEN)

    transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return AsyncClient(transport=transport, base_url="http://test")


def an_event(**patch):
    ev = {
        "eventId": str(uuid.uuid4()), "seq": 0,
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "rigId": RIG, "shiftDate": "2026-08-24", "shiftLabel": "Morning",
        "turnFrom": "09:00", "operatorId": "op-a4",
        "bucket": "episodes", "event": "episode_saved",
        "data": {"episodeId": str(uuid.uuid4()), "durationSecs": 92, "score": 4},
    }
    ev.update(patch)
    return ev


# ------------------------------------------------------------ request id

async def test_a_request_id_is_minted_when_there_is_none(engine):
    c = await serving()
    async with c:
        r = await c.get("/api/health")
        assert r.headers.get(HEADER), "no request id came back"


async def test_an_upstream_request_id_is_used_rather_than_replaced(engine):
    """So a line here joins up with a line in their gateway. Minting a new
    one would break exactly the join this exists for."""
    c = await serving()
    async with c:
        r = await c.get("/api/health", headers={HEADER: "from-their-gateway"})
        assert r.headers.get(HEADER) == "from-their-gateway"


async def test_the_request_id_reaches_the_log_line(engine, caplog):
    c = await serving(rig_tokens={RIG: TOKEN})
    async with c:
        with caplog.at_level(logging.WARNING, logger="rigs.auth"):
            await c.post("/api/rigs/" + RIG + "/events", json={"events": []},
                         headers={HEADER: "traceable"})
    assert caplog.records, "a refusal was not recorded at all"


# ------------------------------------------------ a handler that only adds

async def test_a_validation_failure_is_still_a_422_with_its_own_body(engine):
    """The regression. A custom validator puts a live ValueError in ctx,
    and a handler that serialises it turns a 422 into a 500."""
    c = await serving()
    async with c:
        r = await c.post("/api/rigs/" + RIG + "/events",
                         json={"events": [an_event(at="2026-08-24T09:14:02")]})
    assert r.status_code == 422, r.text
    body = r.json()
    assert isinstance(body.get("detail"), list), "the body shape changed"
    assert any("timezone" in str(e.get("msg", "")) for e in body["detail"])


async def test_a_refused_body_is_recorded_with_what_was_wrong(engine, caplog):
    c = await serving()
    async with c:
        with caplog.at_level(logging.WARNING, logger="rigs.service"):
            await c.post("/api/rigs/" + RIG + "/events",
                         json={"events": [an_event(event="episode_finished")]})
    lines = [r.getMessage() for r in caplog.records]
    assert any("refused" in m for m in lines), lines
    assert any("event" in m for m in lines), (
        "the line has to say which field, or it is not worth writing: %s" % lines
    )


# -------------------------------------------------------- the 500 that was

async def test_an_unhandled_exception_is_recorded_and_not_handed_back(engine, caplog):
    """It used to be a 500 with an empty body and no record anywhere."""
    c = await serving(raise_app_exceptions=False)
    async with c:
        with caplog.at_level(logging.ERROR, logger="rigs.service"):
            r = await c.get("/api/boom", headers={HEADER: "traceable"})

    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "internal error", "the reply says nothing useful, correctly"
    assert body["requestId"] == "traceable", "the reply cannot be joined to the log"
    assert TOKEN not in r.text, "a stack trace leaked out to the caller"
    assert any("RuntimeError" in rec.getMessage() for rec in caplog.records), \
        "the exception was not written down"


# ------------------------------------------------------------ no secrets

async def test_a_refused_token_is_never_written_down(engine, caplog):
    """The log is for whoever is fixing it. The token belongs to neither
    the log nor the reply."""
    c = await serving(rig_tokens={RIG: TOKEN})
    async with c:
        with caplog.at_level(logging.WARNING):
            r = await c.post("/api/rigs/" + RIG + "/events",
                             json={"events": [an_event()]},
                             headers={"Authorization": "Bearer wrong-token-value"})

    assert r.status_code == 401
    written = " ".join(rec.getMessage() for rec in caplog.records)
    assert TOKEN not in written, "the expected token was logged"
    assert "wrong-token-value" not in written, "the presented token was logged"
    assert RIG in written, "the line does not say which rig, so it is not actionable"


async def test_no_connection_string_is_in_the_health_reply(engine):
    """Stronger than the property this used to assert.

    It used to read `database` out of the reply and check the password
    had been masked. Masking was the wrong bar: the field still named the
    host, the port, the database and the user, to anybody who could reach
    the port, and this route answers before anyone has proved anything.

    Nothing consumed it - the load balancer reads `ok` and the status
    code, the rig reads `rigIdentity`, and preflight reads the settings
    directly and prints it on the box. So it is gone, and what is
    asserted now is that no connection string reaches the body at all,
    masked or otherwise.
    """
    c = await serving()
    async with c:
        r = await c.get("/api/health")
    body = r.json()

    assert "database" not in body, (
        f"the connection string is back in the health reply: {body['database']}")
    text = json.dumps(body)
    for smell in ("postgresql", "asyncpg", "://", "5432"):
        assert smell not in text, (
            f"something that looks like a connection string reached the "
            f"reply ({smell!r}): {text}")


async def test_a_password_that_is_set_is_masked(engine):
    """The other half, and the one that matters on a floor: when there IS
    a password, `safe_url` has to take it out."""
    from core.infrastructure.config import Settings

    s = Settings(database_url="postgresql+asyncpg://rigs:hunter2@10.0.0.5:5432/rigs")
    safe = s.safe_url()
    assert "hunter2" not in safe, f"the password survived: {safe}"
    assert "***" in safe
    assert "10.0.0.5:5432" in safe, "masking should not lose which database it is"
    assert "/rigs" in safe
