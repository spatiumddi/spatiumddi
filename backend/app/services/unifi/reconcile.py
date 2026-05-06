"""Per-controller UniFi reconciler.

For one ``UnifiController`` row:

  1. Decrypt credentials and open a ``UnifiClient`` (sets up the
     httpx session, performs legacy login if ``auth_kind=user_password``).
  2. Probe ``info`` for the application version.
  3. List sites; filter by ``site_allowlist``.
  4. Per site (subject to ``mirror_*`` toggles):
        * ``rest/networkconf`` → desired ``Subnet`` rows (one per
          IPAM-relevant ``purpose``) with gateway from ``ip_subnet``.
        * ``stat/sta`` (active) → desired ``IPAddress`` rows keyed
          on the live ``ip``.
        * ``rest/user`` (known) → desired ``IPAddress`` rows for
          DHCP fixed-IP reservations (``status='reserved'``).
  5. Compute the desired-state set, load existing
     ``unifi_controller_id``-tagged rows, apply the diff.
  6. Persist ``last_synced_at`` / ``last_sync_error`` /
     ``controller_version`` / counts / ``last_discovery``,
     append an audit row, commit.

Diff semantics mirror the Proxmox + Docker reconcilers:
``user_modified_at`` rows are preserved (only ``subnet_id`` and the
unifi FK are updated; soft fields are never clobbered). Rows that
the operator has touched stay even when the underlying device
stops appearing in UniFi — the FK is released so the row is no
longer "owned" by the integration.
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
from app.models.ipam import IPAddress, IPBlock, Subnet
from app.models.unifi import UnifiController
from app.services.unifi.client import (
    UnifiClient,
    UnifiClientConfig,
    UnifiClientError,
    _ip_subnet_to_cidr,
    _ip_subnet_to_gateway,
    _is_ipam_relevant_purpose,
    _UnifiNetwork,
    _UnifiSite,
)
from app.services.unifi.client import (
    _UnifiClient as _UnifiClientRow,
)

logger = structlog.get_logger(__name__)

# Same private-supernet roster as the other integration reconcilers.
# When a UniFi network's CIDR has no enclosing operator block, we
# auto-create the supernet (10.0.0.0/8 / 172.16.0.0/12 / 192.168.0.0/16)
# as an unowned top-level block to keep the IPAM tree consistent.
_PRIVATE_SUPERNETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
]
_BIGINT_MAX = 2**63 - 1


# ── Desired-state dataclasses ────────────────────────────────────────


@dataclass(frozen=True)
class _DesiredSubnet:
    network: str
    name: str
    description: str
    gateway: str | None
    site_name: str
    network_id: str  # UniFi network UUID, used to resolve client → subnet


@dataclass(frozen=True)
class _DesiredAddress:
    address: str
    status: str  # "unifi-client" | "reserved"
    hostname: str
    description: str
    mac: str | None
    network_id: str | None  # to find the matching subnet on insert


@dataclass
class ReconcileSummary:
    ok: bool
    error: str | None = None
    controller_version: str | None = None
    site_count: int = 0
    network_count: int = 0
    client_count: int = 0
    blocks_created: int = 0
    blocks_deleted: int = 0
    subnets_created: int = 0
    subnets_updated: int = 0
    subnets_deleted: int = 0
    addresses_created: int = 0
    addresses_updated: int = 0
    addresses_deleted: int = 0
    skipped_no_subnet: int = 0
    sites_skipped: int = 0
    warnings: list[str] = field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────


def _parse_net(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(value, strict=False)
    except (ValueError, TypeError):
        return None


def _private_supernet_of(cidr: str) -> str | None:
    net = _parse_net(cidr)
    if net is None or not isinstance(net, ipaddress.IPv4Network):
        return None
    for parent in _PRIVATE_SUPERNETS:
        if net.subnet_of(parent) and net.prefixlen > parent.prefixlen:  # type: ignore[arg-type]
            return str(parent)
    return None


def _lan_total_ips(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> int:
    if isinstance(net, ipaddress.IPv6Network):
        return min(net.num_addresses, _BIGINT_MAX)
    if net.prefixlen >= 31:
        return net.num_addresses
    return net.num_addresses - 2


def _site_allowed(site: _UnifiSite, allowlist: list) -> bool:
    """``allowlist`` is the JSONB column from the row — empty list
    means all sites are allowed. Match on either short ``name`` or
    human ``desc`` so operators can write whichever is friendlier.
    """
    if not allowlist:
        return True
    return site.name in allowlist or site.desc in allowlist


def _network_allowed(
    network: _UnifiNetwork,
    site: _UnifiSite,
    allowlist: dict,
) -> bool:
    """Per-site VLAN allowlist. ``{"<site>": [10, 20]}`` means
    only VLAN tags 10 + 20 on that site mirror; missing sites
    default to "all networks allowed".
    """
    if not allowlist:
        return True
    by_name = allowlist.get(site.name) or allowlist.get(site.desc)
    if by_name is None:
        return True
    if not isinstance(by_name, list):
        return True
    if network.vlan is None:
        return 0 in by_name  # untagged
    return network.vlan in by_name


def _client_allowed(
    client: _UnifiClientRow,
    *,
    include_wired: bool,
    include_wireless: bool,
    include_vpn: bool,
) -> bool:
    if client.is_vpn and not include_vpn:
        return False
    # If the row is explicitly wired, gate on include_wired; otherwise
    # treat it as wireless. UniFi's flag is reliable for connected
    # clients; for known-but-offline DHCP reservations it can be
    # missing — we mirror those regardless because they're operator
    # config, not a transient device state.
    if client.fixed_ip:
        return True
    if client.is_wired:
        return include_wired
    return include_wireless


def _find_enclosing_operator_block(
    blocks: list[IPBlock], cidr: str, controller_id: Any
) -> IPBlock | None:
    """Find the deepest non-controller-owned block that encloses
    ``cidr``. Returns ``None`` when there's no operator block at all
    (caller will create a controller-owned wrapper).
    """
    net = _parse_net(cidr)
    if net is None:
        return None
    best: IPBlock | None = None
    best_prefix = -1
    for b in blocks:
        if b.unifi_controller_id == controller_id:
            continue
        bnet = _parse_net(str(b.network))
        if bnet is None or type(bnet) is not type(net):
            continue
        if net.subnet_of(bnet) and bnet.prefixlen > best_prefix:  # type: ignore[arg-type]
            best = b
            best_prefix = bnet.prefixlen
    return best


def _find_subnet_for_ip(subnets: list[Subnet], ip: str) -> Subnet | None:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    best: Subnet | None = None
    best_prefix: int = -1
    for s in subnets:
        net = _parse_net(str(s.network))
        if net is None:
            continue
        if addr in net and net.prefixlen > best_prefix:
            best = s
            best_prefix = net.prefixlen
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


# ── Desired state computation ────────────────────────────────────────


def _compute_desired(
    controller: UnifiController,
    site_to_networks: dict[_UnifiSite, list[_UnifiNetwork]],
    site_to_clients: dict[_UnifiSite, list[_UnifiClientRow]],
) -> tuple[list[_DesiredSubnet], list[_DesiredAddress]]:
    subnets: list[_DesiredSubnet] = []
    seen_networks: set[str] = set()

    # ── Subnets — one per (site, network) with a parseable ``ip_subnet``.
    # Skip networks with non-IPAM-relevant purpose (wan / VPN unless we
    # explicitly want them) and disabled ones.
    for site, networks in site_to_networks.items():
        for n in networks:
            if not n.enabled:
                continue
            if not _is_ipam_relevant_purpose(n.purpose):
                continue
            cidr = _ip_subnet_to_cidr(n.ip_subnet)
            if cidr is None:
                continue
            if cidr in seen_networks:
                # Same CIDR on two sites — keep the first one. Mirroring
                # the same network twice would cause the second pass to
                # delete the first's rows; operators with this layout
                # should narrow ``site_allowlist`` until they unify the
                # IP plan.
                continue
            seen_networks.add(cidr)
            label_bits = [n.name or n.network_id]
            if n.vlan:
                label_bits.append(f"vlan {n.vlan}")
            label = " ".join(label_bits)
            desc_bits = [f"UniFi network {n.name or n.network_id} on site {site.desc or site.name}"]
            if n.purpose:
                desc_bits.append(f"purpose {n.purpose}")
            if n.dhcpd_enabled and n.dhcpd_start and n.dhcpd_stop:
                desc_bits.append(f"DHCP {n.dhcpd_start}–{n.dhcpd_stop}")
            subnets.append(
                _DesiredSubnet(
                    network=cidr,
                    name=label,
                    description=" — ".join(desc_bits),
                    gateway=_ip_subnet_to_gateway(n.ip_subnet),
                    site_name=site.name,
                    network_id=n.network_id,
                )
            )

    # ── Addresses
    addresses: list[_DesiredAddress] = []
    seen_addrs: set[str] = set()
    if not controller.mirror_clients and not controller.mirror_fixed_ips:
        return subnets, addresses

    for site, clients in site_to_clients.items():
        for c in clients:
            if not _client_allowed(
                c,
                include_wired=controller.include_wired,
                include_wireless=controller.include_wireless,
                include_vpn=controller.include_vpn,
            ):
                continue
            if not c.ip:
                continue
            if c.ip in seen_addrs:
                continue
            # Distinguish fixed (DHCP reservation) from active.
            if c.fixed_ip:
                if not controller.mirror_fixed_ips:
                    continue
                status = "reserved"
            else:
                if not controller.mirror_clients:
                    continue
                status = "unifi-client"
            seen_addrs.add(c.ip)
            hostname = c.name or c.hostname or ""
            desc_bits = [f"UniFi client on site {site.desc or site.name}"]
            if c.is_wired:
                desc_bits.append("wired")
            elif c.is_vpn:
                desc_bits.append("vpn")
            else:
                desc_bits.append("wireless")
            if c.is_guest:
                desc_bits.append("guest")
            if c.oui:
                desc_bits.append(f"oui {c.oui}")
            addresses.append(
                _DesiredAddress(
                    address=c.ip,
                    status=status,
                    hostname=hostname,
                    description=" — ".join(desc_bits),
                    mac=c.mac,
                    network_id=c.network_id,
                )
            )

    return subnets, addresses


# ── Apply: blocks + subnets ──────────────────────────────────────────


async def _apply_blocks_and_subnets(
    db: AsyncSession,
    controller: UnifiController,
    desired_subnets: list[_DesiredSubnet],
    summary: ReconcileSummary,
) -> None:
    block_rows = (
        (await db.execute(select(IPBlock).where(IPBlock.space_id == controller.ipam_space_id)))
        .scalars()
        .all()
    )
    blocks = list(block_rows)
    controller_owned_wrappers = {
        str(b.network): b for b in blocks if b.unifi_controller_id == controller.id
    }
    existing_networks = {str(b.network): b for b in blocks}

    for d in desired_subnets:
        supernet = _private_supernet_of(d.network)
        if supernet is None or supernet in existing_networks:
            continue
        parent = IPBlock(
            space_id=controller.ipam_space_id,
            network=supernet,
            name=f"Private {supernet}",
            description=f"Auto-created as the private-address parent for {d.network}",
        )
        db.add(parent)
        await db.flush()
        blocks.append(parent)
        existing_networks[supernet] = parent
        summary.blocks_created += 1

    res = await db.execute(select(Subnet).where(Subnet.unifi_controller_id == controller.id))
    current_subnets = {str(s.network): s for s in res.scalars().all()}

    desired_map = {d.network: d for d in desired_subnets}
    used_wrapper_cidrs: set[str] = set()

    for net_str, row in current_subnets.items():
        if net_str in desired_map:
            continue
        await db.delete(row)
        summary.subnets_deleted += 1

    for net_str, d in desired_map.items():
        parent_block = _find_enclosing_operator_block(blocks, d.network, controller.id)

        if parent_block is None:
            wrapper = controller_owned_wrappers.get(net_str)
            if wrapper is None:
                wrapper = IPBlock(
                    space_id=controller.ipam_space_id,
                    network=d.network,
                    name=f"{controller.name} {d.network}",
                    description=f"Auto-created for UniFi controller {controller.name}",
                    unifi_controller_id=controller.id,
                )
                db.add(wrapper)
                await db.flush()
                blocks.append(wrapper)
                controller_owned_wrappers[net_str] = wrapper
                summary.blocks_created += 1
            used_wrapper_cidrs.add(net_str)
            parent_block = wrapper

        net_parsed = _parse_net(d.network)
        expected_total = _lan_total_ips(net_parsed) if net_parsed is not None else 0

        existing = current_subnets.get(net_str)
        if existing is None:
            db.add(
                Subnet(
                    space_id=controller.ipam_space_id,
                    block_id=parent_block.id,
                    network=d.network,
                    name=d.name,
                    description=d.description,
                    gateway=d.gateway,
                    unifi_controller_id=controller.id,
                    total_ips=expected_total,
                )
            )
            summary.subnets_created += 1
        else:
            changed = False
            if existing.block_id != parent_block.id:
                existing.block_id = parent_block.id
                changed = True
            if existing.name != d.name:
                existing.name = d.name
                changed = True
            if existing.description != d.description:
                existing.description = d.description
                changed = True
            if d.gateway and existing.gateway != d.gateway:
                existing.gateway = d.gateway
                changed = True
            if existing.total_ips != expected_total:
                existing.total_ips = expected_total
                changed = True
            if changed:
                summary.subnets_updated += 1

    await db.flush()

    for net_str, wrapper in controller_owned_wrappers.items():
        if net_str in used_wrapper_cidrs:
            continue
        refs = await db.execute(select(Subnet).where(Subnet.block_id == wrapper.id))
        if refs.scalar_one_or_none() is None:
            await db.delete(wrapper)
            summary.blocks_deleted += 1


# ── Apply: addresses ──────────────────────────────────────────────────


async def _apply_addresses(
    db: AsyncSession,
    controller: UnifiController,
    desired: list[_DesiredAddress],
    summary: ReconcileSummary,
) -> None:
    subnet_rows = (
        (await db.execute(select(Subnet).where(Subnet.space_id == controller.ipam_space_id)))
        .scalars()
        .all()
    )
    subnets = list(subnet_rows)

    desired_map: dict[str, _DesiredAddress] = {}
    for d in desired:
        desired_map.setdefault(d.address, d)

    desired_addrs = list(desired_map.keys())
    if desired_addrs:
        existing = (
            (await db.execute(select(IPAddress).where(IPAddress.address.in_(desired_addrs))))
            .scalars()
            .all()
        )
        for row in existing:
            if row.unifi_controller_id == controller.id:
                continue
            if row.unifi_controller_id is not None:
                summary.warnings.append(
                    f"address {row.address} owned by another UniFi controller; not claiming"
                )
                continue
            if (
                row.kubernetes_cluster_id is not None
                or row.docker_host_id is not None
                or row.proxmox_node_id is not None
                or row.tailscale_tenant_id is not None
            ):
                summary.warnings.append(
                    f"address {row.address} owned by another integration; not claiming"
                )
                continue
            row.unifi_controller_id = controller.id
            if row.user_modified_at is None:
                row.user_modified_at = datetime.now(UTC)
        await db.flush()

    res = await db.execute(select(IPAddress).where(IPAddress.unifi_controller_id == controller.id))
    current = {str(a.address): a for a in res.scalars().all()}

    dirty_subnets: set[Any] = {s.id for s in subnets if s.unifi_controller_id == controller.id}

    for addr, row in current.items():
        if addr not in desired_map:
            dirty_subnets.add(row.subnet_id)
            if row.user_modified_at is not None:
                row.unifi_controller_id = None
                summary.addresses_updated += 1
            else:
                await db.delete(row)
                summary.addresses_deleted += 1

    for addr, d in desired_map.items():
        subnet = _find_subnet_for_ip(subnets, d.address)
        if subnet is None:
            summary.skipped_no_subnet += 1
            continue
        if addr in current:
            row = current[addr]
            changed = False
            if row.subnet_id != subnet.id:
                dirty_subnets.add(row.subnet_id)
                row.subnet_id = subnet.id
                changed = True
            if row.user_modified_at is None:
                if row.status != d.status:
                    row.status = d.status
                    changed = True
                if (row.hostname or "") != d.hostname:
                    row.hostname = d.hostname
                    changed = True
                if (row.description or "") != d.description:
                    row.description = d.description
                    changed = True
                if d.mac and (row.mac_address or "") != d.mac.lower():
                    row.mac_address = d.mac.lower()
                    changed = True
            if changed:
                dirty_subnets.add(subnet.id)
                summary.addresses_updated += 1
        else:
            db.add(
                IPAddress(
                    subnet_id=subnet.id,
                    address=d.address,
                    status=d.status,
                    hostname=d.hostname,
                    description=d.description,
                    mac_address=d.mac.lower() if d.mac else None,
                    unifi_controller_id=controller.id,
                )
            )
            dirty_subnets.add(subnet.id)
            summary.addresses_created += 1

    if dirty_subnets:
        await db.flush()
        for subnet_id in dirty_subnets:
            await _recompute_subnet_utilization(db, subnet_id)


# ── Discovery payload ───────────────────────────────────────────────


def _build_discovery_payload(
    site_to_networks: dict[_UnifiSite, list[_UnifiNetwork]],
    site_to_clients: dict[_UnifiSite, list[_UnifiClientRow]],
    desired_subnets: list[_DesiredSubnet],
    desired_addresses: list[_DesiredAddress],
) -> dict[str, Any]:
    """Per-site rollup the admin "Discovery" modal renders.

    Shape:

    ``{
      "summary": {
        "site_total": 2,
        "network_total": 12,
        "network_mirrored": 8,
        "client_total": 134,
        "client_mirrored": 126,
        "addresses_skipped_no_subnet": 0,
      },
      "sites": [
        {"name": "default", "desc": "Default", "networks": 6, "mirrored": 6, "clients": 100},
        ...
      ],
    }``
    """
    desired_subnet_set = {(d.network_id) for d in desired_subnets}
    desired_addr_set = {d.address for d in desired_addresses}

    sites: list[dict[str, Any]] = []
    network_total = 0
    network_mirrored = 0
    client_total = 0
    client_mirrored = 0

    for site, nets in site_to_networks.items():
        clients = site_to_clients.get(site, [])
        site_mirrored = sum(1 for n in nets if n.network_id in desired_subnet_set)
        site_clients_mirrored = sum(1 for c in clients if c.ip and c.ip in desired_addr_set)
        sites.append(
            {
                "name": site.name,
                "desc": site.desc,
                "networks": len(nets),
                "mirrored": site_mirrored,
                "clients": len(clients),
                "clients_mirrored": site_clients_mirrored,
            }
        )
        network_total += len(nets)
        network_mirrored += site_mirrored
        client_total += len(clients)
        client_mirrored += site_clients_mirrored

    sites.sort(key=lambda s: s["name"])

    skipped = sum(
        1
        for d in desired_addresses
        if not any(_addr_in_subnet(d.address, s.network) for s in desired_subnets)
    )

    return {
        "summary": {
            "site_total": len(site_to_networks),
            "network_total": network_total,
            "network_mirrored": network_mirrored,
            "client_total": client_total,
            "client_mirrored": client_mirrored,
            "addresses_skipped_no_subnet": skipped,
        },
        "sites": sites,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _addr_in_subnet(addr: str, cidr: str) -> bool:
    try:
        return ipaddress.ip_address(addr) in ipaddress.ip_network(cidr, strict=False)
    except (ValueError, TypeError):
        return False


# ── Entry point ──────────────────────────────────────────────────────


def _decrypt_or_empty(blob: bytes) -> str:
    if not blob:
        return ""
    try:
        return decrypt_str(blob)
    except ValueError as exc:  # noqa: BLE001 — surface the message
        raise ValueError(f"decrypt failed: {exc}") from exc


async def reconcile_controller(db: AsyncSession, controller: UnifiController) -> ReconcileSummary:
    summary = ReconcileSummary(ok=False)

    try:
        api_key = _decrypt_or_empty(controller.api_key_encrypted)
        username = _decrypt_or_empty(controller.username_encrypted)
        password = _decrypt_or_empty(controller.password_encrypted)
    except ValueError as exc:
        summary.error = str(exc)
        controller.last_sync_error = summary.error
        controller.last_synced_at = datetime.now(UTC)
        await db.commit()
        return summary

    cfg = UnifiClientConfig(
        mode=controller.mode,
        host=controller.host,
        port=controller.port,
        cloud_host_id=controller.cloud_host_id,
        verify_tls=controller.verify_tls,
        ca_bundle_pem=controller.ca_bundle_pem or "",
        auth_kind=controller.auth_kind,
        api_key=api_key,
        username=username,
        password=password,
    )

    site_to_networks: dict[_UnifiSite, list[_UnifiNetwork]] = {}
    site_to_clients: dict[_UnifiSite, list[_UnifiClientRow]] = {}
    version_str = ""

    try:
        async with UnifiClient(cfg) as client:
            version = await client.get_version()
            version_str = version.version
            sites = await client.list_sites()
            allowlist = list(controller.site_allowlist or [])
            net_allow = dict(controller.network_allowlist or {})

            for site in sites:
                if not _site_allowed(site, allowlist):
                    summary.sites_skipped += 1
                    continue
                if controller.mirror_networks:
                    try:
                        nets = await client.list_networks(site.name)
                    except UnifiClientError as exc:
                        summary.warnings.append(f"site {site.name}: list_networks failed — {exc}")
                        nets = []
                    nets = [n for n in nets if _network_allowed(n, site, net_allow)]
                    site_to_networks[site] = nets
                else:
                    site_to_networks[site] = []

                clients_combined: list[_UnifiClientRow] = []
                if controller.mirror_clients:
                    try:
                        clients_combined.extend(await client.list_active_clients(site.name))
                    except UnifiClientError as exc:
                        summary.warnings.append(
                            f"site {site.name}: list_active_clients failed — {exc}"
                        )
                if controller.mirror_fixed_ips:
                    try:
                        for known in await client.list_known_clients(site.name):
                            if known.fixed_ip and known.ip:
                                clients_combined.append(known)
                    except UnifiClientError as exc:
                        summary.warnings.append(
                            f"site {site.name}: list_known_clients failed — {exc}"
                        )
                site_to_clients[site] = clients_combined
    except UnifiClientError as exc:
        summary.error = str(exc)
        controller.last_sync_error = summary.error
        controller.last_synced_at = datetime.now(UTC)
        await db.commit()
        logger.warning(
            "unifi_reconcile_fetch_failed", controller=str(controller.id), error=summary.error
        )
        return summary

    summary.controller_version = version_str
    summary.site_count = len(site_to_networks)
    summary.network_count = sum(len(v) for v in site_to_networks.values())
    summary.client_count = sum(len(v) for v in site_to_clients.values())

    desired_subnets, desired_addresses = _compute_desired(
        controller, site_to_networks, site_to_clients
    )

    await _apply_blocks_and_subnets(db, controller, desired_subnets, summary)
    await _apply_addresses(db, controller, desired_addresses, summary)

    controller.last_synced_at = datetime.now(UTC)
    controller.last_sync_error = None
    controller.controller_version = summary.controller_version
    controller.site_count = summary.site_count
    controller.network_count = summary.network_count
    controller.client_count = summary.client_count
    controller.last_discovery = _build_discovery_payload(
        site_to_networks, site_to_clients, desired_subnets, desired_addresses
    )

    db.add(
        AuditLog(
            user_display_name="system",
            auth_source="system",
            action="unifi.reconcile",
            resource_type="unifi_controller",
            resource_id=str(controller.id),
            resource_display=controller.name,
            new_value={
                "blocks": {
                    "created": summary.blocks_created,
                    "deleted": summary.blocks_deleted,
                },
                "subnets": {
                    "created": summary.subnets_created,
                    "updated": summary.subnets_updated,
                    "deleted": summary.subnets_deleted,
                },
                "addresses": {
                    "created": summary.addresses_created,
                    "updated": summary.addresses_updated,
                    "deleted": summary.addresses_deleted,
                    "skipped_no_subnet": summary.skipped_no_subnet,
                },
                "sites_skipped": summary.sites_skipped,
            },
        )
    )
    await db.commit()
    summary.ok = True
    logger.info(
        "unifi_reconcile_ok",
        controller=str(controller.id),
        version=summary.controller_version,
        sites=summary.site_count,
        networks=summary.network_count,
        clients=summary.client_count,
        subnets_created=summary.subnets_created,
        addresses_created=summary.addresses_created,
        addresses_deleted=summary.addresses_deleted,
    )
    return summary


__all__ = ["ReconcileSummary", "reconcile_controller"]
