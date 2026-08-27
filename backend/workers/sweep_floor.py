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
import time

from core.domains.accounts.repository import AccountSessionRepository
from core.infrastructure.config import get_settings
from core.infrastructure.database import sessionmaker
from core.workflows.floor import sweep
from workers._signals import install_signal_handlers, is_shutdown_requested

log = logging.getLogger("sweep_floor")


async def clear_dead_sessions(maker, log_to=log) -> int:
    """Delete session rows that can no longer authenticate anybody.

    Housekeeping, not security - `live()` already refuses an expired row,
    so nothing here is load-bearing. It rides on this worker because this
    is the process that already wakes up on a timer, but it is emphatically
    not part of the sweep: the sweep is absence detection on a fifteen
    second cadence, and a few dozen dead rows a day do not want looking at
    that often.

    Its own try/except for the same reason. A failure to tidy up must not
    stop the floor being watched.
    """
    try:
        async with maker() as session:
            gone = await AccountSessionRepository(session).purge_expired()
            await session.commit()
        if gone:
            log_to.info("cleared %d expired session(s)", gone)
        return gone
    except Exception:
        log_to.exception("could not clear expired sessions")
        return 0


async def run(every_secs: float, once: bool, silent_after: int, idle_grace: int,
              clock_tolerance: int = 120, projection_behind_after: int = 120,
              purge_every_secs: float = 3600.0, clock=time.monotonic) -> None:
    maker = sessionmaker()
    attempt = 0
    backoff_base = 2
    # Due immediately on the first pass, then hourly.
    next_purge = clock()

    while not is_shutdown_requested():
        try:
            async with maker() as session:
                result = await sweep(
                    session,
                    silent_after_secs=silent_after,
                    idle_grace_secs=idle_grace,
                    clock_tolerance_secs=clock_tolerance,
                    projection_behind_after_secs=projection_behind_after,
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

        if purge_every_secs > 0 and clock() >= next_purge:
            next_purge = clock() + purge_every_secs
            await clear_dead_sessions(maker)

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
    p.add_argument("--clock-tolerance", type=int, default=s.clock_skew_tolerance_secs)
    p.add_argument("--projection-behind-after", type=int,
                   default=s.projection_behind_after_secs)
    p.add_argument("--purge-every-secs", type=float, default=3600.0,
                   help="how often to clear expired sign-in sessions; 0 to never")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    install_signal_handlers()
    asyncio.run(run(args.every_secs, args.once, args.silent_after, args.idle_grace,
                    args.clock_tolerance, args.projection_behind_after,
                    args.purge_every_secs))


if __name__ == "__main__":
    main()
