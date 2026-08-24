"""Copy what has landed on the spool to the cold tier.

The one slow conversation in the system. Everything upstream runs at wire
speed on the local switch; this can be hours behind without any rig
noticing, which is the entire argument for having a spool at all.

    python -m workers.drain_to_archive
    python -m workers.drain_to_archive --exit-after-empty 1
"""

import argparse
import asyncio
import logging
import time

from core.infrastructure.database import sessionmaker
from core.workflows.video import drain_batch
from workers._signals import install_signal_handlers, is_shutdown_requested

log = logging.getLogger("drain_to_archive")


async def run(poll_secs: float, batch: int, exit_after_empty: int) -> int:
    maker = sessionmaker()
    empty = total = attempt = 0
    backoff_base = 2

    while not is_shutdown_requested():
        try:
            async with maker() as session:
                count, keys = await drain_batch(session, limit=batch)
            attempt = 0
        except Exception:
            attempt += 1
            wait = min(backoff_base ** attempt, 60)
            log.exception("drain failed, retrying in %ss", wait)
            time.sleep(wait)
            continue

        if count:
            total += count
            empty = 0
            log.info("archived %d objects (%d total)", count, total)
        else:
            empty += 1
            if exit_after_empty and empty >= exit_after_empty:
                log.info("spool drained after %d objects", total)
                return total
            await asyncio.sleep(poll_secs)

    log.info("shutting down after %d objects", total)
    return total


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--poll-secs", type=float, default=5.0)
    p.add_argument("--batch", type=int, default=50)
    p.add_argument("--exit-after-empty", type=int, default=0)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    install_signal_handlers()
    asyncio.run(run(args.poll_secs, args.batch, args.exit_after_empty))


if __name__ == "__main__":
    main()
