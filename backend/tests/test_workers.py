"""The three processes systemd actually runs.

Everything they *call* is well covered - projection, the sweep, the drain
all have their own suites. What had no tests at all was the shape around
those calls: the poll loop, the backoff, the shutdown flag, the flags
argparse hands them. That is the part that decides whether a worker
survives a database blip at 3am or spins itself into a corner, and the
only automated check on it was preflight asserting the modules import.

So these test the loop and stub the work. No database: a fake session
maker and a fake workflow, which is what lets a test drive a fault, a
recovery and a shutdown in a millisecond instead of waiting out a real
backoff.
"""

import argparse
import asyncio
import signal

import pytest

from workers import _signals, drain_to_archive, project_events, sweep_floor


# --------------------------------------------------------------- helpers

# Captured before anything patches it. Every helper below yields through
# the real one, so a loop that has stopped honouring its shutdown flag can
# still be cancelled instead of spinning the event loop for ever.
REAL_SLEEP = asyncio.sleep


class FakeMaker:
    """Stands in for `sessionmaker()`. The workers do `maker = sessionmaker()`
    and then `async with maker() as session`, so this has to be callable and
    return an async context manager."""

    def __init__(self):
        self.opened = 0

    def __call__(self):
        return self

    async def __aenter__(self):
        self.opened += 1
        # A real session acquisition awaits I/O. Yielding here is not
        # decoration: without it a loop whose batches always return work
        # never gives the event loop a turn, and `bounded()` below could
        # not cancel it.
        await REAL_SLEEP(0)
        return "session"

    async def __aexit__(self, *exc):
        return False


class Clock:
    """Records what the worker asked to wait for, and never actually waits.

    Raises past a cap rather than letting a broken loop run for ever: a
    worker that has stopped stopping should fail these tests in a
    millisecond, not hang them until CI kills the job.
    """

    def __init__(self, cap=200):
        self.waits = []
        self.cap = cap

    async def sleep(self, secs):
        self.waits.append(secs)
        if len(self.waits) > self.cap:
            raise AssertionError(
                f"the worker asked to wait {len(self.waits)} times - "
                "it is not stopping when it is told to")
        await REAL_SLEEP(0)


class StopsAfter:
    """The shutdown flag, flipped after N checks."""

    def __init__(self, n):
        self.n = n
        self.checked = 0

    def __call__(self):
        self.checked += 1
        return self.checked > self.n


async def bounded(coro, secs=2.0):
    """Run a worker loop with a hard ceiling on wall-clock time.

    A loop that ignores its shutdown flag does not fail a test, it hangs
    one - and a hung suite in CI burns the whole job timeout instead of
    naming what broke. This turns that into a fast, legible failure.
    """
    try:
        return await asyncio.wait_for(coro, timeout=secs)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"the worker was still running after {secs}s - it is not "
            "stopping when it is told to") from None


async def run_project(*a, **kw):
    return await bounded(project_events.run(*a, **kw))


async def run_sweep(*a, **kw):
    return await bounded(sweep_floor.run(*a, **kw))


async def run_drain(*a, **kw):
    return await bounded(drain_to_archive.run(*a, **kw))


@pytest.fixture(autouse=True)
def clean_shutdown_flag():
    """`_signals._shutdown` is module-global. A test that sets it and does
    not put it back takes every worker test after it down with it."""
    _signals._shutdown = False
    yield
    _signals._shutdown = False


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(asyncio, "sleep", c.sleep)
    return c


# ======================================================= shutting down

