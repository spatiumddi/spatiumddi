"""BGP prefix-hijack evaluation core (issue #527).

Pure, side-effect-scoped helpers shared by BOTH delivery mechanisms:

* the periodic RIPEstat poll (``app.tasks.bgp_hijack_poll``) — the
  reliable source of truth that works on any standard Celery
  deployment; and
* the optional RIS Live WebSocket consumer
  (``app.services.bgp.ris_live``) — the real-time upgrade, gated behind
  ``settings.bgp_ris_live_enabled`` and default OFF.

Keeping the evaluation logic here (rather than in the task) means the
origin-mismatch + RPKI-validity + latch semantics are identical no
matter which mechanism observed the announcement.

RPKI status semantics (reusing the ROA data already pulled by
``app.tasks.rpki_roa_refresh``):

* ``invalid`` — a ROA covers ``observed_prefix`` but does NOT authorise
  ``observed_origin`` (wrong origin AS, or the announced length exceeds
  the ROA ``max_length``). Highest confidence it's a hijack →
  ``critical``.
* ``unknown`` — no ROA covers the prefix at all. Still a mismatch
  against our expected origin, but RPKI can't confirm → ``warning``.
* ``valid`` — a ROA authorises ``observed_origin`` for the prefix. This
  is legitimate multi-origin (anycast / migration), NOT a hijack — we
  never open a detection for it.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asn import ASN, ASNRpkiRoa
from app.models.bgp_monitor import BGPHijackDetection, BGPTrackedPrefix

logger = structlog.get_logger(__name__)

# ``prefix_hijack`` — someone else announcing our EXACT tracked CIDR.
# ``more_specific`` — an unexpected origin announcing a more-specific
# slice of our tracked prefix (classic sub-prefix hijack; wins BGP
# longest-match).
KIND_PREFIX_HIJACK = "prefix_hijack"
KIND_MORE_SPECIFIC = "more_specific"

RPKI_INVALID = "invalid"
RPKI_UNKNOWN = "unknown"
RPKI_VALID = "valid"

# How long an announcement must be absent before we resolve its
# detection. Comfortably larger than one poll interval so a single
# missed RIPEstat sample doesn't flap the alert.
DEFAULT_DELIST_WINDOW = timedelta(hours=12)

# Defensive per-pass caps so a badly-configured (huge) tracked-prefix
# set can't fan out to thousands of external calls / detection rows.
MAX_PREFIXES_PER_ASN = 256


def _parse_net(value: str | None) -> ipaddress._BaseNetwork | None:
    if not value:
        return None
    try:
        return ipaddress.ip_network(str(value), strict=False)
    except ValueError:
        return None


def severity_for_rpki(status: str) -> str:
    """Per-detection severity override. ``invalid`` is the strongest
    signal (a valid ROA actively contradicts the announcement)."""
    return "critical" if status == RPKI_INVALID else "warning"


async def derive_rpki_status(
    db: AsyncSession,
    observed_prefix: str,
    observed_origin: int,
) -> str:
    """Classify an observed announcement against the ROA table.

    Walks every ROA whose prefix *covers* ``observed_prefix`` (supernet
    or equal). If one authorises ``observed_origin`` within its
    ``max_length`` → ``valid``. If ROAs cover the prefix but none
    authorise the origin → ``invalid``. No covering ROA → ``unknown``.

    We join ROA → ASN to recover the authorised origin AS number
    (``asn.number``); the ROA row itself only carries ``asn_id``.
    """
    obs = _parse_net(observed_prefix)
    if obs is None:
        return RPKI_UNKNOWN

    rows = (
        await db.execute(
            select(ASNRpkiRoa, ASN.number).join(ASN, ASNRpkiRoa.asn_id == ASN.id)
            # Only ROAs whose prefix COVERS (is a supernet of, or equals)
            # the observed prefix. Pushes the covering filter into Postgres
            # via the cidr ``>>=`` operator instead of full-scanning the
            # whole ROA table on every call (once per unexpected origin per
            # prefix per pass). Different address families never contain
            # each other, so ``>>=`` also does the version filter for free.
            # The Python supernet re-check below stays as a defensive belt
            # so behaviour is byte-identical to the prior full-scan.
            .where(ASNRpkiRoa.prefix.op(">>=")(str(obs)))
        )
    ).all()

    covering = False
    for roa, roa_asn_number in rows:
        roa_net = _parse_net(str(roa.prefix))
        if roa_net is None or roa_net.version != obs.version:
            continue
        # ``supernet_of`` (equal or looser) — the ROA prefix must
        # contain the observed prefix.
        try:
            contains = roa_net.supernet_of(obs)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            contains = False
        if not contains:
            continue
        covering = True
        # A ROA authorises the announcement iff the origin matches AND
        # the announced length is within ``max_length``.
        if int(roa_asn_number) == int(observed_origin) and obs.prefixlen <= int(roa.max_length):
            return RPKI_VALID

    return RPKI_INVALID if covering else RPKI_UNKNOWN


async def derive_rpki_status_batch(
    db: AsyncSession,
    pairs: Iterable[tuple[str, int]],
) -> dict[tuple[str, int], str]:
    """Classify many ``(prefix, origin)`` announcements in ONE ROA load.

    ``derive_rpki_status`` issues one covering-ROA query per call; a pushed
    BGP RIB snapshot has thousands of distinct announcements, so calling it
    per-prefix means thousands of serial round-trips inside the ingest
    transaction. This loads the ROA table once and matches every pair in
    memory with byte-identical semantics to the single-shot function.
    """
    wanted = {(str(p), int(o)) for p, o in pairs}
    if not wanted:
        return {}

    rows = (
        await db.execute(
            select(ASNRpkiRoa.prefix, ASNRpkiRoa.max_length, ASN.number).join(
                ASN, ASNRpkiRoa.asn_id == ASN.id
            )
        )
    ).all()
    # Pre-parse the ROA set once.
    roas: list[tuple[Any, int, int]] = []
    for roa_prefix, max_length, asn_number in rows:
        net = _parse_net(str(roa_prefix))
        if net is None:
            continue
        roas.append((net, int(asn_number), int(max_length)))

    out: dict[tuple[str, int], str] = {}
    for prefix, origin in wanted:
        obs = _parse_net(prefix)
        if obs is None:
            out[(prefix, origin)] = RPKI_UNKNOWN
            continue
        covering = False
        status = RPKI_UNKNOWN
        for net, roa_origin, max_length in roas:
            if net.version != obs.version:
                continue
            try:
                contains = net.supernet_of(obs)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                contains = False
            if not contains:
                continue
            covering = True
            if roa_origin == origin and obs.prefixlen <= max_length:
                status = RPKI_VALID
                break
        if status != RPKI_VALID:
            status = RPKI_INVALID if covering else RPKI_UNKNOWN
        out[(prefix, origin)] = status
    return out


def expected_origin_set(tracked: BGPTrackedPrefix) -> set[int]:
    """The origins that may legitimately announce this prefix — the
    tracked AS's own number plus any operator-allowlisted extras."""
    allowed: set[int] = {int(tracked.expected_origin_asn)}
    for extra in tracked.allowed_origins or []:
        try:
            allowed.add(int(extra))
        except (TypeError, ValueError):
            continue
    return allowed


