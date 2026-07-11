"""Cisco Meraki MX integration — per-organization connection config (#606).

Meraki is a pure cloud vendor: a single Dashboard API (``https://api.meraki.com
/api/v1``, header ``X-Cisco-Meraki-API-Key``) keyed by an API key + an
organization id — nothing on-prem to reach. One ``MerakiOrg`` row = one Meraki
organization; the reconciler walks the org's appliance networks.

Two integration shapes ride on this one row (same split as ``PANOSFirewall``):

1. **Read-only mirror (Shape 1).** Per-network appliance **VLANs** → IPAM
   subnets, appliance **DHCP fixed-IP reservations** → IPAM addresses
   (high-value), org **policy objects / groups** → ``FirewallObject``, MX
   **1:1 NAT + port-forward** rules → ``nat_mapping``, and — opt-in — network
   **clients** → IPAM addresses. Strictly read-only.

2. **Per-client block enforcement (Shape 2, the #601 tier).** A separate,
   opt-in write capability. When ``block_sync_enabled`` is armed the #601
   block-sync reconciler moves a blocked client's ``devicePolicy`` to
   ``Blocked`` (or a named group policy) via the Dashboard API — the cloud
   applies it immediately, no on-prem deploy. Uses a DISTINCT write-scoped API
   key (never the read key). See ``app.services.block_sync.reconcile``.

Guardrails mirror OPNsense/UniFi/PAN-OS block-sync (#601): mirror stays
read-only; enforcement is a per-target master switch (default OFF), gated by
the ``security.block_sync`` module, the ``manage_firewall_enforcement``
permission, and the two-person approval workflow (#62).
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MerakiOrg(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A Meraki organization SpatiumDDI polls (and, when armed, moves blocked
    clients to a restrictive group policy on)."""

    __tablename__ = "meraki_org"
    __table_args__ = (Index("ix_meraki_org_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Connection ──────────────────────────────────────────────────
    # Dashboard API base — defaults to the global cloud; override for a
    # regional shard (e.g. ``https://api.meraki.cn/api/v1``) or Meraki's
    # ``api.meraki.com`` mirror hostnames.
    base_url: Mapped[str] = mapped_column(
        String(255), nullable=False, default="https://api.meraki.com/api/v1"
    )
    # The Meraki organization id (from ``getOrganizations``).
    org_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # Fernet-encrypted READ-scoped Dashboard API key.
    api_key_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, default=b"", server_default=sa_text("''::bytea")
    )
    # Optional network-id allow-list (from ``getOrganizationNetworks``). Empty
    # = mirror every appliance network in the org.
    network_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

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
    # VLANs (→ subnets), DHCP fixed-IP reservations (→ addresses), policy
    # objects (→ FirewallObject) and 1:1 NAT/port-forward (→ nat_mapping) are
    # the high-value signal — default ON. The full client list is noisy —
    # opt-in.
    mirror_policy_objects: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    mirror_vlans: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    mirror_dhcp_reservations: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    mirror_nat_rules: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    mirror_clients: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # ── Cadence ─────────────────────────────────────────────────────
    # Meraki's Dashboard API is rate-limited (~10 req/s per org), so a slower
    # default than the on-prem mirrors. 300 s default, 30 s floor.
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)

    # ── Per-client block enforcement (#601 tier) ─────────────────────
    block_sync_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    # Distinct WRITE-scoped Dashboard API key (needs config-write on the
    # networks). When empty the reconciler refuses to push (no silent
    # fall-back to the read key).
    block_sync_api_key_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, default=b"", server_default=sa_text("''::bytea")
    )
    # The device policy a blocked client is moved to. Phase 1 wires the Meraki
    # built-in ``Blocked`` (sent verbatim as ``devicePolicy``); a custom named
    # group policy would additionally need its ``groupPolicyId`` resolved and
    # ``devicePolicy="Group policy"`` — a follow-up. Keep this ``Blocked``.
    block_policy_name: Mapped[str] = mapped_column(String(127), nullable=False, default="Blocked")

    # Block-sync convergence state (surfaced in the UI).
    last_block_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_block_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Sync state ──────────────────────────────────────────────────
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated by the test-connection probe / reconciler — shown in the UI.
    network_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    object_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


__all__ = ["MerakiOrg"]
