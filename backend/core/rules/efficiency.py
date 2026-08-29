"""Efficiency, in one place, computed at read time.

This is the Python half of a number that also exists in
`apps/rig/assets/rig.js`, where the operator sees it on the wall:

    const chargeable = assignedSecs() - S.faultSecs - S.downSecs;
    if (chargeable <= 0) return 0;
    return Math.max(0, Math.min(1, S.recordedSecs / chargeable));

The rig shows it; the database stores the four numbers it is made of and
never the ratio. That is the whole reason `stint_ended` carries
measurements: a stored percentage cannot be corrected without re-running
the floor, and this function can be corrected in an afternoon.

What lands in the denominator is a deliberate choice, not an accident.
Reset, review and idle are all the operator's time and stay in. Fault and
downtime come out - a loose camera mount is not the operator's
productivity problem.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Stint:
    """The four seconds columns as the block holds them.

    Not "as the rig reports them" any more: `recorded_secs` and
    `assigned_secs` are derived from the ledger by the projection,
    because the rig's own counters reset to zero on any restart. See
    `_stint_totals` in core/workflows/projection.py.
    """

    recorded_secs: float
    assigned_secs: float
    fault_secs: float
    down_secs: float


def chargeable_secs(s: Stint) -> float:
    """The time the operator is answerable for."""
    return s.assigned_secs - s.fault_secs - s.down_secs


def efficiency(s: Stint) -> float:
    """Data time over chargeable time, clamped to 0..1.

    Zero when there is no chargeable time at all - a stint that was
    entirely fault and downtime is not a 100% stint, and it is not a
    divide-by-zero either.
    """
    chargeable = chargeable_secs(s)
    if chargeable <= 0:
        return 0.0
    return max(0.0, min(1.0, s.recorded_secs / chargeable))


def unaccounted_secs(s: Stint) -> float:
    """Seconds that reached no bucket at all.

    Invariant I2 says every second of a stint lands in exactly one bucket
    - recorded, reset-and-idle, fault, or downtime. Reset and idle have no
    column of their own; they are the remainder. This makes that remainder
    visible so a negative one, which would mean the buckets overlap and a
    second was counted twice, can be caught rather than quietly averaged
    into a ratio.
    """
    return chargeable_secs(s) - s.recorded_secs
