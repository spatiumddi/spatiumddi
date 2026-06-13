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

Agent-perspective dispatch (this PR): every *reachability* tool (ping /
traceroute / dig / port-test / tls-cert) accepts an optional ``target``.
``target`` omitted or ``kind="server"`` runs inline on the api container
exactly as before (``ran_from="server"``). ``kind="appliance"`` resolves
the Fleet appliance row, re-validates server-side, and dispatches the
already-validated job to the supervisor over the outbound poll channel
(``ran_from="appliance:<name>"``). whois / mac-vendor / dns-propagation
stay server-only and reject a non-server target.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ValidationError

from app.api.deps import DB, CurrentUser
from app.api.v1.dns_tools import (
    DEFAULT_RESOLVERS,
    PropagationCheckResult,
    _query_one,
)
from app.api.v1.tools.schemas import (
    CommandResult,
    DigRequest,
    FirewallLogsRequest,
    FirewallLogsResult,
    HostRequest,
    MacVendorEntry,
    MacVendorRequest,
    MacVendorResult,
    NetToolTarget,
    PortTestRequest,
    PortTestResult,
    PropagationRequest,
    TlsCertRequest,
    TlsCertResult,
    WhoisRequest,
)
from app.core.permissions import require_permission
from app.models.appliance import Appliance
from app.models.audit import AuditLog
from app.models.settings import PlatformSettings
from app.services.appliance import agent_cmd
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


# ── agent-perspective dispatch ──────────────────────────────────────


async def _dispatch_to_appliance[ResultT: BaseModel](
    *,
    tool: str,
    params: dict[str, Any],
    request_model: type[BaseModel],
    result_model: type[ResultT],
    target: NetToolTarget,
    db: DB,
    current_user: CurrentUser,
    allowed: frozenset[str] = agent_cmd.REACHABILITY_TOOLS,
) -> ResultT:
    """Run a reachability tool FROM a Fleet appliance's vantage.

    Steps (in order — each is a deliberate guard):

    a. The tool must be in the reachability set. (Routing already
       guarantees this — only reachability endpoints call us — but we
       re-check so a future caller can't dispatch a server-only tool.)
       → 400.
    b. Resolve the ``Appliance`` row. Unknown id → 404.
    c. RE-VALIDATE the request server-side through the SAME Pydantic
       schema the endpoint used (which re-runs ``assert_target_allowed``
       on every network-reaching field). We NEVER trust the supervisor
       as the sole SSRF check — the validated params are what we ship.
    d. Enqueue + await. Offline → 503; timeout → 504; supervisor error
       → 502.

    Every appliance-targeted run is audit-logged (non-negotiable #4)
    with the tool, the target appliance id+name, and the target host.
    """
    # (a) dispatch gate — reachability tools by default; callers pass an
    # explicit allow-set for appliance-diagnostic tools (e.g. firewall_logs).
    if tool not in allowed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Tool {tool!r} cannot be run from an appliance vantage.",
        )
    if target.id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "target.id is required when target.kind is 'appliance'.",
        )

    # (b) resolve the appliance row.
    appliance = await db.get(Appliance, target.id)
    if appliance is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")

    # (c) re-validate the params server-side through the same schema.
    # The request that reached this handler was already validated by
    # FastAPI, but rebuilding it from the dict we ship to the supervisor
    # makes the SSRF/argv guards the single source of truth for what
    # actually crosses the wire — defence in depth, never trust the
    # agent to be the only check.
    try:
        validated = request_model.model_validate(params)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    # Strip the routing-only ``target`` before shipping — the supervisor
    # always runs locally; a nested target would be meaningless there.
    wire_params = validated.model_dump(mode="json", exclude={"target"})

    target_host = str(params.get("host") or params.get("name") or "")
    ready = agent_cmd.appliance_ready(
        state=appliance.state,
        last_seen_at=appliance.last_seen_at,
    )

    # Audit before dispatch — the run is the auditable event regardless
    # of outcome (the result's success/failure is the tool's, not the
    # authorization decision's).
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="tools.run_from_appliance",
            resource_type="appliance",
            resource_id=str(appliance.id),
            resource_display=appliance.hostname,
            result="success",
            new_value={"tool": tool, "target_host": target_host},
        )
    )
    await db.commit()

    # (d) enqueue + await, mapping transport states to HTTP.
    try:
        outcome = await agent_cmd.enqueue_command(
            appliance.id,
            tool,
            wire_params,
            ready=ready,
            timeout=30.0,
        )
    except agent_cmd.ApplianceOffline as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Appliance {appliance.hostname!r} is offline or not approved.",
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            f"Appliance {appliance.hostname!r} did not return a result in time.",
        ) from exc

    if outcome.error is not None or outcome.result is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Appliance {appliance.hostname!r} could not run {tool}: "
            f"{outcome.error or 'no result returned'}",
        )

    result = result_model.model_validate(outcome.result)
    # Stamp the vantage label so the UI can show where it ran.
    return result.model_copy(update={"ran_from": f"appliance:{appliance.hostname}"})


# ── subprocess tools (default budget) ───────────────────────────────


