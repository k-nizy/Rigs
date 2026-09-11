"""Which schedule a rig should be running right now.

A rig used to be handed whichever payload was pushed most recently, and
that is why a rig ran one shift and then went quiet: a payload covers a
single shift, so at 16:00 the Morning one it was still holding ran out,
`whoIsOn()` returned nothing, and the rig sat on Standby until somebody
reloaded twelve browsers.

The fix is not to make the server clever. It reads the window the desk
already wrote into the payload - `date`, `start`, `end` and `tz` - and
picks the one containing this instant. **A comparison, not a
calculation.** That distinction is load-bearing: the rotation is computed
in exactly one shared JavaScript file that both apps load, and the whole
system rests on the rig never being able to compute a different answer
from the desk that scheduled it. A Python re-implementation here would be
a third answer, and the first one that could silently disagree.

So this never decides who is on a rig. It only decides which of the
things the desk already said is the one that applies now.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domains.schedules.model import Schedule
from core.rules import floor as rules


async def in_force(session: AsyncSession, rig_id: str,
                   now: datetime | None = None) -> Schedule | None:
    """The schedule covering `now`, or the most recent one if none does.

    The fallback matters as much as the selection. A rig booting before
    its first shift starts, or after the last one has ended, still needs
    something to put on screen - and what it shows then is Standby, which
    is the truthful answer. Returning nothing would make the rig fall
    through to generating its own schedule, which is precisely the drift
    this system exists to prevent.
    """
    now = now or datetime.now(timezone.utc)

    # Yesterday, today and tomorrow. A shift can begin on one date and
    # end on the next - the Day shift runs 16:00 to 00:00 - and a floor
    # in a zone far from the server's can be a day either side. Bounded
    # rather than unbounded, because this table only grows.
    today = now.astimezone(timezone.utc).date()
    rows = await session.execute(
        select(Schedule)
        .where(
            Schedule.rig_id == rig_id,
            Schedule.shift_date >= today - timedelta(days=1),
            Schedule.shift_date <= today + timedelta(days=1),
        )
        .order_by(desc(Schedule.pushed_at))
    )
    recent = list(rows.scalars().all())

    # Newest push first, so a re-push of the same shift wins over the
    # version it corrected.
    for sched in recent:
        if rules.shift_covers(sched.payload, sched.shift_date, now, now.tzinfo):
            return sched

    if recent:
        return _nearest(recent, now)

    # Nothing near today at all. Fall back to whatever this rig was last
    # pushed, so a demo or a long-idle floor still has something to show.
    rows = await session.execute(
        select(Schedule)
        .where(Schedule.rig_id == rig_id)
        .order_by(desc(Schedule.pushed_at))
        .limit(1)
    )
    return rows.scalar_one_or_none()


def _nearest(rows: list[Schedule], now: datetime) -> Schedule:
    """Which schedule to hand back when none of them is actually running.

    Every rig on a floor is pushed together and must land on the SAME
    answer here. Otherwise one rig sits in Standby against the Day sheet
    while the rig beside it sits in Standby against the Night one, out of
    a single push, and the floor looks like it is running three different
    schedules at once.

    Returning "whichever row came back first" did exactly that. All the
    rows of one push share a `pushed_at` to the microsecond, so ordering
    by it leaves them all tied, and a tied ORDER BY lets the database
    return them in any order it likes - a different order per query, and
    so a different shift per rig, from identical data.

    The choice is therefore made from the schedules themselves: the next
    shift due to start, or if they have all finished, the one that
    finished most recently. Both are properties the desk wrote into the
    payload, identical on all twelve rigs, and the same rule the push
    server applies.
    """
    dated = []
    for sched in rows:
        window = rules.shift_window(sched.payload, sched.shift_date, now.tzinfo)
        if window is not None:
            dated.append((window, sched))

    # A tie-break that does not depend on row order, only on the payload.
    key = lambda s: (s.shift_date, s.shift_label)

    if not dated:
        return sorted(rows, key=key)[0]

    upcoming = [(w[0], s) for w, s in dated if w[0] > now]
    if upcoming:
        return min(upcoming, key=lambda pair: (pair[0], key(pair[1])))[1]

    return max(dated, key=lambda pair: (pair[0][1], key(pair[1])))[1]


async def turns_for_operator(
    session: AsyncSession, person_id: str, now: datetime | None = None
) -> dict:
    """One person's turns in their shift, across every rig.

    Their shift is the one that names them and covers now; failing that,
    the one naming them that starts next; failing that, the one that
    named them and ended last. That is `in_force`'s rule asked for a
    person instead of a rig, and `_nearest` is reused rather than
    rewritten. My Shift counts down to the first turn before a shift and
    says "finished" after the last, and neither state was reachable
    while this served only the shift in force: at 07:30 an operator was
    told nothing was scheduled, and at 16:05 the same. Bounded a day
    either side of today, as the rig's is - a person whose last shift
    was a week ago has nothing scheduled, and a week-old "finished" with
    a date on it is a screen to misread.

    Matched on the person the push named in each turn, never on the
    seat: the seat is whichever chair the roster put them in today, and
    an account names its person and nothing about where they sit. Which
    is also what makes it *their* shift that is chosen and not the
    floor's - at 17:00 the floor is running Day, and an operator who
    worked Morning is shown Morning.

    Read, not derived. Every field returned here was written into a
    payload by the desk and is handed back unchanged; this only selects -
    which schedules name this person, which of those is theirs at this
    instant, and which of its turns are theirs. The rotation is computed
    in one shared JavaScript file and a second implementation here would
    be a third answer.

    It deliberately does *not* work out the breaks between the turns.
    Each turn already carries `theyGoTo`, so a screen can lay the gaps
    out from two things that were pushed; inferring them here would be
    the server starting to have opinions about a rotation it is only
    supposed to be filing.

    Across every rig, because a rig only knows its own turns and an
    operator's day is not one rig's business - which is exactly why a
    rig cannot answer this and why `theyGoTo` had to travel in the
    payload in the first place.

    And when the day is empty it says why, because three floors produce
    one and a person needs to know which they are looking at. `floor`
    carries two facts - whether anything was pushed, and whether any of
    it names people - and the screen reads back the one that applies:
    nothing pushed (the rig says Standby for the same reason), a push
    that names nobody (a roster of plain names - stated in CLAUDE.md), or
    a push that names people and not this person. The same two facts
    ride on a day that is found, so the shape does not change by case.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()

    # The latest push wins for a given rig, shift and date, the same way
    # `in_force` picks one. Ordering by pushed_at descending and keeping
    # the first of each key means a re-push does not double the day.
    rows = await session.execute(
        select(Schedule)
        .where(
            Schedule.shift_date >= today - timedelta(days=1),
            Schedule.shift_date <= today + timedelta(days=1),
        )
        .order_by(desc(Schedule.pushed_at))
    )
    latest: dict[tuple[str, object, str], Schedule] = {}
    for row in rows.scalars().all():
        latest.setdefault((row.rig_id, row.shift_date, row.shift_label), row)

    floor = {
        "pushed": bool(latest),
        "namesPeople": any(
            (turn.get("operator") or {}).get("personId")
            for row in latest.values()
            for turn in (row.payload or {}).get("turns") or []
        ),
    }

    # Only the sheets that name this person are candidates at all.
    named: list[tuple[Schedule, list[dict]]] = []
    for row in latest.values():
        theirs = [turn for turn in (row.payload or {}).get("turns") or []
                  if (turn.get("operator") or {}).get("personId") == person_id]
        if theirs:
            named.append((row, theirs))
    if not named:
        return {"personId": person_id, "shift": None, "turns": [], "floor": floor}

    covering = [row for row, _ in named
                if rules.shift_covers(row.payload or {}, row.shift_date, now, timezone.utc)]
    chosen = (min(covering, key=lambda r: (r.shift_date, r.shift_label))
              if covering else _nearest([row for row, _ in named], now))
    key = (chosen.shift_date, chosen.shift_label)

    turns: list[dict] = []
    shift: dict | None = None
    for row, theirs in named:
        if (row.shift_date, row.shift_label) != key:
            continue
        payload = row.payload or {}
        # The rig, and what the rig is for. Both are facts about this
        # payload and both ride on every turn taken from it, so a screen
        # says the task of the rig you are on and changes it when you
        # move - the day a push gives two rigs two tasks, it is right.
        turns.extend({**turn, "rigId": payload.get("rigId"),
                      "task": payload.get("task")} for turn in theirs)
        if shift is None:
            shift = {
                **(payload.get("shift") or {}),
                "group": payload.get("group"),
                "task": payload.get("task"),
            }

    turns.sort(key=lambda t: (t.get("from") or "", t.get("rigId") or ""))
    return {"personId": person_id, "shift": shift, "turns": turns, "floor": floor}
