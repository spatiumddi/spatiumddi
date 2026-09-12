"""Feature-module toggles — operator-controlled visibility for whole
sidebar/REST/MCP surfaces.

A row here means ONE thing: an operator changed this module's state.
The shipped default lives in the catalog
(``app.services.feature_modules.MODULES``) and nowhere else, so a module
with no row resolves to its ``default_enabled``. Migrations no longer
seed rows — they used to, which made the catalog default unreachable on
every install and is what #1069 had to undo.

Unknown ids in the table are tolerated (forward-compat with downgrades)
but never gate anything.
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
    place; the row is created lazily, the first time an operator
    toggles that module.
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
