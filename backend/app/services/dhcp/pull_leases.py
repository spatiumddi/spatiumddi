"""Pull leases from a DHCP server and reconcile into SpatiumDDI's DB.

Today this path is exercised by the Windows DHCP read-only driver (Path
A — WinRM + PowerShell), but the shape is driver-agnostic: any driver
that implements ``get_leases`` can plug in.

**Semantics — set-reconcile (upsert + absence-delete):**

 * Upsert one ``DHCPLease`` row per ``(server_id, ip_address)`` seen on
   the wire. New rows are marked ``state="active"``; existing active
   rows have ``expires_at`` / ``hostname`` / ``mac_address`` refreshed
   and ``last_seen_at`` bumped to ``now()``.
 * Mirror each active lease into IPAM as an ``IPAddress`` row with
   ``status="dhcp"`` and ``auto_from_lease=True`` — but only if the
   lease IP falls within a known ``Subnet.network``. IPs outside any
   managed subnet are tracked as leases but not mirrored.
 * **Any active lease we previously tracked for this server that did
   NOT appear in the wire response is gone from the DHCP server** —
   an admin deleted it, or it was released + cleaned up on the server
   before we polled. Delete the ``DHCPLease`` row and, if we created
   the IPAM mirror (``auto_from_lease=True``), drop that too. The
   driver's ``get_leases`` is the ground truth; absence means deleted.

The time-based ``dhcp_lease_cleanup`` sweep continues to handle leases
that drift past ``expires_at`` without being polled (e.g., between
polls, or when lease pull is disabled). The two mechanisms overlap
harmlessly: expiry sweeps anything the pull missed, pull deletes
anything the sweep hasn't seen yet.

Per CLAUDE.md non-negotiable #9, the whole operation is idempotent: a
second run over the same wire state is a no-op (the dedup key is
``(server_id, ip_address)`` and all updates are set-to-observed).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.dhcp import get_driver, is_agentless
from app.models.dhcp import (
    DHCPLease,
    DHCPPool,
    DHCPScope,
    DHCPServer,
    DHCPStaticAssignment,
)
from app.models.ipam import IPAddress, Subnet
from app.services.dhcp.lease_history import record_lease_history

logger = structlog.get_logger(__name__)


@dataclass
class PullLeasesResult:
    server_leases: int = 0  # count returned by the driver
    imported: int = 0  # new DHCPLease rows inserted
    refreshed: int = 0  # existing DHCPLease rows updated in place
    removed: int = 0  # DHCPLease rows dropped because they vanished from the wire
    ipam_created: int = 0  # new IPAddress rows mirrored
    ipam_refreshed: int = 0  # existing auto_from_lease rows bumped
    ipam_revoked: int = 0  # auto_from_lease IPAddress rows deleted alongside removed leases
    out_of_scope: int = 0  # leases whose IP isn't in any subnet
    # Topology counters (populated when the driver supports get_scopes).
    scopes_imported: int = 0  # new DHCPScope rows added
    scopes_refreshed: int = 0  # existing DHCPScope rows updated in place
    scopes_skipped_no_subnet: int = 0  # scope CIDR not tracked in IPAM — skipped
    pools_synced: int = 0  # DHCPPool rows (re-)created from the import
    statics_synced: int = 0  # DHCPStaticAssignment rows (re-)created
    errors: list[str] = field(default_factory=list)


async def pull_leases_from_server(
    db: AsyncSession,
    server: DHCPServer,
    *,
    apply: bool = True,
) -> PullLeasesResult:
    """Poll ``server`` for active leases and reconcile into the DB.

    ``apply=False`` returns the counts without writing — useful for
    dry-run previews from the UI.

    Only drivers registered as agentless participate; agent-based
    drivers (kea) already stream lease events over the agent channel
    and would double-count.
    """
    result = PullLeasesResult()

    if not is_agentless(server.driver):
        result.errors.append(
            f"driver {server.driver!r} is agent-based; lease pull is not applicable"
        )
        return result

    try:
        driver = get_driver(server.driver)
    except ValueError as exc:
        result.errors.append(str(exc))
        return result

    subnets = await _load_subnet_cache(db)

    # Phase 1 — topology (scopes + pools + reservations). Optional: only
    # runs for drivers that expose ``get_scopes``. For each scope whose
    # CIDR matches a known IPAM subnet, upsert the scope, then replace
    # its pools and statics with what Windows reports. Scopes whose CIDR
    # has no matching IPAM subnet are skipped — we intentionally do not
    # auto-create subnets (that belongs in a separate workflow).
    if hasattr(driver, "get_scopes"):
        try:
            wire_scopes = await driver.get_scopes(server)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"get_scopes failed: {exc}")
            logger.warning(
                "dhcp_pull_scopes_driver_failed",
                server=str(server.id),
                driver=server.driver,
                error=str(exc),
            )
            wire_scopes = []
        for wscope in wire_scopes:
            await _upsert_scope(db, server, wscope, subnets, result, apply=apply)

    # Phase 2 — leases.
    try:
        wire = await driver.get_leases(server)
    except Exception as exc:  # noqa: BLE001 — surface any transport/PS error
        result.errors.append(f"get_leases failed: {exc}")
        logger.warning(
            "dhcp_pull_leases_driver_failed",
            server=str(server.id),
            driver=server.driver,
            error=str(exc),
        )
        return result

    result.server_leases = len(wire)

    scope_cache = await _load_scope_cache(db, server.server_group_id)

    now = datetime.now(UTC)

    for lease in wire:
        ip = lease.get("ip_address")
        mac = lease.get("mac_address")
        if not ip or not mac:
            continue

        containing = _find_containing_subnet(ip, subnets)
        scope_id = scope_cache.get(containing.id) if containing else None

        existing = (
            await db.execute(
                select(DHCPLease).where(
                    DHCPLease.server_id == server.id,
                    DHCPLease.ip_address == ip,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            if apply:
                db.add(
                    DHCPLease(
                        server_id=server.id,
                        scope_id=scope_id,
                        ip_address=ip,
                        mac_address=mac,
                        hostname=lease.get("hostname"),
                        client_id=lease.get("client_id"),
                        state="active",
                        expires_at=lease.get("expires_at"),
                        last_seen_at=now,
                    )
                )
            result.imported += 1
        else:
            if apply:
                # Detect MAC supersede — same IP, new MAC. Stamp a history
                # row recording the OLD MAC's tenancy on this IP before
                # we overwrite it. Comparison is case-insensitive
                # because postgres MACADDR canonicalises to lower-case
                # but the wire format from the driver may not.
                old_mac = str(existing.mac_address) if existing.mac_address else None
                if old_mac and mac and old_mac.lower() != str(mac).lower():
                    record_lease_history(
                        db,
                        existing,
                        lease_state="superseded",
                        expired_at=now,
                        mac_override=old_mac,
                    )
                existing.mac_address = mac
                existing.hostname = lease.get("hostname") or existing.hostname
                existing.client_id = lease.get("client_id") or existing.client_id
                existing.state = "active"
                existing.expires_at = lease.get("expires_at") or existing.expires_at
                existing.last_seen_at = now
                if scope_id is not None and existing.scope_id != scope_id:
                    existing.scope_id = scope_id
            result.refreshed += 1

        if containing is None:
            result.out_of_scope += 1
            continue

        ipam_row = (
            await db.execute(
                select(IPAddress).where(
                    IPAddress.subnet_id == containing.id,
                    IPAddress.address == ip,
                )
            )
        ).scalar_one_or_none()

        if ipam_row is None:
            if apply:
                ipam_row = IPAddress(
                    subnet_id=containing.id,
                    address=ip,
                    status="dhcp",
                    hostname=lease.get("hostname"),
                    mac_address=mac,
                    last_seen_at=now,
                    last_seen_method="dhcp",
                    auto_from_lease=True,
                )
                db.add(ipam_row)
                await db.flush()  # assign PK so _sync_dns_record can reference it
            result.ipam_created += 1
        elif ipam_row.auto_from_lease:
            # Only refresh rows we own. Manually-allocated rows are left
            # alone — the lease + IPAM coexist.
            if apply:
                ipam_row.mac_address = mac
                if lease.get("hostname"):
                    ipam_row.hostname = lease.get("hostname")
                ipam_row.last_seen_at = now
                ipam_row.last_seen_method = "dhcp"
            result.ipam_refreshed += 1
        else:
            # Manual allocation — skip DDNS entirely. Whatever hostname
            # the operator set stays put.
            continue

        # Fire DDNS off the freshly-mirrored row. Gate-keeping lives
        # inside the service (subnet.ddns_enabled, policy, static
        # override, idempotency); we just pass through and let it
        # decide. Any exception is logged but doesn't break the
        # lease-pull pass — DNS will reconcile next tick either way.
        if apply and ipam_row is not None:
            try:
                from app.services.dns.ddns import apply_ddns_for_lease  # noqa: PLC0415

                await apply_ddns_for_lease(
                    db,
                    subnet=containing,
                    ipam_row=ipam_row,
                    client_hostname=lease.get("hostname"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "dhcp_pull_leases_ddns_failed",
                    server=str(server.id),
                    ip=ip,
                    error=str(exc),
                )

    # Absence-delete: any active lease we have for this server that did
    # NOT appear in the wire response was removed on the DHCP server
    # (admin purge, manual release, etc). Drop the DB row and any IPAM
    # mirror we created. Manually-allocated IPAM rows (auto_from_lease
    # False) are intentionally left alone — the operator owns those.
    wire_ips = {lease.get("ip_address") for lease in wire if lease.get("ip_address")}
    stale_q = select(DHCPLease).where(
        DHCPLease.server_id == server.id,
        DHCPLease.state == "active",
    )
    if wire_ips:
        stale_q = stale_q.where(~DHCPLease.ip_address.in_(wire_ips))
    stale_leases = list((await db.execute(stale_q)).scalars().all())
    for stale in stale_leases:
        mirror = (
            await db.execute(
                select(IPAddress).where(
                    IPAddress.address == stale.ip_address,
                    IPAddress.auto_from_lease.is_(True),
                )
            )
        ).scalar_one_or_none()
        if mirror is not None:
            if apply:
                await db.delete(mirror)
            result.ipam_revoked += 1
        if apply:
            # Stamp history before the lease row goes away. ``removed``
            # signals "operator/server purged the lease before we polled"
            # vs ``expired`` which is the time-based sweep state.
            record_lease_history(db, stale, lease_state="removed", expired_at=now)
            await db.delete(stale)
        result.removed += 1

    if apply:
        server.last_sync_at = now
        await db.flush()

    return result


# ── helpers ───────────────────────────────────────────────────────────


async def _load_subnet_cache(db: AsyncSession) -> list[tuple[Subnet, ipaddress._BaseNetwork]]:
    """Return ``[(subnet, network)]`` once per call — containment checks
    run in Python to keep the path driver-agnostic and avoid N+1 SQL.
    Cheap at any realistic subnet count.
    """
    res = await db.execute(select(Subnet))
    out: list[tuple[Subnet, ipaddress._BaseNetwork]] = []
    for s in res.scalars().all():
        try:
            net = ipaddress.ip_network(str(s.network), strict=False)
        except (ValueError, TypeError):
            continue
        out.append((s, net))
    return out


async def _load_scope_cache(db: AsyncSession, group_id: Any) -> dict[Any, Any]:
    """Map ``subnet_id -> scope_id`` for active scopes served by this
    server's group. Windows leases have no scope backlink until we
    resolve through the IPAM subnet — this lookup wires the
    ``DHCPLease.scope_id`` FK when the subnet has a scope in this group,
    and leaves it NULL otherwise.
    """
    if group_id is None:
        return {}
    res = await db.execute(
        select(DHCPScope.subnet_id, DHCPScope.id).where(
            DHCPScope.group_id == group_id,
            DHCPScope.is_active.is_(True),
        )
    )
    return {subnet_id: scope_id for subnet_id, scope_id in res.all()}


def _find_containing_subnet(
    ip: str, subnets: list[tuple[Subnet, ipaddress._BaseNetwork]]
) -> Subnet | None:
    try:
        addr = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return None
    # Longest-prefix wins if multiple subnets nest (shouldn't in IPAM,
    # but defensively).
    best: tuple[int, Subnet] | None = None
    for subnet, net in subnets:
        if addr in net:
            if best is None or net.prefixlen > best[0]:
                best = (net.prefixlen, subnet)
    return best[1] if best else None


async def _upsert_scope(
    db: AsyncSession,
    server: DHCPServer,
    wscope: dict[str, Any],
    subnets: list[tuple[Subnet, ipaddress._BaseNetwork]],
    result: PullLeasesResult,
    *,
    apply: bool,
) -> None:
    """Upsert one Windows-reported scope + its pools + its reservations.

    Matching: the scope's ``subnet_cidr`` must exactly match an existing
    IPAM ``Subnet.network`` (prefix-length identical). No auto-create.

    For pools + statics we do a **replace-all** per scope: delete
    everything for this scope_id, then insert what Windows reports.
    That's safe because windows_dhcp is read-only — there are no manual
    pools/statics anyone could have added through our UI for a
    windows_dhcp scope.
    """
    cidr = wscope.get("subnet_cidr")
    if not cidr:
        return
    try:
        target = ipaddress.ip_network(cidr, strict=False)
    except (ValueError, TypeError):
        return

    matching_subnet: Subnet | None = None
    for subnet, net in subnets:
        if net == target:
            matching_subnet = subnet
            break
    if matching_subnet is None:
        result.scopes_skipped_no_subnet += 1
        return

    # Scope is keyed by (group_id, subnet_id) now. If the Windows server
    # has no group, skip — a groupless Windows server can't own scopes
    # in the group-centric model (migration assigns every server a
    # singleton group, but a freshly-created unpartitioned server hits
    # this branch until an operator attaches it to a group).
    if server.server_group_id is None:
        result.scopes_skipped_no_subnet += 1
        return

    # DHCPScope eager-loads ``pools`` and ``statics`` collections, so the
    # result iterator must be uniqued before calling scalar_one_or_none().
    existing_scope = (
        (
            await db.execute(
                select(DHCPScope).where(
                    DHCPScope.group_id == server.server_group_id,
                    DHCPScope.subnet_id == matching_subnet.id,
                )
            )
        )
        .unique()
        .scalar_one_or_none()
    )

    scope_fields = {
        "name": wscope.get("name") or "",
        "description": wscope.get("description") or "",
        "is_active": bool(wscope.get("is_active", True)),
        "lease_time": int(wscope.get("lease_time") or 86400),
        "options": wscope.get("options") or {},
        "address_family": "ipv4",
    }

    if existing_scope is None:
        if apply:
            existing_scope = DHCPScope(
                group_id=server.server_group_id,
                subnet_id=matching_subnet.id,
                **scope_fields,
            )
            db.add(existing_scope)
            await db.flush()
        result.scopes_imported += 1
    else:
        if apply:
            for field_name, value in scope_fields.items():
                setattr(existing_scope, field_name, value)
        result.scopes_refreshed += 1

    if not apply or existing_scope is None:
        return

    # Replace-all: drop old pools + statics for this scope, re-insert.
    # Cheap on scopes with tens of entries; avoids diff-merge complexity.
    await db.execute(DHCPPool.__table__.delete().where(DHCPPool.scope_id == existing_scope.id))
    await db.execute(
        DHCPStaticAssignment.__table__.delete().where(
            DHCPStaticAssignment.scope_id == existing_scope.id
        )
    )

    for pool in wscope.get("pools") or []:
        if not pool.get("start_ip") or not pool.get("end_ip"):
            continue
        db.add(
            DHCPPool(
                scope_id=existing_scope.id,
                start_ip=pool["start_ip"],
                end_ip=pool["end_ip"],
                pool_type=pool.get("pool_type") or "dynamic",
                name="",
            )
        )
        result.pools_synced += 1

    for static in wscope.get("statics") or []:
        if not static.get("ip_address") or not static.get("mac_address"):
            continue
        db.add(
            DHCPStaticAssignment(
                scope_id=existing_scope.id,
                ip_address=static["ip_address"],
                mac_address=static["mac_address"],
                client_id=static.get("client_id"),
                hostname=static.get("hostname") or "",
                description=static.get("description") or "",
            )
        )
        result.statics_synced += 1

    await db.flush()


__all__ = ["PullLeasesResult", "pull_leases_from_server"]
