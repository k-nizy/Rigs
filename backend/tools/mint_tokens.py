"""Generate the per-rig credentials, and the files that carry them.

Twelve rigs, twelve tokens, and two places each one has to end up:

    the server   RIG_TOKENS in its environment, as JSON
    the rig      rig-config.js on that machine, with its id and its token

Both are produced here so they cannot drift. A rig whose token is not in
the server's list is refused, and a rig with the wrong id files every
episode under somebody else's name - so the two files are generated from
one run rather than assembled by hand twice.

    python -m tools.mint_tokens                    # print, write nothing
    python -m tools.mint_tokens --out ./secrets    # write the config files

One token per rig, never shared. A token names exactly one rig and is
refused for any other, which is the property that stops one compromised
machine attributing work across the floor - and it is worth nothing if
all twelve machines carry the same string.

These are secrets. The output goes to a terminal and, if you ask, to
files. Neither is a safe place to leave them: put them where secrets
live, hand the config files to Ansible, and delete what is left.
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

CONFIG = '''/* Written by tools/mint_tokens.py. One per machine.
 *
 * This file is the only thing that differs between the twelve rigs, and
 * it is what stops all of them believing they are the same one.
 *
 * It carries a secret. It should be readable by the browser the kiosk
 * runs as and by nobody else.
 */
window.RIG_ID    = "{rig}";
window.RIG_TOKEN = "{token}";
'''


def mint(rigs: list[str]) -> dict[str, str]:
    # token_urlsafe(32) is 256 bits. There is no reason to be frugal here:
    # it is typed by nobody and read by nobody.
    return {rig: secrets.token_urlsafe(32) for rig in rigs}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rigs", nargs="*", default=DEFAULT_RIGS,
                   help="rig ids; defaults to RIG-01..RIG-12")
    p.add_argument("--out", type=Path, default=None,
                   help="directory to write per-rig rig-config.js into")
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

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for rig, token in tokens.items():
            path = args.out / rig / "rig-config.js"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(CONFIG.format(rig=rig, token=token), encoding="utf-8")
        print(f"# wrote {len(tokens)} config files under {args.out}/")
        print("# Each goes to apps/rig/rig-config.js on that machine, and")
        print("# nowhere else. Delete this directory once Ansible has them.")
    else:
        print("# ---- for each rig -----------------------------------------")
        print("# Re-run with --out DIR to write these as files.")
        print()
        for rig, token in tokens.items():
            print(f'#   {rig}: window.RIG_ID = "{rig}"; '
                  f'window.RIG_TOKEN = "{token[:6]}..."')

    print()
    print("# Verify with:  curl -s http://HOST/api/health")
    print('# It should answer  "rigAuth":"on"  once the server has these.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
