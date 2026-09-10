"""The two tools a deployment actually depends on.

`tools.preflight` is the API unit's ExecStartPre - it is the gate, so a
preflight that has quietly stopped working stops being one, and every
failure it names is five minutes here against an hour on a floor. Its
FAIL branches only ever run against a misconfigured floor, which is
exactly where nobody wants to discover a typo in the check itself. One
already got in: a `record(BROKEN, ...)` naming a constant that does not
exist, which would have raised NameError instead of reporting the fault
it had correctly found.

`tools.mint_tokens` generates the twelve credentials. Nothing checked
that they were different from each other, which is the entire property.
"""

import re

import pytest

from tools import mint_tokens, preflight


# Everything a real Settings has, so a stand-in cannot be missing a
# field the checks read. Listing them by hand is what broke this file:
# five settings were added for password reset and all 28 tests in it died
# on `AttributeError: no attribute 'smtp_host'` - the same shape as the
# bug preflight's own table check had, where ten domains were listed by
# hand and `accounts` was not one of them.
#
# A list maintained by hand next to a thing that grows is a list that
# will be wrong, and the third time is enough.
def _real_defaults() -> dict:
    from pydantic_core import PydanticUndefined

    from core.infrastructure.config import Settings

    out = {}
    for name, field in Settings.model_fields.items():
        if field.default_factory is not None:
            out[name] = field.default_factory()
        elif field.default is not PydanticUndefined:
            out[name] = field.default
        else:
            out[name] = ""          # required, so it has no default to copy
    return out


class FakeSettings:
    """A settings object for the checks, with everything a real one has.

    Three layers, in order. The real model's defaults, so nothing is ever
    missing. Then the deliberate "off" values below - this file's tests
    want a bare deployment unless `FLOOR` turns something on, and several
    of them assert on exactly that. Then whatever the test passed.
    """

    # Off, whatever the real default is. `session_cookie_secure` is the
    # one that differs on purpose: it ships True, and these tests want to
    # start from a deployment that has configured nothing.
    OFF = dict(
        rig_tokens={}, rig_addresses={}, desk_token="",
        rig_rate_limit_per_min=0, session_cookie_secure=False,
        login_rate_limit_per_min=0, login_lockout_after=0,
        video_keep_days=0, test_database_url="",
        database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/rigs",
    )

    def __init__(self, **kw):
        for layer in (_real_defaults(), self.OFF, kw):
            for name, value in layer.items():
                setattr(self, name, value)


@pytest.fixture(autouse=True)
def clean_results():
    """`preflight.results` is module-global and printed at the end. A test
    that leaves rows in it changes the next test's verdict."""
    preflight.results.clear()
    yield
    preflight.results.clear()


def posture(accounts=1, **kw) -> dict[str, tuple[str, str]]:
    """Run the checks and return {name: (state, detail)}."""
    preflight.check_posture(FakeSettings(**kw), accounts)
    return {name: (state, detail) for state, name, detail in preflight.results}


FLOOR = dict(
    rig_tokens={"RIG-01": "t1", "RIG-02": "t2"},
    rig_addresses={"RIG-01": "10.0.0.11", "RIG-02": "10.0.0.12"},
    desk_token="d",
    rig_rate_limit_per_min=120,
    video_keep_days=90,
    test_database_url="",
    session_cookie_secure=True,
    login_rate_limit_per_min=10,
    login_lockout_after=5,
)


# =============================================== the check itself works

