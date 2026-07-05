"""Reconcile a Looking Glass collector's pushed Adj-RIB-In into the DB.

Called by ``POST /looking-glass/agents/routes``. Cloned from
``app.services.dhcp.pull_leases`` — the RIB is a set-reconcile just like
the DHCP lease table, with the same absence-marker + zero-wire floor
guard, differing only in the withdrawal semantics:

**Semantics — set-reconcile (upsert + absence-withdraw):**

 * The route identity key is ``(prefix, next_hop)`` — BGP allows multiple
   paths per prefix (unlike DHCP's ``(server_id, ip)``), so a prefix with
   two next-hops is two rows.
 * Upsert one ``BGPLGRoute`` per wire path: new keys insert with
   ``first_seen_at=now``; existing keys refresh the mutable attributes
   (``as_path`` / ``local_pref`` / ``med`` / communities / ``is_best`` /
   ``rpki_status``), bump ``last_seen_at``, and **clear ``withdrawn_at``**
   (a re-announce of a previously-withdrawn path).
 * ``rpki_status`` is computed at ingest via ``derive_rpki_status`` from
   the #527 hijack monitor, deduped per distinct ``(prefix, origin_asn)``
   so a full snapshot makes one ROA lookup per unique announcement, not
   one per path.
 * ``matched_{block,subnet,space,asn,vrf}_id`` are resolved at ingest via
   ``app.services.looking_glass.ipam_link.resolve_route_links`` against a
   TTL-cached preload of the IPAM tree + ASN catalog (issue #566 Phase 3).
   This is the write path the columns were shipped for in Phase 1 — see
   that module for the containment/inheritance semantics. A periodic
   re-resolve sweep (``app.tasks.looking_glass.reresolve_route_links``)
   re-runs the same matcher over every active route so an IPAM edit made
   between RIB pushes still converges within a few minutes.
 * **Absence-withdraw (full snapshot only):** every tracked row for this
   peer with ``withdrawn_at IS NULL`` that is NOT in the wire set has left
   the peer's feed — set ``withdrawn_at=now()``, bump ``flap_count``, and
   stamp ``last_flap_at=now()`` (issue #566 Phase 5 — the recency
   dimension the ``bgp_lg_route_flap`` alert needs so a route that flapped
   a lot long ago but has been stable since doesn't page forever).
   Withdrawn rows are **never hard-deleted** — ``withdrawn_at`` is the
   designed history marker (the UI / alerts read it).

**Zero-wire floor guard (correctness-critical, cloned from #482):** an
EMPTY snapshot from a peer whose ``prefixes_received > 0`` is
indistinguishable from a collector hiccup (gRPC timeout, session flap
yielding a momentary empty ``ListPath``). Rather than let one empty poll
withdraw the entire RIB, skip the absence-withdraw for that snapshot and
log a warning; a genuine full withdrawal arrives as a real wire snapshot
whose keys the reconcile still processes, not as silence.

Idempotent (non-negotiable #9): a second identical snapshot is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import ip_address, ip_network
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bgp_looking_glass import BGPLGPeer, BGPLGRoute
from app.services.bgp.hijack_monitor import derive_rpki_status_batch
from app.services.looking_glass.ipam_link import (
    get_resolution_cache,
    resolve_route_links,
)

logger = structlog.get_logger(__name__)


@dataclass
class RoutesIngestResult:
    wire_routes: int = 0  # count of valid paths in the pushed snapshot
    imported: int = 0  # new BGPLGRoute rows inserted
    refreshed: int = 0  # existing rows updated in place (incl. re-announce)
    withdrawn: int = 0  # rows marked withdrawn_at by absence-reconcile
    errors: list[str] = field(default_factory=list)


async def ingest_routes(
    db: AsyncSession,
    peer: BGPLGPeer,
    wire: list[dict[str, Any]],
    *,
    snapshot: bool = True,
    apply: bool = True,
) -> RoutesIngestResult:
    """Reconcile ``wire`` (the collector's pushed paths) into ``BGPLGRoute``.

    ``snapshot=True`` treats ``wire`` as the peer's COMPLETE current RIB and
    runs the absence-withdraw sweep; ``snapshot=False`` is a delta batch
    (upsert-only, no withdraws). ``apply=False`` computes the counters
    without writing (dry-run preview).
    """
    result = RoutesIngestResult()
    now = datetime.now(UTC)

    # Validate + dedup wire paths up front:
    #  * ``prefix`` / ``next_hop`` are strict Postgres CIDR / INET columns, so
    #    a malformed value (host bits set, garbage, wrong family) would raise
    #    DataError at flush and 500 the WHOLE push. Parse-and-skip instead so
    #    one bad path can't discard a good snapshot. ip_network(strict=True)
    #    matches Postgres CIDR strictness (rejects non-network host bits).
    #  * Dedup on the ``(prefix, next_hop)`` identity: two wire entries with
    #    the same key would both take the insert branch (``by_key`` is built
    #    once from the DB and not updated mid-loop) and collide on
    #    ``uq_bgp_lg_route``. Normalising through ipaddress also canonicalises
    #    the strings so they match the DB round-trip. Last write wins.
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for r in wire:
        prefix_raw, next_hop_raw = r.get("prefix"), r.get("next_hop")
        if not prefix_raw or not next_hop_raw:
            continue
        try:
            prefix = str(ip_network(str(prefix_raw), strict=True))
            next_hop = str(ip_address(str(next_hop_raw)))
        except ValueError:
            result.errors.append(f"skipped malformed route {prefix_raw!r} via {next_hop_raw!r}")
            continue
        deduped[(prefix, next_hop)] = {**r, "prefix": prefix, "next_hop": next_hop}
    valid = list(deduped.values())
    result.wire_routes = len(valid)

    # Bulk-preload every tracked path for this peer once (avoid the per-path
    # N+1). Keyed on the route identity — stringified so wire strings match
    # asyncpg's IPv4Network / IPv4Address round-trip.
    existing_rows = list(
        (await db.execute(select(BGPLGRoute).where(BGPLGRoute.peer_id == peer.id))).scalars().all()
    )
    by_key: dict[tuple[str, str], BGPLGRoute] = {
        (str(r.prefix), str(r.next_hop)): r for r in existing_rows
    }

    # ── Zero-wire floor guard (#482) ────────────────────────────────────
    # An empty snapshot from an established peer that has learned prefixes
    # is a collector hiccup, not a genuine full withdrawal. Skip the sweep
    # and let the next real snapshot reconcile.
    if snapshot and not valid:
        if peer.prefixes_received and peer.prefixes_received > 0:
            msg = (
                f"empty RIB snapshot from peer with prefixes_received="
                f"{peer.prefixes_received} — skipping absence-withdraw (#482)"
            )
            result.errors.append(msg)
            logger.warning(
                "lg_routes_empty_wire_skip_withdraw",
                peer=str(peer.id),
                prefixes_received=peer.prefixes_received,
            )
            return result
        # prefixes_received == 0: nothing to protect — fall through so any
        # lingering active rows get withdrawn below.

    # ── RPKI status per distinct (prefix, origin) — one batched ROA load ──
    # (not one DB round-trip per prefix: a full snapshot has thousands of
    # distinct announcements, and per-call querying would serialise thousands
    # of covering-ROA lookups inside this one ingest transaction).
    rpki_pairs = {
        (str(r["prefix"]), int(r["origin_asn"])) for r in valid if r.get("origin_asn") is not None
    }
    rpki_cache = await derive_rpki_status_batch(db, rpki_pairs)

    # ── IPAM / ASN / VRF linkage — one preload for the whole snapshot ────
    # (TTL-cached — see ipam_link.py's module docstring for why a fresh
    # subnet+block+space+ASN scan per ingest call would scale with peer
    # count for no benefit.)
    link_cache = await get_resolution_cache(db)

    # ── Upsert every wire path ───────────────────────────────────────────
    wire_keys: set[tuple[str, str]] = set()
    for r in valid:
        prefix = str(r["prefix"])
        next_hop = str(r["next_hop"])
        wire_keys.add((prefix, next_hop))
        origin = r.get("origin_asn")
        rpki = rpki_cache.get((prefix, int(origin)), "unknown") if origin is not None else "unknown"
        links = resolve_route_links(link_cache, prefix, origin)

        row = by_key.get((prefix, next_hop))
        if row is None:
            if apply:
                db.add(
                    BGPLGRoute(
                        peer_id=peer.id,
                        prefix=prefix,
                        origin_asn=origin,
                        as_path=list(r.get("as_path") or []),
                        next_hop=next_hop,
                        local_pref=r.get("local_pref"),
                        med=r.get("med"),
                        communities=list(r.get("communities") or []),
                        large_communities=list(r.get("large_communities") or []),
                        ext_communities=list(r.get("ext_communities") or []),
                        rpki_status=rpki,
                        is_best=bool(r.get("is_best", False)),
                        matched_block_id=links.block_id,
                        matched_subnet_id=links.subnet_id,
                        matched_space_id=links.space_id,
                        matched_asn_id=links.asn_id,
                        matched_vrf_id=links.vrf_id,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            result.imported += 1
        else:
            if apply:
                row.origin_asn = origin
                row.as_path = list(r.get("as_path") or [])
                row.local_pref = r.get("local_pref")
                row.med = r.get("med")
                row.communities = list(r.get("communities") or [])
                row.large_communities = list(r.get("large_communities") or [])
                row.ext_communities = list(r.get("ext_communities") or [])
                row.rpki_status = rpki
                row.is_best = bool(r.get("is_best", False))
                row.matched_block_id = links.block_id
                row.matched_subnet_id = links.subnet_id
                row.matched_space_id = links.space_id
                row.matched_asn_id = links.asn_id
                row.matched_vrf_id = links.vrf_id
                row.last_seen_at = now
                # Re-announce of a previously-withdrawn path — clear the marker.
                row.withdrawn_at = None
            result.refreshed += 1

    # ── Absence-withdraw (full snapshot only) ────────────────────────────
    if snapshot:
        for key, row in by_key.items():
            if row.withdrawn_at is None and key not in wire_keys:
                if apply:
                    row.withdrawn_at = now
                    row.flap_count = (row.flap_count or 0) + 1
                    row.last_flap_at = now
                result.withdrawn += 1

    if apply:
        await db.flush()

    return result


__all__ = ["RoutesIngestResult", "ingest_routes"]
