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

from core.infrastructure.database import sessionmaker
from core.workflows.video import drain_batch, expire_archive, expire_pending
from workers._signals import install_signal_handlers, is_shutdown_requested

log = logging.getLogger("drain_to_archive")


async def run(poll_secs: float, batch: int, exit_after_empty: int,
              keep_days: int = 0, pending_days: int = 0) -> int:
    maker = sessionmaker()
    empty = total = attempt = 0
    backoff_base = 2

    while not is_shutdown_requested():
        try:
            async with maker() as session:
                count, keys = await drain_batch(session, limit=batch)
                # Retention runs beside the drain rather than as its own
                # worker: they are the two ends of one lifecycle and the
                # cadence that suits one suits the other. Does nothing at
                # all unless a policy has been set.
                # Takes the rig asked to upload and never delivered. Not
                # a deletion - the bytes were never here - just the
                # service no longer counting them as owed.
                if pending_days > 0:
                    await expire_pending(session, after_days=pending_days)

                if keep_days > 0:
                    gone, gone_bytes = await expire_archive(
                        session, keep_days=keep_days, limit=batch)
                    if gone:
                        log.info("expired %d takes (%.1f GB)", gone, gone_bytes / 1e9)
            attempt = 0
        except Exception:
            attempt += 1
            wait = min(backoff_base ** attempt, 60)
            log.exception("drain failed, retrying in %ss", wait)
            # await, not time.sleep: this is an async function, and a
            # blocking sleep here stops the whole loop it runs on.
            await asyncio.sleep(wait)
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
    from core.infrastructure.config import get_settings

    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--poll-secs", type=float, default=5.0)
    p.add_argument("--batch", type=int, default=50)
    p.add_argument("--exit-after-empty", type=int, default=0)
    p.add_argument("--pending-days", type=int, default=s.video_pending_after_days,
                   help="stop waiting for uploads older than this; 0 waits for ever")
    p.add_argument("--keep-days", type=int, default=s.video_keep_days,
                   help="delete archived video older than this; 0 keeps it for ever")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    install_signal_handlers()
    asyncio.run(run(args.poll_secs, args.batch, args.exit_after_empty,
                    args.keep_days, args.pending_days))


if __name__ == "__main__":
    main()
