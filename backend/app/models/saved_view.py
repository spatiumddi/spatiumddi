"""Saved searches / saved views (issue #77).

A ``SavedView`` is a per-user, per-page named bundle of list-page UI
state — filters, sort, visible columns — stored as an opaque JSON
``payload`` the frontend shapes per page. "All subnets in DC1 over 80%
utilization, sorted by name" becomes a one-click view.

The row is owned by the user who created it (``ON DELETE CASCADE`` with
the user) and never shared — there's no cross-user visibility, so the
CRUD surface scopes every query by ``user_id`` instead of going through
the RBAC permission grammar.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SavedView(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "saved_view"
    __table_args__ = (
        # A user can't have two views with the same name on the same page.
        UniqueConstraint("user_id", "page", "name", name="uq_saved_view_user_page_name"),
        # Composite covers the only query shape (filter by user, optional
        # page) — kept in the model so create_all matches the migration's
        # index set (no redundant standalone user_id index).
        Index("ix_saved_view_user_page", "user_id", "page"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Stable page key the view belongs to, e.g. ``network.services`` /
    # ``network.circuits``. Opaque to the backend; the frontend owns the
    # mapping from route → key and only ever lists views for its own key.
    page: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Filter + sort + column state, shaped by the page. Opaque JSON so a
    # page can evolve its saved shape without a migration.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # At most one default per (user, page) — enforced in the router, not a
    # DB constraint (a partial unique index would block the common
    # "flip the default" two-write flow without a transaction dance).
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
