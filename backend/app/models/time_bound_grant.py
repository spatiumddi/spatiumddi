"""Time-bound (temporary, auto-expiring) RBAC grants — issue #65.

A :class:`TimeBoundGrant` row attaches one
``{action, resource_type, resource_id?}`` permission to a group until
``expires_at``. ``app.core.permissions.user_has_permission`` walks the
caller's live grants (loaded into ``User._active_time_bound_grants`` by the
auth dependency) as a purely additive union over the static role grants — so
a temporary grant can widen access without touching any role.

Lifecycle:

* Created via ``POST /api/v1/groups/time-bound-grants`` (admin on ``group``).
* Consulted live on every request, with an ``expires_at > now()`` filter at
  query time so expiry is honoured even before the sweep runs.
* Soft-revoked by either the operator (revoke-now) or the 60 s beat sweep
  (``app.tasks.time_bound_grant_sweep``). Soft-revoke sets ``revoked_at`` and
  keeps the row for audit / history — rows are never hard-deleted.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.auth import Group


class TimeBoundGrant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "time_bound_grant"

    # Composite index for the per-request "live grants for these groups"
    # query: filter group_id IN (...) AND revoked_at IS NULL AND
    # expires_at > now(). Mirrors the index created in migration
    # d5e9b2c14a07 so autogenerate / ``alembic check`` see no drift.
    __table_args__ = (Index("ix_time_bound_grant_live", "group_id", "revoked_at", "expires_at"),)

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("group.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The granted permission triple. ``resource_id`` None / "" means the
    # whole resource_type (matches ``core.permissions._resource_id_matches``).
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # When the grant stops being effective. The auth dependency filters
    # ``expires_at > now()`` at load time so expiry applies even before the
    # sweep flips ``revoked_at``.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # Set on soft-revoke (operator revoke-now or the expiry sweep). NULL = live.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reason: Mapped[str] = mapped_column(String(1000), nullable=False, default="")

    granted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    group: Mapped["Group"] = relationship("Group")