def test_every_branch_names_a_state_that_exists():
    """The bug that got in once: a level constant that does not exist
    raises NameError from inside the check, so a correctly detected fault
    is reported as a crash. Every row must carry a real state."""
    for case in (
        {}, FLOOR,
        dict(FLOOR, rig_addresses={}),
        dict(FLOOR, rig_tokens={"RIG-01": "t1"}),
        dict(FLOOR, rig_addresses={"RIG-01": "10.0.0.11"}),
        dict(FLOOR, rig_addresses={"RIG-01": "10.0.0.11", "RIG-02": "10.0.0.11"}),
        dict(FLOOR, test_database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/rigs"),
        dict(FLOOR, session_cookie_secure=False),
        dict(FLOOR, login_rate_limit_per_min=0),
        dict(FLOOR, login_lockout_after=0),
    ):
        for accounts in (None, 0, 3):
            preflight.results.clear()
            preflight.check_posture(FakeSettings(**case), accounts)
            for state, name, _ in preflight.results:
                assert state in (preflight.OK, preflight.WARN, preflight.FAIL), (
                    f"{name} reported an unknown state {state!r}")


def test_a_correctly_provisioned_floor_is_all_clear():
    got = posture(**FLOOR)
    assert all(state == preflight.OK for state, _ in got.values()), got


# ======================================================== rig identity

class TestRigIdentity:
    def test_addresses_and_tokens_naming_the_same_rigs_pass(self):
        assert posture(**FLOOR)["rig identity"][0] == preflight.OK

    def test_tokens_with_no_addresses_is_fatal(self):
        """Every rig would be refused an identity and refuse to start, so
        the floor is down. That is broken, not merely unwise."""
        state, detail = posture(**dict(FLOOR, rig_addresses={}))["rig identity"]
        assert state == preflight.FAIL
        assert "RIG_ADDRESSES" in detail

    def test_a_rig_with_an_address_and_no_token_is_fatal(self):
        state, detail = posture(**dict(FLOOR, rig_tokens={"RIG-01": "t1"}))["rig identity"]
        assert state == preflight.FAIL
        assert "RIG-02" in detail and "refused" in detail

    def test_a_rig_with_a_token_and_no_address_is_fatal(self):
        state, detail = posture(
            **dict(FLOOR, rig_addresses={"RIG-01": "10.0.0.11"}))["rig identity"]
        assert state == preflight.FAIL
        assert "RIG-02" in detail and "identified" in detail

    def test_two_rigs_at_one_address_is_fatal(self):
        state, detail = posture(**dict(
            FLOOR, rig_addresses={"RIG-01": "10.0.0.11", "RIG-02": "10.0.0.11"},
        ))["rig identity"]
        assert state == preflight.FAIL
        assert "10.0.0.11" in detail

    def test_nothing_configured_at_all_is_a_demo_not_a_fault(self):
        """A laptop. Warned about, so --strict still refuses it on a
        floor, but it is not broken."""
        assert posture()["rig identity"][0] == preflight.WARN

    def test_the_fatal_cases_say_which_rigs(self):
        _, detail = posture(**dict(
            FLOOR,
            rig_tokens={"RIG-01": "t1", "RIG-03": "t3"},
            rig_addresses={"RIG-01": "10.0.0.11", "RIG-02": "10.0.0.12"},
        ))["rig identity"]
        assert "RIG-02" in detail and "RIG-03" in detail, (
            f"a fault naming no rig is a fault nobody can fix: {detail}")


# ======================================================== the other gates

class TestPosture:
    def test_no_rig_tokens_is_a_warning(self):
        assert posture(**dict(FLOOR, rig_tokens={}, rig_addresses={}))["rig auth"][0] \
            == preflight.WARN

    def test_no_desk_token_is_a_warning(self):
        assert posture(**dict(FLOOR, desk_token=""))["desk auth"][0] == preflight.WARN

    def test_no_rate_limit_is_a_warning(self):
        assert posture(**dict(FLOOR, rig_rate_limit_per_min=0))["rate limit"][0] \
            == preflight.WARN

    def test_no_retention_is_a_warning(self):
        """0 keeps everything for ever, which fills a disk rather than
        losing data - a warning, not a fault."""
        assert posture(**dict(FLOOR, video_keep_days=0))["retention"][0] == preflight.WARN


class TestTheDatabaseGuard:
    """The check DEPLOY.md calls the only thing between a tired evening
    and an empty ledger: the suite drops every table it can see."""

    def test_the_same_database_for_test_and_live_is_fatal(self):
        state, detail = posture(**dict(
            FLOOR,
            database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/rigs",
            test_database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/rigs",
        ))["databases"]
        assert state == preflight.FAIL
        assert "drops every table" in detail

    def test_two_urls_differing_only_by_password_are_the_same_database(self):
        """The documented reason it compares host and name rather than the
        URL string. Two credentials for one database is still one
        database."""
        state, _ = posture(**dict(
            FLOOR,
            database_url="postgresql+asyncpg://u:one@127.0.0.1:5432/rigs",
            test_database_url="postgresql+asyncpg://u:two@127.0.0.1:5432/rigs",
        ))["databases"]
        assert state == preflight.FAIL, "a password change disguised the same database"

    def test_different_databases_on_one_host_are_fine(self):
        state, _ = posture(**dict(
            FLOOR,
            database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/rigs",
            test_database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/rigs_test",
        ))["databases"]
        assert state == preflight.OK

    def test_the_same_name_on_a_different_host_is_fine(self):
        state, _ = posture(**dict(
            FLOOR,
            database_url="postgresql+asyncpg://u:p@10.0.0.5:5432/rigs",
            test_database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/rigs",
        ))["databases"]
        assert state == preflight.OK

    def test_no_test_url_set_says_nothing_at_all(self):
        assert "databases" not in posture(**FLOOR)


# ========================================================== mint_tokens

class TestMintTokens:
    def test_one_token_per_rig(self):
        rigs = ["RIG-01", "RIG-02", "RIG-03"]
        assert sorted(mint_tokens.mint(rigs)) == sorted(rigs)

    def test_every_token_is_different(self):
        """The whole property. A token names exactly one rig and is
        refused for any other, and that is worth nothing if all twelve
        machines carry the same string."""
        tokens = mint_tokens.mint(mint_tokens.DEFAULT_RIGS)
        assert len(set(tokens.values())) == len(tokens)

    def test_two_runs_never_produce_the_same_token(self):
        a = mint_tokens.mint(["RIG-01"])["RIG-01"]
        b = mint_tokens.mint(["RIG-01"])["RIG-01"]
        assert a != b, "tokens are not being generated randomly"

    def test_a_token_is_long_enough_to_be_a_secret(self):
        token = mint_tokens.mint(["RIG-01"])["RIG-01"]
        assert len(token) >= 40, f"{len(token)} characters is not 256 bits"
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token), (
            "a token with other characters has to be quoted in JSON, in a "
            "systemd unit and in a shell, and one of those will get it wrong")

    def test_the_default_floor_is_twelve_rigs(self):
        assert mint_tokens.DEFAULT_RIGS == [f"RIG-{i:02d}" for i in range(1, 13)]

    def test_it_no_longer_writes_per_machine_config_files(self):
        """It used to write a rig-config.js per rig for Ansible to place
        on the machines, and nothing ever read them - the page loads that
        file from the server. A step that looks like provisioning and does
        nothing is worse than no step."""
        import inspect
        src = inspect.getsource(mint_tokens)
        assert "--out" not in src, "the flag that wrote files nothing reads is back"
        assert "rig-config.js" not in src.split('"""', 2)[2], (
            "it is writing per-machine config files again")

    def test_it_offers_the_address_map_beside_the_tokens(self):
        """The two have to name the same rigs and preflight checks that
        they do, so they are produced from one run."""
        import inspect
        src = inspect.getsource(mint_tokens)
        assert "--addresses" in src and "RIG_ADDRESSES" in src


