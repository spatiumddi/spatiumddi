"""UniFi Network Application integration — per-controller endpoint
config (issue #30).

Same shape as ``ProxmoxNode`` / ``DockerHost`` / ``KubernetesCluster``
but for a UniFi controller, which can be either a local console
(direct HTTP to ``https://<controller>/proxy/network/...``) or a
cloud-hosted console reached via ``api.ui.com`` and the
cloud-connector path. The reconciler picks the transport from
``mode`` and constructs the same logical paths underneath.

Auth flavours:
  * ``api_key`` — modern UniFi OS (≥ 4.x circa 2024). Header
    ``X-API-Key`` works for both the public Integration API and
    the legacy controller API behind ``/proxy/network/api/...``.
    Required for ``mode="cloud"``.
  * ``user_password`` — legacy local controllers that predate the
    integration API. Cookie + CSRF flow. Stored Fernet-encrypted.

We expose the legacy controller API as the rich-data source
because the public Integration API deliberately omits MAC,
hostname, network_id, oui, fixed_ip, ip_subnet, dhcpd_*, etc. —
none of which we can synthesise. Both APIs ride the same TLS
connection, so this is a per-call routing choice, not an
auth/transport split.
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


class UnifiController(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A UniFi Network controller SpatiumDDI mirrors networks +
    clients from. One row covers a single controller; a single
    controller may own many sites.
    """

    __tablename__ = "unifi_controller"
    __table_args__ = (Index("ix_unifi_controller_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Transport ────────────────────────────────────────────────────
    # ``local``  — direct HTTPS to a controller IP/hostname. The base
    #              URL is ``https://{host}:{port}/proxy/network``.
    # ``cloud``  — every call is wrapped in the cloud connector path
    #              ``https://api.ui.com/proxy/network/integration/v1/
    #              connector/consoles/{cloud_host_id}/...``.
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=443)
    cloud_host_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verify_tls: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    ca_bundle_pem: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # ── Auth ─────────────────────────────────────────────────────────
    auth_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="api_key")
    api_key_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, default=b"", server_default=sa_text("''::bytea")
    )
    username_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, default=b"", server_default=sa_text("''::bytea")
    )
    password_encrypted: Mapped[bytes] = mapped_column(
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
    # Networks default ON — a controller without subnets mirrored is
    # useless. Clients default ON because the issue's selling point is
    # "plug it in, it shows up in IPAM"; admins who run noisy guest
    # SSIDs can flip it off or scope to specific networks.
    mirror_networks: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    mirror_clients: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    mirror_fixed_ips: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

    # ``[]`` = mirror every site the controller exposes. Any non-empty
    # list narrows by site name (legacy "desc" / "name") or site id —
    # the reconciler matches both. Recommended once the controller's
    # site count climbs past ~5 (admin UI surfaces a soft warning).
    site_allowlist: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa_text("'[]'::jsonb")
    )
    # Per-site VLAN allowlist: ``{"<site_id_or_name>": [10, 20]}``. A
    # site missing from this map mirrors all of its networks. Use this
    # to keep guest SSIDs out of IPAM without disabling whole sites.
    network_allowlist: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa_text("'{}'::jsonb")
    )

    include_wired: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    include_wireless: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    # VPN clients (L2TP / OpenVPN / WireGuard / Teleport) are usually
    # operator-managed elsewhere; default off so we don't fight the
    # "every laptop on the road shows up as a stale row" problem.
    include_vpn: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # ── Cadence ─────────────────────────────────────────────────────
    # Floor at 30 s for local; reconciler clamps to 60 s when
    # ``mode="cloud"`` so we don't hammer api.ui.com (it rate-limits).
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # ── Sync state ──────────────────────────────────────────────────
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated by the test-connection probe and refreshed each pass.
    controller_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    site_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    network_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Per-pass discovery rollup — site / network / client counts +
    # any sites we couldn't read (auth scoped to a subset). Same shape
    # convention as ``ProxmoxNode.last_discovery``: written by the
    # reconciler, surfaced verbatim in the admin Discovery modal.
    last_discovery: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


__all__ = ["UnifiController"]
