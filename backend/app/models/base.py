import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    """All models inherit ``AsyncAttrs`` so ``await row.awaitable_attrs.<rel>``
    is available — the async-safe way to force a lazy-load under an
    AsyncSession without tripping MissingGreenlet. Purely additive; existing
    sync-style access still works everywhere relationships are eagerly loaded
    (e.g. via ``selectinload``)."""


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """30-day soft-delete + recovery for high-blast-radius IPAM/DNS/DHCP rows.

    The default ORM query filter (``app.db._filter_soft_deleted``) injects
    ``deleted_at IS NULL`` into every SELECT touching these models unless
    the caller opts in via ``execution_options(include_deleted=True)``.

    Cascades into descendants share the same ``deletion_batch_id`` so a
    single restore brings them all back atomically. A standalone soft-delete
    gets a fresh batch UUID, which still lets the restore endpoint use the
    same lookup-by-batch logic.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    deletion_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