# ================================================ the desk's own switches
#
# A switch is announced in three places in this service: `announce()` at
# startup, `/api/health` for a load balancer, and preflight for the unit
# that refuses to come up. The person-auth switches were wired into the
# first two and not the third, so a deployment was warned that the *rig*
# door was open and told nothing about the *desk* door being open.


class TestPersonAuth:
    def test_no_accounts_is_a_warning(self):
        """The same fault as an empty DESK_TOKEN, which is already a
        warning: anyone who reaches the desk may push a schedule."""
        got = posture(accounts=0, **FLOOR)
        assert got["person auth"][0] == preflight.WARN
        assert "no accounts" in got["person auth"][1]

    def test_it_says_how_to_make_one(self):
        """A warning somebody cannot act on is only half a warning."""
        assert "mint_account" in posture(accounts=0, **FLOOR)["person auth"][1]

    def test_accounts_are_reported_and_counted(self):
        state, detail = posture(accounts=4, **FLOOR)["person auth"]
        assert state == preflight.OK
        assert "4 accounts" in detail

    def test_a_database_it_could_not_ask_is_not_reported_as_ok(self):
        """A check that cannot answer must not answer yes. That is the
        bug this file exists to catch, in another place."""
        assert posture(accounts=None, **FLOOR)["person auth"][0] == preflight.WARN


class TestTheSessionCookie:
    def test_an_insecure_cookie_is_a_warning(self):
        got = posture(**dict(FLOOR, session_cookie_secure=False))
        assert got["session cookie"][0] == preflight.WARN
        assert "cleartext" in got["session cookie"][1]

    def test_a_secure_cookie_is_clear(self):
        assert posture(**FLOOR)["session cookie"][0] == preflight.OK

    def test_it_is_the_one_switch_that_defaults_safe(self):
        """Every other switch here is off until somebody turns it on, so
        finding it off says nothing. This one is on until somebody turns
        it off, so finding it off is always a decision - and worth
        saying. Asserted against the real settings, not the fake."""
        from core.infrastructure.config import Settings

        assert Settings.model_fields["session_cookie_secure"].default is True
        assert Settings.model_fields["login_lockout_after"].default > 0
        assert Settings.model_fields["login_rate_limit_per_min"].default > 0


