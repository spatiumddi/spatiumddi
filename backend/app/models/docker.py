"""Docker integration — per-host connection config.

Mirrors the ``KubernetesCluster`` model shape. Each connected Docker
host is a ``DockerHost`` row with a connection endpoint, credentials,
and an IPAM space / DNS group binding. Many hosts per install are
supported — each binds independently.

Connection types:
  * ``unix``  — local socket, e.g. ``/var/run/docker.sock``.
                Only works when SpatiumDDI has the host socket
                mounted into its api container.
  * ``tcp``   — remote daemon on ``tcp://host:2376`` with optional
                mTLS (``ca_bundle_pem`` + ``client_cert_pem`` +
                ``client_key_pem``). Best default for remote.
  * ``ssh``   — deferred. Docker CLI supports ``ssh://user@host`` via
                ``docker system dial-stdio``; replicating that flow in
                Python requires paramiko + port-forwarding which is
                more code than the other two combined. Phase 1.1.

The TLS client key + (eventual) SSH key are Fernet-encrypted at rest.
CA bundle + client cert are cleartext (public material).
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


class DockerHost(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A Docker daemon SpatiumDDI pulls network + container state from."""

    __tablename__ = "docker_host"
    __table_args__ = (Index("ix_docker_host_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Connection ──────────────────────────────────────────────────
    # One of: "unix" | "tcp".
    connection_type: Mapped[str] = mapped_column(String(16), nullable=False, default="tcp")
    # Endpoint: path for unix, "host:port" for tcp. Never carries the
    # scheme — the client constructs the right httpx URL from type +
    # endpoint so ``tcp://`` and ``https://`` aren't interchangeable.
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)

    # TLS (tcp only). All three are optional — unencrypted tcp works
    # too but the validator in the router warns against it.
    ca_bundle_pem: Mapped[str] = mapped_column(Text, nullable=False, default="")
    client_cert_pem: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Fernet-encrypted private key. Empty bytes = no client cert.
    client_key_encrypted: Mapped[bytes] = mapped_column(
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
    # Networks are always mirrored; containers are opt-in because a
    # busy CI host spinning up ephemeral test containers could churn
    # the ``ip_address`` table thousands of times per day. Same shape
    # as ``KubernetesCluster.mirror_pods``.
    mirror_containers: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    # ``bridge`` (Docker's default unconfigured 172.17.0.0/16 bridge)
    # and ``host`` / ``none`` networks get skipped by default — noise.
    # Toggling this mirrors them too.
    include_default_networks: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    # Default False → only Running containers land in IPAM. True
    # includes Created / Exited / Paused / Restarting so operators can
    # see capacity even when things are stopped.
    include_stopped_containers: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # ── Cadence ─────────────────────────────────────────────────────
    # Docker responds fast and containers don't change that often.
    # 60 s default, 30 s floor. Swept by ``sweep_docker_hosts`` on a
    # 30 s beat tick.
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # ── Sync state ──────────────────────────────────────────────────
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated by the test-connection probe — shown in the UI so
    # operators see engine version + container count after saving.
    engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


__all__ = ["DockerHost"]