def _server_target(target: NetToolTarget | None) -> bool:
    """True when the request runs on the api container (the default /
    back-compatible path): no target, or an explicit ``kind="server"``.
    """
    return target is None or target.kind == "server"


def _reject_non_server(tool: str, target: NetToolTarget | None) -> None:
    """Guard for server-only tools that share a request model carrying a
    ``target`` field (mtr shares HostRequest). Reject any non-server
    target with a 400 — these tools have no per-vantage meaning."""
    if not _server_target(target):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Tool {tool!r} can only run from the server vantage.",
        )


@router.post("/ping", response_model=CommandResult, dependencies=[_RequirePerm])
async def ping(
    body: HostRequest, db: DB, current_user: CurrentUser, _rl=RateLimitDefault
) -> CommandResult:
    if not _server_target(body.target):
        assert body.target is not None  # narrowed by _server_target
        return await _dispatch_to_appliance(
            tool="ping",
            params=body.model_dump(mode="json"),
            request_model=HostRequest,
            result_model=CommandResult,
            target=body.target,
            db=db,
            current_user=current_user,
        )
    try:
        return await run_ping(body.host)
    except NetToolArgError as exc:
        raise _arg_error(exc) from exc


@router.post("/traceroute", response_model=CommandResult, dependencies=[_RequirePerm])
async def traceroute(
    body: HostRequest, db: DB, current_user: CurrentUser, _rl=RateLimitDefault
) -> CommandResult:
    if not _server_target(body.target):
        assert body.target is not None
        return await _dispatch_to_appliance(
            tool="traceroute",
            params=body.model_dump(mode="json"),
            request_model=HostRequest,
            result_model=CommandResult,
            target=body.target,
            db=db,
            current_user=current_user,
        )
    try:
        return await run_traceroute(body.host)
    except NetToolArgError as exc:
        raise _arg_error(exc) from exc


@router.post("/mtr", response_model=CommandResult, dependencies=[_RequirePerm])
async def mtr(body: HostRequest, _rl=RateLimitDefault) -> CommandResult:
    # mtr is intentionally NOT in the appliance reachability set (it
    # needs CAP_NET_RAW and the per-vantage value is covered by
    # ping/traceroute). Reject a non-server target rather than silently
    # running on the server.
    _reject_non_server("mtr", body.target)
    try:
        return await run_mtr(body.host)
    except NetToolArgError as exc:
        raise _arg_error(exc) from exc


@router.post("/dig", response_model=CommandResult, dependencies=[_RequirePerm])
async def dig(
    body: DigRequest, db: DB, current_user: CurrentUser, _rl=RateLimitDefault
) -> CommandResult:
    if not _server_target(body.target):
        assert body.target is not None
        return await _dispatch_to_appliance(
            tool="dig",
            params=body.model_dump(mode="json"),
            request_model=DigRequest,
            result_model=CommandResult,
            target=body.target,
            db=db,
            current_user=current_user,
        )
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
async def port_test(
    body: PortTestRequest, db: DB, current_user: CurrentUser, _rl=RateLimitDefault
) -> PortTestResult:
    if not _server_target(body.target):
        assert body.target is not None
        return await _dispatch_to_appliance(
            tool="port-test",
            params=body.model_dump(mode="json"),
            request_model=PortTestRequest,
            result_model=PortTestResult,
            target=body.target,
            db=db,
            current_user=current_user,
        )
    return await test_port(body.host, body.port, body.protocol, body.timeout_seconds)


@router.post("/tls-cert", response_model=TlsCertResult, dependencies=[_RequirePerm])
async def tls_cert(
    body: TlsCertRequest, db: DB, current_user: CurrentUser, _rl=RateLimitDefault
) -> TlsCertResult:
    if not _server_target(body.target):
        assert body.target is not None
        return await _dispatch_to_appliance(
            tool="tls-cert",
            params=body.model_dump(mode="json"),
            request_model=TlsCertRequest,
            result_model=TlsCertResult,
            target=body.target,
            db=db,
            current_user=current_user,
        )
    return await inspect_tls_cert(body.host, body.port, body.server_name, body.timeout_seconds)


# ── firewall logs (appliance-diagnostic, #404) ──────────────────────


@router.post("/firewall-logs", response_model=FirewallLogsResult, dependencies=[_RequirePerm])
async def firewall_logs(
    body: FirewallLogsRequest, db: DB, current_user: CurrentUser, _rl=RateLimitDefault
) -> FirewallLogsResult:
    """Tail an appliance's nftables drop logs (#404).

    Always runs from an appliance vantage — the api container can't read host
    kernel logs, so a server target is rejected. Dispatched over the same
    supervisor poll/reply channel the reachability tools use, so it works for
    the local control-plane appliance AND remote fleet appliances. The UI polls
    this with the returned ``cursor`` for a near-realtime tail.
    """
    if _server_target(body.target):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "firewall-logs requires an appliance target — the control plane "
            "can't read host kernel logs itself.",
        )
    assert body.target is not None
    return await _dispatch_to_appliance(
        tool="firewall_logs",
        params=body.model_dump(mode="json"),
        request_model=FirewallLogsRequest,
        result_model=FirewallLogsResult,
        target=body.target,
        db=db,
        current_user=current_user,
        allowed=frozenset({"firewall_logs"}),
    )


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
