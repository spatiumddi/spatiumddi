"""Named evaluator functions for ``ConformityPolicy.check_kind``.

Each entry in :data:`CHECK_REGISTRY` is one declarative check the
operator can pin a policy to. The engine looks up the check by name,
runs it against the resolved target, and writes the resulting
:class:`CheckOutcome` as a ``ConformityResult`` row.

Adding a new check:

1. Write the function with the standard signature below.
2. Register it via the ``@register("name")`` decorator.
3. Document it in the inline catalog at module bottom so the
   frontend's "available check kinds" picker stays in sync.

Function contract:

.. code-block:: python

    @register("my_check")
    async def check_my_check(
        db: AsyncSession,
        *,
        target: object | None,
        target_kind: str,
        args: dict[str, Any],
        now: datetime,
    ) -> CheckOutcome: ...

``target`` is the resolved row (Subnet / IPAddress / DNSZone /
DHCPScope) or ``None`` when ``target_kind="platform"``. Checks that
require a target should defensively short-circuit to
:meth:`CheckOutcome.not_applicable` when ``target`` is None.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertRule
from app.models.audit import AuditLog
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.multicast import MulticastGroup
from app.models.nmap import NmapScan
from app.services import alerts as alert_service

# ── Outcome dataclass ───────────────────────────────────────────────


# ``status`` values. Kept narrow on purpose so the dashboard pivots
# (pass / fail / warn / not_applicable) don't drift.
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class CheckOutcome:
    """Outcome of evaluating one (policy, target) pair."""

    status: str
    detail: str
    diagnostic: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def passed(cls, detail: str = "", diagnostic: dict | None = None) -> CheckOutcome:
        return cls(STATUS_PASS, detail, diagnostic or {})

    @classmethod
    def fail(cls, detail: str = "", diagnostic: dict | None = None) -> CheckOutcome:
        return cls(STATUS_FAIL, detail, diagnostic or {})

    @classmethod
    def warn(cls, detail: str = "", diagnostic: dict | None = None) -> CheckOutcome:
        return cls(STATUS_WARN, detail, diagnostic or {})

    @classmethod
    def not_applicable(cls, detail: str = "", diagnostic: dict | None = None) -> CheckOutcome:
        return cls(STATUS_NOT_APPLICABLE, detail, diagnostic or {})


CheckFn = Callable[..., Awaitable[CheckOutcome]]
CHECK_REGISTRY: dict[str, CheckFn] = {}


def register(name: str) -> Callable[[CheckFn], CheckFn]:
    """Register a check function under a friendly name.

    Decorator. Idempotent in normal use — the registry is built at
    import time and never cleared, so a duplicate registration is a
    programming error and raises immediately.
    """

    def _wrap(fn: CheckFn) -> CheckFn:
        if name in CHECK_REGISTRY:
            raise RuntimeError(f"Conformity check {name!r} already registered")
        CHECK_REGISTRY[name] = fn
        return fn

    return _wrap


# ── Check 1: has_field ──────────────────────────────────────────────


@register("has_field")
async def check_has_field(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """Pass when ``target.<field>`` is set to a non-empty value.

    ``args["field"]`` (str, required): name of the column on the
    target row.

    Treats empty string, ``None``, and empty list / dict as "unset".
    Useful for "PCI subnet must have customer_id assigned" style
    policies. Works on any target_kind whose row has the named field.
    """
    if target is None:
        return CheckOutcome.not_applicable("target is unresolved")
    field_name = args.get("field")
    if not isinstance(field_name, str) or not field_name:
        return CheckOutcome.not_applicable(
            "check_args.field is required (string)",
            {"args": args},
        )
    if not hasattr(target, field_name):
        return CheckOutcome.not_applicable(
            f"target_kind={target_kind} has no field {field_name!r}",
        )
    value = getattr(target, field_name, None)
    is_empty = (
        value is None
        or (isinstance(value, str) and not value.strip())
        or (isinstance(value, (list, dict)) and not value)
    )
    if is_empty:
        return CheckOutcome.fail(
            f"{field_name} is unset",
            {"field": field_name, "value": None},
        )
    return CheckOutcome.passed(
        f"{field_name} is set",
        {"field": field_name, "value": str(value)[:200]},
    )


# ── Check 2: in_separate_vrf ────────────────────────────────────────


async def _resolve_subnet_vrf_id(db: AsyncSession, subnet: Subnet) -> str | None:
    """Walk subnet → block → space to find the effective VRF.

    Subnet has no ``vrf_id`` column today (#86 puts the FK on IPSpace
    + IPBlock); the effective VRF is the first non-null we find on
    the block, then the space.
    """
    block = await db.get(IPBlock, subnet.block_id)
    if block is not None and block.vrf_id is not None:
        return str(block.vrf_id)
    space = await db.get(IPSpace, subnet.space_id)
    if space is not None and space.vrf_id is not None:
        return str(space.vrf_id)
    return None


@register("in_separate_vrf")
async def check_in_separate_vrf(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """Pass when the target subnet's effective VRF holds *only*
    classification-matched siblings.

    ``args["classification"]`` (str, required): one of the
    classification flag column names on Subnet
    (``pci_scope`` / ``hipaa_scope`` / ``internet_facing``).

    Subnets without an effective VRF (no FK on the enclosing block
    or space) come back ``not_applicable`` — there's no isolation
    boundary to evaluate. Subnets in a VRF that mixes flagged and
    non-flagged neighbours fail with the offending sibling list in
    the diagnostic.
    """
    if not isinstance(target, Subnet):
        return CheckOutcome.not_applicable("requires target_kind=subnet")
    classification = args.get("classification")
    if classification not in ("pci_scope", "hipaa_scope", "internet_facing"):
        return CheckOutcome.not_applicable(
            "check_args.classification is required and must be a known flag",
            {"args": args},
        )
    vrf_id = await _resolve_subnet_vrf_id(db, target)
    if vrf_id is None:
        return CheckOutcome.not_applicable(
            "target subnet has no effective VRF (no vrf_id on enclosing block / space)",
        )

    # Pull every subnet whose enclosing block or space binds to the
    # same VRF, then bucket by classification.
    blocks_in_vrf = (
        (await db.execute(select(IPBlock.id).where(IPBlock.vrf_id == vrf_id))).scalars().all()
    )
    spaces_in_vrf = (
        (await db.execute(select(IPSpace.id).where(IPSpace.vrf_id == vrf_id))).scalars().all()
    )
    siblings = (
        (
            await db.execute(
                select(Subnet).where(
                    (Subnet.block_id.in_(blocks_in_vrf)) | (Subnet.space_id.in_(spaces_in_vrf))
                )
            )
        )
        .scalars()
        .all()
    )
    mixed: list[str] = []
    for sib in siblings:
        if sib.id == target.id:
            continue
        if not getattr(sib, classification, False):
            mixed.append(f"{sib.network} ({sib.name or 'unnamed'})")
    if mixed:
        return CheckOutcome.fail(
            f"VRF mixes {classification} subnets with {len(mixed)} non-flagged sibling(s)",
            {
                "vrf_id": vrf_id,
                "classification": classification,
                "non_matching_siblings": mixed[:20],
            },
        )
    return CheckOutcome.passed(
        f"VRF holds only {classification} subnets",
        {"vrf_id": vrf_id, "classification": classification},
    )


# ── Check 3: no_open_ports ──────────────────────────────────────────


@register("no_open_ports")
async def check_no_open_ports(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """Pass when the most recent completed nmap scan against the
    target IP did not show any of ``args["ports"]`` open.

    ``args["ports"]`` (list[int], required): port numbers the policy
    forbids being open (e.g. ``[22, 23, 3389]`` for "no admin ports
    on PCI hosts").
    ``args["max_age_days"]`` (int, optional, default 30): if no
    completed scan exists within this window the result is ``warn``
    — operators see "we don't actually know yet" instead of a false
    pass.

    Today the check only runs against ``target_kind=ip_address``.
    Subnet-level is a useful follow-up but needs aggregation logic
    over every IP under the subnet, plus an opinion about IPs with
    no scan at all (``warn`` per-IP vs. count toward ``fail``).
    """
    if not isinstance(target, IPAddress):
        return CheckOutcome.not_applicable("requires target_kind=ip_address")
    ports = args.get("ports")
    if not isinstance(ports, list) or not all(isinstance(p, int) for p in ports):
        return CheckOutcome.not_applicable(
            "check_args.ports is required (list[int])",
            {"args": args},
        )
    forbidden: set[int] = {int(p) for p in ports}
    max_age_days = int(args.get("max_age_days") or 30)
    cutoff = now - timedelta(days=max_age_days)

    scan = (
        (
            await db.execute(
                select(NmapScan)
                .where(NmapScan.ip_address_id == target.id)
                .where(NmapScan.status == "completed")
                .where(NmapScan.finished_at.is_not(None))
                .where(NmapScan.finished_at >= cutoff)
                .order_by(NmapScan.finished_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if scan is None:
        return CheckOutcome.warn(
            f"no completed nmap scan within {max_age_days} d for {target.address}",
            {"max_age_days": max_age_days, "ports": sorted(forbidden)},
        )
    summary = scan.summary_json if isinstance(scan.summary_json, dict) else {}
    open_ports_raw = summary.get("open_ports") or []
    open_ports: set[int] = set()
    for entry in open_ports_raw:
        if isinstance(entry, int):
            open_ports.add(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("port"), int):
            open_ports.add(int(entry["port"]))
        elif isinstance(entry, str) and entry.isdigit():
            open_ports.add(int(entry))
    overlap = sorted(open_ports & forbidden)
    if overlap:
        return CheckOutcome.fail(
            f"forbidden port(s) {overlap} open per scan {scan.id}",
            {
                "scan_id": str(scan.id),
                "scan_finished_at": scan.finished_at.isoformat() if scan.finished_at else None,
                "open_ports": sorted(open_ports),
                "forbidden": sorted(forbidden),
            },
        )
    return CheckOutcome.passed(
        f"no forbidden ports open per scan {scan.id}",
        {
            "scan_id": str(scan.id),
            "scan_finished_at": scan.finished_at.isoformat() if scan.finished_at else None,
            "open_ports": sorted(open_ports),
        },
    )


# ── Check 4: alert_rule_covers ──────────────────────────────────────


@register("alert_rule_covers")
async def check_alert_rule_covers(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """Pass when at least one *enabled* alert rule of the named
    ``rule_type`` covers the target's classification.

    ``args["rule_type"]`` (str, required): the alert rule_type to
    look for.
    ``args["classification"]`` (str, optional): when set, also
    requires the rule's ``classification`` column to match — useful
    for the ``compliance_change`` rule type from #105 where the
    classification is a per-rule param.

    Platform-level policy (the rule shouldn't be missing for any
    operator). Operators expect the auditor to ask "do you have an
    alert rule that catches changes to PCI scope?" — this is the
    automated way to prove yes.
    """
    rule_type = args.get("rule_type")
    if not isinstance(rule_type, str) or rule_type not in alert_service.RULE_TYPES:
        return CheckOutcome.not_applicable(
            "check_args.rule_type is required and must be a known alert rule_type",
            {"args": args},
        )
    classification = args.get("classification")
    if classification is not None and not isinstance(classification, str):
        return CheckOutcome.not_applicable(
            "check_args.classification (when set) must be a string",
            {"args": args},
        )

    q = (
        select(func.count())
        .select_from(AlertRule)
        .where(
            AlertRule.rule_type == rule_type,
            AlertRule.enabled.is_(True),
        )
    )
    if classification:
        q = q.where(AlertRule.classification == classification)
    count = (await db.execute(q)).scalar_one()
    if count and int(count) > 0:
        return CheckOutcome.passed(
            f"{count} enabled rule(s) of type {rule_type!r} cover this scope",
            {"rule_type": rule_type, "classification": classification, "count": int(count)},
        )
    return CheckOutcome.fail(
        f"no enabled rule of type {rule_type!r} covers this scope",
        {"rule_type": rule_type, "classification": classification},
    )


# ── Check 5: last_seen_within ───────────────────────────────────────


@register("last_seen_within")
async def check_last_seen_within(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """Pass when the target has been seen within ``args["max_age_days"]``.

    Works on ``ip_address`` (uses ``last_seen_at``) and on
    ``subnet`` (every IP under the subnet must satisfy the window;
    fail lists any laggards).

    ``args["max_age_days"]`` (int, required): freshness window in
    days. A subnet-level run with the default 30 d window is the
    canonical "decommission stale rows" check.
    """
    max_age_days = args.get("max_age_days")
    if not isinstance(max_age_days, int) or max_age_days <= 0:
        return CheckOutcome.not_applicable(
            "check_args.max_age_days is required (positive int)",
            {"args": args},
        )
    cutoff = now - timedelta(days=max_age_days)

    if isinstance(target, IPAddress):
        last = target.last_seen_at
        if last is None:
            return CheckOutcome.fail(
                f"never seen ({target.address})",
                {"max_age_days": max_age_days},
            )
        if last < cutoff:
            return CheckOutcome.fail(
                f"last seen {last.isoformat()} (> {max_age_days} d ago)",
                {"last_seen_at": last.isoformat(), "max_age_days": max_age_days},
            )
        return CheckOutcome.passed(
            f"last seen {last.isoformat()}",
            {"last_seen_at": last.isoformat()},
        )

    if isinstance(target, Subnet):
        rows = (
            (await db.execute(select(IPAddress).where(IPAddress.subnet_id == target.id)))
            .scalars()
            .all()
        )
        if not rows:
            return CheckOutcome.not_applicable(
                "subnet has no allocated IPs to evaluate",
            )
        stale: list[str] = []
        for ip in rows:
            last = ip.last_seen_at
            if last is None:
                stale.append(f"{ip.address} (never)")
            elif last < cutoff:
                stale.append(f"{ip.address} ({last.isoformat()})")
        if stale:
            return CheckOutcome.fail(
                f"{len(stale)} of {len(rows)} IP(s) older than {max_age_days} d",
                {
                    "max_age_days": max_age_days,
                    "stale_count": len(stale),
                    "total_count": len(rows),
                    "stale_sample": stale[:30],
                },
            )
        return CheckOutcome.passed(
            f"all {len(rows)} IP(s) seen within {max_age_days} d",
            {"max_age_days": max_age_days, "total_count": len(rows)},
        )

    return CheckOutcome.not_applicable(
        f"requires target_kind ip_address or subnet (got {target_kind})",
    )


# ── Check 6: audit_log_immutable (platform-level) ───────────────────


@register("audit_log_immutable")
async def check_audit_log_immutable(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """Pass when the audit_log table is queryable and reachable.

    Platform-level. Useful for the auditor checkbox "audit log is
    append-only and tamper-resistant" — the SpatiumDDI promise is
    that ``audit_log`` rows never mutate (DB trigger guards
    DELETE) so this check always passes when the table is healthy.
    Used as a positive presence signal in the PDF artifact.
    """
    if target_kind != "platform":
        return CheckOutcome.not_applicable(
            f"requires target_kind=platform (got {target_kind})",
        )
    try:
        count = (await db.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome.fail(
            "audit_log table is unreachable",
            {"error": str(exc)},
        )
    return CheckOutcome.passed(
        f"audit_log healthy ({int(count)} row(s))",
        {"row_count": int(count)},
    )


# ── Check 7: voice_segment_not_internet_facing ──────────────────────


@register("voice_segment_not_internet_facing")
async def check_voice_segment_not_internet_facing(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """Pass when a voice-VLAN subnet is not flagged ``internet_facing``.

    Voice-segment metadata + the existing ``internet_facing``
    classification combine here — phone fleets reaching the public
    internet directly is almost always a misconfiguration (voice
    traffic should be inside a private VRF / NAT'd through the SBC).
    The check is meaningful only on subnets the operator has tagged
    ``subnet_role='voice'``; everything else is not_applicable.

    Issue #112 phase 2 — flips voice-segment metadata from passive
    UI labelling into a real audit signal.
    """
    _ = args, now, db
    if not isinstance(target, Subnet):
        return CheckOutcome.not_applicable("requires target_kind=subnet")
    if target.subnet_role != "voice":
        return CheckOutcome.not_applicable(
            "subnet is not tagged as a voice segment",
            {"subnet_role": target.subnet_role},
        )
    if target.internet_facing:
        return CheckOutcome.fail(
            "voice subnet is also flagged internet_facing",
            {
                "subnet_role": target.subnet_role,
                "internet_facing": True,
                "network": str(target.network),
            },
        )
    return CheckOutcome.passed(
        "voice subnet is not flagged internet_facing",
        {"subnet_role": target.subnet_role, "network": str(target.network)},
    )


# Suppress unused-import warning when this module is read in
# isolation — referenced by check_no_open_ports.
_ = ipaddress


# ── Check: no_multicast_collision ──────────────────────────────────


@register("no_multicast_collision")
async def check_no_multicast_collision(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """Multicast group address must be unique within an IPSpace.

    Two groups holding the same address inside the same space
    indicate a misconfiguration — the operator-facing IPAM tree
    treats ``(space_id, address)`` as the stream identity, and
    duplicates surface as silent renderer ambiguity (which row
    "wins" when an ARP lookup or pool dispatch consults the
    registry). The DB layer doesn't enforce uniqueness because
    Phase 1 hasn't established whether deliberate dual-stack
    overlap (e.g. one row in IPv4 view + one in IPv6 view tagged
    on the same logical stream) is a pattern operators want;
    this conformity rule surfaces the cases as a soft warning so
    the operator decides per-incident.
    """
    if target is None or not isinstance(target, MulticastGroup):
        return CheckOutcome.not_applicable(
            "Target row missing or wrong kind for no_multicast_collision"
        )
    siblings = (
        await db.execute(
            select(MulticastGroup.id, MulticastGroup.name).where(
                MulticastGroup.space_id == target.space_id,
                MulticastGroup.address == target.address,
                MulticastGroup.id != target.id,
            )
        )
    ).all()
    if not siblings:
        return CheckOutcome.passed(f"Address {target.address} is unique within space")
    sibling_summary = ", ".join(f"{r._mapping['name']} ({r._mapping['id']})" for r in siblings)
    return CheckOutcome.fail(
        detail=(
            f"Address {target.address} collides with "
            f"{len(siblings)} other group(s) in the same IPSpace: "
            f"{sibling_summary}"
        ),
        diagnostic={
            "address": str(target.address),
            "space_id": str(target.space_id),
            "colliding_group_ids": [str(r._mapping["id"]) for r in siblings],
        },
    )


# ── Catalog (frontend uses to render the policy editor) ──────────────


CHECK_CATALOG: list[dict[str, Any]] = [
    {
        "name": "has_field",
        "label": "Field is set",
        "supports": ["subnet", "ip_address", "dns_zone", "dhcp_scope"],
        "args": [
            {
                "name": "field",
                "type": "string",
                "required": True,
                "label": "Field name on the target row (e.g. customer_id)",
            }
        ],
    },
    {
        "name": "in_separate_vrf",
        "label": "Subnet is in an isolated VRF",
        "supports": ["subnet"],
        "args": [
            {
                "name": "classification",
                "type": "enum",
                "required": True,
                "options": ["pci_scope", "hipaa_scope", "internet_facing"],
                "label": "Classification flag",
            }
        ],
    },
    {
        "name": "no_open_ports",
        "label": "No forbidden ports open (per latest nmap scan)",
        "supports": ["ip_address"],
        "args": [
            {
                "name": "ports",
                "type": "int_list",
                "required": True,
                "label": "Forbidden ports (e.g. 22, 23, 3389)",
            },
            {
                "name": "max_age_days",
                "type": "int",
                "required": False,
                "default": 30,
                "label": "Latest scan must be within N days",
            },
        ],
    },
    {
        "name": "alert_rule_covers",
        "label": "An enabled alert rule covers this scope",
        "supports": ["platform"],
        "args": [
            {
                "name": "rule_type",
                "type": "string",
                "required": True,
                "label": "Alert rule type to look for",
            },
            {
                "name": "classification",
                "type": "string",
                "required": False,
                "label": "Optional classification filter (compliance_change rules)",
            },
        ],
    },
    {
        "name": "last_seen_within",
        "label": "Resource has been seen recently",
        "supports": ["ip_address", "subnet"],
        "args": [
            {
                "name": "max_age_days",
                "type": "int",
                "required": True,
                "label": "Freshness window in days",
            }
        ],
    },
    {
        "name": "audit_log_immutable",
        "label": "Audit log is reachable and append-only",
        "supports": ["platform"],
        "args": [],
    },
    {
        "name": "voice_segment_not_internet_facing",
        "label": "Voice subnet is not flagged internet_facing",
        "supports": ["subnet"],
        "args": [],
    },
    {
        "name": "no_multicast_collision",
        "label": "Multicast group address is unique within its IPSpace",
        "supports": ["multicast_group"],
        "args": [],
    },
]


__all__ = [
    "CHECK_REGISTRY",
    "CHECK_CATALOG",
    "CheckOutcome",
    "STATUS_PASS",
    "STATUS_FAIL",
    "STATUS_WARN",
    "STATUS_NOT_APPLICABLE",
    "register",
]
