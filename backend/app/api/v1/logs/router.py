"""Logs API — Windows Event Log reads over WinRM.

Two read endpoints + a source-discovery endpoint. No writes, no
retention (events live on the Windows side). This is the first slice
of a broader logging surface: future additions include agent logs,
audit-log streaming, and the SpatiumDDI service logs themselves.

Endpoints:
  ``GET  /logs/sources`` — lists every server that supports log pulls
  (Windows DNS + DHCP with WinRM credentials set) along with each
  driver's ``available_log_names`` for the source picker.

  ``POST /logs/query`` — runs ``Get-WinEvent -FilterHashtable`` on
  the named server and returns a neutral list of event rows.

Authorisation:
  ``read`` on ``server`` — aligns with the permission required to
  view DNS / DHCP server details. Superadmin bypass applies per the
  standard RBAC path.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DB
from app.core.permissions import require_permission
from app.drivers.dhcp import AGENTLESS_DRIVERS as DHCP_AGENTLESS
from app.drivers.dhcp import get_driver as get_dhcp_driver
from app.drivers.dns import AGENTLESS_DRIVERS as DNS_AGENTLESS
from app.drivers.dns import get_driver as get_dns_driver
from app.models.dhcp import DHCPServer
from app.models.dns import DNSServer

logger = structlog.get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_permission("read", "server"))])


# ── Schemas ──────────────────────────────────────────────────────────────────


class LogNameOption(BaseModel):
    """One available event log on a given server."""

    name: str
    display: str


class LogSource(BaseModel):
    """A server that supports log pulls + its available log names."""

    server_id: uuid.UUID
    server_name: str
    server_kind: Literal["dns", "dhcp"]
    driver: str
    host: str
    logs: list[LogNameOption]


class LogEventRow(BaseModel):
    """One event row returned by ``Get-WinEvent``."""

    time: str
    id: int
    level: str
    provider: str
    machine: str
    message: str


class LogQueryRequest(BaseModel):
    """POST body for ``/logs/query``."""

    server_id: uuid.UUID
    server_kind: Literal["dns", "dhcp"]
    log_name: str
    max_events: int = Field(default=100, ge=1, le=500)
    level: int | None = Field(default=None, ge=1, le=5)
    since: datetime | None = None
    event_id: int | None = None


class LogQueryResponse(BaseModel):
    server_id: uuid.UUID
    server_kind: Literal["dns", "dhcp"]
    log_name: str
    events: list[LogEventRow]
    truncated: bool  # True when result count == max_events (more may exist)


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/sources", response_model=list[LogSource])
async def list_sources(db: DB) -> list[LogSource]:
    """List every server we can pull logs from.

    Today that's any Windows DNS / DHCP server with WinRM credentials
    configured. Callers render this as a "pick a server" dropdown in
    the Logs UI. When new log sources land (agents, control-plane),
    they get a ``server_kind`` of their own.
    """
    out: list[LogSource] = []

    # DNS — iterate all servers using an agentless driver with creds.
    dns_rows = (
        await db.execute(
            DNSServer.__table__.select().where(DNSServer.credentials_encrypted.is_not(None))
        )
    ).all()
    for row in dns_rows:
        s = row._mapping
        if s["driver"] not in DNS_AGENTLESS:
            continue
        try:
            driver = get_dns_driver(s["driver"])
        except ValueError:
            continue
        log_fn = getattr(driver, "available_log_names", None)
        if not callable(log_fn):
            continue
        logs = [LogNameOption(name=n, display=d) for n, d in log_fn()]
        out.append(
            LogSource(
                server_id=s["id"],
                server_name=s["name"],
                server_kind="dns",
                driver=s["driver"],
                host=s["host"],
                logs=logs,
            )
        )

    # DHCP — same pattern.
    dhcp_rows = (
        await db.execute(
            DHCPServer.__table__.select().where(DHCPServer.credentials_encrypted.is_not(None))
        )
    ).all()
    for row in dhcp_rows:
        s = row._mapping
        if s["driver"] not in DHCP_AGENTLESS:
            continue
        try:
            driver = get_dhcp_driver(s["driver"])
        except ValueError:
            continue
        log_fn = getattr(driver, "available_log_names", None)
        if not callable(log_fn):
            continue
        logs = [LogNameOption(name=n, display=d) for n, d in log_fn()]
        out.append(
            LogSource(
                server_id=s["id"],
                server_name=s["name"],
                server_kind="dhcp",
                driver=s["driver"],
                host=s["host"],
                logs=logs,
            )
        )

    out.sort(key=lambda s: (s.server_kind, s.server_name.lower()))
    return out


async def _resolve_server(db: DB, server_kind: str, server_id: uuid.UUID) -> tuple[Any, str]:
    """Load the right server row for the requested kind, or 404.

    Returns ``(server, driver_name)``.
    """
    server: Any
    allowed: frozenset[str]
    if server_kind == "dns":
        server = await db.get(DNSServer, server_id)
        allowed = DNS_AGENTLESS
    else:  # dhcp
        server = await db.get(DHCPServer, server_id)
        allowed = DHCP_AGENTLESS
    if server is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{server_kind.upper()} server not found",
        )
    if server.driver not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Driver {server.driver!r} doesn't support log pulls; only "
                f"agentless drivers ({', '.join(sorted(allowed))}) do."
            ),
        )
    if not getattr(server, "credentials_encrypted", None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Server has no WinRM credentials configured — can't pull logs "
                "without them. Add credentials on the server edit form first."
            ),
        )
    return server, server.driver


@router.post("/query", response_model=LogQueryResponse)
async def query_logs(body: LogQueryRequest, db: DB) -> LogQueryResponse:
    """Run a filtered ``Get-WinEvent`` against the named server and return
    matching rows.

    The driver validates that the requested log actually exists for
    that server; on Windows server variants that don't have a given
    log enabled, ``Get-WinEvent`` returns an empty list rather than
    raising — so the UI sees "no events" instead of a 500.
    """
    server, driver_name = await _resolve_server(db, body.server_kind, body.server_id)

    if body.server_kind == "dns":
        driver = get_dns_driver(driver_name)
    else:
        driver = get_dhcp_driver(driver_name)

    get_events_fn = getattr(driver, "get_events", None)
    if not callable(get_events_fn):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Driver {driver_name!r} does not support log pulls.",
        )

    try:
        raw_events = await get_events_fn(
            server,
            log_name=body.log_name,
            max_events=body.max_events,
            level=body.level,
            since=body.since,
            event_id=body.event_id,
        )
    except ValueError as exc:
        # Driver surfaces credential / config errors as ValueError.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        # WinRM transport / PowerShell errors. 502 — upstream failed, not us.
        logger.warning(
            "logs_query_upstream_failed",
            server_id=str(body.server_id),
            kind=body.server_kind,
            log_name=body.log_name,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Log query failed on {server.host}: {exc}",
        )

    events = [LogEventRow(**e) for e in raw_events]
    return LogQueryResponse(
        server_id=body.server_id,
        server_kind=body.server_kind,
        log_name=body.log_name,
        events=events,
        truncated=len(events) >= body.max_events,
    )
