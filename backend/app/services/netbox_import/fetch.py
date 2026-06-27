"""Endpoint pulls → normalized raw dicts (issue #36 §3, §4.2).

Each ``fetch_*`` coroutine walks one paginated NetBox endpoint via
:class:`app.services.netbox_import.client.NetBoxClient` and returns a list
of *normalized* raw dicts: FK brief objects flattened to ``{id, name,
slug}`` triples, choice fields reduced to their ``.value``, and the
version-dependent ``site`` vs ``scope`` shape collapsed to one neutral
``site`` triple. ``mapping.py`` then maps these neutral dicts onto the
canonical IR — so the NetBox wire-shape branching lives **here**, not in
the mappers.

Read asymmetry handled here (issue body §2):

* FK fields serialize as brief ``{id, url, display, name, slug, …}``
  objects on read — we keep ``{id, name, slug}``.
* Choice fields (``status``, ``family``, ip ``role``) serialize as
  ``{value, label}`` — we keep ``.value``.
* **Version branch**: on prefixes / VLANs the ``site`` FK (≤4.1) became
  ``scope_type`` + ``scope_id`` + ``scope`` (4.2+). ``_normalize_scope``
  feature-detects ``"scope_type" in obj`` and produces one neutral
  ``site`` triple either way.
* Everything optional is read with ``.get()`` so a missing field on an
  older / newer NetBox never KeyErrors.

Scope filters (``vrf_id`` / ``tenant_id`` / ``status`` / ``family`` /
``within_include``) are forwarded verbatim as query params so the
operator can import a slice (§3.6).
"""

from __future__ import annotations

from typing import Any

import structlog

from .client import NetBoxClient

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Normalization helpers — flatten NetBox read shapes to neutral dicts.
# --------------------------------------------------------------------------- #


def _brief(obj: Any) -> dict[str, Any] | None:
    """Flatten a NetBox FK brief object to ``{id, name, slug}`` or None.

    A NetBox FK serializes on read as ``{id, url, display, name, slug,
    …}`` (the exact keys vary per object — e.g. RIR has no slug-less
    variant, tenant has ``name`` + ``slug``). We keep only what the
    mappers need and tolerate ``None`` (an unset FK).
    """
    if not isinstance(obj, dict):
        return None
    return {
        "id": obj.get("id"),
        "name": obj.get("name"),
        "slug": obj.get("slug"),
    }


def _choice(obj: Any) -> str | None:
    """Reduce a NetBox choice ``{value, label}`` field to its ``.value``.

    Tolerates a bare string (some older serializers) and ``None``.
    """
    if isinstance(obj, dict):
        value = obj.get("value")
        return str(value) if value is not None else None
    if isinstance(obj, str):
        return obj
    return None


def _rt_names(targets: Any) -> list[str]:
    """Pull ``[rt.name …]`` out of a VRF import/export-targets list."""
    out: list[str] = []
    if isinstance(targets, list):
        for rt in targets:
            if isinstance(rt, dict) and rt.get("name"):
                out.append(str(rt["name"]))
    return out


