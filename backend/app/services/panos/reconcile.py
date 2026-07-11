"""Per-firewall PAN-OS / Panorama read-only reconciler (#605).

For one ``PANOSFirewall`` row:

  1. Fetch system info + address objects/groups (+ NAT rules, interfaces,
     DHCP leases per the mirror toggles) from the PAN-OS API.
  2. Mirror address objects/groups into ``firewall_endpoint_object`` rows,
     resolving each to a live IPAM ``ip_address`` / ``subnet`` where the value
     matches (the "shadow IPAM" drift join).
  3. Mirror NAT rules into ``nat_mapping`` rows stamped with
     ``panos_firewall_id`` provenance (so they sweep on target delete and
     never collide with operator-entered rows).
  4. Optionally mirror zone/interface CIDRs → IPAM subnets and DHCP leases →
     IPAM addresses (same claim/create/update/un-claim diff the OPNsense
     mirror uses, with the full sibling-integration ownership guard).
  5. Persist sync state + an audit row.

Strictly read-only on the firewall — the DAG-enforcement writes live in
``app.services.block_sync.reconcile``.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_str
from app.models.audit import AuditLog
from app.models.ipam import IPAddress, IPBlock, NATMapping, Subnet
from app.models.panos import FIREWALL_OBJECT_KINDS, FirewallObject, PANOSFirewall
from app.services.panos.client import (
    PANOSClient,
    PANOSClientError,
    _PANAddressObject,
    _PANInterface,
    _PANLease,
    _PANNatRule,
    resolved_cidr_for,
)

logger = structlog.get_logger(__name__)

_BIGINT_MAX = 2**63 - 1

# Sibling integration provenance columns — a row carrying any of these is
# owned by another mirror and must not be claimed by the PAN-OS reconciler.
_OTHER_INTEGRATION_FKS = (
    "kubernetes_cluster_id",
    "docker_host_id",
    "proxmox_node_id",
    "tailscale_tenant_id",
    "unifi_controller_id",
    "cloud_endpoint_id",
    "opnsense_router_id",
    "netbird_instance_id",
)


@dataclass
class ReconcileSummary:
    ok: bool
    error: str | None = None
    sw_version: str | None = None
    model: str | None = None
    object_count: int = 0
    nat_rule_count: int = 0
    interface_count: int = 0
    lease_count: int = 0
    objects_created: int = 0
    objects_updated: int = 0
    objects_deleted: int = 0
    nat_created: int = 0
    nat_updated: int = 0
    nat_deleted: int = 0
    subnets_created: int = 0
    subnets_updated: int = 0
    subnets_deleted: int = 0
    subnets_matched: int = 0
    blocks_created: int = 0
    blocks_deleted: int = 0
    addresses_created: int = 0
    addresses_updated: int = 0
    addresses_deleted: int = 0
    skipped_no_subnet: int = 0
    warnings: list[str] = field(default_factory=list)


def _parse_net(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(value, strict=False)
    except (ValueError, TypeError):
        return None


def _lan_total_ips(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> int:
    if isinstance(net, ipaddress.IPv6Network):
        return min(net.num_addresses, _BIGINT_MAX)
    if net.prefixlen >= 31:
        return net.num_addresses
    return net.num_addresses - 2


# ── FirewallObject mirror ────────────────────────────────────────────


async def _load_ipam_resolve_maps(
    db: AsyncSession, fw: PANOSFirewall
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Preload the in-space host→ip_id and cidr→subnet_id maps in two queries so
    the per-object drift resolve is a dict lookup, not an N+1 SELECT storm."""
    ip_rows = (
        await db.execute(
            select(IPAddress.id, IPAddress.address)
            .join(Subnet, IPAddress.subnet_id == Subnet.id)
            .where(Subnet.space_id == fw.ipam_space_id)
        )
    ).all()
    ip_by_host = {str(addr): rid for rid, addr in ip_rows}
    subnet_rows = (
        await db.execute(
            select(Subnet.id, Subnet.network).where(Subnet.space_id == fw.ipam_space_id)
        )
    ).all()
    subnet_by_cidr = {str(net): sid for sid, net in subnet_rows}
    return ip_by_host, subnet_by_cidr


