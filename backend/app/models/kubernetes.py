"""Kubernetes integration — cluster connection config (Phase 1a).

Stores the read-only service-account credentials for each connected
cluster, plus the IPAM space / DNS group the cluster's reconciled state
will land in once Phase 1b (the actual sync task) ships. Per-cluster
rows, not PlatformSettings — operators run many clusters and each
binds independently.

The bearer token is Fernet-encrypted at rest alongside the DHCP /
DNS / auth-provider driver creds. CA bundle is stored in cleartext
(it's a public cert).
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
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class KubernetesCluster(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A Kubernetes cluster SpatiumDDI pulls state from.

    **Phase 1a**: only holds the connection config + bindings. No sync
    state beyond ``last_synced_at`` / ``last_sync_error`` which stay
    null until Phase 1b lands the reconciler.
    """

    __tablename__ = "kubernetes_cluster"
    __table_args__ = (Index("ix_kubernetes_cluster_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Connection
    api_server_url: Mapped[str] = mapped_column(String(500), nullable=False)
    # PEM-encoded CA bundle. Cleartext — it's a public cert. Empty
    # string means "trust the system CA store" (cloud providers with
    # publicly-signed API servers).
    ca_bundle_pem: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Fernet-encrypted bearer token (service account token).
    token_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # Binding — discovered state lands here.
    ipam_space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_space.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Optional DNS binding — unset means "don't mirror Ingress records".
    dns_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server_group.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Operator-provided CIDRs. Pod CIDR could be derived from Node
    # objects and service CIDR can't be (apiserver flag, not API),
    # so keep both as explicit inputs. Empty string = "don't reserve
    # a block for this CIDR in IPAM".
    pod_cidr: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    service_cidr: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # Poll cadence — 60 s default matches the DHCP / DNS health cadence.
    # Floored at 30 s; anything higher puts real load on the apiserver
    # for multi-cluster deployments.
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # Opt-in pod IP mirroring. Pods churn — a busy cluster can generate
    # thousands of create/delete events per day, which would noisy-up
    # audit log and bloat ``ip_address``. Off by default; operators who
    # want pod-level visibility can flip it on per cluster. Service
    # ClusterIPs are always mirrored — they're stable and one per Service.
    mirror_pods: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # Sync state (populated in Phase 1b).
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated by the test-connection probe — surfaced in the UI so
    # operators see cluster version + node count after saving.
    cluster_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    node_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


__all__ = ["KubernetesCluster"]
