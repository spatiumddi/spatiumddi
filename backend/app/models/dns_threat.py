"""Per-client DNS behaviour rollups for threat analytics (issue #699).

``dns_query_log_entry`` is operator triage on a 24 h window. Threat
analytics needs something different: a *summary per client* that
outlives the raw rows, so "this host has looked like an exfil tunnel
every hour for three days" is answerable after the evidence lines have
been pruned.

One row per (client IP, hourly bucket), written by the aggregator in
``app.tasks.dns_threat``. The feature columns are deliberately
detection-agnostic — they describe what a client's DNS behaviour
*looked like*, not what any one heuristic concluded — so the DGA and
beaconing detections deferred out of #699's first slice can score off
the same rollup without a second aggregation pass.

``tunnel_score`` and ``tunnel_signals`` are the one detection that
ships in this slice. The signals blob carries each contributing
signal's raw value and weighted contribution, so an operator (and a
support thread) can see *why* a host scored 82 rather than being
handed a bare number.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DNSClientWindow(Base):
    """One client's DNS behaviour over one hourly bucket."""

    __tablename__ = "dns_client_window"
    __table_args__ = (
        # Recomputed in place as late log lines arrive, so the bucket
        # identity has to be unique for the upsert to land.
        UniqueConstraint("client_ip", "window_start", name="uq_dns_client_window_ip_bucket"),
        # "Worst offenders in the recent past" — the query behind both
        # the alert matcher and the Threat tab.
        Index("ix_dns_client_window_score", "window_start", "tunnel_score"),
        Index("ix_dns_client_window_ip", "client_ip"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    client_ip: Mapped[str] = mapped_column(INET, nullable=False)
    # Aggregated ACROSS servers rather than per-server: the question is
    # "is this host behaving like a tunnel", and a client that spreads
    # its queries over two resolvers is not two half-suspicious clients.
    # ``server_count`` keeps the fan-out visible.
    server_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # ── detection-agnostic behaviour features ────────────────────────
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distinct_qnames: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distinct_parents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The parent domain this client hit hardest in the window, and how
    # many distinct subdomains it asked for underneath it. A tunnel
    # concentrates: thousands of unique labels under ONE parent.
    top_parent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    top_parent_subdomains: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_label_length: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_label_length: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    mean_label_entropy: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # TXT / NULL / CNAME carry the most payload per response, so tunnels
    # lean on them far harder than ordinary traffic does.
    payload_qtype_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── tunneling verdict ────────────────────────────────────────────
    tunnel_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tunnel_signals: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    # Set when every parseable name in the window matched the benign
    # allowlist — genuinely cleared. NOT set when nothing was parseable,
    # which is a data problem and must stay visible, because every
    # surface filters allowlisted rows out by default.
    #
    # Kept as a column rather than dropping the row so the Threat tab's
    # "Show cleared (allowlisted)" toggle can answer "we looked at this
    # client and cleared it" instead of the client simply never
    # appearing.
    allowlisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
