"""The gate: what a manager may do, what an operator may not.

Phase 3. Everything here is a route test on purpose. A screen can hide a
button and an operator can still reach the route with curl, so the only
gate that counts is the one on this side - and the tests below all run
with the browser removed entirely.

Three properties are being protected.

**The desk is manager-only.** Push, the live board, floor-wide
efficiency. An operator who signs in and asks for any of them is refused
by the service, not by a hidden tab.

**Off until configured, exactly like every other switch here.** With no
accounts at all the service behaves as it did before there was such a
thing, so `npm run serve` and the laptop demo need no setup. One account
existing is what turns the gate on.

**A shared secret does not outrank a person.** `DESK_TOKEN` still opens
the door on a deployment with no accounts, and stops being enough the
moment there are any - otherwise the weakest credential wins and the role
check is decoration.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from core.domains.accounts.model import Account
from core.domains.accounts.passwords import hash_password
from core.domains.schedules.model import Schedule
from core.infrastructure.config import Settings, get_settings
from services.rigs.people import CSRF_COOKIE, CSRF_HEADER, reset_login_limiter

PASSWORD = "not-a-real-password-12"
MANAGER = "r.osei@verlet.co"
OPERATOR = "m.chen@verlet.co"
OTHER_OPERATOR = "t.rivera@verlet.co"

UTC = timezone.utc

# Today, because `/api/me/shift` answers "the shift running now" and a
# fixture pinned to a date in the past covers nothing. The window below
# is 00:00 to 00:00, which `shift_window` reads as crossing midnight and
# so covers the whole of the day - there is no minute of it that this
# test can fall outside.
def today() -> date:
    return datetime.now(UTC).date()

# The desk routes an operator must not reach. Each is listed once here
# and asserted against every role below, so a route added to the service
# without a decision about who may read it shows up as a gap in this
# list rather than as an open door.
DESK_READS = [
    "/api/state",
    "/api/floor/state",
    "/api/floor/alerts",
    "/api/floor/video",
    "/api/floor/efficiency?shift_date=2026-08-26&shift_label=Morning",
    # The scores every person gave their own takes. A manager may see
    # them; an operator seeing everybody's is the question the efficiency
    # route refuses to settle by accident, and this one refuses it the
    # same way.
    "/api/floor/scores?shift_date=2026-08-26&shift_label=Morning",
    # Who is on the floor is the desk's to read, for the same reason the
    # board is: it names every operator, and an operator seeing the list
    # of everybody is the question the efficiency route refuses to settle
    # by accident.
    "/api/people?q=",
    # /api/people/{id} is gated the same way but cannot sit in this list:
    # the let-through sweeps expect 200, and an id nobody has is a
    # correct 404. Its gate is asserted in TestPeopleAreTheDesksToManage.
]


@pytest.fixture(autouse=True)
def _fresh_limiter():
    reset_login_limiter()
    yield
    reset_login_limiter()


@asynccontextmanager
async def serving(**overrides):
    """See test_login.serving - `session_cookie_secure` is off here for
    the same reason: httpx will not send a Secure cookie over http."""
    from services.rigs.app import create_app

    base = get_settings()
    settings = Settings(**{**base.model_dump(),
                           "session_cookie_secure": False, **overrides})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def accounts(session, manager=True, operator=True, other=False):
    rows = []
    if manager:
        rows.append(Account(email=MANAGER, name="Ruth Osei", role="manager",
                            password_hash=hash_password(PASSWORD)))
    if operator:
        rows.append(Account(email=OPERATOR, name="Mei Chen", role="operator",
                            person_id=uuid.UUID('bbbbbbbb-0000-4000-8000-000000000001'),
                            password_hash=hash_password(PASSWORD)))
    if other:
        rows.append(Account(email=OTHER_OPERATOR, name="Tomas Rivera",
                            role="operator", person_id=uuid.UUID('bbbbbbbb-0000-4000-8000-000000000003'),
                            password_hash=hash_password(PASSWORD)))
    session.add_all(rows)
    await session.commit()
    return rows


async def signed_in(client, email):
    """Sign in and return the CSRF token the desk would echo back."""
    r = await client.post("/api/auth/login",
                          json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrfToken"]


def a_payload(rig="RIG-01", turns=None, day=None):
    return {
        "rigId": rig, "group": "A", "task": "Box transfer - bin to conveyor",
        "shift": {"label": "Morning", "date": (day or today()).isoformat(),
                  "start": "00:00", "end": "00:00", "tz": "UTC"},
        "blockMinutes": 15, "rotation": "hold",
        "turns": turns or [],
    }


def a_turn(frm, to, op_id, name, goes="Break", person=None):
    op = {"id": op_id, "name": name}
    if person is not None:
        op["personId"] = str(person)
    return {"from": frm, "to": to, "minutes": 45,
            "operator": op, "relievedBy": "Somebody", "theyGoTo": goes}


async def push_a_day(session):
    """Two rigs, two operators, so 'only mine' has something to exclude.
    The turns name people, the way the desk pushes once it assigns by
    picking - an account is a person now, and a push that named only
    seats would give it no day to show (stated in CLAUDE.md)."""
    day = today()
    pushed = datetime.now(UTC)
    rows = [
        Schedule(push_id=uuid.uuid4(), pushed_at=pushed,
                 rig_id="RIG-01", shift_date=day, shift_label="Morning",
                 payload=a_payload("RIG-01", [
                     a_turn("08:00", "08:45", "op-a2", "Mei Chen", person=uuid.UUID("bbbbbbbb-0000-4000-8000-000000000001")),
                     a_turn("08:45", "09:30", "op-a3", "Tomas Rivera", person=uuid.UUID("bbbbbbbb-0000-4000-8000-000000000003")),
                 ], day)),
        Schedule(push_id=uuid.uuid4(), pushed_at=pushed,
                 rig_id="RIG-02", shift_date=day, shift_label="Morning",
                 payload=a_payload("RIG-02", [
                     a_turn("09:45", "10:30", "op-a2", "Mei Chen", "Think", person=uuid.UUID("bbbbbbbb-0000-4000-8000-000000000001")),
                 ], day)),
    ]
    session.add_all(rows)
    await session.commit()


# ----------------------------------------------- off until configured


class TestWithNobodyToSignInAs:
    """A deployment with no accounts behaves exactly as it did before
    accounts existed. This is what keeps the laptop demo working."""

    async def test_the_push_is_open(self, engine, session):
        async with serving() as c:
            r = await c.post("/api/push", json={"payloads": [a_payload()]})
        assert r.status_code == 200

    async def test_the_desk_reads_are_open(self, engine, session):
        async with serving() as c:
            for path in DESK_READS:
                r = await c.get(path)
                assert r.status_code == 200, f"{path} -> {r.status_code}"

    async def test_health_agrees_that_it_is_off(self, engine, session):
        async with serving() as c:
            assert (await c.get("/api/health")).json()["personAuth"] == "off"


# --------------------------------------------------- once anybody exists


class TestOnceSomebodyHasAnAccount:
    async def test_the_push_now_needs_somebody(self, engine, session):
        await accounts(session)
        async with serving() as c:
            r = await c.post("/api/push", json={"payloads": [a_payload()]})
        assert r.status_code == 401

    async def test_the_desk_reads_now_need_somebody(self, engine, session):
        await accounts(session)
        async with serving() as c:
            for path in DESK_READS:
                r = await c.get(path)
                assert r.status_code == 401, f"{path} -> {r.status_code}"


class TestAnOperatorIsRefusedTheDesk:
    """The thing the whole piece of work was asked for."""

    async def test_an_operator_cannot_push_a_schedule(self, engine, session):
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c, OPERATOR)
            r = await c.post("/api/push", json={"payloads": [a_payload()]},
                             headers={CSRF_HEADER: csrf})
        assert r.status_code == 403
        assert "manager" in r.json()["detail"]

    async def test_an_operator_cannot_read_the_floor(self, engine, session):
        await accounts(session)
        async with serving() as c:
            await signed_in(c, OPERATOR)
            for path in DESK_READS:
                r = await c.get(path)
                assert r.status_code == 403, f"{path} -> {r.status_code}"

    async def test_an_operator_cannot_read_everybody_s_efficiency(
            self, engine, session):
        """Specifically called out: this route returns every operator's
        personal score, and whether an operator sees the person next to
        them is an open question that must not be settled by accident."""
        await accounts(session)
        async with serving() as c:
            await signed_in(c, OPERATOR)
            r = await c.get("/api/floor/efficiency"
                            "?shift_date=2026-08-26&shift_label=Morning")
        assert r.status_code == 403


class TestAManagerIsLetThrough:
    async def test_a_manager_may_push(self, engine, session):
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c, MANAGER)
            r = await c.post("/api/push", json={"payloads": [a_payload()]},
                             headers={CSRF_HEADER: csrf})
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 1

    async def test_a_manager_may_read_the_floor(self, engine, session):
        await accounts(session)
        async with serving() as c:
            await signed_in(c, MANAGER)
            for path in DESK_READS:
                r = await c.get(path)
                assert r.status_code == 200, f"{path} -> {r.status_code}"

    async def test_signing_out_closes_it_again(self, engine, session):
        await accounts(session)
        async with serving() as c:
            await signed_in(c, MANAGER)
            assert (await c.get("/api/state")).status_code == 200
            await c.post("/api/auth/logout")
            assert (await c.get("/api/state")).status_code == 401


# ------------------------------------------------------------- CSRF


class TestTheWriteNeedsItsToken:
    async def test_a_push_without_the_header_is_refused(self, engine, session):
        """The browser attaches the session cookie to any request, so the
        cookie alone cannot be what authorises a write."""
        await accounts(session)
        async with serving() as c:
            await signed_in(c, MANAGER)
            r = await c.post("/api/push", json={"payloads": [a_payload()]})
        assert r.status_code == 403
        assert "csrf" in r.json()["detail"].lower()

    async def test_a_push_with_the_wrong_header_is_refused(self, engine, session):
        await accounts(session)
        async with serving() as c:
            await signed_in(c, MANAGER)
            r = await c.post("/api/push", json={"payloads": [a_payload()]},
                             headers={CSRF_HEADER: "a-guess"})
        assert r.status_code == 403

    async def test_the_token_in_the_reply_matches_the_cookie(
            self, engine, session):
        """Either source works, which is what lets a client that cannot
        read cookies still make a write."""
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c, MANAGER)
            assert c.cookies.get(CSRF_COOKIE) == csrf

    async def test_reads_do_not_need_it(self, engine, session):
        """A cross-site GET cannot change anything and its response is
        unreadable to the page that provoked it."""
        await accounts(session)
        async with serving() as c:
            await signed_in(c, MANAGER)
            assert (await c.get("/api/state")).status_code == 200


# --------------------------------- a shared secret does not outrank a person


class TestTheDeskTokenDoesNotBypassTheRole:
    TOKEN = "the-desk-token"

    async def test_the_token_alone_still_works_with_no_accounts(
            self, engine, session):
        async with serving(desk_token=self.TOKEN) as c:
            r = await c.post("/api/push", json={"payloads": [a_payload()]},
                             headers={"Authorization": "Bearer " + self.TOKEN})
        assert r.status_code == 200

    async def test_the_token_alone_is_not_enough_once_accounts_exist(
            self, engine, session):
        """Otherwise anyone holding the shared secret walks straight past
        the role check, and the role check is decoration."""
        await accounts(session)
        async with serving(desk_token=self.TOKEN) as c:
            r = await c.post("/api/push", json={"payloads": [a_payload()]},
                             headers={"Authorization": "Bearer " + self.TOKEN})
        assert r.status_code == 401

    async def test_an_operator_holding_the_token_is_still_refused(
            self, engine, session):
        await accounts(session)
        async with serving(desk_token=self.TOKEN) as c:
            csrf = await signed_in(c, OPERATOR)
            r = await c.post("/api/push", json={"payloads": [a_payload()]},
                             headers={"Authorization": "Bearer " + self.TOKEN,
                                      CSRF_HEADER: csrf})
        assert r.status_code == 403

    async def test_a_manager_still_needs_the_token_when_one_is_set(
            self, engine, session):
        """Both gates apply. Each is independently off-until-configured,
        and they and together rather than either being a way round."""
        await accounts(session)
        async with serving(desk_token=self.TOKEN) as c:
            csrf = await signed_in(c, MANAGER)
            without = await c.post("/api/push", json={"payloads": [a_payload()]},
                                   headers={CSRF_HEADER: csrf})
            withit = await c.post(
                "/api/push", json={"payloads": [a_payload()]},
                headers={"Authorization": "Bearer " + self.TOKEN,
                         CSRF_HEADER: csrf})
        assert without.status_code == 401
        assert withit.status_code == 200


# ------------------------------------------------------ an operator's own


class TestMyShift:
    async def test_nobody_signed_in_is_refused(self, engine, session):
        async with serving() as c:
            assert (await c.get("/api/me/shift")).status_code == 401

    async def test_a_manager_is_refused_because_they_are_not_on_the_sheet(
            self, engine, session):
        await accounts(session)
        async with serving() as c:
            await signed_in(c, MANAGER)
            r = await c.get("/api/me/shift")
        assert r.status_code == 403

    async def test_an_operator_gets_their_own_turns_across_rigs(
            self, engine, session):
        """A rig knows only its own turns. This person's morning walks
        across two of them, which is the thing no rig can answer."""
        await accounts(session, other=True)
        await push_a_day(session)
        async with serving() as c:
            await signed_in(c, OPERATOR)
            r = await c.get("/api/me/shift")

        assert r.status_code == 200
        body = r.json()
        assert body["personId"] == "bbbbbbbb-0000-4000-8000-000000000001"
        assert [t["rigId"] for t in body["turns"]] == ["RIG-01", "RIG-02"]
        assert [t["from"] for t in body["turns"]] == ["08:00", "09:45"]

    async def test_it_carries_nobody_else(self, engine, session):
        """The whole point of scoping it in the route."""
        await accounts(session, other=True)
        await push_a_day(session)
        async with serving() as c:
            await signed_in(c, OPERATOR)
            r = await c.get("/api/me/shift")
        assert "Tomas Rivera" not in r.text
        assert "op-a3" not in r.text

    async def test_two_operators_get_two_different_days(self, engine, session):
        await accounts(session, other=True)
        await push_a_day(session)
        async with serving() as mei, serving() as tomas:
            await signed_in(mei, OPERATOR)
            await signed_in(tomas, OTHER_OPERATOR)
            mine = (await mei.get("/api/me/shift")).json()
            theirs = (await tomas.get("/api/me/shift")).json()

        assert [t["from"] for t in mine["turns"]] == ["08:00", "09:45"]
        assert [t["from"] for t in theirs["turns"]] == ["08:45"]

    async def test_the_shift_and_task_travel_with_it(self, engine, session):
        await accounts(session)
        await push_a_day(session)
        async with serving() as c:
            await signed_in(c, OPERATOR)
            body = (await c.get("/api/me/shift")).json()
        assert body["shift"]["label"] == "Morning"
        assert body["shift"]["group"] == "A"
        assert "Box transfer" in body["shift"]["task"]

    async def test_theygoto_travels_so_a_screen_can_draw_the_gaps(
            self, engine, session):
        """The server deliberately does not work the breaks out. Each
        turn says where its operator goes, which is enough for a screen
        to lay the gap out from two things that were pushed."""
        await accounts(session)
        await push_a_day(session)
        async with serving() as c:
            await signed_in(c, OPERATOR)
            turns = (await c.get("/api/me/shift")).json()["turns"]
        assert [t["theyGoTo"] for t in turns] == ["Break", "Think"]

    async def test_an_operator_with_nothing_pushed_is_told_so_plainly(
            self, engine, session):
        await accounts(session)
        async with serving() as c:
            await signed_in(c, OPERATOR)
            body = (await c.get("/api/me/shift")).json()
        assert body["turns"] == []
        assert body["shift"] is None


class TestMyEfficiency:
    QUERY = "?shift_date=2026-08-26&shift_label=Morning"

    async def test_nobody_signed_in_is_refused(self, engine, session):
        async with serving() as c:
            assert (await c.get("/api/me/scores" + self.QUERY)).status_code == 401

    async def test_a_manager_is_refused(self, engine, session):
        await accounts(session)
        async with serving() as c:
            await signed_in(c, MANAGER)
            r = await c.get("/api/me/scores" + self.QUERY)
        assert r.status_code == 403

    async def test_an_operator_gets_a_list_of_only_themselves(
            self, engine, session):
        await accounts(session)
        async with serving() as c:
            await signed_in(c, OPERATOR)
            r = await c.get("/api/me/scores" + self.QUERY)
        assert r.status_code == 200
        body = r.json()
        assert body["shiftLabel"] == "Morning"
        assert all(row["personId"] == "bbbbbbbb-0000-4000-8000-000000000001" for row in body["people"])

    async def test_a_shift_they_did_not_work_is_empty_not_zero(
            self, engine, session):
        """A zeroed row reads as 'you recorded nothing'. An empty list
        reads as 'you were not here', which is what is true."""
        await accounts(session)
        async with serving() as c:
            await signed_in(c, OPERATOR)
            body = (await c.get("/api/me/scores" + self.QUERY)).json()
        assert body["people"] == []


# ------------------------------------------- the rig, still not involved


class TestTheRigIsStillUntouched:
    RIG = "RIG-03"
    RIG_TOKEN = "a-token-placed-by-ansible"

    async def test_a_rig_needs_no_account_and_no_session(self, engine, session):
        """Accounts existing must not make a rig need one. Twelve rigs
        that stop filing events the day somebody creates a login is the
        worst possible outcome of this whole piece of work."""
        await accounts(session)
        async with serving(rig_tokens={self.RIG: self.RIG_TOKEN}) as c:
            r = await c.get(f"/api/rigs/{self.RIG}/cursor",
                            headers={"Authorization": "Bearer " + self.RIG_TOKEN})
        assert r.status_code == 200

    async def test_a_rig_may_still_read_its_own_schedule(self, engine, session):
        await accounts(session)
        await push_a_day(session)
        async with serving(rig_tokens={"RIG-01": self.RIG_TOKEN}) as c:
            r = await c.get("/api/rigs/RIG-01/schedule.json",
                            headers={"Authorization": "Bearer " + self.RIG_TOKEN})
        assert r.status_code == 200
        assert r.json()["rigId"] == "RIG-01"

    async def test_a_manager_session_still_cannot_speak_for_a_rig(
            self, engine, session):
        await accounts(session)
        async with serving(rig_tokens={self.RIG: self.RIG_TOKEN}) as c:
            await signed_in(c, MANAGER)
            r = await c.get(f"/api/rigs/{self.RIG}/cursor")
        assert r.status_code == 401


class TestPeopleAreTheDesksToManage:
    """Creating, renaming and disabling a person are manager's acts, and
    they follow the same three rules as the push: open on a deployment
    with no accounts, 401 once anybody has one, 403 for an operator."""

    BODY = {"name": "Ben Carter"}

    async def test_open_until_somebody_has_an_account(self, engine, session):
        async with serving() as c:
            r = await c.post("/api/people", json=self.BODY)
        assert r.status_code == 201, r.text

    async def test_needs_somebody_once_accounts_exist(self, engine, session):
        await accounts(session)
        async with serving() as c:
            r = await c.post("/api/people", json=self.BODY)
        assert r.status_code == 401

    async def test_an_operator_may_not_add_a_person(self, engine, session):
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c, OPERATOR)
            r = await c.post("/api/people", json=self.BODY,
                             headers={CSRF_HEADER: csrf})
        assert r.status_code == 403
        assert "manager" in r.json()["detail"]

    async def test_a_manager_may(self, engine, session):
        await accounts(session)
        async with serving() as c:
            csrf = await signed_in(c, MANAGER)
            r = await c.post("/api/people", json=self.BODY,
                             headers={CSRF_HEADER: csrf})
        assert r.status_code == 201, r.text

    async def test_a_manager_without_the_csrf_token_is_refused(self, engine, session):
        """A browser session is exactly what CSRF is about. The desk
        echoes the token; a form on another origin cannot."""
        await accounts(session)
        async with serving() as c:
            await signed_in(c, MANAGER)
            r = await c.post("/api/people", json=self.BODY)
        assert r.status_code == 403

    NOBODY = "/api/people/00000000-0000-4000-8000-000000000000"

    async def test_reading_one_person_needs_somebody_once_accounts_exist(
            self, engine, session):
        await accounts(session)
        async with serving() as c:
            r = await c.get(self.NOBODY)
        assert r.status_code == 401

    async def test_an_operator_may_not_read_one_person(self, engine, session):
        await accounts(session)
        async with serving() as c:
            await signed_in(c, OPERATOR)
            r = await c.get(self.NOBODY)
        assert r.status_code == 403

    async def test_a_manager_reading_nobody_gets_a_404_not_a_refusal(
            self, engine, session):
        """The gate lets a manager through, and then the answer is that
        nobody has that id - which is a different thing from being
        refused, and the sweep above cannot tell them apart."""
        await accounts(session)
        async with serving() as c:
            await signed_in(c, MANAGER)
            r = await c.get(self.NOBODY)
        assert r.status_code == 404


# ------------------------------------------ an account names its person

# CLAUDE.md, "An account names its person, and the seat is never on the
# account". /me/shift and /me/efficiency used to answer "how did I do" by
# asking what the account's *chair* did. On a cover day that showed one
# person another's numbers as their own, on their own screen.

MEI = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000001")
PRIYA = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")


def a_person_turn(frm, to, op_id, name, person, goes="Break"):
    t = a_turn(frm, to, op_id, name, goes)
    t["operator"]["personId"] = str(person)
    return t


async def cover_day(session):
    """Priya covers Mei's usual seat, op-a2, this morning. Mei is on op-a4
    instead. Grouped by chair, each sees the other's day."""
    day = today()
    pushed = datetime.now(UTC)
    session.add_all([
        Schedule(push_id=uuid.uuid4(), pushed_at=pushed,
                 rig_id="RIG-01", shift_date=day, shift_label="Morning",
                 payload=a_payload("RIG-01", [
                     a_person_turn("08:00", "08:45", "op-a2", "Priya Anand", PRIYA),
                     a_person_turn("08:45", "09:30", "op-a4", "Mei Chen", MEI),
                 ], day)),
    ])
    await session.commit()


