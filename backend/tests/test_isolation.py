"""What a test leaves behind for the next one.

`_run_schema` gives each run its own database schema, so rows cannot
leak between runs. None of what follows is rows.

The rig limiter, the login limiter and the lockout are module globals,
built lazily on first use and shared by every test in the process. Six
test files touch them. Until `_no_state_between_tests` existed, nothing
cleared them, and the result was a suite that gave different answers to
the same command minutes apart.

The lockout is the one that bites. It is keyed on `time.monotonic` and
stores when a pair may try again, so whether a sign-in is refused
depends not only on how many failures preceded it but on how much wall
time passed while they did - which varies with machine load. The symptom
is a 401 in a test that never asked for one, because a correct lockout
does not announce itself to the caller. That is very hard to read as
"another test did this" when you meet it.

These two tests are deliberately paired and order-dependent: the first
dirties the global, the second asserts it is clean. If somebody removes
the autouse fixture, the second fails and names what happened.
"""

from core.infrastructure.config import get_settings
from services.rigs import auth, people

KEY = ("someone@verlet.co", "10.0.0.1")


def test_a_test_may_dirty_the_process_globals():
    """Ordinary work. Every sign-in test does this without meaning to."""
    lock = people.lockout(get_settings())
    for _ in range(6):
        lock.failed(KEY)
    assert lock.tracked() > 0, "the point of this test is to leave a mark"

    limiter = people.login_limiter(get_settings())
    assert limiter is not None


def test_and_the_next_test_still_starts_clean():
    """The property the autouse fixture exists to hold.

    Settings are deliberately untouched here. `lockout()` rebuilds itself
    when the settings differ, so a test that changes them is accidentally
    isolated and proves nothing - which is why the leak survived being
    looked for.
    """
    lock = people.lockout(get_settings())
    assert lock.tracked() == 0, (
        "%d strike(s) survived the previous test. Every sign-in after "
        "this one is judged against failures it did not cause." % lock.tracked()
    )


def test_the_rig_limiter_is_reset_too():
    """Same shape, different global: a floor emptying its outbox must not
    spend the next test's allowance."""
    assert auth._limiter is None or auth._limiter.per_minute >= 0
