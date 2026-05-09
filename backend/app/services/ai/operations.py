"""Copilot write operations — preview / apply pattern.

A write tool the LLM fires never executes directly. Instead, the
``propose_*`` tool calls the operation's :func:`preview` (read-only)
and persists an ``ai_operation_proposal`` row. The chat surface
renders the proposal as an Apply / Discard card; the actual mutation
runs only after an explicit POST to ``/api/v1/ai/proposals/{id}/apply``.

This module owns the registry of operations + their preview / apply
implementations. The ``propose_*`` tools live in
``services/ai/tools/`` and import :func:`get_operation` to do the
preview + persist dance; the API router lives in
``api/v1/ai/proposals.py`` and imports the same registry to do the
apply / discard dance.

CLAUDE.md non-negotiables that apply here:
* #4 (audit everything) — apply functions MUST go through the
  service layer paths that already audit, OR write their own audit
  row before commit. The proposal row itself is *not* a substitute
  for an audit-log row.
* #2 (async throughout) — preview + apply are both async; they
  receive the calling user's DB session and User row.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.ipam import IPAddress, Subnet
from app.services.nmap import NmapArgError, build_argv

# Per-proposal TTL. 30 minutes is a generous window for a thoughtful
# review without keeping yesterday's proposals lying around — the
# cleanup task drops expired+unapplied rows on the next sweep.
PROPOSAL_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class Operation:
    """One write operation. ``preview`` produces the human-readable
    description (no side effects); ``apply`` performs the mutation
    and returns a JSON-serialisable result.

    The third argument on the callables is typed ``Any`` rather than
    ``BaseModel`` so concrete operations can declare their args
    Pydantic subclass directly (mypy treats function args as
    contravariant — a function that requires
    ``CreateIPAddressArgs`` is not assignable where ``BaseModel`` is
    expected). The registry validates against ``args_model`` on
    dispatch, so the contract is preserved at runtime. Mirrors how
    ``ToolExecutor`` in ``tools/base.py`` solves the same shape.
    """

    name: str
    description: str
    args_model: type[BaseModel]
    preview: Callable[[AsyncSession, User, Any], Awaitable[PreviewResult]]
    apply: Callable[[AsyncSession, User, Any], Awaitable[dict[str, Any]]]
    # Free-form category for grouping in the admin UI — same vocabulary
    # as the read-only tool registry ("ipam", "dns", "dhcp").
    category: str = "ops"


@dataclass(frozen=True)
class PreviewResult:
    """Outcome of an operation's :func:`preview` step.

    ``ok=False`` means the preview itself rejected the args (e.g.
    subnet doesn't exist, address out of range) — surface ``detail``
    to the operator and don't even create a proposal row. ``ok=True``
    proceeds to persist the proposal with ``preview_text``.
    """

    ok: bool
    detail: str
    preview_text: str = ""


_OPERATIONS: dict[str, Operation] = {}


def register(op: Operation) -> None:
    if op.name in _OPERATIONS:
        raise ValueError(f"Operation {op.name!r} already registered")
    _OPERATIONS[op.name] = op


def get_operation(name: str) -> Operation | None:
    return _OPERATIONS.get(name)


def all_operations() -> list[Operation]:
    return sorted(_OPERATIONS.values(), key=lambda o: o.name)


def expires_at_default() -> datetime:
    """Stamp every new proposal with ``now + PROPOSAL_TTL``."""
    return datetime.now(UTC) + PROPOSAL_TTL


# ── create_ip_address operation (issue #90 Phase 2 first write tool) ─────────


class CreateIPAddressArgs(BaseModel):
    """Args for the ``create_ip_address`` operation."""

    subnet_id: str = Field(description="UUID of the subnet to create the address in")
    address: str = Field(description="The IP address as a string (e.g. 10.0.5.10)")
    status: str = Field(
        default="allocated",
        description=(
            "IP status: 'allocated' (default), 'reserved', or "
            "'static_dhcp'. Static_dhcp requires mac_address."
        ),
    )
    hostname: str | None = Field(default=None, description="Hostname (e.g. web01)")
    fqdn: str | None = Field(
        default=None, description="Fully-qualified domain name (e.g. web01.prod.example.com)"
    )
    mac_address: str | None = Field(default=None, description="MAC address in any standard format")
    description: str = Field(default="", description="Free-form description")


async def _preview_create_ip_address(
    db: AsyncSession, user: User, args: CreateIPAddressArgs
) -> PreviewResult:
    # Resolve the subnet so the preview can name it.
    subnet = await db.get(Subnet, args.subnet_id)
    if subnet is None:
        return PreviewResult(ok=False, detail=f"Subnet {args.subnet_id} not found")

    try:
        addr_obj = ipaddress.ip_address(args.address)
    except ValueError:
        return PreviewResult(ok=False, detail=f"Invalid IP address: {args.address!r}")

    try:
        net = ipaddress.ip_network(str(subnet.network), strict=False)
    except ValueError:
        return PreviewResult(ok=False, detail=f"Subnet network {subnet.network!r} is unparseable")
    if addr_obj not in net:
        return PreviewResult(
            ok=False,
            detail=(
                f"Address {args.address} is not within subnet {subnet.network} "
                f"({subnet.name or 'unnamed'})"
            ),
        )

    # Check for existing allocation — a non-blocking cue that apply
    # will likely 409. The preview deliberately doesn't reject; the
    # operator might be replacing a stale row.
    existing = (
        await db.execute(
            select(IPAddress).where(
                IPAddress.subnet_id == subnet.id,
                IPAddress.address == args.address,
            )
        )
    ).scalar_one_or_none()
    suffix = ""
    if existing is not None:
        suffix = (
            f" — note: address is already recorded with status "
            f"{existing.status!r}; apply will fail unless you delete it first"
        )

    parts = [
        f"Create IP {args.address}",
        f"in subnet {subnet.network}{f' ({subnet.name})' if subnet.name else ''}",
        f"status={args.status}",
    ]
    if args.hostname:
        parts.append(f"hostname={args.hostname}")
    if args.fqdn:
        parts.append(f"fqdn={args.fqdn}")
    if args.mac_address:
        parts.append(f"mac={args.mac_address}")
    if args.description:
        # Truncate to keep the preview readable.
        d = args.description if len(args.description) < 80 else args.description[:77] + "..."
        parts.append(f"desc={d!r}")
    return PreviewResult(ok=True, detail="ready", preview_text=", ".join(parts) + suffix)


async def _apply_create_ip_address(
    db: AsyncSession, user: User, args: CreateIPAddressArgs
) -> dict[str, Any]:
    """Re-validate at apply time + insert the row.

    Mirrors the conflict checks from the IPAM router's create_address
    handler. We don't import that handler directly (it's bound to a
    FastAPI request shape) — duplicating the few-line conflict check
    is the simpler alternative until the apply set grows.
    """
    from app.api.v1.dhcp._audit import write_audit  # local import to avoid cycle

    subnet = await db.get(Subnet, args.subnet_id)
    if subnet is None:
        raise ValueError(f"Subnet {args.subnet_id} not found")

    try:
        addr_obj = ipaddress.ip_address(args.address)
    except ValueError as exc:
        raise ValueError(f"Invalid IP address: {args.address!r}") from exc

    net = ipaddress.ip_network(str(subnet.network), strict=False)
    if addr_obj not in net:
        raise ValueError(f"Address {args.address} is not within subnet {subnet.network}")

    # Re-check for existing allocation under the apply transaction.
    existing = (
        await db.execute(
            select(IPAddress).where(
                IPAddress.subnet_id == subnet.id,
                IPAddress.address == args.address,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(
            f"Address {args.address} is already allocated in subnet {subnet.network} "
            f"(status={existing.status})"
        )

    if args.status == "static_dhcp" and not args.mac_address:
        raise ValueError("mac_address is required when status is 'static_dhcp'")

    row = IPAddress(
        subnet_id=subnet.id,
        address=args.address,
        status=args.status,
        hostname=args.hostname,
        fqdn=args.fqdn,
        mac_address=args.mac_address,
        description=args.description or "",
    )
    db.add(row)
    await db.flush()

    # Audit the apply path so the audit log captures the outcome (the
    # propose step doesn't audit — proposals can be discarded). The
    # event ties the AI to the mutation via the user_display_name +
    # action="ai_apply".
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="ipam.ip_address",
        resource_id=str(row.id),
        resource_display=str(args.address),
        new_value={
            "subnet_id": str(subnet.id),
            "subnet": str(subnet.network),
            "address": args.address,
            "status": args.status,
            "hostname": args.hostname,
            "via": "ai_proposal",
        },
    )
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "address": args.address,
        "subnet_id": str(subnet.id),
        "status": args.status,
        "hostname": args.hostname,
    }


# ── run_nmap_scan operation ────────────────────────────────────────────


class RunNmapScanArgs(BaseModel):
    """Args for the ``run_nmap_scan`` operation.

    Mirrors :class:`app.api.v1.nmap.schemas.NmapScanCreate` but typed
    looser (``preset`` as plain str so the LLM can supply any of the
    documented presets without a Literal-of-Literals headache for
    JSON-Schema generation in older clients).
    """

    target_ip: str = Field(
        description=(
            "IP address, hostname, or CIDR to scan. CIDR scans use the "
            "``subnet_sweep`` preset by default and are capped on the "
            "backend at /16 worth of hosts."
        ),
    )
    preset: str = Field(
        default="quick",
        description=(
            "Nmap preset: quick | service_version | service_and_os | "
            "os_fingerprint | subnet_sweep | default_scripts | "
            "udp_top1000 | aggressive | custom. ``service_and_os`` is "
            "the right pick for device profiling. ``subnet_sweep`` "
            "(-sn) for ping-sweep across a CIDR. Stick to ``quick`` "
            "or ``service_version`` for routine port checks."
        ),
    )
    port_spec: str | None = Field(
        default=None,
        description="Optional ``-p`` value (e.g. '22,80,443' or 'T:1-1024').",
    )
    extra_args: str | None = Field(
        default=None,
        description=(
            "Optional extra nmap flags. Validated server-side — " "dangerous flags are rejected."
        ),
    )


async def _preview_run_nmap_scan(
    db: AsyncSession, user: User, args: RunNmapScanArgs
) -> PreviewResult:
    target = (args.target_ip or "").strip()
    if not target:
        return PreviewResult(ok=False, detail="target_ip is required")

    try:
        argv = build_argv(target, args.preset, args.port_spec, args.extra_args)
    except NmapArgError as exc:
        return PreviewResult(
            ok=False,
            detail=f"nmap arg validation failed: {exc}",
        )

    parts = [
        f"Run nmap **{args.preset}** scan against `{target}`",
    ]
    if args.port_spec:
        parts.append(f"ports={args.port_spec}")
    if args.extra_args:
        parts.append(f"extra={args.extra_args!r}")
    parts.append(f"argv: `{' '.join(argv)}`")
    parts.append(
        "This will issue real network probes from the SpatiumDDI host. "
        "Apply only if you're authorised to scan this target."
    )
    return PreviewResult(ok=True, detail="ready", preview_text="\n".join(parts))


async def _apply_run_nmap_scan(
    db: AsyncSession, user: User, args: RunNmapScanArgs
) -> dict[str, Any]:
    """Persist a queued nmap_scan row + dispatch the Celery task.

    Mirrors the create_scan handler in
    :mod:`app.api.v1.nmap.router` but skips the ip_address_id branch
    (the AI surface always passes ``target_ip``). Audit row uses the
    same ``resource_type='nmap_scan'`` shape.
    """
    from app.models.audit import AuditLog  # local import to avoid cycle
    from app.models.nmap import NmapScan  # local import to avoid cycle

    target = args.target_ip.strip()
    # Re-validate at apply time too — argv builder gates dangerous flags.
    try:
        build_argv(target, args.preset, args.port_spec, args.extra_args)
    except NmapArgError as exc:
        raise ValueError(f"nmap arg validation failed: {exc}") from exc

    scan = NmapScan(
        target_ip=target,
        preset=args.preset,
        port_spec=args.port_spec,
        extra_args=args.extra_args,
        status="queued",
        created_by_user_id=user.id,
    )
    db.add(scan)
    await db.flush()
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=getattr(user, "auth_source", "local") or "local",
            action="create",
            resource_type="nmap_scan",
            resource_id=str(scan.id),
            resource_display=f"nmap:{target}",
            new_value={
                "preset": args.preset,
                "port_spec": args.port_spec,
                "extra_args": args.extra_args,
                "target_ip": target,
                "via": "ai_proposal",
            },
        )
    )
    await db.commit()
    await db.refresh(scan)

    # Dispatch — broker outage shouldn't fail the apply (mirror
    # router behaviour). The row is queued; operator can re-trigger.
    try:
        from app.tasks.nmap import run_scan_task  # noqa: PLC0415

        run_scan_task.delay(str(scan.id))
    except Exception:  # noqa: BLE001 — broker down
        pass

    return {
        "id": str(scan.id),
        "target_ip": target,
        "preset": args.preset,
        "status": "queued",
        "hint": (
            "Scan dispatched. Poll get_nmap_scan_results until "
            "status == 'completed' to read the open ports / OS guess."
        ),
    }


register(
    Operation(
        name="run_nmap_scan",
        description=(
            "Trigger an on-demand nmap scan. Always go through "
            "propose_run_nmap_scan — never call this directly. The "
            "scan touches the network, so operator approval is "
            "required before each apply."
        ),
        args_model=RunNmapScanArgs,
        preview=_preview_run_nmap_scan,
        apply=_apply_run_nmap_scan,
        category="network",
    )
)


register(
    Operation(
        name="create_ip_address",
        description=(
            "Allocate a new IP address inside a subnet. Use this when "
            "the operator asks you to create / allocate / assign an "
            "IP. Pass subnet_id (UUID), address, status, and optional "
            "hostname / fqdn / mac_address / description. Always go "
            "through propose_create_ip_address — never call this "
            "directly without an explicit operator approval step."
        ),
        args_model=CreateIPAddressArgs,
        preview=_preview_create_ip_address,
        apply=_apply_create_ip_address,
        category="ipam",
    )
)


# ── Tier 5 (issue #101) — DNS record / DHCP static / alert rule / chat archive ──
#
# Each operation follows the same preview / apply / register pattern
# as ``create_ip_address`` above. ``create_subnet`` was deliberately
# deferred — subnet creation has too many edge cases (auto-allocate
# network/broadcast rows, parent-block overlap checks, allocation
# policy) to ship without a dedicated design pass.


# ── create_dns_record ─────────────────────────────────────────────────


_DNS_RECORD_TYPES = {
    "A",
    "AAAA",
    "CNAME",
    "MX",
    "TXT",
    "NS",
    "PTR",
    "SRV",
    "CAA",
    "TLSA",
    "SSHFP",
    "NAPTR",
    "LOC",
}


class CreateDNSRecordArgs(BaseModel):
    """Args for the ``create_dns_record`` operation."""

    zone_id: str = Field(description="UUID of the parent DNS zone.")
    name: str = Field(
        description=(
            "Relative record name. Use ``@`` for the zone apex. Do NOT "
            "include the trailing zone (use ``host1`` not "
            "``host1.example.com``)."
        )
    )
    record_type: str = Field(
        description=(
            "Record type — A / AAAA / CNAME / MX / TXT / NS / PTR / "
            "SRV / CAA / TLSA / SSHFP / NAPTR / LOC."
        )
    )
    value: str = Field(
        description=(
            "Right-hand-side value. For A/AAAA an IP; for CNAME / NS "
            "a target FQDN; for TXT the quoted text; for MX the "
            "target host (priority is a separate arg)."
        )
    )
    ttl: int | None = Field(
        default=None,
        description=(
            "Override TTL in seconds. None inherits the zone's "
            "default. Range 60 – 604800 when supplied."
        ),
        ge=60,
        le=604_800,
    )
    priority: int | None = Field(
        default=None,
        description="MX/SRV priority. Required for MX records.",
        ge=0,
        le=65_535,
    )


async def _preview_create_dns_record(
    db: AsyncSession, user: User, args: CreateDNSRecordArgs
) -> PreviewResult:
    from app.models.dns import DNSRecord, DNSZone  # local import — avoid cycle

    rtype = args.record_type.strip().upper()
    if rtype not in _DNS_RECORD_TYPES:
        return PreviewResult(ok=False, detail=f"Unsupported record type {rtype!r}.")
    if rtype == "MX" and args.priority is None:
        return PreviewResult(ok=False, detail="MX records require a priority.")

    zone = await db.get(DNSZone, args.zone_id)
    if zone is None:
        return PreviewResult(ok=False, detail=f"Zone {args.zone_id} not found.")
    if getattr(zone, "deleted_at", None) is not None:
        return PreviewResult(ok=False, detail=f"Zone {args.zone_id} is deleted.")

    name = args.name.strip()
    if not name:
        return PreviewResult(ok=False, detail="name is required (use ``@`` for apex).")

    # Surface a heads-up if a row with the same (zone, name, type, value)
    # already exists; preview doesn't reject — operator may want a parallel
    # row (e.g. multiple A records for round-robin).
    existing = (
        await db.execute(
            select(DNSRecord).where(
                DNSRecord.zone_id == zone.id,
                DNSRecord.name == name,
                DNSRecord.record_type == rtype,
                DNSRecord.value == args.value,
                DNSRecord.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    suffix = " — note: an identical record already exists" if existing else ""

    parts = [
        f"Create **{rtype}** record `{name}` in zone `{zone.name}`",
        f"value=`{args.value}`",
    ]
    if args.ttl is not None:
        parts.append(f"ttl={args.ttl}")
    if args.priority is not None:
        parts.append(f"priority={args.priority}")
    return PreviewResult(ok=True, detail="ready", preview_text=", ".join(parts) + suffix)


async def _apply_create_dns_record(
    db: AsyncSession, user: User, args: CreateDNSRecordArgs
) -> dict[str, Any]:
    from app.api.v1.dhcp._audit import write_audit  # local import to avoid cycle
    from app.models.dns import DNSRecord, DNSZone

    rtype = args.record_type.strip().upper()
    if rtype not in _DNS_RECORD_TYPES:
        raise ValueError(f"Unsupported record type {rtype!r}.")
    if rtype == "MX" and args.priority is None:
        raise ValueError("MX records require a priority.")

    zone = await db.get(DNSZone, args.zone_id)
    if zone is None:
        raise ValueError(f"Zone {args.zone_id} not found.")

    name = args.name.strip()
    fqdn = (
        zone.name
        if name in ("@", "")
        else f"{name}.{zone.name}".rstrip(".") + ("." if zone.name.endswith(".") else "")
    )

    row = DNSRecord(
        zone_id=zone.id,
        name=name,
        fqdn=fqdn,
        record_type=rtype,
        value=args.value,
        ttl=args.ttl,
        priority=args.priority,
        created_by_user_id=user.id,
    )
    db.add(row)
    await db.flush()

    write_audit(
        db,
        user=user,
        action="create",
        resource_type="dns.record",
        resource_id=str(row.id),
        resource_display=f"{name} {rtype} {args.value}",
        new_value={
            "zone_id": str(zone.id),
            "zone": zone.name,
            "name": name,
            "type": rtype,
            "value": args.value,
            "ttl": args.ttl,
            "priority": args.priority,
            "via": "ai_proposal",
        },
    )
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "zone_id": str(zone.id),
        "fqdn": fqdn,
        "record_type": rtype,
        "value": args.value,
    }


register(
    Operation(
        name="create_dns_record",
        description=(
            "Create a DNS resource record inside a zone. Always route "
            "via propose_create_dns_record — DNS edits propagate to "
            "live servers, so operator approval per-apply is required."
        ),
        args_model=CreateDNSRecordArgs,
        preview=_preview_create_dns_record,
        apply=_apply_create_dns_record,
        category="dns",
    )
)


# ── create_dns_zone (issue #127 Phase 4e) ─────────────────────────────


_DNS_DRIVER_HINTS = {"bind9", "powerdns", "windows_dns"}
# DNSSEC online signing + ALIAS + LUA records require the PowerDNS
# driver — the preview rejects when ``dnssec_enabled=true`` lands in a
# group whose servers don't include any PowerDNS member, since signing
# would fail at apply time and confuse the operator.
_POWERDNS_ONLY_FEATURES = ("dnssec_enabled",)


class CreateDNSZoneArgs(BaseModel):
    """Args for the ``create_dns_zone`` operation.

    ``driver_hint`` (issue #127 Phase 4e) lets the model express the
    operator's intent — "I need DNSSEC online signing, so this zone
    has to land on a PowerDNS group" — without forcing it to know the
    exact group UUID. When supplied, the preview either:

    * uses ``driver_hint`` to select a matching group when
      ``group_id`` is omitted, OR
    * cross-checks ``driver_hint`` against an explicit ``group_id``
      and rejects on driver mismatch (e.g. operator picked a BIND9
      group but asked for ``driver_hint="powerdns"``).
    """

    name: str = Field(
        description=(
            "Zone name (FQDN). Trailing dot is added automatically if "
            "not present (e.g. ``example.com`` becomes ``example.com.``)."
        )
    )
    group_id: str | None = Field(
        default=None,
        description=(
            "UUID of the DNS server group that should own this zone. "
            "Optional — when omitted, ``driver_hint`` (if supplied) "
            "selects a matching group automatically; if neither is "
            "supplied the preview returns the available groups so the "
            "operator can pick."
        ),
    )
    driver_hint: str | None = Field(
        default=None,
        description=(
            "Preferred backend driver — one of ``bind9``, "
            "``powerdns``, or ``windows_dns``. Required for "
            "``dnssec_enabled=true`` (only PowerDNS supports online "
            "signing). When ``group_id`` is set, this is validated "
            "against the group's actual driver mix."
        ),
    )
    zone_type: str = Field(
        default="primary",
        description="Zone type — ``primary``, ``secondary``, ``forward``, or ``stub``.",
    )
    kind: str = Field(
        default="forward",
        description="``forward`` (a normal name → record zone) or ``reverse`` (PTR zone).",
    )
    primary_ns: str = Field(
        default="",
        description="Primary nameserver FQDN (e.g. ``ns1.example.com.``). Recommended.",
    )
    admin_email: str = Field(
        default="",
        description="Zone admin email rendered into SOA RNAME (e.g. ``hostmaster@example.com``).",
    )
    dnssec_enabled: bool = Field(
        default=False,
        description=(
            "Turn on DNSSEC for this zone. Requires a PowerDNS group "
            "(BIND9 + Windows DNS don't support online signing here). "
            'Pair with ``driver_hint="powerdns"`` to auto-select a '
            "compatible group."
        ),
    )
    ttl: int = Field(
        default=3600,
        description="Default record TTL in seconds.",
        ge=60,
        le=604_800,
    )


def _normalize_zone_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        return ""
    return name if name.endswith(".") else name + "."


async def _resolve_group_for_zone(
    db: AsyncSession,
    *,
    group_id: str | None,
    driver_hint: str | None,
):
    """Return ``(group, drivers_set, error_text)``. On success
    ``error_text`` is empty; on failure ``group`` is ``None`` and
    ``error_text`` is the operator-facing reason.
    """
    from app.models.dns import DNSServer, DNSServerGroup  # noqa: PLC0415

    # Fetch every group + the distinct driver set so the preview can
    # pick by hint or validate explicit selections in one round trip.
    rows = (
        await db.execute(
            select(DNSServerGroup, DNSServer.driver)
            .outerjoin(DNSServer, DNSServer.group_id == DNSServerGroup.id)
            .order_by(DNSServerGroup.name)
        )
    ).all()
    by_id: dict[str, tuple[Any, set[str]]] = {}
    for grp, drv in rows:
        slot = by_id.setdefault(str(grp.id), (grp, set()))
        if drv:
            slot[1].add(drv)

    if group_id:
        slot = by_id.get(str(group_id))
        if slot is None:
            return None, set(), f"DNS server group {group_id!r} not found."
        grp, drivers = slot
        if driver_hint and drivers and driver_hint not in drivers:
            return (
                None,
                drivers,
                (
                    f"Group {grp.name!r} has drivers {sorted(drivers)} "
                    f"which doesn't include the requested "
                    f"driver_hint={driver_hint!r}. Pick a different "
                    f"group or drop the hint."
                ),
            )
        return grp, drivers, ""

    if driver_hint:
        candidates = [(grp, drivers) for grp, drivers in by_id.values() if driver_hint in drivers]
        if not candidates:
            available = sorted({d for _, ds in by_id.values() for d in ds})
            return (
                None,
                set(),
                (
                    f"No DNS server group has any {driver_hint!r} "
                    f"member. Available drivers: {available or '(none)'}."
                ),
            )
        # Tie-break by name so picks are deterministic.
        candidates.sort(key=lambda gd: gd[0].name)
        grp, drivers = candidates[0]
        return grp, drivers, ""

    # Neither group_id nor hint — surface the inventory so the LLM can
    # ask the operator for a pick instead of guessing.
    listing = ", ".join(
        f"{grp.name} ({sorted(ds) or ['no servers']})" for grp, ds in by_id.values()
    )
    return (
        None,
        set(),
        (
            "group_id or driver_hint is required. Available groups: "
            f"{listing or '(none configured)'}."
        ),
    )


async def _preview_create_dns_zone(
    db: AsyncSession, user: User, args: CreateDNSZoneArgs
) -> PreviewResult:
    from app.api.v1.dns.router import VALID_ZONE_TYPES  # noqa: PLC0415
    from app.models.dns import DNSZone  # noqa: PLC0415

    name = _normalize_zone_name(args.name)
    if not name:
        return PreviewResult(ok=False, detail="Zone name is required.")

    if args.zone_type not in VALID_ZONE_TYPES:
        return PreviewResult(
            ok=False, detail=f"zone_type must be one of {sorted(VALID_ZONE_TYPES)}."
        )

    if args.driver_hint is not None and args.driver_hint not in _DNS_DRIVER_HINTS:
        return PreviewResult(
            ok=False,
            detail=f"driver_hint must be one of {sorted(_DNS_DRIVER_HINTS)}.",
        )

    grp, drivers, err = await _resolve_group_for_zone(
        db, group_id=args.group_id, driver_hint=args.driver_hint
    )
    if grp is None:
        return PreviewResult(ok=False, detail=err)

    # PowerDNS-only features (currently DNSSEC) — reject if the
    # selected group has no PowerDNS member. Without this guard the
    # apply would land the row but the agent would refuse to sign.
    for feat in _POWERDNS_ONLY_FEATURES:
        if getattr(args, feat) and "powerdns" not in drivers:
            return PreviewResult(
                ok=False,
                detail=(
                    f"{feat}=true requires a PowerDNS-driver server in "
                    f"the group, but {grp.name!r} has drivers "
                    f"{sorted(drivers) or ['(none)']}. Add a PowerDNS "
                    f"server to the group, pick a different group, or "
                    f'set driver_hint="powerdns" without group_id.'
                ),
            )

    existing = (
        await db.execute(
            select(DNSZone).where(
                DNSZone.group_id == grp.id,
                DNSZone.view_id.is_(None),
                DNSZone.name == name,
                DNSZone.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return PreviewResult(
            ok=False,
            detail=f"A zone named {name!r} already exists in group {grp.name!r}.",
        )

    parts = [f"Create zone `{name}` in group `{grp.name}`"]
    parts.append(f"drivers={sorted(drivers) or ['(none)']}")
    parts.append(f"type={args.zone_type}/{args.kind}")
    if args.dnssec_enabled:
        parts.append("DNSSEC=on (PowerDNS online signing)")
    return PreviewResult(ok=True, detail="ready", preview_text=", ".join(parts))


async def _apply_create_dns_zone(
    db: AsyncSession, user: User, args: CreateDNSZoneArgs
) -> dict[str, Any]:
    from app.models.audit import AuditLog  # noqa: PLC0415
    from app.models.dns import DNSZone  # noqa: PLC0415

    name = _normalize_zone_name(args.name)
    grp, _drivers, err = await _resolve_group_for_zone(
        db, group_id=args.group_id, driver_hint=args.driver_hint
    )
    if grp is None:
        raise ValueError(err)

    zone = DNSZone(
        group_id=grp.id,
        name=name,
        zone_type=args.zone_type,
        kind=args.kind,
        ttl=args.ttl,
        primary_ns=args.primary_ns,
        admin_email=args.admin_email,
        dnssec_enabled=args.dnssec_enabled,
    )
    db.add(zone)
    await db.flush()

    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="create",
            resource_type="dns_zone",
            resource_id=str(zone.id),
            resource_display=zone.name,
            result="success",
            new_value={
                "group_id": str(grp.id),
                "group": grp.name,
                "zone_type": args.zone_type,
                "kind": args.kind,
                "dnssec_enabled": args.dnssec_enabled,
                "driver_hint": args.driver_hint,
                "via": "ai_proposal",
            },
        )
    )
    await db.commit()
    await db.refresh(zone)
    return {
        "id": str(zone.id),
        "group_id": str(grp.id),
        "name": name,
        "zone_type": args.zone_type,
        "kind": args.kind,
        "dnssec_enabled": args.dnssec_enabled,
    }


register(
    Operation(
        name="create_dns_zone",
        description=(
            "Create a new DNS zone. Honors driver_hint to route the "
            "zone onto a PowerDNS / BIND9 / Windows DNS group; "
            "DNSSEC zones require a PowerDNS group."
        ),
        args_model=CreateDNSZoneArgs,
        preview=_preview_create_dns_zone,
        apply=_apply_create_dns_zone,
        category="dns",
    )
)


# ── create_dhcp_static ────────────────────────────────────────────────


class CreateDHCPStaticArgs(BaseModel):
    """Args for the ``create_dhcp_static`` operation."""

    scope_id: str = Field(description="UUID of the parent DHCP scope.")
    ip_address: str = Field(description="IP to reserve (must fall inside the scope).")
    mac_address: str = Field(description="MAC address (any standard format).")
    hostname: str | None = Field(default=None, description="Optional hostname.")
    description: str = Field(default="", description="Free-form description.")


async def _preview_create_dhcp_static(
    db: AsyncSession, user: User, args: CreateDHCPStaticArgs
) -> PreviewResult:
    from app.models.dhcp import DHCPScope, DHCPStaticAssignment

    scope = await db.get(DHCPScope, args.scope_id)
    if scope is None:
        return PreviewResult(ok=False, detail=f"DHCP scope {args.scope_id} not found.")

    try:
        addr_obj = ipaddress.ip_address(args.ip_address)
    except ValueError:
        return PreviewResult(ok=False, detail=f"Invalid IP {args.ip_address!r}.")

    try:
        net = ipaddress.ip_network(str(scope.subnet), strict=False)
    except ValueError:
        return PreviewResult(ok=False, detail=f"Scope subnet {scope.subnet!r} is unparseable.")
    if addr_obj not in net:
        return PreviewResult(
            ok=False,
            detail=(f"IP {args.ip_address} is outside scope subnet {scope.subnet}."),
        )

    # Conflict probe — do NOT reject in preview; surface as a hint so
    # the operator can decide whether to abort or replace.
    existing = (
        await db.execute(
            select(DHCPStaticAssignment).where(
                DHCPStaticAssignment.scope_id == scope.id,
                or_(
                    DHCPStaticAssignment.ip_address == args.ip_address,
                    DHCPStaticAssignment.mac_address == args.mac_address.lower(),
                ),
            )
        )
    ).scalar_one_or_none()
    suffix = ""
    if existing is not None:
        suffix = " — note: a static for this IP or MAC already exists; apply will fail"

    parts = [
        f"Create DHCP static reservation in scope `{scope.name or scope.subnet}`",
        f"ip={args.ip_address}",
        f"mac={args.mac_address}",
    ]
    if args.hostname:
        parts.append(f"hostname={args.hostname}")
    if args.description:
        d = args.description if len(args.description) < 80 else args.description[:77] + "..."
        parts.append(f"desc={d!r}")
    return PreviewResult(ok=True, detail="ready", preview_text=", ".join(parts) + suffix)


async def _apply_create_dhcp_static(
    db: AsyncSession, user: User, args: CreateDHCPStaticArgs
) -> dict[str, Any]:
    from app.api.v1.dhcp._audit import write_audit
    from app.models.dhcp import DHCPScope, DHCPStaticAssignment

    scope = await db.get(DHCPScope, args.scope_id)
    if scope is None:
        raise ValueError(f"DHCP scope {args.scope_id} not found.")

    addr_obj = ipaddress.ip_address(args.ip_address)
    net = ipaddress.ip_network(str(scope.subnet), strict=False)
    if addr_obj not in net:
        raise ValueError(f"IP {args.ip_address} is outside scope subnet {scope.subnet}.")

    row = DHCPStaticAssignment(
        scope_id=scope.id,
        ip_address=args.ip_address,
        mac_address=args.mac_address.lower(),
        hostname=args.hostname or "",
        description=args.description or "",
        created_by_user_id=user.id,
    )
    db.add(row)
    await db.flush()

    write_audit(
        db,
        user=user,
        action="create",
        resource_type="dhcp.static_assignment",
        resource_id=str(row.id),
        resource_display=f"{args.ip_address} ({args.mac_address})",
        new_value={
            "scope_id": str(scope.id),
            "ip_address": args.ip_address,
            "mac_address": args.mac_address.lower(),
            "hostname": args.hostname,
            "via": "ai_proposal",
        },
    )
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "scope_id": str(scope.id),
        "ip_address": args.ip_address,
        "mac_address": args.mac_address.lower(),
    }


register(
    Operation(
        name="create_dhcp_static",
        description=(
            "Create a DHCP static reservation (MAC → IP) inside a "
            "scope. Always route via propose_create_dhcp_static — the "
            "reservation propagates to the Kea / Windows DHCP backend "
            "on apply, so operator approval is required."
        ),
        args_model=CreateDHCPStaticArgs,
        preview=_preview_create_dhcp_static,
        apply=_apply_create_dhcp_static,
        category="dhcp",
    )
)


# ── create_alert_rule ─────────────────────────────────────────────────
#
# Scoped to the simplest rule_type — ``subnet_utilization`` — so the
# tool is useful out of the box. Operators authoring the more complex
# ``compliance_change`` / ``domain_*`` types can keep doing it via the
# Alerts UI; we'd add per-rule_type proposers if the operator demand
# materialises.


class CreateAlertRuleArgs(BaseModel):
    """Args for the ``create_alert_rule`` operation (subnet_utilization)."""

    name: str = Field(description="Human-readable rule name.")
    threshold_percent: int = Field(
        description=(
            "Subnet utilization percent at which the rule fires. "
            "Range 1 – 100. Typical values: 80 (warning), 95 "
            "(critical)."
        ),
        ge=1,
        le=100,
    )
    severity: Literal["info", "warning", "critical"] = Field(
        default="warning",
        description="Alert severity assigned to events fired by this rule.",
    )
    description: str = Field(default="", description="Free-form description.")


async def _preview_create_alert_rule(
    db: AsyncSession, user: User, args: CreateAlertRuleArgs
) -> PreviewResult:
    parts = [
        f"Create alert rule **{args.name}**",
        "type=subnet_utilization",
        f"threshold={args.threshold_percent}%",
        f"severity={args.severity}",
    ]
    return PreviewResult(ok=True, detail="ready", preview_text=", ".join(parts))


async def _apply_create_alert_rule(
    db: AsyncSession, user: User, args: CreateAlertRuleArgs
) -> dict[str, Any]:
    from app.api.v1.dhcp._audit import write_audit
    from app.models.alerts import AlertRule

    row = AlertRule(
        name=args.name,
        description=args.description,
        rule_type="subnet_utilization",
        severity=args.severity,
        threshold_percent=args.threshold_percent,
        enabled=True,
    )
    db.add(row)
    await db.flush()

    write_audit(
        db,
        user=user,
        action="create",
        resource_type="alert.rule",
        resource_id=str(row.id),
        resource_display=args.name,
        new_value={
            "name": args.name,
            "rule_type": "subnet_utilization",
            "threshold_percent": args.threshold_percent,
            "severity": args.severity,
            "via": "ai_proposal",
        },
    )
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "name": args.name,
        "rule_type": "subnet_utilization",
        "threshold_percent": args.threshold_percent,
        "severity": args.severity,
    }


register(
    Operation(
        name="create_alert_rule",
        description=(
            "Create a subnet-utilization alert rule. Always route via "
            "propose_create_alert_rule. Other rule_type values (domain "
            "expiring, compliance_change, …) keep their UI authoring "
            "path; this proposer is scoped to the simplest case."
        ),
        args_model=CreateAlertRuleArgs,
        preview=_preview_create_alert_rule,
        apply=_apply_create_alert_rule,
        category="ops",
    )
)


# ── archive_session ───────────────────────────────────────────────────
#
# Quality-of-life write — sets ``AIChatSession.archived_at = now()`` so
# the session disappears from the History panel's default view without
# being permanently deleted. Restorable via the unarchive flow on the
# History panel.


class ArchiveSessionArgs(BaseModel):
    """Args for the ``archive_session`` operation."""

    session_id: str = Field(description="UUID of the AI chat session to archive.")


async def _preview_archive_session(
    db: AsyncSession, user: User, args: ArchiveSessionArgs
) -> PreviewResult:
    from app.models.ai import AIChatSession

    sess = await db.get(AIChatSession, args.session_id)
    if sess is None:
        return PreviewResult(ok=False, detail=f"Session {args.session_id} not found.")
    if sess.user_id != user.id:
        return PreviewResult(ok=False, detail="You can only archive your own chat sessions.")
    if sess.archived_at is not None:
        return PreviewResult(ok=False, detail=f"Session {args.session_id} is already archived.")
    label = sess.name or "Untitled"
    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=f"Archive chat session **{label}** (id `{sess.id}`)",
    )


async def _apply_archive_session(
    db: AsyncSession, user: User, args: ArchiveSessionArgs
) -> dict[str, Any]:
    from app.api.v1.dhcp._audit import write_audit
    from app.models.ai import AIChatSession

    sess = await db.get(AIChatSession, args.session_id)
    if sess is None:
        raise ValueError(f"Session {args.session_id} not found.")
    if sess.user_id != user.id:
        raise ValueError("You can only archive your own chat sessions.")
    if sess.archived_at is not None:
        raise ValueError(f"Session {args.session_id} is already archived.")
    sess.archived_at = datetime.now(UTC)
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="ai.chat_session",
        resource_id=str(sess.id),
        resource_display=sess.name or "Untitled",
        new_value={"archived_at": sess.archived_at.isoformat(), "via": "ai_proposal"},
    )
    await db.commit()
    return {"id": str(sess.id), "archived_at": sess.archived_at.isoformat()}


register(
    Operation(
        name="archive_session",
        description=(
            "Archive an AI chat session (your own only). Hides it from "
            "the default History view but keeps the data. Always route "
            "via propose_archive_session."
        ),
        args_model=ArchiveSessionArgs,
        preview=_preview_archive_session,
        apply=_apply_archive_session,
        category="ops",
    )
)


# ── create_multicast_group (issue #126 Phase 4) ─────────────────────


_IPV4_MULTICAST = ipaddress.ip_network("224.0.0.0/4")
_IPV6_MULTICAST = ipaddress.ip_network("ff00::/8")


class CreateMulticastGroupArgs(BaseModel):
    """Args for the ``create_multicast_group`` operation.

    The address must sit inside ``224.0.0.0/4`` (IPv4) or
    ``ff00::/8`` (IPv6) — same CHECK constraint the DB layer
    enforces, surfaced here for clean preview rejection.
    """

    space_id: str = Field(description="UUID of the parent IPSpace that hosts this group.")
    address: str = Field(
        description=(
            "Multicast address. IPv4 inside 224.0.0.0/4 (e.g. "
            "239.5.7.42) or IPv6 inside ff00::/8 (e.g. ff05::1:3)."
        )
    )
    name: str = Field(description="Human-friendly name (e.g. 'Cam7 Studio-B HD').")
    application: str = Field(
        default="",
        description=(
            "Free-text application label — what's flowing on the "
            "wire. Examples: 'SMPTE 2110-20 video', 'Dante audio', "
            "'AAPL options L2'."
        ),
    )
    domain_id: str | None = Field(
        default=None,
        description=(
            "Optional PIM domain UUID. When supplied, the group "
            "binds to the domain's routing context."
        ),
    )
    rtp_payload_type: int | None = Field(
        default=None, ge=0, le=127, description="RTP payload type for media flows."
    )


def _validate_multicast_address(addr: str) -> str | None:
    """Returns an error string when ``addr`` isn't inside the IANA
    multicast ranges. ``None`` on success."""
    try:
        parsed = ipaddress.ip_address(addr)
    except ValueError as exc:
        return f"Invalid IP literal: {exc}"
    if isinstance(parsed, ipaddress.IPv4Address) and parsed in _IPV4_MULTICAST:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed in _IPV6_MULTICAST:
        return None
    return (
        "Address must be inside 224.0.0.0/4 (IPv4) or ff00::/8 "
        "(IPv6) — the multicast registry only accepts addresses in "
        "those ranges."
    )


async def _preview_create_multicast_group(
    db: AsyncSession, user: User, args: CreateMulticastGroupArgs
) -> PreviewResult:
    from app.models.ipam import IPSpace  # noqa: PLC0415
    from app.models.multicast import (  # noqa: PLC0415
        MulticastDomain,
        MulticastGroup,
    )

    space = await db.get(IPSpace, args.space_id)
    if space is None:
        return PreviewResult(ok=False, detail=f"IPSpace {args.space_id!r} not found.")

    err = _validate_multicast_address(args.address)
    if err is not None:
        return PreviewResult(ok=False, detail=err)

    if args.domain_id is not None:
        if (await db.get(MulticastDomain, args.domain_id)) is None:
            return PreviewResult(ok=False, detail=f"Multicast domain {args.domain_id!r} not found.")

    # Soft-warn on duplicates (the registry doesn't enforce
    # uniqueness — the conformity check does — but a stale
    # MulticastGroup at the same address is almost always an
    # operator error).
    dup = (
        await db.execute(
            select(MulticastGroup.id, MulticastGroup.name).where(
                MulticastGroup.space_id == args.space_id,
                MulticastGroup.address == args.address,
            )
        )
    ).first()
    dup_note = ""
    if dup is not None:
        dup_note = (
            f"\n  - WARNING: address {args.address} is already used by "
            f"group {dup[1]!r} ({dup[0]}) in this space — the "
            "no_multicast_collision conformity rule will fire."
        )

    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=(
            f"Create multicast group:\n"
            f"  - address: {args.address}\n"
            f"  - name: {args.name}\n"
            f"  - application: {args.application or '(none)'}\n"
            f"  - space: {space.name} ({args.space_id})\n"
            f"  - domain_id: {args.domain_id or '(none)'}"
            f"{dup_note}"
        ),
    )


async def _apply_create_multicast_group(
    db: AsyncSession, user: User, args: CreateMulticastGroupArgs
) -> dict[str, Any]:
    from app.models.audit import AuditLog  # noqa: PLC0415
    from app.models.multicast import MulticastGroup  # noqa: PLC0415

    err = _validate_multicast_address(args.address)
    if err is not None:
        raise ValueError(err)

    row = MulticastGroup(
        space_id=args.space_id,
        address=args.address,
        name=args.name,
        application=args.application,
        domain_id=args.domain_id,
        rtp_payload_type=args.rtp_payload_type,
    )
    db.add(row)
    await db.flush()

    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="create",
            resource_type="multicast_group",
            resource_id=str(row.id),
            resource_display=f"{row.name} ({row.address})",
            result="success",
            new_value={
                "space_id": args.space_id,
                "address": args.address,
                "name": args.name,
                "application": args.application,
                "domain_id": args.domain_id,
                "via": "ai_proposal",
            },
        )
    )
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "address": str(row.address),
        "name": row.name,
        "application": row.application,
        "space_id": str(row.space_id),
        "domain_id": str(row.domain_id) if row.domain_id else None,
    }


register(
    Operation(
        name="create_multicast_group",
        description=(
            "Create a multicast group registry entry. Address must "
            "be inside the IANA multicast ranges; the operator can "
            "rename / re-tag from the UI after the LLM-driven create."
        ),
        args_model=CreateMulticastGroupArgs,
        preview=_preview_create_multicast_group,
        apply=_apply_create_multicast_group,
        category="multicast",
    )
)


# ── allocate_multicast_groups (issue #126 Phase 4 Wave 2) ────────────


_MULTICAST_BULK_MAX = 256


class AllocateMulticastGroupsArgs(BaseModel):
    """Args for the ``allocate_multicast_groups`` bulk-stamp
    operation. Mirrors the shape of the existing
    ``POST /multicast/groups/bulk-allocate`` endpoint so the LLM
    learns one grammar that maps cleanly onto operator muscle
    memory."""

    space_id: str = Field(description="UUID of the parent IPSpace.")
    count: int = Field(
        ge=1,
        le=_MULTICAST_BULK_MAX,
        description=(
            f"Number of contiguous addresses to stamp (1..{_MULTICAST_BULK_MAX}). "
            "The cap matches the underlying REST endpoint — multicast "
            "registries are curated, not swept."
        ),
    )
    name_template: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Name template using the standard token set: ``{n}`` (counter), "
            "``{n:03d}`` (zero-padded), ``{n:x}`` (hex), and "
            "``{oct1}``-``{oct4}`` (octets of the rendered IP). Example: "
            "``cam-{n:02d}`` -> ``cam-01``, ``cam-02``..."
        ),
    )
    start_address: str = Field(
        description=(
            "First address in the run. Must sit inside 224.0.0.0/4 "
            "(IPv4) or ff00::/8 (IPv6); the run walks forward from "
            "here and 422s if it would exit the multicast range."
        ),
    )
    template_start: int = Field(
        default=1,
        ge=0,
        description="Initial value for the ``{n}`` token (default 1).",
    )
    application: str = Field(
        default="",
        description="Free-text application label applied to every group (e.g. 'SMPTE 2110 video').",
    )
    domain_id: str | None = Field(
        default=None,
        description="Optional PIM domain UUID to bind every new group to.",
    )


async def _bulk_allocate_helper(
    db: AsyncSession, args: AllocateMulticastGroupsArgs
) -> tuple[list[Any], int, str | None]:
    """Re-runs the same candidate builder the REST bulk-allocate
    endpoint uses (``api.v1.multicast.router._build_bulk_candidates``)
    so the LLM-driven flow shares one validation path with the UI.

    Returns ``(items, conflict_count, error_message)``. ``error_message``
    is non-None when a hard validation fails (bad address class, run
    walks past the multicast range, etc) — the operation surfaces it
    as a preview rejection.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    from app.api.v1.multicast.router import (  # noqa: PLC0415
        MulticastBulkAllocateRequest,
        _build_bulk_candidates,
    )

    try:
        body = MulticastBulkAllocateRequest(
            space_id=args.space_id,  # type: ignore[arg-type]
            count=args.count,
            name_template=args.name_template,
            start_address=args.start_address,
            template_start=args.template_start,
            application=args.application,
            domain_id=args.domain_id,  # type: ignore[arg-type]
        )
    except Exception as exc:  # pydantic ValidationError or similar
        return [], 0, str(exc)

    try:
        items = await _build_bulk_candidates(db, body)
    except HTTPException as exc:
        # ``_build_bulk_candidates`` raises 422 when the run would
        # walk past the multicast range. Surface the operator-
        # friendly detail rather than the HTTP shape.
        return [], 0, str(exc.detail)

    conflicts = sum(1 for it in items if it.conflict is not None)
    return items, conflicts, None


async def _preview_allocate_multicast_groups(
    db: AsyncSession, user: User, args: AllocateMulticastGroupsArgs
) -> PreviewResult:
    from app.models.ipam import IPSpace  # noqa: PLC0415
    from app.models.multicast import MulticastDomain  # noqa: PLC0415

    if (await db.get(IPSpace, args.space_id)) is None:
        return PreviewResult(ok=False, detail=f"IPSpace {args.space_id!r} not found.")
    if args.domain_id is not None:
        if (await db.get(MulticastDomain, args.domain_id)) is None:
            return PreviewResult(
                ok=False,
                detail=f"Multicast domain {args.domain_id!r} not found.",
            )

    items, conflict_count, err = await _bulk_allocate_helper(db, args)
    if err is not None:
        return PreviewResult(ok=False, detail=err)

    if conflict_count > 0:
        # Render the colliding addresses inline so the operator sees
        # what to renumber. The chat surface clips long previews
        # gracefully; the proposal can still be applied if the LLM
        # presents it (the apply layer re-runs the candidate
        # builder + 409s cleanly).
        conflict_lines = "\n".join(f"    - {it.address} (in use)" for it in items if it.conflict)[
            :1000
        ]
        return PreviewResult(
            ok=False,
            detail=(
                f"{conflict_count} of {len(items)} addresses are already "
                f"taken — adjust ``start_address`` or ``count``:\n"
                f"{conflict_lines}"
            ),
        )

    sample = ", ".join(f"{it.address}={it.name}" for it in items[:3])
    if len(items) > 3:
        sample += f", … (+{len(items) - 3} more)"
    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=(
            f"Bulk-allocate {len(items)} multicast group(s) starting at "
            f"{args.start_address}:\n"
            f"  - template: {args.name_template} (start {args.template_start})\n"
            f"  - application: {args.application or '(none)'}\n"
            f"  - domain_id: {args.domain_id or '(none)'}\n"
            f"  - sample: {sample}"
        ),
    )


