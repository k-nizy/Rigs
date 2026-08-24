"""Look for what did not happen.

This worker claims nothing. It is a timer, and that is the point: the two
most valuable alerts on this floor are a rig sitting idle because nobody
arrived, and a rig that lost power. Neither produces an event, so neither
can be subscribed to. The only way to notice them is to go and look.

    python -m workers.sweep_floor
    python -m workers.sweep_floor --once
"""

import argparse
import asyncio
import logging

from core.infrastructure.config import get_settings
from core.infrastructure.database import sessionmaker
from core.workflows.floor import sweep
from workers._signals import install_signal_handlers, is_shutdown_requested

log = logging.getLogger("sweep_floor")


async def run(every_secs: float, once: bool, silent_after: int, idle_grace: int) -> None:
    maker = sessionmaker()
    attempt = 0
    backoff_base = 2

    while not is_shutdown_requested():
        try:
            async with maker() as session:
                result = await sweep(
                    session, silent_after_secs=silent_after, idle_grace_secs=idle_grace
                )
            attempt = 0
            if result["opened"] or result["resolved"]:
                log.info(
                    "%d open now (+%d, -%d)",
                    result["found"], result["opened"], result["resolved"],
                )
            else:
                log.debug("%d open, nothing changed", result["found"])
        except Exception:
            attempt += 1
            wait = min(backoff_base ** attempt, 60)
            log.exception("sweep failed, retrying in %ss", wait)
            # await, not time.sleep: this is an async function, and a
            # blocking sleep here stops the whole loop it runs on.
            await asyncio.sleep(wait)
            continue

        if once:
            return
        await asyncio.sleep(every_secs)

    log.info("shutting down")


def main() -> None:
    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--every-secs", type=float, default=15.0)
    p.add_argument("--once", action="store_true", help="sweep once and exit")
    p.add_argument("--silent-after", type=int, default=s.rig_silent_after_secs)
    p.add_argument("--idle-grace", type=int, default=300)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    install_signal_handlers()
    asyncio.run(run(args.every_secs, args.once, args.silent_after, args.idle_grace))


if __name__ == "__main__":
    main()
