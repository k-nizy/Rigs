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

OPERATOR = "operator"   # the person at the rig said it was fixed
RESUMED = "resumed"     # nobody said, but the rig went back to work


def is_open(row: RigDowntimeEvent) -> bool:
    return row.up_at is None


def close(row: RigDowntimeEvent, up_at: datetime, down_secs: float | None,
          ended_by: str = OPERATOR) -> RigDowntimeEvent:
    """Close an open outage.

    `down_secs` is what the rig measured, and it is preferred over the
    difference between the two timestamps: the rig counted frame by frame
    against a monotonic clock, while subtracting two wall-clock readings
    would fold any NTP correction into the answer.
    """
    row.up_at = up_at
    row.down_secs = down_secs if down_secs is not None else (up_at - row.down_at).total_seconds()
    row.ended_by = ended_by
    return row


def resume(row: RigDowntimeEvent, working_at: datetime) -> RigDowntimeEvent:
    """Close an outage nobody ever closed, on the evidence that the rig
    is plainly working again.

    This exists because of a real hole. Only `rig_up` closed an outage,
    and there are ordinary ways for it never to arrive: the operator ends
    their session with the rig still down, a technician fixes it, and the
    rig comes back with no memory of the outage. Nothing then closes the
    row, so the floor board showed a working rig as down, with the
    duration climbing for ever. That is the alert a manager is most
    likely to act on, and one that never clears teaches people to ignore
    the rest.

    Closed on work, not on a heartbeat. A heartbeat proves the software
    is running; it says nothing about whether the gripper is fixed. A
    passed shift check or a recorded take proves somebody used the rig.

    `down_secs` is a ceiling rather than a measurement, which is what
    `ended_by` is for: it was fixed at some point at or before this.
    """
    return close(row, working_at, None, ended_by=RESUMED)
