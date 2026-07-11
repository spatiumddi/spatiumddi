"""Palo Alto PAN-OS / Panorama integration — per-firewall connection config
plus the mirrored firewall address-object store (issue #605).

Same per-target shape as ``OPNsenseRouter`` / ``NetbirdInstance`` but for a
Palo Alto NGFW or a Panorama management server. A single ``PANOSFirewall``
row points SpatiumDDI at one managed *scope*:

* a **standalone** NGFW — one ``vsys`` (default ``vsys1``);
* a **Panorama** device-group — ``is_panorama=True`` + ``device_group``.

Two integration shapes ride on this one row:

1. **Read-only mirror (Shape 1).** The reconciler pulls address objects +
   groups (→ ``FirewallObject`` mirror rows), NAT rules (→ ``nat_mapping``
   rows with provenance), and — when enabled — zone/interface CIDRs +
   DHCP leases (→ IPAM subnets / addresses). Auth is a read-scoped API key
   minted with ``type=keygen``, Fernet-encrypted at rest. This half is
   *strictly read-only*: SpatiumDDI never mutates firewall config here.

2. **Dynamic Address Group enforcement (Shape 2, the #601 tier).** A
   SEPARATE, opt-in write capability. When ``block_sync_enabled`` is armed
   the ``#601`` block-sync reconciler registers ``IP → tag`` via the PAN-OS
   **User-ID API** (no policy commit — a pre-created DAG matching the tag
   picks it up near-instantly). This uses DISTINCT write-scoped credentials
   (a User-ID-capable API key) and never falls back to the read key. See
   ``app.services.block_sync.reconcile`` for the converging reconciler and
   ``PANOSFirewall.block_sync_enabled`` / ``block_tag_name`` below.

Guardrails mirror OPNsense/UniFi block-sync (#601): the mirror stays
read-only; enforcement is a per-target master switch (default OFF, distinct
from the ``integrations.paloalto`` feature module); enforcement is further
gated by the ``security.block_sync`` module, the ``manage_firewall_enforcement``
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
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# ``FirewallObject.kind`` — the shape a PAN-OS address object resolves to.
# ``host`` = single IP/netmask-32, ``network`` = a CIDR, ``range`` = an
# IP range (``ip-range``), ``fqdn`` = an FQDN object. ``group`` = an
# address-group (its ``value`` is a comma-joined member list, not a CIDR).
FIREWALL_OBJECT_KINDS: tuple[str, ...] = ("host", "network", "range", "fqdn", "group")


class PANOSFirewall(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A Palo Alto NGFW / Panorama scope SpatiumDDI polls (and, when armed,
    pushes DAG tags to)."""

    __tablename__ = "panos_firewall"
    __table_args__ = (Index("ix_panos_firewall_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Connection ──────────────────────────────────────────────────
    # Host without scheme (e.g. ``pa.example.com`` or ``10.0.0.1``). The
    # client builds ``https://{host}:{port}/api/...`` (XML) and
    # ``/restapi/v{api_version}/...`` (REST).
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=443)
    # Set to False for self-signed lab boxes. Setting guidance / the
    # test-connection error message points operators at uploading the CA
    # cert as the right answer for prod.
    verify_tls: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    # Optional PEM for self-signed / internal CAs.
    ca_bundle_pem: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # PAN-OS REST API version segment (e.g. ``10.1``, ``11.0``). Objects/NAT
    # ride REST; the User-ID + op-command paths ride the legacy XML API which
    # is version-agnostic.
    api_version: Mapped[str] = mapped_column(String(8), nullable=False, default="10.1")

    # Fernet-encrypted READ-scoped API key (minted via ``type=keygen``).
    # Empty bytes = unset. Distinct from the enforcement key below.
    api_key_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, default=b"", server_default=sa_text("''::bytea")
    )

    # ── Scoping (Panorama vs standalone) ────────────────────────────
    # A Panorama server centralises many firewalls via device-groups; a
    # standalone NGFW is the single-``vsys`` case. Model one row per managed
    # scope: standalone → set ``vsys`` (default ``vsys1``); Panorama → set
    # ``is_panorama`` + ``device_group``.
    is_panorama: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    vsys: Mapped[str] = mapped_column(String(64), nullable=False, default="vsys1")
    device_group: Mapped[str] = mapped_column(String(255), nullable=False, default="")

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
    # Address objects/groups + NAT rules are the high-value "shadow IPAM"
    # signal — both default ON. Zone/interface CIDRs → subnet context and
    # DHCP leases → IPAM addresses are opt-in secondary sources (a firewall
    # may front many LANs SpatiumDDI already models elsewhere).
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
    # 60 s default, 30 s floor. Swept by ``sweep_panos_firewalls`` on a
    # 30 s beat tick.
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # ── DAG enforcement (#601 tier — write-back via User-ID) ─────────
    # The mirror above stays strictly read-only. This block is a SEPARATE,
    # opt-in write capability: the #601 block-sync reconciler registers
    # SpatiumDDI's ``network_block`` desired-state IPs as ``IP → tag`` via
    # the PAN-OS User-ID API. A pre-created Dynamic Address Group matching
    # the tag enforces it with NO policy commit.
    #
    # ``block_sync_enabled`` is the per-target master switch — default OFF,
    # independent of both ``enabled`` (mirror) and the
    # ``integrations.paloalto`` feature module. Nothing is ever pushed
    # unless an operator explicitly arms this AND the ``security.block_sync``
    # module is on.
    block_sync_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    # Distinct WRITE-scoped credential — a User-ID-capable API key. The read
    # mirror only needs a read-only admin; enforcement needs User-ID write.
    # When empty the reconciler refuses to push (it does NOT silently fall
    # back to the read key).
    block_sync_api_key_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, default=b"", server_default=sa_text("''::bytea")
    )
    # The tag SpatiumDDI registers on blocked IPs (e.g. ``spatiumddi-quarantine``).
    # The operator pre-creates a DAG whose match is ``'<block_tag_name>'``.
    # Empty = not configured (reconcile no-ops).
    block_tag_name: Mapped[str] = mapped_column(
        String(127), nullable=False, default="spatiumddi-quarantine"
    )

    # Block-sync convergence state (surfaced in the UI).
    last_block_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_block_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Sync state ──────────────────────────────────────────────────
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated by the test-connection probe / reconciler — shown in the UI.
    sw_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nat_rule_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FirewallObject(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A mirrored PAN-OS address object / group — SpatiumDDI's "shadow IPAM".

    Named deliberately to NOT collide with the appliance's own fleet-nftables
    ``FirewallPolicy`` / ``FirewallRule`` / ``FirewallAlias`` (#285). The table
    name ``firewall_endpoint_object`` reinforces that these mirror an external
    firewall's *endpoint* address objects, not the appliance's own rules.

    Rows carry provenance (``panos_firewall_id`` FK, ``ON DELETE CASCADE``) so
    they sweep when the target is removed. Where an object resolves to a known
    IPAM CIDR/IP, ``ip_address_id`` / ``subnet_id`` are stamped so the UI can
    show the drift both ways (object with no IPAM row, subnet with no object).
    """

    __tablename__ = "firewall_endpoint_object"
    __table_args__ = (
        # One object name is unique within a firewall scope.
        UniqueConstraint("panos_firewall_id", "name", name="uq_firewall_object_fw_name"),
        Index("ix_firewall_object_panos_firewall_id", "panos_firewall_id"),
        Index("ix_firewall_object_value", "value"),
    )

    panos_firewall_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("panos_firewall.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The PAN-OS object name (its primary key on the firewall).
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # host | network | range | fqdn | group — validated against
    # ``FIREWALL_OBJECT_KINDS`` at the reconciler.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # The object's value verbatim from the firewall: a CIDR (``10.0.0.5/32``),
    # a range (``10.0.0.5-10.0.0.9``), an FQDN, or — for ``group`` — a
    # comma-joined member-name list. Kept as text so lossless round-trips and
    # drift reporting work regardless of kind.
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # PAN-OS object tags (``["web", "pci"]``) — surfaced in the UI and usable
    # for DAG-tag mapping.
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # For ``host``/``network``/``range`` objects, the canonical first IP or
    # CIDR parsed out of ``value`` — used for the IPAM drift join. NULL for
    # ``fqdn`` / unresolvable ``group`` objects.
    resolved_cidr: Mapped[str | None] = mapped_column(INET, nullable=True)

    # Optional links to the live IPAM rows this object resolves to. Both
    # ``ON DELETE SET NULL`` — deleting the IPAM row leaves the mirror intact
    # (the drift report then flags "object with no IPAM row").
    ip_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_address.id", ondelete="SET NULL"), nullable=True
    )
    subnet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subnet.id", ondelete="SET NULL"), nullable=True
    )


__all__ = ["FIREWALL_OBJECT_KINDS", "PANOSFirewall", "FirewallObject"]
