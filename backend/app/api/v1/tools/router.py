"""Built-in network tools HTTP surface (issue #58).

A stateless, synchronous network-utilities surface — one POST per tool.
Unlike the nmap scanner (persisted scans + SSE streaming), these run
inline and return the result in the response body.

Every endpoint:

* Gates on the ``use_network_tools`` permission (non-negotiable #3 —
  server-side authz independent of the UI). Superadmin always bypasses.
* Carries a per-user Redis rate-limit dependency. On-prem tools use the
  ``default`` budget; the off-prem tools (whois, DNS propagation) use
  the tighter ``offprem`` budget.

The router itself is module-gated at the ``include_router`` site in
``app/api/v1/router.py`` via ``require_module("tools.network")`` (404s
the whole surface when the feature module is off).

Server-perspective only. The frontend renders a disabled "Run from"
selector placeholder for the deferred agent-perspective work.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import DB
from app.api.v1.dns_tools import (
    DEFAULT_RESOLVERS,
    PropagationCheckResult,
    _query_one,
)
from app.api.v1.tools.schemas import (
    CommandResult,
    DigRequest,
    HostRequest,
    MacVendorEntry,
    MacVendorRequest,
    MacVendorResult,
    PortTestRequest,
    PortTestResult,
    PropagationRequest,
    TlsCertRequest,
    TlsCertResult,
    WhoisRequest,
)
from app.core.permissions import require_permission
from app.models.settings import PlatformSettings
from app.services.nettools import (
    inspect_tls_cert,
    run_dig,
    run_mtr,
    run_ping,
    run_traceroute,
    run_whois,
    test_port,
)
from app.services.nettools.runner import NetToolArgError
from app.services.nettools.throttle import RateLimitDefault, RateLimitOffprem
from app.services.oui import bulk_lookup_vendors, is_voip_phone_vendor, normalize_mac_key

# The resource_type the whole surface gates on. Granting
# ``{action: admin/read, resource_type: use_network_tools}`` to a group's
# role unlocks the tools page. Superadmin bypasses.
PERMISSION = "use_network_tools"

router = APIRouter(tags=["tools"])

_RequirePerm = Depends(require_permission("read", PERMISSION))


def _arg_error(exc: NetToolArgError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


# ── subprocess tools (default budget) ───────────────────────────────


@router.post("/ping", response_model=CommandResult, dependencies=[_RequirePerm])
async def ping(body: HostRequest, _rl=RateLimitDefault) -> CommandResult:
    try:
        return await run_ping(body.host)
    except NetToolArgError as exc:
        raise _arg_error(exc) from exc


@router.post("/traceroute", response_model=CommandResult, dependencies=[_RequirePerm])
async def traceroute(body: HostRequest, _rl=RateLimitDefault) -> CommandResult:
    try:
        return await run_traceroute(body.host)
    except NetToolArgError as exc:
        raise _arg_error(exc) from exc


@router.post("/mtr", response_model=CommandResult, dependencies=[_RequirePerm])
async def mtr(body: HostRequest, _rl=RateLimitDefault) -> CommandResult:
    try:
        return await run_mtr(body.host)
    except NetToolArgError as exc:
        raise _arg_error(exc) from exc


@router.post("/dig", response_model=CommandResult, dependencies=[_RequirePerm])
async def dig(body: DigRequest, _rl=RateLimitDefault) -> CommandResult:
    try:
        return await run_dig(body.name, body.record_type, body.server)
    except NetToolArgError as exc:
        raise _arg_error(exc) from exc


# ── off-prem subprocess tool (tighter budget) ───────────────────────


@router.post("/whois", response_model=CommandResult, dependencies=[_RequirePerm])
async def whois(body: WhoisRequest, _rl=RateLimitOffprem) -> CommandResult:
    try:
        return await run_whois(body.query)
    except NetToolArgError as exc:
        raise _arg_error(exc) from exc


# ── socket tools (default budget) ───────────────────────────────────


@router.post("/port-test", response_model=PortTestResult, dependencies=[_RequirePerm])
async def port_test(body: PortTestRequest, _rl=RateLimitDefault) -> PortTestResult:
    return await test_port(body.host, body.port, body.protocol, body.timeout_seconds)


@router.post("/tls-cert", response_model=TlsCertResult, dependencies=[_RequirePerm])
async def tls_cert(body: TlsCertRequest, _rl=RateLimitDefault) -> TlsCertResult:
    return await inspect_tls_cert(body.host, body.port, body.server_name, body.timeout_seconds)


# ── DNS propagation (reuses the dns_tools helper; off-prem budget) ──


@router.post("/dns-propagation", response_model=PropagationCheckResult, dependencies=[_RequirePerm])
async def dns_propagation(body: PropagationRequest, _rl=RateLimitOffprem) -> PropagationCheckResult:
    """Query a record across several public resolvers in parallel.

    Reuses ``dns_tools._query_one`` + ``DEFAULT_RESOLVERS`` so the
    behaviour exactly matches the DNS-zone propagation check — this is
    the same tool surfaced from the network-tools page.
    """
    targets: list[tuple[str, str | None]]
    if body.resolvers:
        targets = [(ip, None) for ip in body.resolvers]
    else:
        targets = [(r["address"], r["name"]) for r in DEFAULT_RESOLVERS]

    results = await asyncio.gather(
        *[
            _query_one(ip, name, body.name, body.record_type, body.timeout_seconds)
            for ip, name in targets
        ]
    )
    return PropagationCheckResult(
        name=body.name,
        record_type=body.record_type,
        queried_at_ms=int(time.time() * 1000),
        results=list(results),
    )


# ── MAC vendor lookup (reuses services/oui; default budget) ─────────


@router.post("/mac-vendor", response_model=MacVendorResult, dependencies=[_RequirePerm])
async def mac_vendor(body: MacVendorRequest, db: DB, _rl=RateLimitDefault) -> MacVendorResult:
    """Resolve OUI vendor names for a batch of MACs.

    Surfaces the ``oui_lookup_enabled`` short-circuit explicitly via
    ``oui_enabled`` so the UI can render "OUI lookup is disabled — enable
    it in Settings → IPAM" instead of empty vendor cells.
    """
    ps = await db.get(PlatformSettings, 1)
    oui_enabled = bool(ps and ps.oui_lookup_enabled)

    vendors = await bulk_lookup_vendors(db, list(body.macs))  # {} when disabled
    entries: list[MacVendorEntry] = []
    for raw in body.macs:
        key = normalize_mac_key(raw)
        name = vendors.get(key) if key else None
        entries.append(
            MacVendorEntry(
                mac=raw,
                vendor=name,
                is_voip_phone=is_voip_phone_vendor(name),
            )
        )
    return MacVendorResult(oui_enabled=oui_enabled, entries=entries)
