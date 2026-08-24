"""Downtime opens and closes, and can end in more than one way.

Two rules worth stating, because both are ways real floors behave rather
than edge cases invented here:

  A rig_up with no open downtime is ignored, not an error. Replay starts
  from an arbitrary point in the ledger, and the matching rig_down may
  simply not be in the range being projected.

  A second rig_down while one is already open does not open another. The
  rig cannot be doubly down; the operator pressing through the issue tree
  twice is one outage.
"""

from datetime import datetime

from core.domains.rig_downtime_events.model import RigDowntimeEvent


def is_open(row: RigDowntimeEvent) -> bool:
    return row.up_at is None


def close(row: RigDowntimeEvent, up_at: datetime, down_secs: float | None) -> RigDowntimeEvent:
    """Close an open outage.

    `down_secs` is what the rig measured, and it is preferred over the
    difference between the two timestamps: the rig counted frame by frame
    against a monotonic clock, while subtracting two wall-clock readings
    would fold any NTP correction into the answer.
    """
    row.up_at = up_at
    row.down_secs = down_secs if down_secs is not None else (up_at - row.down_at).total_seconds()
    return row
