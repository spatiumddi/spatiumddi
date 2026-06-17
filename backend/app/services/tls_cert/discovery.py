"""TLS cert target auto-discovery (issue #118, Phase 1).

Projects probe targets from DNS state, so the cert inventory tracks the
hostnames SpatiumDDI already manages instead of being a parallel list the
operator maintains by hand:

* **create** — one ``tls_cert_target(source='discovered')`` per A/AAAA
  record whose parent zone has ``auto_tls_probe=True`` *or* whose own
  ``auto_tls_probe=True``. Connect host is the record's stored ``fqdn``;
  the connect tuple ``(host, 443, NULL)`` dedupes the same name surfacing
  from multiple records.
* **disable** — a discovered target whose source record was opted out or
  soft-deleted is flagged ``enabled=False`` (kept, not deleted, so its
  probe history survives and it re-enables cleanly if the record returns).
* **relink** — fill any target's empty ``dns_zone_id`` / ``domain_id`` by
  longest-suffix match of its observed SANs against managed zones +
  domains (so a manually-added external cert still shows under the right
  Domain / DNS-zone Certificates tab once probed).

Soft-deleted ``dns_record`` / ``dns_zone`` rows are excluded automatically
by the ORM soft-delete query filter (``app.db._filter_soft_deleted``).

Mirrors the no-commit contract of the probe service — the caller (the
discovery task) owns the commit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dns import DNSRecord, DNSZone
from app.models.domain import Domain
from app.models.tls_cert import SOURCE_DISCOVERED, TLSCertTarget

logger = structlog.get_logger(__name__)


def _best_suffix_match(
    names: list[str], candidates_desc: list[tuple[uuid.UUID, str]]
) -> uuid.UUID | None:
    """Longest-suffix match of any name against ``candidates_desc`` (which
    must be pre-sorted longest-name-first). The leading ``.`` guard keeps
    ``example.com.au`` from matching the zone ``example.com``."""
    for cid, cname in candidates_desc:
        for name in names:
            if name == cname or name.endswith("." + cname):
                return cid
    return None


async def _relink_by_san(db: AsyncSession) -> int:
    """Fill empty zone/domain FKs on any target from its observed SANs."""
    zones = [
        (zid, (zname or "").rstrip(".").lower())
        for zid, zname in (await db.execute(select(DNSZone.id, DNSZone.name))).all()
    ]
    domains = [
        (did, (dname or "").rstrip(".").lower())
        for did, dname in (await db.execute(select(Domain.id, Domain.name))).all()
    ]
    zones.sort(key=lambda z: len(z[1]), reverse=True)
    domains.sort(key=lambda d: len(d[1]), reverse=True)

    relinked = 0
    # Only targets actually missing a link can be updated here — scanning
    # every target each tick is wasted work.
    targets = (
        (
            await db.execute(
                select(TLSCertTarget).where(
                    or_(
                        TLSCertTarget.dns_zone_id.is_(None),
                        TLSCertTarget.domain_id.is_(None),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    for t in targets:
        sans = [s.rstrip(".").lower() for s in (t.sans_json or []) if isinstance(s, str)]
        if not sans:
            continue
        if t.dns_zone_id is None:
            zid = _best_suffix_match(sans, zones)
            if zid is not None:
                t.dns_zone_id = zid
                relinked += 1
        if t.domain_id is None:
            did = _best_suffix_match(sans, domains)
            if did is not None:
                t.domain_id = did
                relinked += 1
    return relinked


async def reconcile_discovered_targets(
    db: AsyncSession, now: datetime | None = None
) -> dict[str, int]:
    """Create / disable discovered targets + relink by SAN. No commit."""
    _ = now or datetime.now(UTC)

    rows = (
        await db.execute(
            select(DNSRecord, DNSZone)
            .join(DNSZone, DNSRecord.zone_id == DNSZone.id)
            .where(
                DNSRecord.record_type.in_(("A", "AAAA")),
                or_(
                    DNSZone.auto_tls_probe.is_(True),
                    DNSRecord.auto_tls_probe.is_(True),
                ),
            )
        )
    ).all()

    # Dedupe candidates on the connect tuple (same FQDN from >1 record).
    candidates: dict[tuple[str, int, None], tuple[DNSRecord, DNSZone]] = {}
    for rec, zone in rows:
        host = (rec.fqdn or "").rstrip(".").lower()
        if not host:
            continue
        candidates.setdefault((host, 443, None), (rec, zone))

    existing = (
        (await db.execute(select(TLSCertTarget).where(TLSCertTarget.source == SOURCE_DISCOVERED)))
        .scalars()
        .all()
    )
    existing_by_key = {(t.host.lower(), t.port, t.server_name): t for t in existing}

    created = reenabled = 0
    for (host, port, sni), (rec, zone) in candidates.items():
        et = existing_by_key.get((host, port, sni))
        if et is not None:
            # Already tracked. Re-enable + refresh provenance if the record
            # came back (a re-added record is a NEW row with a fresh uuid).
            # Discovered targets are a projection of DNS state — to stop
            # probing, opt the record/zone OUT (auto_tls_probe=False); a
            # manually-disabled discovered row is re-enabled here by design.
            touched = False
            if not et.enabled:
                et.enabled = True
                touched = True
            if et.dns_record_id != rec.id:
                et.dns_record_id = rec.id
                touched = True
            if et.dns_zone_id is None:
                et.dns_zone_id = zone.id
            reenabled += 1 if touched else 0
            continue
        # Don't collide with a manually-added target on the same tuple.
        clash = await db.scalar(
            select(TLSCertTarget).where(
                func.lower(TLSCertTarget.host) == host,
                TLSCertTarget.port == port,
                TLSCertTarget.server_name.is_(None),
            )
        )
        if clash is not None:
            continue
        db.add(
            TLSCertTarget(
                host=host,
                port=port,
                server_name=None,
                display_name=host,
                source=SOURCE_DISCOVERED,
                dns_record_id=rec.id,
                dns_zone_id=zone.id,
                domain_id=zone.domain_id,
                enabled=True,
                next_check_at=None,  # picked up on the next probe sweep
            )
        )
        created += 1

    # Disable discovered targets no longer backed by an opted-in record —
    # keyed on the connect tuple (not dns_record_id), so a hard-deleted
    # record (FK SET NULL'd to dns_record_id=None) is caught too.
    candidate_keys = set(candidates.keys())
    disabled = 0
    for t in existing:
        key = (t.host.lower(), t.port, t.server_name)
        if t.enabled and key not in candidate_keys:
            t.enabled = False
            disabled += 1

    relinked = await _relink_by_san(db)

    if created or disabled or relinked or reenabled:
        logger.info(
            "tls_cert_discovery",
            created=created,
            reenabled=reenabled,
            disabled=disabled,
            relinked=relinked,
        )
    return {
        "created": created,
        "reenabled": reenabled,
        "disabled": disabled,
        "relinked": relinked,
    }
