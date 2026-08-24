"""Claim outstanding ledger rows and project them into the fact tables.

The standard worker shape: a claim-and-process poll loop, never one-shot
by default, with an exit-after-N-empty-polls flag for batch and pod use.

    python -m workers.project_events
    python -m workers.project_events --exit-after-empty 1   # drain and stop
    python -m workers.project_events --replay               # wipe and rebuild
"""

import argparse
import asyncio
import logging
import time

from core.infrastructure.database import sessionmaker
from core.workflows.projection import project_batch, reset_projections
from workers._signals import install_signal_handlers, is_shutdown_requested

log = logging.getLogger("project_events")


async def run(poll_secs: float, batch: int, exit_after_empty: int, replay: bool) -> int:
    maker = sessionmaker()

    if replay:
        async with maker() as session:
            await reset_projections(session)
        log.warning("projections wiped; the ledger will be rebuilt from the start")

    empty = 0
    total = 0
    attempt = 0
    backoff_base = 2

    while not is_shutdown_requested():
        try:
            async with maker() as session:
                count, done = await project_batch(session, limit=batch)
            attempt = 0
        except Exception:
            # Exponential backoff, not a library. A database that is down
            # comes back; a worker that gives up does not.
            attempt += 1
            wait = min(backoff_base ** attempt, 60)
            log.exception("projection failed, retrying in %ss", wait)
            time.sleep(wait)
            continue

        if count:
            total += count
            empty = 0
            log.info("projected %d events (%d total)", count, total)
        else:
            empty += 1
            if exit_after_empty and empty >= exit_after_empty:
                log.info("ledger drained after %d events", total)
                return total
            await asyncio.sleep(poll_secs)

    log.info("shutting down after %d events", total)
    return total


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--poll-secs", type=float, default=2.0)
    p.add_argument("--batch", type=int, default=500)
    p.add_argument("--exit-after-empty", type=int, default=0,
                   help="stop after this many empty polls; 0 runs forever")
    p.add_argument("--replay", action="store_true",
                   help="wipe every fact table and rebuild from the ledger")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    install_signal_handlers()
    asyncio.run(run(args.poll_secs, args.batch, args.exit_after_empty, args.replay))


if __name__ == "__main__":
    main()
