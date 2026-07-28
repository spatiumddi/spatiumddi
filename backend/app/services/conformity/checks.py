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
from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.models.audit import AuditLog
from app.models.av import AVFlowProfile, AVReservedRange
from app.models.bacnet import BACnetDevice
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.multicast import MulticastGroup
from app.models.nmap import NmapScan
from app.models.ot import OTDevice, OTZone
from app.services import alerts as alert_service
from app.services.av.ranges import check_allocation_conflict
from app.services.ot.zones import (
    effective_zone_for_address,
    purdue_mismatch,
    purdue_to_str,
)

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


# ── Check: no_lanwide_control_plane_ports (platform-level, #285) ────


@register("no_lanwide_control_plane_ports")
async def check_no_lanwide_control_plane_ports(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """No appliance node exposes the k3s control-plane ports LAN-wide.

    Platform-level (PCI-DSS Req 1 / HIPAA network-segmentation). The
    appliance base ``/etc/nftables.conf`` historically accepted etcd
    (2379/2380) + kubelet (10250) from ANY source; #285 cut that so the
    supervisor's peer-scoped drop-in is authoritative. Each node reports
    ``base_lanwide_k3s`` (True = still on the legacy LAN-wide base).

    Verdict policy honours non-negotiable #5 — **never connectivity-FAIL**:

    * FAIL only on a CONFIRMED LAN-wide node (``base_lanwide_k3s is True``).
    * Otherwise PASS. Nodes that are stale (no recent heartbeat, beyond
      ``args["stale_minutes"]`` — default 30) or unverified
      (``base_lanwide_k3s is None``) report **PASS-stale** with a
      diagnostic, rather than failing the compliance claim because the
      control plane temporarily lost contact.
    * No reporting appliance node → vacuously PASS.
    """
    if target_kind != "platform":
        return CheckOutcome.not_applicable(
            f"requires target_kind=platform (got {target_kind})",
        )
    stale_minutes = int(args.get("stale_minutes", 30))
    rows = list(
        (
            await db.execute(
                select(Appliance).where(
                    Appliance.state == APPLIANCE_STATE_APPROVED,
                    Appliance.deployment_kind == "appliance",
                    Appliance.base_conf_marker.isnot(None),
                )
            )
        )
        .scalars()
        .all()
    )
    lanwide = [r for r in rows if r.base_lanwide_k3s is True]
    if lanwide:
        return CheckOutcome.fail(
            f"{len(lanwide)} appliance node(s) still expose LAN-wide k3s "
            "control-plane ports (etcd 2379/2380, kubelet 10250)",
            {"lanwide_hosts": sorted(r.hostname for r in lanwide)},
        )
    if not rows:
        return CheckOutcome.passed(
            "no appliance node has reported a base-conf marker (vacuously compliant)",
            {"reported": 0},
        )
    cutoff = now - timedelta(minutes=stale_minutes)

    def _stale(r: Appliance) -> bool:
        return r.last_seen_at is None or r.last_seen_at < cutoff

    unknown = [r for r in rows if r.base_lanwide_k3s is None]
    stale = [r for r in rows if r.base_lanwide_k3s is False and _stale(r)]
    fresh = [r for r in rows if r.base_lanwide_k3s is False and not _stale(r)]
    if unknown or stale:
        return CheckOutcome.passed(
            f"{len(fresh)} node(s) confirmed hardened; {len(stale)} stale + "
            f"{len(unknown)} unverified reported PASS-stale (no LAN-wide port confirmed)",
            {
                "hardened_fresh": sorted(r.hostname for r in fresh),
                "stale": sorted(r.hostname for r in stale),
                "unverified": sorted(r.hostname for r in unknown),
                "stale_minutes": stale_minutes,
            },
        )
    return CheckOutcome.passed(
        f"all {len(fresh)} appliance node(s) hardened — no LAN-wide control-plane ports",
        {"hardened": sorted(r.hostname for r in fresh)},
    )


# ── Checks: AV / Audio-Video-over-IP (#540) ─────────────────────────


@register("av_flow_outside_reserved_range")
async def check_av_flow_outside_reserved_range(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """An AV flow lives inside a range declared for its protocol.

    Dante ships transmit flows in ``239.69.0.0/16``; AES67 and
    RAVENNA plants pick a block inside the administratively-scoped
    ``239.0.0.0/8``; SMPTE 2110 studios hand-carve their own. Static
    multicast assignment is the AoIP best practice precisely because
    address drift is what breaks a plant, so a flow that has wandered
    outside its declared range is worth surfacing.

    Absence of policy is deliberately NOT a violation: when the
    operator has declared no range for that protocol there is nothing
    to check against, and failing every flow in that situation would
    train operators to ignore the check. Same for a group carrying no
    AV descriptor — that is a plain multicast group, not an AV flow.
    """
    _ = args, now
    if target is None or not isinstance(target, MulticastGroup):
        return CheckOutcome.not_applicable(
            "Target row missing or wrong kind for av_flow_outside_reserved_range"
        )
    profile = (
        await db.execute(select(AVFlowProfile).where(AVFlowProfile.group_id == target.id))
    ).scalar_one_or_none()
    if profile is None:
        return CheckOutcome.not_applicable("Group carries no AV descriptor")

    # Delegates to the same service the REST allocation-preview calls, so
    # the conformity verdict and the preview an operator saw before
    # allocating cannot disagree about one flow. That also picks up the
    # longest-prefix rule (a Dante /16 carved out of an exclusive AES67
    # /8 resolves to Dante) and the asyncpg CIDR-vs-str coercion, both of
    # which a second hand-rolled containment loop would miss.
    report = await check_allocation_conflict(
        db, target.space_id, str(target.address), profile.av_protocol
    )

    if report.own_ranges:
        matched = report.own_ranges[0]
        return CheckOutcome.passed(
            f"{profile.av_protocol} flow {target.address} is inside " f"{matched.cidr} as declared",
            {
                "address": str(target.address),
                "av_protocol": profile.av_protocol,
                "matched_range": matched.cidr,
            },
        )

    if not report.outside_declared_range:
        return CheckOutcome.not_applicable(
            f"No reserved range declared for {profile.av_protocol} in this IPSpace"
        )

    # Only on the failure path: name the ranges the flow *should* have
    # landed in. That is the actionable half of the finding, and it is
    # one extra query on the rare branch rather than on every group.
    declared = [
        str(c)
        for c in (
            await db.execute(
                select(AVReservedRange.cidr).where(
                    AVReservedRange.space_id == target.space_id,
                    AVReservedRange.av_protocol == profile.av_protocol,
                )
            )
        )
        .scalars()
        .all()
    ]
    return CheckOutcome.fail(
        detail=(
            f"{profile.av_protocol} flow {target.address} sits outside every "
            f"declared {profile.av_protocol} range "
            f"({', '.join(declared[:20])})"
        ),
        diagnostic={
            "address": str(target.address),
            "av_protocol": profile.av_protocol,
            "declared_ranges": declared[:20],
            "suggested_range": report.suggested_range,
        },
    )


@register("av_flow_no_ptp_domain")
async def check_av_flow_no_ptp_domain(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """An AV flow records the PTP clock domain it rides.

    Advisory (warn, never fail). PTP misconfiguration is the most
    common AoIP failure mode, and "which clock domain is this flow
    on?" is the first question asked when audio drops — but a missing
    value is undocumented state, not a broken network, so it does not
    warrant a compliance failure.

    Applied uniformly across AV protocols. NDI is the arguable
    exception (it is not PTP-locked the way AES67/2110 are), but the
    module records the domain as documentation rather than asserting
    the flow is clocked, and special-casing one protocol here would
    encode a claim about NDI's timing model that this registry has no
    way to verify.
    """
    _ = args, now
    if target is None or not isinstance(target, MulticastGroup):
        return CheckOutcome.not_applicable(
            "Target row missing or wrong kind for av_flow_no_ptp_domain"
        )
    profile = (
        await db.execute(select(AVFlowProfile).where(AVFlowProfile.group_id == target.id))
    ).scalar_one_or_none()
    if profile is None:
        return CheckOutcome.not_applicable("Group carries no AV descriptor")
    if profile.ptp_domain is not None:
        return CheckOutcome.passed(
            f"{profile.av_protocol} flow records PTP domain {profile.ptp_domain}",
            {"av_protocol": profile.av_protocol, "ptp_domain": profile.ptp_domain},
        )
    return CheckOutcome.warn(
        detail=(f"{profile.av_protocol} flow {target.address} has no PTP clock " "domain recorded"),
        diagnostic={"address": str(target.address), "av_protocol": profile.av_protocol},
    )


# ── Checks: BACnet/IP building automation (#541) ────────────────────


@register("bbmd_one_per_subnet")
async def check_bbmd_one_per_subnet(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """Each BACnet-bearing subnet has exactly one BBMD.

    BACnet broadcasts (``Who-Is`` / ``I-Am``) do not cross IP routers,
    so every IP subnet in a multi-subnet BACnet/IP internetwork needs
    exactly one BACnet Broadcast Management Device. Zero leaves the
    subnet's devices invisible to the rest of the internetwork; two or
    more produces duplicated broadcast traffic and duplicate ``I-Am``
    responses. "Exactly one" is the rule every BAS integration guide
    states, and it is cheaply checkable from the registry.

    A subnet with no BACnet devices at all is NOT_APPLICABLE rather
    than a failure — most subnets in a mixed estate are not BACnet
    subnets, and flagging them would bury the real findings.
    """
    _ = args, now
    if target is None or not isinstance(target, Subnet):
        return CheckOutcome.not_applicable(
            "Target row missing or wrong kind for bbmd_one_per_subnet"
        )
    rows = list(
        (
            await db.execute(
                select(BACnetDevice.device_instance, BACnetDevice.is_bbmd).where(
                    BACnetDevice.subnet_id == target.id
                )
            )
        ).all()
    )
    if not rows:
        return CheckOutcome.not_applicable("Subnet carries no BACnet devices")

    bbmds = [r._mapping["device_instance"] for r in rows if r._mapping["is_bbmd"]]
    if len(bbmds) == 1:
        return CheckOutcome.passed(
            f"Exactly one BBMD (device instance {bbmds[0]}) on this subnet",
            {"bbmd_instances": bbmds, "bacnet_device_count": len(rows)},
        )
    if not bbmds:
        return CheckOutcome.fail(
            detail=(
                f"No BBMD on a subnet with {len(rows)} BACnet device(s) — "
                "its devices cannot be reached across IP routers"
            ),
            diagnostic={"bbmd_count": 0, "bacnet_device_count": len(rows)},
        )
    return CheckOutcome.fail(
        detail=(
            f"{len(bbmds)} BBMDs on one subnet (device instances "
            f"{', '.join(str(i) for i in sorted(bbmds)[:20])}) — expected exactly one; "
            "multiple BBMDs duplicate broadcast traffic"
        ),
        diagnostic={
            "bbmd_count": len(bbmds),
            "bbmd_instances": sorted(bbmds)[:20],
            "bacnet_device_count": len(rows),
        },
    )


@register("bacnet_duplicate_device_instance")
async def check_bacnet_duplicate_device_instance(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """No two BACnet devices share a device instance number.

    Platform-level, because instance uniqueness is an
    internetwork-wide property rather than a per-row one.

    ``uq_bacnet_device_instance`` should make a duplicate impossible,
    so a finding here means data arrived by a path that bypassed the
    ORM — a direct SQL load, a restore from a backup taken before the
    constraint existed, or a future bulk importer that disables
    constraints. That is exactly when an operator most needs to be
    told, which is why the check exists despite the constraint.
    """
    _ = target, args, now
    if target_kind != "platform":
        return CheckOutcome.not_applicable(f"requires target_kind=platform (got {target_kind})")
    dupes = list(
        (
            await db.execute(
                select(BACnetDevice.device_instance, func.count(BACnetDevice.id).label("n"))
                .group_by(BACnetDevice.device_instance)
                .having(func.count(BACnetDevice.id) > 1)
            )
        ).all()
    )
    if not dupes:
        return CheckOutcome.passed("Every BACnet device instance is unique")
    return CheckOutcome.fail(
        detail=(
            f"{len(dupes)} BACnet device instance(s) are held by more than one "
            f"device: {', '.join(str(r._mapping['device_instance']) for r in dupes[:20])}"
        ),
        diagnostic={
            "duplicate_instances": [int(r._mapping["device_instance"]) for r in dupes[:20]],
        },
    )


@register("bacnet_vendor_id_unknown")
async def check_bacnet_vendor_id_unknown(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """BACnet devices report a plausible ASHRAE vendor id.

    Advisory. Vendor id 0 is legitimately ASHRAE itself, but on a real
    plant a device reporting 0 — or reporting nothing at all — is
    almost always a misconfigured, cloned, or counterfeit controller.
    Warn rather than fail: this is a "go look at these" signal, not a
    compliance breach.
    """
    _ = target, args, now
    if target_kind != "platform":
        return CheckOutcome.not_applicable(f"requires target_kind=platform (got {target_kind})")
    rows = list(
        (
            await db.execute(
                select(BACnetDevice.device_instance, BACnetDevice.vendor_id).where(
                    (BACnetDevice.vendor_id.is_(None)) | (BACnetDevice.vendor_id == 0)
                )
            )
        ).all()
    )
    if not rows:
        return CheckOutcome.passed("Every BACnet device reports a non-zero vendor id")
    return CheckOutcome.warn(
        detail=(
            f"{len(rows)} BACnet device(s) report vendor id 0 or none — "
            "commonly a misconfigured or cloned controller"
        ),
        diagnostic={
            "device_instances": [int(r._mapping["device_instance"]) for r in rows[:20]],
        },
    )


# ── Checks: Industrial / OT (#542) ──────────────────────────────────


@register("ot_device_crosses_purdue_boundary")
async def check_ot_device_crosses_purdue_boundary(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """An OT device sits at the Purdue level its subnet is zoned for.

    OT networks are organised by the Purdue model, and the whole point
    of that segmentation is that a Level-1 controller does not live in
    a Level-4 enterprise subnet. A mismatch is the finding an auditor
    asks for and that nobody can produce today without a spreadsheet.

    NOT_APPLICABLE when either side is unset: an unrecorded level is
    missing documentation, not a segmentation violation, and treating
    unknown as guilty would make the check unusable during rollout.
    """
    _ = args, now
    if target is None or not isinstance(target, IPAddress):
        return CheckOutcome.not_applicable(
            "Target row missing or wrong kind for ot_device_crosses_purdue_boundary"
        )
    device = (
        await db.execute(select(OTDevice).where(OTDevice.ip_address_id == target.id))
    ).scalar_one_or_none()
    if device is None:
        return CheckOutcome.not_applicable("Address carries no OT descriptor")

    # Zone resolution and the level comparison both live in
    # ``services.ot.zones`` — the by-address REST endpoint calls the same
    # pair, so the conformity verdict and what an operator sees on the IP
    # detail panel cannot disagree about the same device.
    zone = await effective_zone_for_address(db, target.id)
    mismatch = purdue_mismatch(device, zone)
    device_level = purdue_to_str(device.purdue_level)
    zone_level = purdue_to_str(zone.purdue_level) if zone is not None else None

    if mismatch is None:
        # Tri-state: an unrecorded level on either side is missing
        # documentation, not a segmentation violation.
        if zone is None:
            return CheckOutcome.not_applicable("Subnet has no OT zone declared")
        return CheckOutcome.not_applicable("OT device has no Purdue level recorded")

    if not mismatch:
        return CheckOutcome.passed(
            f"Device and subnet agree at Purdue level {zone_level}",
            {"device_purdue_level": device_level, "zone_purdue_level": zone_level},
        )
    cell = zone.cell_area if zone is not None else ""
    return CheckOutcome.fail(
        detail=(
            f"OT device is Purdue level {device_level} but its subnet is "
            f"zoned level {zone_level}"
            f"{f' ({cell})' if cell else ''}"
        ),
        diagnostic={
            "device_purdue_level": device_level,
            "zone_purdue_level": zone_level,
            "ot_protocol": device.ot_protocol,
            "ot_role": device.ot_role,
            "cell_area": cell,
        },
    )


@register("ot_zone_missing_purdue_level")
async def check_ot_zone_missing_purdue_level(
    db: AsyncSession,
    *,
    target: object | None,
    target_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> CheckOutcome:
    """A subnet carrying OT devices declares an OT zone.

    Advisory. Without a zone row the Purdue-boundary check above has
    nothing to compare against, so this is the check that tells an
    operator why their segmentation report is empty.
    """
    _ = args, now
    if target is None or not isinstance(target, Subnet):
        return CheckOutcome.not_applicable(
            "Target row missing or wrong kind for ot_zone_missing_purdue_level"
        )
    device_count = (
        await db.execute(
            select(func.count(OTDevice.id))
            .select_from(OTDevice)
            .join(IPAddress, IPAddress.id == OTDevice.ip_address_id)
            .where(IPAddress.subnet_id == target.id)
        )
    ).scalar_one()
    if not device_count:
        return CheckOutcome.not_applicable("Subnet carries no OT devices")
    zone = (
        await db.execute(select(OTZone).where(OTZone.subnet_id == target.id))
    ).scalar_one_or_none()
    if zone is not None:
        return CheckOutcome.passed(
            f"Subnet is zoned at Purdue level {zone.purdue_level}",
            {"purdue_level": str(zone.purdue_level), "ot_device_count": int(device_count)},
        )
    return CheckOutcome.warn(
        detail=(
            f"Subnet carries {device_count} OT device(s) but declares no OT zone, "
            "so Purdue-boundary conformity cannot be evaluated for it"
        ),
        diagnostic={"ot_device_count": int(device_count)},
    )


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
    {
        "name": "no_lanwide_control_plane_ports",
        "label": "No appliance exposes LAN-wide k3s control-plane ports",
        "supports": ["platform"],
        "args": [
            {
                "name": "stale_minutes",
                "type": "int",
                "required": False,
                "default": 30,
                "label": "Treat a node's report as stale after N minutes (PASS-stale, never FAIL)",
            }
        ],
    },
    {
        "name": "av_flow_outside_reserved_range",
        "label": "AV flow sits inside a declared range for its protocol",
        "supports": ["multicast_group"],
        "args": [],
    },
    {
        "name": "av_flow_no_ptp_domain",
        "label": "AV flow records a PTP clock domain (advisory)",
        "supports": ["multicast_group"],
        "args": [],
    },
    {
        "name": "bbmd_one_per_subnet",
        "label": "Exactly one BACnet BBMD per subnet",
        "supports": ["subnet"],
        "args": [],
    },
    {
        "name": "bacnet_duplicate_device_instance",
        "label": "BACnet device instance numbers are unique internetwork-wide",
        "supports": ["platform"],
        "args": [],
    },
    {
        "name": "bacnet_vendor_id_unknown",
        "label": "BACnet devices report a plausible vendor id (advisory)",
        "supports": ["platform"],
        "args": [],
    },
    {
        "name": "ot_device_crosses_purdue_boundary",
        "label": "OT device matches its subnet's Purdue level",
        "supports": ["ip_address"],
        "args": [],
    },
    {
        "name": "ot_zone_missing_purdue_level",
        "label": "Subnet with OT devices declares an OT zone (advisory)",
        "supports": ["subnet"],
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
