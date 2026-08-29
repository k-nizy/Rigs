"""Who changed the floor's day.

Every event a rig files is attributable - rig, shift, turn, operator.
Nothing a manager did was. A push rewrites what twelve machines run for
a whole calendar day, and "who put the floor on this schedule" is a
question asked only after something has gone wrong, which is exactly
when it needs to have been recorded already.

Two properties beyond the recording itself:

**The name is a snapshot.** A record that re-renders through a live join
changes when somebody is renamed, and a history that rewrites itself is
not a history.

**The gate did not move.** Recording the actor meant taking
`require_manager` out of the route's dependency list and making it a
parameter, so the account itself is available. That is security wiring,
so the refusals are asserted here again rather than assumed to have
survived.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from core.domains.accounts.model import Account
from core.domains.accounts.passwords import hash_password
from core.domains.schedules.model import Schedule, SchedulePush
from core.domains.schedules.repository import SchedulePushRepository
from core.infrastructure.config import Settings, get_settings
from services.rigs.people import CSRF_HEADER, reset_login_limiter

import pytest

PASSWORD = "a-real-password-12"
MANAGER = "r.osei@verlet.co"
OPERATOR = "m.chen@verlet.co"
DAY = date(2026, 8, 29)


@pytest.fixture(autouse=True)
def _fresh():
    reset_login_limiter()
    yield
    reset_login_limiter()


@asynccontextmanager
async def serving(caller="10.0.0.2", **overrides):
    from services.rigs.app import create_app

    base = get_settings()
    settings = Settings(**{**base.model_dump(),
                           "session_cookie_secure": False, **overrides})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(
        transport=ASGITransport(app=app, client=(caller, 51234)),
        base_url="http://test",
    ) as c:
        yield c


async def accounts(session, operator=False):
    rows = [Account(email=MANAGER, name="Ruth Osei", role="manager",
                    password_hash=hash_password(PASSWORD))]
    if operator:
        rows.append(Account(email=OPERATOR, name="Mei Chen", role="operator",
                            operator_id="op-a2",
                            password_hash=hash_password(PASSWORD)))
    session.add_all(rows)
    await session.commit()
    return rows[0]


async def signed_in(client, email=MANAGER):
    r = await client.post("/api/auth/login",
                          json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrfToken"]


def payload(rig="RIG-01", label="Morning", day=DAY):
    return {
        "rigId": rig, "group": "A", "task": "Box transfer",
        "shift": {"label": label, "date": day.isoformat(),
                  "start": "08:00", "end": "16:00", "tz": "UTC"},
        "turns": [],
    }


def a_day():
    """Twelve rigs across three shifts, as the desk actually sends."""
    return [payload(f"RIG-{i:02d}", label)
            for label in ("Morning", "Day", "Night")
            for i in range(1, 13)]


# ------------------------------------------------------ what is recorded


class TestThePushIsAttributed:
    async def test_a_manager_push_records_who(self, engine, session):
        who = await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            r = await c.post("/api/push", json={"payloads": [payload()]},
                             headers={CSRF_HEADER: csrf})
        assert r.status_code == 200

        row = (await session.execute(select(SchedulePush))).scalars().one()
        assert row.actor_kind == "manager"
        assert row.actor_email == MANAGER
        assert row.actor_name == "Ruth Osei"
        assert row.account_id == who.id

    async def test_it_records_where_from(self, engine, session):
        await accounts(session)
        async with serving(caller="10.0.0.77") as c:
            csrf = await signed_in(c)
            await c.post("/api/push", json={"payloads": [payload()]},
                         headers={CSRF_HEADER: csrf})
        row = (await session.execute(select(SchedulePush))).scalars().one()
        assert row.address == "10.0.0.77"

    async def test_the_audit_row_and_the_schedules_share_a_push_id(
            self, engine, session):
        """So a schedule row on a rig traces back to the person who put
        it there, without either table owning the other."""
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            r = await c.post("/api/push", json={"payloads": a_day()},
                             headers={CSRF_HEADER: csrf})
        push_id = uuid.UUID(r.json()["pushId"])

        audit = await SchedulePushRepository(session).for_push(push_id)
        assert audit is not None

        scheds = (await session.execute(
            select(Schedule).where(Schedule.push_id == push_id))).scalars().all()
        assert len(scheds) == 36
        assert {s.push_id for s in scheds} == {audit.push_id}

    async def test_it_records_what_the_push_covered(self, engine, session):
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            await c.post("/api/push", json={"payloads": a_day()},
                         headers={CSRF_HEADER: csrf})
        row = (await session.execute(select(SchedulePush))).scalars().one()
        assert row.covered["payloads"] == 36
        assert row.covered["rigs"] == 12
        assert row.covered["dates"] == [DAY.isoformat()]
        assert sorted(row.covered["shifts"]) == ["Day", "Morning", "Night"]

    async def test_the_name_is_a_snapshot_not_a_lookup(self, engine, session):
        """A record that renders through a live join changes when somebody
        is renamed. A history that rewrites itself is not a history."""
        who = await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            await c.post("/api/push", json={"payloads": [payload()]},
                         headers={CSRF_HEADER: csrf})

        who.name = "Ruth Osei-Bonsu"
        who.email = "r.osei-bonsu@verlet.co"
        await session.commit()

        row = (await session.execute(select(SchedulePush))).scalars().one()
        assert row.actor_name == "Ruth Osei", "the audit row followed a rename"
        assert row.actor_email == MANAGER

    async def test_two_pushes_are_two_rows(self, engine, session):
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            for _ in range(3):
                await c.post("/api/push", json={"payloads": [payload()]},
                             headers={CSRF_HEADER: csrf})
        rows = (await session.execute(select(SchedulePush))).scalars().all()
        assert len(rows) == 3
        assert len({r.push_id for r in rows}) == 3


class TestSomethingThatIsNotAPerson:
    TOKEN = "the-desk-token"

    async def test_the_desk_token_is_not_recorded_as_somebody(
            self, engine, session):
        """A shared secret is not a person and must not be filed as one."""
        async with serving(desk_token=self.TOKEN) as c:
            r = await c.post("/api/push", json={"payloads": [payload()]},
                             headers={"Authorization": "Bearer " + self.TOKEN})
        assert r.status_code == 200

        row = (await session.execute(select(SchedulePush))).scalars().one()
        assert row.actor_kind == "token"
        assert row.actor_email is None
        assert row.actor_name is None
        assert row.account_id is None

    async def test_an_unconfigured_deployment_says_so(self, engine, session):
        """"open" is written down rather than left looking like a manager
        whose name nobody captured."""
        async with serving() as c:
            await c.post("/api/push", json={"payloads": [payload()]})
        row = (await session.execute(select(SchedulePush))).scalars().one()
        assert row.actor_kind == "open"


class TestARejectedPushLeavesNothing:
    async def test_a_malformed_push_records_no_row(self, engine, session):
        """The floor did not change, so nothing should say it did."""
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            r = await c.post("/api/push",
                             json={"payloads": [{"rigId": "RIG-01"}]},
                             headers={CSRF_HEADER: csrf})
        assert r.status_code == 422
        assert (await session.execute(select(SchedulePush))).scalars().all() == []

    async def test_a_push_refused_for_who_you_are_records_no_row(
            self, engine, session):
        await accounts(session, operator=True)
        async with serving() as c:
            csrf = await signed_in(c, OPERATOR)
            r = await c.post("/api/push", json={"payloads": [payload()]},
                             headers={CSRF_HEADER: csrf})
        assert r.status_code == 403
        assert (await session.execute(select(SchedulePush))).scalars().all() == []


# ------------------------------------------------------- reading it back


class TestReadingTheHistory:
    async def test_a_manager_can_see_who_pushed(self, engine, session):
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            await c.post("/api/push", json={"payloads": a_day()},
                         headers={CSRF_HEADER: csrf})
            r = await c.get("/api/schedules/pushes")

        assert r.status_code == 200
        one = r.json()["pushes"][0]
        assert one["by"] == "Ruth Osei"
        assert one["email"] == MANAGER
        assert one["how"] == "manager"
        assert one["covered"]["rigs"] == 12

    async def test_newest_first(self, engine, session):
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            for label in ("Morning", "Day", "Night"):
                await c.post("/api/push",
                             json={"payloads": [payload(label=label)]},
                             headers={CSRF_HEADER: csrf})
            got = (await c.get("/api/schedules/pushes")).json()["pushes"]

        assert [p["covered"]["shifts"][0] for p in got] == ["Night", "Day", "Morning"]

    async def test_an_operator_cannot_read_it(self, engine, session):
        """It names people and where they were."""
        await accounts(session, operator=True)
        async with serving() as c:
            await signed_in(c, OPERATOR)
            r = await c.get("/api/schedules/pushes")
        assert r.status_code == 403

    async def test_nobody_signed_in_cannot_read_it(self, engine, session):
        await accounts(session)
        async with serving() as c:
            r = await c.get("/api/schedules/pushes")
        assert r.status_code == 401


# -------------------------------------- the gate, asserted again on purpose


class TestTheGateSurvivedTheChange:
    """`require_manager` moved from the route's dependency list to a
    parameter so the account is available to record. FastAPI resolves
    both the same way, but that is security wiring and "should be
    equivalent" is not a thing to take on trust."""

    async def test_an_operator_is_still_refused_the_push(self, engine, session):
        await accounts(session, operator=True)
        async with serving() as c:
            csrf = await signed_in(c, OPERATOR)
            r = await c.post("/api/push", json={"payloads": [payload()]},
                             headers={CSRF_HEADER: csrf})
        assert r.status_code == 403

    async def test_nobody_signed_in_is_still_refused_the_push(
            self, engine, session):
        await accounts(session)
        async with serving() as c:
            r = await c.post("/api/push", json={"payloads": [payload()]})
        assert r.status_code == 401

    async def test_csrf_is_still_required(self, engine, session):
        await accounts(session)
        async with serving() as c:
            await signed_in(c)
            r = await c.post("/api/push", json={"payloads": [payload()]})
        assert r.status_code == 403

    async def test_both_push_paths_are_gated_the_same(self, engine, session):
        await accounts(session, operator=True)
        for path in ("/api/push", "/api/schedules/push"):
            async with serving() as c:
                csrf = await signed_in(c, OPERATOR)
                r = await c.post(path, json={"payloads": [payload()]},
                                 headers={CSRF_HEADER: csrf})
            assert r.status_code == 403, path

    async def test_the_alias_records_the_same_way(self, engine, session):
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c)
            await c.post("/api/schedules/push", json={"payloads": [payload()]},
                         headers={CSRF_HEADER: csrf})
        row = (await session.execute(select(SchedulePush))).scalars().one()
        assert row.actor_email == MANAGER

    async def test_a_rig_still_needs_nothing_of_this(self, engine, session):
        await accounts(session)
        async with serving(rig_tokens={"RIG-03": "a-token"}) as c:
            r = await c.get("/api/rigs/RIG-03/cursor",
                            headers={"Authorization": "Bearer a-token"})
        assert r.status_code == 200