class TestTheLoginThrottles:
    def test_no_rate_limit_is_a_warning(self):
        got = posture(**dict(FLOOR, login_rate_limit_per_min=0))
        assert got["login rate limit"][0] == preflight.WARN

    def test_no_lockout_is_a_warning(self):
        got = posture(**dict(FLOOR, login_lockout_after=0))
        assert got["login lockout"][0] == preflight.WARN
        assert "list" in got["login lockout"][1]

    def test_both_configured_are_clear(self):
        got = posture(**FLOOR)
        assert got["login rate limit"][0] == preflight.OK
        assert got["login lockout"][0] == preflight.OK


def test_the_stand_in_has_every_field_a_real_settings_has():
    """The guard on the stand-in itself.

    Adding a setting and reading it in `check_posture` used to fail every
    test in this file with an AttributeError, because the fake listed its
    fields by hand. It derives them now, and this is what says so - if
    somebody reverts that to a hand-written list, this fails rather than
    twenty-eight unrelated tests failing for a reason none of them are
    about.
    """
    from core.infrastructure.config import Settings

    fake = FakeSettings()
    missing = [n for n in Settings.model_fields if not hasattr(fake, n)]
    assert missing == [], (
        f"FakeSettings is missing {missing} - it has drifted from the real "
        f"Settings, and every check that reads one of those will die on an "
        f"AttributeError rather than on anything it is testing")


# ============================================ password reset by email


class TestPasswordReset:
    def test_no_relay_is_reported_as_off_rather_than_as_a_problem(self):
        """Off is the right state for most floors, so it is `ok` - but it
        has to say what to do instead, or somebody locked out has nothing
        to go on."""
        state, detail = posture(**FLOOR)["password reset"]
        assert state == preflight.OK
        assert "off" in detail
        assert "mint_account" in detail

    def test_a_relay_with_nowhere_to_point_is_fatal(self):
        """SMTP configured and no base URL is a flow that sends mail with
        a broken link in it - worse than not sending any."""
        got = posture(**dict(FLOOR, smtp_host="mail.test"))
        assert got["password reset"][0] == preflight.FAIL
        assert "PUBLIC_BASE_URL" in got["password reset"][1]

    def test_configured_says_where_and_for_how_long(self):
        got = posture(**dict(FLOOR, smtp_host="mail.test",
                             public_base_url="https://floor.test",
                             password_reset_minutes=30,
                             password_reset_per_hour=5))
        state, detail = got["password reset"]
        assert state == preflight.OK
        assert "https://floor.test" in detail
        assert "30 minutes" in detail

    def test_turning_it_on_warns_that_addresses_are_now_credentials(self):
        """The part nobody thinks about when they switch it on. Those
        addresses are typed once at mint time and nothing has ever checked
        one, so a typo is a reset link posted to a stranger."""
        got = posture(**dict(FLOOR, smtp_host="mail.test",
                             public_base_url="https://floor.test"))
        state, detail = got["reset addresses"]
        assert state == preflight.WARN
        assert "credential" in detail
        assert "mint_account list" in detail

    def test_that_warning_is_absent_when_reset_is_off(self):
        """A warning on every deployment is a warning nobody reads."""
        assert "reset addresses" not in posture(**FLOOR)

    def test_cleartext_to_the_relay_is_a_warning(self):
        got = posture(**dict(FLOOR, smtp_host="mail.test",
                             public_base_url="https://floor.test",
                             smtp_starttls=False))
        assert got["reset transport"][0] == preflight.WARN
        assert "cleartext" in got["reset transport"][1]


def test_every_switch_that_can_be_off_is_reported():
    """The gap itself, as an assertion.

    Anything a deployment can leave open should appear here by name, or
    somebody ships with it open and is never told. This lists them, so
    adding a switch and forgetting this file fails rather than passes.
    """
    named = set(posture(accounts=0, **dict(
        FLOOR, rig_tokens={}, rig_addresses={}, desk_token="",
        rig_rate_limit_per_min=0, session_cookie_secure=False,
        login_rate_limit_per_min=0, login_lockout_after=0)))

    for switch in ("rig auth", "rig identity", "desk auth", "rate limit",
                   "person auth", "session cookie",
                   "login rate limit", "login lockout"):
        assert switch in named, f"preflight says nothing about {switch}"


# ============================================ what the table check expects


