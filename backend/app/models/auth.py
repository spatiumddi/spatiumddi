import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Column,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Many-to-many: users ↔ groups
user_group = Table(
    "user_group",
    Base.metadata,
    Column("user_id", UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", UUID(as_uuid=True), ForeignKey("group.id", ondelete="CASCADE"), primary_key=True),
)

# Many-to-many: groups ↔ roles
group_role = Table(
    "group_role",
    Base.metadata,
    Column("group_id", UUID(as_uuid=True), ForeignKey("group.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", UUID(as_uuid=True), ForeignKey("role.id", ondelete="CASCADE"), primary_key=True),
)


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user"

    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Auth source: local | ldap | oidc
    auth_source: Mapped[str] = mapped_column(String(20), nullable=False, default="local")
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # LDAP DN or OIDC sub

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    force_password_change: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # MFA
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    groups: Mapped[list["Group"]] = relationship("Group", secondary=user_group, back_populates="users")
    sessions: Mapped[list["UserSession"]] = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")
    api_tokens: Mapped[list["APIToken"]] = relationship("APIToken", foreign_keys="[APIToken.user_id]", back_populates="user", cascade="all, delete-orphan")


class Group(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "group"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    auth_source: Mapped[str] = mapped_column(String(20), nullable=False, default="local")
    external_dn: Mapped[str | None] = mapped_column(String(1000), nullable=True)  # LDAP DN

    users: Mapped[list[User]] = relationship("User", secondary=user_group, back_populates="groups")
    roles: Mapped[list["Role"]] = relationship("Role", secondary=group_role, back_populates="groups")


class Role(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """
    Named permission set. Roles are assigned to groups; permissions within a role
    may be scoped to specific resource IDs (e.g., a particular IPSpace or Subnet).
    """
    __tablename__ = "role"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Permissions stored as JSONB: list of {action, resource_type, resource_id}
    # e.g., [{"action": "ipam:subnet:write", "resource_type": "ip_space", "resource_id": "<uuid>"}]
    permissions: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)

    groups: Mapped[list[Group]] = relationship("Group", secondary=group_role, back_populates="roles")


class UserSession(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "user_session"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user: Mapped[User] = relationship("User", back_populates="sessions")


class APIToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "api_token"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(10), nullable=False)  # First 8 chars for identification

    # scope: global | user
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="user")

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )

    # Optional restriction: list of allowed API path prefixes
    allowed_paths: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    permissions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user: Mapped[User | None] = relationship("User", foreign_keys=[user_id], back_populates="api_tokens")
