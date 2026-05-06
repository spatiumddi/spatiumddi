"""Feature-module toggles — operator-controlled visibility for whole
sidebar/REST/MCP surfaces.

Default-enabled-on-install is the product policy (admins discover what
exists before they decide to disable it). Off-prem / secret-touching
modules can override that by seeding ``enabled=False`` in the migration.

The catalog of known module ids lives in
``app.services.feature_modules.MODULES`` — the table only stores the
operator's per-module override; unknown ids in the table are tolerated
(forward-compat with downgrades) but never gate anything.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FeatureModule(Base):
    """A togglable platform feature.

    ``id`` is a stable dotted name (e.g. ``network.customer``,
    ``ai.copilot``). The catalog is hardcoded in
    ``app.services.feature_modules`` so a new feature is added in one
    place and seeds its row via Alembic.
    """

    __tablename__ = "feature_module"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