async def _apply_allocate_multicast_groups(
    db: AsyncSession, user: User, args: AllocateMulticastGroupsArgs
) -> dict[str, Any]:
    from app.models.audit import AuditLog  # noqa: PLC0415
    from app.models.multicast import MulticastGroup  # noqa: PLC0415

    items, conflict_count, err = await _bulk_allocate_helper(db, args)
    if err is not None:
        raise ValueError(err)
    if conflict_count > 0:
        raise ValueError(
            f"{conflict_count} address(es) already in use; re-run preview "
            "after adjusting start_address or count."
        )

    created_ids: list[str] = []
    for item in items:
        row = MulticastGroup(
            space_id=args.space_id,
            address=item.address,
            name=item.name,
            application=args.application,
            domain_id=args.domain_id,
        )
        db.add(row)
        await db.flush()
        created_ids.append(str(row.id))

    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="bulk_allocate",
            resource_type="multicast_group",
            resource_id=str(args.space_id),
            resource_display=(f"{len(created_ids)} group(s) starting at {args.start_address}"),
            result="success",
            new_value={
                "count": len(created_ids),
                "start_address": args.start_address,
                "name_template": args.name_template,
                "space_id": args.space_id,
                "domain_id": args.domain_id,
                "via": "ai_proposal",
            },
        )
    )
    await db.commit()
    return {
        "created": len(created_ids),
        "group_ids": created_ids,
        "start_address": args.start_address,
    }


register(
    Operation(
        name="allocate_multicast_groups",
        description=(
            "Bulk-stamp N sequential multicast groups with a name "
            "template. Capped at 256. Refuses if any candidate "
            "address already has a group in the same space; "
            "operator must re-preview after adjusting start / count."
        ),
        args_model=AllocateMulticastGroupsArgs,
        preview=_preview_allocate_multicast_groups,
        apply=_apply_allocate_multicast_groups,
        category="multicast",
    )
)
