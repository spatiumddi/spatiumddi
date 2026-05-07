"""Alerts — rule evaluator + delivery.

Called once per minute from a Celery beat tick (see tasks/alerts.py).
For each enabled ``AlertRule`` we:

  1. Compute the set of subjects that currently match the rule.
  2. For each newly-matching subject with no existing open event,
     open a new ``AlertEvent`` and dispatch it to the configured
     delivery channels (syslog + webhook, reusing the platform-level
     audit-forward targets).
  3. For each open event whose subject no longer matches, flip
     ``resolved_at`` to now.

The filter from ``PlatformSettings.utilization_max_prefix_*`` applies
to ``subnet_utilization`` rules so small PTP / loopback subnets can't
trip the alarm — same predicate the dashboard honours.

Domain rule types use a slightly different shape: the four match
families come from ``Domain`` row state (expiry date, drift flag,
registrar transition, dnssec transition). Two of them are
"transition-once" rules (``domain_registrar_changed`` /
``domain_dnssec_status_changed``) — the evaluator latches the
observed value into ``AlertEvent.last_observed_value`` so a single
flip fires exactly one event, and that event auto-resolves after
``_TRANSITION_AUTO_RESOLVE_DAYS`` (7 d) or when an operator marks
it resolved.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertEvent, AlertRule
from app.models.asn import ASN, ASNRpkiRoa
from app.models.audit import AuditLog
from app.models.circuit import Circuit
from app.models.dhcp import DHCPLease, DHCPScope, DHCPServer
from app.models.dns import DNSServer, DNSZone
from app.models.domain import Domain
from app.models.ipam import IPAddress, IPBlock, Subnet
from app.models.network_service import NetworkService, NetworkServiceResource
from app.models.overlay import OverlayNetwork
from app.models.ownership import Site
from app.models.settings import PlatformSettings
from app.models.vrf import VRF
from app.services import audit_forward

logger = structlog.get_logger(__name__)


RULE_TYPE_SUBNET_UTILIZATION = "subnet_utilization"
RULE_TYPE_SERVER_UNREACHABLE = "server_unreachable"
# ASN / RPKI rule types — Phase 2 of issue #85.
RULE_TYPE_ASN_HOLDER_DRIFT = "asn_holder_drift"
RULE_TYPE_ASN_WHOIS_UNREACHABLE = "asn_whois_unreachable"
RULE_TYPE_RPKI_ROA_EXPIRING = "rpki_roa_expiring"
RULE_TYPE_RPKI_ROA_EXPIRED = "rpki_roa_expired"
# Domain rule types — Phase 2 of issue #87.
RULE_TYPE_DOMAIN_EXPIRING = "domain_expiring"
RULE_TYPE_DOMAIN_NS_DRIFT = "domain_nameserver_drift"
RULE_TYPE_DOMAIN_REGISTRAR_CHANGED = "domain_registrar_changed"
RULE_TYPE_DOMAIN_DNSSEC_CHANGED = "domain_dnssec_status_changed"
# Circuit rule types — alerting hooks for issue #93.
RULE_TYPE_CIRCUIT_TERM_EXPIRING = "circuit_term_expiring"
RULE_TYPE_CIRCUIT_STATUS_CHANGED = "circuit_status_changed"
# Service catalog rule types — alerting hooks for issue #94.
RULE_TYPE_SERVICE_TERM_EXPIRING = "service_term_expiring"
RULE_TYPE_SERVICE_RESOURCE_ORPHANED = "service_resource_orphaned"
# Compliance change alerts — issue #105. One rule type with two
# params (``classification`` + ``change_scope``) covers every flag
# without exploding into N near-identical rule_type rows.
RULE_TYPE_COMPLIANCE_CHANGE = "compliance_change"
RULE_TYPE_AUDIT_CHAIN_BROKEN = "audit_chain_broken"
# Voice-VLAN client-count drop — issue #112 phase 2. Counts active
# DHCP leases on every subnet tagged ``subnet_role='voice'``; fires
# when the count drops below ``threshold_percent`` (reused as a raw
# count threshold for this rule type — operators set it to e.g. 10
# meaning "alert me when fewer than 10 phones are reachable").
RULE_TYPE_VOICE_LEASE_COUNT_BELOW = "voice_lease_count_below"

RULE_TYPES = frozenset(
    {
        RULE_TYPE_SUBNET_UTILIZATION,
        RULE_TYPE_SERVER_UNREACHABLE,
        RULE_TYPE_ASN_HOLDER_DRIFT,
        RULE_TYPE_ASN_WHOIS_UNREACHABLE,
        RULE_TYPE_RPKI_ROA_EXPIRING,
        RULE_TYPE_RPKI_ROA_EXPIRED,
        RULE_TYPE_DOMAIN_EXPIRING,
        RULE_TYPE_DOMAIN_NS_DRIFT,
        RULE_TYPE_DOMAIN_REGISTRAR_CHANGED,
        RULE_TYPE_DOMAIN_DNSSEC_CHANGED,
        RULE_TYPE_CIRCUIT_TERM_EXPIRING,
        RULE_TYPE_CIRCUIT_STATUS_CHANGED,
        RULE_TYPE_SERVICE_TERM_EXPIRING,
        RULE_TYPE_SERVICE_RESOURCE_ORPHANED,
        RULE_TYPE_COMPLIANCE_CHANGE,
        RULE_TYPE_AUDIT_CHAIN_BROKEN,
        RULE_TYPE_VOICE_LEASE_COUNT_BELOW,
    }
)

# Compliance-change rule constants. Keep in lock-step with the
# Subnet model in ``backend/app/models/ipam.py`` — only flags that
# exist as Subnet columns can be matched. Inheritance from
# block / space is intentionally deferred (the schema doesn't carry
# the flags above subnet level today; revisit when block/space-level
# classification lands).
COMPLIANCE_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"pci_scope", "hipaa_scope", "internet_facing"}
)
_CLASSIFICATION_LABEL: dict[str, str] = {
    "pci_scope": "PCI",
    "hipaa_scope": "HIPAA",
    "internet_facing": "internet-facing",
}

COMPLIANCE_CHANGE_SCOPES: frozenset[str] = frozenset({"any_change", "create", "delete"})
_COMPLIANCE_CHANGE_SCOPE_ACTIONS: dict[str, frozenset[str]] = {
    "any_change": frozenset({"create", "update", "delete"}),
    "create": frozenset({"create"}),
    "delete": frozenset({"delete"}),
}

# Compliance events are point-in-time notifications, not ongoing
# conditions. Keep them open just long enough to surface on the
# alerts dashboard, then auto-resolve.
_COMPLIANCE_CHANGE_AUTO_RESOLVE_HOURS = 24

# Cap the audit-row scan per pass — guards against a runaway backfill
# if a rule sat disabled for a long time then got flipped on. The
# watermark advances by however many rows we processed, so the next
# tick picks up where this one left off.
_COMPLIANCE_CHANGE_SCAN_LIMIT = 1000

# Resource types in audit_log we know how to map back to a Subnet for
# classification lookup. Anything outside this set is skipped with a
# logged debug. The map values name a mapper function below.
_COMPLIANCE_RESOURCE_TYPES: frozenset[str] = frozenset({"subnet", "ip_address", "dhcp_scope"})

# Resource-kind → SQLAlchemy model for the orphan sweep. Mirrors the
# router's ``_KIND_MODEL`` map. ``overlay_network`` lit up alongside
# #95 so the sweep covers it too.
_ORPHAN_RESOURCE_MODELS: dict[str, Any] = {
    "vrf": VRF,
    "subnet": Subnet,
    "ip_block": IPBlock,
    "dns_zone": DNSZone,
    "dhcp_scope": DHCPScope,
    "circuit": Circuit,
    "site": Site,
    "overlay_network": OverlayNetwork,
}

# ``circuit_status_changed`` — destination statuses that are
# operator-noteworthy. ``active`` ↔ ``pending`` flips during
# commissioning are routine and don't fire.
_CIRCUIT_STATUS_CHANGE_DESTS: frozenset[str] = frozenset({"suspended", "decom"})

# Default consecutive-failure threshold for ``asn_whois_unreachable``.
_ASN_WHOIS_UNREACHABLE_THRESHOLD = 3

# Default expiring threshold when ``domain_expiring`` doesn't pin one.
_DEFAULT_EXPIRING_THRESHOLD_DAYS = 30

# Auto-resolve window for the two "fires once on transition" domain
# rule types (registrar / DNSSEC change). Transitions don't resolve
# themselves the way threshold-bound conditions do, so we time-box
# the open event. Operators can also manually resolve at any point.
_TRANSITION_AUTO_RESOLVE_DAYS = 7


def _prefix_len(network: str) -> tuple[int, int] | None:
    """Return (prefix_len, family) — family is 4 or 6. None on parse error."""
    try:
        net = ipaddress.ip_network(network, strict=False)
    except ValueError:
        return None
    return net.prefixlen, net.version


def _include_subnet(subnet: Subnet, settings: PlatformSettings | None) -> bool:
    """Mirror of frontend/src/lib/utilization.ts:includeInUtilization."""
    if settings is None:
        return True
    parsed = _prefix_len(str(subnet.network))
    if parsed is None:
        return True
    prefix, family = parsed
    max_prefix = (
        settings.utilization_max_prefix_ipv4
        if family == 4
        else settings.utilization_max_prefix_ipv6
    )
    return prefix <= max_prefix


# ── Subject evaluation ─────────────────────────────────────────────────────


async def _matching_subnet_subjects(
    db: AsyncSession,
    rule: AlertRule,
    settings: PlatformSettings | None,
) -> list[tuple[str, str, str]]:
    """Return [(subject_id, display, message), ...] for a subnet_utilization rule."""
    threshold = rule.threshold_percent if rule.threshold_percent is not None else 90
    res = await db.execute(select(Subnet).where(Subnet.utilization_percent >= threshold))
    subnets = list(res.scalars().all())
    matches: list[tuple[str, str, str]] = []
    for s in subnets:
        if not _include_subnet(s, settings):
            continue
        pct = float(s.utilization_percent)
        display = f"{s.network}" + (f" — {s.name}" if s.name else "")
        message = (
            f"Subnet {display} utilisation {pct:.1f}% (threshold {threshold}%) — "
            f"{s.allocated_ips}/{s.total_ips} IPs allocated"
        )
        matches.append((str(s.id), display, message))
    return matches


async def _matching_voice_lease_count_below_subjects(
    db: AsyncSession,
    rule: AlertRule,
) -> list[tuple[str, str, str]]:
    """Voice-VLAN subnets where active-lease count has fallen below
    ``rule.threshold_percent`` (re-used as a raw count threshold).

    Useful for catching mass-disconnect events on a phone fleet — if
    a switch / PoE upstream / SBC goes down, every phone drops its
    lease and the count plummets. Operator picks the threshold per
    deployment (typical: ~50% of expected fleet size).
    """
    threshold = int(rule.threshold_percent) if rule.threshold_percent is not None else 1
    # Voice-tagged subnets only — `subnet_role='voice'` is the gate.
    voice_subnets = list(
        (await db.execute(select(Subnet).where(Subnet.subnet_role == "voice"))).scalars().all()
    )
    if not voice_subnets:
        return []

    # Count active leases per voice subnet. ``DHCPLease`` carries
    # ``ip_address`` (INET) + ``state`` — we count rows whose IP is
    # inside the subnet CIDR and state == 'active'. PostgreSQL's
    # ``<<`` (contained-by-network) is the natural operator.
    matches: list[tuple[str, str, str]] = []
    for s in voice_subnets:
        cidr = str(s.network) if s.network else None
        if not cidr:
            continue
        # ``<<`` is the Postgres "is contained by" operator on inet /
        # cidr types. The bind parameter is a plain string so we cast
        # it explicitly with ``::cidr`` — without the cast asyncpg
        # picks VARCHAR and Postgres rejects the operator.
        count = (
            await db.execute(
                select(func.count(DHCPLease.id))
                .where(DHCPLease.state == "active")
                .where(text("ip_address << CAST(:c AS cidr)").bindparams(c=cidr))
            )
        ).scalar_one()
        if int(count or 0) >= threshold:
            continue
        display = f"{s.network}" + (f" — {s.name}" if s.name else "")
        message = (
            f"Voice subnet {display} has {int(count or 0)} active lease(s) "
            f"(threshold {threshold}) — possible mass-disconnect event"
        )
        matches.append((str(s.id), display, message))
    return matches


async def _matching_asn_drift_subjects(
    db: AsyncSession, rule: AlertRule  # noqa: ARG001 — symmetry with sibling evaluators
) -> list[tuple[str, str, str]]:
    """Every ``asn`` row currently in ``whois_state="drift"``."""
    res = await db.execute(select(ASN).where(ASN.whois_state == "drift"))
    matches: list[tuple[str, str, str]] = []
    for row in res.scalars().all():
        display = f"AS{row.number}" + (f" ({row.name})" if row.name else "")
        new_holder = row.holder_org or "<unknown>"
        message = f"AS{row.number} WHOIS holder changed — current holder: {new_holder}"
        matches.append((str(row.id), display, message))
    return matches


async def _matching_asn_unreachable_subjects(
    db: AsyncSession, rule: AlertRule  # noqa: ARG001
) -> list[tuple[str, str, str]]:
    """Every ``asn`` row whose ``whois_data.consecutive_failures`` has
    crossed the threshold and is currently in ``whois_state="unreachable"``.

    ``consecutive_failures`` lives inside the JSONB ``whois_data`` blob
    (the refresh task increments it on every failed RDAP fetch and
    resets it on success). Reading it via ORM gives us the live value
    without a JSONB query expression.
    """
    res = await db.execute(select(ASN).where(ASN.whois_state == "unreachable"))
    matches: list[tuple[str, str, str]] = []
    for row in res.scalars().all():
        data = row.whois_data if isinstance(row.whois_data, dict) else {}
        try:
            failures = int(data.get("consecutive_failures") or 0)
        except (TypeError, ValueError):
            failures = 0
        if failures < _ASN_WHOIS_UNREACHABLE_THRESHOLD:
            continue
        display = f"AS{row.number}" + (f" ({row.name})" if row.name else "")
        message = f"AS{row.number} WHOIS unreachable — {failures} consecutive RDAP fetch failures"
        matches.append((str(row.id), display, message))
    return matches


async def _matching_rpki_roa_expiring_subjects(
    db: AsyncSession, rule: AlertRule  # noqa: ARG001
) -> list[tuple[str, str, str]]:
    """Every ROA in ``state="expiring_soon"``.

    The refresh task derives the state ladder; the alert evaluator
    just reads it. Severity is operator-chosen on the rule itself —
    soft / warning / critical for <30d / <7d / <24h respectively;
    operators create N rules with different severities + filters
    when they want graduated alerting.
    """
    res = await db.execute(select(ASNRpkiRoa).where(ASNRpkiRoa.state == "expiring_soon"))
    matches: list[tuple[str, str, str]] = []
    now = datetime.now(UTC)
    for roa in res.scalars().all():
        # Resolve the parent AS for a human-friendly display string.
        parent = await db.get(ASN, roa.asn_id)
        parent_label = f"AS{parent.number}" if parent is not None else "AS?"
        display = f"{parent_label} {roa.prefix}-{roa.max_length}"
        when = ""
        if roa.valid_to is not None:
            delta = roa.valid_to - now
            days = max(0, delta.days)
            when = f" — expires in {days}d"
        message = (
            f"RPKI ROA {parent_label} {roa.prefix} maxLen {roa.max_length} "
            f"({roa.trust_anchor}) is expiring soon{when}"
        )
        matches.append((str(roa.id), display, message))
    return matches


async def _matching_rpki_roa_expired_subjects(
    db: AsyncSession, rule: AlertRule  # noqa: ARG001
) -> list[tuple[str, str, str]]:
    """Every ROA in ``state="expired"``."""
    res = await db.execute(select(ASNRpkiRoa).where(ASNRpkiRoa.state == "expired"))
    matches: list[tuple[str, str, str]] = []
    for roa in res.scalars().all():
        parent = await db.get(ASN, roa.asn_id)
        parent_label = f"AS{parent.number}" if parent is not None else "AS?"
        display = f"{parent_label} {roa.prefix}-{roa.max_length}"
        message = (
            f"RPKI ROA {parent_label} {roa.prefix} maxLen {roa.max_length} "
            f"({roa.trust_anchor}) has expired"
        )
        matches.append((str(roa.id), display, message))
    return matches


# Suppress the unused-import warning for ``timedelta`` when this module
# is read in isolation — used in expiring-soon message rendering.
_ = timedelta


async def _matching_server_subjects(
    db: AsyncSession, rule: AlertRule
) -> list[tuple[str, str, str]]:
    """Return matches for a server_unreachable rule."""
    server_type = rule.server_type or "any"
    matches: list[tuple[str, str, str]] = []

    if server_type in ("dns", "any"):
        res = await db.execute(
            select(DNSServer).where(
                or_(DNSServer.status == "unreachable", DNSServer.status == "error")
            )
        )
        for s in res.scalars().all():
            display = f"DNS {s.name}"
            message = f"DNS server {s.name} is {s.status}"
            matches.append((f"dns:{s.id}", display, message))

    if server_type in ("dhcp", "any"):
        res = await db.execute(
            select(DHCPServer).where(
                or_(DHCPServer.status == "unreachable", DHCPServer.status == "error")
            )
        )
        for s in res.scalars().all():
            display = f"DHCP {s.name}"
            message = f"DHCP server {s.name} is {s.status}"
            matches.append((f"dhcp:{s.id}", display, message))

    return matches


# ── Domain rule evaluators ──────────────────────────────────────────


def _escalate_severity_for_expiring(
    base_severity: str,
    *,
    threshold_days: int,
    days_to_expiry: float,
) -> str:
    """For ``domain_expiring`` we widen the rule's base severity based
    on how close the actual expiry is — the issue spec calls for soft
    at threshold / warning at threshold/4 / critical at threshold/12.

    The base severity acts as a *floor*: a rule authored with
    ``severity="critical"`` always fires critical; a rule authored
    with ``severity="info"`` upgrades to warning / critical as the
    expiry window narrows. This way operators get one rule per
    domain (or zero — defaults to warning at threshold/4), not three.
    """

    def _rank(s: str) -> int:
        return {"info": 0, "warning": 1, "critical": 2}.get(s, 1)

    base_rank = _rank(base_severity)
    actual_rank = 0  # info at the soft threshold

    # Avoid division blowups for absurdly small thresholds. Floor of 1.
    safe = max(1, threshold_days)
    if days_to_expiry <= safe / 12:
        actual_rank = 2  # critical
    elif days_to_expiry <= safe / 4:
        actual_rank = 1  # warning

    final = max(base_rank, actual_rank)
    return ("info", "warning", "critical")[final]


async def _matching_domain_expiring_subjects(
    db: AsyncSession,
    rule: AlertRule,
    now: datetime,
) -> list[tuple[str, str, str, str]]:
    """Return ``[(subject_id, display, message, severity)]`` for the
    ``domain_expiring`` rule type. Severity escalates per the
    threshold/4 / threshold/12 boundaries.
    """
    threshold_days = rule.threshold_days or _DEFAULT_EXPIRING_THRESHOLD_DAYS
    cutoff = now + timedelta(days=threshold_days)

    rows = (
        (
            await db.execute(
                select(Domain)
                .where(Domain.expires_at.is_not(None))
                .where(Domain.expires_at <= cutoff)
            )
        )
        .scalars()
        .all()
    )

    matches: list[tuple[str, str, str, str]] = []
    for d in rows:
        # Defensive coerce — Postgres returns timezone-aware, but
        # tests may construct naive datetimes.
        exp = d.expires_at
        if exp is None:
            continue
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        delta = exp - now
        days_to_expiry = delta.total_seconds() / 86400.0

        sev = _escalate_severity_for_expiring(
            rule.severity,
            threshold_days=threshold_days,
            days_to_expiry=days_to_expiry,
        )

        if days_to_expiry <= 0:
            descriptor = "expired"
        elif days_to_expiry < 1:
            descriptor = "expires within 24 h"
        else:
            descriptor = f"expires in {int(days_to_expiry)} day(s)"

        message = (
            f"Domain {d.name} {descriptor} (expires_at "
            f"{exp.isoformat()}, threshold {threshold_days} d)"
        )
        matches.append((str(d.id), d.name, message, sev))
    return matches


async def _matching_domain_drift_subjects(
    db: AsyncSession, rule: AlertRule
) -> list[tuple[str, str, str]]:
    """``domain_nameserver_drift`` — fires for every domain whose
    operator-set ``expected_nameservers`` doesn't match the
    last-observed ``actual_nameservers``."""
    rows = (
        (await db.execute(select(Domain).where(Domain.nameserver_drift.is_(True)))).scalars().all()
    )
    matches: list[tuple[str, str, str]] = []
    for d in rows:
        expected = sorted(d.expected_nameservers or [])
        actual = sorted(d.actual_nameservers or [])
        message = f"Domain {d.name} NS drift — " f"expected={expected!r}, actual={actual!r}"
        matches.append((str(d.id), d.name, message))
    return matches


async def _evaluate_domain_transition_rule(
    db: AsyncSession,
    rule: AlertRule,
    *,
    field_name: str,
    rule_label: str,
    now: datetime,
) -> tuple[int, int, int, int, int]:
    """Shared body for the two "fires once on transition" domain rules.

    Walks every Domain row, looks up the most recent open event for
    ``(rule, subject_id)``. When the current value of ``field_name``
    differs from the snapshot stored in that event's
    ``last_observed_value.to``, opens a new event with the snapshot
    ``{"from": <previous>, "to": <current>}``. Auto-resolves any open
    event older than ``_TRANSITION_AUTO_RESOLVE_DAYS`` days.

    Returns ``(opened, resolved, delivered_syslog, delivered_webhook,
    delivered_smtp)`` aligned with the main evaluator's accumulators.

    Note: this approach relies on each new transition's "from" being
    the previous "to", so re-firing on the same value-pair is
    suppressed by the existing-open-event check. A registrar that
    flips A→B→A within the auto-resolve window opens two events (the
    A→B transition, then B→A); that's the intended behaviour.
    """
    targets = await audit_forward._load_targets()  # noqa: SLF001

    opened = 0
    resolved = 0
    delivered_syslog = 0
    delivered_webhook = 0
    delivered_smtp = 0

    # Index existing OPEN events by subject_id so we can compare the
    # snapshot the last firing latched against the row's current value.
    open_res = await db.execute(
        select(AlertEvent).where(
            AlertEvent.rule_id == rule.id,
            AlertEvent.resolved_at.is_(None),
        )
    )
    open_events = list(open_res.scalars().all())
    open_by_subject: dict[str, AlertEvent] = {ev.subject_id: ev for ev in open_events}

    # Auto-resolve any open transition event whose age exceeds the
    # window. Time-bounding these is important — the alternative is a
    # UI cluttered with months-old "registrar changed" rows.
    cutoff = now - timedelta(days=_TRANSITION_AUTO_RESOLVE_DAYS)
    for ev in list(open_events):
        if ev.fired_at < cutoff:
            ev.resolved_at = now
            resolved += 1
            del open_by_subject[ev.subject_id]

    # We also need each domain's *previous* observed value (i.e. the
    # last "to" we latched into an event, regardless of whether that
    # event is still open). Without it the first transition after
    # rule-create has no "from" to record. Look up the most recent
    # event row per subject — open or resolved.
    last_event_res = await db.execute(
        select(AlertEvent).where(AlertEvent.rule_id == rule.id).order_by(AlertEvent.fired_at.desc())
    )
    last_event_by_subject: dict[str, AlertEvent] = {}
    for ev in last_event_res.scalars().all():
        if ev.subject_id not in last_event_by_subject:
            last_event_by_subject[ev.subject_id] = ev

    rows = (await db.execute(select(Domain))).scalars().all()
    for d in rows:
        subject_id = str(d.id)
        current_value = getattr(d, field_name)
        # Bool / nullable string both serialise into JSON cleanly.
        if open_by_subject.get(subject_id) is not None:
            # Already an open transition for this domain — wait it
            # out (will auto-resolve at the cutoff above).
            continue

        prior_event = last_event_by_subject.get(subject_id)
        if prior_event is not None and isinstance(prior_event.last_observed_value, dict):
            prior_value = prior_event.last_observed_value.get("to")
        else:
            prior_value = None

        # First-ever sighting (no prior event): record the "first
        # observation" silently — open + immediately resolve so we
        # have a baseline without paging the operator. Unset values
        # (registrar=NULL on a row that's never been refreshed) get
        # treated as "no observation yet" and skipped.
        if prior_event is None:
            if current_value is None:
                continue
            baseline = AlertEvent(
                rule_id=rule.id,
                subject_type="domain",
                subject_id=subject_id,
                subject_display=d.name,
                severity="info",
                message=f"Initial {rule_label} baseline for {d.name}: {current_value!r}",
                fired_at=now,
                resolved_at=now,
                last_observed_value={"from": None, "to": current_value},
            )
            db.add(baseline)
            continue

        if current_value == prior_value:
            continue

        # Real transition. Open a fresh event + deliver.
        message = f"Domain {d.name} {rule_label} changed: " f"{prior_value!r} → {current_value!r}"
        event = AlertEvent(
            rule_id=rule.id,
            subject_type="domain",
            subject_id=subject_id,
            subject_display=d.name,
            severity=rule.severity,
            message=message,
            fired_at=now,
            last_observed_value={"from": prior_value, "to": current_value},
        )
        db.add(event)
        await db.flush()  # populate event.id for delivery payload
        ds, dw, dm = await _deliver(rule, event, targets)
        event.delivered_syslog = ds
        event.delivered_webhook = dw
        event.delivered_smtp = dm
        opened += 1
        if ds:
            delivered_syslog += 1
        if dw:
            delivered_webhook += 1
        if dm:
            delivered_smtp += 1

    return opened, resolved, delivered_syslog, delivered_webhook, delivered_smtp


# ── Circuit rule evaluators ─────────────────────────────────────────


async def _matching_circuit_term_expiring_subjects(
    db: AsyncSession,
    rule: AlertRule,
    now: datetime,
) -> list[tuple[str, str, str, str]]:
    """Return ``[(subject_id, display, message, severity)]`` for the
    ``circuit_term_expiring`` rule type. Mirrors ``domain_expiring`` —
    severity escalates per ``threshold/4`` / ``threshold/12`` so a
    single rule covers info / warning / critical without three
    separate rules.

    ``status='decom'`` rows are excluded — a decommissioned circuit
    expiring is not actionable. Soft-deleted rows are also excluded.
    """
    threshold_days = rule.threshold_days or _DEFAULT_EXPIRING_THRESHOLD_DAYS
    cutoff = (now + timedelta(days=threshold_days)).date()

    rows = (
        (
            await db.execute(
                select(Circuit)
                .where(Circuit.deleted_at.is_(None))
                .where(Circuit.status != "decom")
                .where(Circuit.term_end_date.is_not(None))
                .where(Circuit.term_end_date <= cutoff)
            )
        )
        .scalars()
        .all()
    )

    matches: list[tuple[str, str, str, str]] = []
    today = now.date()
    for c in rows:
        if c.term_end_date is None:
            continue
        days_to_expiry = (c.term_end_date - today).days
        sev = _escalate_severity_for_expiring(
            rule.severity,
            threshold_days=threshold_days,
            days_to_expiry=days_to_expiry,
        )
        if days_to_expiry <= 0:
            descriptor = "term has expired"
        elif days_to_expiry == 1:
            descriptor = "term expires tomorrow"
        else:
            descriptor = f"term expires in {days_to_expiry} day(s)"
        message = (
            f"Circuit {c.name} {descriptor} "
            f"(term_end_date {c.term_end_date.isoformat()}, threshold "
            f"{threshold_days} d)"
        )
        matches.append((str(c.id), c.name, message, sev))
    return matches


async def _evaluate_circuit_status_changed_rule(
    db: AsyncSession,
    rule: AlertRule,
    now: datetime,
) -> tuple[int, int, int, int, int]:
    """``circuit_status_changed`` — fires once when a circuit's status
    transitions into ``suspended`` or ``decom``.

    The router stamps ``previous_status`` + ``last_status_change_at``
    on every status update (see
    ``backend/app/api/v1/circuits/router.py:_stamp_status_transition``)
    so this evaluator just keys events on ``last_status_change_at``:
    a new firing is keyed by the timestamp, and the most recent event
    for the subject latches that timestamp into
    ``last_observed_value.changed_at``. If we see a row whose current
    timestamp doesn't match the latched one we have a fresh transition
    to fire on. Auto-resolves after ``_TRANSITION_AUTO_RESOLVE_DAYS``.

    Routine ``active`` ↔ ``pending`` flips during commissioning are
    intentionally excluded — only the ``suspended`` / ``decom`` states
    surface to the operator.
    """
    targets = await audit_forward._load_targets()  # noqa: SLF001

    opened = 0
    resolved = 0
    delivered_syslog = 0
    delivered_webhook = 0
    delivered_smtp = 0

    # All open events for this rule, keyed by subject.
    open_res = await db.execute(
        select(AlertEvent).where(
            AlertEvent.rule_id == rule.id,
            AlertEvent.resolved_at.is_(None),
        )
    )
    open_events = list(open_res.scalars().all())
    open_by_subject: dict[str, AlertEvent] = {ev.subject_id: ev for ev in open_events}

    # Auto-resolve old open events.
    cutoff = now - timedelta(days=_TRANSITION_AUTO_RESOLVE_DAYS)
    for ev in list(open_events):
        if ev.fired_at < cutoff:
            ev.resolved_at = now
            resolved += 1
            del open_by_subject[ev.subject_id]

    # Most recent event (open or resolved) per subject — needed so we
    # can compare its latched ``changed_at`` against the row's current
    # ``last_status_change_at``. Without that, every evaluation pass
    # would re-fire on the same transition.
    last_event_res = await db.execute(
        select(AlertEvent).where(AlertEvent.rule_id == rule.id).order_by(AlertEvent.fired_at.desc())
    )
    last_event_by_subject: dict[str, AlertEvent] = {}
    for ev in last_event_res.scalars().all():
        if ev.subject_id not in last_event_by_subject:
            last_event_by_subject[ev.subject_id] = ev

    rows = (await db.execute(select(Circuit).where(Circuit.deleted_at.is_(None)))).scalars().all()
    for c in rows:
        subject_id = str(c.id)
        if c.last_status_change_at is None:
            continue
        if c.status not in _CIRCUIT_STATUS_CHANGE_DESTS:
            continue

        # Skip if there's an open event for this subject — wait for
        # the auto-resolve cutoff above.
        if subject_id in open_by_subject:
            continue

        # If the most recent event already latched this exact
        # ``last_status_change_at``, we've already fired for it.
        prior_event = last_event_by_subject.get(subject_id)
        if prior_event is not None and isinstance(prior_event.last_observed_value, dict):
            latched = prior_event.last_observed_value.get("changed_at")
            if latched == c.last_status_change_at.isoformat():
                continue

        from_label = c.previous_status or "<unset>"
        to_label = c.status
        message = f"Circuit {c.name} status: {from_label} → {to_label}"

        event = AlertEvent(
            rule_id=rule.id,
            subject_type="circuit",
            subject_id=subject_id,
            subject_display=c.name,
            severity=rule.severity,
            message=message,
            fired_at=now,
            last_observed_value={
                "from": c.previous_status,
                "to": c.status,
                "changed_at": c.last_status_change_at.isoformat(),
            },
        )
        db.add(event)
        await db.flush()
        ds, dw, dm = await _deliver(rule, event, targets)
        event.delivered_syslog = ds
        event.delivered_webhook = dw
        event.delivered_smtp = dm
        opened += 1
        if ds:
            delivered_syslog += 1
        if dw:
            delivered_webhook += 1
        if dm:
            delivered_smtp += 1

    return opened, resolved, delivered_syslog, delivered_webhook, delivered_smtp


# ── Service catalog rule evaluators ─────────────────────────────────


async def _matching_service_term_expiring_subjects(
    db: AsyncSession,
    rule: AlertRule,
    now: datetime,
) -> list[tuple[str, str, str, str]]:
    """Return ``[(subject_id, display, message, severity)]`` for the
    ``service_term_expiring`` rule type. Mirrors the
    ``circuit_term_expiring`` shape — same severity escalation, same
    ``decom`` / soft-delete exclusions.
    """
    threshold_days = rule.threshold_days or _DEFAULT_EXPIRING_THRESHOLD_DAYS
    cutoff = (now + timedelta(days=threshold_days)).date()

    rows = (
        (
            await db.execute(
                select(NetworkService)
                .where(NetworkService.deleted_at.is_(None))
                .where(NetworkService.status != "decom")
                .where(NetworkService.term_end_date.is_not(None))
                .where(NetworkService.term_end_date <= cutoff)
            )
        )
        .scalars()
        .all()
    )

    matches: list[tuple[str, str, str, str]] = []
    today = now.date()
    for s in rows:
        if s.term_end_date is None:
            continue
        days_to_expiry = (s.term_end_date - today).days
        sev = _escalate_severity_for_expiring(
            rule.severity,
            threshold_days=threshold_days,
            days_to_expiry=days_to_expiry,
        )
        if days_to_expiry <= 0:
            descriptor = "term has expired"
        elif days_to_expiry == 1:
            descriptor = "term expires tomorrow"
        else:
            descriptor = f"term expires in {days_to_expiry} day(s)"
        message = (
            f"Service {s.name} {descriptor} "
            f"(term_end_date {s.term_end_date.isoformat()}, threshold "
            f"{threshold_days} d)"
        )
        matches.append((str(s.id), s.name, message, sev))
    return matches


async def _matching_service_resource_orphaned_subjects(
    db: AsyncSession,
    rule: AlertRule,  # noqa: ARG001 — symmetry with sibling evaluators
) -> list[tuple[str, str, str]]:
    """Every ``NetworkServiceResource`` join row whose target row no
    longer exists or is soft-deleted.

    The subject_id is the join row's own PK (not the missing target's
    ID) so that detaching the orphan link resolves the alert via the
    standard "subject no longer matches" branch in ``evaluate_all``.

    Soft-deleted services are skipped — their join rows are
    intentionally preserved during the trash window so a restore
    brings the bundle back intact, and surfacing alerts for them while
    they're in the trash bin would just be noise.
    """
    rows = (
        await db.execute(
            select(NetworkServiceResource, NetworkService.name)
            .join(
                NetworkService,
                NetworkServiceResource.service_id == NetworkService.id,
            )
            .where(NetworkService.deleted_at.is_(None))
        )
    ).all()

    matches: list[tuple[str, str, str]] = []
    for link, svc_name in rows:
        # ``overlay_network`` is reserved for #95 and the router blocks
        # attach attempts, so no orphan is possible. If a row somehow
        # exists, treat it as orphaned so the operator notices.
        model = _ORPHAN_RESOURCE_MODELS.get(link.resource_kind)
        if model is None:
            display = f"{svc_name}::{link.resource_kind}::{link.resource_id}"
            message = (
                f"Service {svc_name!r} has a resource link of unknown kind "
                f"{link.resource_kind!r} — manual review needed"
            )
            matches.append((str(link.id), display, message))
            continue

        target = await db.get(model, link.resource_id)
        is_orphan = target is None or getattr(target, "deleted_at", None) is not None
        if not is_orphan:
            continue

        display = f"{svc_name}::{link.resource_kind}::{link.resource_id}"
        message = (
            f"Service {svc_name!r} references {link.resource_kind} "
            f"{link.resource_id} but the target row no longer exists — "
            f"detach or re-attach to resolve"
        )
        matches.append((str(link.id), display, message))
    return matches


# ── Compliance change rule evaluator ────────────────────────────────


async def _resolve_compliance_subnet(
    db: AsyncSession,
    *,
    resource_type: str,
    resource_id: str,
    old_value: dict[str, Any] | None,
) -> Subnet | None:
    """Map an audit_log row's ``(resource_type, resource_id)`` back to
    the Subnet whose classification flags should be consulted.

    For ``subnet`` rows the resource itself IS the subnet. For
    ``ip_address`` and ``dhcp_scope`` rows we look up the live row to
    find its ``subnet_id``. On ``delete`` actions the live row is gone,
    so we fall back to the audit's ``old_value`` JSON if it carried a
    ``subnet_id``. Returns None when the subnet can't be identified
    — caller will skip the row.
    """
    try:
        rid_uuid = uuid.UUID(resource_id)
    except (ValueError, TypeError):
        return None

    if resource_type == "subnet":
        return await db.get(Subnet, rid_uuid)

    if resource_type == "ip_address":
        ip = await db.get(IPAddress, rid_uuid)
        if ip is not None:
            return await db.get(Subnet, ip.subnet_id)
        # Deleted — look in old_value.
        if old_value and "subnet_id" in old_value:
            try:
                sid = uuid.UUID(str(old_value["subnet_id"]))
            except (ValueError, TypeError):
                return None
            return await db.get(Subnet, sid)
        return None

    if resource_type == "dhcp_scope":
        scope = await db.get(DHCPScope, rid_uuid)
        if scope is not None and scope.subnet_id is not None:
            return await db.get(Subnet, scope.subnet_id)
        if old_value and "subnet_id" in old_value:
            try:
                sid = uuid.UUID(str(old_value["subnet_id"]))
            except (ValueError, TypeError):
                return None
            return await db.get(Subnet, sid)
        return None

    return None


async def _evaluate_compliance_change_rule(
    db: AsyncSession,
    rule: AlertRule,
    now: datetime,
) -> tuple[int, int, int, int, int]:
    """``compliance_change`` — fire one event per audit-log mutation
    against a subnet (or descendant IP / DHCP scope) whose
    classification flag matches ``rule.classification``.

    State model:

    * ``rule.last_scanned_audit_at`` is the high-water mark. NULL on
      a fresh rule means "never scanned" — we stamp it to ``now()``
      on the first pass so historical rows don't retro-fire when an
      operator first enables the rule.
    * Each audit row that matches opens one ``AlertEvent`` keyed by
      the audit row's UUID, so re-running the evaluator is idempotent.
    * Open events auto-resolve after
      ``_COMPLIANCE_CHANGE_AUTO_RESOLVE_HOURS``. Operators can also
      manually mark them resolved on the alerts page.

    Per-pass scan is capped at ``_COMPLIANCE_CHANGE_SCAN_LIMIT`` rows
    so a long-disabled rule flipping on doesn't pause the evaluator.
    """
    targets = await audit_forward._load_targets()  # noqa: SLF001

    opened = 0
    resolved = 0
    delivered_syslog = 0
    delivered_webhook = 0
    delivered_smtp = 0

    classification = rule.classification or ""
    if classification not in COMPLIANCE_CLASSIFICATIONS:
        logger.warning(
            "alert_compliance_unknown_classification",
            rule=str(rule.id),
            classification=classification,
        )
        return opened, resolved, delivered_syslog, delivered_webhook, delivered_smtp

    actions = _COMPLIANCE_CHANGE_SCOPE_ACTIONS.get(
        rule.change_scope or "any_change",
        _COMPLIANCE_CHANGE_SCOPE_ACTIONS["any_change"],
    )

    # Auto-resolve old open events for this rule.
    auto_resolve_cutoff = now - timedelta(hours=_COMPLIANCE_CHANGE_AUTO_RESOLVE_HOURS)
    open_res = await db.execute(
        select(AlertEvent).where(
            AlertEvent.rule_id == rule.id,
            AlertEvent.resolved_at.is_(None),
        )
    )
    for ev in open_res.scalars().all():
        if ev.fired_at < auto_resolve_cutoff:
            ev.resolved_at = now
            resolved += 1

    # Watermark — first run baselines to ``now`` and exits without
    # firing on history.
    if rule.last_scanned_audit_at is None:
        rule.last_scanned_audit_at = now
        return opened, resolved, delivered_syslog, delivered_webhook, delivered_smtp

    watermark = rule.last_scanned_audit_at

    audit_rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.timestamp > watermark)
                .where(AuditLog.action.in_(actions))
                .where(AuditLog.resource_type.in_(_COMPLIANCE_RESOURCE_TYPES))
                .where(AuditLog.result == "success")
                .order_by(AuditLog.timestamp)
                .limit(_COMPLIANCE_CHANGE_SCAN_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    if not audit_rows:
        return opened, resolved, delivered_syslog, delivered_webhook, delivered_smtp

    label = _CLASSIFICATION_LABEL.get(classification, classification)

    # Index existing events for this rule keyed by audit row UUID so
    # repeated passes don't double-fire. Compliance events use the
    # audit row's UUID as the subject_id, so the open-event index is
    # also the dedup index.
    existing_event_subjects = {
        ev.subject_id
        for ev in (await db.execute(select(AlertEvent).where(AlertEvent.rule_id == rule.id)))
        .scalars()
        .all()
    }

    last_seen_ts = watermark
    for row in audit_rows:
        last_seen_ts = row.timestamp

        if str(row.id) in existing_event_subjects:
            continue

        subnet = await _resolve_compliance_subnet(
            db,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            old_value=row.old_value if isinstance(row.old_value, dict) else None,
        )
        if subnet is None:
            continue
        if not getattr(subnet, classification, False):
            continue

        actor = row.user_display_name or "<system>"
        changed = (
            ", ".join(row.changed_fields)
            if isinstance(row.changed_fields, list) and row.changed_fields
            else ""
        )
        descriptor = f"{row.action}"
        if changed:
            descriptor = f"{row.action} ({changed})"

        display = f"{row.resource_type} {row.resource_display}"[:500]
        subnet_label = f"{subnet.network}"
        if subnet.name:
            subnet_label += f" ({subnet.name})"
        message = (
            f"{label}-scoped {row.resource_type} {row.resource_display} "
            f"in subnet {subnet_label} — {descriptor} by {actor}"
        )

        event = AlertEvent(
            rule_id=rule.id,
            subject_type=f"audit:{row.resource_type}",
            subject_id=str(row.id),
            subject_display=display,
            severity=rule.severity,
            message=message,
            fired_at=now,
            last_observed_value={
                "audit_id": str(row.id),
                "audit_timestamp": row.timestamp.isoformat(),
                "subnet_id": str(subnet.id),
                "classification": classification,
                "action": row.action,
                "actor": actor,
                "changed_fields": (
                    row.changed_fields if isinstance(row.changed_fields, list) else None
                ),
            },
        )
        db.add(event)
        await db.flush()
        ds, dw, dm = await _deliver(rule, event, targets)
        event.delivered_syslog = ds
        event.delivered_webhook = dw
        event.delivered_smtp = dm
        opened += 1
        if ds:
            delivered_syslog += 1
        if dw:
            delivered_webhook += 1
        if dm:
            delivered_smtp += 1

    # Advance watermark past the last row we examined regardless of
    # whether it matched — we don't want to re-scan the same window
    # next pass.
    rule.last_scanned_audit_at = last_seen_ts

    return opened, resolved, delivered_syslog, delivered_webhook, delivered_smtp


# Built-in compliance_change rules seeded on first start. Disabled by
# default — the operator opts in by flipping ``enabled`` on the row
# after wiring the audit-forward targets they want the alerts to fan
# out to. We deliberately avoid auto-creating these only when at
# least one classification flag is set, because that would create a
# chicken-and-egg problem where flipping the first PCI flag wouldn't
# also fire the rule on its own create event.
_COMPLIANCE_RULE_SEEDS: list[dict[str, Any]] = [
    {
        "name": "PCI scope changes",
        "description": (
            "Fires whenever a PCI-scoped subnet (or an IP / DHCP scope inside "
            "one) is created, updated, or deleted. Toggle on after configuring "
            "an audit-forward target to receive the events."
        ),
        "rule_type": RULE_TYPE_COMPLIANCE_CHANGE,
        "classification": "pci_scope",
        "change_scope": "any_change",
        "severity": "warning",
    },
    {
        "name": "HIPAA scope changes",
        "description": (
            "Fires whenever a HIPAA-scoped subnet (or an IP / DHCP scope inside "
            "one) is created, updated, or deleted."
        ),
        "rule_type": RULE_TYPE_COMPLIANCE_CHANGE,
        "classification": "hipaa_scope",
        "change_scope": "any_change",
        "severity": "warning",
    },
    {
        "name": "Internet-facing scope changes",
        "description": (
            "Fires whenever an internet-facing subnet (or an IP / DHCP scope "
            "inside one) is created, updated, or deleted."
        ),
        "rule_type": RULE_TYPE_COMPLIANCE_CHANGE,
        "classification": "internet_facing",
        "change_scope": "any_change",
        "severity": "warning",
    },
]


_AUDIT_CHAIN_RULE_NAME = "audit-chain-broken"


async def seed_audit_chain_alert_rule() -> None:
    """Seed the singleton ``audit-chain-broken`` rule (issue #73).

    Enabled by default — tampering is one of the few signals every
    deployment wants to know about; opt-out is for the rare operator
    who genuinely doesn't want it. Keyed on ``name`` since there's
    only one rule per platform.
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.alerts import AlertRule  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(AlertRule).where(AlertRule.name == _AUDIT_CHAIN_RULE_NAME)
        )
        if existing is not None:
            return
        session.add(
            AlertRule(
                name=_AUDIT_CHAIN_RULE_NAME,
                description=(
                    "Fires when the nightly audit-log chain verifier finds a "
                    "row whose hash doesn't match its predecessor — strong "
                    "evidence of tampering with the audit trail. Critical "
                    "severity by default; auto-resolves on the next pass "
                    "that finds the chain back in sync."
                ),
                rule_type=RULE_TYPE_AUDIT_CHAIN_BROKEN,
                severity="critical",
                enabled=True,
                notify_syslog=True,
                notify_webhook=True,
                notify_smtp=True,
            )
        )
        await session.commit()