async def record_detection(
    db: AsyncSession,
    *,
    tracked: BGPTrackedPrefix,
    observed_prefix: str,
    observed_origin: int,
    detection_kind: str,
    rpki_status: str,
    now: datetime,
    source: str = "ripestat_poll",
    detail: dict[str, Any] | None = None,
) -> tuple[BGPHijackDetection, bool]:
    """Upsert the open detection for this ``(asn, observed_prefix,
    observed_origin, kind)`` tuple. Returns ``(row, opened)`` where
    ``opened`` is True only on first observation.

    This is the dedup/latch chokepoint: while an announcement persists
    we bump ``last_seen_at`` on the same row so the alert evaluator sees
    one stable subject; the row resolves via
    :func:`resolve_stale_detections` once the announcement delists.
    """
    existing = await db.scalar(
        select(BGPHijackDetection).where(
            BGPHijackDetection.asn_id == tracked.asn_id,
            BGPHijackDetection.observed_prefix == observed_prefix,
            BGPHijackDetection.observed_origin_asn == observed_origin,
            BGPHijackDetection.detection_kind == detection_kind,
            BGPHijackDetection.resolved_at.is_(None),
        )
    )
    severity = severity_for_rpki(rpki_status)
    if existing is not None:
        existing.last_seen_at = now
        existing.rpki_status = rpki_status
        existing.severity = severity
        if detail is not None:
            existing.detail = detail
        # A re-observation via RIS Live on a poll-opened row (or vice
        # versa) keeps the earliest source label.
        return existing, False

    row = BGPHijackDetection(
        tracked_prefix_id=tracked.id,
        asn_id=tracked.asn_id,
        tracked_prefix=str(tracked.prefix),
        observed_prefix=observed_prefix,
        expected_origin_asn=int(tracked.expected_origin_asn),
        observed_origin_asn=int(observed_origin),
        detection_kind=detection_kind,
        rpki_status=rpki_status,
        severity=severity,
        source=source,
        first_seen_at=now,
        last_seen_at=now,
        detail=detail,
    )
    db.add(row)
    await db.flush()
    return row, True


