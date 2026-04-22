"""Audit-forwarding target model.

One row per delivery destination. The ``kind`` discriminator picks
which column subset matters (syslog vs webhook); the rest stay at
their defaults. Formats are pluggable — the service layer picks the
right formatter at send time.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AuditForwardTarget(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "audit_forward_target"

    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    # kind: syslog | webhook
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # format: rfc5424_json | rfc5424_cef | rfc5424_leef | rfc3164 | json_lines
    # Ignored for kind="webhook" — webhooks always deliver JSON.
    format: Mapped[str] = mapped_column(String(32), nullable=False, default="rfc5424_json")

    # ── syslog fields ──────────────────────────────────────────────
    host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=514)
    # protocol: udp | tcp | tls
    protocol: Mapped[str] = mapped_column(String(10), nullable=False, default="udp")
    facility: Mapped[int] = mapped_column(Integer, nullable=False, default=16)
    # Optional PEM-encoded CA certificate for TLS verification. When NULL
    # on a TLS target, the system CA bundle is used.
    ca_cert_pem: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── webhook fields ─────────────────────────────────────────────
    url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    # Plaintext today (matches existing audit_forward_webhook_auth_header on
    # platform_settings); migrating to Fernet-at-rest is a separate pass
    # tracked with the other secret-hardening work.
    auth_header: Mapped[str] = mapped_column(String(1024), nullable=False, default="")

    # ── filter ─────────────────────────────────────────────────────
    # Drop events below this severity. Accepted values: info | warn |
    # error | denied. Null = forward everything.
    min_severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Optional allowlist of ``AuditLog.resource_type`` values. Null or
    # empty = forward everything.
    resource_types: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)


__all__ = ["AuditForwardTarget"]
