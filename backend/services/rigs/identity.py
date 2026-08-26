"""Which rig is calling, before it has said anything.

Every other route on this service knows who it is talking to because the
caller presented a token. This is the route that hands the token over, so
it cannot ask for one; it has to work out who the caller is from what it
can observe.

The reason it exists at all is a deployment fact that is easy to miss.
`apps/rig/index.html` loads `rig-config.js` with a relative path, and the
kiosk browser loads the page **from the server**. So the file a
provisioning tool writes onto the rig machine is never read by anything -
the browser asks the server for its copy. Twelve machines therefore
loaded one blank file, fell back to the same hard-coded default, and every
one of them believed it was the same rig. Nothing downstream could see it:
from here, twelve rigs reporting as one is exactly what one very busy rig
looks like.

So the server decides, per caller, and the rig is told. The address is not
a secret and is not treated as one - it selects an identity, the token
inside that identity is what authenticates, and every route after this one
still checks it.
"""

from __future__ import annotations

import ipaddress
import json

# Addresses the service will believe a forwarded header from. nginx runs
# on the same host in the deployment this ships with - `upstream
# rigs_service { server 127.0.0.1:8000; }` - so the only peer allowed to
# say "the real client was somewhere else" is the loopback one.
#
# This is the whole of the spoofing defence and it is worth being plain
# about why it is shaped this way. If any peer could set the header, then
# any caller could claim any address and so collect any rig's token, and
# the map below would be decoration. Trusting a header only from loopback
# means an attacker has to already be running on the service host, at
# which point the token file is theirs anyway.
_LOOPBACK = ipaddress.ip_network("127.0.0.0/8")
_LOOPBACK6 = ipaddress.ip_network("::1/128")


def _is_loopback(addr: str | None) -> bool:
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip in _LOOPBACK or ip in _LOOPBACK6


def caller_address(peer: str | None, real_ip: str | None) -> str | None:
    """The address to judge this request by.

    `peer` is who actually opened the socket; `real_ip` is what a proxy
    said. The proxy is believed only when it is the loopback one, because
    nginx overwrites `X-Real-IP` with the true peer it saw and a client
    cannot forge a header on a hop it does not control.
    """
    if _is_loopback(peer) and real_ip:
        return real_ip.strip() or None
    return peer


def rig_at(address: str | None, addresses: dict[str, str]) -> str | None:
    """Which rig calls from this address, or None.

    Two rigs configured at one address is a provisioning mistake that must
    not resolve to whichever happens to be first - that is the same tie
    the schedule picker was fixed for. It resolves to nobody, loudly.
    """
    if not address:
        return None
    try:
        want = ipaddress.ip_address(address)
    except ValueError:
        return None

    found = []
    for rig_id, configured in addresses.items():
        try:
            if ipaddress.ip_address(str(configured).strip()) == want:
                found.append(rig_id)
        except ValueError:
            continue        # a malformed entry names nobody, rather than everybody
    return found[0] if len(found) == 1 else None


def config_js(rig_id: str | None, token: str | None, address: str | None) -> str:
    """The file `apps/rig/index.html` loads, rendered for one caller.

    A caller we cannot place gets a real file that names nobody rather
    than a 404, so the page still loads and the rig can say what is wrong
    on its own screen. A rig that silently becomes the default is the
    failure this whole module exists to prevent; a rig that says "this
    machine has no identity" is one somebody can fix.
    """
    seen = json.dumps(address or "unknown")
    if not rig_id:
        return (
            "/* Served by the rigs service. This machine is not one of the\n"
            "   rigs on the floor: no RIG_ADDRESSES entry matches the address\n"
            f"   it called from, {address or 'unknown'}.\n"
            "\n"
            "   Nothing is set on purpose. The rig reads that as having no\n"
            "   identity and refuses to work rather than filing takes under\n"
            "   somebody else's name. */\n"
            f"window.RIG_ID = null;\nwindow.RIG_TOKEN = null;\nwindow.RIG_SEEN_AS = {seen};\n"
        )

    return (
        "/* Served by the rigs service, for this machine only. Do not copy\n"
        "   it to another rig: the token names one rig and is refused for\n"
        "   any other, which is what stops one machine attributing work\n"
        "   across the floor. */\n"
        f"window.RIG_ID = {json.dumps(rig_id)};\n"
        f"window.RIG_TOKEN = {json.dumps(token or '')};\n"
        f"window.RIG_SEEN_AS = {seen};\n"
    )
