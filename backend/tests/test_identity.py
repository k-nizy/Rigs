"""Which rig is calling, decided by the server rather than by a file
nothing reads.

The bug this closes: `apps/rig/index.html` loads `rig-config.js` by a
relative path and the kiosk loads the page from the server, so a config
placed on the rig machine is never read. Twelve machines loaded one blank
file and every one of them became the same hard-coded default - and from
the service's side twelve rigs reporting as one is indistinguishable from
one very busy rig.
"""

import re
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from core.infrastructure.config import Settings, get_settings
from services.rigs.identity import caller_address, config_js, rig_at

REPO = Path(__file__).resolve().parents[2]

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


# =============================================== the deploy, on a floor
#
# Everything above proves the service answers correctly. None of it
# proves the question ever reaches the service.
#
# On a floor the page asks for `rig-config.js` by a relative path, and
# what answers is whatever the web server puts at that URL. There is a
# real file at exactly that path in this repository - the placeholder,
# checked in so the demo runs on a laptop with nothing behind it - so
# "just serve the folder" is always a configuration that starts, and it
# is the broken one. Every rig reads the placeholder, nobody is
# identified, and the service is never asked. That is the original bug,
# and it lives entirely in files no test above reads.
#
# So both deployments are checked here, by the strongest means each
# allows. The gateway is checked by asking it, which reproduces the bug
# exactly: if the static mount wins, the assertion sees the placeholder.
# nginx is checked by reading it, because there is no nginx on a
# developer's machine. That is weaker and worth saying plainly - it
# catches a block deleted, retargeted, or stripped of the header the
# service depends on, and it cannot catch a syntax error. `nginx -t` on
# the box is the other half, and this does not replace it.

PLACEHOLDER = REPO / "apps" / "rig" / "rig-config.js"
NGINX = (REPO / "deploy" / "nginx.conf").read_text(encoding="utf-8")

# The one URL all three have to agree on. Checked against `index.html`
# below rather than trusted, because a rename would leave the nginx block
# and the gateway route answering a path nobody asks for - while the
# static folder answers the new one, which is the bug again.
CONFIG_URL = "/apps/rig/rig-config.js"


def _block(conf: str, header: str) -> str:
    """What is inside one `location` block, found by its opening line.

    Searching the whole file for a directive proves nothing about where
    that directive is. `no-store` on the static folder is not `no-store`
    on the proxy, and reading the first as the second is how a config
    passes a test while serving a stale identity.
    """
    assert header in conf, f"nginx.conf has no `{header}` block"
    start = conf.index(header)
    depth = 0
    for i in range(start, len(conf)):
        if conf[i] == "{":
            depth += 1
        elif conf[i] == "}":
            depth -= 1
            if depth == 0:
                return conf[start:i + 1]
    raise AssertionError(f"`{header}` is never closed")


def _lf(text: str) -> str:
    """One line ending, so a comparison is about content."""
    return "\n".join(text.splitlines()).strip()


async def _via_gateway(client, peer):
    """The URL the page actually asks for, from a given machine.

    Deliberately not `/api/rigs/config.js`: the whole question here is
    which of the two things listening at *this* path answers.
    """
    client._transport.client = (peer, 50000)
    return await client.get(CONFIG_URL)


