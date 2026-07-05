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
from app.models.bgp_looking_glass import BGPLGPeer, BGPLGRoute, LookingGlassCollector
from app.models.bgp_monitor import BGPHijackDetection, BGPTrackedPrefix
from app.models.circuit import Circuit
from app.models.dhcp import (
    DHCPLease,
    DHCPObservedResponder,
    DHCPPool,
    DHCPScope,
    DHCPServer,
    RAObservedRouter,
)
from app.models.dns import DNSServer, DNSZone
from app.models.domain import Domain
from app.models.ipam import IPAddress, IPBlock, IpMacHistory, Subnet
from app.models.metrics import DNSMetricSample
from app.models.network_service import NetworkService, NetworkServiceResource
from app.models.overlay import OverlayNetwork
from app.models.ownership import Site
from app.models.settings import PlatformSettings
from app.models.tls_cert import (
    STATE_MISMATCH,
    STATE_UNREACHABLE,
    TLSCertTarget,
)
from app.models.vrf import VRF
from app.services import audit_forward
from app.services.bgp.hijack_monitor import (
    RPKI_INVALID,
    expected_origin_set,
    severity_for_rpki,
)

logger = structlog.get_logger(__name__)


RULE_TYPE_SUBNET_UTILIZATION = "subnet_utilization"
RULE_TYPE_SERVER_UNREACHABLE = "server_unreachable"
# ASN / RPKI rule types — Phase 2 of issue #85.
RULE_TYPE_ASN_HOLDER_DRIFT = "asn_holder_drift"
RULE_TYPE_ASN_WHOIS_UNREACHABLE = "asn_whois_unreachable"
RULE_TYPE_RPKI_ROA_EXPIRING = "rpki_roa_expiring"
RULE_TYPE_RPKI_ROA_EXPIRED = "rpki_roa_expired"
# BGP prefix-hijack rule types — issue #527. Backed by the
# ``bgp_hijack_detection`` latch table populated by
# ``app.tasks.bgp_hijack_poll`` (+ the optional RIS Live consumer). The
# matcher reads active (unresolved, unacknowledged) detection rows;
# per-detection severity (critical for RPKI-invalid, warning for
# RPKI-unknown) rides through as a severity override.
RULE_TYPE_BGP_PREFIX_HIJACK = "bgp_prefix_hijack"
RULE_TYPE_BGP_MORE_SPECIFIC = "bgp_more_specific_announced"
# BGP Looking Glass internal-RIB alert family — issue #566 Phase 5.
# Companion to the #527 public-table hijack monitor above: that watches
# the public routing table via RIPEstat/RIS Live, these watch the
# operator's OWN live table via the receive-only Looking Glass
# collector (bgp_lg_peer / bgp_lg_route). unexpected_origin and
# more_specific deliberately reuse the SAME BGPTrackedPrefix config
# table #527 already has (an operator's "prefixes I own + expected
# origin ASN" list, curated once at /asns/{id}/tracked-prefixes) —
# both the external and internal monitors read it.
RULE_TYPE_BGP_LG_SESSION_DOWN = "bgp_lg_session_down"
RULE_TYPE_BGP_LG_RPKI_INVALID_ROUTE = "bgp_lg_rpki_invalid_route"
RULE_TYPE_BGP_LG_UNEXPECTED_ORIGIN = "bgp_lg_unexpected_origin"
RULE_TYPE_BGP_LG_MORE_SPECIFIC = "bgp_lg_more_specific"
RULE_TYPE_BGP_LG_ROUTE_FLAP = "bgp_lg_route_flap"
RULE_TYPE_BGP_LG_MISSING_ADVERTISEMENT = "bgp_lg_missing_advertisement"
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
# Issue #565 — the Celery worker/beat found the DB schema behind the
# bundled Alembic head ("code deployed before migrate ran"). Subject =
# the platform (a single singleton event). Managed directly by the
# ``app.tasks.schema_check`` periodic task (like ``audit_chain_broken``
# above), not the generic evaluator — the task opens/resolves the
# event itself off the version-vs-head comparison.
RULE_TYPE_SCHEMA_BEHIND_HEAD = "schema_behind_head"
# Voice-VLAN client-count drop — issue #112 phase 2. Counts active
# DHCP leases on every subnet tagged ``subnet_role='voice'``; fires
# when the count drops below ``threshold_percent`` (reused as a raw
# count threshold for this rule type — operators set it to e.g. 10
# meaning "alert me when fewer than 10 phones are reachable").
RULE_TYPE_VOICE_LEASE_COUNT_BELOW = "voice_lease_count_below"

# Issue #183 Phase 6 — k3s server cert expiry. Subject = appliance.
# Same threshold-escalation shape as ``circuit_term_expiring`` /
# ``domain_expiring``: warning at threshold_days, escalating to
# critical as expiry approaches. Default threshold 30 d.
RULE_TYPE_K3S_API_CERT_EXPIRING = "k3s_api_cert_expiring"

# Address-space hygiene — issue #45. Fires when a subnet holds more than
# ``threshold_percent`` (re-used as a raw count) allocated IPs whose
# ``last_seen_at`` is older than ``threshold_days`` (default 90). The
# companion to the Stale-IP report: the report is the operator-driven
# drilldown, this is the passive "your hygiene is slipping" feed.
RULE_TYPE_STALE_IP_COUNT = "stale_ip_count"

# A dynamic DHCP pool whose live occupancy has reached ``threshold_percent``
# (assigned ÷ range size) OR whose free-address count has dropped below
# ``min_free_addresses``. Orthogonal to ``subnet_utilization`` — that counts
# allocated IPAM rows, this counts active DHCP leases inside the pool range,
# so a pool can be exhausted (clients failing to get a lease) while the IPAM
# subnet shows low allocated-row utilisation. Issue #339.
RULE_TYPE_DHCP_POOL_EXHAUSTION = "dhcp_pool_exhaustion"

# Issue #285 Phase 2d — fleet firewall drift. Subject = appliance. Fires
# when the control-plane-rendered firewall hash (FirewallApplyState.
# rendered_hash) hasn't been applied by the host runner (applied_hash)
# past a grace window, AND the node's last apply was a clean ``ok`` — so
# it's a genuine stall, NOT an apply error (its own ``error:*`` chip) or a
# deliberate auto-revert (``reverted`` — alarming on those would never
# resolve since applied_hash != rendered_hash permanently). Distinct from
# agent-offline: the message cross-references the supervisor's last_seen_at
# to say whether the supervisor itself is stale or just the host runner.
RULE_TYPE_FIREWALL_APPLY_STALLED = "firewall.apply_stalled"

# Issue #76 — internal cert / API-token / secret expiry. One rule spans
# multiple credential tables (supervisor mTLS certs + API tokens with an
# expiry), so the subject_type is the generic "secret" and the subject_id
# encodes the source + row id (``appliance_cert:<id>`` / ``api_token:<id>``)
# to keep each credential's event distinct. Severity escalates like the
# other ``*_expiring`` rules (threshold/4 → warning, threshold/12 →
# critical). Catches the "we forgot to rotate" 3am-page failure mode.
RULE_TYPE_SECRET_EXPIRING = "secret_expiring"

# Issue #46 — planned-decommission awareness. Subject = subnet. Fires
# when a subnet's ``decom_date`` falls within ``threshold_days`` (default
# 30). Same threshold-escalation shape as the other ``*_expiring`` rules
# (warning at threshold/4 → critical at threshold/12); a past-due decom
# date (negative days) is always critical. Catches the "we scheduled this
# segment for retirement and forgot" failure mode.
RULE_TYPE_DECOM_EXPIRING = "decom_expiring"

# DNS query-behaviour anomalies — issue #371. Subject = dns_server. Evaluated
# on the 60 s tick against the per-server ``dns_metric_sample`` rcode deltas
# the agents already report (no new collection). Both reuse the generic
# AlertRule int columns instead of a bespoke window column:
#   * ``dns_nxdomain_spike`` — fires when, over the trailing window, a server's
#     NXDOMAIN ratio (nxdomain ÷ queries_total) reaches ``threshold_percent``
#     AND the absolute NXDOMAIN count reaches ``min_free_addresses`` (the
#     low-traffic guard, so a server answering 3 queries / 2 NXDOMAIN doesn't
#     page). Catches DGA beacons / broken-client search-domain storms.
#   * ``dns_query_rate_spike`` — fires when the trailing window's query total
#     exceeds the prior equal-length window by ``threshold_percent`` AND clears
#     the ``min_free_addresses`` absolute floor (so tiny servers don't page on
#     a 3→9 query "300% spike"). A cold prior window counts as a spike once the
#     floor is cleared.
# The window itself is a fixed module constant (not operator-tunable in v1) —
# same approach as the firewall / transition rules' fixed grace windows.
RULE_TYPE_DNS_NXDOMAIN_SPIKE = "dns_nxdomain_spike"
RULE_TYPE_DNS_QUERY_RATE_SPIKE = "dns_query_rate_spike"
# Response Rate Limiting actively dropping (#146 Phase 3). Subject =
# dns_server. Open-while-true: fires when RateDropped summed over the window
# clears the floor, auto-resolves when the flood subsides.
RULE_TYPE_DNS_RATE_LIMIT_DROPPING = "dns_rate_limit_dropping"

# Active IP reconciliation hygiene alerts — issue #369. Subject = ip_address.
# Reuse the on-the-wire liveness signal (IPAddress.last_seen_at) the discovery
# sweep + SNMP poll already write + the ip_mac_history observation log; no new
# collectors. The window for each is ``threshold_days`` (reused).
#   * ip_free_but_responding — an 'available' row that answered within the last
#     threshold_days (default 1). "IPAM says free, host is up."
#   * stale_reservation — a 'reserved'/'static_dhcp' row last seen > threshold_days
#     ago (default 90). The gap stale_ip_count deliberately leaves (allocated-only).
#   * unknown_mac_in_static_range — a 'reserved'/'static_dhcp' row whose
#     ip_mac_history holds a recently-observed (≤ threshold_days, default 7) MAC
#     differing from the recorded one — a squat.
RULE_TYPE_IP_FREE_BUT_RESPONDING = "ip_free_but_responding"
RULE_TYPE_STALE_RESERVATION = "stale_reservation"
RULE_TYPE_UNKNOWN_MAC_IN_STATIC_RANGE = "unknown_mac_in_static_range"

# Rogue DHCP server detection — issue #370. Subject = dhcp_responder. Fires on
# dhcp_observed_responder rows classified ``rogue`` (a DHCP server answering on
# a managed segment that isn't a known group member and isn't allowlisted),
# observed within ``threshold_days`` (default 1). The agent's active probe is
# opt-in, so this only has data on segments running the probe.
RULE_TYPE_ROGUE_DHCP = "rogue_dhcp"
_ROGUE_DHCP_RECENCY_DAYS = 1

# Rogue IPv6 Router-Advertisement detection — issue #524. Subject = ra_router.
# Fires on ra_observed_router rows classified ``rogue`` (an RA source that
# isn't on the group's expected-router allowlist), observed within
# ``threshold_days`` (default 1). The agent's passive RA sniffer is opt-in, so
# this only has data on segments running the sniffer.
RULE_TYPE_ROGUE_RA = "rogue_ra"
_ROGUE_RA_RECENCY_DAYS = 1

# New-device (arpwatch) detection — issue #459. Subject = ip_mac_observation
# (composite ``ip_id:mac``). Fires on ip_mac_history rows classified ``new``
# (a MAC never seen before, not allowlisted, not on the known fleet) observed
# within ``threshold_days`` (default 7). Locally-administered (randomised) MACs
# are excluded by default to avoid a reconnection storm — set the rule's
# ``classification`` to ``"all"`` to include them. Auto-resolves once the MAC is
# acknowledged / allowlisted (reclassified) or ages out of the window.
RULE_TYPE_NEW_MAC_SEEN = "new_mac_seen"
_NEW_MAC_SEEN_RECENCY_DAYS = 7

# TLS certificate monitoring — issue #118. Subject = tls_cert (one per
# tls_cert_target). ``tls_cert_expiring`` is a standard escalating-expiry
# rule (info → warning → critical as not_after nears, like domain_expiring);
# ``tls_cert_chain_invalid`` / ``tls_cert_unreachable`` are standard
# open-while-true rules; ``tls_cert_changed`` is a transition-once rule that
# latches the fingerprint pair and auto-resolves after the window.
RULE_TYPE_TLS_CERT_EXPIRING = "tls_cert_expiring"
RULE_TYPE_TLS_CERT_CHAIN_INVALID = "tls_cert_chain_invalid"
RULE_TYPE_TLS_CERT_UNREACHABLE = "tls_cert_unreachable"
RULE_TYPE_TLS_CERT_CHANGED = "tls_cert_changed"
# Cert-rotation deviation (#118 Phase 3) — the issuing CA changed (a
# normally-ACME cert coming back from a different issuer); transition-once.
RULE_TYPE_TLS_CERT_ISSUER_CHANGED = "tls_cert_issuer_changed"
# DNSBL / RBL reputation (#528) — recurring-condition latch: fires while a
# public-facing IP is listed on ≥1 enabled blocklist, auto-resolves when the
# sweep finds it delisted (the shared open/resolve loop handles both).
RULE_TYPE_IP_BLOCKLISTED = "ip_blocklisted"
# Unreachable only pages after a couple of consecutive failures so a
# single transient handshake blip doesn't fire.
_TLS_CERT_UNREACHABLE_MIN_FAILURES = 2

