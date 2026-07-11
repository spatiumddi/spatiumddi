"""OPNsense integration — per-firewall connection config.

Same shape as ``ProxmoxNode`` / ``TailscaleTenant`` but for an
OPNsense firewall's REST API. A single ``OPNsenseRouter`` row points
SpatiumDDI at one OPNsense box; the reconciler mirrors its interface
CIDRs (LAN / OPT* / VLANs) as IPAM subnets and its DHCPv4 leases +
static reservations (+ optionally the ARP table) as IP addresses.

Auth is always HTTP Basic with an API key/secret pair: the API key
goes in as the username, the API secret as the password, over
``https://{host}:{port}``. OPNsense API keys are minted per-user
under System → Access → Users → API keys; producing a read-only
account is a matter of scoping the user's group privileges (see the
setup guide in the admin page).
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


class OPNsenseRouter(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An OPNsense firewall SpatiumDDI polls for interface + DHCP state."""

    __tablename__ = "opnsense_router"
    __table_args__ = (Index("ix_opnsense_router_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Connection ──────────────────────────────────────────────────
    # Host without scheme (e.g. ``opnsense.example.com`` or ``10.0.0.1``).
    # The client builds ``https://{host}:{port}/api/...``.
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=443)
    # Set to False for self-signed lab boxes. Setting guidance — and the
    # test-connection error message — points operators at uploading the
    # CA cert as the right answer for prod.
    verify_tls: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    # Optional PEM for self-signed / internal CAs. When non-empty, the
    # client trusts this CA in addition to the system store.
    ca_bundle_pem: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # API key (the Basic-auth *username*) — not a secret, the secret is
    # the api_secret below. Stored in plaintext like Proxmox's token_id.
    api_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Fernet-encrypted API secret (the Basic-auth *password*). Empty
    # bytes = unset.
    api_secret_encrypted: Mapped[bytes] = mapped_column(
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
    # DHCPv4 leases + static reservations are the high-value signal —
    # both default ON. ARP is a noisier secondary source (every device
    # the firewall has ever seen on the wire), so it defaults OFF.
    mirror_dhcp_leases: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    mirror_static_mappings: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    mirror_arp: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # ── Cadence ─────────────────────────────────────────────────────
    # 60 s default, 30 s floor. Swept by ``sweep_opnsense_routers`` on a
    # 30 s beat tick.
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # ── Active block sync (#601 — enforcement write-back) ────────────
    # The mirror above stays strictly read-only. This block is a
    # SEPARATE, opt-in write capability that pushes SpatiumDDI's
    # ``network_block`` desired-state (IPs) into an operator-pre-created
    # OPNsense firewall *table alias*. SpatiumDDI only ever mutates alias
    # membership (add/delete + reconfigure) — never rule CRUD.
    #
    # ``block_sync_enabled`` is the per-target master switch — default
    # OFF, independent of both ``enabled`` (mirror) and the
    # ``integrations.opnsense`` feature module. Nothing is ever pushed
    # unless an operator explicitly arms this AND the
    # ``security.block_sync`` module is on.
    block_sync_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    # Distinct WRITE-scoped credentials. The read mirror only needs a
    # read-only API user; enforcement needs a Firewall-privileged user.
    # When empty, the reconciler refuses to push (it does NOT silently
    # fall back to the read creds). Same Basic-auth shape as above.
    block_sync_api_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    block_sync_api_secret_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, default=b"", server_default=sa_text("''::bytea")
    )
    # The OPNsense table alias SpatiumDDI mutates (e.g. ``spatiumddi_blocked``).
    # The operator pre-creates one block rule referencing this alias; we
    # add/remove IPs to converge. Empty = not configured (reconcile no-ops).
    block_alias_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # Block-sync convergence state (surfaced in the UI).
    last_block_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_block_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Sync state ──────────────────────────────────────────────────
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated by the test-connection probe / reconciler — shown in
    # the UI.
    firmware_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    interface_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


__all__ = ["OPNsenseRouter"]
