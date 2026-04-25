"""Tailscale integration — per-tenant connection config.

Same shape as ``KubernetesCluster`` / ``DockerHost`` / ``ProxmoxNode``
but for a Tailscale tenant (tailnet). Auth is a personal-access
token (PAT); tailnet is the slug printed on
https://login.tailscale.com/admin/settings/general — or the
literal ``-`` for the operator's default tailnet.

Read-only mirror: SpatiumDDI never writes to Tailscale. The
reconciler hits ``GET /api/v2/tailnet/{tn}/devices?fields=all`` and
mirrors each device's ``addresses[]`` (CGNAT IPv4 + IPv6 ULA) into
the bound IPAM space. Tailnet domain is auto-derived from the
first device FQDN — no separate config field.
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


class TailscaleTenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A Tailscale tenant (tailnet) SpatiumDDI polls for device state."""

    __tablename__ = "tailscale_tenant"
    __table_args__ = (Index("ix_tailscale_tenant_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Connection ──────────────────────────────────────────────────
    # Tailnet slug from the admin console, or the literal ``-`` to
    # mean "the PAT's default tailnet". Stored verbatim — the API
    # uses it as a path segment.
    tailnet: Mapped[str] = mapped_column(String(255), nullable=False, default="-")
    # Fernet-encrypted PAT (``tskey-api-…``). Empty = unset.
    api_key_encrypted: Mapped[bytes] = mapped_column(
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
    # The CGNAT block to use for IPv4 device IPs. Tailscale defaults
    # every tailnet to ``100.64.0.0/10``, but the operator's tailnet
    # may have been assigned a different CGNAT slice — keep the
    # override knob.
    cgnat_cidr: Mapped[str] = mapped_column(String(32), nullable=False, default="100.64.0.0/10")
    # IPv6 ULA block. Tailscale assigns every tailnet
    # ``fd7a:115c:a1e0::/48`` by default. Override if your tailnet
    # was provisioned with a different ULA prefix.
    ipv6_cidr: Mapped[str] = mapped_column(
        String(64), nullable=False, default="fd7a:115c:a1e0::/48"
    )
    # Skip devices whose ``expires`` timestamp has passed. On by
    # default since expired devices are usually stale and clutter
    # IPAM with hosts that can't actually reach the tailnet.
    skip_expired: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

    # ── Cadence ─────────────────────────────────────────────────────
    # Tailscale's documented rate limit is 100 req/min. 60 s default
    # gives plenty of headroom; 30 s floor keeps parity with Docker /
    # Proxmox / Kubernetes integrations.
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # ── Sync state ──────────────────────────────────────────────────
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Populated by the test-connection probe + reconciler — shown in
    # the UI. Tailnet domain is derived from the first device FQDN.
    tailnet_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    device_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


__all__ = ["TailscaleTenant"]
