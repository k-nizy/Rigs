"""How a session ends, and what that says about the shift."""

from datetime import datetime

from core.domains.sessions.model import Session

HANDOVER = "handover"   # ran to its boundary - the ordinary case
OPERATOR = "operator"   # ended early, with the rig down


def close(row: Session, ended_at: datetime, ended_by: str) -> Session:
    """Close a session, but never re-close one.

    Replay walks the ledger from the beginning, so a session already
    closed by a stint_ended must not be reopened and re-closed by a later
    session_ended. First close wins.
    """
    if row.ended_at is None:
        row.ended_at = ended_at
        row.ended_by = ended_by
    return row