def _resolve_object_links(
    resolved_cidr: str | None,
    ip_by_host: dict[str, Any],
    subnet_by_cidr: dict[str, Any],
) -> tuple[Any, Any]:
    """Best-effort link a mirrored object to a live IPAM row using the preloaded
    maps. Returns ``(ip_address_id, subnet_id)`` — either/both may be None."""
    if not resolved_cidr:
        return None, None
    net = _parse_net(resolved_cidr)
    if net is None:
        return None, None
    # A /32 (or /128) host object → the exact in-space IPAddress, if any.
    if net.prefixlen in (32, 128):
        ip_id = ip_by_host.get(str(net.network_address))
        if ip_id is not None:
            return ip_id, None
    # Otherwise (or if no host match) → an exact-CIDR subnet match.
    return None, subnet_by_cidr.get(str(net))


async def _apply_objects(
    db: AsyncSession,
    fw: PANOSFirewall,
    desired: list[_PANAddressObject],
    summary: ReconcileSummary,
) -> None:
    existing_rows = (
        (await db.execute(select(FirewallObject).where(FirewallObject.panos_firewall_id == fw.id)))
        .scalars()
        .all()
    )
    existing = {r.name: r for r in existing_rows}
    desired_map = {d.name: d for d in desired if d.name}

    for name, row in existing.items():
        if name not in desired_map:
            await db.delete(row)
            summary.objects_deleted += 1

    ip_by_host, subnet_by_cidr = await _load_ipam_resolve_maps(db, fw) if desired_map else ({}, {})
    for name, d in desired_map.items():
        kind = d.kind if d.kind in FIREWALL_OBJECT_KINDS else "host"
        resolved = resolved_cidr_for(kind, d.value)
        ip_id, subnet_id = _resolve_object_links(resolved, ip_by_host, subnet_by_cidr)
        row = existing.get(name)
        if row is None:
            db.add(
                FirewallObject(
                    panos_firewall_id=fw.id,
                    name=name,
                    kind=kind,
                    value=d.value,
                    description=d.description,
                    tags=list(d.tags),
                    resolved_cidr=resolved,
                    ip_address_id=ip_id,
                    subnet_id=subnet_id,
                )
            )
            summary.objects_created += 1
        else:
            changed = False
            if row.kind != kind:
                row.kind, changed = kind, True
            if (row.value or "") != d.value:
                row.value, changed = d.value, True
            if (row.description or "") != d.description:
                row.description, changed = d.description, True
            if list(row.tags or []) != list(d.tags):
                row.tags, changed = list(d.tags), True
            if (row.resolved_cidr or None) != resolved:
                row.resolved_cidr, changed = resolved, True
            if row.ip_address_id != ip_id:
                row.ip_address_id, changed = ip_id, True
            if row.subnet_id != subnet_id:
                row.subnet_id, changed = subnet_id, True
            if changed:
                summary.objects_updated += 1


# ── NAT mirror ───────────────────────────────────────────────────────


def _nat_ips(rule: _PANNatRule) -> tuple[str | None, str | None]:
    """Map a PAN-OS NAT rule to ``(internal_ip, external_ip)`` for the
    ``nat_mapping`` shape. DNAT: external = original dst, internal = translated
    dst. SNAT: internal = original source, external = translated source."""

    def _one_ip(value: str | None) -> str | None:
        if not value:
            return None
        v = value.strip()
        try:
            return str(ipaddress.ip_interface(v).ip) if "/" in v else str(ipaddress.ip_address(v))
        except (ValueError, TypeError):
            return None  # named object / range / 'any' — not a bare IP

    if rule.translated_dst:  # inbound DNAT / port-forward
        return _one_ip(rule.translated_dst), _one_ip(rule.original_dst)
    if rule.translated_src:  # SNAT
        return _one_ip(rule.source), _one_ip(rule.translated_src)
    return None, None


async def _apply_nat(
    db: AsyncSession,
    fw: PANOSFirewall,
    desired: list[_PANNatRule],
    summary: ReconcileSummary,
) -> None:
    existing_rows = (
        (await db.execute(select(NATMapping).where(NATMapping.panos_firewall_id == fw.id)))
        .scalars()
        .all()
    )
    existing = {r.name: r for r in existing_rows}
    desired_map: dict[str, _PANNatRule] = {}
    for d in desired:
        if d.name and d.name not in desired_map:
            desired_map[d.name] = d

    for name, row in existing.items():
        if name not in desired_map:
            await db.delete(row)
            summary.nat_deleted += 1

    for name, d in desired_map.items():
        internal, external = _nat_ips(d)
        # A rule whose endpoints are named objects / 'any' resolves to no bare
        # IPs — a content-free nat_mapping row. Don't mirror it; drop the row if
        # a previous sync created one when the rule still had a literal IP.
        if internal is None and external is None:
            row = existing.get(name)
            if row is not None:
                await db.delete(row)
                summary.nat_deleted += 1
            continue
        description = d.description or f"PAN-OS NAT rule on {fw.name}"
        row = existing.get(name)
        if row is None:
            db.add(
                NATMapping(
                    name=name,
                    kind=d.kind,
                    internal_ip=internal,
                    external_ip=external,
                    protocol="any",
                    device_label=fw.name,
                    description=description,
                    panos_firewall_id=fw.id,
                )
            )
            summary.nat_created += 1
        else:
            changed = False
            if row.kind != d.kind:
                row.kind, changed = d.kind, True
            if (str(row.internal_ip) if row.internal_ip else None) != internal:
                row.internal_ip, changed = internal, True
            if (str(row.external_ip) if row.external_ip else None) != external:
                row.external_ip, changed = external, True
            if (row.description or "") != description:
                row.description, changed = description, True
            if changed:
                summary.nat_updated += 1


