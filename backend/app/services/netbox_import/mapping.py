"""Pure NetBox-shape → canonical-IR mappers (issue #36 §2).

Every function here takes a *normalized* raw dict (the output of
``fetch.py``, which already flattened FK briefs + choice ``.value``s +
the version-dependent site/scope shape) and returns a canonical IR
dataclass from :mod:`app.services.netbox_import.canonical`. No DB access,
no NetBox wire-shape branching (that all lives in ``fetch.py``) — these
are pure data transforms, so they're trivially unit-testable against
recorded JSON.

The load-bearing pieces:

* **Status maps** (§2.7): IP ``active`` → ``allocated`` (NOT the
  integration-owned ``dhcp``); prefix ``container`` → an ``IPBlock``,
  else the prefix status maps onto ``Subnet.status``.
* **Block-vs-subnet decision** (§2.5): sort prefixes by ``(prefixlen
  ASC, network ASC)`` parent-first; a prefix is an ``IPBlock`` if it's a
  ``container`` **or** it encloses an already-classified prefix in the
  same VRF, else a ``Subnet``. Two overlapping prefixes can't both be
  subnets (``Subnet`` overlap is forbidden space-wide), so the enclosing
  one is forced to a block.
* **Multicast kind detection**: IPv4 inside ``224.0.0.0/4`` / IPv6 inside
  ``ff00::/8`` → ``kind="multicast"`` (mirrors ``ipam/router.py``).
* **``netbox_id`` stashing**: merged into ``custom_fields`` where the
  target model has that column, else into ``tags`` (per
  ``netbox_ctx/03_models.md``). VLAN has neither, so its IR carries
  ``netbox_id`` for the preview / audit only.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import structlog

from .canonical import (
    ImportedAddress,
    ImportedBlock,
    ImportedCustomer,
    ImportedSite,
    ImportedSubnet,
    ImportedVLAN,
    ImportedVRF,
)

logger = structlog.get_logger(__name__)

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Multicast detection ranges (mirrors create_subnet in ipam/router.py).
_V4_MULTICAST = ipaddress.IPv4Network("224.0.0.0/4")
_V6_MULTICAST = ipaddress.IPv6Network("ff00::/8")

# --------------------------------------------------------------------------- #
# Status maps (§2.7).
# --------------------------------------------------------------------------- #

# NetBox prefix status → SpatiumDDI Subnet.status. ``container`` is absent
# here — a container prefix is routed to an IPBlock (no status), not a
# subnet. Unknown / missing values fall back to "active".
_PREFIX_STATUS_MAP: dict[str, str] = {
    "active": "active",
    "reserved": "reserved",
    "deprecated": "deprecated",
}

# NetBox IP status → SpatiumDDI IPAddress.status. Uses the operator-
# settable values for a one-shot migration (NOT the integration-owned
# ``dhcp`` status); ``dhcp`` / ``slaac`` collapse to ``allocated``.
_IP_STATUS_MAP: dict[str, str] = {
    "active": "allocated",
    "reserved": "reserved",
    "deprecated": "deprecated",
    "dhcp": "allocated",
    "slaac": "allocated",
}

# SpatiumDDI IPAddress.role enum (IP_ROLES in models/ipam.py). A NetBox
# IP role.value that's in this set maps straight through; otherwise the
# raw value lands in custom_fields["netbox_role"] and role stays NULL.
_IP_ROLES: frozenset[str] = frozenset(
    {"host", "loopback", "anycast", "vip", "vrrp", "secondary", "gateway", "web", "api", "lb"}
)

# SpatiumDDI Subnet.subnet_role values (SUBNET_ROLES). A NetBox prefix
# role.slug/name in this set maps to subnet_role; otherwise the raw role
# lands in custom_fields["netbox_role"].
_SUBNET_ROLES: frozenset[str] = frozenset({"data", "voice", "management", "guest"})


def _multicast_kind(net: IPNetwork) -> str:
    """Return ``"multicast"`` / ``"unicast"`` for a network (§3a)."""
    if isinstance(net, ipaddress.IPv4Network):
        is_mc = net.subnet_of(_V4_MULTICAST)
    else:
        is_mc = net.subnet_of(_V6_MULTICAST)
    return "multicast" if is_mc else "unicast"


def _parse_net(cidr: str) -> IPNetwork | None:
    """Parse a CIDR to a network object, or ``None`` if unparseable."""
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None


def _merge_custom_fields(nb_custom_fields: Any, netbox_id: int | None) -> dict[str, Any]:
    """Merge NetBox custom_fields + stash netbox_id into a fresh dict.

    NetBox custom_fields serialize as a dict of ``{field_name: value}``;
    we drop ``None`` values (an empty NetBox CF). The NetBox primary key
    is stashed under ``netbox_id`` so a re-import can match the row.
    """
    out: dict[str, Any] = {}
    if isinstance(nb_custom_fields, dict):
        for key, value in nb_custom_fields.items():
            if value is not None:
                out[str(key)] = value
    if netbox_id is not None:
        out["netbox_id"] = netbox_id
    return out


def _tag_dict(netbox_id: int | None, **extra: Any) -> dict[str, Any]:
    """Build a ``tags`` dict carrying netbox_id (for models w/o custom_fields)."""
    out: dict[str, Any] = {}
    if netbox_id is not None:
        out["netbox_id"] = netbox_id
    for key, value in extra.items():
        if value is not None:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# Tenant → Customer.
# --------------------------------------------------------------------------- #


def map_customer(tenant: dict[str, Any]) -> ImportedCustomer:
    """NetBox tenant → :class:`ImportedCustomer`."""
    group = tenant.get("group") or {}
    tags = {}
    group_name = group.get("name") if isinstance(group, dict) else None
    if group_name:
        tags["netbox_tenant_group"] = group_name
    cf = _merge_custom_fields(tenant.get("custom_fields"), tenant.get("id"))
    # Tenant slug has no native column — keep it in custom_fields.
    if tenant.get("slug"):
        cf.setdefault("netbox_slug", tenant["slug"])
    return ImportedCustomer(
        name=str(tenant.get("name") or "").strip(),
        notes=str(tenant.get("description") or ""),
        custom_fields=cf,
        tags=tags,
        netbox_id=tenant.get("id"),
    )


# --------------------------------------------------------------------------- #
# Site / Region → Site.
# --------------------------------------------------------------------------- #


def map_site(site: dict[str, Any]) -> ImportedSite:
    """NetBox site → :class:`ImportedSite`.

    Region is the primary parent axis (``parent_code`` = region slug);
    site-group folds into a ``tags`` label. NetBox site status (no native
    column) folds into ``notes`` + a ``tags`` flag. Site has no
    ``custom_fields`` column, so ``netbox_id`` rides in ``tags``.
    """
    region = site.get("region") or {}
    region_slug = region.get("slug") if isinstance(region, dict) else None
    region_name = region.get("name") if isinstance(region, dict) else None
    group = site.get("group") or {}
    group_name = group.get("name") if isinstance(group, dict) else None

    notes_parts: list[str] = []
    if site.get("physical_address"):
        notes_parts.append(str(site["physical_address"]))
    if site.get("description"):
        notes_parts.append(str(site["description"]))
    status = site.get("status")
    if status:
        notes_parts.append(f"NetBox status: {status}")

    tags = _tag_dict(
        site.get("id"),
        netbox_site_group=group_name,
        netbox_status=status,
    )

    code = site.get("slug") or None
    return ImportedSite(
        name=str(site.get("name") or "").strip(),
        code=str(code) if code else None,
        parent_code=str(region_slug) if region_slug else None,
        kind="datacenter",
        region=str(region_name) if region_name else None,
        notes="\n".join(notes_parts),
        tags=tags,
        netbox_id=site.get("id"),
    )


def map_region_as_site(region: dict[str, Any]) -> ImportedSite:
    """NetBox region → a parent :class:`ImportedSite` node.

    Regions become Site tree nodes so a prefix/site's ``parent_code``
    (the region slug) resolves to a real parent. The region's own
    ``parent`` (a region) gives the grandparent linkage.
    """
    parent = region.get("parent") or {}
    parent_slug = parent.get("slug") if isinstance(parent, dict) else None
    return ImportedSite(
        name=str(region.get("name") or "").strip(),
        code=str(region["slug"]) if region.get("slug") else None,
        parent_code=str(parent_slug) if parent_slug else None,
        kind="datacenter",
        region=str(region.get("name") or "") or None,
        notes=str(region.get("description") or ""),
        tags=_tag_dict(region.get("id"), netbox_kind="region"),
        netbox_id=region.get("id"),
    )


# --------------------------------------------------------------------------- #
# VRF → VRF.
# --------------------------------------------------------------------------- #


def map_vrf(vrf: dict[str, Any]) -> ImportedVRF:
    """NetBox VRF → :class:`ImportedVRF`."""
    tenant = vrf.get("tenant") or {}
    tenant_name = tenant.get("name") if isinstance(tenant, dict) else None
    cf = _merge_custom_fields(vrf.get("custom_fields"), vrf.get("id"))
    if vrf.get("enforce_unique") is not None:
        cf["netbox_enforce_unique"] = vrf["enforce_unique"]
    return ImportedVRF(
        name=str(vrf.get("name") or "").strip(),
        rd=str(vrf["rd"]) if vrf.get("rd") else None,
        import_targets=list(vrf.get("import_targets") or []),
        export_targets=list(vrf.get("export_targets") or []),
        description=str(vrf.get("description") or ""),
        customer_name=str(tenant_name) if tenant_name else None,
        custom_fields=cf,
        tags={},
        netbox_id=vrf.get("id"),
    )


# --------------------------------------------------------------------------- #
# Aggregate → top-level IPBlock.
# --------------------------------------------------------------------------- #


def map_aggregate(aggregate: dict[str, Any], *, space_name: str) -> ImportedBlock | None:
    """NetBox aggregate → top-level :class:`ImportedBlock` under Global space.

    Returns ``None`` for an unparseable prefix (the caller drops it with a
    warning). RIR has no native concept — it lands in ``custom_fields``.
    """
    cidr = str(aggregate.get("prefix") or "").strip()
    net = _parse_net(cidr)
    if net is None:
        return None
    cf = _merge_custom_fields(aggregate.get("custom_fields"), aggregate.get("id"))
    rir = aggregate.get("rir") or {}
    rir_name = rir.get("name") if isinstance(rir, dict) else None
    if rir_name:
        cf["netbox_rir"] = rir_name
    tenant = aggregate.get("tenant") or {}
    tenant_name = tenant.get("name") if isinstance(tenant, dict) else None
    return ImportedBlock(
        network=str(net),
        name=f"auto:{net}",
        description=str(aggregate.get("description") or ""),
        space_name=space_name,
        parent_cidr=None,
        customer_name=str(tenant_name) if tenant_name else None,
        site_code=None,
        custom_fields=cf,
        tags={},
        netbox_id=aggregate.get("id"),
    )


# --------------------------------------------------------------------------- #
# VLAN → VLAN.
# --------------------------------------------------------------------------- #


def map_vlan(vlan: dict[str, Any]) -> ImportedVLAN | None:
    """NetBox VLAN → :class:`ImportedVLAN`.

    Returns ``None`` when ``vid`` / ``name`` is missing (a malformed VLAN
    the caller drops with a warning). VLAN has no tags/custom_fields, so
    ``netbox_id`` is carried for the preview / audit only.
    """
    vid = vlan.get("vid")
    name = str(vlan.get("name") or "").strip()
    if vid is None or not name:
        return None
    return ImportedVLAN(
        vid=int(vid),
        name=name,
        description=str(vlan.get("description") or ""),
        netbox_id=vlan.get("id"),
    )


# --------------------------------------------------------------------------- #
# Prefix → IPBlock or Subnet (the crux, §2.5).
# --------------------------------------------------------------------------- #


def _prefix_role(prefix: dict[str, Any]) -> tuple[str | None, str | None]:
    """Resolve a prefix role to ``(subnet_role, raw_role_for_cf)``.

    A role whose slug/name is in ``data|voice|management|guest`` becomes
    ``Subnet.subnet_role``; any other role is preserved raw in
    ``custom_fields["netbox_role"]`` so nothing is lost.
    """
    role = prefix.get("role") or {}
    if not isinstance(role, dict):
        return None, None
    slug = (role.get("slug") or "").lower()
    name = (role.get("name") or "").lower()
    if slug in _SUBNET_ROLES:
        return slug, None
    if name in _SUBNET_ROLES:
        return name, None
    raw = role.get("slug") or role.get("name")
    return None, (str(raw) if raw else None)


def classify_prefixes(prefixes: list[dict[str, Any]]) -> dict[int, str]:
    """Decide IPBlock vs Subnet for every prefix (§2.5).

    Returns a map of ``netbox prefix id → "block" | "subnet"``. The rule,
    applied **child-first** (sort by ``(prefixlen DESC, network ASC)``
    within each VRF so a more-specific child is classified *before* the
    prefix that encloses it — only then can the enclosing prefix see the
    child in ``prior`` and demote itself to a block):

    * ``status == "container"`` ⇒ block.
    * a prefix that **encloses** any already-classified prefix in the same
      VRF ⇒ block (two overlapping prefixes can't both be subnets —
      ``Subnet`` overlap is forbidden space-wide).
    * otherwise (a true leaf) ⇒ subnet.

    Note: this is the *classification* order, independent of the commit's
    insert order, which stays parent-first (largest prefix first) so a
    block exists before the subnets/child-blocks it contains.

    Unparseable prefixes are skipped (the mapper drops them with a
    warning). VRF identity is keyed on ``vrf.id`` (``None`` = global
    table) so overlapping CIDRs in different VRFs are classified
    independently.
    """
    # (vrf_key, prefixlen, network_int, id, net, status)
    parsed: list[tuple[Any, int, int, int, IPNetwork, str | None]] = []
    for p in prefixes:
        pid = p.get("id")
        net = _parse_net(str(p.get("prefix") or ""))
        if net is None or pid is None:
            continue
        vrf = p.get("vrf") or {}
        vrf_key = vrf.get("id") if isinstance(vrf, dict) else None
        parsed.append(
            (
                vrf_key,
                net.prefixlen,
                int(net.network_address),
                int(pid),
                net,
                p.get("status"),
            )
        )

    # Child-first ordering: longer prefixlen (most-specific) first, then
    # network address ascending. A child is therefore already in `prior`
    # when its enclosing supernet is evaluated, so the enclosing rule fires.
    parsed.sort(key=lambda t: (-t[1], t[2]))

    decisions: dict[int, str] = {}
    # Per-VRF list of already-classified networks (any kind).
    classified_by_vrf: dict[Any, list[IPNetwork]] = {}

    for vrf_key, _plen, _naddr, pid, net, status in parsed:
        prior = classified_by_vrf.setdefault(vrf_key, [])
        if status == "container":
            kind = "block"
        elif _encloses_any(net, prior):
            # This prefix is a strict supernet of something already
            # classified in the same VRF — it must be a block.
            kind = "block"
        else:
            kind = "subnet"
        decisions[pid] = kind
        prior.append(net)

    return decisions


def _encloses_any(net: IPNetwork, others: list[IPNetwork]) -> bool:
    """True if ``net`` is a strict supernet of any network in ``others``."""
    for other in others:
        if other == net:
            continue
        # Paired isinstance narrowing so subnet_of's same-version contract
        # is type-honest (mirrors dhcp_import/commit.py _create_subnet).
        if isinstance(net, ipaddress.IPv4Network) and isinstance(other, ipaddress.IPv4Network):
            if other.subnet_of(net):
                return True
        elif isinstance(net, ipaddress.IPv6Network) and isinstance(other, ipaddress.IPv6Network):
            if other.subnet_of(net):
                return True
    return False


def map_prefix_block(prefix: dict[str, Any], *, space_name: str | None) -> ImportedBlock | None:
    """NetBox prefix (classified as a block) → :class:`ImportedBlock`."""
    cidr = str(prefix.get("prefix") or "").strip()
    net = _parse_net(cidr)
    if net is None:
        return None
    cf = _merge_custom_fields(prefix.get("custom_fields"), prefix.get("id"))
    if prefix.get("is_pool") is not None:
        cf["netbox_is_pool"] = prefix["is_pool"]
    _subnet_role, raw_role = _prefix_role(prefix)
    role_value = _subnet_role or raw_role
    if role_value:
        cf.setdefault("netbox_role", role_value)
    tenant = prefix.get("tenant") or {}
    tenant_name = tenant.get("name") if isinstance(tenant, dict) else None
    site = prefix.get("site") or {}
    site_code = site.get("slug") if isinstance(site, dict) else None
    return ImportedBlock(
        network=str(net),
        name=str(prefix.get("description") or "") or f"auto:{net}",
        description=str(prefix.get("description") or ""),
        space_name=space_name,
        parent_cidr=None,  # the committer resolves nesting largest-first
        customer_name=str(tenant_name) if tenant_name else None,
        site_code=str(site_code) if site_code else None,
        custom_fields=cf,
        tags={},
        netbox_id=prefix.get("id"),
    )


def map_prefix_subnet(prefix: dict[str, Any], *, space_name: str | None) -> ImportedSubnet | None:
    """NetBox prefix (classified as a leaf) → :class:`ImportedSubnet`."""
    cidr = str(prefix.get("prefix") or "").strip()
    net = _parse_net(cidr)
    if net is None:
        return None
    cf = _merge_custom_fields(prefix.get("custom_fields"), prefix.get("id"))
    if prefix.get("is_pool") is not None:
        cf["netbox_is_pool"] = prefix["is_pool"]
    subnet_role, raw_role = _prefix_role(prefix)
    if raw_role:
        cf.setdefault("netbox_role", raw_role)
    status = _PREFIX_STATUS_MAP.get(str(prefix.get("status") or ""), "active")
    tenant = prefix.get("tenant") or {}
    tenant_name = tenant.get("name") if isinstance(tenant, dict) else None
    site = prefix.get("site") or {}
    site_code = site.get("slug") if isinstance(site, dict) else None
    vlan = prefix.get("vlan") or {}
    # The VLAN brief on a prefix doesn't carry vid directly; the committer
    # resolves vlan_ref via the imported VLAN list. We stash the NetBox
    # vlan id so the committer can match — but the IR's vlan_vid field is
    # the VLAN's vid, resolved by the caller against the VLAN pass.
    vlan_netbox_id = vlan.get("id") if isinstance(vlan, dict) else None
    if vlan_netbox_id is not None:
        cf.setdefault("netbox_vlan_id", vlan_netbox_id)
    return ImportedSubnet(
        network=str(net),
        name=str(prefix.get("description") or ""),
        description=str(prefix.get("description") or ""),
        space_name=space_name,
        status=status,
        vlan_vid=None,  # resolved against the VLAN pass by the caller
        customer_name=str(tenant_name) if tenant_name else None,
        site_code=str(site_code) if site_code else None,
        subnet_role=subnet_role,
        kind=_multicast_kind(net),
        custom_fields=cf,
        tags={},
        netbox_id=prefix.get("id"),
    )


# --------------------------------------------------------------------------- #
# IP address → IPAddress.
# --------------------------------------------------------------------------- #


def _split_dns_name(dns_name: str) -> tuple[str | None, str | None]:
    """Split NetBox ``dns_name`` into ``(hostname, fqdn)``.

    The full name is the fqdn; the host label is the first segment.
    """
    name = (dns_name or "").strip().rstrip(".")
    if not name:
        return None, None
    hostname = name.split(".", 1)[0]
    return hostname, name


def map_address(ip: dict[str, Any]) -> ImportedAddress | None:
    """NetBox IP address → :class:`ImportedAddress`.

    The ``/mask`` is stripped from ``address`` (the mask only locates the
    enclosing subnet — kept as ``subnet_cidr`` for the committer's
    most-specific-subnet resolution). Returns ``None`` on an unparseable
    address (the caller drops it with a warning).
    """
    raw = str(ip.get("address") or "").strip()
    if not raw:
        return None
    try:
        iface = ipaddress.ip_interface(raw)
    except ValueError:
        return None
    bare = str(iface.ip)
    subnet_cidr = str(iface.network)

    status = _IP_STATUS_MAP.get(str(ip.get("status") or ""), "allocated")

    raw_role = str(ip.get("role") or "").lower() or None
    if raw_role in _IP_ROLES:
        role: str | None = raw_role
        cf_role: str | None = None
    else:
        role = None
        cf_role = raw_role

    cf = _merge_custom_fields(ip.get("custom_fields"), ip.get("id"))
    if cf_role:
        cf["netbox_role"] = cf_role

    hostname, fqdn = _split_dns_name(str(ip.get("dns_name") or ""))

    # assigned_object enrichment (device / interface) — read-only.
    description = str(ip.get("description") or "")
    assigned = ip.get("assigned_object") or {}
    managed_by: str | None = None
    if isinstance(assigned, dict):
        device_name = assigned.get("device_name")
        iface_name = assigned.get("name")
        if device_name:
            managed_by = str(device_name)
        if iface_name:
            suffix = f"NetBox iface: {iface_name}"
            description = f"{description}\n{suffix}".strip() if description else suffix
    if managed_by:
        cf.setdefault("netbox_managed_by", managed_by)

    return ImportedAddress(
        address=bare,
        status=status,
        role=role,
        hostname=hostname,
        fqdn=fqdn,
        description=description,
        subnet_cidr=subnet_cidr,
        space_name=None,  # resolved by the committer via the enclosing subnet
        custom_fields=cf,
        tags={},
        netbox_id=ip.get("id"),
    )