@pytest_asyncio.fixture
async def gateway():
    """The development gateway over ASGI, static mounts and all.

    No database needed: the identity route reads the settings and the
    peer address and nothing else, and httpx does not run lifespan, so
    the three workers stay asleep.
    """
    import local_gateway

    transport = ASGITransport(app=local_gateway.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestTheDevelopmentGateway:
    async def test_the_service_answers_this_path_not_the_folder(self, gateway, floor):
        """The bug, reproduced. `/apps/rig` is mounted as static files and
        a real `rig-config.js` sits inside it, so this one URL has two
        possible answers and only one of them identifies anybody."""
        r = await _via_gateway(gateway, peer="10.0.0.17")
        assert r.status_code == 200
        assert '"RIG-07"' in r.text, (
            "the folder answered: this machine was handed the placeholder, "
            "which names no rig, and would have run as the demo default")
        assert '"token-seven"' in r.text

    async def test_the_placeholder_is_not_what_came_back(self, gateway, floor):
        """Compared against the file itself rather than a marker string
        taken from it, so editing the placeholder cannot quietly weaken
        this.

        Both sides are flattened to one line ending first. Without that
        this test passes while the placeholder is being served: the file
        is checked out CRLF, `read_text` quietly translates it to LF, and
        the static mount hands back the bytes untouched - so the two
        differ by nothing that matters and `!=` is satisfied. It was
        found by deleting the route and watching this test stay green.
        """
        r = await _via_gateway(gateway, peer="10.0.0.17")
        assert _lf(r.text) != _lf(PLACEHOLDER.read_text(encoding="utf-8"))

    async def test_an_unknown_machine_is_told_so_rather_than_falling_through(
            self, gateway, floor):
        """The route has to answer both cases. Falling through to the
        folder for the callers it cannot place would name nobody and look
        exactly like a laptop, which is the one thing that must not happen
        on a floor."""
        r = await _via_gateway(gateway, peer="10.0.0.99")
        assert "window.RIG_ID = null" in r.text
        for token in TOKENS.values():
            assert token not in r.text

    async def test_the_rest_of_the_folder_is_still_served(self, gateway):
        """The balance. One route wins one URL - removing the mount to
        achieve that would take the whole app off the air."""
        r = await gateway.get("/apps/rig/assets/rig.js")
        assert r.status_code == 200


class TestTheFloorsWebServer:
    def test_the_identity_path_is_proxied_to_the_service(self):
        block = _block(NGINX, f"location = {CONFIG_URL}")
        assert re.search(r"proxy_pass\s+http://\S*/api/rigs/config\.js\s*;", block), (
            "nginx.conf no longer sends this path to the service, so the "
            "checked-in placeholder answers it and every rig is the same rig")

    def test_it_is_an_exact_match_so_the_folder_cannot_shadow_it(self):
        """`location =` beats any prefix wherever it sits in the file, and
        that one character is what makes the ordering here not matter -
        unlike the gateway above, where the route must be declared before
        the mount. Written as a prefix it would still win today by being
        longer than `/apps/rig/`, and would stop winning the moment
        anything more specific was added.
        """
        assert f"location = {CONFIG_URL}" in NGINX
        assert f"location {CONFIG_URL}" not in NGINX

    def test_the_proxy_tells_the_service_which_machine_called(self):
        """Without this the service sees the proxy's own address for every
        rig on the floor: twelve machines, one caller, which is the shape
        of the original bug. It has to be `$remote_addr` rather than the
        header as it arrived - the service believes this header from
        loopback, so forwarding a caller's own copy would let any machine
        name itself any rig and be handed that rig's token.
        """
        block = _block(NGINX, f"location = {CONFIG_URL}")
        assert re.search(r"proxy_set_header\s+X-Real-IP\s+\$remote_addr\s*;", block)

    def test_the_answer_is_never_cached(self):
        """A stale copy is a rig filing every episode under another rig's
        name. The service says this as well; both of them, because either
        one alone is a single point of failure for an error nothing
        downstream can see."""
        block = _block(NGINX, f"location = {CONFIG_URL}")
        assert "no-store" in block

    def test_all_three_agree_on_the_url_the_page_asks_for(self):
        """The page's own `<script src>`, resolved against the folder it
        is served from. If that is ever renamed, the nginx block and the
        gateway route go on answering a URL nobody requests while the
        static folder answers the new one - silently, and correctly as far
        as every other test here can tell.
        """
        html = (REPO / "apps" / "rig" / "index.html").read_text(encoding="utf-8")
        src = re.search(r'<script src="([^"]*rig-config[^"]*\.js)"', html).group(1)
        assert not src.startswith(("/", "../")), (
            "loaded from somewhere other than the rig's own folder, so the "
            "two locations below are guarding the wrong path")
        assert "/apps/rig/" + src == CONFIG_URL

        assert f"location = {CONFIG_URL}" in NGINX

        import local_gateway

        paths = [r.path for r in local_gateway.app.routes if hasattr(r, "path")]
        assert CONFIG_URL in paths


async def test_a_head_request_is_answered_by_the_service_too(client, floor):
    """The same question asked with a different verb.

    `@router.get` registers GET alone - FastAPI's APIRoute, unlike
    Starlette's plain Route, does not add HEAD for you. So HEAD fell
    through: on `local_gateway` to the static mount, which served the
    1405-byte placeholder that names nobody, and behind nginx - where
    `location =` matches every method - to a 405.

    Two deployments, two different wrong answers to "which rig am I",
    which is the exact shape of the bug this whole file exists to close.
    Nothing asks this way today, because the page uses a script tag. That
    is why it is worth pinning rather than leaving to be discovered.
    """
    client._transport.client = ("10.0.0.17", 50000)
    r = await client.head("/api/rigs/config.js")
    assert r.status_code == 200, (
        "HEAD reached no route: %s. The placeholder or a 405 is what a "
        "caller gets instead of its identity." % r.status_code
    )
    assert "javascript" in r.headers["content-type"], (
        "the page loads this with a script tag; a HEAD that described it "
        "as something else would be describing a different resource"
    )


async def test_head_and_get_describe_the_same_answer(client, floor):
    """A HEAD whose headers disagree with the GET is its own trap."""
    client._transport.client = ("10.0.0.17", 50000)
    got = await client.get("/api/rigs/config.js")
    head = await client.head("/api/rigs/config.js")
    assert head.status_code == got.status_code
    assert head.headers.get("cache-control") == got.headers.get("cache-control"), (
        "a cached rig-config is a rig filing episodes under another rig's name"
    )
