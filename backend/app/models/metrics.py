"""Per-server metric sample models (agent-reported deltas).

Each row is one fixed-width time bucket of counter deltas, reported
by the agent after it polls its local daemon (BIND9 statistics-
channels for DNS, Kea ``statistic-get-all`` for DHCP). Storing deltas
rather than raw counters means retention pruning doesn't require a
running-sum recompute, and counter resets on daemon restart don't
back-propagate into the time series. The FK cascade drops a server's
samples when the server row is removed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DNSMetricSample(Base):
    __tablename__ = "dns_metric_sample"
    __table_args__ = (PrimaryKeyConstraint("server_id", "bucket_at", name="pk_dns_metric_sample"),)

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server.id", ondelete="CASCADE"),
        nullable=False,
    )
    bucket_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    queries_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    noerror: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    nxdomain: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    servfail: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    recursion: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class DHCPMetricSample(Base):
    __tablename__ = "dhcp_metric_sample"
    __table_args__ = (PrimaryKeyConstraint("server_id", "bucket_at", name="pk_dhcp_metric_sample"),)

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server.id", ondelete="CASCADE"),
        nullable=False,
    )
    bucket_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    discover: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    offer: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    request: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ack: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    nak: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    decline: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    release: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    inform: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


__all__ = ["DNSMetricSample", "DHCPMetricSample"]
