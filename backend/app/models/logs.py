"""Per-server query/activity log entries shipped from agents.

Each row is one parsed log line. Agents tail their daemon's log file
(BIND9 query log, Kea ``kea-dhcp4.log``), batch new lines, and POST
to the control plane every few seconds. The control plane parses
lines into structured columns and stores them here for the Logs UI.

This is **operator triage**, not analytics. We keep a short rolling
window (default 24 h) so the UI stays responsive and the table size
is bounded; longer retention belongs in Loki / a SIEM.

The FK cascade drops a server's entries when the server row is
removed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import INET, MACADDR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DNSQueryLogEntry(Base):
    """One parsed BIND9 query log line.

    Example raw line:

        ``client @0x... 192.0.2.5#54321 (example.com): query: example.com IN A +E(0)K (10.0.0.1)``

    We extract the client IP / port, qname, qclass, qtype, and the
    flags string into structured columns; the full original line is
    kept in ``raw`` for cases the parser doesn't fully understand.
    """

    __tablename__ = "dns_query_log_entry"
    __table_args__ = (
        Index("ix_dns_query_log_server_ts", "server_id", "ts"),
        Index("ix_dns_query_log_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server.id", ondelete="CASCADE"),
        nullable=False,
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    client_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    client_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qname: Mapped[str | None] = mapped_column(String(512), nullable=True)
    qclass: Mapped[str | None] = mapped_column(String(8), nullable=True)
    qtype: Mapped[str | None] = mapped_column(String(16), nullable=True)
    flags: Mapped[str | None] = mapped_column(String(64), nullable=True)
    view: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw: Mapped[str] = mapped_column(Text, nullable=False, default="")


class DHCPLogEntry(Base):
    """One parsed Kea ``kea-dhcp4.log`` line.

    Kea's stock log format:

        ``2026-04-25 16:30:01.123 INFO  [kea-dhcp4.leases/12345.139...] DHCP4_LEASE_ALLOC [hwtype=1 aa:bb:cc:dd:ee:ff], cid=[no info], tid=0x12345678: lease 192.0.2.10 has been allocated for 3600 seconds``

    Structured columns capture the most-filterable bits (severity,
    log code, MAC, IP, transaction ID); the full original line stays
    in ``raw``.
    """

    __tablename__ = "dhcp_log_entry"
    __table_args__ = (
        Index("ix_dhcp_log_server_ts", "server_id", "ts"),
        Index("ix_dhcp_log_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server.id", ondelete="CASCADE"),
        nullable=False,
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mac_address: Mapped[str | None] = mapped_column(MACADDR, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    transaction_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw: Mapped[str] = mapped_column(Text, nullable=False, default="")


__all__ = ["DNSQueryLogEntry", "DHCPLogEntry"]
