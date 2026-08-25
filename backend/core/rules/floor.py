"""When something on the floor is worth telling somebody about.

Pure functions over plain data. No database, no clock of their own - the
caller passes `now` - so every decision here can be tested by writing
down the situation rather than by arranging rows.

The two that matter most are absences. A rig sitting on handover because
nobody arrived emits nothing; a rig whose machine lost power emits
nothing. An event bus is structurally blind to both - there is no message
to subscribe to - which is why these are swept for on a timer instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo as TzInfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Alert kinds. Stable strings: they are stored, and the desk renders them.
RIG_SILENT = "rig_silent"
RIG_IDLE = "rig_idle"
RIG_DOWN = "rig_down"
REPEAT_FAULT = "repeat_fault"
TURN_OVERRAN = "turn_overran"
CLOCK_ADRIFT = "clock_adrift"
PROJECTION_BEHIND = "projection_behind"

# Not a rig. An alert about the service itself has to go somewhere, and
# the column is not nullable; this reads as what it is on a board.
FLOOR = "FLOOR"


@dataclass(frozen=True)
class Alert:
    """One thing worth a manager's attention.

    `key` is what makes sweeping idempotent: the same situation swept
    twice produces the same key, so an alert is opened once and left
    alone until it clears.
    """

    kind: str
    rig_id: str
    key: str
    detail: str


# ------------------------------------------------------------- absence

def rig_silent(rig_id: str, last_seen: datetime | None, now: datetime,
               after_secs: int) -> Alert | None:
    """A rig that has not been heard from.

    Deliberately driven by heartbeats and not by the event ledger. A rig
    can be alive and correct and emit no events for a long time - it is on
    standby, or the operator is mid-take - so silence in the ledger is not
    evidence of anything. Silence in the heartbeat is.
    """
    if last_seen is None:
        # Never heard from at all. Real on a floor being provisioned, and
        # worth saying so rather than leaving the rig absent from the board.
        return Alert(RIG_SILENT, rig_id, f"{RIG_SILENT}:{rig_id}", "never heard from")

    gap = (now - last_seen).total_seconds()
    if gap <= after_secs:
        return None
    return Alert(
        RIG_SILENT, rig_id, f"{RIG_SILENT}:{rig_id}",
        f"no heartbeat for {int(gap)}s",
    )


def rig_idle(rig_id: str, turn_from: str | None, turn_started: datetime | None,
             last_event_at: datetime | None, now: datetime,
             grace_secs: int) -> Alert | None:
    """A turn is running and nothing is happening at the rig.

    The schedule says somebody is due. If no event has arrived since the
    turn began - not a check, not an episode, nothing - past a grace
    window, then either nobody came or nobody is working. Both are worth
    a manager knowing, and neither produces an event of its own.

    The grace window is what stops this firing during an ordinary
    handover, where an operator is walking to the rig.
    """
    if turn_from is None or turn_started is None:
        return None  # nothing scheduled: standby is not idleness

    since = turn_started if last_event_at is None or last_event_at < turn_started else last_event_at
    quiet = (now - since).total_seconds()
    if quiet <= grace_secs:
        return None

    # Keyed on the turn, so one alert per turn rather than one per sweep.
    return Alert(
        RIG_IDLE, rig_id, f"{RIG_IDLE}:{rig_id}:{turn_from}",
        f"nothing for {int(quiet)}s into the {turn_from} turn",
    )


def clock_adrift(rig_id: str, skew_secs: float | None,
                 tolerance_secs: float) -> Alert | None:
    """A rig whose clock disagrees with the server.

    The reason this is measured at all is written on `TimestampedBase`:
    twelve rigs disagreeing about the time makes the whole event stream
    unsortable. It was measured on every heartbeat and then never looked
    at, which is the same as not measuring it.

    It costs more than tidiness. `rig_idle` compares the newest event's
    timestamp - the rig's own - against a turn boundary computed here. A
    rig running ten minutes slow looks like a rig where nothing has
    happened for ten minutes, so a skewed clock manufactures exactly the
    alert this system exists to make trustworthy.

    The measurement includes one-way network latency, so it is a ceiling
    on the true skew rather than the skew itself. That is fine for an
    alert: a rig that looks ten minutes out is ten minutes out.
    """
    if skew_secs is None:
        return None
    off = abs(skew_secs)
    if off <= tolerance_secs:
        return None
    # skew = server_now - rig_reported. Positive means the rig is behind.
    direction = "behind" if skew_secs > 0 else "ahead of"
    return Alert(
        CLOCK_ADRIFT, rig_id, f"{CLOCK_ADRIFT}:{rig_id}",
        f"clock is {int(off)}s {direction} the server",
    )


def projection_behind(oldest_unprojected_at: datetime | None, waiting: int,
                      now: datetime, after_secs: float) -> Alert | None:
    """The backend has stopped turning events into facts.

    The whole design here is about noticing absences that nothing
    publishes - a rig nobody came to, a rig that lost power. A projection
    worker that has died is the same shape of failure pointed at
    ourselves: it emits nothing, the ledger keeps accepting events, and
    every board in the building goes on answering cheerfully with
    yesterday's episodes. Nobody finds out by being told.

    Measured on `received_at`, the server's own clock, deliberately. Using
    the rig's `at` would let one rig with a wrong clock either invent this
    alert or hide it, and this is the alert that says whether the other
    ones can be believed.
    """
    if oldest_unprojected_at is None or waiting == 0:
        return None
    lag = (now - oldest_unprojected_at).total_seconds()
    if lag <= after_secs:
        return None      # ordinary queue depth, not a stall
    return Alert(
        PROJECTION_BEHIND, FLOOR, f"{PROJECTION_BEHIND}",
        f"{waiting} events have been waiting up to {int(lag)}s to become facts - "
        f"the floor board is stale",
    )


# ---------------------------------------------------------- from events

def rig_down(rig_id: str, issue: str, needs_manager: bool,
             down_at: datetime, now: datetime) -> Alert:
    """An outage that has not been closed.

    This one does have an event behind it, but the alert tracks the
    *open* state rather than the moment - a rig that went down an hour ago
    and is still down is the thing a manager needs on the board.
    """
    mins = int((now - down_at).total_seconds() // 60)
    manager = ", manager needed" if needs_manager else ""
    return Alert(
        RIG_DOWN, rig_id, f"{RIG_DOWN}:{rig_id}:{down_at.isoformat()}",
        f"{issue} - down {mins} min{manager}",
    )


def repeat_fault(rig_id: str, subsystem: str, shift_count: int,
                 threshold: int) -> Alert | None:
    """The same subsystem failing on the same rig, shift after shift.

    A single fault is noise; the same one three times is a rig that needs
    fixing properly rather than checking again. This is the reason faults
    are logged at all.
    """
    if shift_count < threshold:
        return None
    return Alert(
        REPEAT_FAULT, rig_id, f"{REPEAT_FAULT}:{rig_id}:{subsystem}",
        f"{subsystem} has failed on {shift_count} shifts",
    )


def turn_overran(rig_id: str, turn_from: str, turn_to: str,
                 ended_at: datetime, boundary: datetime) -> Alert | None:
    """A stint that ran past its boundary.

    Not a fault. The rig deliberately lets an episode finish rather than
    cutting a take at the turn boundary, so an overrun is the system
    working. It is still time out of somebody's break, and nothing else
    records that it was.
    """
    over = (ended_at - boundary).total_seconds()
    if over <= 0:
        return None
    return Alert(
        TURN_OVERRAN, rig_id, f"{TURN_OVERRAN}:{rig_id}:{turn_from}",
        f"the {turn_from}-{turn_to} turn ran {int(over)}s past its boundary",
    )


# ------------------------------------------------------------- helpers

def payload_zone(payload: dict, fallback: TzInfo) -> TzInfo:
    """The floor's own zone, as the desk wrote it into the payload.

    Every "HH:MM" in a payload is wall-clock time *on the floor*. Reading
    them in the server's zone is how this system spent a day believing no
    turn was in progress on any of twelve rigs: the desk meant 00:15 local
    and a UTC server heard 00:15 UTC, which on a floor at UTC+3 is three
    hours into the previous shift.

    So the zone travels with the schedule and is read here, never assumed.
    `fallback` covers schedules pushed before the field existed; a payload
    that carries one is always believed over it.
    """
    name = (payload.get("shift") or {}).get("tz")
    if not name:
        return fallback
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # An unknown zone is the desk and this server disagreeing about
        # the tz database. Falling back keeps the floor readable; being
        # wrong quietly in the other direction is what this fixed.
        return fallback


def turn_bounds(shift_date, turn: dict, tz: TzInfo) -> tuple[datetime, datetime]:
    """Absolute start and end of a turn, read from the pushed payload.

    Read, never derived. The rotation is computed in exactly one shared
    JavaScript file, and this is not it; these are the `from` and `to` the
    desk already wrote into the payload.

    `tz` must be the floor's zone - see `payload_zone`. It is a parameter
    rather than a lookup so this stays a pure function of its arguments.

    A turn whose `to` is not after its `from` has crossed midnight, which
    the Night shift does every day.
    """
    def at(hhmm: str) -> datetime:
        h, m = hhmm.split(":")
        return datetime(shift_date.year, shift_date.month, shift_date.day,
                        int(h), int(m), tzinfo=tz)

    start, end = at(turn["from"]), at(turn["to"])
    if end <= start:
        end += timedelta(days=1)
    return start, end


def turn_in_progress(payload: dict, shift_date, now: datetime,
                     tz: TzInfo) -> dict | None:
    """Which turn the schedule says is running, or None outside the shift.

    `tz` is only the fallback: the payload's own zone wins when it has one.
    `now` may be in any zone - both sides of the comparison are aware.
    """
    zone = payload_zone(payload, tz)
    for turn in payload.get("turns", []):
        start, end = turn_bounds(shift_date, turn, zone)
        if start <= now < end:
            return {"turn": turn, "start": start, "end": end}
    return None