def _normalize_scope(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Collapse the version-dependent site/scope shape to one ``site`` triple.

    NetBox ≤4.1 carries a ``site`` FK on prefixes / VLANs. NetBox 4.2+
    replaced it with a generic scope (``scope_type`` = ``"dcim.site"`` +
    ``scope_id`` + an inlined ``scope`` brief). We feature-detect
    ``"scope_type" in obj`` and return the site brief either way (or
    ``None`` when the scope is non-site / unset).
    """
    if "scope_type" in obj:
        if obj.get("scope_type") == "dcim.site":
            return _brief(obj.get("scope"))
        # Non-site scope (region / site-group / location) — no direct
        # Site link for a prefix/VLAN in the importer's single-parent model.
        return None
    return _brief(obj.get("site"))


# --------------------------------------------------------------------------- #
# Per-endpoint pulls. Each returns a list of normalized raw dicts.
# --------------------------------------------------------------------------- #


async def fetch_rirs(nb: NetBoxClient) -> list[dict[str, Any]]:
    """``/api/ipam/rirs/`` — RIR names for aggregate provenance."""
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/ipam/rirs/"):
        out.append({"id": r.get("id"), "name": r.get("name"), "slug": r.get("slug")})
    logger.info("netbox_import.fetch.rirs", count=len(out))
    return out


async def fetch_roles(nb: NetBoxClient) -> list[dict[str, Any]]:
    """``/api/ipam/roles/`` — prefix/VLAN role names."""
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/ipam/roles/"):
        out.append({"id": r.get("id"), "name": r.get("name"), "slug": r.get("slug")})
    logger.info("netbox_import.fetch.roles", count=len(out))
    return out


async def fetch_tenant_groups(nb: NetBoxClient) -> list[dict[str, Any]]:
    """``/api/tenancy/tenant-groups/`` — group label for Customer tags."""
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/tenancy/tenant-groups/"):
        out.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "slug": r.get("slug"),
                "parent": _brief(r.get("parent")),
            }
        )
    logger.info("netbox_import.fetch.tenant_groups", count=len(out))
    return out


async def fetch_tenants(nb: NetBoxClient, *, tenant_id: int | None = None) -> list[dict[str, Any]]:
    """``/api/tenancy/tenants/`` → ``Customer`` source rows."""
    params: dict[str, Any] = {}
    if tenant_id is not None:
        params["id"] = tenant_id
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/tenancy/tenants/", params or None):
        out.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "slug": r.get("slug"),
                "description": r.get("description") or "",
                "group": _brief(r.get("group")),
                "custom_fields": r.get("custom_fields") or {},
                "tags": r.get("tags") or [],
            }
        )
    logger.info("netbox_import.fetch.tenants", count=len(out))
    return out


async def fetch_regions(nb: NetBoxClient) -> list[dict[str, Any]]:
    """``/api/dcim/regions/`` — the primary Site parent-tree axis."""
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/dcim/regions/"):
        out.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "slug": r.get("slug"),
                "parent": _brief(r.get("parent")),
                "description": r.get("description") or "",
                "_depth": r.get("_depth", 0),
            }
        )
    logger.info("netbox_import.fetch.regions", count=len(out))
    return out


async def fetch_site_groups(nb: NetBoxClient) -> list[dict[str, Any]]:
    """``/api/dcim/site-groups/`` — folded into a Site ``tags`` label."""
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/dcim/site-groups/"):
        out.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "slug": r.get("slug"),
                "parent": _brief(r.get("parent")),
                "_depth": r.get("_depth", 0),
            }
        )
    logger.info("netbox_import.fetch.site_groups", count=len(out))
    return out


async def fetch_sites(nb: NetBoxClient) -> list[dict[str, Any]]:
    """``/api/dcim/sites/`` → ``Site`` source rows."""
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/dcim/sites/"):
        out.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "slug": r.get("slug"),
                "status": _choice(r.get("status")),
                "region": _brief(r.get("region")),
                "group": _brief(r.get("group")),
                "tenant": _brief(r.get("tenant")),
                "physical_address": r.get("physical_address") or "",
                "description": r.get("description") or "",
                "custom_fields": r.get("custom_fields") or {},
                "tags": r.get("tags") or [],
            }
        )
    logger.info("netbox_import.fetch.sites", count=len(out))
    return out


async def fetch_vrfs(nb: NetBoxClient, *, tenant_id: int | None = None) -> list[dict[str, Any]]:
    """``/api/ipam/vrfs/`` → ``VRF`` source rows."""
    params: dict[str, Any] = {}
    if tenant_id is not None:
        params["tenant_id"] = tenant_id
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/ipam/vrfs/", params or None):
        out.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "rd": r.get("rd"),
                "description": r.get("description") or "",
                "tenant": _brief(r.get("tenant")),
                "enforce_unique": r.get("enforce_unique"),
                "import_targets": _rt_names(r.get("import_targets")),
                "export_targets": _rt_names(r.get("export_targets")),
                "custom_fields": r.get("custom_fields") or {},
                "tags": r.get("tags") or [],
            }
        )
    logger.info("netbox_import.fetch.vrfs", count=len(out))
    return out


async def fetch_route_targets(nb: NetBoxClient) -> list[dict[str, Any]]:
    """``/api/ipam/route-targets/`` — RT catalog (names already inlined on VRFs)."""
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/ipam/route-targets/"):
        out.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "tenant": _brief(r.get("tenant")),
                "description": r.get("description") or "",
            }
        )
    logger.info("netbox_import.fetch.route_targets", count=len(out))
    return out


async def fetch_aggregates(
    nb: NetBoxClient, *, tenant_id: int | None = None
) -> list[dict[str, Any]]:
    """``/api/ipam/aggregates/`` → top-level ``IPBlock`` source rows."""
    params: dict[str, Any] = {}
    if tenant_id is not None:
        params["tenant_id"] = tenant_id
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/ipam/aggregates/", params or None):
        out.append(
            {
                "id": r.get("id"),
                "prefix": r.get("prefix"),
                "rir": _brief(r.get("rir")),
                "tenant": _brief(r.get("tenant")),
                "description": r.get("description") or "",
                "custom_fields": r.get("custom_fields") or {},
                "tags": r.get("tags") or [],
            }
        )
    logger.info("netbox_import.fetch.aggregates", count=len(out))
    return out


async def fetch_prefixes(
    nb: NetBoxClient,
    *,
    vrf_id: int | None = None,
    tenant_id: int | None = None,
    status: str | None = None,
    family: int | None = None,
    within_include: str | None = None,
) -> list[dict[str, Any]]:
    """``/api/ipam/prefixes/`` → ``IPBlock`` / ``Subnet`` source rows.

    Forwards the operator's scope filters as query params so an import
    can be sliced to one VRF / tenant / status / address-family / CIDR
    window. The ``site`` vs ``scope`` version shape is collapsed via
    :func:`_normalize_scope`.
    """
    params: dict[str, Any] = {}
    if vrf_id is not None:
        params["vrf_id"] = vrf_id
    if tenant_id is not None:
        params["tenant_id"] = tenant_id
    if status is not None:
        params["status"] = status
    if family is not None:
        params["family"] = family
    if within_include is not None:
        params["within_include"] = within_include
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/ipam/prefixes/", params or None):
        out.append(
            {
                "id": r.get("id"),
                "prefix": r.get("prefix"),
                "status": _choice(r.get("status")),
                "vrf": _brief(r.get("vrf")),
                "site": _normalize_scope(r),
                "tenant": _brief(r.get("tenant")),
                "vlan": _brief(r.get("vlan")),
                "role": _brief(r.get("role")),
                "is_pool": r.get("is_pool"),
                "description": r.get("description") or "",
                "custom_fields": r.get("custom_fields") or {},
                "tags": r.get("tags") or [],
            }
        )
    logger.info("netbox_import.fetch.prefixes", count=len(out))
    return out


async def fetch_ip_ranges(nb: NetBoxClient, *, vrf_id: int | None = None) -> list[dict[str, Any]]:
    """``/api/ipam/ip-ranges/`` — metadata only (no DHCP pool creation, §1)."""
    params: dict[str, Any] = {}
    if vrf_id is not None:
        params["vrf_id"] = vrf_id
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/ipam/ip-ranges/", params or None):
        out.append(
            {
                "id": r.get("id"),
                "start_address": r.get("start_address"),
                "end_address": r.get("end_address"),
                "vrf": _brief(r.get("vrf")),
                "status": _choice(r.get("status")),
                "description": r.get("description") or "",
            }
        )
    logger.info("netbox_import.fetch.ip_ranges", count=len(out))
    return out


async def fetch_ip_addresses(
    nb: NetBoxClient,
    *,
    vrf_id: int | None = None,
    tenant_id: int | None = None,
    status: str | None = None,
    family: int | None = None,
) -> list[dict[str, Any]]:
    """``/api/ipam/ip-addresses/`` → ``IPAddress`` source rows.

    The generic ``assigned_object`` relation is read for *enrichment*
    only (device / interface name into description / managed_by) — never
    imported as its own row (§1 out-of-scope).
    """
    params: dict[str, Any] = {}
    if vrf_id is not None:
        params["vrf_id"] = vrf_id
    if tenant_id is not None:
        params["tenant_id"] = tenant_id
    if status is not None:
        params["status"] = status
    if family is not None:
        params["family"] = family
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/ipam/ip-addresses/", params or None):
        out.append(
            {
                "id": r.get("id"),
                "address": r.get("address"),
                "status": _choice(r.get("status")),
                "role": _choice(r.get("role")),
                "dns_name": r.get("dns_name") or "",
                "vrf": _brief(r.get("vrf")),
                "tenant": _brief(r.get("tenant")),
                "description": r.get("description") or "",
                "assigned_object": _assigned_object(r.get("assigned_object")),
                "custom_fields": r.get("custom_fields") or {},
                "tags": r.get("tags") or [],
            }
        )
    logger.info("netbox_import.fetch.ip_addresses", count=len(out))
    return out


def _assigned_object(obj: Any) -> dict[str, Any] | None:
    """Flatten an IP's generic ``assigned_object`` for enrichment only.

    NetBox inlines the assigned interface / object on read; we keep the
    interface name + its parent device name (when present) so the mapper
    can fold them into ``description`` / ``managed_by``. Read-only — never
    becomes its own row (§1).
    """
    if not isinstance(obj, dict):
        return None
    device = obj.get("device")
    device_name = device.get("name") if isinstance(device, dict) else None
    return {
        "name": obj.get("name"),
        "device_name": device_name,
    }


async def fetch_vlan_groups(nb: NetBoxClient) -> list[dict[str, Any]]:
    """``/api/ipam/vlan-groups/`` — group label (both ≤4.0 and 4.1+ shapes)."""
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/ipam/vlan-groups/"):
        out.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "slug": r.get("slug"),
                # ≤4.0: min_vid / max_vid ; 4.1+: vid_ranges — both optional.
                "min_vid": r.get("min_vid"),
                "max_vid": r.get("max_vid"),
                "vid_ranges": r.get("vid_ranges"),
            }
        )
    logger.info("netbox_import.fetch.vlan_groups", count=len(out))
    return out


async def fetch_vlans(nb: NetBoxClient) -> list[dict[str, Any]]:
    """``/api/ipam/vlans/`` → ``VLAN`` source rows."""
    out: list[dict[str, Any]] = []
    async for r in nb.paginate("/api/ipam/vlans/"):
        out.append(
            {
                "id": r.get("id"),
                "vid": r.get("vid"),
                "name": r.get("name"),
                "status": _choice(r.get("status")),
                "site": _normalize_scope(r),
                "group": _brief(r.get("group")),
                "role": _brief(r.get("role")),
                "tenant": _brief(r.get("tenant")),
                "description": r.get("description") or "",
            }
        )
    logger.info("netbox_import.fetch.vlans", count=len(out))
    return out
