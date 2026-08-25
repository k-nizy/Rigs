"""A ceiling on how fast one rig may talk to this service.

Off unless configured, because the platform team's gateway may already
own this and two rate limiters disagreeing is worse than one. It exists
so that a deployment without one in front is not obliged to leave ingest
open to a rig in a retry loop.

**A token bucket, not a fixed window.** The floor's sustained rate is
about 0.01 requests per second per rig, so a limit sized for the average
would be absurd. What actually happens is bursts: a rig that has been off
the network for an hour comes back with a backlog and sends it in batches
as fast as it can. That is correct behaviour and must not be punished.
A bucket lets the burst through and then holds the rig to the average.

**Being limited is safe, which is why this can exist at all.** A 429 is
just a failed request to the rig's uploader: it keeps the events, backs
off, and tries again, and ingest dedupes on `(rigId, eventId)` so the
retry costs nothing. No event is lost by being refused. That property is
what makes a limiter appropriate here rather than dangerous.

**In-process, and honest about it.** Each instance has its own buckets,
so N instances allow N times the limit. For a twelve-rig floor behind one
or two processes that is fine, and pretending otherwise would mean a
shared store this service does not need. A limiter that has to be exactly
right across instances is a gateway's job, and they have a gateway.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# A caller with no bucket yet is not tracked until it asks. This caps how
# many we will ever track, so an unauthenticated deployment cannot be made
# to allocate for ever by inventing rig ids.
MAX_TRACKED = 4096


@dataclass
class _Bucket:
    tokens: float
    last: float


@dataclass
class RateLimiter:
    """`per_minute` requests sustained, `burst` allowed at once.

    `per_minute = 0` disables it entirely and `allow()` always says yes,
    so callers do not need to check twice.
    """

    per_minute: int
    burst: int = 0
    clock: object = time.monotonic
    _buckets: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # A burst equal to a minute's worth by default: a rig coming back
        # from an outage empties its outbox in one go and is then held to
        # the average, which is exactly the shape of the real traffic.
        if self.burst <= 0:
            self.burst = max(self.per_minute, 1)

    @property
    def enabled(self) -> bool:
        return self.per_minute > 0

    def allow(self, key: str) -> float:
        """0.0 if the call may proceed, else seconds until it may.

        Returning the wait rather than a bool so the reply can carry a
        Retry-After. A 429 with no idea when to come back invites exactly
        the tight retry loop the limit exists to stop.
        """
        if not self.enabled:
            return 0.0

        now = self.clock()
        rate = self.per_minute / 60.0
        bucket = self._buckets.get(key)

        if bucket is None:
            if len(self._buckets) >= MAX_TRACKED:
                # Full. Rather than evicting somebody else's bucket - which
                # is how a limiter gets bypassed by churning keys - refuse
                # and say so. This is only reachable without auth on.
                return 60.0
            bucket = _Bucket(tokens=float(self.burst), last=now)
            self._buckets[key] = bucket

        # Refill for the time that has passed, never above the burst.
        elapsed = max(0.0, now - bucket.last)
        bucket.last = now
        bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * rate)

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0

        # How long until one token exists again.
        return (1.0 - bucket.tokens) / rate

    def forget(self, key: str) -> None:
        self._buckets.pop(key, None)

    def tracked(self) -> int:
        return len(self._buckets)