RULE_TYPES = frozenset(
    {
        RULE_TYPE_SUBNET_UTILIZATION,
        RULE_TYPE_SERVER_UNREACHABLE,
        RULE_TYPE_ASN_HOLDER_DRIFT,
        RULE_TYPE_ASN_WHOIS_UNREACHABLE,
        RULE_TYPE_RPKI_ROA_EXPIRING,
        RULE_TYPE_RPKI_ROA_EXPIRED,
        RULE_TYPE_BGP_PREFIX_HIJACK,
        RULE_TYPE_BGP_MORE_SPECIFIC,
        RULE_TYPE_BGP_LG_SESSION_DOWN,
        RULE_TYPE_BGP_LG_RPKI_INVALID_ROUTE,
        RULE_TYPE_BGP_LG_UNEXPECTED_ORIGIN,
        RULE_TYPE_BGP_LG_MORE_SPECIFIC,
        RULE_TYPE_BGP_LG_ROUTE_FLAP,
        RULE_TYPE_BGP_LG_MISSING_ADVERTISEMENT,
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
        RULE_TYPE_SCHEMA_BEHIND_HEAD,
        RULE_TYPE_VOICE_LEASE_COUNT_BELOW,
        RULE_TYPE_K3S_API_CERT_EXPIRING,
        RULE_TYPE_STALE_IP_COUNT,
        RULE_TYPE_DHCP_POOL_EXHAUSTION,
        RULE_TYPE_FIREWALL_APPLY_STALLED,
        RULE_TYPE_SECRET_EXPIRING,
        RULE_TYPE_DECOM_EXPIRING,
        RULE_TYPE_DNS_NXDOMAIN_SPIKE,
        RULE_TYPE_DNS_QUERY_RATE_SPIKE,
        RULE_TYPE_DNS_RATE_LIMIT_DROPPING,
        RULE_TYPE_IP_FREE_BUT_RESPONDING,
        RULE_TYPE_STALE_RESERVATION,
        RULE_TYPE_UNKNOWN_MAC_IN_STATIC_RANGE,
        RULE_TYPE_ROGUE_DHCP,
        RULE_TYPE_ROGUE_RA,
        RULE_TYPE_NEW_MAC_SEEN,
        RULE_TYPE_TLS_CERT_EXPIRING,
        RULE_TYPE_TLS_CERT_CHAIN_INVALID,
        RULE_TYPE_TLS_CERT_UNREACHABLE,
        RULE_TYPE_TLS_CERT_CHANGED,
        RULE_TYPE_TLS_CERT_ISSUER_CHANGED,
        RULE_TYPE_IP_BLOCKLISTED,
    }
)

# IP-hygiene window defaults (issue #369), each reused as ``threshold_days``.
_FREE_RESPONDING_RECENCY_DAYS = 1
_STALE_RESERVATION_DAYS = 90
_SQUAT_RECENCY_DAYS = 7
# Defensive cap on per-IP hygiene events opened per tick — a badly-misconfigured
# discovery run shouldn't open thousands of AlertEvents in one 60 s pass. The
# matcher logs when it truncates (no silent cap).
_IP_HYGIENE_MAX_EVENTS = 500

# DNS query-anomaly evaluation window + defaults (issue #371). 15 min spans
# ~15 one-minute buckets / 3 five-minute buckets — long enough to smooth a
# single noisy bucket, short enough to page within a quarter hour.
_DNS_ANOMALY_WINDOW = timedelta(minutes=15)
_DNS_NXDOMAIN_RATIO_DEFAULT = 40  # % of queries that are NXDOMAIN
_DNS_NXDOMAIN_MIN_COUNT_DEFAULT = 200  # absolute NXDOMAIN floor over the window
_DNS_QUERY_RATE_SPIKE_PCT_DEFAULT = 200  # current ≥ prior × (1 + 200%) = ×3
_DNS_QUERY_RATE_MIN_DEFAULT = 1000  # absolute query floor over the window
# RRL actively-dropping floor (#146 Phase 3): RateDropped summed over the
# window must clear this to fire — a sustained drop stream means the server
# is shedding a flood, i.e. likely under attack. Below it, a few drops from
# an over-eager client are just noise.
_DNS_RATE_LIMIT_DROP_MIN_DEFAULT = 100

# Issue #285 Phase 2d — how long a control-plane-rendered firewall hash may
# go un-applied (ok-status) before it's "stalled". Comfortably larger than
# the worst-case render→heartbeat→apply→report round-trip (1-2 heartbeats),
# so the normal one-tick lag never alarms. Anchored on a ``stalled_since``
# watermark the matcher stamps on first observation (NOT last_rendered_at,
# which the server bumps every heartbeat).
_FIREWALL_STALE_GRACE = timedelta(minutes=3)
_FIREWALL_APPLY_STALLED_RULE_NAME = "Firewall apply stalled"

# Default stale-IP alert params when the rule doesn't pin them.
_STALE_IP_DEFAULT_COUNT_THRESHOLD = 10
_STALE_IP_DEFAULT_DAYS = 90

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

# BGP Looking Glass alert-family constants (issue #566 Phase 5).
# Fixed windows, not operator-tunable columns — same precedent as
# _FIREWALL_STALE_GRACE / _DNS_ANOMALY_WINDOW above.
#
# session_down: a peer flapping momentarily (TCP reset, brief
# reconnect) shouldn't page; only a sustained down state does. Anchored
# on BGPLGPeer.down_since, which THIS module stamps (see
# _matching_bgp_lg_session_down_subjects) — mirrors
# _FIREWALL_STALE_GRACE's stalled_since pattern exactly.
_BGP_LG_SESSION_DOWN_GRACE = timedelta(minutes=2)

# route_flap: a route counts as "flapping" once its lifetime
# withdraw-count crosses the floor AND the most recent flap was within
# this trailing window — so a route that flapped a lot long ago but has
# been stable since ages out instead of paging forever.
_BGP_LG_FLAP_WINDOW = timedelta(minutes=10)
_BGP_LG_FLAP_COUNT_DEFAULT = 5

# Defensive cap mirroring _IP_HYGIENE_MAX_EVENTS — a freshly-enabled
# rule against a large RIB shouldn't open thousands of AlertEvents in
# one 60s tick. Logs a warning when truncated (no silent cap).
_BGP_LG_MAX_EVENTS = 500

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


async def _matching_dhcp_pool_exhaustion_subjects(
    db: AsyncSession,
    rule: AlertRule,
) -> list[tuple[str, str, str]]:
    """Dynamic DHCP pools that have hit ``threshold_percent`` occupancy OR
    dropped below ``min_free_addresses`` free addresses (issue #339).

    Occupancy is live, from active ``DHCPLease`` rows inside the pool range
    (see :func:`app.services.dhcp.pool_occupancy.compute_pool_occupancy_batch`),
    so it works for Kea and Windows alike. Only ``pool_type='dynamic'`` pools
    are considered — excluded / reserved ranges never hand out leases. With
    neither threshold set the rule defaults to 90% occupancy so a bare
    enable still does something sensible.
    """
    from app.services.dhcp.pool_occupancy import compute_pool_occupancy_batch

    pct_threshold = rule.threshold_percent
    min_free = rule.min_free_addresses
    if pct_threshold is None and min_free is None:
        pct_threshold = 90

    pools = list(
        (await db.execute(select(DHCPPool).where(DHCPPool.pool_type == "dynamic"))).scalars().all()
    )
    if not pools:
        return []

    # Resolve scope display names in one query (pool → scope name).
    scope_ids = {p.scope_id for p in pools}
    scope_rows = (
        await db.execute(select(DHCPScope.id, DHCPScope.name).where(DHCPScope.id.in_(scope_ids)))
    ).all()
    scope_names: dict[uuid.UUID, str] = {row[0]: row[1] for row in scope_rows}

    # One batched lease query for all pools rather than one per pool (N+1).
    occ_by_pool = await compute_pool_occupancy_batch(db, pools)

    matches: list[tuple[str, str, str]] = []
    for pool in pools:
        occ = occ_by_pool[pool.id]
        if occ.total <= 0:
            continue
        over_pct = pct_threshold is not None and occ.percent >= pct_threshold
        under_free = min_free is not None and occ.free < min_free
        if not (over_pct or under_free):
            continue
        scope_name = scope_names.get(pool.scope_id) or ""
        pool_label = pool.name or f"{pool.start_ip}–{pool.end_ip}"
        display = pool_label + (f" ({scope_name})" if scope_name else "")
        reasons = []
        if over_pct:
            reasons.append(f"{occ.percent:.1f}% occupied (threshold {pct_threshold}%)")
        if under_free:
            reasons.append(f"{occ.free} free (floor {min_free})")
        message = (
            f"DHCP pool {display} — {', '.join(reasons)}; "
            f"{occ.assigned}/{occ.total} addresses leased"
        )
        matches.append((str(pool.id), display, message))
    return matches


async def _matching_stale_ip_count_subjects(
    db: AsyncSession,
    rule: AlertRule,
) -> list[tuple[str, str, str]]:
    """Subnets holding ≥ ``threshold_percent`` (raw count) allocated IPs
    whose ``last_seen_at`` is older than ``threshold_days`` (default 90).

    Reads the same discovery (#23) liveness signal the Stale-IP report
    uses. ``include_never_seen`` is intentionally off for the alert —
    never-seen rows are noisy (often in discovery-disabled subnets), so
    the alert fires only on the high-confidence "seen, then went dark"
    signal. Operators chase the full list, including never-seen, from the
    report page.
    """
    from app.services.ipam.stale_ips import count_stale_per_subnet

    threshold = (
        int(rule.threshold_percent)
        if rule.threshold_percent is not None
        else _STALE_IP_DEFAULT_COUNT_THRESHOLD
    )
    stale_days = (
        int(rule.threshold_days) if rule.threshold_days is not None else _STALE_IP_DEFAULT_DAYS
    )
    counts = await count_stale_per_subnet(db, stale_days=stale_days, include_never_seen=False)
    over = {sid: n for sid, n in counts.items() if n >= max(1, threshold)}
    if not over:
        return []

    subnets = list(
        (await db.execute(select(Subnet).where(Subnet.id.in_(over.keys())))).scalars().all()
    )
    matches: list[tuple[str, str, str]] = []
    for s in subnets:
        n = over[s.id]
        display = f"{s.network}" + (f" — {s.name}" if s.name else "")
        message = (
            f"Subnet {display} has {n} stale allocated IP(s) not seen on the "
            f"wire in {stale_days}+ days (threshold {threshold}) — review for "
            f"deprecation from the Stale-IP report"
        )
        matches.append((str(s.id), display, message))
    return matches


