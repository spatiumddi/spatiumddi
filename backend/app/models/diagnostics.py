"""SQLAlchemy model for the ``internal_error`` table (issue #123).

One row per *fingerprint* of unhandled exception. The capture path in
``app.services.diagnostics.capture`` either bumps ``occurrence_count``
on a fingerprint match (within the suppression window) or inserts a
fresh row. Operators read this through the admin Diagnostics page.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InternalError(Base):
    __tablename__ = "internal_error"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    # ``api`` / ``worker`` / ``beat``.
    service: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="unhandled_exception",
        server_default="unhandled_exception",
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    route_or_task: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exception_class: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text(), nullable=False)
    traceback: Mapped[str] = mapped_column(Text(), nullable=False)
    context_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        default=1,
        server_default="1",
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    suppressed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
