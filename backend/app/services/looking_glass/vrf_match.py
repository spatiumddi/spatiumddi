"""Cross-reference a learned route's ``ext_communities`` against VRF
route-target lists (issue #566 Phase 6).

``VRF.import_targets`` / ``VRF.export_targets`` (``app.models.vrf``) are
always the bare canonical ``"ASN:N"`` / ``"IP:N"`` form (``_RD_RT_RE`` in
``app.api.v1.vrfs.router``). What a route's ``ext_communities`` actually
contain on the wire is NOT verified live — the agent-side GoBGP RIB poll
(``agent/looking-glass/spatium_lg_agent/rib.py``) stringifies whatever
GoBGP's ``-j`` output gives back for the extended-community attribute
verbatim, and that module's own docstring flags it as "not exercised by a
live session" — so normalization here is defensive: strip a handful of
plausible vendor-style label prefixes down to the bare form before
comparing, rather than assuming the agent always emits the bare form.

This match takes **precedence** over the plain IPAM-effective-VRF match
(``app.services.looking_glass.ipam_link.resolve_route_links``) whenever a
Route Target hit is found — a VPNv4/VPNv6 route's RD/RT is the actual
protocol-level signal for VRF membership; the IPAM-effective match is only
a fallback for routes with no VPN attributes at all. Callers
(``routes_ingest.ingest_routes`` and the periodic
``app.tasks.looking_glass.reresolve_route_links`` sweep) are responsible
for applying that precedence — this module only computes the RT-based
half.

VRF counts are small (tens to low hundreds, never RIB-scale), so building
the whole reverse index once per ingest call is cheap — same justification
as ``derive_rpki_status_batch``'s one-batched-ROA-load shape
(``app.services.bgp.hijack_monitor``).
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.vrf import VRF

_RT_PREFIX_RE = re.compile(r"^(rt|target|route-target)\s*[:=]\s*", re.IGNORECASE)


def normalize_rt(raw: str) -> str:
    """Strip a vendor-style label prefix down to the bare 'ASN:N'/'IP:N' value."""
    return _RT_PREFIX_RE.sub("", raw.strip())


async def build_vrf_rt_index(db: AsyncSession) -> dict[str, list[uuid.UUID]]:
    """``RT value -> [vrf_id, ...]``, over the union of every VRF's
    ``import_targets`` + ``export_targets``.

    Uncached, unlike ``ipam_link.get_resolution_cache`` — VRF rows are cheap
    to scan and callers (one ingest call, one re-resolve sweep run) already
    control their own cadence, so a second TTL cache here would just add
    staleness without saving anything meaningful.
    """
    index: dict[str, list[uuid.UUID]] = defaultdict(list)
    rows = (await db.execute(select(VRF.id, VRF.import_targets, VRF.export_targets))).all()
    for vrf_id, imp, exp in rows:
        for rt in {*(imp or []), *(exp or [])}:
            index[rt].append(vrf_id)
    return dict(index)


def match_vrf_for_route(
    ext_communities: list[str], index: dict[str, list[uuid.UUID]]
) -> uuid.UUID | None:
    """First matching VRF id for a route's ``ext_communities``, or ``None``.

    A route whose normalized RT hits more than one VRF (two VRFs sharing an
    RT is a legitimate hub-and-spoke pattern) picks deterministically —
    lowest-UUID VRF wins, so the result never depends on dict/scan order
    (mirrors the tie-break convention in
    ``app.services.looking_glass.reachability`` / the pre-existing
    ``find_bgp_route_for_ip`` LPM tie-break).
    """
    candidates: set[uuid.UUID] = set()
    for raw in ext_communities:
        candidates.update(index.get(normalize_rt(str(raw)), ()))
    if not candidates:
        return None
    return min(candidates, key=str)


__all__ = ["build_vrf_rt_index", "match_vrf_for_route", "normalize_rt"]