# ── Interface + lease IPAM mirror (opt-in) ───────────────────────────


def _find_subnet_for_ip(subnets: list[Subnet], ip: str) -> Subnet | None:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    best: Subnet | None = None
    best_prefix = -1
    for s in subnets:
        net = _parse_net(str(s.network))
        if net is None:
            continue
        if addr in net and net.prefixlen > best_prefix:
            best, best_prefix = s, net.prefixlen
    return best


async def _recompute_subnet_utilization(db: AsyncSession, subnet_id: Any) -> None:
    allocated = (
        await db.scalar(
            select(func.count())
            .select_from(IPAddress)
            .where(IPAddress.subnet_id == subnet_id)
            .where(IPAddress.status != "available")
        )
        or 0
    )
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        return
    subnet.allocated_ips = allocated
    subnet.utilization_percent = (
        round(allocated / subnet.total_ips * 100, 2) if subnet.total_ips > 0 else 0.0
    )


def _find_enclosing_operator_block(blocks: list[IPBlock], cidr: str) -> IPBlock | None:
    """The tightest unowned (operator) block that contains ``cidr`` — used as the
    subnet's parent so we don't create a redundant wrapper. Same shape as the
    OPNsense reconciler's lookup."""
    net = _parse_net(cidr)
    if net is None:
        return None
    best: IPBlock | None = None
    best_prefix = -1
    for b in blocks:
        bnet = _parse_net(str(b.network))
        if bnet is None or type(bnet) is not type(net):
            continue
        if net.subnet_of(bnet) and bnet.prefixlen > best_prefix:  # type: ignore[arg-type]
            best, best_prefix = b, bnet.prefixlen
    return best