async def resolve_stale_detections(
    db: AsyncSession,
    *,
    asn_id: Any,
    now: datetime,
    delist_window: timedelta = DEFAULT_DELIST_WINDOW,
) -> int:
    """Resolve any open detection for this AS whose announcement hasn't
    been re-observed within ``delist_window``. Returns the resolved
    count. Mirrors the latch-and-auto-resolve behaviour of the
    ``domain_*`` / ``circuit_*`` alert rules."""
    cutoff = now - delist_window
    open_rows = (
        (
            await db.execute(
                select(BGPHijackDetection).where(
                    BGPHijackDetection.asn_id == asn_id,
                    BGPHijackDetection.resolved_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    resolved = 0
    for row in open_rows:
        if row.last_seen_at < cutoff:
            row.resolved_at = now
            resolved += 1
    return resolved


async def evaluate_tracked_prefix(
    db: AsyncSession,
    tracked: BGPTrackedPrefix,
    *,
    now: datetime,
    source: str = "ripestat_poll",
) -> dict[str, Any]:
    """Fetch the current announcements of one tracked prefix and record
    detections for every unexpected origin.

    * Exact-prefix origins come from RIPEstat ``prefix-overview``.
    * More-specific sub-prefixes come from RIPEstat ``related-prefixes``.

    Returns a per-prefix summary for the caller's counters + audit.
    """
    # Late import so the service module doesn't hard-depend on httpx at
    # import time (keeps the alert evaluator import light).
    from app.services.bgp import (  # noqa: PLC0415
        fetch_prefix_overview,
        fetch_related_prefixes,
    )

    allowed = expected_origin_set(tracked)
    opened = 0
    observed_origins: list[int] = []
    unavailable = False

    # ── exact-prefix hijack ───────────────────────────────────────────
    overview = await fetch_prefix_overview(str(tracked.prefix))
    if overview.get("available"):
        for entry in overview.get("asns") or []:
            origin = entry.get("asn")
            if origin is None:
                continue
            try:
                origin_i = int(origin)
            except (TypeError, ValueError):
                continue
            observed_origins.append(origin_i)
            if origin_i in allowed:
                continue
            status = await derive_rpki_status(db, str(tracked.prefix), origin_i)
            if status == RPKI_VALID:
                continue
            _row, was_opened = await record_detection(
                db,
                tracked=tracked,
                observed_prefix=str(tracked.prefix),
                observed_origin=origin_i,
                detection_kind=KIND_PREFIX_HIJACK,
                rpki_status=status,
                now=now,
                source=source,
                detail={"holder": entry.get("holder")},
            )
            if was_opened:
                opened += 1
    else:
        unavailable = True

    # ── more-specific (sub-prefix) hijack ─────────────────────────────
    related = await fetch_related_prefixes(str(tracked.prefix))
    if related.get("available"):
        tracked_net = _parse_net(str(tracked.prefix))
        for entry in related.get("prefixes") or []:
            rel = (entry.get("relationship") or "").lower()
            if "more specific" not in rel:
                continue
            sub_prefix = entry.get("prefix")
            origin = entry.get("origin_asn")
            if not sub_prefix or origin is None:
                continue
            try:
                origin_i = int(origin)
            except (TypeError, ValueError):
                continue
            if origin_i in allowed:
                continue
            # Guard: only sub-prefixes actually inside the tracked prefix.
            sub_net = _parse_net(str(sub_prefix))
            if tracked_net is None or sub_net is None or sub_net.version != tracked_net.version:
                continue
            try:
                inside = tracked_net.supernet_of(sub_net)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                inside = False
            if not inside or sub_net.prefixlen <= tracked_net.prefixlen:
                continue
            status = await derive_rpki_status(db, str(sub_prefix), origin_i)
            if status == RPKI_VALID:
                continue
            _row, was_opened = await record_detection(
                db,
                tracked=tracked,
                observed_prefix=str(sub_prefix),
                observed_origin=origin_i,
                detection_kind=KIND_MORE_SPECIFIC,
                rpki_status=status,
                now=now,
                source=source,
                detail={"relationship": entry.get("relationship")},
            )
            if was_opened:
                opened += 1
    else:
        unavailable = True

    if observed_origins:
        tracked.last_seen_origins = sorted(set(observed_origins))
    tracked.last_checked_at = now

    return {
        "prefix": str(tracked.prefix),
        "opened": opened,
        "observed_origins": sorted(set(observed_origins)),
        "unavailable": unavailable,
    }


async def refresh_tracked_prefixes_for_asn(
    db: AsyncSession,
    asn: ASN,
    *,
    now: datetime,
) -> int:
    """Reconcile the auto-managed tracked prefixes for one AS.

    Sources:
    * ``roa`` — every distinct prefix in the AS's ``asn_rpki_roa`` rows
      (the prefixes we've asserted authority over).
    * ``announced`` — RIPEstat ``announced-prefixes`` for the AS.

    Manual rows (``source="manual"``) are never touched here. Auto rows
    that vanish from BOTH sources are removed. ``expected_origin_asn``
    is (re)stamped from ``asn.number`` so a correction propagates.

    Returns the number of tracked-prefix rows added.
    """
    from app.services.bgp import fetch_announced_prefixes  # noqa: PLC0415

    # Gather source prefixes.
    roa_prefixes: set[str] = set()
    roa_rows = (
        (await db.execute(select(ASNRpkiRoa.prefix).where(ASNRpkiRoa.asn_id == asn.id)))
        .scalars()
        .all()
    )
    for p in roa_rows:
        net = _parse_net(str(p))
        if net is not None:
            roa_prefixes.add(str(net))

    announced_prefixes: set[str] = set()
    ann = await fetch_announced_prefixes(int(asn.number))
    if ann.get("available"):
        for entry in ann.get("prefixes") or []:
            net = _parse_net(entry.get("prefix"))
            if net is not None:
                announced_prefixes.add(str(net))

    source_by_prefix: dict[str, str] = {}
    for p in roa_prefixes | announced_prefixes:
        in_roa = p in roa_prefixes
        in_ann = p in announced_prefixes
        source_by_prefix[p] = "both" if (in_roa and in_ann) else ("roa" if in_roa else "announced")

    # Cap to keep the per-tick call fan-out bounded.
    if len(source_by_prefix) > MAX_PREFIXES_PER_ASN:
        logger.info(
            "bgp_tracked_prefix_capped",
            asn=int(asn.number),
            total=len(source_by_prefix),
            cap=MAX_PREFIXES_PER_ASN,
        )
        source_by_prefix = dict(list(source_by_prefix.items())[:MAX_PREFIXES_PER_ASN])

    existing_rows = (
        (await db.execute(select(BGPTrackedPrefix).where(BGPTrackedPrefix.asn_id == asn.id)))
        .scalars()
        .all()
    )
    existing_by_prefix: dict[str, BGPTrackedPrefix] = {}
    for row in existing_rows:
        net = _parse_net(str(row.prefix))
        existing_by_prefix[str(net) if net else str(row.prefix)] = row

    added = 0
    for prefix, src in source_by_prefix.items():
        row = existing_by_prefix.get(prefix)
        if row is None:
            db.add(
                BGPTrackedPrefix(
                    asn_id=asn.id,
                    prefix=prefix,
                    expected_origin_asn=int(asn.number),
                    source=src,
                    enabled=True,
                )
            )
            added += 1
        else:
            # Re-stamp derived fields; never touch a manual row's source.
            row.expected_origin_asn = int(asn.number)
            if row.source != "manual":
                row.source = src

    # Prune auto rows that disappeared from both sources.
    for prefix, row in existing_by_prefix.items():
        if row.source == "manual":
            continue
        if prefix not in source_by_prefix:
            await db.delete(row)

    return added


__all__ = [
    "KIND_PREFIX_HIJACK",
    "KIND_MORE_SPECIFIC",
    "RPKI_INVALID",
    "RPKI_UNKNOWN",
    "RPKI_VALID",
    "DEFAULT_DELIST_WINDOW",
    "derive_rpki_status",
    "severity_for_rpki",
    "expected_origin_set",
    "record_detection",
    "resolve_stale_detections",
    "evaluate_tracked_prefix",
    "refresh_tracked_prefixes_for_asn",
]
