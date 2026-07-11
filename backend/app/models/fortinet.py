"""Fortinet FortiGate integration — per-firewall connection config (#606).

Same per-target read-only-mirror shape as ``PANOSFirewall`` (#605) but for a
FortiGate NGFW driven over the **FortiOS REST API** (``/api/v2/cmdb/...`` +
``/api/v2/monitor/...``, bearer-token auth). One ``FortinetFirewall`` row
points SpatiumDDI at one FortiGate VDOM (default ``root``).

Only the **read-only mirror (Shape 1)** rides on this row: address objects /
groups → ``FirewallObject``, VIPs (destination NAT) → ``nat_mapping``, and —
when enabled — interface CIDRs → IPAM subnets and DHCP leases → IPAM
addresses. This half is strictly read-only.

**Enforcement (Shape 2) is deliberately NOT a field here.** On FortiGate the
clean, deploy-free block primitive is an *External Threat Feed* connector:
SpatiumDDI hosts a token-scoped block-list URL and the FortiGate polls it, so
there are **no write credentials on the firewall at all**. That "feed
inversion" (#606) lives in ``app.models.firewall_feed.FirewallFeed`` /
``app.services.firewall_feeds`` — an operator points the FortiGate's threat
feed at the SpatiumDDI feed URL; nothing is pushed from here.

FortiManager centralisation (JSON-RPC over an ADOM) is a follow-up; this row
models a FortiGate reached directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class FortinetFirewall(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A FortiGate VDOM SpatiumDDI polls (read-only mirror)."""

    __tablename__ = "fortinet_firewall"
    __table_args__ = (Index("ix_fortinet_firewall_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Connection ──────────────────────────────────────────────────
    # Host without scheme (``fg.example.com`` / ``10.0.0.1``). The client
    # builds ``https://{host}:{port}/api/v2/...``.
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=443)
    verify_tls: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    # Optional PEM for self-signed / internal CAs.
    ca_bundle_pem: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # FortiGate VDOM to scope reads to (default ``root``). All CMDB/monitor
    # calls pass ``?vdom=<vdom>``.
    vdom: Mapped[str] = mapped_column(String(64), nullable=False, default="root")

    # Fernet-encrypted READ-scoped REST API token (a FortiGate "REST API
    # admin" bearer token, ``Authorization: Bearer <token>``). Empty = unset.
    api_token_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, default=b"", server_default=sa_text("''::bytea")
    )

    # ── Binding ─────────────────────────────────────────────────────
    ipam_space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_space.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dns_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server_group.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Mirror policy ───────────────────────────────────────────────
    # Address objects/groups + VIPs (DNAT) are the high-value "shadow IPAM"
    # signal — both default ON. Interface CIDRs → subnet context and DHCP
    # leases → IPAM addresses are opt-in secondary sources.
    mirror_address_objects: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    mirror_nat_rules: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    mirror_interfaces: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    mirror_dhcp_leases: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # ── Cadence ─────────────────────────────────────────────────────
    # 60 s default, 30 s floor. Swept by ``sweep_fortinet_firewalls``.
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # ── Sync state ──────────────────────────────────────────────────
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated by the test-connection probe / reconciler — shown in the UI.
    sw_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nat_rule_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


__all__ = ["FortinetFirewall"]
