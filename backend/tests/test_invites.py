"""Invites: an account with no password, and a link to set one.

A new operator used to get an account from `mint_account` and a password
read out by whoever ran it, so every first password travelled through a
conversation. An invite is the reset flow doing the same job for
somebody who has no password yet: a manager presses a button beside a
person who has an address, the service mints their operator account
with a password nobody knows, and mails the same link "Forgotten your
password?" sends. They open it, choose their own, and are signed in.

Two ways to send one - the desk's route and the terminal - and one
function underneath both, so they cannot drift.

No mail leaves this machine: `mail.send` is replaced with a box.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from core.domains.accounts.model import Account
from core.domains.accounts.passwords import hash_password
from core.domains.accounts.repository import AccountRepository
from core.domains.people.repository import PersonRepository
from core.infrastructure.config import Settings, get_settings
from services.rigs import mail, people
from services.rigs.people import CSRF_HEADER

MANAGER = "r.osei@verlet.co"
OPERATOR = "m.chen@verlet.co"
PASSWORD = "correct-horse-battery-staple"

ON = dict(smtp_host="mail.test", public_base_url="https://floor.test",
          session_cookie_secure=False)


class Outbox(list):
    deliver = True

    def sender(self):
        async def send(to, subject, body, settings):
            self.append({"to": to, "subject": subject, "body": body})
            return self.deliver
        return send

    def token(self, index=-1):
        link = next(w for w in self[index]["body"].split() if w.startswith("https://"))
        assert "#reset=" in link, f"the invite link is not a fragment: {link}"
        return link.split("#reset=")[1]


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(mail, "send", box.sender())
    monkeypatch.setattr(people.mail, "send", box.sender())
    return box


@asynccontextmanager
async def serving(**overrides):
    from services.rigs.app import create_app

    base = get_settings()
    settings = Settings(**{**base.model_dump(), **ON, **overrides})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def a_floor(session):
    """A manager, an operator who already signs in as Mei, and three more
    people: one with an address and no account, one with no address, one
    who has left."""
    repo = PersonRepository(session)
    mei = await repo.create("Mei Chen", email=OPERATOR)
    fatima = await repo.create("Fatima Zahra", email="f.zahra@verlet.co")
    omar = await repo.create("Omar Haddad")
    gone = await repo.create("Old Hand", email="old.hand@verlet.co")
    gone.disabled_at = datetime.now(timezone.utc)
    session.add_all([
        Account(email=MANAGER, name="Ruth Osei", role="manager",
                password_hash=hash_password(PASSWORD),
                password_set_at=datetime.now(timezone.utc)),
        Account(email=OPERATOR, name="Mei Chen", role="operator", person_id=mei.id,
                password_hash=hash_password(PASSWORD),
                password_set_at=datetime.now(timezone.utc)),
    ])
    await session.commit()
    return {"mei": mei, "fatima": fatima, "omar": omar, "gone": gone}


async def signed_in(client, email=MANAGER):
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrfToken"]


async def invite(client, csrf, person_id):
    return await client.post(f"/api/people/{person_id}/invite", headers={CSRF_HEADER: csrf})


# ------------------------------------------------------------ the desk's way


async def test_a_manager_invites_a_person_with_an_address(engine, session, outbox):
    """The whole path: press the button, an account exists with no
    password anybody knows, the link arrives, and following it signs
    them in with a password of their own."""
    floor = await a_floor(session)
    async with serving() as c:
        csrf = await signed_in(c)
        r = await invite(c, csrf, floor["fatima"].id)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "email": "f.zahra@verlet.co",
                            "account": "created", "sent": True}

        account = await AccountRepository(session).by_person_id(floor["fatima"].id)
        assert account is not None and account.role == "operator"
        assert account.name == "Fatima Zahra" and account.email == "f.zahra@verlet.co"
        assert account.password_set_at is None, "an invited account has no password yet"

        assert len(outbox) == 1 and outbox[0]["to"] == "f.zahra@verlet.co"
        assert "/apps/my-shift/#reset=" in outbox[0]["body"], "the link should land on My Shift"

        # No password can sign in yet - not even an empty one.
        r = await c.post("/api/auth/login", json={"email": "f.zahra@verlet.co", "password": ""})
        assert r.status_code in (401, 422)

        # Following the link sets a password and signs them in.
        r = await c.post("/api/auth/reset", json={"token": outbox.token(),
                                                  "newPassword": "a phrase of her own choosing"})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "operator"
        await session.refresh(account)
        assert account.password_set_at is not None

        r = await c.get("/api/me/shift")
        assert r.status_code == 200, "the invited operator should be signed in to My Shift"


async def test_nobody_without_an_address_can_be_invited(engine, session, outbox):
    floor = await a_floor(session)
    async with serving() as c:
        csrf = await signed_in(c)
        r = await invite(c, csrf, floor["omar"].id)
    assert r.status_code == 409, r.text
    assert "address" in r.json()["detail"]
    assert await AccountRepository(session).by_person_id(floor["omar"].id) is None
    assert outbox == []


async def test_somebody_who_has_left_is_not_invited(engine, session, outbox):
    floor = await a_floor(session)
    async with serving() as c:
        csrf = await signed_in(c)
        r = await invite(c, csrf, floor["gone"].id)
    assert r.status_code == 409
    assert outbox == []


async def test_an_id_nobody_has_is_404(engine, session, outbox):
    await a_floor(session)
    async with serving() as c:
        csrf = await signed_in(c)
        r = await invite(c, csrf, uuid.uuid4())
    assert r.status_code == 404


async def test_somebody_who_already_signs_in_is_not_invited(engine, session, outbox):
    """Mei has a password of her own. An invite would mail her a link to
    replace it, which is a reset, and the reset flow is where that lives."""
    floor = await a_floor(session)
    async with serving() as c:
        csrf = await signed_in(c)
        r = await invite(c, csrf, floor["mei"].id)
    assert r.status_code == 409
    assert "already signs in" in r.json()["detail"]
    assert outbox == []


async def test_inviting_again_sends_a_fresh_link_and_voids_the_old(engine, session, outbox):
    """The mail did not arrive, the manager presses again. One account
    still; the first link is dead, the second works."""
    floor = await a_floor(session)
    async with serving() as c:
        csrf = await signed_in(c)
        assert (await invite(c, csrf, floor["fatima"].id)).json()["account"] == "created"
        r = await invite(c, csrf, floor["fatima"].id)
        assert r.status_code == 200 and r.json()["account"] == "resent"
        assert len(outbox) == 2

        rows = await session.execute(
            __import__("sqlalchemy").select(Account).where(Account.person_id == floor["fatima"].id))
        assert len(rows.scalars().all()) == 1

        first = await c.post("/api/auth/reset", json={"token": outbox.token(0),
                                                      "newPassword": "a phrase of her own choosing"})
        assert first.status_code == 400, "the earlier link should have been voided"
        second = await c.post("/api/auth/reset", json={"token": outbox.token(1),
                                                       "newPassword": "a phrase of her own choosing"})
        assert second.status_code == 200


async def test_without_a_relay_the_floor_says_so_and_mints_nothing(engine, session, outbox):
    """No mail can leave, so no account is left half-made behind a link
    that will never arrive. The terminal path still works on such a
    floor; the reply says so."""
    floor = await a_floor(session)
    async with serving(smtp_host="") as c:
        csrf = await signed_in(c)
        r = await invite(c, csrf, floor["fatima"].id)
    assert r.status_code == 503, r.text
    assert "mint_account" in r.json()["detail"]
    assert await AccountRepository(session).by_person_id(floor["fatima"].id) is None


async def test_only_a_manager_may_invite(engine, session, outbox):
    floor = await a_floor(session)
    async with serving() as c:
        r = await c.post(f"/api/people/{floor['fatima'].id}/invite")
        assert r.status_code == 401
        csrf = await signed_in(c, OPERATOR)
        r = await invite(c, csrf, floor["fatima"].id)
        assert r.status_code == 403
    assert outbox == []


async def test_the_people_list_says_who_signs_in(engine, session, outbox):
    """What the desk draws the button from: none, invited, active."""
    floor = await a_floor(session)
    async with serving() as c:
        csrf = await signed_in(c)
        await invite(c, csrf, floor["fatima"].id)
        r = await c.get("/api/people?q=")
        state = {p["name"]: p["account"] for p in r.json()["people"]}
        assert state == {"Mei Chen": "active", "Fatima Zahra": "invited", "Omar Haddad": "none"}

        await c.post("/api/auth/reset", json={"token": outbox.token(),
                                              "newPassword": "a phrase of her own choosing"})
        # The reset signed Fatima in; read the list as the manager again.
        csrf = await signed_in(c)
        r = await c.get("/api/people?q=")
        assert {p["name"]: p["account"] for p in r.json()["people"]}["Fatima Zahra"] == "active"


# --------------------------------------------------------- the terminal's way


async def test_the_terminal_sends_the_same_invite(engine, session, outbox, monkeypatch):
    """`mint_account invite --person` - the same function under the
    button, so the two cannot drift."""
    from tools import mint_account
    from types import SimpleNamespace

    floor = await a_floor(session)
    on = Settings(**{**get_settings().model_dump(), **ON})
    monkeypatch.setattr(mint_account, "get_settings", lambda: on)

    args = SimpleNamespace(cmd="invite", person=str(floor["fatima"].id), email=None)
    assert await mint_account._act(args, session) == 0

    account = await AccountRepository(session).by_person_id(floor["fatima"].id)
    assert account is not None and account.password_set_at is None
    assert len(outbox) == 1 and "#reset=" in outbox[0]["body"]


async def test_the_terminal_refuses_the_same_things(engine, session, outbox, monkeypatch):
    from tools import mint_account
    from types import SimpleNamespace

    floor = await a_floor(session)
    on = Settings(**{**get_settings().model_dump(), **ON})
    monkeypatch.setattr(mint_account, "get_settings", lambda: on)

    with pytest.raises(SystemExit, match="address"):
        await mint_account._act(SimpleNamespace(cmd="invite", person=str(floor["omar"].id), email=None), session)
    with pytest.raises(SystemExit, match="already signs in"):
        await mint_account._act(SimpleNamespace(cmd="invite", person=str(floor["mei"].id), email=None), session)
    assert outbox == []


async def test_a_minted_account_says_when_its_password_was_set(engine, session, monkeypatch):
    """The column the list reads. The tool sets it when it sets a
    password, and so does every route that does - an account is
    'invited' only until somebody chooses one."""
    from tools import mint_account
    from types import SimpleNamespace

    floor = await a_floor(session)
    args = SimpleNamespace(cmd="operator", email="f.zahra@verlet.co", name="Fatima Zahra",
                           person=str(floor["fatima"].id), password=PASSWORD, generate=False)
    assert await mint_account._act(args, session) == 0
    account = await AccountRepository(session).by_email("f.zahra@verlet.co")
    assert account.password_set_at is not None
