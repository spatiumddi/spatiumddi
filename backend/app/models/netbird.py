"""NetBird integration — per-instance connection config.

Same shape as ``TailscaleTenant`` / ``ProxmoxNode`` / ``DockerHost``
but for a NetBird deployment (a management server + a personal-access
token). NetBird is a managed WireGuard mesh overlay — like Tailscale
but self-hostable — with a real REST management API, which is what
lets it be a read-only pull mirror (unlike raw WireGuard, which has
no API).

Read-only mirror: SpatiumDDI never writes to NetBird. The reconciler
hits ``GET /api/peers`` on the instance's management server and
mirrors each peer's overlay ``ip`` into the bound IPAM space. The
management DNS domain is auto-derived from the first peer's
``dns_label`` — no separate config field.

Unlike Tailscale (cloud-only, fixed ``api.tailscale.com`` host),
NetBird is self-hosted by default, so each instance carries its own
management-server ``api_url`` (operator-supplied) plus a ``verify_tls``
toggle for private-CA / self-signed deployments. NetBird peers carry a
single IPv4 overlay address (no IPv6 ULA), so there is one overlay
CIDR — not the CGNAT + ULA pair Tailscale mirrors.
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


class NetbirdInstance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A NetBird deployment SpatiumDDI polls for peer state."""

    __tablename__ = "netbird_instance"
    __table_args__ = (Index("ix_netbird_instance_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Connection ──────────────────────────────────────────────────
    # Management-server base URL. Cloud is ``https://api.netbird.io``;
    # a self-hosted install is the dashboard/management host (the API
    # is served under ``/api`` on the same host). Operator-supplied, so
    # the test-connection probe runs it through the advisory SSRF guard
    # at the API boundary (as the other operator-URL integrations do).
    api_url: Mapped[str] = mapped_column(
        String(255), nullable=False, default="https://api.netbird.io"
    )
    # Verify the management server's TLS certificate. Off lets a
    # self-hosted install with a private-CA / self-signed cert connect.
    verify_tls: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    # Fernet-encrypted personal-access token (``nbp_…``). Empty = unset.
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
    # The overlay range peer IPs live in. NetBird allocates peer
    # addresses from CGNAT space (``100.64.0.0/10``) by default; the
    # actual sub-range is management-server config, so keep the
    # override knob. The whole range is mirrored as one flat subnet —
    # the mesh is a routed overlay, not a subdivided LAN.
    network_cidr: Mapped[str] = mapped_column(String(32), nullable=False, default="100.64.0.0/10")
    # Skip peers whose NetBird login has expired (only when the peer
    # actually has login-expiration enabled). On by default since an
    # expired peer can't reach the mesh and just clutters IPAM.
    skip_expired: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

    # ── Cadence ─────────────────────────────────────────────────────
    # 60 s default; 30 s floor keeps parity with the other mirrors.
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # ── Sync state ──────────────────────────────────────────────────
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Populated by the test-connection probe + reconciler — shown in
    # the UI. Derived from the first peer's ``dns_label`` FQDN.
    dns_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    peer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


__all__ = ["NetbirdInstance"]