async def _apply_interfaces(
    db: AsyncSession,
    fw: PANOSFirewall,
    interfaces: list[_PANInterface],
    summary: ReconcileSummary,
) -> None:
    """Mirror interface CIDRs as PAN-owned subnets. Each subnet is parented on
    the tightest enclosing operator block, or — failing that — a per-CIDR wrapper
    block (``network == cidr``) so the parent always contains the subnet. A
    single shared wrapper would strand subnets in unrelated ranges under a
    wrong-CIDR parent (e.g. a 192.168.x subnet under a 10.0.0.0/8 wrapper)."""
    all_subnets = (
        (await db.execute(select(Subnet).where(Subnet.space_id == fw.ipam_space_id)))
        .scalars()
        .all()
    )
    fw_subnets = {str(s.network): s for s in all_subnets if s.panos_firewall_id == fw.id}
    operator_subnets = {
        str(s.network): s
        for s in all_subnets
        if s.panos_firewall_id is None
        and all(getattr(s, fk) is None for fk in _OTHER_INTEGRATION_FKS)
    }
    foreign_subnets = {
        str(s.network): s
        for s in all_subnets
        if s.panos_firewall_id is None and str(s.network) not in operator_subnets
    }

    # Dedupe desired by CIDR (multiple sub-interfaces can share a network).
    desired: dict[str, _PANInterface] = {}
    for iface in interfaces:
        if iface.cidr not in desired:
            desired[iface.cidr] = iface

    blocks = list(
        (await db.execute(select(IPBlock).where(IPBlock.space_id == fw.ipam_space_id)))
        .scalars()
        .all()
    )
    operator_blocks = [
        b
        for b in blocks
        if b.panos_firewall_id is None
        and all(getattr(b, fk) is None for fk in _OTHER_INTEGRATION_FKS)
    ]
    # One wrapper block per CIDR, keyed by network — never a single shared one.
    fw_wrappers = {str(b.network): b for b in blocks if b.panos_firewall_id == fw.id}

    # Delete PAN-owned subnets no longer present (un-claim if foreign IPs live in).
    for cidr, row in list(fw_subnets.items()):
        if cidr in desired:
            continue
        surviving = await db.scalar(
            select(func.count())
            .select_from(IPAddress)
            .where(IPAddress.subnet_id == row.id)
            .where(IPAddress.panos_firewall_id.is_(None))
        )
        if surviving:
            row.panos_firewall_id = None
            summary.subnets_updated += 1
        else:
            await db.delete(row)
            summary.subnets_deleted += 1

    used_wrapper_cidrs: set[str] = set()
    for cidr, iface in desired.items():
        if cidr in operator_subnets:
            summary.subnets_matched += 1
            continue
        if cidr in foreign_subnets:
            summary.warnings.append(
                f"subnet {cidr} already exists, owned by another integration; not duplicating"
            )
            continue

        parent = _find_enclosing_operator_block(operator_blocks, cidr)
        if parent is None:
            wrapper = fw_wrappers.get(cidr)
            if wrapper is None:
                wrapper = IPBlock(
                    space_id=fw.ipam_space_id,
                    network=cidr,
                    name=f"{fw.name} {cidr}",
                    description=f"Auto-created for Palo Alto firewall {fw.name}",
                    panos_firewall_id=fw.id,
                )
                db.add(wrapper)
                await db.flush()
                fw_wrappers[cidr] = wrapper
                summary.blocks_created += 1
            used_wrapper_cidrs.add(cidr)
            parent = wrapper

        net = _parse_net(cidr)
        total = _lan_total_ips(net) if net is not None else 0
        existing = fw_subnets.get(cidr)
        name = f"{fw.name}/{iface.zone or iface.name}"
        desc = f"PAN-OS interface {iface.name}" + (f" (zone {iface.zone})" if iface.zone else "")
        if existing is None:
            db.add(
                Subnet(
                    space_id=fw.ipam_space_id,
                    block_id=parent.id,
                    network=cidr,
                    name=name,
                    description=desc,
                    gateway=iface.address,
                    panos_firewall_id=fw.id,
                    total_ips=total,
                )
            )
            summary.subnets_created += 1
        else:
            changed = False
            if existing.block_id != parent.id:
                existing.block_id, changed = parent.id, True
            if existing.name != name:
                existing.name, changed = name, True
            if existing.description != desc:
                existing.description, changed = desc, True
            if iface.address and existing.gateway != iface.address:
                existing.gateway, changed = iface.address, True
            if existing.total_ips != total:
                existing.total_ips, changed = total, True
            if changed:
                summary.subnets_updated += 1
    await db.flush()

    # Drop PAN-owned wrapper blocks that no longer back a subnet.
    for cidr, wrapper in fw_wrappers.items():
        if cidr in used_wrapper_cidrs:
            continue
        refs = await db.scalar(
            select(func.count()).select_from(Subnet).where(Subnet.block_id == wrapper.id)
        )
        if not refs:
            await db.delete(wrapper)
            summary.blocks_deleted += 1


async def _apply_leases(
    db: AsyncSession,
    fw: PANOSFirewall,
    leases: list[_PANLease],
    summary: ReconcileSummary,
) -> None:
    subnets = (
        (await db.execute(select(Subnet).where(Subnet.space_id == fw.ipam_space_id)))
        .scalars()
        .all()
    )
    subnets = list(subnets)
    current_rows = (
        (await db.execute(select(IPAddress).where(IPAddress.panos_firewall_id == fw.id)))
        .scalars()
        .all()
    )
    current = {str(a.address): a for a in current_rows}
    desired: dict[str, _PANLease] = {}
    for ls in leases:
        if ls.address not in desired:
            desired[ls.address] = ls

    dirty: set[Any] = set()
    for addr, row in current.items():
        if addr not in desired:
            dirty.add(row.subnet_id)
            if row.user_modified_at is not None:
                row.panos_firewall_id = None
                summary.addresses_updated += 1
            else:
                await db.delete(row)
                summary.addresses_deleted += 1

    for addr, ls in desired.items():
        subnet = _find_subnet_for_ip(subnets, addr)
        if subnet is None:
            summary.skipped_no_subnet += 1
            continue
        desc = f"PAN-OS DHCP lease ({ls.state})"
        row = current.get(addr)
        if row is None:
            db.add(
                IPAddress(
                    subnet_id=subnet.id,
                    address=addr,
                    status="dhcp",
                    hostname=ls.hostname or "",
                    description=desc,
                    mac_address=ls.mac,
                    auto_from_lease=True,
                    panos_firewall_id=fw.id,
                )
            )
            dirty.add(subnet.id)
            summary.addresses_created += 1
        elif row.user_modified_at is None:
            changed = False
            if row.status != "dhcp":
                row.status, changed = "dhcp", True
            if (row.hostname or "") != (ls.hostname or ""):
                row.hostname, changed = ls.hostname or "", True
            if (row.description or "") != desc:
                row.description, changed = desc, True
            if ls.mac and (row.mac_address or "") != ls.mac:
                row.mac_address, changed = ls.mac, True
            if changed:
                dirty.add(subnet.id)
                summary.addresses_updated += 1

    if dirty:
        await db.flush()
        for sid in dirty:
            await _recompute_subnet_utilization(db, sid)


