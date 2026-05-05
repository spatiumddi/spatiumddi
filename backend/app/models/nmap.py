"""Nmap scan history.

A single row represents one operator-triggered nmap scan against an
IP. The ``target_ip`` column is denormalised so the scan record
survives even if the originating ``IPAddress`` row is deleted —
operators frequently scan IPs that aren't (yet) in IPAM.

The ``raw_stdout`` column is line-buffered while the scan runs; the
SSE streaming endpoint polls this column at ~500 ms intervals to
forward new lines to the browser. Once the scan reaches a terminal
state (``completed`` / ``failed`` / ``cancelled``) we also persist
the full XML emitted via ``nmap -oX -`` and a parsed ``summary_json``
for fast UI rendering without re-parsing on every detail open.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NmapScan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One nmap invocation initiated from the SpatiumDDI UI."""

    __tablename__ = "nmap_scan"
    __table_args__ = (
        Index("ix_nmap_scan_target_ip_started", "target_ip", "started_at"),
        Index("ix_nmap_scan_status", "status"),
        Index("ix_nmap_scan_ip_address", "ip_address_id"),
    )

    # Operator-supplied scan target. Despite the column name (kept for
    # audit / API continuity) this can be either an IP literal or a
    # hostname / FQDN — nmap resolves the latter at scan time. Stored
    # as VARCHAR(255) (DNS hard upper bound).
    target_ip: Mapped[str] = mapped_column(String(255), nullable=False)
    ip_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_address.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Inputs ─────────────────────────────────────────────────────────
    # preset: quick | service_version | os_fingerprint | default_scripts
    #         | udp_top1000 | aggressive | custom
    preset: Mapped[str] = mapped_column(
        String(32), nullable=False, default="quick", server_default="quick"
    )
    port_spec: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extra_args: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Run state ──────────────────────────────────────────────────────
    # status: queued | running | completed | failed | cancelled
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command_line: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Output ─────────────────────────────────────────────────────────
    raw_xml: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Provenance ─────────────────────────────────────────────────────
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )


__all__ = ["NmapScan"]
