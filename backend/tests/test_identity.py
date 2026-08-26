"""Which rig is calling, decided by the server rather than by a file
nothing reads.

The bug this closes: `apps/rig/index.html` loads `rig-config.js` by a
relative path and the kiosk loads the page from the server, so a config
placed on the rig machine is never read. Twelve machines loaded one blank
file and every one of them became the same hard-coded default - and from
the service's side twelve rigs reporting as one is indistinguishable from
one very busy rig.
"""

import pytest

from core.infrastructure.config import Settings, get_settings
from services.rigs.identity import caller_address, config_js, rig_at

ADDRESSES = {"RIG-01": "10.0.0.11", "RIG-07": "10.0.0.17"}
TOKENS = {"RIG-01": "token-one", "RIG-07": "token-seven"}


# ================================================= the rules, no service

class TestWhichRigIsThis:
    def test_a_known_address_is_the_rig_configured_at_it(self):
        assert rig_at("10.0.0.17", ADDRESSES) == "RIG-07"

    def test_an_address_no_rig_uses_is_nobody(self):
        assert rig_at("10.0.0.99", ADDRESSES) is None

    def test_no_address_at_all_is_nobody(self):
        assert rig_at(None, ADDRESSES) is None

    def test_nothing_configured_means_nobody_rather_than_everybody(self):
        assert rig_at("10.0.0.11", {}) is None

    def test_two_rigs_at_one_address_resolve_to_neither(self):
        """A provisioning mistake must not resolve to whichever came
        first. That is the same tie the schedule picker was fixed for, and
        the same answer: refuse."""
        both = {"RIG-01": "10.0.0.11", "RIG-02": "10.0.0.11"}
        assert rig_at("10.0.0.11", both) is None

    def test_a_malformed_entry_names_nobody_rather_than_everybody(self):
        assert rig_at("10.0.0.11", {"RIG-01": "not-an-address"}) is None

    def test_the_same_address_written_differently_is_the_same_address(self):
        """Compared as addresses, not as strings, so a leading zero or a
        v6 spelling does not silently create a second machine."""
        assert rig_at("10.0.0.7", {"RIG-01": " 10.0.0.7 "}) == "RIG-01"
        assert rig_at("::1", {"RIG-01": "0:0:0:0:0:0:0:1"}) == "RIG-01"


class TestWhoOpenedTheSocket:
    def test_with_no_proxy_the_peer_is_the_caller(self):
        assert caller_address("10.0.0.17", None) == "10.0.0.17"

    def test_the_loopback_proxy_is_believed(self):
        """nginx runs on the service host and overwrites X-Real-IP with
        the peer it actually saw."""
        assert caller_address("127.0.0.1", "10.0.0.17") == "10.0.0.17"

    def test_a_remote_caller_cannot_claim_to_be_somewhere_else(self):
        """The whole spoofing defence. If any peer could set the header,
        any caller could collect any rig's token and the address map would
        be decoration."""
        assert caller_address("10.0.0.99", "10.0.0.17") == "10.0.0.99"

    def test_a_remote_caller_forging_the_header_is_still_itself(self):
        assert rig_at(caller_address("10.0.0.99", "10.0.0.11"), ADDRESSES) is None


class TestWhatTheFileSays:
    def test_a_known_rig_is_given_its_id_and_its_own_token(self):
        js = config_js("RIG-07", "token-seven", "10.0.0.17")
        assert '"RIG-07"' in js and '"token-seven"' in js

    def test_an_unknown_caller_is_given_a_file_that_names_nobody(self):
        js = config_js(None, None, "10.0.0.99")
        assert "window.RIG_ID = null" in js
        assert "window.RIG_TOKEN = null" in js

    def test_an_unknown_caller_is_never_given_a_token(self):
        js = config_js(None, None, "10.0.0.99")
        for token in TOKENS.values():
            assert token not in js


# ==================================================== the route, served

async def _config(client, peer="10.0.0.17", real_ip=None):
    headers = {"x-real-ip": real_ip} if real_ip else {}
    # httpx/ASGI reports the peer as "testclient" unless told otherwise;
    # the transport's client tuple is what request.client.host reads.
    client._transport.client = (peer, 50000)
    return await client.get("/api/rigs/config.js", headers=headers)


@pytest.fixture
def floor(monkeypatch):
    """A configured floor: two rigs, each with an address and a token."""
    get_settings.cache_clear()
    monkeypatch.setenv("RIG_ADDRESSES", '{"RIG-01": "10.0.0.11", "RIG-07": "10.0.0.17"}')
    monkeypatch.setenv("RIG_TOKENS", '{"RIG-01": "token-one", "RIG-07": "token-seven"}')
    yield
    get_settings.cache_clear()


async def test_the_settings_read_both_maps_from_the_environment(floor):
    s = get_settings()
    assert s.rig_addresses == ADDRESSES
    assert s.rig_tokens == TOKENS


async def test_a_rig_is_told_which_rig_it_is(client, floor):
    r = await _config(client, peer="10.0.0.17")
    assert r.status_code == 200
    assert '"RIG-07"' in r.text
    assert '"token-seven"' in r.text


async def test_a_rig_is_never_handed_another_rig_s_token(client, floor):
    """The property the whole scheme exists for. One machine that can read
    the floor's tokens can attribute work across the floor, and nothing
    downstream could tell."""
    r = await _config(client, peer="10.0.0.17")
    assert "token-one" not in r.text, "RIG-07 was handed RIG-01's token"


async def test_a_machine_at_an_unknown_address_is_told_it_is_nobody(client, floor):
    r = await _config(client, peer="10.0.0.99")
    assert r.status_code == 200, "a real file, so the page loads and can say what is wrong"
    assert "window.RIG_ID = null" in r.text
    for token in TOKENS.values():
        assert token not in r.text


async def test_the_caller_cannot_ask_to_be_a_particular_rig(client, floor):
    """There is no rig id in the path and no parameter that changes the
    answer, so there is nothing to ask for."""
    r = await client.get("/api/rigs/config.js?rig=RIG-01&rigId=RIG-01",
                         headers={"authorization": "Bearer token-one"})
    assert "token-one" not in r.text
    assert "token-seven" not in r.text


async def test_a_forged_forwarded_header_from_a_remote_peer_is_ignored(client, floor):
    r = await _config(client, peer="10.0.0.99", real_ip="10.0.0.17")
    assert "token-seven" not in r.text, "a caller talked its way into RIG-07's token"
    assert "window.RIG_ID = null" in r.text


async def test_the_loopback_proxy_is_believed_so_nginx_works(client, floor):
    r = await _config(client, peer="127.0.0.1", real_ip="10.0.0.17")
    assert '"RIG-07"' in r.text and '"token-seven"' in r.text


async def test_it_is_never_cached(client, floor):
    """A stale copy is a rig filing every episode under another rig's
    name, which is exactly what this route exists to stop."""
    r = await _config(client, peer="10.0.0.17")
    assert "no-store" in r.headers.get("cache-control", "")


async def test_a_floor_with_no_addresses_configured_identifies_nobody(client):
    """The laptop case. Nothing set, so nobody is named - and with rig
    auth also off, the rig treats that as a demo rather than a fault."""
    get_settings.cache_clear()
    r = await _config(client, peer="10.0.0.17")
    assert "window.RIG_ID = null" in r.text
