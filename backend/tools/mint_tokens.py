"""Generate the per-rig credentials, and the two lines that carry them.

Twelve rigs, twelve tokens, and one place they end up: `RIG_TOKENS` in
the server's environment. The rig is handed its own by the service, which
decides from the address the machine called from.

    python -m tools.mint_tokens                  # the tokens
    python -m tools.mint_tokens --addresses      # and an address map to fill in

This used to write a `rig-config.js` per rig for Ansible to place on the
machines, and those files were never read by anything. The rig's page
loads `rig-config.js` relatively and the kiosk loads the page from the
server, so the browser asks the server for its copy - twelve machines got
one blank file and all became the same rig. Writing them is worse than
useless now: it is a convincing-looking step that does nothing, so it is
gone rather than deprecated.

One token per rig, never shared. A token names exactly one rig and is
refused for any other, which is the property that stops one compromised
machine attributing work across the floor - and it is worth nothing if
all twelve machines carry the same string.

`RIG_ADDRESSES` has to name the same rigs as `RIG_TOKENS`; preflight
compares them. The addresses are not this tool's to know - whoever owns
the floor network does - so `--addresses` prints the skeleton with the
right rigs in it rather than inventing values.

These are secrets. The output goes to a terminal, which is not a safe
place to leave them: put them where secrets live and clear the scrollback.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The floor, as CLAUDE.md defines it: twelve rigs, four groups of three.
DEFAULT_RIGS = [f"RIG-{i:02d}" for i in range(1, 13)]


def mint(rigs: list[str]) -> dict[str, str]:
    # token_urlsafe(32) is 256 bits. There is no reason to be frugal here:
    # it is typed by nobody and read by nobody.
    return {rig: secrets.token_urlsafe(32) for rig in rigs}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rigs", nargs="*", default=DEFAULT_RIGS,
                   help="rig ids; defaults to RIG-01..RIG-12")
    p.add_argument("--addresses", action="store_true",
                   help="also print a RIG_ADDRESSES skeleton to fill in")
    p.add_argument("--desk", action="store_true",
                   help="also mint a DESK_TOKEN for the schedule push")
    args = p.parse_args()

    tokens = mint(args.rigs)

    print("# ---- for the server's environment -------------------------")
    print("# One line. Do not commit it; .env is gitignored for this reason.")
    print()
    print("RIG_TOKENS=" + json.dumps(tokens, separators=(",", ":")))
    if args.desk:
        print("DESK_TOKEN=" + secrets.token_urlsafe(32))
    print()

    if args.addresses:
        print("# ---- which machine is which -------------------------------")
        print("# Fill in the address each rig calls from, then set this")
        print("# beside RIG_TOKENS. The service hands a rig its identity by")
        print("# matching the caller against this map, so a rig missing from")
        print("# it is never told who it is and refuses to start.")
        print("#")
        print("# The same rigs as above, and preflight checks that it is.")
        print()
        skeleton = {rig: "CHANGE-ME" for rig in tokens}
        print("RIG_ADDRESSES=" + json.dumps(skeleton, separators=(",", ":")))
        print()

    print("# Verify with:  curl -s http://HOST/api/health")
    print('# It should answer  "rigAuth":"on"  and  "rigIdentity":"on"')
    print("# once the server has both lines.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
