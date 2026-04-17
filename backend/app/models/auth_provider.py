"""External authentication providers (LDAP / OIDC / SAML / RADIUS / TACACS+)
and their external-group → internal-group mappings."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

PROVIDER_TYPES = ("ldap", "oidc", "saml", "radius", "tacacs")
# Password-grant provider types — tried during the POST /auth/login
# fallthrough (in priority order, after local auth fails). OIDC and SAML use
# a browser redirect flow and are not part of this list.
PASSWORD_PROVIDER_TYPES = ("ldap", "radius", "tacacs")


class AuthProvider(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "auth_provider"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    # ldap | oidc | saml | radius | tacacs
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Lower value = tried first. Local auth is always attempted before any provider.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    # Non-secret type-specific config (host, port, discovery URL, attr names, etc.)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Fernet-encrypted JSONB holding secrets (bind password, client secret, private key).
    secrets_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # On first successful external login: create User row automatically?
    auto_create_users: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # On subsequent logins: refresh email / display_name from the IdP?
    auto_update_users: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    mappings: Mapped[list[AuthGroupMapping]] = relationship(
        "AuthGroupMapping",
        back_populates="provider",
        cascade="all, delete-orphan",
        order_by="AuthGroupMapping.priority",
        lazy="selectin",
    )


class AuthGroupMapping(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "auth_group_mapping"

    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth_provider.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # External group identifier — LDAP DN, OIDC claim value, or SAML attribute value.
    external_group: Mapped[str] = mapped_column(String(1000), nullable=False)
    internal_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("group.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    provider: Mapped[AuthProvider] = relationship("AuthProvider", back_populates="mappings")

    __table_args__ = (
        UniqueConstraint("provider_id", "external_group", name="uq_auth_group_mapping_external"),
    )
