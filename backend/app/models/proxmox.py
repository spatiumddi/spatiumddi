"""Proxmox VE integration — per-endpoint connection config.

Same shape as ``KubernetesCluster`` / ``DockerHost`` but for a PVE
REST endpoint. A single ``ProxmoxNode`` row represents either a
standalone PVE host **or** a whole cluster — the PVE REST API is
homogeneous across cluster members, so pointing SpatiumDDI at any
one node gives it the cluster's full state via ``/cluster/status``
and ``/nodes``.

Auth is always API-token (``user@realm!tokenid`` + UUID secret).
PVE tokens carry explicit ACLs so producing a read-only token is a
checkbox away — see the setup guide in the admin page.
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


class ProxmoxNode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A Proxmox VE endpoint SpatiumDDI polls for network + guest state.

    The row name ``ProxmoxNode`` matches the CLAUDE.md spec and is
    accurate for standalone hosts; for clusters it's a slight misnomer
    (we're polling one node but mirroring cluster-wide state). Kept
    for consistency — the admin UI refers to these as "endpoints".
    """

    __tablename__ = "proxmox_node"
    __table_args__ = (Index("ix_proxmox_node_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Connection ──────────────────────────────────────────────────
    # Host without scheme (e.g. ``pve01.example.com`` or ``10.0.0.5``).
    # The client builds ``https://{host}:{port}/api2/json/...``.
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=8006)
    # Set to False for self-signed labs. Setting guidance — and the
    # test-connection error message — points operators at uploading the
    # CA cert as the right answer for prod.
    verify_tls: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    # Optional PEM for self-signed / internal CAs. When non-empty, the
    # client trusts this CA in addition to the system store.
    ca_bundle_pem: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Token ID like ``root@pam!spatiumddi`` — not a secret, the secret
    # is the UUID below.
    token_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Fernet-encrypted token secret (UUID). Empty bytes = unset.
    token_secret_encrypted: Mapped[bytes] = mapped_column(
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
    # Unlike Docker containers (which can be ephemeral CI workers and
    # noisy), Proxmox VMs + LXC are typically long-lived operator
    # inventory. Default ON so the integration is useful out of the
    # box; operators who only want subnet-mirroring can turn either
    # toggle off.
    mirror_vms: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    mirror_lxc: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    # Default False → only running guests land in IPAM. True keeps
    # stopped VMs/LXCs too (useful for capacity-planning views).
    include_stopped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # SDN VNets without declared subnets are common in the "PVE as L2
    # passthrough, gateway lives upstream" pattern. With this toggle on,
    # the reconciler tries to infer each empty VNet's CIDR from the
    # NICs of guests attached to it:
    #   1. Prefer any ``static_cidr`` on a guest NIC (exact prefix from
    #      ipconfigN / LXC ``ip=`` — always safe, no guessing).
    #   2. Fall back to guest-agent / ``/interfaces`` runtime IPs and
    #      assume /24 around them — speculative; wrong for /23 or /25
    #      deployments. Logged as a warning so operators can fix by
    #      declaring proper SDN subnets with ``pvesh create``.
    # Default OFF because the /24 fallback is a guess and operators who
    # want exact behaviour should declare subnets in PVE.
    infer_vnet_subnets: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # ── Cadence ─────────────────────────────────────────────────────
    # PVE API is fast but cluster-wide polling (iterate every node's
    # qemu/lxc/network endpoints) can get chatty — 120 s default,
    # 30 s floor. Swept by ``sweep_proxmox_nodes`` on a 30 s beat tick.
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120)

    # ── Sync state ──────────────────────────────────────────────────
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated by the test-connection probe — shown in the UI.
    pve_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cluster_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    node_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Per-reconcile discovery snapshot — counts + per-guest list with
    # reason codes. Drives the "Discovery" modal in the admin page so
    # operators can see which VMs aren't reporting runtime IPs (agent
    # off / not installed / no NICs / no static IP) without trawling
    # logs. Written by the reconciler on every successful pass.
    # Shape: see services/proxmox/reconcile.py::_build_discovery_payload.
    last_discovery: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


__all__ = ["ProxmoxNode"]
