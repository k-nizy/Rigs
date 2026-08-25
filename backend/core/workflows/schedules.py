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
        return recent[0]

    # Nothing near today at all. Fall back to whatever this rig was last
    # pushed, so a demo or a long-idle floor still has something to show.
    rows = await session.execute(
        select(Schedule)
        .where(Schedule.rig_id == rig_id)
        .order_by(desc(Schedule.pushed_at))
        .limit(1)
    )
    return rows.scalar_one_or_none()
