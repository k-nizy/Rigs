"""Graceful shutdown, shared by every worker.

The platform team's convention: SIGINT and SIGTERM set a flag, the loop
finishes the item it is holding and then stops. A worker killed mid-batch
must never leave a half-projected ledger, and the ledger is what makes
that recoverable anyway - but finishing cleanly means the logs say what
happened.
"""

import signal

_shutdown = False


def install_signal_handlers() -> None:
    def _handle(signum, _frame):
        global _shutdown
        _shutdown = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, AttributeError):
            # Not the main thread, or a platform without SIGTERM.
            pass


def is_shutdown_requested() -> bool:
    return _shutdown
