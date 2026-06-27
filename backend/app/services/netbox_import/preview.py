"""Side-effect-free preview orchestrator for the NetBox importer (issue #36 §4.2).

:func:`preview_netbox_import` is the read-only half of the two-phase
preview → (stateless round-trip) → commit flow. It opens one
:class:`app.services.netbox_import.client.NetBoxClient`, pulls every
in-scope endpoint via ``fetch.py`` **in dependency order**, maps each raw
NetBox object onto the canonical IR via ``mapping.py`` — with **zero DB
writes** — and then calls :func:`app.services.netbox_import.commit.detect_conflicts`
to flag every imported entity whose key already exists in the target DB.

The full IR (every ``Imported*`` dataclass + the conflict list +
warnings) is returned to the operator and round-tripped back verbatim as
``CommitIn.plan`` at commit time, so the server stores nothing between
the two calls (the same stateless contract the DNS / DHCP importers use).

The cross-reference resolution that can only happen with the whole pull
in hand lives here, not in the pure mappers:

* **Space assignment** — ``space_strategy="per_vrf"`` synthesises one
  :class:`ImportedSpace` per VRF (linked via ``vrf_name``) plus one
  ``"Global"`` space for the vrf=null table + aggregates; each prefix /
  address resolves its ``space_name`` from its VRF. ``space_strategy=
  "single"`` collapses everything into the operator-chosen
  ``target_space_id`` space and the IR carries no ``ImportedSpace`` rows
  (the committer reuses the existing target space).
* **Block-vs-subnet decision** — :func:`classify_prefixes` runs over the
  whole prefix pull (parent-first) so an enclosing prefix is forced to a
  block before the prefix it encloses is classified.
* **VLAN vid resolution** — a prefix's ``vlan`` brief carries only the
  NetBox VLAN id, so the subnet's ``vlan_vid`` is resolved against the
  imported VLAN list (``netbox vlan id → vid``).

Anything NetBox carries that the IR can't model (ip-ranges,
``assigned_object`` enrichment limits, region/site-group double-parent,
vid/name clashes within the synthetic router) lands in
``ImportPreview.warnings`` so the preview UI surfaces it rather than
dropping it silently.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from .canonical import (
    ImportedAddress,
    ImportedBlock,
    ImportedCustomer,
    ImportedSite,
    ImportedSpace,
    ImportedSubnet,
    ImportedVLAN,
    ImportedVRF,
    ImportPreview,
)
from .client import PULL_CEILING, NetBoxClient
from .fetch import (
    fetch_aggregates,
    fetch_ip_addresses,
    fetch_ip_ranges,
    fetch_prefixes,
    fetch_regions,
    fetch_sites,
    fetch_tenants,
    fetch_vlans,
    fetch_vrfs,
)
from .mapping import (
    classify_prefixes,
    map_address,
    map_aggregate,
    map_customer,
    map_prefix_block,
    map_prefix_subnet,
    map_region_as_site,
    map_site,
    map_vlan,
    map_vrf,
)

logger = structlog.get_logger(__name__)

# The synthesized Global space name (vrf=null table + aggregates). The
# committer reuses an existing default space if one is present, else
# creates this one.
GLOBAL_SPACE_NAME = "Global"


def _space_name_for_vrf(vrf_id: Any, vrf_id_to_name: dict[Any, str]) -> str:
    """Resolve the IPSpace name for a prefix/address by its VRF id.

    ``vrf_id=None`` (the NetBox global table) → the Global space.
    """
    if vrf_id is None:
        return GLOBAL_SPACE_NAME
    return vrf_id_to_name.get(vrf_id, GLOBAL_SPACE_NAME)


async def preview_netbox_import(
    db: Any,  # AsyncSession — typed Any to keep this importable without a live session
    *,
    base_url: str,
    token: str,
    verify_tls: bool = True,
    space_strategy: str = "per_vrf",
    target_space_id: uuid.UUID | None = None,
    filters: dict[str, Any] | None = None,
) -> ImportPreview:
    """Pull a NetBox install + build the canonical IR (no DB writes).

    Opens a :class:`NetBoxClient`, detects the version (drives the
    site/scope parse branch in ``fetch.py``), pulls every in-scope
    endpoint in dependency order, maps each object onto the IR, resolves
    cross-references (space assignment per ``space_strategy``,
    block-vs-subnet classification, VLAN vid resolution), and flags
    conflicts against the live DB via :func:`detect_conflicts`.

    ``space_strategy``:
        * ``"per_vrf"`` (default) — one IPSpace per VRF + a Global space;
          each prefix / address lands in its VRF's space. Overlapping
          CIDRs in different VRFs stay isolated.
        * ``"single"`` — everything into the operator-chosen
          ``target_space_id`` (no ``ImportedSpace`` rows synthesised).

    ``filters`` forwards the operator's scope slice (``vrf_id`` /
    ``tenant_id`` / ``status`` / ``family`` / ``within_include``) to the
    prefix / address / vrf / tenant pulls.

    Raises :class:`NetBoxClientError` on an unreachable / unauthorised
    NetBox. The pull ceiling (:data:`PULL_CEILING`) caps total
    prefixes + addresses — over the ceiling the preview raises and asks
    the operator to narrow scope with filters.
    """
    # local import so this module imports cleanly without the commit
    # half loaded (mirrors the dns_import wrapper boundary).
    from .commit import detect_conflicts

    filters = filters or {}
    vrf_filter = filters.get("vrf_id")
    tenant_filter = filters.get("tenant_id")
    status_filter = filters.get("status")
    family_filter = filters.get("family")
    within_include = filters.get("within_include")

    warnings: list[str] = []

    async with NetBoxClient(base_url=base_url, token=token, verify_tls=verify_tls) as nb:
        version = await nb.detect_version()

        # ── Dependency-ordered pulls. tenants → regions → sites → vrfs →
        # aggregates → prefixes → ip-ranges → ip-addresses → vlans. ──
        raw_tenants = await fetch_tenants(nb, tenant_id=tenant_filter)
        raw_regions = await fetch_regions(nb)
        raw_sites = await fetch_sites(nb)
        raw_vrfs = await fetch_vrfs(nb, tenant_id=tenant_filter)
        raw_aggregates = await fetch_aggregates(nb, tenant_id=tenant_filter)
        raw_prefixes = await fetch_prefixes(
            nb,
            vrf_id=vrf_filter,
            tenant_id=tenant_filter,
            status=status_filter,
            family=family_filter,
            within_include=within_include,
        )
        raw_ip_ranges = await fetch_ip_ranges(nb, vrf_id=vrf_filter)
        raw_addresses = await fetch_ip_addresses(
            nb,
            vrf_id=vrf_filter,
            tenant_id=tenant_filter,
            status=status_filter,
            family=family_filter,
        )
        raw_vlans = await fetch_vlans(nb)

    # ── Pull-ceiling guard (§3.8) — committer has no per-row cap, so the
    # pull side is where we protect the worker from an OOM. ──
    pulled_rows = len(raw_prefixes) + len(raw_addresses)
    if pulled_rows > PULL_CEILING:
        from .client import NetBoxClientError

        raise NetBoxClientError(
            f"NetBox pull would import {pulled_rows} prefix+address rows, over the "
            f"{PULL_CEILING} ceiling. Narrow the import with a vrf / tenant / status / "
            "family / within_include filter and re-run."
        )

    # ── Customers ───────────────────────────────────────────────────── #
    customers: list[ImportedCustomer] = []
    for t in raw_tenants:
        c = map_customer(t)
        if c.name:
            customers.append(c)

    # ── Sites — regions first (parent-first tree), then sites ──────────
    sites: list[ImportedSite] = []
    site_codes_seen: set[str] = set()
    # Regions become parent Site nodes so a site's parent_code (region
    # slug) resolves to a real parent. Sort by _depth so parents come
    # before children.
    for region in sorted(raw_regions, key=lambda r: r.get("_depth", 0)):
        s = map_region_as_site(region)
        if s.name:
            sites.append(s)
            if s.code:
                site_codes_seen.add(s.code)
    for site in raw_sites:
        s = map_site(site)
        if not s.name:
            continue
        # A site whose region parent wasn't pulled (filtered out) loses
        # its parent linkage — warn and drop the dangling parent_code so
        # the committer doesn't fail to resolve it.
        if s.parent_code and s.parent_code not in site_codes_seen:
            warnings.append(
                f"Site {s.name!r} references region {s.parent_code!r} that wasn't imported; "
                "importing it at the top level."
            )
            s.parent_code = None
        sites.append(s)
        if s.code:
            site_codes_seen.add(s.code)

    # ── VRFs ────────────────────────────────────────────────────────── #
    vrfs: list[ImportedVRF] = []
    vrf_id_to_name: dict[Any, str] = {}
    for v in raw_vrfs:
        iv = map_vrf(v)
        if iv.name:
            vrfs.append(iv)
            if iv.netbox_id is not None:
                vrf_id_to_name[iv.netbox_id] = iv.name

    # ── Space strategy ─────────────────────────────────────────────────
    spaces: list[ImportedSpace] = []
    use_per_vrf = space_strategy == "per_vrf"
    if use_per_vrf:
        # One IPSpace per VRF (linked via vrf_name) + a Global space.
        for iv in vrfs:
            spaces.append(
                ImportedSpace(
                    name=iv.name,
                    vrf_name=iv.name,
                    is_default=False,
                    customer_name=iv.customer_name,
                    description=iv.description,
                )
            )
        spaces.append(
            ImportedSpace(
                name=GLOBAL_SPACE_NAME,
                vrf_name=None,
                is_default=True,
                customer_name=None,
                description="NetBox global routing table + aggregates",
            )
        )
    else:
        # single-space strategy: the committer reuses target_space_id; no
        # synthetic ImportedSpace rows. We still need a placeholder name
        # so prefix/address space resolution has a stable key — the
        # committer maps the single sentinel onto target_space_id.
        if target_space_id is None:
            warnings.append(
                "space_strategy='single' requires target_space_id; the commit will reject this plan."
            )

    def _space_for(vrf_brief: Any) -> str | None:
        """Resolve a prefix/address's space name from its VRF brief."""
        if not use_per_vrf:
            # The committer resolves the single target space directly;
            # carry None so nothing in the IR pins a per-VRF space.
            return None
        vrf_id = vrf_brief.get("id") if isinstance(vrf_brief, dict) else None
        return _space_name_for_vrf(vrf_id, vrf_id_to_name)

    # ── VLANs (synthetic router) — dedupe on vid + name ────────────────
    vlans: list[ImportedVLAN] = []
    vlan_netbox_id_to_vid: dict[Any, int] = {}
    seen_vids: set[int] = set()
    seen_vlan_names: set[str] = set()
    for raw_vlan in raw_vlans:
        iv_vlan = map_vlan(raw_vlan)
        if iv_vlan is None:
            warnings.append(f"NetBox VLAN id={raw_vlan.get('id')} has no vid/name; skipped.")
            continue
        if iv_vlan.vid in seen_vids:
            warnings.append(
                f"VLAN vid {iv_vlan.vid} ({iv_vlan.name!r}) clashes with an earlier VLAN under "
                "the synthetic router; skipped (uniqueness is (router, vid))."
            )
            continue
        if iv_vlan.name in seen_vlan_names:
            warnings.append(
                f"VLAN name {iv_vlan.name!r} (vid {iv_vlan.vid}) clashes with an earlier VLAN "
                "under the synthetic router; skipped (uniqueness is (router, name))."
            )
            continue
        seen_vids.add(iv_vlan.vid)
        seen_vlan_names.add(iv_vlan.name)
        vlans.append(iv_vlan)
        if iv_vlan.netbox_id is not None:
            vlan_netbox_id_to_vid[iv_vlan.netbox_id] = iv_vlan.vid

    # ── Aggregates → top-level blocks (always Global space) ────────────
    blocks: list[ImportedBlock] = []
    for agg in raw_aggregates:
        ib = map_aggregate(agg, space_name=GLOBAL_SPACE_NAME if use_per_vrf else "")
        if ib is None:
            warnings.append(
                f"NetBox aggregate id={agg.get('id')} prefix={agg.get('prefix')!r} "
                "is unparseable; skipped."
            )
            continue
        # single-space strategy carries no per-space name (committer uses
        # target_space_id).
        if not use_per_vrf:
            ib.space_name = None
        blocks.append(ib)

    # ── Prefixes → blocks / subnets (classify whole pull parent-first) ──
    decisions = classify_prefixes(raw_prefixes)
    subnets: list[ImportedSubnet] = []
    for prefix in raw_prefixes:
        pid = prefix.get("id")
        kind = decisions.get(pid, "subnet") if pid is not None else "subnet"
        space_name = _space_for(prefix.get("vrf"))
        if kind == "block":
            ib = map_prefix_block(prefix, space_name=space_name)
            if ib is None:
                warnings.append(
                    f"NetBox prefix id={pid} prefix={prefix.get('prefix')!r} is unparseable; skipped."
                )
                continue
            blocks.append(ib)
        else:
            isub = map_prefix_subnet(prefix, space_name=space_name)
            if isub is None:
                warnings.append(
                    f"NetBox prefix id={pid} prefix={prefix.get('prefix')!r} is unparseable; skipped."
                )
                continue
            # Resolve the prefix's VLAN id (carried in custom_fields by
            # the mapper) onto the imported VLAN's vid.
            nb_vlan_id = isub.custom_fields.pop("netbox_vlan_id", None)
            if nb_vlan_id is not None:
                vid = vlan_netbox_id_to_vid.get(nb_vlan_id)
                if vid is not None:
                    isub.vlan_vid = vid
                else:
                    warnings.append(
                        f"Subnet {isub.network} references NetBox VLAN id={nb_vlan_id} that "
                        "wasn't imported; subnet imported without a VLAN link."
                    )
            # A multicast subnet rejects address rows on the create path;
            # warn so the operator knows IPs in it won't be stamped.
            if isub.kind == "multicast":
                warnings.append(
                    f"Subnet {isub.network} is multicast; addresses inside it will be skipped "
                    "(create a multicast group post-import instead)."
                )
            subnets.append(isub)

    # ── ip-ranges → warning only (no DHCP pool creation, §1) ───────────
    for rng in raw_ip_ranges:
        warnings.append(
            f"NetBox ip-range {rng.get('start_address')}–{rng.get('end_address')} is not imported "
            "(DHCP pool creation is out of scope); record it manually if needed."
        )

    # ── IP addresses ───────────────────────────────────────────────────
    addresses: list[ImportedAddress] = []
    for ip in raw_addresses:
        ia = map_address(ip)
        if ia is None:
            warnings.append(
                f"NetBox IP id={ip.get('id')} address={ip.get('address')!r} is unparseable; skipped."
            )
            continue
        # Address space resolution mirrors the prefix path (per-VRF or
        # single). The committer ultimately binds the address to its
        # most-specific imported subnet regardless.
        ia.space_name = _space_for(ip.get("vrf"))
        addresses.append(ia)

    preview = ImportPreview(
        source="netbox",
        customers=customers,
        sites=sites,
        vrfs=vrfs,
        spaces=spaces,
        vlans=vlans,
        blocks=blocks,
        subnets=subnets,
        addresses=addresses,
        conflicts=[],
        warnings=warnings,
    )

    # ── Conflict detection against the live DB (advisory; the committer
    # re-detects fresh at commit time). ──
    preview.conflicts = await detect_conflicts(
        db,
        preview=preview,
        space_strategy=space_strategy,
        target_space_id=target_space_id,
    )

    logger.info(
        "netbox_import.preview",
        endpoint=base_url,
        netbox_version=version,
        space_strategy=space_strategy,
        customers=len(customers),
        sites=len(sites),
        vrfs=len(vrfs),
        spaces=len(spaces),
        vlans=len(vlans),
        blocks=len(blocks),
        subnets=len(subnets),
        addresses=len(addresses),
        conflicts=len(preview.conflicts),
        warnings=len(warnings),
    )

    return preview