async def seed_builtin_compliance_alert_rules() -> None:
    """Insert the three disabled compliance-change rules on first
    boot. Idempotent — only inserts a row when no rule with the same
    ``(rule_type, classification)`` pair already exists.

    Operators who toggle / rename / re-author one of these are never
    overridden, because the seed key is the ``classification`` value
    rather than ``name``. Renaming "PCI scope changes" → "PCI v4
    cardholder data audit hook" still suppresses the seed.
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415 — late import to dodge cycles

    async with AsyncSessionLocal() as session:
        for seed in _COMPLIANCE_RULE_SEEDS:
            existing = await session.scalar(
                select(AlertRule).where(
                    AlertRule.rule_type == seed["rule_type"],
                    AlertRule.classification == seed["classification"],
                )
            )
            if existing is not None:
                continue
            session.add(
                AlertRule(
                    name=seed["name"],
                    description=seed["description"],
                    rule_type=seed["rule_type"],
                    classification=seed["classification"],
                    change_scope=seed["change_scope"],
                    severity=seed["severity"],
                    enabled=False,
                    notify_syslog=True,
                    notify_webhook=True,
                    notify_smtp=False,
                )
            )
        await session.commit()


# ── Delivery ───────────────────────────────────────────────────────────────


def _severity_to_syslog(severity: str) -> int:
    """Map alert severity → RFC 5424 severity (mirrors audit_forward)."""
    if severity == "critical":
        return 2  # crit
    if severity == "warning":
        return 4  # warning
    return 6  # info


async def _deliver(
    rule: AlertRule,
    event: AlertEvent,
    targets: list[dict[str, Any]],
) -> tuple[bool, bool, bool]:
    """Fan an event out to every audit-forward target whose ``kind``
    matches an enabled rule channel. Returns
    ``(delivered_syslog, delivered_webhook, delivered_smtp)`` as
    booleans suitable for stamping onto the event row.

    Per-target ``min_severity`` / ``resource_types`` filters still
    apply via ``_deliver_to_target``. A dead target isolates to its
    own row; the others still see the event.
    """
    delivered_syslog = False
    delivered_webhook = False
    delivered_smtp = False

    payload: dict[str, Any] = {
        "kind": "alert",
        "rule_id": str(rule.id),
        "rule_name": rule.name,
        "rule_type": rule.rule_type,
        "severity": event.severity,
        "fired_at": event.fired_at.isoformat(),
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "subject_display": event.subject_display,
        "message": event.message,
    }

    for target in targets:
        kind = target.get("kind")
        if kind == "syslog" and not rule.notify_syslog:
            continue
        if kind == "webhook" and not rule.notify_webhook:
            continue
        if kind == "smtp" and not rule.notify_smtp:
            continue
        try:
            await audit_forward._deliver_to_target(target, payload)  # noqa: SLF001
            if kind == "syslog":
                delivered_syslog = True
            elif kind == "webhook":
                delivered_webhook = True
            elif kind == "smtp":
                delivered_smtp = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "alert_deliver_failed",
                rule=str(rule.id),
                event=str(event.id),
                target=target.get("name"),
                kind=kind,
                error=str(exc),
            )

    return delivered_syslog, delivered_webhook, delivered_smtp


# ── Main entry point ───────────────────────────────────────────────────────


async def evaluate_all(db: AsyncSession) -> dict[str, int]:
    """Evaluate every enabled rule; open / resolve events as needed.

    Returns a summary dict for the scheduled-task audit row: opened,
    resolved, delivered_syslog, delivered_webhook. Per-rule failures are
    logged but don't abort the pass — one broken rule shouldn't silence
    the rest.
    """
    settings = await db.get(PlatformSettings, 1)
    targets = await audit_forward._load_targets()  # noqa: SLF001

    # Alerts have their own enabled toggle per rule; we still rely on
    # audit-forward's target table for actual delivery. With no targets
    # configured the event is recorded but goes nowhere — still visible
    # in the /alerts UI.
    now = datetime.now(UTC)

    opened = 0
    resolved = 0
    delivered_syslog = 0
    delivered_webhook = 0
    delivered_smtp = 0

    res = await db.execute(select(AlertRule).where(AlertRule.enabled.is_(True)))
    rules = list(res.scalars().all())
    for rule in rules:
        try:
            # Each match tuple is (subject_id, display, message,
            # severity_override). Threshold-style rules pass
            # severity_override=None so the rule's own severity
            # applies; ``domain_expiring`` overrides per-row based on
            # how close the actual expiry is.
            matches: list[tuple[str, str, str, str | None]] = []

            if rule.rule_type == RULE_TYPE_SUBNET_UTILIZATION:
                base = await _matching_subnet_subjects(db, rule, settings)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "subnet"
            elif rule.rule_type == RULE_TYPE_VOICE_LEASE_COUNT_BELOW:
                base = await _matching_voice_lease_count_below_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "subnet"
            elif rule.rule_type == RULE_TYPE_SERVER_UNREACHABLE:
                base = await _matching_server_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "server"
            elif rule.rule_type == RULE_TYPE_ASN_HOLDER_DRIFT:
                matches = await _matching_asn_drift_subjects(db, rule)
                subject_type = "asn"
            elif rule.rule_type == RULE_TYPE_ASN_WHOIS_UNREACHABLE:
                matches = await _matching_asn_unreachable_subjects(db, rule)
                subject_type = "asn"
            elif rule.rule_type == RULE_TYPE_RPKI_ROA_EXPIRING:
                matches = await _matching_rpki_roa_expiring_subjects(db, rule)
                subject_type = "rpki_roa"
            elif rule.rule_type == RULE_TYPE_RPKI_ROA_EXPIRED:
                matches = await _matching_rpki_roa_expired_subjects(db, rule)
                subject_type = "rpki_roa"
            elif rule.rule_type == RULE_TYPE_DOMAIN_EXPIRING:
                expiring = await _matching_domain_expiring_subjects(db, rule, now)
                matches = [(sid, disp, msg, sev) for sid, disp, msg, sev in expiring]
                subject_type = "domain"
            elif rule.rule_type == RULE_TYPE_DOMAIN_NS_DRIFT:
                drift = await _matching_domain_drift_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in drift]
                subject_type = "domain"
            elif rule.rule_type == RULE_TYPE_CIRCUIT_TERM_EXPIRING:
                expiring = await _matching_circuit_term_expiring_subjects(db, rule, now)
                matches = [(sid, disp, msg, sev) for sid, disp, msg, sev in expiring]
                subject_type = "circuit"
            elif rule.rule_type == RULE_TYPE_SERVICE_TERM_EXPIRING:
                expiring = await _matching_service_term_expiring_subjects(db, rule, now)
                matches = [(sid, disp, msg, sev) for sid, disp, msg, sev in expiring]
                subject_type = "network_service"
            elif rule.rule_type == RULE_TYPE_SERVICE_RESOURCE_ORPHANED:
                orphans = await _matching_service_resource_orphaned_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in orphans]
                subject_type = "network_service_resource"
            elif rule.rule_type == RULE_TYPE_CIRCUIT_STATUS_CHANGED:
                # Transition-style rule with its own evaluator that
                # latches ``(from, to, changed_at)`` snapshots and
                # auto-resolves after ``_TRANSITION_AUTO_RESOLVE_DAYS``.
                op_, res_, dsy, dwh, dsm = await _evaluate_circuit_status_changed_rule(
                    db, rule, now
                )
                opened += op_
                resolved += res_
                delivered_syslog += dsy
                delivered_webhook += dwh
                delivered_smtp += dsm
                continue
            elif rule.rule_type == RULE_TYPE_COMPLIANCE_CHANGE:
                # Audit-log-driven; opens one event per matching audit
                # row with its own auto-resolve window. Watermark stored
                # on the rule itself.
                op_, res_, dsy, dwh, dsm = await _evaluate_compliance_change_rule(db, rule, now)
                opened += op_
                resolved += res_
                delivered_syslog += dsy
                delivered_webhook += dwh
                delivered_smtp += dsm
                continue
            elif rule.rule_type in (
                RULE_TYPE_DOMAIN_REGISTRAR_CHANGED,
                RULE_TYPE_DOMAIN_DNSSEC_CHANGED,
            ):
                # Transition-once rules don't fit the open/resolve
                # symmetry — they have their own evaluator that
                # latches snapshots into AlertEvent.last_observed_value
                # and auto-resolves after _TRANSITION_AUTO_RESOLVE_DAYS.
                field_name, label = (
                    ("registrar", "registrar")
                    if rule.rule_type == RULE_TYPE_DOMAIN_REGISTRAR_CHANGED
                    else ("dnssec_signed", "DNSSEC status")
                )
                op_, res_, dsy, dwh, dsm = await _evaluate_domain_transition_rule(
                    db,
                    rule,
                    field_name=field_name,
                    rule_label=label,
                    now=now,
                )
                opened += op_
                resolved += res_
                delivered_syslog += dsy
                delivered_webhook += dwh
                delivered_smtp += dsm
                continue
            else:
                logger.warning("alert_unknown_rule_type", rule=str(rule.id), type=rule.rule_type)
                continue

            # Index current open events by subject_id for this rule.
            open_res = await db.execute(
                select(AlertEvent).where(
                    AlertEvent.rule_id == rule.id,
                    AlertEvent.resolved_at.is_(None),
                )
            )
            open_events = list(open_res.scalars().all())
            open_by_subject = {ev.subject_id: ev for ev in open_events}

            match_ids = {sid for sid, _, _, _ in matches}

            # Open new events for unseen matches.
            for subject_id, display, message, severity_override in matches:
                if subject_id in open_by_subject:
                    continue
                event = AlertEvent(
                    rule_id=rule.id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    subject_display=display,
                    severity=severity_override or rule.severity,
                    message=message,
                    fired_at=now,
                )
                db.add(event)
                await db.flush()  # populate event.id for delivery payload
                ds, dw, dm = await _deliver(rule, event, targets)
                event.delivered_syslog = ds
                event.delivered_webhook = dw
                event.delivered_smtp = dm
                opened += 1
                if ds:
                    delivered_syslog += 1
                if dw:
                    delivered_webhook += 1
                if dm:
                    delivered_smtp += 1

            # Resolve open events whose subject no longer matches.
            for subject_id, event in open_by_subject.items():
                if subject_id in match_ids:
                    continue
                event.resolved_at = now
                resolved += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "alert_rule_eval_failed",
                rule=str(rule.id),
                rule_type=rule.rule_type,
                error=str(exc),
            )

    await db.commit()
    return {
        "opened": opened,
        "resolved": resolved,
        "delivered_syslog": delivered_syslog,
        "delivered_webhook": delivered_webhook,
        "delivered_smtp": delivered_smtp,
    }
