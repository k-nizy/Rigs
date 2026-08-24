"""The declarative base every model inherits.

Kept in its own module on purpose: models import from here, repositories
import models, and nothing imports back up. That is the circular-import
firewall the layering depends on.
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampedBase(Base):
    """For rows the server creates. `received_at` is deliberately separate
    from any timestamp the rig reports: the difference between them is
    clock skew, and twelve rigs disagreeing about the time makes the whole
    event stream unsortable."""

    __abstract__ = True

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
