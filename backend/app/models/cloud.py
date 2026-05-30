"""Cloud integration (issue #37, Part A) — per-account endpoint config.

Same read-only-mirror shape as ``ProxmoxNode`` / ``DockerHost`` /
``KubernetesCluster`` / ``UnifiController`` but for a public-cloud
account. A single ``CloudEndpoint`` row represents one cloud account
SpatiumDDI polls for infrastructure state (VPCs / subnets / instance
NICs / public IPs / load-balancer frontends → IPAM rows).

The ``provider`` discriminator gates connector dispatch. AWS / Azure /
GCP are implemented; the enum reserves the token-only providers
(``hetzner`` / ``digitalocean`` / ``linode`` / ``vultr``) so a future
phase can light them up without a model change — they line up with the
same provider names Part B (Cloud DNS) reserves, so "I connected my
DigitalOcean account" could eventually feed both surfaces from one
credential.

Auth varies per provider and lives Fernet-encrypted in
``credentials_encrypted`` (a JSON dict; see ``services/cloud/base.py``
for the per-provider shape). Non-secret routing identifiers
(subscription ids for Azure, project ids for GCP) live in the plaintext
``provider_config`` JSONB so the UI can show them without a decrypt;
``regions`` is a first-class column because it is the common cross-
provider scope filter.

Mirror rows provenance via the ``cloud_endpoint_id`` FK on
``IPBlock`` / ``Subnet`` / ``IPAddress`` with ``ON DELETE CASCADE`` so
removing the endpoint sweeps every materialised row atomically — same
contract every other integration uses.
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

# Providers wired into the connector dispatch today. The model column is
# a plain ``String`` so reserving more (token-only providers) needs no
# migration; the API validator is the gate on what's actually accepted.
CLOUD_PROVIDERS_IMPLEMENTED: frozenset[str] = frozenset({"aws", "azure", "gcp"})


class CloudEndpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A public-cloud account SpatiumDDI polls for infrastructure state."""

    __tablename__ = "cloud_endpoint"
    __table_args__ = (Index("ix_cloud_endpoint_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Provider selection ──────────────────────────────────────────
    # "aws" | "azure" | "gcp"  (reserved: hetzner | digitalocean | linode | vultr)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)

    # ── Credentials (Fernet-encrypted JSON at rest) ─────────────────
    # AWS:   {"access_key_id", "secret_access_key"}
    # Azure: {"tenant_id", "client_id", "client_secret"}
    # GCP:   {"service_account_json"}   (the whole key file as a string)
    # Empty bytes = unset.
    credentials_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, default=b"", server_default=sa_text("''::bytea")
    )

    # Non-secret routing scope, shown in the UI without a decrypt:
    # Azure: {"subscription_ids": [...]}   GCP: {"project_ids": [...]}   AWS: {}
    provider_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa_text("'{}'::jsonb")
    )
    # Region / location allow-list. Empty = all regions the account can
    # see. AWS fans out a client per region; Azure / GCP filter the flat
    # resource list. Stored as a JSON string array.
    regions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa_text("'[]'::jsonb")
    )

    # ── Binding ─────────────────────────────────────────────────────
    # Private VPC/VNet networks + their subnets + instance NICs land
    # under this space (one IPBlock per VPC CIDR, Subnets beneath).
    ipam_space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_space.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Optional separate space for cloud-public / cloud-lb IPs. NULL =
    # keep them in ``ipam_space_id`` (only mirrored when an enclosing
    # subnet exists there — public IPs are otherwise out-of-band /32s).
    public_space_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_space.id", ondelete="SET NULL"),
        nullable=True,
    )
    dns_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server_group.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Mirror policy ───────────────────────────────────────────────
    mirror_load_balancers: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    # Default OFF → only running instances land in IPAM. ON keeps
    # stopped / deallocated instances too (capacity-planning views).
    mirror_stopped_instances: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # ── Cadence ─────────────────────────────────────────────────────
    # Cloud APIs are slower + rate-limited; 300 s default, 60 s floor.
    # Swept by ``sweep_cloud_endpoints`` on a 30 s beat tick.
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)

    # ── Sync state ──────────────────────────────────────────────────
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated by the test-connection probe + every reconcile — shown
    # in the UI. ``provider_account_id`` is the AWS account id / Azure
    # tenant-or-sub / GCP project the credentials resolved to.
    provider_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    network_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    instance_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Per-reconcile discovery snapshot — counts + per-instance reason
    # codes (no NIC / no enclosing subnet / stopped). Drives the
    # "Discovery" modal, same as Proxmox. Written on every success.
    last_discovery: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


__all__ = ["CLOUD_PROVIDERS_IMPLEMENTED", "CloudEndpoint"]