def test_preflight_expects_every_table_the_models_define():
    """The check that would have caught the bug this test was written for.

    `check_tables` used to import ten domains by hand. `accounts` was
    added without anybody knowing that third list existed, so preflight
    expected eleven tables while the models defined thirteen - and would
    have reported a database with no `accounts` table as ready, on a
    service where every sign-in needs one.

    It has to run in a fresh interpreter. Inside pytest, conftest has
    already imported every model, so `Base.metadata` is complete however
    preflight behaves and the assertion passes against the broken code.
    That is exactly how the bug survived.
    """
    import json
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    probe = (
        "import json, sys;"
        "sys.path.insert(0, %r);"
        "from tools import preflight;"
        "preflight.load_every_domain();"
        "from core.base.model import Base;"
        "print(json.dumps(sorted(Base.metadata.tables)))" % str(root)
    )
    out = subprocess.run([sys.executable, "-c", probe],
                         capture_output=True, text=True, cwd=str(root))
    assert out.returncode == 0, out.stderr
    seen = set(json.loads(out.stdout.strip().splitlines()[-1]))

    on_disk = {p.parent.name for p in (root / "core" / "domains").glob("*/model.py")}
    assert on_disk, "no domains found at all - the probe is looking in the wrong place"

    # Every table this service has, including the ones a login needs.
    for table in ("accounts", "account_sessions", "schedule_pushes",
                  "schedules", "rig_events", "episodes"):
        assert table in seen, (
            f"preflight would not look for {table!r}, so it would pass a "
            f"database that is missing it. It expects: {sorted(seen)}")


def test_preflight_finds_a_domain_added_to_the_tree():
    """No list to remember. A directory with a model.py in it is found,
    which is the property that stops this drifting a third time."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    on_disk = {p.parent.name for p in (root / "core" / "domains").glob("*/model.py")}
    assert set(preflight.load_every_domain()) == on_disk
    assert "accounts" in on_disk


# ------------------------------------------ mint_account names a person
#
# The tool opens its own engine on the plain database; the suite lives in
# a private run schema (see conftest). So these drive `_act` - the whole
# of the tool past the parser - with a session from the run schema, the
# same confinement the app under test gets. `run(argv)` is what `main`
# calls and is exercised by the parser test above; the logic is here.

import argparse as _argparse


def _mint_args(**kw):
    """What the parser hands `_act` for `operator --person ...`."""
    base = dict(cmd="operator", email=None, name=None, person=None,
                password="a-long-enough-pw-12", generate=False)
    base.update(kw)
    return _argparse.Namespace(**base)


async def test_an_operator_is_minted_as_a_person_not_a_seat(engine, session, capsys):
    """`--person` names the row in `people`; `--operator-id` is gone. An
    account created by the tool points at who, never at where."""
    from core.domains.people.repository import PersonRepository
    from core.domains.accounts.repository import AccountRepository
    from tools import mint_account

    mei = await PersonRepository(session).create("Mei Chen")
    await session.commit()

    rc = await mint_account._act(
        _mint_args(email="m.chen@verlet.co", name="Mei Chen", person=str(mei.id)), session)
    assert rc == 0, capsys.readouterr().out
    acct = await AccountRepository(session).by_email("m.chen@verlet.co")
    assert acct is not None and acct.person_id == mei.id
    assert acct.operator_id is None, "a seat was taken onto the account"


async def test_minting_an_operator_for_nobody_is_refused(engine, session):
    from tools import mint_account
    import uuid
    with pytest.raises(SystemExit) as e:
        await mint_account._act(
            _mint_args(email="x@verlet.co", name="X", person=str(uuid.uuid4())), session)
    assert "nobody" in str(e.value).lower()


async def test_two_accounts_cannot_be_minted_for_one_person(engine, session):
    from core.domains.people.repository import PersonRepository
    from tools import mint_account
    mei = await PersonRepository(session).create("Mei Chen")
    await session.commit()
    assert await mint_account._act(
        _mint_args(email="one@verlet.co", name="Mei Chen", person=str(mei.id)), session) == 0
    with pytest.raises(SystemExit) as e:
        await mint_account._act(
            _mint_args(email="two@verlet.co", name="Mei Chen", person=str(mei.id)), session)
    assert "already signs in" in str(e.value)


def test_the_parser_asks_for_a_person_and_not_a_seat():
    """The CLI surface itself: --person is required for an operator and
    --operator-id no longer exists."""
    from tools import mint_account
    import asyncio
    with pytest.raises(SystemExit):
        asyncio.run(mint_account.run(["operator", "--email", "a@b.c", "--name", "A",
                                      "--operator-id", "op-a2", "--password", "x"]))
