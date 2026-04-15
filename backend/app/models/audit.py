import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditLog(Base):
    """
    Append-only audit trail of all data mutations.
    Stored in PostgreSQL (not the log store) for queryability and compliance.
    No application-layer deletes are ever performed on this table.
    A DB-level trigger prevents DELETE as an extra guard.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_timestamp", "timestamp"),
        Index("ix_audit_log_user_id", "user_id"),
        Index("ix_audit_log_resource", "resource_type", "resource_id"),
        Index("ix_audit_log_action", "action"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Who
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # auth_source: local | ldap | oidc | system
    auth_source: Mapped[str] = mapped_column(String(20), nullable=False, default="local")
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # What
    # action: create | update | delete | login | logout | sync | permission_change | ...
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_display: Mapped[str] = mapped_column(String(500), nullable=False)

    # State change
    old_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    changed_fields: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    # Correlation
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # result: success | denied | error
    result: Mapped[str] = mapped_column(String(20), nullable=False, default="success")
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
