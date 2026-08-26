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


class FakeSettings:
    """Only the fields check_posture reads."""

    def __init__(self, **kw):
        self.rig_tokens = kw.get("rig_tokens", {})
        self.rig_addresses = kw.get("rig_addresses", {})
        self.desk_token = kw.get("desk_token", "")
        self.rig_rate_limit_per_min = kw.get("rig_rate_limit_per_min", 0)
        self.video_keep_days = kw.get("video_keep_days", 0)
        self.database_url = kw.get(
            "database_url", "postgresql+asyncpg://u:p@127.0.0.1:5432/rigs")
        self.test_database_url = kw.get("test_database_url", "")


@pytest.fixture(autouse=True)
def clean_results():
    """`preflight.results` is module-global and printed at the end. A test
    that leaves rows in it changes the next test's verdict."""
    preflight.results.clear()
    yield
    preflight.results.clear()


def posture(**kw) -> dict[str, tuple[str, str]]:
    """Run the checks and return {name: (state, detail)}."""
    preflight.check_posture(FakeSettings(**kw))
    return {name: (state, detail) for state, name, detail in preflight.results}


FLOOR = dict(
    rig_tokens={"RIG-01": "t1", "RIG-02": "t2"},
    rig_addresses={"RIG-01": "10.0.0.11", "RIG-02": "10.0.0.12"},
    desk_token="d",
    rig_rate_limit_per_min=120,
    video_keep_days=90,
    test_database_url="",
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
    ):
        preflight.results.clear()
        preflight.check_posture(FakeSettings(**case))
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
