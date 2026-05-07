"""SQLAlchemy model for the ``backup_target`` table (issue #117
Phase 1b).

One row per operator-configured backup destination — Phase 1b
ships ``local_volume``; Phase 1c (S3) and 1d (SCP/Azure Blob)
add new ``kind`` values without schema changes thanks to the
JSONB ``config`` column.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BackupTarget(Base):
    __tablename__ = "backup_target"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    passphrase_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    passphrase_hint: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    schedule_cron: Mapped[str | None] = mapped_column(String(120), nullable=True)

    retention_keep_last_n: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retention_keep_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_run_status: Mapped[str] = mapped_column(String(20), nullable=False, default="never")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_run_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_run_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_run_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
