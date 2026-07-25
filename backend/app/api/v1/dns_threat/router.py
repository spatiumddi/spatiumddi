"""DNS threat analytics read surface (issue #699).

Read-only: the detections write themselves via the rollup task, and
there is nothing here an operator mutates. Gated behind the default-off
``security.dns_threat`` feature module at the router include, so the
whole prefix 404s on installs that haven't opted in.

Gated on ``read:server`` at the router, matching the Logs surface it
summarises — anyone trusted to read raw queries is trusted to read a
rollup of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from app.api.deps import DB, CurrentUser
from app.core.permissions import require_permission
from app.models.dns_threat import DNSClientWindow
from app.services.dns_threat.aggregate import INTERESTING_SCORE

# Same gate the Logs router uses: this is a rollup of the DNS query
# log, so whoever may read raw queries may read a summary of them.
router = APIRouter(dependencies=[Depends(require_permission("read", "server"))])


class ClientWindowRow(BaseModel):
    id: str
    client_ip: str
    window_start: datetime
    window_end: datetime
    query_count: int
    distinct_qnames: int
    distinct_parents: int
    top_parent: str | None
    top_parent_subdomains: int
    max_label_length: int
    mean_label_entropy: float
    payload_qtype_count: int
    tunnel_score: float
    tunnel_signals: list[dict[str, Any]]
    allowlisted: bool
    server_count: int


class ThreatSummary(BaseModel):
    """Rollup for the Security dashboard card."""

    windows_scored: int
    clients_seen: int
    suspicious_clients: int
    peak_score: float
    worst_client_ip: str | None
    worst_client_score: float | None
    worst_client_parent: str | None
    since: datetime
    # False when the rollup has produced nothing at all, which is the
    # difference between "no threats" and "not actually running" — the
    # UI needs to say which, or an idle card reads as an all-clear.
    has_data: bool


def _to_row(w: DNSClientWindow) -> ClientWindowRow:
    return ClientWindowRow(
        id=str(w.id),
        client_ip=str(w.client_ip),
        window_start=w.window_start,
        window_end=w.window_end,
        query_count=w.query_count,
        distinct_qnames=w.distinct_qnames,
        distinct_parents=w.distinct_parents,
        top_parent=w.top_parent,
        top_parent_subdomains=w.top_parent_subdomains,
        max_label_length=w.max_label_length,
        mean_label_entropy=w.mean_label_entropy,
        payload_qtype_count=w.payload_qtype_count,
        tunnel_score=w.tunnel_score,
        tunnel_signals=w.tunnel_signals or [],
        allowlisted=w.allowlisted,
        server_count=w.server_count,
    )


@router.get("/windows", response_model=list[ClientWindowRow])
async def list_windows(
    db: DB,
    current_user: CurrentUser,
    client_ip: str | None = Query(default=None),
    hours: int = Query(default=24, ge=1, le=24 * 30),
    min_score: float = Query(default=INTERESTING_SCORE, ge=0, le=100),
    include_allowlisted: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[ClientWindowRow]:
    """Scored client windows, worst first.

    Defaults to the interesting band rather than everything: on a busy
    resolver the vast majority of windows score ~0, and a list that
    opens on thousands of zeroes buries the handful that matter. Pass
    ``min_score=0`` to see the full picture.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    stmt = (
        select(DNSClientWindow)
        .where(DNSClientWindow.window_start >= since)
        .order_by(desc(DNSClientWindow.tunnel_score), desc(DNSClientWindow.window_start))
        .limit(limit)
    )
    if client_ip:
        stmt = stmt.where(DNSClientWindow.client_ip == client_ip)
        # A per-client drilldown wants the client's whole history, not
        # just its interesting hours.
        stmt = stmt.order_by(None).order_by(desc(DNSClientWindow.window_start))
    else:
        stmt = stmt.where(DNSClientWindow.tunnel_score >= min_score)
    if not include_allowlisted:
        stmt = stmt.where(DNSClientWindow.allowlisted.is_(False))
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_row(w) for w in rows]


@router.get("/summary", response_model=ThreatSummary)
async def threat_summary(
    db: DB,
    current_user: CurrentUser,
    hours: int = Query(default=24, ge=1, le=24 * 30),
    min_score: float = Query(default=INTERESTING_SCORE, ge=0, le=100),
) -> ThreatSummary:
    since = datetime.now(UTC) - timedelta(hours=hours)

    totals = (
        await db.execute(
            select(
                func.count().label("windows"),
                func.count(func.distinct(DNSClientWindow.client_ip)).label("clients"),
                func.coalesce(func.max(DNSClientWindow.tunnel_score), 0.0).label("peak"),
            ).where(DNSClientWindow.window_start >= since)
        )
    ).one()

    suspicious = (
        await db.execute(
            select(func.count(func.distinct(DNSClientWindow.client_ip))).where(
                DNSClientWindow.window_start >= since,
                DNSClientWindow.allowlisted.is_(False),
                DNSClientWindow.tunnel_score >= min_score,
            )
        )
    ).scalar_one()

    worst = (
        await db.execute(
            select(DNSClientWindow)
            .where(
                DNSClientWindow.window_start >= since,
                DNSClientWindow.allowlisted.is_(False),
            )
            .order_by(desc(DNSClientWindow.tunnel_score))
            .limit(1)
        )
    ).scalar_one_or_none()

    return ThreatSummary(
        windows_scored=totals.windows or 0,
        clients_seen=totals.clients or 0,
        suspicious_clients=suspicious or 0,
        peak_score=float(totals.peak or 0.0),
        worst_client_ip=str(worst.client_ip) if worst else None,
        worst_client_score=worst.tunnel_score if worst else None,
        worst_client_parent=worst.top_parent if worst else None,
        since=since,
        has_data=(totals.windows or 0) > 0,
    )
