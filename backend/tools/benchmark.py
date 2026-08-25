"""What this service costs at floor scale, measured rather than assumed.

Every sizing number in this repo has been an assumption. This replaces
the ones that matter.

Start from the floor itself. Twelve rigs, three eight-hour shifts,
45-minute turns, an episode roughly every two minutes:

    ~272 events per rig per shift
    ~9,800 events per day across twelve rigs
    ~0.11 events per second, sustained
    ~3.6 million ledger rows after a year

So throughput is not the risk and it would be dishonest to present a
big ingest number as if it were reassuring. The risk is the other end:
two queries run continuously against a table that only ever grows.

    the floor sweep       every 15 seconds, for ever
    /api/floor/state      polled by the desk while anyone is watching

Those are what this measures, at a ledger size you choose, so "it is
fine" becomes a number with a date on it.

    python -m tools.benchmark                # 30 days of floor
    python -m tools.benchmark --days 365     # a year
    python -m tools.benchmark --days 730     # two years, for headroom

Runs against TEST_DATABASE_URL and rebuilds the schema first, so it
never touches the dev database. Not part of `pytest` - it is slow on
purpose, because the point is a big table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select                                  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from core.base.model import Base                                     # noqa: E402
from core.domains.alerts.model import Alert                          # noqa: E402,F401
from core.domains.episode_videos.model import EpisodeVideo           # noqa: E402,F401
from core.domains.episodes.model import Episode                      # noqa: E402
from core.domains.rig_downtime_events.model import RigDowntimeEvent  # noqa: E402,F401
from core.domains.rig_events.model import RigEvent                   # noqa: E402
from core.domains.rig_productivity_blocks.model import RigProductivityBlock  # noqa: E402,F401
from core.domains.rig_shift_checks.model import RigShiftCheck        # noqa: E402,F401
from core.domains.rig_status.repository import RigStatusRepository   # noqa: E402
from core.domains.schedules.model import Schedule                    # noqa: E402
from core.domains.sessions.model import Session                      # noqa: E402,F401
from core.infrastructure import database                             # noqa: E402
from core.infrastructure.config import get_settings                  # noqa: E402
from core.workflows.floor import (                                   # noqa: E402
    _last_event_at, _overruns, _projection_lag, _repeat_faults,
    floor_state, sweep,
)
from core.workflows.projection import project_batch                  # noqa: E402

RIGS = [f"RIG-{i:02d}" for i in range(1, 13)]
SHIFTS = [("Morning", 8), ("Day", 16), ("Night", 0)]

# The floor, as CLAUDE.md defines it.
TURN_MINUTES = 45
EPISODE_SECONDS = 120          # a take plus the reset after it
REAL_RATE = 9792 / 86400       # events per second, sustained, twelve rigs


def a_turn(rig: str, day: datetime, shift: str, turn_from: str, op: str,
           seq: int) -> list[dict]:
    """One operator's turn, as the rig files it."""
    out = []
    t = day

    def stamp(offset):
        return (t + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")

    def ev(event, bucket, data, offset):
        nonlocal seq
        e = {
            "eventId": str(uuid.uuid4()), "seq": seq, "at": stamp(offset),
            "rigId": rig, "shiftDate": day.date().isoformat(), "shiftLabel": shift,
            "turnFrom": turn_from, "operatorId": op,
            "bucket": bucket, "event": event, "data": data,
        }
        seq += 1
        return e

    out.append(ev("shift_check", "rig_shift_checks", {"outcome": "passed"}, 0))
    n = TURN_MINUTES * 60 // EPISODE_SECONDS
    for i in range(n):
        out.append(ev("episode_saved", "episodes", {
            "episodeId": str(uuid.uuid4()),
            "durationSecs": 92, "score": 4 + (i % 2),
        }, 30 + i * EPISODE_SECONDS))
    out.append(ev("stint_ended", "rig_productivity_blocks", {
        "episodes": n, "recordedSecs": n * 92, "assignedSecs": TURN_MINUTES * 60,
        "faultSecs": 0, "downSecs": 0,
    }, TURN_MINUTES * 60 - 2))
    out.append(ev("session_ended", "sessions",
                  {"endedBy": "handover", "recordedSecs": n * 92},
                  TURN_MINUTES * 60 - 1))
    return out


def a_day(start: datetime, seq_by_rig: dict[str, int]) -> list[dict]:
    events = []
    for shift, hour in SHIFTS:
        base = start.replace(hour=hour, minute=0, second=0, microsecond=0)
        turns = (8 * 60) // TURN_MINUTES
        for rig_i, rig in enumerate(RIGS):
            for turn_i in range(turns):
                at = base + timedelta(minutes=TURN_MINUTES * turn_i)
                op = "op-%s%d" % ("abcd"[rig_i // 3], (turn_i % 4) + 1)
                events.extend(a_turn(rig, at, shift, at.strftime("%H:%M"), op,
                                     seq_by_rig[rig]))
                seq_by_rig[rig] += TURN_MINUTES * 60 // EPISODE_SECONDS + 3
    return events


def payload_for(rig: str, day) -> dict:
    """A schedule shaped like the desk's, so turn_in_progress has work to do."""
    turns = []
    for i in range((8 * 60) // TURN_MINUTES):
        start = 8 * 60 + i * TURN_MINUTES
        turns.append({
            "from": "%02d:%02d" % divmod(start, 60),
            "to": "%02d:%02d" % divmod(start + TURN_MINUTES, 60),
            "minutes": TURN_MINUTES,
            "operator": {"id": "op-a%d" % ((i % 4) + 1), "name": "Operator %d" % i},
            "relievedBy": "Someone Else", "theyGoTo": "Break",
        })
    return {
        "rigId": rig, "group": "A", "task": "Box transfer",
        "shift": {"label": "Morning", "date": day.isoformat(),
                  "start": "08:00", "end": "16:00", "tz": "UTC"},
        "blockMinutes": 15, "rotation": "hold", "autoSignIn": True, "turns": turns,
    }


async def timed(label, coro):
    t = time.perf_counter()
    result = await coro
    return (time.perf_counter() - t), result


async def main(days: int, batch: int) -> None:
    settings = get_settings()
    url = settings.test_database_url
    if not url:
        print("TEST_DATABASE_URL is not set in backend/.env")
        return

    print(f"rebuilding the schema in {settings.safe_url(url)}")
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    database.configure(url)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

    # ---- the schedules the sweep and the board read
    async with maker() as s:
        push = uuid.uuid4()
        for rig in RIGS:
            s.add(Schedule(push_id=push, pushed_at=now, rig_id=rig,
                           shift_date=now.date(), shift_label="Morning",
                           payload=payload_for(rig, now.date())))
            await RigStatusRepository(s).beat(rig, now, now, 0.2)
        await s.commit()

    # ---- seed the ledger
    print(f"seeding {days} days of floor across {len(RIGS)} rigs...")
    seq_by_rig = {r: 0 for r in RIGS}
    total = 0
    seed_start = time.perf_counter()
    for d in range(days):
        events = a_day(start + timedelta(days=d), seq_by_rig)
        rows = [{
            "rig_id": e["rigId"], "event_id": uuid.UUID(e["eventId"]), "seq": e["seq"],
            "at": datetime.fromisoformat(e["at"].replace("Z", "+00:00")),
            "shift_date": datetime.fromisoformat(e["at"].replace("Z", "+00:00")).date(),
            "shift_label": e["shiftLabel"], "turn_from": e["turnFrom"],
            "operator_id": e["operatorId"], "bucket": e["bucket"], "event": e["event"],
            "envelope": e, "projected_at": None,
        } for e in events]
        async with maker() as s:
            for i in range(0, len(rows), 5000):
                await s.execute(RigEvent.__table__.insert(), rows[i:i + 5000])
            await s.commit()
        total += len(rows)
        if (d + 1) % 30 == 0 or d == days - 1:
            print(f"  day {d + 1}/{days}: {total:,} events")
    seed_secs = time.perf_counter() - seed_start
    print(f"  seeded {total:,} events in {seed_secs:.1f}s "
          f"({total / seed_secs:,.0f}/s bulk insert)")

    # ---- projection
    print("projecting...")
    projected = 0
    proj_start = time.perf_counter()
    while True:
        async with maker() as s:
            n, _ = await project_batch(s, limit=batch)
        projected += n
        if n == 0:
            break
        if projected % 100_000 < batch:
            print(f"  {projected:,} projected")
    proj_secs = time.perf_counter() - proj_start
    print(f"  projected {projected:,} in {proj_secs:.1f}s "
          f"({projected / max(proj_secs, 1e-9):,.0f}/s)")

    # ---- the queries that run for ever
    async with maker() as s:
        sizes = {}
        for name, model in (("rig_events", RigEvent), ("episodes", Episode),
                            ("blocks", RigProductivityBlock),
                            ("shift_checks", RigShiftCheck)):
            sizes[name] = (await s.execute(
                select(func.count()).select_from(model))).scalar()

        # Which part of the sweep costs what. A total is not actionable;
        # this says where to put an index.
        parts = {}
        parts["projection lag"], _ = await timed("", _projection_lag(s))
        parts["repeat faults"], _ = await timed("", _repeat_faults(s, 7))
        parts["overruns"], _ = await timed("", _overruns(s, 24))
        t = time.perf_counter()
        for rig in RIGS:
            await _last_event_at(s, rig)
        parts["last event, x12"] = time.perf_counter() - t

        sweep_ms, sweep_result = await timed("sweep", sweep(s, now=now))
        sweep2_ms, _ = await timed("sweep", sweep(s, now=now))
        board_ms, board = await timed("board", floor_state(s, now=now))
        board2_ms, _ = await timed("board", floor_state(s, now=now))

    print()
    print("=" * 62)
    print(f"  ledger rows            {sizes['rig_events']:>12,}")
    print(f"  episodes               {sizes['episodes']:>12,}")
    print(f"  productivity blocks    {sizes['blocks']:>12,}")
    print(f"  shift checks           {sizes['shift_checks']:>12,}")
    print("-" * 62)
    print(f"  bulk insert            {total / seed_secs:>9,.0f} events/s")
    print(f"  projection             {projected / max(proj_secs, 1e-9):>9,.0f} events/s")
    print(f"  the real floor needs   {REAL_RATE:>12.3f} events/s")
    print(f"  headroom               {(projected / max(proj_secs, 1e-9)) / REAL_RATE:>11,.0f}x")
    print("-" * 62)
    print(f"  floor sweep            {sweep_ms * 1000:>9.1f} ms   (runs every 15s)")
    print(f"    again, warm          {sweep2_ms * 1000:>9.1f} ms")
    for name, secs in sorted(parts.items(), key=lambda kv: -kv[1]):
        print(f"    {name:<20} {secs * 1000:>9.1f} ms")
    print(f"  /api/floor/state       {board_ms * 1000:>9.1f} ms   (desk polls it)")
    print(f"    again, warm          {board2_ms * 1000:>9.1f} ms")
    print("=" * 62)
    print(f"  sweep uses {sweep2_ms / 15 * 100:.2f}% of its 15-second budget")
    print(f"  {len(board['rigs'])} rigs on the board, "
          f"{sum(len(r['alerts']) for r in board['rigs'])} alerts open, "
          f"backend lag {board['backend']['projectionLagSecs']}s")
    print(json.dumps(sweep_result))

    await engine.dispose()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--batch", type=int, default=5000)
    args = p.parse_args()
    asyncio.run(main(args.days, args.batch))
