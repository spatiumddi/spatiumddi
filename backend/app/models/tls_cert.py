"""TLS certificate monitoring (issue #118).

SpatiumDDI already tracks Domains (RDAP expiry / NS-drift / registrar
changes — all alerting). This is the next layer down: the TLS certs
actually served from the hostnames we manage. Targets are auto-discovered
from DNS A/AAAA records (or added ad-hoc), probed on a schedule, and the
captured cert identity + chain validity is alerted on.

Two tables:

* :class:`TLSCertTarget` — *what to probe* + the per-row schedule + the
  latest-known cert identity (denormalised onto the row so list views
  never join). One row per ``(host, port, server_name)`` connect tuple.
* :class:`TLSCertProbe` — immutable per-probe history (rotated; 90-day
  retention via the prune task), one snapshot per probe.

The issue sketches a third ``tls_cert`` "latest-known" table; we fold
that role into the target row (denormalised identity columns) to avoid a
circular FK + a join on every list view. The probe table is the history.

We never hold private keys for these certs — only the public material the
endpoint serves — so nothing here is encrypted at rest.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Target provenance.
SOURCE_MANUAL = "manual"
SOURCE_DISCOVERED = "discovered"

# Derived health bucket (most-urgent wins; see derive_tls_state).
STATE_UNKNOWN = "unknown"  # never probed yet
STATE_OK = "ok"
STATE_EXPIRING = "expiring"  # not_after within the warn window
STATE_EXPIRED = "expired"
STATE_MISMATCH = "mismatch"  # chain invalid / hostname mismatch
STATE_UNREACHABLE = "unreachable"  # probe transport / handshake failed

TLS_CERT_STATES = frozenset(
    {
        STATE_UNKNOWN,
        STATE_OK,
        STATE_EXPIRING,
        STATE_EXPIRED,
        STATE_MISMATCH,
        STATE_UNREACHABLE,
    }
)


class TLSCertTarget(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tls_cert_target"
    __table_args__ = (
        # Dedupe key — the same FQDN can surface from multiple zones /
        # views, but a probe is uniquely identified by its connect tuple.
        # NULLS NOT DISTINCT so the dominant NULL-SNI case is actually
        # DB-enforced (Postgres treats NULLs as distinct by default, which
        # would let two (host,443,NULL) rows slip the constraint). PG15+;
        # matches the network.py / ownership.py convention.
        UniqueConstraint(
            "host",
            "port",
            "server_name",
            name="uq_tls_cert_target_host_port_sni",
            postgresql_nulls_not_distinct=True,
        ),
    )

    # ── connect target ──────────────────────────────────────────────
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=443, server_default="443")
    # SNI override; falls back to ``host`` when NULL.
    server_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SOURCE_MANUAL, server_default=SOURCE_MANUAL
    )

    # ── provenance / linkage (all SET NULL so deleting the source row
    # leaves the cert history intact) ───────────────────────────────
    dns_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_record.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    dns_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_zone.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("domain.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # IPAM linkage (#118 Phase 2) — set for role-discovered targets + lets
    # the IP detail modal list "certs served from this IP".
    ip_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_address.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ── schedule ────────────────────────────────────────────────────
    # Per-target cadence override; NULL → use the platform default.
    interval_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── derived state ───────────────────────────────────────────────
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=STATE_UNKNOWN, server_default=STATE_UNKNOWN
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # ── latest-known cert identity (denormalised from the newest
    # successful probe; NULL until first probe) ─────────────────────
    serial: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subject_cn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issuer_cn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sans_json: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    key_algo: Mapped[str | None] = mapped_column(String(20), nullable=True)
    key_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sig_algo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chain_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chain_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    chain_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    self_signed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    fingerprint_sha256: Mapped[str | None] = mapped_column(String(95), nullable=True)


class TLSCertProbe(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tls_cert_probe"
    __table_args__ = (
        # Latest-probe lookup + history pagination per target.
        Index("ix_tls_cert_probe_target_probed", "target_id", "probed_at"),
    )

    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tls_cert_target.id", ondelete="CASCADE"),
        nullable=False,
    )
    probed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Snapshot of what was observed (all nullable — a failed probe has none).
    serial: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subject_cn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issuer_cn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sans_json: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    key_algo: Mapped[str | None] = mapped_column(String(20), nullable=True)
    key_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sig_algo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chain_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chain_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    chain_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    self_signed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    fingerprint_sha256: Mapped[str | None] = mapped_column(String(95), nullable=True)

    # Optional captured PEM — powers the get_cert_chain UI / MCP tool.
    # Public material only; we never possess these certs' private keys.
    leaf_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    chain_pem: Mapped[str | None] = mapped_column(Text, nullable=True)


# Column-name list of the identity snapshot — shared by the probe service
# when it copies a probe's observation onto the target's denormalised row.
IDENTITY_FIELDS: tuple[str, ...] = (
    "serial",
    "subject_cn",
    "issuer_cn",
    "not_before",
    "not_after",
    "sans_json",
    "key_algo",
    "key_size",
    "sig_algo",
    "chain_depth",
    "chain_valid",
    "chain_error",
    "self_signed",
    "fingerprint_sha256",
)


def identity_snapshot(probe: TLSCertProbe) -> dict[str, Any]:
    """Pull the identity fields off a probe row as a plain dict (for
    copying onto the target's denormalised columns)."""
    return {f: getattr(probe, f) for f in IDENTITY_FIELDS}