async def mei_with_a_person(session):
    """Mei's account names Mei the person - and no seat."""
    session.add(Account(email=OPERATOR, name="Mei Chen", role="operator",
                        person_id=MEI, password_hash=hash_password(PASSWORD)))
    await session.commit()


class TestAnAccountNamesItsPerson:
    async def test_my_shift_is_the_persons_day_not_the_chairs(self, engine, session):
        await mei_with_a_person(session)
        await cover_day(session)
        async with serving() as c:
            await signed_in(c, OPERATOR)
            body = (await c.get("/api/me/shift")).json()
        assert [t["operator"]["name"] for t in body["turns"]] == ["Mei Chen"], \
            "Mei's screen showed the chair's day - which is Priya's today"
        assert body["turns"][0]["operator"]["id"] == "op-a4", \
            "the seat comes from the push, not the account"
        assert body["personId"] == str(MEI)

    async def test_my_scores_are_mine_and_not_the_chairs(self, engine, session):
        from core.domains.episodes.model import Episode
        await mei_with_a_person(session)
        day = today()
        n = 0
        for person, name, seat, score in ((PRIYA, "Priya Anand", "op-a2", 3),
                                          (MEI, "Mei Chen", "op-a4", 5)):
            n += 1
            session.add(Episode(
                episode_id=uuid.uuid4(), rig_id="RIG-01", shift_date=day, shift_label="Morning",
                turn_from="08:00", operator_id=seat, operator_name=name, person_id=person,
                at=datetime.now(UTC), duration_secs=90.0, outcome="saved", score=score,
                source_event=5000 + n))
        await session.commit()
        async with serving() as c:
            await signed_in(c, OPERATOR)
            r = await c.get("/api/me/scores", params={"shift_date": day.isoformat(), "shift_label": "Morning"})
        assert r.status_code == 200, r.text
        people = r.json()["people"]
        assert [p["name"] for p in people] == ["Mei Chen"], "an operator saw somebody else's scores"
        assert people[0]["average"] == 5.0

    async def test_an_operator_account_needs_a_person_not_a_seat(self, engine, session):
        """The old rule - an operator must name a seat - is retired. The new
        one is the same shape pointed at the right thing."""
        from sqlalchemy.exc import IntegrityError
        session.add(Account(email="nobody@verlet.co", name="No One", role="operator",
                            password_hash=hash_password(PASSWORD)))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
        session.add(Account(email="m2@verlet.co", name="Mei Chen", role="operator",
                            person_id=MEI, password_hash=hash_password(PASSWORD)))
        await session.commit()

    async def test_two_accounts_cannot_be_the_same_person(self, engine, session):
        from sqlalchemy.exc import IntegrityError
        await mei_with_a_person(session)
        session.add(Account(email="again@verlet.co", name="Mei Chen", role="operator",
                            person_id=MEI, password_hash=hash_password(PASSWORD)))
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_the_session_tells_the_screen_the_person_and_no_seat(self, engine, session):
        await mei_with_a_person(session)
        async with serving() as c:
            body = (await c.post("/api/auth/login",
                                 json={"email": OPERATOR, "password": PASSWORD})).json()
        assert body["personId"] == str(MEI)
        assert "operatorId" not in body, "the seat must not be on the account, so not in the session either"