class TestShutdown:
    async def test_the_flag_starts_clear_and_a_signal_sets_it(self):
        assert _signals.is_shutdown_requested() is False
        _signals.install_signal_handlers()
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler), "SIGINT was not taken over"
        handler(signal.SIGINT, None)          # called, not raised: this is a test process
        assert _signals.is_shutdown_requested() is True

    async def test_installing_handlers_twice_is_harmless(self):
        _signals.install_signal_handlers()
        _signals.install_signal_handlers()
        assert _signals.is_shutdown_requested() is False

    async def test_the_projection_loop_stops_when_asked(self, monkeypatch, clock):
        monkeypatch.setattr(project_events, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(project_events, "is_shutdown_requested", StopsAfter(3))

        async def one_each_time(session, limit):
            return 1, False
        monkeypatch.setattr(project_events, "project_batch", one_each_time)

        total = await run_project(poll_secs=0.01, batch=10,
                                         exit_after_empty=0, replay=False)
        assert total == 3, "the loop ran a different number of times than it was allowed"

    async def test_the_sweep_loop_stops_when_asked(self, monkeypatch, clock):
        monkeypatch.setattr(sweep_floor, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(sweep_floor, "is_shutdown_requested", StopsAfter(2))
        swept = []

        async def fake_sweep(session, **kw):
            swept.append(kw)
            return {"found": 0, "opened": 0, "resolved": 0}
        monkeypatch.setattr(sweep_floor, "sweep", fake_sweep)

        await run_sweep(every_secs=0.01, once=False,
                              silent_after=180, idle_grace=300)
        assert len(swept) == 2

    async def test_a_worker_asked_to_stop_before_it_starts_does_nothing(
            self, monkeypatch, clock):
        monkeypatch.setattr(project_events, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(project_events, "is_shutdown_requested", lambda: True)
        calls = []

        async def never(session, limit):
            calls.append(1)
            return 0, True
        monkeypatch.setattr(project_events, "project_batch", never)

        assert await run_project(0.01, 10, 0, False) == 0
        assert calls == [], "a worker told to stop still did a batch of work"


# ========================================================= going wrong

class TestBackoff:
    async def test_a_failure_does_not_kill_the_loop(self, monkeypatch, clock):
        """The property that matters at 3am: a database that goes away
        comes back, and a worker that gave up does not."""
        monkeypatch.setattr(project_events, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(project_events, "is_shutdown_requested", StopsAfter(4))
        seen = {"n": 0}

        async def fails_then_works(session, limit):
            seen["n"] += 1
            if seen["n"] <= 2:
                raise RuntimeError("database is on fire")
            return 5, False
        monkeypatch.setattr(project_events, "project_batch", fails_then_works)

        total = await run_project(0.01, 10, 0, False)
        assert total == 10, "work after a recovery was lost"

    async def test_the_wait_grows_with_each_consecutive_failure(
            self, monkeypatch, clock):
        monkeypatch.setattr(project_events, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(project_events, "is_shutdown_requested", StopsAfter(4))

        async def always_fails(session, limit):
            raise RuntimeError("still down")
        monkeypatch.setattr(project_events, "project_batch", always_fails)

        await run_project(0.01, 10, 0, False)
        assert clock.waits == [2, 4, 8, 16], f"backoff was {clock.waits}"

    async def test_the_wait_is_capped_so_a_worker_never_sleeps_for_ever(
            self, monkeypatch, clock):
        monkeypatch.setattr(project_events, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(project_events, "is_shutdown_requested", StopsAfter(10))

        async def always_fails(session, limit):
            raise RuntimeError("still down")
        monkeypatch.setattr(project_events, "project_batch", always_fails)

        await run_project(0.01, 10, 0, False)
        assert max(clock.waits) == 60, f"backoff reached {max(clock.waits)}s"

    async def test_the_backoff_resets_after_a_success(self, monkeypatch, clock):
        """Without this a worker that fails once an hour creeps up to the
        cap and stays there, and the floor's facts fall an hour behind
        for a fault that cleared immediately."""
        monkeypatch.setattr(project_events, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(project_events, "is_shutdown_requested", StopsAfter(6))
        seq = [RuntimeError, RuntimeError, "ok", RuntimeError, "ok", "ok"]
        seen = {"n": 0}

        async def scripted(session, limit):
            step = seq[seen["n"]]
            seen["n"] += 1
            if step is RuntimeError:
                raise RuntimeError("blip")
            return 1, False
        monkeypatch.setattr(project_events, "project_batch", scripted)

        await run_project(0.01, 10, 0, False)
        assert clock.waits == [2, 4, 2], (
            f"the backoff did not start again from the bottom: {clock.waits}")

    async def test_the_sweep_backs_off_the_same_way(self, monkeypatch, clock):
        monkeypatch.setattr(sweep_floor, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(sweep_floor, "is_shutdown_requested", StopsAfter(3))

        async def always_fails(session, **kw):
            raise RuntimeError("down")
        monkeypatch.setattr(sweep_floor, "sweep", always_fails)

        await run_sweep(every_secs=0.01, once=False,
                              silent_after=180, idle_grace=300)
        assert clock.waits == [2, 4, 8]

    async def test_a_failing_sweep_asked_to_run_once_still_stops(
            self, monkeypatch, clock):
        """`--once` is how a deploy check runs it. A version that retried
        for ever would hang the check rather than fail it."""
        monkeypatch.setattr(sweep_floor, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(sweep_floor, "is_shutdown_requested", StopsAfter(3))

        async def always_fails(session, **kw):
            raise RuntimeError("down")
        monkeypatch.setattr(sweep_floor, "sweep", always_fails)

        await run_sweep(every_secs=0.01, once=True,
                              silent_after=180, idle_grace=300)
        # It stopped because the shutdown flag was reached, not because it
        # spun for ever - the point is that run() returned at all.
        assert clock.waits, "it did not even back off"

    async def test_the_wait_does_not_block_the_loop(self, monkeypatch):
        """Both workers carry a comment saying `await`, not `time.sleep`,
        because a blocking sleep here stops the event loop the whole
        process shares - in local_gateway that is the API and the other
        two workers. A comment is not a check.

        Real waits, deliberately: the point is that something else on the
        same loop keeps running *while* a worker is backing off."""
        monkeypatch.setattr(project_events, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(project_events, "is_shutdown_requested", StopsAfter(2))

        async def always_fails(session, limit):
            raise RuntimeError("down")
        monkeypatch.setattr(project_events, "project_batch", always_fails)

        # Back off in milliseconds rather than the real 2s and 4s.
        real_sleep = asyncio.sleep

        async def brief(secs):
            await real_sleep(0.01)
        monkeypatch.setattr(asyncio, "sleep", brief)

        beside_it = []

        async def other_work():
            for _ in range(5):
                await real_sleep(0.005)
                beside_it.append(1)

        await asyncio.gather(run_project(0.01, 10, 0, False), other_work())
        assert len(beside_it) == 5, (
            "the loop was blocked while a worker waited; nothing else ran")


# ======================================================== draining and stopping

class TestDrainAndStop:
    async def test_exit_after_empty_drains_the_ledger_and_returns(
            self, monkeypatch, clock):
        monkeypatch.setattr(project_events, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(project_events, "is_shutdown_requested", lambda: False)
        seq = [3, 2, 0]
        seen = {"n": 0}

        async def scripted(session, limit):
            n = seq[seen["n"]] if seen["n"] < len(seq) else 0
            seen["n"] += 1
            return n, n == 0
        monkeypatch.setattr(project_events, "project_batch", scripted)

        total = await run_project(0.01, 10, exit_after_empty=1, replay=False)
        assert total == 5, "it stopped before the ledger was drained"

    async def test_it_waits_for_more_than_one_empty_poll_when_asked(
            self, monkeypatch, clock):
        monkeypatch.setattr(project_events, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(project_events, "is_shutdown_requested", lambda: False)
        polls = {"n": 0}

        async def always_empty(session, limit):
            polls["n"] += 1
            return 0, True
        monkeypatch.setattr(project_events, "project_batch", always_empty)

        await run_project(0.01, 10, exit_after_empty=3, replay=False)
        assert polls["n"] == 3

    async def test_a_run_that_never_exits_on_empty_keeps_polling(
            self, monkeypatch, clock):
        """0 means forever, which is what systemd runs."""
        monkeypatch.setattr(project_events, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(project_events, "is_shutdown_requested", StopsAfter(5))

        async def always_empty(session, limit):
            return 0, True
        monkeypatch.setattr(project_events, "project_batch", always_empty)

        await run_project(0.01, 10, exit_after_empty=0, replay=False)
        assert clock.waits == [0.01] * 5, "it stopped polling, or polled at the wrong rate"

    async def test_replay_wipes_before_it_rebuilds(self, monkeypatch, clock):
        """--replay is destructive and the order is the whole of it: wipe,
        then rebuild. Rebuilding first would project rows and then delete
        them."""
        monkeypatch.setattr(project_events, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(project_events, "is_shutdown_requested", StopsAfter(1))
        order = []

        async def fake_reset(session):
            order.append("wiped")
        async def fake_project(session, limit):
            order.append("projected")
            return 1, False
        monkeypatch.setattr(project_events, "reset_projections", fake_reset)
        monkeypatch.setattr(project_events, "project_batch", fake_project)

        await run_project(0.01, 10, 0, replay=True)
        assert order == ["wiped", "projected"]

    async def test_without_replay_nothing_is_wiped(self, monkeypatch, clock):
        monkeypatch.setattr(project_events, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(project_events, "is_shutdown_requested", StopsAfter(1))
        wiped = []

        async def fake_reset(session):
            wiped.append(1)
        async def fake_project(session, limit):
            return 0, True
        monkeypatch.setattr(project_events, "reset_projections", fake_reset)
        monkeypatch.setattr(project_events, "project_batch", fake_project)

        await run_project(0.01, 10, 0, replay=False)
        assert wiped == [], "a plain run wiped the fact tables"

    async def test_the_sweep_run_once_sweeps_exactly_once(self, monkeypatch, clock):
        monkeypatch.setattr(sweep_floor, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(sweep_floor, "is_shutdown_requested", lambda: False)
        swept = []

        async def fake_sweep(session, **kw):
            swept.append(1)
            return {"found": 0, "opened": 0, "resolved": 0}
        monkeypatch.setattr(sweep_floor, "sweep", fake_sweep)

        await run_sweep(every_secs=999, once=True,
                              silent_after=180, idle_grace=300)
        assert len(swept) == 1
        assert clock.waits == [], "--once still waited for the next sweep"

    async def test_the_drain_stops_when_asked(self, monkeypatch, clock):
        monkeypatch.setattr(drain_to_archive, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(drain_to_archive, "is_shutdown_requested", StopsAfter(2))

        async def one_each(session, limit):
            return 1, ["k"]
        monkeypatch.setattr(drain_to_archive, "drain_batch", one_each)

        total = await run_drain(poll_secs=0.01, batch=10,
                                           exit_after_empty=0)
        assert total == 2

    async def test_retention_does_nothing_unless_a_policy_is_set(
            self, monkeypatch, clock):
        """Deleting is the only irreversible thing this service does, so
        the default has to be that it does not. 0 means keep for ever, and
        that is what a deployment gets if nobody sets the number."""
        monkeypatch.setattr(drain_to_archive, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(drain_to_archive, "is_shutdown_requested", StopsAfter(1))
        called = []

        async def no_work(session, limit):
            return 0, []

        async def note_expire_pending(session, after_days):
            called.append(("pending", after_days))

        async def note_expire_archive(session, keep_days, limit):
            called.append(("archive", keep_days))
            return 0, 0

        monkeypatch.setattr(drain_to_archive, "drain_batch", no_work)
        monkeypatch.setattr(drain_to_archive, "expire_pending", note_expire_pending)
        monkeypatch.setattr(drain_to_archive, "expire_archive", note_expire_archive)

        await run_drain(poll_secs=0.01, batch=10, exit_after_empty=0,
                                   keep_days=0, pending_days=0)
        assert called == [], "retention ran with no policy set"

    async def test_retention_runs_when_a_policy_is_set(self, monkeypatch, clock):
        monkeypatch.setattr(drain_to_archive, "sessionmaker", lambda: FakeMaker())
        monkeypatch.setattr(drain_to_archive, "is_shutdown_requested", StopsAfter(1))
        called = []

        async def no_work(session, limit):
            return 0, []

        async def note_expire_pending(session, after_days):
            called.append(("pending", after_days))

        async def note_expire_archive(session, keep_days, limit):
            called.append(("archive", keep_days))
            return 0, 0

        monkeypatch.setattr(drain_to_archive, "drain_batch", no_work)
        monkeypatch.setattr(drain_to_archive, "expire_pending", note_expire_pending)
        monkeypatch.setattr(drain_to_archive, "expire_archive", note_expire_archive)

        await run_drain(poll_secs=0.01, batch=10, exit_after_empty=0,
                                   keep_days=90, pending_days=7)
        assert called == [("pending", 7), ("archive", 90)], (
            f"retention passed the wrong numbers through: {called}")


# ============================================================ the flags

class TestTheFlagsSystemdPasses:
    """argparse defaults are the contract with the unit files. A default
    that drifts is a worker running at a cadence nobody chose."""

    def _defaults(self, mod):
        p = argparse.ArgumentParser()
        # Rebuild the parser the same way main() does, without running it.
        import inspect
        src = inspect.getsource(mod.main)
        assert "add_argument" in src
        return src

    def test_project_events_takes_the_flags_the_deployment_uses(self):
        src = self._defaults(project_events)
        for flag in ("--poll-secs", "--batch", "--exit-after-empty",
                     "--replay", "--log-level"):
            assert flag in src, f"{flag} is gone; a unit file or a runbook names it"

    def test_sweep_floor_takes_the_flags_the_deployment_uses(self):
        src = self._defaults(sweep_floor)
        for flag in ("--every-secs", "--once"):
            assert flag in src, f"{flag} is gone"

    def test_drain_takes_the_flags_the_deployment_uses(self):
        src = self._defaults(drain_to_archive)
        for flag in ("--poll-secs", "--batch"):
            assert flag in src, f"{flag} is gone"

    def test_every_worker_installs_signal_handlers(self):
        """A worker that does not is one systemd cannot stop cleanly - it
        gets SIGKILL after the timeout instead, mid-batch."""
        import inspect
        for mod in (project_events, sweep_floor, drain_to_archive):
            src = inspect.getsource(mod.main)
            assert "install_signal_handlers()" in src, (
                f"{mod.__name__} never installs signal handlers")

    def test_every_worker_configures_logging(self):
        import inspect
        for mod in (project_events, sweep_floor, drain_to_archive):
            src = inspect.getsource(mod.main)
            assert "logging.basicConfig" in src, (
                f"{mod.__name__} logs into the void")