# ── Entry point ───────────────────────────────────────────────────────


async def reconcile_firewall(db: AsyncSession, fw: PANOSFirewall) -> ReconcileSummary:
    summary = ReconcileSummary(ok=False)

    api_key = ""
    if fw.api_key_encrypted:
        try:
            api_key = decrypt_str(fw.api_key_encrypted)
        except ValueError as exc:
            summary.error = f"api-key decrypt failed: {exc}"
    if not api_key:
        summary.error = summary.error or "no API key configured"
        fw.last_sync_error = summary.error
        fw.last_synced_at = datetime.now(UTC)
        await db.commit()
        return summary

    try:
        async with PANOSClient(
            host=fw.host,
            port=fw.port,
            api_key=api_key,
            api_version=fw.api_version,
            is_panorama=fw.is_panorama,
            vsys=fw.vsys,
            device_group=fw.device_group,
            verify_tls=fw.verify_tls,
            ca_bundle_pem=fw.ca_bundle_pem or "",
        ) as client:
            info = await client.get_system_info()
            objects: list[_PANAddressObject] = []
            if fw.mirror_address_objects:
                objects = await client.list_address_objects()
                objects += await client.list_address_groups()
            nat_rules = await client.list_nat_rules() if fw.mirror_nat_rules else []
            interfaces = await client.list_interfaces() if fw.mirror_interfaces else []
            leases = await client.list_dhcp_leases() if fw.mirror_dhcp_leases else []
    except PANOSClientError as exc:
        summary.error = str(exc)
        fw.last_sync_error = summary.error
        fw.last_synced_at = datetime.now(UTC)
        await db.commit()
        logger.warning("panos_reconcile_fetch_failed", firewall=str(fw.id), error=summary.error)
        return summary

    summary.sw_version = info.version
    summary.model = info.model
    summary.object_count = len(objects)
    summary.nat_rule_count = len(nat_rules)
    summary.interface_count = len(interfaces)
    summary.lease_count = len(leases)

    # Always apply, even for a disabled mirror: the fetch above already passed
    # an empty desired set when the toggle is off, so the apply sweeps any rows
    # a previous (enabled) sync created instead of stranding them forever.
    await _apply_objects(db, fw, objects, summary)
    await _apply_nat(db, fw, nat_rules, summary)
    await _apply_interfaces(db, fw, interfaces, summary)
    await _apply_leases(db, fw, leases, summary)

    fw.last_synced_at = datetime.now(UTC)
    fw.last_sync_error = None
    fw.sw_version = info.version
    fw.model = info.model
    fw.object_count = summary.object_count
    fw.nat_rule_count = summary.nat_rule_count

    db.add(
        AuditLog(
            user_display_name="system",
            auth_source="system",
            action="panos.reconcile",
            resource_type="panos_firewall",
            resource_id=str(fw.id),
            resource_display=fw.name,
            new_value={
                "objects": {
                    "created": summary.objects_created,
                    "updated": summary.objects_updated,
                    "deleted": summary.objects_deleted,
                },
                "nat": {
                    "created": summary.nat_created,
                    "updated": summary.nat_updated,
                    "deleted": summary.nat_deleted,
                },
                "subnets": {
                    "created": summary.subnets_created,
                    "updated": summary.subnets_updated,
                    "deleted": summary.subnets_deleted,
                    "matched": summary.subnets_matched,
                },
                "addresses": {
                    "created": summary.addresses_created,
                    "updated": summary.addresses_updated,
                    "deleted": summary.addresses_deleted,
                },
            },
        )
    )
    await db.commit()
    summary.ok = True
    logger.info(
        "panos_reconcile_ok",
        firewall=str(fw.id),
        version=summary.sw_version,
        objects=summary.object_count,
        nat=summary.nat_rule_count,
        objects_created=summary.objects_created,
        nat_created=summary.nat_created,
    )
    return summary


__all__ = ["ReconcileSummary", "reconcile_firewall"]