async def _matching_ip_free_but_responding_subjects(
    db: AsyncSession,
    rule: AlertRule,
) -> list[tuple[str, str, str]]:
    """``available`` IPs that answered on the wire within the recency window
    (issue #369, case 1) — "IPAM says free, host is up"."""
    days = rule.threshold_days if rule.threshold_days is not None else _FREE_RESPONDING_RECENCY_DAYS
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    rows = (
        (
            await db.execute(
                select(IPAddress)
                .where(
                    IPAddress.status == "available",
                    IPAddress.last_seen_at.is_not(None),
                    IPAddress.last_seen_at >= cutoff,
                )
                .limit(_IP_HYGIENE_MAX_EVENTS)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) >= _IP_HYGIENE_MAX_EVENTS:
        logger.warning("ip_free_but_responding_truncated", cap=_IP_HYGIENE_MAX_EVENTS)
    matches: list[tuple[str, str, str]] = []
    for r in rows:
        via = f" via {r.last_seen_method}" if r.last_seen_method else ""
        message = (
            f"IP {r.address} is marked 'available' but answered on the wire{via} "
            f"at {r.last_seen_at.isoformat() if r.last_seen_at else '?'} (within "
            f"{days}d) — reclaim it as allocated/discovered or investigate the host."
        )
        matches.append((str(r.id), str(r.address), message))
    return matches


async def _matching_stale_reservation_subjects(
    db: AsyncSession,
    rule: AlertRule,
) -> list[tuple[str, str, str]]:
    """``reserved`` / ``static_dhcp`` IPs not seen within ``threshold_days``
    (issue #369, case 2). The gap ``stale_ip_count`` leaves (allocated-only).
    High-confidence: requires the row to have been seen at least once."""
    days = rule.threshold_days if rule.threshold_days is not None else _STALE_RESERVATION_DAYS
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    rows = (
        (
            await db.execute(
                select(IPAddress)
                .where(
                    IPAddress.status.in_(("reserved", "static_dhcp")),
                    IPAddress.last_seen_at.is_not(None),
                    IPAddress.last_seen_at < cutoff,
                )
                .limit(_IP_HYGIENE_MAX_EVENTS)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) >= _IP_HYGIENE_MAX_EVENTS:
        logger.warning("stale_reservation_truncated", cap=_IP_HYGIENE_MAX_EVENTS)
    matches: list[tuple[str, str, str]] = []
    for r in rows:
        label = str(r.address) + (f" ({r.hostname})" if r.hostname else "")
        message = (
            f"{r.status} IP {label} hasn't been seen on the wire since "
            f"{r.last_seen_at.isoformat() if r.last_seen_at else '?'} (> {days}d) — "
            f"verify the host still exists or release the reservation."
        )
        matches.append((str(r.id), str(r.address), message))
    return matches


async def _matching_unknown_mac_in_static_range_subjects(
    db: AsyncSession,
    rule: AlertRule,
) -> list[tuple[str, str, str]]:
    """``reserved`` / ``static_dhcp`` IPs whose ip_mac_history holds a recently
    observed MAC differing from the recorded one (issue #369, case 3) — a squat.

    The discovery sweep + SNMP poll log every observed MAC into ip_mac_history
    (see services.ipam.discovery.record_mac_observation); operator-set
    ``mac_address`` is never overwritten, so a differing recent history row is
    a genuine "someone else is answering on this IP" signal.
    """
    days = rule.threshold_days if rule.threshold_days is not None else _SQUAT_RECENCY_DAYS
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    rows = (
        await db.execute(
            select(IPAddress, IpMacHistory.mac_address, IpMacHistory.last_seen)
            .join(IpMacHistory, IpMacHistory.ip_address_id == IPAddress.id)
            .where(
                IPAddress.status.in_(("reserved", "static_dhcp")),
                IPAddress.mac_address.is_not(None),
                IpMacHistory.mac_address != IPAddress.mac_address,
                IpMacHistory.last_seen >= cutoff,
            )
            .limit(_IP_HYGIENE_MAX_EVENTS)
        )
    ).all()
    # One event per IP — keep the most-recent offending observation if several.
    by_ip: dict[uuid.UUID, tuple[IPAddress, str, datetime]] = {}
    for ip_row, obs_mac, obs_at in rows:
        prev = by_ip.get(ip_row.id)
        if prev is None or obs_at > prev[2]:
            by_ip[ip_row.id] = (ip_row, str(obs_mac), obs_at)
    matches: list[tuple[str, str, str]] = []
    for ip_id, (ip_row, obs_mac, obs_at) in by_ip.items():
        message = (
            f"IP {ip_row.address} ({ip_row.status}) is recorded with MAC "
            f"{ip_row.mac_address} but a different MAC {obs_mac} answered at "
            f"{obs_at.isoformat()} — possible squatter or a device that moved."
        )
        matches.append((str(ip_id), str(ip_row.address), message))
    return matches


async def _matching_rogue_dhcp_subjects(
    db: AsyncSession,
    rule: AlertRule,
) -> list[tuple[str, str, str]]:
    """dhcp_observed_responder rows classified ``rogue`` + seen within the
    recency window (issue #370). Auto-resolves once a responder stops being
    seen as rogue (operator allowlists it → reclassified, or it goes away)."""
    days = rule.threshold_days if rule.threshold_days is not None else _ROGUE_DHCP_RECENCY_DAYS
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    rows = (
        (
            await db.execute(
                select(DHCPObservedResponder).where(
                    DHCPObservedResponder.classification == "rogue",
                    DHCPObservedResponder.last_seen_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    matches: list[tuple[str, str, str]] = []
    for r in rows:
        display = f"{r.source_ip} (server-id {r.server_identifier})"
        offered = f", offered {r.offered_ip}" if r.offered_ip else ""
        message = (
            f"Unrecognised DHCP server answering on a managed segment: "
            f"source {r.source_ip}, server-id {r.server_identifier}"
            f"{f', MAC {r.source_mac}' if r.source_mac else ''}{offered}. "
            f"Not a known group member or allowlisted — investigate a rogue / "
            f"misconfigured DHCP server, or acknowledge it if expected."
        )
        matches.append((str(r.id), display, message))
    return matches


async def _matching_rogue_ra_subjects(
    db: AsyncSession,
    rule: AlertRule,
) -> list[tuple[str, str, str]]:
    """ra_observed_router rows classified ``rogue`` + seen within the recency
    window (issue #524). Auto-resolves once a router stops being seen as rogue
    (operator allowlists it → reclassified, or it goes away)."""
    days = rule.threshold_days if rule.threshold_days is not None else _ROGUE_RA_RECENCY_DAYS
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    rows = (
        (
            await db.execute(
                select(RAObservedRouter).where(
                    RAObservedRouter.classification == "rogue",
                    RAObservedRouter.last_seen_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    matches: list[tuple[str, str, str]] = []
    for r in rows:
        display = str(r.source_ip)
        prefixes = ", ".join(r.prefixes or []) or "none advertised"
        message = (
            f"Unrecognised IPv6 router advertising on a managed segment: "
            f"source {r.source_ip}"
            f"{f', MAC {r.source_mac}' if r.source_mac else ''} "
            f"(M={int(r.managed_flag)} O={int(r.other_flag)}, prefixes: {prefixes}). "
            f"Not on the RA allowlist — investigate a rogue / misconfigured "
            f"router, or acknowledge it if expected."
        )
        matches.append((str(r.id), display, message))
    return matches


async def _matching_new_mac_seen_subjects(
    db: AsyncSession,
    rule: AlertRule,
) -> list[tuple[str, str, str]]:
    """ip_mac_history rows classified ``new`` + first seen within the recency
    window (issue #459) — a MAC never seen before, not allowlisted, not on the
    known fleet. One event per ``(ip, mac)`` pair so two MACs on one IP both
    surface. Auto-resolves once acknowledged / allowlisted (reclassified) or the
    sighting ages out of the window.

    Locally-administered (randomised) MACs are excluded unless the rule's
    ``classification`` is ``"all"`` — modern phones rotate them per network and
    would otherwise storm the operator on every reconnect.
    """
    days = rule.threshold_days if rule.threshold_days is not None else _NEW_MAC_SEEN_RECENCY_DAYS
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    include_randomized = (rule.classification or "").lower() == "all"
    conds = [
        IpMacHistory.classification == "new",
        IpMacHistory.first_seen >= cutoff,
    ]
    if not include_randomized:
        conds.append(IpMacHistory.is_randomized.is_(False))
    rows = (
        await db.execute(
            select(
                IPAddress, IpMacHistory.mac_address, IpMacHistory.first_seen, IpMacHistory.source
            )
            .join(IpMacHistory, IpMacHistory.ip_address_id == IPAddress.id)
            .where(*conds)
            .order_by(IpMacHistory.first_seen.desc())
            .limit(_IP_HYGIENE_MAX_EVENTS)
        )
    ).all()
    matches: list[tuple[str, str, str]] = []
    for ip_row, obs_mac, first_at, source in rows:
        subject_id = f"{ip_row.id}:{obs_mac}"
        display = f"{ip_row.address} ({obs_mac})"
        message = (
            f"New device: MAC {obs_mac} first seen on {ip_row.address} at "
            f"{first_at.isoformat()} (source: {source}). Not previously known, "
            f"allowlisted, or part of the allocated fleet — acknowledge, add to "
            f"the allowlist, or block it."
        )
        matches.append((subject_id, display, message))
    return matches


async def _dns_server_names(db: AsyncSession, server_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Resolve DNS server ids → display names in one query (issue #371)."""
    if not server_ids:
        return {}
    rows = (
        await db.execute(select(DNSServer.id, DNSServer.name).where(DNSServer.id.in_(server_ids)))
    ).all()
    return {row[0]: row[1] for row in rows}


async def _matching_dns_nxdomain_spike_subjects(
    db: AsyncSession,
    rule: AlertRule,
) -> list[tuple[str, str, str]]:
    """DNS servers whose trailing-window NXDOMAIN ratio + count cross the rule
    thresholds (issue #371). Reads the per-server ``dns_metric_sample`` deltas
    the agents already report; no new collection.
    """
    ratio_threshold = (
        rule.threshold_percent
        if rule.threshold_percent is not None
        else _DNS_NXDOMAIN_RATIO_DEFAULT
    )
    min_count = (
        rule.min_free_addresses
        if rule.min_free_addresses is not None
        else _DNS_NXDOMAIN_MIN_COUNT_DEFAULT
    )
    since = datetime.now(UTC) - _DNS_ANOMALY_WINDOW
    rows = (
        await db.execute(
            select(
                DNSMetricSample.server_id,
                func.sum(DNSMetricSample.queries_total).label("q"),
                func.sum(DNSMetricSample.nxdomain).label("nx"),
            )
            .where(DNSMetricSample.bucket_at >= since)
            .group_by(DNSMetricSample.server_id)
        )
    ).all()
    if not rows:
        return []
    names = await _dns_server_names(db, [r.server_id for r in rows])
    win_min = int(_DNS_ANOMALY_WINDOW.total_seconds() // 60)
    matches: list[tuple[str, str, str]] = []
    for r in rows:
        q = int(r.q or 0)
        nx = int(r.nx or 0)
        if nx < min_count or q <= 0:
            continue
        ratio = nx / q * 100
        if ratio < ratio_threshold:
            continue
        name = names.get(r.server_id) or str(r.server_id)
        message = (
            f"DNS server {name} — {nx} NXDOMAIN responses ({ratio:.0f}% of {q} "
            f"queries) in the last {win_min} min (threshold {ratio_threshold}% / "
            f"floor {min_count}). Possible DGA beacon, broken client, or "
            f"mistyped-search-domain storm."
        )
        matches.append((str(r.server_id), name, message))
    return matches


async def _matching_dns_query_rate_spike_subjects(
    db: AsyncSession,
    rule: AlertRule,
) -> list[tuple[str, str, str]]:
    """DNS servers whose trailing-window query total spikes vs the prior
    equal-length window (issue #371)."""
    pct = (
        rule.threshold_percent
        if rule.threshold_percent is not None
        else _DNS_QUERY_RATE_SPIKE_PCT_DEFAULT
    )
    floor = (
        rule.min_free_addresses
        if rule.min_free_addresses is not None
        else _DNS_QUERY_RATE_MIN_DEFAULT
    )
    now = datetime.now(UTC)
    cur_since = now - _DNS_ANOMALY_WINDOW
    prev_since = now - 2 * _DNS_ANOMALY_WINDOW

    async def _sums(lower: datetime, upper: datetime | None) -> dict[uuid.UUID, int]:
        stmt = select(
            DNSMetricSample.server_id,
            func.sum(DNSMetricSample.queries_total),
        ).where(DNSMetricSample.bucket_at >= lower)
        if upper is not None:
            stmt = stmt.where(DNSMetricSample.bucket_at < upper)
        stmt = stmt.group_by(DNSMetricSample.server_id)
        return {sid: int(q or 0) for sid, q in (await db.execute(stmt)).all()}

    cur = await _sums(cur_since, None)
    if not cur:
        return []
    prev = await _sums(prev_since, cur_since)
    names = await _dns_server_names(db, list(cur.keys()))
    win_min = int(_DNS_ANOMALY_WINDOW.total_seconds() // 60)
    matches: list[tuple[str, str, str]] = []
    for sid, q_cur in cur.items():
        if q_cur < floor:
            continue
        q_prev = prev.get(sid, 0)
        # A cold prior window: any current ≥ floor is a spike. Otherwise the
        # current window must exceed the prior by pct%.
        threshold_val = q_prev * (1 + pct / 100) if q_prev > 0 else float(floor)
        if q_cur < threshold_val:
            continue
        name = names.get(sid) or str(sid)
        if q_prev > 0:
            message = (
                f"DNS server {name} — query-rate spike: {q_cur} queries in the "
                f"last {win_min} min vs {q_prev} in the prior {win_min} min "
                f"(+{(q_cur / q_prev - 1) * 100:.0f}%, threshold +{pct}%)."
            )
        else:
            message = (
                f"DNS server {name} — {q_cur} queries in the last {win_min} min "
                f"from a cold prior window (floor {floor})."
            )
        matches.append((str(sid), name, message))
    return matches


async def _matching_dns_rate_limit_dropping_subjects(
    db: AsyncSession,
    rule: AlertRule,
) -> list[tuple[str, str, str]]:
    """DNS servers whose Response Rate Limiting is actively shedding a flood
    (#146 Phase 3): RateDropped summed over the trailing window clears the
    floor. Open-while-true — auto-resolves when the flood subsides."""
    floor = (
        rule.min_free_addresses
        if rule.min_free_addresses is not None
        else _DNS_RATE_LIMIT_DROP_MIN_DEFAULT
    )
    since = datetime.now(UTC) - _DNS_ANOMALY_WINDOW
    stmt = (
        select(
            DNSMetricSample.server_id,
            func.sum(DNSMetricSample.rate_dropped).label("dropped"),
            func.sum(DNSMetricSample.rate_slipped).label("slipped"),
        )
        .where(DNSMetricSample.bucket_at >= since)
        .group_by(DNSMetricSample.server_id)
    )
    rows = (await db.execute(stmt)).all()
    hits = {sid: (int(d or 0), int(s or 0)) for sid, d, s in rows if int(d or 0) >= floor}
    if not hits:
        return []
    names = await _dns_server_names(db, list(hits.keys()))
    win_min = int(_DNS_ANOMALY_WINDOW.total_seconds() // 60)
    matches: list[tuple[str, str, str]] = []
    for sid, (dropped, slipped) in hits.items():
        name = names.get(sid) or str(sid)
        message = (
            f"DNS server {name} — Response Rate Limiting dropped {dropped} "
            f"responses in the last {win_min} min ({slipped} slipped/truncated); "
            f"the server is shedding a query flood (floor {floor}). Investigate a "
            "possible amplification attempt or misbehaving client."
        )
        matches.append((str(sid), name, message))
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


async def _matching_bgp_hijack_subjects(
    db: AsyncSession,
    rule: AlertRule,  # noqa: ARG001 — symmetry with sibling evaluators
    detection_kind: str,
) -> list[tuple[str, str, str, str | None]]:
    """Every active ``bgp_hijack_detection`` of ``detection_kind``.

    "Active" = ``resolved_at IS NULL`` (announcement still observed and
    within the delist window) AND ``acknowledged = False`` (operator
    hasn't muted it). The detection table is the latch; the poll task
    resolves rows on delist so the standard evaluator auto-resolves the
    ``AlertEvent`` when the subject stops matching.

    Per-detection severity (``critical`` for RPKI-invalid, ``warning``
    for RPKI-unknown) rides through as the tuple's severity override.
    """
    res = await db.execute(
        select(BGPHijackDetection).where(
            BGPHijackDetection.detection_kind == detection_kind,
            BGPHijackDetection.resolved_at.is_(None),
            BGPHijackDetection.acknowledged.is_(False),
        )
    )
    matches: list[tuple[str, str, str, str | None]] = []
    for row in res.scalars().all():
        kind_label = (
            "announcing" if detection_kind == "prefix_hijack" else "announcing more-specific"
        )
        display = f"{row.observed_prefix} ← AS{row.observed_origin_asn}"
        message = (
            f"BGP hijack: AS{row.observed_origin_asn} is {kind_label} "
            f"{row.observed_prefix} (tracked prefix {row.tracked_prefix}, "
            f"expected origin AS{row.expected_origin_asn}) — "
            f"RPKI {row.rpki_status}"
        )
        matches.append((str(row.id), display, message, row.severity))
    return matches


async def _matching_bgp_lg_session_down_subjects(
    db: AsyncSession,
    rule: AlertRule,  # noqa: ARG001 — symmetry with sibling evaluators
    now: datetime,
) -> list[tuple[str, str, str]]:
    """``bgp_lg_session_down`` — an enabled peer whose session is not
    Established, sustained past a grace window.

    Grace is anchored on ``BGPLGPeer.down_since``, a watermark THIS
    function stamps on first non-established observation and clears the
    moment the session re-establishes — mirrors
    ``_matching_firewall_apply_stalled_subjects``'s ``stalled_since``
    pattern exactly. A converged session auto-resolves via
    ``evaluate_all``'s standard "subject no longer matches" diff; no
    explicit resolve logic needed here.
    """
    rows = (
        await db.execute(
            select(BGPLGPeer, LookingGlassCollector)
            .join(LookingGlassCollector, LookingGlassCollector.id == BGPLGPeer.collector_id)
            .where(BGPLGPeer.enabled.is_(True))
        )
    ).all()

    matches: list[tuple[str, str, str]] = []
    for peer, collector in rows:
        if peer.session_state == "established":
            if peer.down_since is not None:
                peer.down_since = None
            continue
        if peer.down_since is None:
            peer.down_since = now  # first observation — start the grace clock
            continue
        if (now - peer.down_since) <= _BGP_LG_SESSION_DOWN_GRACE:
            continue
        collector_note = (
            f"collector '{collector.name}' is also reporting {collector.status}"
            if collector.status != "active"
            else f"collector '{collector.name}' is reporting normally"
        )
        display = f"{peer.name} (AS{peer.peer_asn} @ {peer.peer_address})"
        message = (
            f"BGP Looking Glass session '{peer.name}' to AS{peer.peer_asn} "
            f"({peer.peer_address}) has been {peer.session_state} since "
            f"{peer.down_since.isoformat()} — last known {peer.prefixes_received} "
            f"prefixes received; {collector_note}."
        )
        matches.append((str(peer.id), display, message))
    return matches


async def _matching_bgp_lg_rpki_invalid_route_subjects(
    db: AsyncSession,
    rule: AlertRule,  # noqa: ARG001
) -> list[tuple[str, str, str, str | None]]:
    """``bgp_lg_rpki_invalid_route`` — every active learned route whose
    RPKI status is ``invalid`` (computed at ingest via
    ``derive_rpki_status_batch``, no re-validation needed here). Always
    rides in at ``critical`` severity via a severity override — RPKI
    invalidity on YOUR OWN table is the strongest possible in-network
    leak/misconfig signal (mirrors ``severity_for_rpki``'s "invalid ⇒
    critical" mapping from the #527 hijack monitor)."""
    rows = (
        await db.execute(
            select(BGPLGRoute, BGPLGPeer)
            .join(BGPLGPeer, BGPLGPeer.id == BGPLGRoute.peer_id)
            .where(BGPLGRoute.rpki_status == RPKI_INVALID, BGPLGRoute.withdrawn_at.is_(None))
            .limit(_BGP_LG_MAX_EVENTS)
        )
    ).all()
    matches: list[tuple[str, str, str, str | None]] = []
    for route, peer in rows:
        display = f"{route.prefix} ← AS{route.origin_asn}"
        message = (
            f"RPKI-invalid route in the Looking Glass RIB: {route.prefix} originated by "
            f"AS{route.origin_asn}, learned from peer '{peer.name}' (AS{peer.peer_asn}) — "
            f"no covering ROA authorises this origin/length."
        )
        matches.append((str(route.id), display, message, severity_for_rpki(RPKI_INVALID)))
    if len(rows) >= _BGP_LG_MAX_EVENTS:
        logger.warning("bgp_lg_rpki_invalid_route_truncated", cap=_BGP_LG_MAX_EVENTS)
    return matches


async def _matching_bgp_lg_unexpected_origin_subjects(
    db: AsyncSession,
    rule: AlertRule,  # noqa: ARG001
) -> list[tuple[str, str, str]]:
    """``bgp_lg_unexpected_origin`` — an owned tracked prefix (exact
    CIDR match against ``BGPTrackedPrefix``) learned in the live RIB
    with an origin ASN outside ``expected_origin_set(tracked)``. Same
    "internal hijack / fat-fingered redistribute / route leak" shape as
    #527's exact-prefix detector, reading the internal RIB instead of
    RIPEstat."""
    rows = (
        await db.execute(
            select(BGPLGRoute, BGPTrackedPrefix, BGPLGPeer)
            .join(BGPTrackedPrefix, BGPTrackedPrefix.prefix == BGPLGRoute.prefix)
            .join(BGPLGPeer, BGPLGPeer.id == BGPLGRoute.peer_id)
            .where(
                BGPTrackedPrefix.enabled.is_(True),
                BGPLGRoute.withdrawn_at.is_(None),
                BGPLGRoute.origin_asn.is_not(None),
            )
            .limit(_BGP_LG_MAX_EVENTS)
        )
    ).all()
    matches: list[tuple[str, str, str]] = []
    for route, tracked, peer in rows:
        if route.origin_asn in expected_origin_set(tracked):
            continue
        display = (
            f"{route.prefix} ← AS{route.origin_asn} (expected AS{tracked.expected_origin_asn})"
        )
        message = (
            f"Tracked prefix {tracked.prefix} is learned with unexpected origin "
            f"AS{route.origin_asn} (expected AS{tracked.expected_origin_asn}) via peer "
            f"'{peer.name}' — possible internal leak or misconfigured redistribution."
        )
        matches.append((str(route.id), display, message))
    return matches


async def _matching_bgp_lg_more_specific_subjects(
    db: AsyncSession,
    rule: AlertRule,  # noqa: ARG001
) -> list[tuple[str, str, str]]:
    """``bgp_lg_more_specific`` — a route STRICTLY more specific
    (Postgres ``<<``, contained-and-not-equal) than an owned tracked
    aggregate, with an unexpected origin. The classic internal
    sub-prefix leak that wins BGP best-path over your aggregate via
    longest-match. Same origin-allowlist semantics as
    ``_matching_bgp_lg_unexpected_origin_subjects`` — only the
    containment operator differs (exact ``==`` there, strict ``<<``
    here)."""
    rows = (
        await db.execute(
            select(BGPLGRoute, BGPTrackedPrefix, BGPLGPeer)
            .join(BGPTrackedPrefix, BGPLGRoute.prefix.op("<<")(BGPTrackedPrefix.prefix))
            .join(BGPLGPeer, BGPLGPeer.id == BGPLGRoute.peer_id)
            .where(
                BGPTrackedPrefix.enabled.is_(True),
                BGPLGRoute.withdrawn_at.is_(None),
                BGPLGRoute.origin_asn.is_not(None),
            )
            .limit(_BGP_LG_MAX_EVENTS)
        )
    ).all()
    matches: list[tuple[str, str, str]] = []
    for route, tracked, peer in rows:
        if route.origin_asn in expected_origin_set(tracked):
            continue
        display = f"{route.prefix} (more-specific of {tracked.prefix}) ← AS{route.origin_asn}"
        message = (
            f"More-specific {route.prefix} of owned aggregate {tracked.prefix} learned with "
            f"unexpected origin AS{route.origin_asn} (expected AS{tracked.expected_origin_asn}) "
            f"via peer '{peer.name}' — this sub-prefix wins best-path over your aggregate."
        )
        matches.append((str(route.id), display, message))
    return matches


async def _matching_bgp_lg_route_flap_subjects(
    db: AsyncSession,
    rule: AlertRule,
    now: datetime,
) -> list[tuple[str, str, str]]:
    """``bgp_lg_route_flap`` — an active route whose lifetime flap count
    (``BGPLGRoute.flap_count``, bumped once per absence-withdraw in
    ``routes_ingest.py``) has crossed ``rule.threshold_percent`` (reused
    as a raw flap-count floor — same int-as-count convention as
    ``voice_lease_count_below`` / ``stale_ip_count``), AND the most
    recent flap (``last_flap_at``) is within the trailing
    ``_BGP_LG_FLAP_WINDOW``. The recency gate is what makes this a
    "currently unstable" predicate rather than a permanent scar —
    once no new flap lands for the window, the route drops out of the
    match set and the AlertEvent auto-resolves via the standard diff.
    """
    threshold = rule.threshold_percent or _BGP_LG_FLAP_COUNT_DEFAULT
    since = now - _BGP_LG_FLAP_WINDOW
    rows = (
        await db.execute(
            select(BGPLGRoute, BGPLGPeer)
            .join(BGPLGPeer, BGPLGPeer.id == BGPLGRoute.peer_id)
            .where(
                BGPLGRoute.flap_count >= threshold,
                BGPLGRoute.last_flap_at.is_not(None),
                BGPLGRoute.last_flap_at >= since,
            )
            .limit(_BGP_LG_MAX_EVENTS)
        )
    ).all()
    matches: list[tuple[str, str, str]] = []
    win_min = int(_BGP_LG_FLAP_WINDOW.total_seconds() // 60)
    for route, peer in rows:
        display = f"{route.prefix} via {peer.name}"
        message = (
            f"Route {route.prefix} via peer '{peer.name}' (AS{peer.peer_asn}) has flapped "
            f"{route.flap_count} times, most recently {route.last_flap_at.isoformat()} "
            f"(threshold {threshold} within the trailing ~{win_min} min) — unstable path."
        )
        matches.append((str(route.id), display, message))
    return matches


async def _matching_bgp_lg_missing_advertisement_subjects(
    db: AsyncSession,
    rule: AlertRule,  # noqa: ARG001
) -> list[tuple[str, str, str]]:
    """``bgp_lg_missing_advertisement`` — a subnet flagged
    ``bgp_should_advertise`` with NO active learned route covering it
    (Postgres ``>>=``, contains-or-equal) across ANY peer.

    Deliberately does NOT wait on Phase 3's ``matched_subnet_id``
    resolution — that FK is populated by a longest-prefix-match
    reconcile that may not exist yet. This does the CIDR containment
    check directly against ``BGPLGRoute.prefix`` so the alert works
    whether or not Phase 3 has landed. ``Subnet``'s global soft-delete
    query filter (``app.db._filter_soft_deleted``) already excludes
    trashed subnets — no explicit ``deleted_at`` predicate needed.
    """
    covering_exists = (
        select(BGPLGRoute.id)
        .where(BGPLGRoute.withdrawn_at.is_(None), BGPLGRoute.prefix.op(">>=")(Subnet.network))
        .correlate(Subnet)
        .exists()
    )
    rows = (
        (
            await db.execute(
                select(Subnet)
                .where(Subnet.bgp_should_advertise.is_(True), ~covering_exists)
                .limit(_BGP_LG_MAX_EVENTS)
            )
        )
        .scalars()
        .all()
    )
    matches: list[tuple[str, str, str]] = []
    for subnet in rows:
        display = f"{subnet.network} ({subnet.name})" if subnet.name else str(subnet.network)
        message = (
            f"Subnet {subnet.network} is flagged 'should advertise via BGP' but no active "
            f"Looking Glass peer is currently learning a covering route — check redistribution "
            f"on your edge routers."
        )
        matches.append((str(subnet.id), display, message))
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


_SEVERITY_ORDER = ("info", "warning", "critical")


def _severity_rank(severity: str) -> int:
    """Ordinal rank for an alert severity: ``info < warning < critical``.

    Unknown / unexpected values rank as ``warning`` (1) — same default
    the pre-existing escalation helper used. Shared by the expiring-rule
    escalation helper and the ``evaluate_all`` open-event loop so an
    already-open ``*_expiring`` event can be bumped up (never down) as
    its expiry date nears.
    """
    return {"info": 0, "warning": 1, "critical": 2}.get(severity, 1)


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

    base_rank = _severity_rank(base_severity)
    actual_rank = 0  # info at the soft threshold

    # Avoid division blowups for absurdly small thresholds. Floor of 1.
    safe = max(1, threshold_days)
    if days_to_expiry <= safe / 12:
        actual_rank = 2  # critical
    elif days_to_expiry <= safe / 4:
        actual_rank = 1  # warning

    final = max(base_rank, actual_rank)
    return _SEVERITY_ORDER[final]


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


async def _matching_tls_cert_expiring_subjects(
    db: AsyncSession,
    rule: AlertRule,
    now: datetime,
) -> list[tuple[str, str, str, str]]:
    """``tls_cert_expiring`` — escalating-expiry rule over the latest-known
    ``not_after`` per enabled target (same severity ramp as domain_expiring).
    Auto-resolves via the generic loop once the cert is renewed past the
    cutoff (not_after moves out → no longer returned)."""
    threshold_days = rule.threshold_days or _DEFAULT_EXPIRING_THRESHOLD_DAYS
    cutoff = now + timedelta(days=threshold_days)
    rows = (
        (
            await db.execute(
                select(TLSCertTarget).where(
                    TLSCertTarget.enabled.is_(True),
                    TLSCertTarget.not_after.is_not(None),
                    TLSCertTarget.not_after <= cutoff,
                    # NOTE: intentionally NOT excluding unreachable here — a
                    # cert's expiry is a fact from the last good probe, so a
                    # briefly-unreachable endpoint near expiry should keep the
                    # expiring event OPEN (excluding it makes the generic loop
                    # resolve→reopen → notification flap on a flapping host).
                    # The unreachable rule co-fires to signal the data is stale.
                )
            )
        )
        .scalars()
        .all()
    )
    matches: list[tuple[str, str, str, str]] = []
    for t in rows:
        exp = t.not_after
        if exp is None:
            continue
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        days_to_expiry = (exp - now).total_seconds() / 86400.0
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
        label = t.display_name or t.host
        message = (
            f"TLS cert for {label} {descriptor} (not_after "
            f"{exp.isoformat()}, threshold {threshold_days} d)"
        )
        matches.append((str(t.id), label, message, sev))
    return matches


async def _matching_tls_cert_chain_invalid_subjects(
    db: AsyncSession, rule: AlertRule
) -> list[tuple[str, str, str]]:
    """``tls_cert_chain_invalid`` — fires for a reachable, unexpired cert
    that isn't usable: an untrusted chain (self-signed / wrong CA / broken
    chain → ``chain_valid IS FALSE``) OR a trusted chain served on the wrong
    hostname (SAN/CN mismatch → ``chain_valid IS TRUE`` but the probed name
    isn't covered). ``derive_tls_state`` buckets BOTH as ``STATE_MISMATCH``
    (expiry + unreachable take precedence in that ordering), so keying on the
    state covers the hostname-mismatch case the rule advertises without
    double-paging an expired or down cert (owned by the expiring /
    unreachable rules). Auto-resolves once a probe validates + name-matches."""
    rows = (
        (
            await db.execute(
                select(TLSCertTarget).where(
                    TLSCertTarget.enabled.is_(True),
                    TLSCertTarget.state == STATE_MISMATCH,
                )
            )
        )
        .scalars()
        .all()
    )
    matches: list[tuple[str, str, str]] = []
    for t in rows:
        label = t.display_name or t.host
        if t.chain_valid is False:
            detail = t.chain_error or "certificate chain did not validate"
        else:
            # Trusted chain, wrong name — the SAN-drift case the module advertises.
            detail = "certificate served does not match the expected hostname (SAN mismatch)"
        message = f"TLS cert for {label} invalid: {detail}"
        matches.append((str(t.id), label, message))
    return matches


async def _matching_tls_cert_unreachable_subjects(
    db: AsyncSession, rule: AlertRule
) -> list[tuple[str, str, str]]:
    """``tls_cert_unreachable`` — fires while the endpoint can't be probed,
    gated on a couple of consecutive failures so a single transient blip
    doesn't page. Auto-resolves on the next successful probe."""
    rows = (
        (
            await db.execute(
                select(TLSCertTarget).where(
                    TLSCertTarget.enabled.is_(True),
                    TLSCertTarget.state == STATE_UNREACHABLE,
                    TLSCertTarget.consecutive_failures >= _TLS_CERT_UNREACHABLE_MIN_FAILURES,
                )
            )
        )
        .scalars()
        .all()
    )
    matches: list[tuple[str, str, str]] = []
    for t in rows:
        label = t.display_name or t.host
        message = (
            f"TLS endpoint {label} unreachable "
            f"({t.consecutive_failures} consecutive failures): "
            f"{t.last_error or 'probe failed'}"
        )
        matches.append((str(t.id), label, message))
    return matches


async def _evaluate_tls_cert_transition_rule(
    db: AsyncSession,
    rule: AlertRule,
    now: datetime,
    *,
    value_attr: str = "fingerprint_sha256",
    what: str = "fingerprint",
) -> tuple[int, int, int, int, int]:
    """Transition-once rule over a per-target cert attribute, modelled on
    :func:`_evaluate_domain_transition_rule`. ``value_attr`` selects the
    watched column:

    * ``fingerprint_sha256`` (``tls_cert_changed``) — any cert swap;
      legitimate on renewal, suspicious otherwise.
    * ``issuer_cn`` (``tls_cert_issuer_changed``) — the issuing CA changed,
      i.e. cert-rotation *deviation* (a normally-ACME cert coming back from a
      different issuer). Higher-signal than a plain fingerprint change.

    First sighting records a silent baseline; open events auto-resolve after
    ``_TRANSITION_AUTO_RESOLVE_DAYS``."""
    targets = await audit_forward._load_targets()  # noqa: SLF001

    opened = resolved = delivered_syslog = delivered_webhook = delivered_smtp = 0

    open_res = await db.execute(
        select(AlertEvent).where(
            AlertEvent.rule_id == rule.id,
            AlertEvent.resolved_at.is_(None),
        )
    )
    open_events = list(open_res.scalars().all())
    open_by_subject: dict[str, AlertEvent] = {ev.subject_id: ev for ev in open_events}

    cutoff = now - timedelta(days=_TRANSITION_AUTO_RESOLVE_DAYS)
    for ev in list(open_events):
        if ev.fired_at < cutoff:
            ev.resolved_at = now
            resolved += 1
            del open_by_subject[ev.subject_id]

    last_event_res = await db.execute(
        select(AlertEvent).where(AlertEvent.rule_id == rule.id).order_by(AlertEvent.fired_at.desc())
    )
    last_event_by_subject: dict[str, AlertEvent] = {}
    for ev in last_event_res.scalars().all():
        last_event_by_subject.setdefault(ev.subject_id, ev)

    rows = (
        (await db.execute(select(TLSCertTarget).where(TLSCertTarget.enabled.is_(True))))
        .scalars()
        .all()
    )
    for t in rows:
        subject_id = str(t.id)
        current_value = getattr(t, value_attr)
        if open_by_subject.get(subject_id) is not None:
            continue

        prior_event = last_event_by_subject.get(subject_id)
        if prior_event is not None and isinstance(prior_event.last_observed_value, dict):
            prior_value = prior_event.last_observed_value.get("to")
        else:
            prior_value = None

        label = t.display_name or t.host
        if prior_event is None:
            if current_value is None:
                continue
            db.add(
                AlertEvent(
                    rule_id=rule.id,
                    subject_type="tls_cert",
                    subject_id=subject_id,
                    subject_display=label,
                    severity="info",
                    message=f"Initial TLS cert {what} baseline for {label}: {current_value}",
                    fired_at=now,
                    resolved_at=now,
                    last_observed_value={"from": None, "to": current_value},
                )
            )
            continue

        if current_value is None or current_value == prior_value:
            continue

        message = f"TLS cert {what} for {label} changed: {prior_value} → {current_value}"
        event = AlertEvent(
            rule_id=rule.id,
            subject_type="tls_cert",
            subject_id=subject_id,
            subject_display=label,
            severity=rule.severity,
            message=message,
            fired_at=now,
            last_observed_value={"from": prior_value, "to": current_value},
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


# ── Appliance k3s cert evaluator (#183 Phase 6) ────────────────────


async def _matching_k3s_api_cert_expiring_subjects(
    db: AsyncSession,
    rule: AlertRule,
    now: datetime,
) -> list[tuple[str, str, str, str]]:
    """Return ``[(subject_id, display, message, severity)]`` for the
    ``k3s_api_cert_expiring`` rule type. Mirrors the
    ``circuit_term_expiring`` shape — severity escalates per
    ``threshold/4`` (warning) / ``threshold/12`` (critical) so one
    rule covers the 30 / 7-day expiry chain.

    Only matches appliances where the supervisor has reported
    ``k3s_api_cert_expires_at`` (k3s is the runtime). Soft-deleted
    rows are excluded — a revoked appliance's cert expiring is
    operator-actionable but not via this alert.
    """
    from app.models.appliance import Appliance  # noqa: PLC0415

    threshold_days = rule.threshold_days or _DEFAULT_EXPIRING_THRESHOLD_DAYS
    cutoff = now + timedelta(days=threshold_days)

    rows = (
        (
            await db.execute(
                select(Appliance)
                .where(Appliance.revoked_at.is_(None))
                .where(Appliance.k3s_api_cert_expires_at.is_not(None))
                .where(Appliance.k3s_api_cert_expires_at <= cutoff)
            )
        )
        .scalars()
        .all()
    )

    matches: list[tuple[str, str, str, str]] = []
    for a in rows:
        if a.k3s_api_cert_expires_at is None:
            continue
        delta = a.k3s_api_cert_expires_at - now
        days_to_expiry = delta.days
        sev = _escalate_severity_for_expiring(
            rule.severity,
            threshold_days=threshold_days,
            days_to_expiry=days_to_expiry,
        )
        if days_to_expiry <= 0:
            descriptor = "has expired"
        elif days_to_expiry == 1:
            descriptor = "expires tomorrow"
        else:
            descriptor = f"expires in {days_to_expiry} day(s)"
        message = (
            f"k3s API server cert on {a.hostname} {descriptor} "
            f"({a.k3s_api_cert_expires_at.isoformat()}, threshold "
            f"{threshold_days} d). k3s rotates this automatically; "
            f"a restart of k3s.service should pick up a refreshed cert."
        )
        matches.append((str(a.id), a.hostname, message, sev))
    return matches


async def _matching_secret_expiring_subjects(
    db: AsyncSession,
    rule: AlertRule,
    now: datetime,
) -> list[tuple[str, str, str, str]]:
    """Return ``[(subject_id, display, message, severity)]`` for the
    ``secret_expiring`` rule type (#76). Scans every internal credential
    that carries an expiry and fires once per credential expiring within
    ``threshold_days``:

    * **Supervisor mTLS certs** (``appliance.cert_expires_at``) — the
      internal-CA-signed cert each appliance's supervisor presents on the
      agent-comms channel. (The k3s API-server cert has its own
      ``k3s_api_cert_expiring`` rule and is intentionally NOT duplicated.)
    * **API tokens** (``api_token.expires_at``) — active tokens with an
      expiry.

    ``subject_id`` is ``appliance_cert:<id>`` / ``api_token:<id>`` so each
    credential latches its own event; severity escalates per ``threshold/4``
    (warning) / ``threshold/12`` (critical). TSIG keys and ACME *accounts*
    carry no expiry of their own (their issued certs do — out of scope), so
    they contribute no subjects here.
    """
    from app.models.appliance import Appliance  # noqa: PLC0415
    from app.models.auth import APIToken  # noqa: PLC0415

    threshold_days = rule.threshold_days or _DEFAULT_EXPIRING_THRESHOLD_DAYS
    cutoff = now + timedelta(days=threshold_days)
    matches: list[tuple[str, str, str, str]] = []

    def _descriptor(days: int) -> str:
        if days <= 0:
            return "has expired"
        if days == 1:
            return "expires tomorrow"
        return f"expires in {days} day(s)"

    # 1. Supervisor mTLS certs (internal CA-signed; agent-comms channel).
    certs = (
        (
            await db.execute(
                select(Appliance)
                .where(Appliance.revoked_at.is_(None))
                .where(Appliance.cert_expires_at.is_not(None))
                .where(Appliance.cert_expires_at <= cutoff)
            )
        )
        .scalars()
        .all()
    )
    for a in certs:
        if a.cert_expires_at is None:
            continue
        days = (a.cert_expires_at - now).days
        sev = _escalate_severity_for_expiring(
            rule.severity, threshold_days=threshold_days, days_to_expiry=days
        )
        message = (
            f"Supervisor mTLS certificate for appliance {a.hostname} "
            f"{_descriptor(days)} ({a.cert_expires_at.isoformat()}, threshold "
            f"{threshold_days} d). Re-key it from the Fleet drilldown."
        )
        matches.append((f"appliance_cert:{a.id}", f"{a.hostname} supervisor cert", message, sev))

    # 2. API tokens with an expiry (active only).
    tokens = (
        (
            await db.execute(
                select(APIToken)
                .where(APIToken.is_active.is_(True))
                .where(APIToken.expires_at.is_not(None))
                .where(APIToken.expires_at <= cutoff)
            )
        )
        .scalars()
        .all()
    )
    for t in tokens:
        if t.expires_at is None:
            continue
        days = (t.expires_at - now).days
        sev = _escalate_severity_for_expiring(
            rule.severity, threshold_days=threshold_days, days_to_expiry=days
        )
        message = (
            f"API token '{t.name}' ({t.prefix}…) {_descriptor(days)} "
            f"({t.expires_at.isoformat()}, threshold {threshold_days} d). "
            f"Rotate it from Settings → API Tokens."
        )
        matches.append((f"api_token:{t.id}", f"{t.name} API token", message, sev))

    # 3. ACME-issued Web UI TLS certs (#438) — active letsencrypt certs
    #    nearing expiry. Distinct subject prefix from the supervisor cert
    #    so they latch independently. Phase-2 auto-renewal normally renews
    #    these well before this fires; an alert means renewal is stuck.
    from app.models.appliance import (  # noqa: PLC0415
        CERT_SOURCE_LETSENCRYPT,
        ApplianceCertificate,
    )

    web_certs = (
        (
            await db.execute(
                select(ApplianceCertificate)
                .where(ApplianceCertificate.source == CERT_SOURCE_LETSENCRYPT)
                .where(ApplianceCertificate.is_active.is_(True))
                .where(ApplianceCertificate.valid_to.is_not(None))
                .where(ApplianceCertificate.valid_to <= cutoff)
            )
        )
        .scalars()
        .all()
    )
    for c in web_certs:
        if c.valid_to is None:
            continue
        days = (c.valid_to - now).days
        sev = _escalate_severity_for_expiring(
            rule.severity, threshold_days=threshold_days, days_to_expiry=days
        )
        message = (
            f"Let's Encrypt Web UI certificate '{c.subject_cn or c.name}' "
            f"{_descriptor(days)} ({c.valid_to.isoformat()}, threshold "
            f"{threshold_days} d). Auto-renewal may be stuck — check "
            f"Appliance → Web UI Certificate."
        )
        matches.append(
            (f"appliance_cert_tls:{c.id}", f"{c.subject_cn or c.name} (LE)", message, sev)
        )

    return matches


async def _matching_firewall_apply_stalled_subjects(
    db: AsyncSession,
    rule: AlertRule,
    now: datetime,
) -> list[tuple[str, str, str, str]]:
    """Return ``[(subject_id, display, message, severity)]`` for appliances
    whose control-plane-rendered firewall ruleset hasn't been applied by the
    host runner past the grace window (#285 Phase 2d).

    Gates hard on ``applied_status`` so it never false-fires:

    * ``error:*`` → the node's own *applied-error* state (distinct chip), not
      a stall.
    * ``reverted`` → a deliberate auto-revert (2c); alarming would never
      resolve since ``applied_hash != rendered_hash`` permanently.
    * only an ``ok`` node with a real ``rendered != applied`` mismatch counts.

    The grace is anchored on a ``stalled_since`` watermark the matcher stamps
    on first observation (and clears on convergence / a non-ok status), so a
    normal one-heartbeat render→apply lag never alarms and a converged node
    auto-resolves via ``evaluate_all``'s ``open_by_subject`` diff. The session
    commit at the end of ``evaluate_all`` persists the watermark mutations.
    """
    from app.models.appliance import Appliance  # noqa: PLC0415
    from app.models.firewall import FirewallApplyState  # noqa: PLC0415

    rows = (
        await db.execute(
            select(FirewallApplyState, Appliance)
            .join(Appliance, Appliance.id == FirewallApplyState.appliance_id)
            .where(Appliance.revoked_at.is_(None))
            .where(FirewallApplyState.rendered_hash.is_not(None))
        )
    ).all()

    matches: list[tuple[str, str, str, str]] = []
    for st, a in rows:
        converged = st.applied_hash == st.rendered_hash
        if converged or st.applied_status != "ok":
            # Not stalled (in sync, or an error/reverted state owns its own
            # signal) — clear the watermark so a later genuine stall starts a
            # fresh grace clock.
            if st.stalled_since is not None:
                st.stalled_since = None
            continue
        # ok-status node with a genuine mismatch.
        if st.stalled_since is None:
            st.stalled_since = now  # first observation — start the grace clock
            continue
        if (now - st.stalled_since) <= _FIREWALL_STALE_GRACE:
            continue  # still within grace — the normal render→apply lag
        # Sustained mismatch → fire. Cross-reference the Wave-E watchdog
        # (last_seen_at) so the operator knows whether the supervisor itself
        # is wedged or just the host firewall runner is the laggard.
        seen = a.last_seen_at
        if seen is not None and (now - seen) > _FIREWALL_STALE_GRACE:
            cause = f"the supervisor heartbeat is stale (last seen {seen.isoformat()})"
        else:
            cause = (
                "the supervisor is heartbeating but the host firewall runner "
                "hasn't applied the rendered ruleset"
            )
        message = (
            f"Firewall drift on {a.hostname}: control plane rendered "
            f"{st.rendered_hash[:12] if st.rendered_hash else 'none'} but the node "
            f"last applied {st.applied_hash[:12] if st.applied_hash else 'none'} — {cause}. "
            f"Check `journalctl -u spatium-firewall-reload` on the node."
        )
        matches.append((str(a.id), a.hostname, message, rule.severity))
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


async def _matching_decom_expiring_subjects(
    db: AsyncSession,
    rule: AlertRule,
    now: datetime,
) -> list[tuple[str, str, str, str]]:
    """Return ``[(subject_id, display, message, severity)]`` for the
    ``decom_expiring`` rule type (issue #46). Mirrors the
    ``service_term_expiring`` shape — same severity escalation, same
    soft-delete exclusion. A past-due decom date (negative days)
    surfaces a "decommission overdue" message at critical severity.
    """
    threshold_days = rule.threshold_days or _DEFAULT_EXPIRING_THRESHOLD_DAYS
    cutoff = (now + timedelta(days=threshold_days)).date()

    rows = (
        (
            await db.execute(
                select(Subnet)
                .where(Subnet.deleted_at.is_(None))
                .where(Subnet.decom_date.is_not(None))
                .where(Subnet.decom_date <= cutoff)
            )
        )
        .scalars()
        .all()
    )

    matches: list[tuple[str, str, str, str]] = []
    today = now.date()
    for s in rows:
        if s.decom_date is None:
            continue
        days_to_expiry = (s.decom_date - today).days
        sev = _escalate_severity_for_expiring(
            rule.severity,
            threshold_days=threshold_days,
            days_to_expiry=days_to_expiry,
        )
        display = s.name or s.network
        if days_to_expiry < 0:
            descriptor = f"decommission is {-days_to_expiry} day(s) overdue"
        elif days_to_expiry == 0:
            descriptor = "is scheduled for decommission today"
        elif days_to_expiry == 1:
            descriptor = "is scheduled for decommission tomorrow"
        else:
            descriptor = f"is scheduled for decommission in {days_to_expiry} day(s)"
        message = (
            f"Subnet {display} {descriptor} "
            f"(decom_date {s.decom_date.isoformat()}, threshold {threshold_days} d)"
        )
        matches.append((str(s.id), display, message, sev))
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


_SCHEMA_BEHIND_HEAD_RULE_NAME = "schema-behind-head"


async def seed_schema_behind_head_alert_rule() -> None:
    """Seed the singleton ``schema-behind-head`` rule (issue #565).

    Enabled by default — a worker running against a DB behind the
    bundled migrations is a real "code deployed before migrate ran"
    footgun that every deployment wants surfaced loudly instead of a
    silent background retry loop. Keyed on ``name`` (one rule per
    platform); an operator who disables / renames it is never
    overridden by a later boot.
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.alerts import AlertRule  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(AlertRule).where(AlertRule.name == _SCHEMA_BEHIND_HEAD_RULE_NAME)
        )
        if existing is not None:
            return
        session.add(
            AlertRule(
                name=_SCHEMA_BEHIND_HEAD_RULE_NAME,
                description=(
                    "Fires when the Celery worker/beat finds the database schema "
                    "behind the Alembic head bundled in the running image — i.e. "
                    "the app was deployed before 'alembic upgrade head' ran, so "
                    "background tasks fail against missing tables/columns. "
                    "Auto-resolves on the next check that finds the schema back "
                    "at head. Set STRICT_SCHEMA_CHECK=true to also refuse to "
                    "process tasks while behind."
                ),
                rule_type=RULE_TYPE_SCHEMA_BEHIND_HEAD,
                severity="critical",
                enabled=True,
                notify_syslog=True,
                notify_webhook=True,
                notify_smtp=True,
            )
        )
        await session.commit()


async def seed_firewall_apply_stalled_alert_rule() -> None:
    """Seed the singleton ``firewall.apply_stalled`` rule (issue #285 Phase 2d).

    DISABLED by default — it's only meaningful once an operator has enabled
    server-side firewall render (``firewall_enabled``); seeding it on signals
    its existence in the Alerts UI without firing on installs that never opt
    in. Keyed on ``name`` (one rule per platform); an operator who enables /
    renames it is never overridden.
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.alerts import AlertRule  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(AlertRule).where(AlertRule.name == _FIREWALL_APPLY_STALLED_RULE_NAME)
        )
        if existing is not None:
            return
        session.add(
            AlertRule(
                name=_FIREWALL_APPLY_STALLED_RULE_NAME,
                description=(
                    "Fires when an appliance's control-plane-rendered firewall "
                    "ruleset hasn't been applied by the host runner past a short "
                    "grace window, while the node's last apply was a clean 'ok' "
                    "(i.e. a genuine stall, not an apply error or a deliberate "
                    "auto-revert). Distinct from agent-offline — the message says "
                    "whether the supervisor itself is stale or just the host "
                    "firewall runner. Auto-resolves once the node applies the "
                    "rendered ruleset. Enable once server-side firewall render is on."
                ),
                rule_type=RULE_TYPE_FIREWALL_APPLY_STALLED,
                severity="warning",
                enabled=False,
                notify_syslog=True,
                notify_webhook=True,
                notify_smtp=False,
            )
        )
        await session.commit()


_DNS_NXDOMAIN_SPIKE_RULE_NAME = "DNS NXDOMAIN spike"
_DNS_QUERY_RATE_SPIKE_RULE_NAME = "DNS query-rate spike"


async def seed_dns_query_anomaly_alert_rules() -> None:
    """Seed the two DNS query-anomaly rules (issue #371), DISABLED by default.

    Like the firewall-stalled rule, these are seeded off so their existence is
    discoverable in the Alerts UI without firing on installs that don't run
    agent-based BIND9 (and therefore have no ``dns_metric_sample`` data). Keyed
    on ``rule_type`` so an operator who enables / renames either is never
    overridden by a later boot.
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.alerts import AlertRule  # noqa: PLC0415

    seeds = [
        {
            "name": _DNS_NXDOMAIN_SPIKE_RULE_NAME,
            "rule_type": RULE_TYPE_DNS_NXDOMAIN_SPIKE,
            "description": (
                "Fires when a DNS server's NXDOMAIN responses reach "
                f"threshold_percent% of total queries (default "
                f"{_DNS_NXDOMAIN_RATIO_DEFAULT}%) AND the absolute NXDOMAIN "
                f"count clears min_free_addresses (default "
                f"{_DNS_NXDOMAIN_MIN_COUNT_DEFAULT}, the low-traffic guard) "
                "over a 15-minute window. Catches DGA malware beacons, broken "
                "clients, and mistyped-search-domain storms. Auto-resolves when "
                "the ratio falls back under threshold."
            ),
            "threshold_percent": _DNS_NXDOMAIN_RATIO_DEFAULT,
            "min_free_addresses": _DNS_NXDOMAIN_MIN_COUNT_DEFAULT,
        },
        {
            "name": _DNS_QUERY_RATE_SPIKE_RULE_NAME,
            "rule_type": RULE_TYPE_DNS_QUERY_RATE_SPIKE,
            "description": (
                "Fires when a DNS server's query total over the last 15 minutes "
                "exceeds the prior 15-minute window by threshold_percent% "
                f"(default {_DNS_QUERY_RATE_SPIKE_PCT_DEFAULT}% = ×3) AND clears "
                f"the min_free_addresses absolute floor (default "
                f"{_DNS_QUERY_RATE_MIN_DEFAULT}, so tiny servers don't page on a "
                "3→9 'spike'). Auto-resolves when the rate settles."
            ),
            "threshold_percent": _DNS_QUERY_RATE_SPIKE_PCT_DEFAULT,
            "min_free_addresses": _DNS_QUERY_RATE_MIN_DEFAULT,
        },
        {
            "name": "DNS rate limiting actively dropping",
            "rule_type": RULE_TYPE_DNS_RATE_LIMIT_DROPPING,
            "description": (
                "Fires when a BIND9 server's Response Rate Limiting drops more "
                "than min_free_addresses responses (default "
                f"{_DNS_RATE_LIMIT_DROP_MIN_DEFAULT}) over a 15-minute window — "
                "the server is actively shedding a query flood, i.e. likely "
                "under a DNS amplification attempt. Needs RRL enabled on the "
                "server group (issue #146 Phase 1). Auto-resolves when the flood "
                "subsides. threshold_percent is unused."
            ),
            "threshold_percent": None,
            "min_free_addresses": _DNS_RATE_LIMIT_DROP_MIN_DEFAULT,
        },
    ]
    async with AsyncSessionLocal() as session:
        for seed in seeds:
            existing = await session.scalar(
                select(AlertRule).where(AlertRule.rule_type == seed["rule_type"])
            )
            if existing is not None:
                continue
            session.add(
                AlertRule(
                    name=seed["name"],
                    description=seed["description"],
                    rule_type=seed["rule_type"],
                    threshold_percent=seed["threshold_percent"],
                    min_free_addresses=seed["min_free_addresses"],
                    severity="warning",
                    enabled=False,
                    notify_syslog=True,
                    notify_webhook=True,
                    notify_smtp=False,
                )
            )
        await session.commit()


async def seed_ip_hygiene_alert_rules() -> None:
    """Seed the three IP-reconciliation hygiene rules (issue #369), DISABLED by
    default so installs that don't run discovery aren't suddenly paged. Keyed on
    ``rule_type`` — an operator who enables / renames one is never overridden.
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.alerts import AlertRule  # noqa: PLC0415

    seeds = [
        {
            "name": "IP free but responding",
            "rule_type": RULE_TYPE_IP_FREE_BUT_RESPONDING,
            "description": (
                "Fires on an IP marked 'available' that answered on the wire "
                f"within threshold_days (default {_FREE_RESPONDING_RECENCY_DAYS}) "
                "— IPAM thinks it's free but a host is using it. Reclaim it or "
                "investigate. Needs subnet discovery (ping/ARP sweep or SNMP)."
            ),
            "threshold_days": _FREE_RESPONDING_RECENCY_DAYS,
            "severity": "warning",
        },
        {
            "name": "Stale reservation",
            "rule_type": RULE_TYPE_STALE_RESERVATION,
            "description": (
                "Fires on a reserved / static_dhcp IP not seen on the wire for "
                f"more than threshold_days (default {_STALE_RESERVATION_DAYS}). "
                "The reservation-aware companion to the stale-IP alert (which is "
                "allocated-only). Verify the host or release the reservation."
            ),
            "threshold_days": _STALE_RESERVATION_DAYS,
            "severity": "info",
        },
        {
            "name": "Unknown MAC in static range",
            "rule_type": RULE_TYPE_UNKNOWN_MAC_IN_STATIC_RANGE,
            "description": (
                "Fires when a reserved / static_dhcp IP is answered by a MAC that "
                "differs from the one recorded on the row, observed within "
                f"threshold_days (default {_SQUAT_RECENCY_DAYS}) — a squatter or a "
                "device that moved. Needs subnet discovery (ping/ARP or SNMP)."
            ),
            "threshold_days": _SQUAT_RECENCY_DAYS,
            "severity": "warning",
        },
    ]
    async with AsyncSessionLocal() as session:
        for seed in seeds:
            existing = await session.scalar(
                select(AlertRule).where(AlertRule.rule_type == seed["rule_type"])
            )
            if existing is not None:
                continue
            session.add(
                AlertRule(
                    name=seed["name"],
                    description=seed["description"],
                    rule_type=seed["rule_type"],
                    threshold_days=seed["threshold_days"],
                    severity=seed["severity"],
                    enabled=False,
                    notify_syslog=True,
                    notify_webhook=True,
                    notify_smtp=False,
                )
            )
        await session.commit()


async def seed_rogue_dhcp_alert_rule() -> None:
    """Seed the singleton ``rogue_dhcp`` rule (issue #370), DISABLED by default.

    Meaningful only once an operator turns on the agent's active DHCP probe
    (``DHCP_ROGUE_PROBE_ENABLED=1``); seeding it off makes it discoverable in
    the Alerts UI without firing on installs that never opt in. Keyed on
    ``rule_type`` — an operator who enables / renames it is never overridden.
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.alerts import AlertRule  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(AlertRule).where(AlertRule.rule_type == RULE_TYPE_ROGUE_DHCP)
        )
        if existing is not None:
            return
        session.add(
            AlertRule(
                name="Rogue DHCP server",
                description=(
                    "Fires when the DHCP agent's active probe sees an OFFER from "
                    "a DHCP server that isn't a known group member and isn't on "
                    "the responder allowlist — a rogue or misconfigured DHCP "
                    "server on the segment. Auto-resolves when the responder "
                    "stops appearing or is acknowledged. Enable once the probe "
                    "(DHCP_ROGUE_PROBE_ENABLED) is on."
                ),
                rule_type=RULE_TYPE_ROGUE_DHCP,
                threshold_days=_ROGUE_DHCP_RECENCY_DAYS,
                severity="warning",
                enabled=False,
                notify_syslog=True,
                notify_webhook=True,
                notify_smtp=False,
            )
        )
        await session.commit()


async def seed_rogue_ra_alert_rule() -> None:
    """Seed the singleton ``rogue_ra`` rule (issue #524), DISABLED by default.

    Meaningful only once an operator turns on the agent's passive RA sniffer
    (``DHCP_RA_SNIFFER_ENABLED=1``); seeding it off makes it discoverable in
    the Alerts UI without firing on installs that never opt in. Keyed on
    ``rule_type`` — an operator who enables / renames it is never overridden.
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.alerts import AlertRule  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(AlertRule).where(AlertRule.rule_type == RULE_TYPE_ROGUE_RA)
        )
        if existing is not None:
            return
        session.add(
            AlertRule(
                name="Rogue IPv6 router (RA)",
                description=(
                    "Fires when the DHCP agent's passive RA sniffer sees a Router "
                    "Advertisement from an IPv6 router that isn't on the RA "
                    "allowlist — a rogue or misconfigured router on the segment. "
                    "Auto-resolves when the router stops appearing or is "
                    "acknowledged. Enable once the sniffer "
                    "(DHCP_RA_SNIFFER_ENABLED) is on."
                ),
                rule_type=RULE_TYPE_ROGUE_RA,
                threshold_days=_ROGUE_RA_RECENCY_DAYS,
                severity="warning",
                enabled=False,
                notify_syslog=True,
                notify_webhook=True,
                notify_smtp=False,
            )
        )
        await session.commit()


async def seed_new_mac_seen_alert_rule() -> None:
    """Seed the singleton ``new_mac_seen`` rule (issue #459), DISABLED by default.

    The companion to the ``security.new_device_watch`` feature module: noisy
    until the operator runs a baseline import to mark the existing fleet as
    ``known``, so it seeds off and discoverable in the Alerts UI. Keyed on
    ``rule_type`` — an operator who enables / renames it is never overridden.
    Excludes locally-administered (randomised) MACs by default (``classification``
    left NULL; set to ``"all"`` to include them).
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.alerts import AlertRule  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(AlertRule).where(AlertRule.rule_type == RULE_TYPE_NEW_MAC_SEEN)
        )
        if existing is not None:
            return
        session.add(
            AlertRule(
                name="New device on the network",
                description=(
                    "Fires when a MAC address never seen before appears on the "
                    "network (via a DHCP lease, SNMP ARP/FDB, the ping/ARP sweep, "
                    "or the opt-in L2 sniffer) and is not allowlisted or part of "
                    "the allocated fleet. Auto-resolves when the MAC is "
                    "acknowledged, allowlisted, or ages out of the window. "
                    "Enable once new-device watch (security.new_device_watch) is "
                    "on and you've run a baseline import. Randomised (privacy) "
                    "MACs are skipped by default."
                ),
                rule_type=RULE_TYPE_NEW_MAC_SEEN,
                threshold_days=_NEW_MAC_SEEN_RECENCY_DAYS,
                severity="info",
                enabled=False,
                notify_syslog=True,
                notify_webhook=True,
                notify_smtp=False,
            )
        )
        await session.commit()


async def seed_bgp_hijack_alert_rules() -> None:
    """Seed the two ``bgp_prefix_hijack`` / ``bgp_more_specific_announced``
    rules (issue #527), DISABLED by default.

    External-signal rules over the global routing table are noisy until
    the operator has curated the tracked-prefix + allowlist set, so they
    seed off (matching the ``rogue_dhcp`` / ``rogue_ra`` precedent) —
    discoverable in the Alerts UI without firing on installs that never
    turn on BGP monitoring (``PlatformSettings.bgp_monitoring_enabled``).
    Keyed on ``rule_type``; an operator who enables / renames one is
    never overridden.
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.alerts import AlertRule  # noqa: PLC0415

    seeds = (
        (
            RULE_TYPE_BGP_PREFIX_HIJACK,
            "BGP prefix hijack",
            (
                "Fires when an unexpected origin AS is observed announcing one "
                "of your tracked prefixes on the public routing table (RIPEstat "
                "poll, or the optional RIS Live feed). Severity escalates to "
                "critical when RPKI says the announcement is invalid, warning "
                "when RPKI coverage is unknown. Auto-resolves when the "
                "announcement delists or is acknowledged. Enable once BGP "
                "monitoring (Settings → bgp_monitoring_enabled) is on."
            ),
        ),
        (
            RULE_TYPE_BGP_MORE_SPECIFIC,
            "BGP more-specific announced",
            (
                "Fires when an unexpected origin AS announces a MORE-SPECIFIC "
                "sub-prefix of one of your tracked prefixes — the classic "
                "sub-prefix hijack that wins BGP best-path by longest match. "
                "Severity escalates on RPKI-invalid. Auto-resolves when the "
                "sub-prefix delists or is acknowledged. Enable once BGP "
                "monitoring is on."
            ),
        ),
    )

    async with AsyncSessionLocal() as session:
        for rule_type, name, description in seeds:
            existing = await session.scalar(
                select(AlertRule).where(AlertRule.rule_type == rule_type)
            )
            if existing is not None:
                continue
            session.add(
                AlertRule(
                    name=name,
                    description=description,
                    rule_type=rule_type,
                    severity="warning",
                    enabled=False,
                    notify_syslog=True,
                    notify_webhook=True,
                    notify_smtp=False,
                )
            )
        await session.commit()


async def seed_bgp_lg_alert_rules() -> None:
    """Seed the six ``bgp_lg_*`` rules (issue #566 Phase 5), DISABLED by
    default — the Looking Glass collector needs an operator-configured
    peer (and, for unexpected_origin/more_specific, at least one
    BGPTrackedPrefix owned-prefix row from the #527 UI) before any of
    these mean anything. Discoverable-but-off, matching the
    bgp_prefix_hijack / bgp_more_specific_announced precedent
    (``seed_bgp_hijack_alert_rules`` above). Keyed on ``rule_type``; an
    operator who enables/renames one is never overridden.
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.alerts import AlertRule  # noqa: PLC0415

    seeds: tuple[tuple[str, str, str, int | None], ...] = (
        (
            RULE_TYPE_BGP_LG_SESSION_DOWN,
            "BGP Looking Glass session down",
            (
                "Fires when a configured Looking Glass peer session drops out of "
                "Established for longer than a short grace window. Shows the last "
                "known prefix count and cross-references the owning collector's "
                "own health. Auto-resolves the moment the session re-establishes. "
                "Enable once you've configured at least one peer under "
                "Network → BGP Looking Glass."
            ),
            None,
        ),
        (
            RULE_TYPE_BGP_LG_RPKI_INVALID_ROUTE,
            "BGP Looking Glass RPKI-invalid route",
            (
                "Fires when a route in YOUR live routing table (not the public "
                "table — see the separate BGP prefix hijack rule for that) has an "
                "RPKI status of invalid: a ROA covers the prefix but does not "
                "authorise the observed origin/length. Always critical severity. "
                "Auto-resolves when the route withdraws or its RPKI status "
                "changes. The strongest in-network leak/misconfig signal."
            ),
            None,
        ),
        (
            RULE_TYPE_BGP_LG_UNEXPECTED_ORIGIN,
            "BGP Looking Glass unexpected origin",
            (
                "Fires when one of your tracked/owned prefixes (configured under "
                "an ASN's Tracked Prefixes — the same list the BGP prefix hijack "
                "rule reads) is learned in your OWN live table with an origin ASN "
                "outside the expected/allowlisted set. Catches internal leaks and "
                "fat-fingered redistribution before they reach the public table. "
                "Requires at least one enabled tracked prefix to ever fire."
            ),
            None,
        ),
        (
            RULE_TYPE_BGP_LG_MORE_SPECIFIC,
            "BGP Looking Glass more-specific announced",
            (
                "Fires when a route strictly more-specific than one of your "
                "tracked/owned aggregates is learned in your live table with an "
                "unexpected origin ASN — the classic internal sub-prefix leak "
                "that wins best-path over your aggregate via longest-match. "
                "Requires at least one enabled tracked prefix to ever fire."
            ),
            None,
        ),
        (
            RULE_TYPE_BGP_LG_ROUTE_FLAP,
            "BGP Looking Glass route flap",
            (
                "Fires when a learned route's flap count (announce/withdraw "
                "churn) crosses the configured threshold (Threshold %, reused "
                "here as a raw flap-count floor — default 5) with the most "
                "recent flap inside the trailing ~10 minutes. Auto-resolves once "
                "the route stops flapping for that window."
            ),
            _BGP_LG_FLAP_COUNT_DEFAULT,
        ),
        (
            RULE_TYPE_BGP_LG_MISSING_ADVERTISEMENT,
            "BGP Looking Glass missing advertisement",
            (
                "Fires when a subnet flagged 'should advertise via BGP' "
                "(Subnet.bgp_should_advertise) has no active learned route "
                "covering it across any configured peer — catches 'why is this "
                "network unreachable' before the tickets come in. Requires "
                "flagging at least one subnet as bgp_should_advertise=true to "
                "ever fire."
            ),
            None,
        ),
    )

    async with AsyncSessionLocal() as session:
        for rule_type, name, description, default_threshold in seeds:
            existing = await session.scalar(
                select(AlertRule).where(AlertRule.rule_type == rule_type)
            )
            if existing is not None:
                continue
            session.add(
                AlertRule(
                    name=name,
                    description=description,
                    rule_type=rule_type,
                    severity="warning",
                    enabled=False,
                    notify_syslog=True,
                    notify_webhook=True,
                    notify_smtp=False,
                    threshold_percent=default_threshold,
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


_TLS_CERT_RULE_SEEDS: list[dict[str, object]] = [
    {
        "name": "TLS cert expiring",
        "rule_type": RULE_TYPE_TLS_CERT_EXPIRING,
        "severity": "warning",
        "threshold_days": 30,
        "description": (
            "Fires when a monitored TLS endpoint's served certificate is "
            "within threshold_days of expiry. Severity escalates info → "
            "warning → critical as the expiry nears. Auto-resolves on renewal."
        ),
    },
    {
        "name": "TLS cert chain invalid",
        "rule_type": RULE_TYPE_TLS_CERT_CHAIN_INVALID,
        "severity": "critical",
        "threshold_days": None,
        "description": (
            "Fires when a monitored endpoint's certificate is reachable and "
            "unexpired but not usable: an untrusted chain (self-signed / wrong "
            "CA / broken chain) or a trusted cert served for the wrong hostname "
            "(SAN/CN mismatch). Expiry is covered by the expiring rule. "
            "Auto-resolves once the cert validates and matches the hostname."
        ),
    },
    {
        "name": "TLS cert unreachable",
        "rule_type": RULE_TYPE_TLS_CERT_UNREACHABLE,
        "severity": "warning",
        "threshold_days": None,
        "description": (
            "Fires when a monitored endpoint can't be probed (TCP refused / "
            "TLS handshake failed / DNS) for a couple of consecutive cycles. "
            "Auto-resolves on the next successful probe."
        ),
    },
    {
        "name": "TLS cert changed",
        "rule_type": RULE_TYPE_TLS_CERT_CHANGED,
        "severity": "info",
        "threshold_days": None,
        "description": (
            "Fires once when a monitored endpoint's certificate fingerprint "
            "changes unexpectedly (legitimate on renewal, suspicious "
            "otherwise). Auto-resolves after the transition window."
        ),
    },
    {
        "name": "TLS cert issuer changed",
        "rule_type": RULE_TYPE_TLS_CERT_ISSUER_CHANGED,
        "severity": "warning",
        "threshold_days": None,
        "description": (
            "Fires once when a monitored endpoint's certificate comes back "
            "from a DIFFERENT issuing CA — cert-rotation deviation (e.g. a "
            "normally-Let's-Encrypt cert suddenly issued by another CA), a "
            "higher-signal subset of 'cert changed'. Auto-resolves after the "
            "transition window."
        ),
    },
]


async def seed_tls_cert_alert_rules() -> None:
    """Seed the four ``tls_cert_*`` rules (issue #118), DISABLED by default.

    Opt-in like the other monitoring signals — seeding them surfaces their
    existence in the Alerts UI without firing on installs that never add a
    probe target. Keyed on ``rule_type`` (one per type); an operator who
    enables / renames one is never overridden.
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.alerts import AlertRule  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        for seed in _TLS_CERT_RULE_SEEDS:
            existing = await session.scalar(
                select(AlertRule).where(AlertRule.rule_type == seed["rule_type"])
            )
            if existing is not None:
                continue
            session.add(
                AlertRule(
                    name=seed["name"],
                    description=seed["description"],
                    rule_type=seed["rule_type"],
                    severity=seed["severity"],
                    threshold_days=seed["threshold_days"],
                    enabled=False,
                    notify_syslog=True,
                    notify_webhook=True,
                    notify_smtp=False,
                )
            )
        await session.commit()


async def _matching_ip_blocklisted_subjects(
    db: AsyncSession, rule: AlertRule
) -> list[tuple[str, str, str]]:
    """Public-facing IPs currently listed on ≥1 enabled DNSBL (#528).

    Recurring-condition rule — one subject per listed IP. The shared
    open/resolve loop opens an event on first listing and auto-resolves it
    when the IP drops out of this set (the sweep flips ``listed=False``).
    ``subject_id`` is the IP so the latch survives list churn: the IP stays
    a subject as long as ANY enabled list has it.
    """
    from app.models.dnsbl import DNSBLList, DNSBLListing  # noqa: PLC0415

    rows = (
        await db.execute(
            select(DNSBLListing, DNSBLList.name)
            .join(DNSBLList, DNSBLList.id == DNSBLListing.list_id)
            .where(DNSBLListing.listed.is_(True), DNSBLList.enabled.is_(True))
        )
    ).all()
    by_ip: dict[str, list[str]] = {}
    for listing, list_name in rows:
        by_ip.setdefault(str(listing.ip), []).append(list_name)

    out: list[tuple[str, str, str]] = []
    for ip, list_names in sorted(by_ip.items()):
        names = ", ".join(sorted(list_names))
        msg = (
            f"IP {ip} is listed on {len(list_names)} DNS blocklist(s): {names}. "
            "Mail deliverability / reputation may be affected."
        )
        out.append((ip, ip, msg))
    return out


async def seed_ip_blocklisted_alert_rule() -> None:
    """Seed the ``ip_blocklisted`` rule (#528), DISABLED by default.

    Opt-in like the other monitoring signals — surfaces the rule in the
    Alerts UI without firing on installs that never enable the DNSBL sweep.
    Keyed on ``rule_type``; an operator who enables / renames it is never
    overridden.
    """
    from app.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.alerts import AlertRule  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(AlertRule).where(AlertRule.rule_type == RULE_TYPE_IP_BLOCKLISTED)
        )
        if existing is not None:
            return
        session.add(
            AlertRule(
                name="IP on DNS blocklist",
                description=(
                    "Fires when a public-facing IP (public IPAM address, "
                    "internet-facing subnet, NAT/PAT egress, or operator-pinned) "
                    "is found on one or more enabled DNS blocklists (Spamhaus, "
                    "Barracuda, SpamCop, SORBS, …). Auto-resolves when the daily "
                    "sweep finds the IP delisted. Requires the DNSBL sweep enabled."
                ),
                rule_type=RULE_TYPE_IP_BLOCKLISTED,
                severity="warning",
                enabled=False,
                notify_syslog=True,
                notify_webhook=True,
                notify_smtp=False,
            )
        )
        await session.commit()


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
            elif rule.rule_type == RULE_TYPE_STALE_IP_COUNT:
                base = await _matching_stale_ip_count_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "subnet"
            elif rule.rule_type == RULE_TYPE_DHCP_POOL_EXHAUSTION:
                base = await _matching_dhcp_pool_exhaustion_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "dhcp_pool"
            elif rule.rule_type == RULE_TYPE_DNS_NXDOMAIN_SPIKE:
                base = await _matching_dns_nxdomain_spike_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "dns_server"
            elif rule.rule_type == RULE_TYPE_DNS_QUERY_RATE_SPIKE:
                base = await _matching_dns_query_rate_spike_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "dns_server"
            elif rule.rule_type == RULE_TYPE_DNS_RATE_LIMIT_DROPPING:
                base = await _matching_dns_rate_limit_dropping_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "dns_server"
            elif rule.rule_type == RULE_TYPE_IP_FREE_BUT_RESPONDING:
                base = await _matching_ip_free_but_responding_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "ip_address"
            elif rule.rule_type == RULE_TYPE_STALE_RESERVATION:
                base = await _matching_stale_reservation_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "ip_address"
            elif rule.rule_type == RULE_TYPE_UNKNOWN_MAC_IN_STATIC_RANGE:
                base = await _matching_unknown_mac_in_static_range_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "ip_address"
            elif rule.rule_type == RULE_TYPE_ROGUE_DHCP:
                base = await _matching_rogue_dhcp_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "dhcp_responder"
            elif rule.rule_type == RULE_TYPE_ROGUE_RA:
                base = await _matching_rogue_ra_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "ra_router"
            elif rule.rule_type == RULE_TYPE_NEW_MAC_SEEN:
                base = await _matching_new_mac_seen_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "ip_mac_observation"
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
            elif rule.rule_type == RULE_TYPE_BGP_PREFIX_HIJACK:
                matches = await _matching_bgp_hijack_subjects(db, rule, "prefix_hijack")
                subject_type = "bgp_hijack"
            elif rule.rule_type == RULE_TYPE_BGP_MORE_SPECIFIC:
                matches = await _matching_bgp_hijack_subjects(db, rule, "more_specific")
                subject_type = "bgp_hijack"
            elif rule.rule_type == RULE_TYPE_BGP_LG_SESSION_DOWN:
                base = await _matching_bgp_lg_session_down_subjects(db, rule, now)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "bgp_lg_peer"
            elif rule.rule_type == RULE_TYPE_BGP_LG_RPKI_INVALID_ROUTE:
                matches = await _matching_bgp_lg_rpki_invalid_route_subjects(db, rule)
                subject_type = "bgp_lg_route"
            elif rule.rule_type == RULE_TYPE_BGP_LG_UNEXPECTED_ORIGIN:
                base = await _matching_bgp_lg_unexpected_origin_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "bgp_lg_route"
            elif rule.rule_type == RULE_TYPE_BGP_LG_MORE_SPECIFIC:
                base = await _matching_bgp_lg_more_specific_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "bgp_lg_route"
            elif rule.rule_type == RULE_TYPE_BGP_LG_ROUTE_FLAP:
                base = await _matching_bgp_lg_route_flap_subjects(db, rule, now)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "bgp_lg_route"
            elif rule.rule_type == RULE_TYPE_BGP_LG_MISSING_ADVERTISEMENT:
                base = await _matching_bgp_lg_missing_advertisement_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in base]
                subject_type = "subnet"
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
            elif rule.rule_type == RULE_TYPE_K3S_API_CERT_EXPIRING:
                expiring = await _matching_k3s_api_cert_expiring_subjects(db, rule, now)
                matches = [(sid, disp, msg, sev) for sid, disp, msg, sev in expiring]
                subject_type = "appliance"
            elif rule.rule_type == RULE_TYPE_SECRET_EXPIRING:
                expiring = await _matching_secret_expiring_subjects(db, rule, now)
                matches = [(sid, disp, msg, sev) for sid, disp, msg, sev in expiring]
                subject_type = "secret"
            elif rule.rule_type == RULE_TYPE_FIREWALL_APPLY_STALLED:
                stalled = await _matching_firewall_apply_stalled_subjects(db, rule, now)
                matches = [(sid, disp, msg, sev) for sid, disp, msg, sev in stalled]
                subject_type = "appliance"
            elif rule.rule_type == RULE_TYPE_SERVICE_TERM_EXPIRING:
                expiring = await _matching_service_term_expiring_subjects(db, rule, now)
                matches = [(sid, disp, msg, sev) for sid, disp, msg, sev in expiring]
                subject_type = "network_service"
            elif rule.rule_type == RULE_TYPE_DECOM_EXPIRING:
                expiring = await _matching_decom_expiring_subjects(db, rule, now)
                matches = [(sid, disp, msg, sev) for sid, disp, msg, sev in expiring]
                subject_type = "subnet"
            elif rule.rule_type == RULE_TYPE_TLS_CERT_EXPIRING:
                expiring = await _matching_tls_cert_expiring_subjects(db, rule, now)
                matches = [(sid, disp, msg, sev) for sid, disp, msg, sev in expiring]
                subject_type = "tls_cert"
            elif rule.rule_type == RULE_TYPE_TLS_CERT_CHAIN_INVALID:
                invalid = await _matching_tls_cert_chain_invalid_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in invalid]
                subject_type = "tls_cert"
            elif rule.rule_type == RULE_TYPE_TLS_CERT_UNREACHABLE:
                down = await _matching_tls_cert_unreachable_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in down]
                subject_type = "tls_cert"
            elif rule.rule_type == RULE_TYPE_TLS_CERT_CHANGED:
                # Transition-once — latches the fingerprint pair + auto-resolves.
                op_, res_, dsy, dwh, dsm = await _evaluate_tls_cert_transition_rule(
                    db, rule, now, value_attr="fingerprint_sha256", what="fingerprint"
                )
                opened += op_
                resolved += res_
                delivered_syslog += dsy
                delivered_webhook += dwh
                delivered_smtp += dsm
                continue
            elif rule.rule_type == RULE_TYPE_TLS_CERT_ISSUER_CHANGED:
                # Transition-once on the issuing CA — cert-rotation deviation.
                op_, res_, dsy, dwh, dsm = await _evaluate_tls_cert_transition_rule(
                    db, rule, now, value_attr="issuer_cn", what="issuer"
                )
                opened += op_
                resolved += res_
                delivered_syslog += dsy
                delivered_webhook += dwh
                delivered_smtp += dsm
                continue
            elif rule.rule_type == RULE_TYPE_IP_BLOCKLISTED:
                listed_ips = await _matching_ip_blocklisted_subjects(db, rule)
                matches = [(sid, disp, msg, None) for sid, disp, msg in listed_ips]
                subject_type = "ip_blocklist"
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
            elif rule.rule_type == RULE_TYPE_AUDIT_CHAIN_BROKEN:
                # Externally driven — the dedicated
                # ``app.tasks.audit_chain_verify.verify_audit_chain``
                # Celery task creates / resolves AlertEvent rows for
                # this rule on its own schedule (nightly + on-demand).
                # The general evaluator just silently passes; without
                # this branch the warning loop spammed once per
                # 60s tick.
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

            # Open new events for unseen matches; escalate existing ones.
            for subject_id, display, message, severity_override in matches:
                existing = open_by_subject.get(subject_id)
                if existing is not None:
                    # Subject is already open. For the *_expiring rule
                    # family the matcher recomputes a per-row severity
                    # every tick that climbs info → warning → critical as
                    # the expiry date nears (issue #46). Bump the open
                    # event and re-deliver when that severity is *higher*
                    # than what's already recorded — never downgrade and
                    # never re-deliver on an unchanged severity (avoids
                    # 60 s notification spam). Non-escalating rules pass a
                    # stable severity, so the rank compare never trips and
                    # they're left untouched.
                    new_severity = severity_override or rule.severity
                    if _severity_rank(new_severity) > _severity_rank(existing.severity):
                        existing.severity = new_severity
                        existing.message = message
                        ds, dw, dm = await _deliver(rule, existing, targets)
                        # OR-in: a channel that delivered on open should
                        # stay flagged even if a later escalation skips it.
                        existing.delivered_syslog = existing.delivered_syslog or ds
                        existing.delivered_webhook = existing.delivered_webhook or dw
                        existing.delivered_smtp = existing.delivered_smtp or dm
                        if ds:
                            delivered_syslog += 1
                        if dw:
                            delivered_webhook += 1
                        if dm:
                            delivered_smtp += 1
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
