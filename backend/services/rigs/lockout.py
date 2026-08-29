"""Slowing down somebody working through a password list.

The rate limiter next door caps how fast one address may call the login
route: ten a minute, on by default. That is enough against a strong
password and not enough against a weak one. Ten a minute is six hundred
an hour and fourteen thousand a day, all of it under the limit, and a
list of common passwords is ten thousand entries long. The limiter would
refuse none of it.

So this counts *consecutive failures* and makes the next attempt wait.
Five wrong guesses, then one minute, then two, four, eight, capped at
fifteen. A dictionary attack goes from taking a day to taking years, and
that is the whole of what this buys. It is aimed at exactly one thing:
the operator who chose a password somebody could guess.

**It counts per (account, address) pair, and that is the important
decision.** The textbook version locks the account, which means anyone
who knows a manager's email address can lock them out with five wrong
guesses - and on this floor that is a better attack than guessing the
password. A shift with no pushed schedule leaves twelve rigs in Standby
refusing to work, so denying a manager the desk for fifteen minutes at a
crew change does more damage than reading their board would. Per-pair,
an attacker locks out only themselves; the manager standing at the desk
is on another address and never notices.

**It keys on the email as typed, whether or not that account exists.**
If unknown addresses never locked and real ones did, the difference
would say which addresses are real - the same enumeration leak the dummy
hash in `people.py` exists to close, reopened from another side.

**It always clears itself.** A correct password clears it immediately,
and every cool-off expires on its own. Nothing here needs an
administrator to undo, because at three in the morning on a floor there
is not one.

What it does not stop: the same list tried from many addresses. Per-pair
counting cannot see that, and pretending otherwise would be worse than
saying so. In-process, like the limiter, with the same consequence - N
instances allow N times the attempts, and a restart forgets.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# A pair with no entry is not tracked until it fails, so this bounds what
# an unauthenticated caller can make us allocate by inventing addresses
# and addresses' worth of email.
MAX_TRACKED = 8192


@dataclass
class _Strike:
    fails: int
    until: float          # monotonic time this pair may try again
    seen: float           # last touched, for eviction


@dataclass
class Lockout:
    """`after` consecutive failures, then a doubling wait up to `max_wait`.

    `after = 0` disables it entirely and `check()` always says yes, so
    callers do not have to ask twice.
    """

    after: int = 5
    first_wait: float = 60.0
    max_wait: float = 900.0
    clock: object = time.monotonic
    _strikes: dict = field(default_factory=dict, repr=False)

    @property
    def enabled(self) -> bool:
        return self.after > 0

    def check(self, key) -> float:
        """0.0 if this pair may try, else seconds until it may."""
        if not self.enabled:
            return 0.0
        strike = self._strikes.get(key)
        if strike is None:
            return 0.0
        left = strike.until - self.clock()
        return left if left > 0 else 0.0

    def failed(self, key) -> float:
        """Record a wrong password. Returns the wait now owed, if any."""
        if not self.enabled:
            return 0.0

        now = self.clock()
        strike = self._strikes.get(key)
        if strike is None:
            self._evict(now)
            if len(self._strikes) >= MAX_TRACKED:
                # Full. Refusing to track is safer than evicting somebody
                # else's strike, which is how a limiter is bypassed by
                # churning keys - and the cost of not tracking is only
                # that the rate limiter is doing the work alone.
                return 0.0
            strike = _Strike(fails=0, until=0.0, seen=now)
            self._strikes[key] = strike

        strike.fails += 1
        strike.seen = now

        over = strike.fails - self.after
        if over < 0:
            return 0.0

        # 1st over -> first_wait, then doubling, capped.
        wait = min(self.first_wait * (2 ** over), self.max_wait)
        strike.until = now + wait
        return wait

    def succeeded(self, key) -> None:
        """A right password ends it, immediately and completely."""
        self._strikes.pop(key, None)

    def forget(self, key) -> None:
        self._strikes.pop(key, None)

    def tracked(self) -> int:
        return len(self._strikes)

    def _evict(self, now: float) -> None:
        """Drop pairs whose cool-off is long over.

        Only walked when a new pair arrives, so the cost lands on the
        caller creating the entries rather than on every sign-in.
        """
        if len(self._strikes) < MAX_TRACKED:
            return
        stale = now - (self.max_wait * 2)
        for key in [k for k, s in self._strikes.items()
                    if s.until < now and s.seen < stale]:
            del self._strikes[key]
