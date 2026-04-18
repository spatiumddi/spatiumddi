"""Import preview + commit for IPAM resources.

The importer accepts a :class:`ParsedPayload` (see :mod:`.parser`) plus a
target IP space (either by id or name). Subnets are matched to existing
rows by ``(space_id, network)`` — where the network is canonicalised via
``ipaddress.ip_network(..., strict=False)``.

Conflict resolution strategies:

- ``skip``       — ignore rows that already exist; still create new ones
- ``overwrite``  — update existing rows in place
- ``fail``       — raise 409 if any conflict is detected (default)

Parent block detection: for each subnet, if the row did not specify
``block`` / ``block_network``, the importer picks the smallest existing
block in the space whose CIDR contains the subnet. If none is found and
the strategy is not ``fail``, a block matching the subnet's own CIDR is
auto-created as a parent (because subnets require ``block_id``).
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services.ipam_io.parser import ParsedPayload

logger = structlog.get_logger(__name__)

Strategy = Literal["skip", "overwrite", "fail"]


# ── Data classes ───────────────────────────────────────────────────────────────


@dataclass
class DiffRow:
    kind: str  # "subnet" | "block" | "address"
    action: str  # "create" | "update" | "conflict" | "skip"
    network: str
    name: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None


@dataclass
class ImportPreview:
    space_id: str
    space_name: str
    creates: list[DiffRow] = field(default_factory=list)
    updates: list[DiffRow] = field(default_factory=list)
    conflicts: list[DiffRow] = field(default_factory=list)
    errors: list[DiffRow] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "space_id": self.space_id,
            "space_name": self.space_name,
            "summary": {
                "creates": len(self.creates),
                "updates": len(self.updates),
                "conflicts": len(self.conflicts),
                "errors": len(self.errors),
            },
            "creates": [row.__dict__ for row in self.creates],
            "updates": [row.__dict__ for row in self.updates],
            "conflicts": [row.__dict__ for row in self.conflicts],
            "errors": [row.__dict__ for row in self.errors],
        }


@dataclass
class ImportResult:
    space_id: str
    created_subnets: int = 0
    updated_subnets: int = 0
    skipped: int = 0
    auto_created_blocks: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__


# ── Helpers ────────────────────────────────────────────────────────────────────


def _canon_network(value: str) -> str:
    return str(ipaddress.ip_network(value, strict=False))


async def _resolve_space(
    db: AsyncSession,
    space_id: uuid.UUID | None,
    space_name: str | None,
) -> IPSpace:
    if space_id:
        space = await db.get(IPSpace, space_id)
        if space is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target IP space not found",
            )
        return space
    if space_name:
        result = await db.execute(select(IPSpace).where(IPSpace.name == space_name))
        space = result.scalar_one_or_none()
        if space is not None:
            return space
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Import requires a target space_id (or an existing space_name)",
    )


async def _load_existing_blocks(db: AsyncSession, space_id: uuid.UUID) -> list[IPBlock]:
    result = await db.execute(select(IPBlock).where(IPBlock.space_id == space_id))
    return list(result.scalars().all())


async def _load_existing_subnets(db: AsyncSession, space_id: uuid.UUID) -> dict[str, Subnet]:
    result = await db.execute(select(Subnet).where(Subnet.space_id == space_id))
    return {str(s.network): s for s in result.scalars().all()}


def _find_parent_block(
    subnet_net: ipaddress.IPv4Network | ipaddress.IPv6Network,
    blocks: list[IPBlock],
) -> IPBlock | None:
    """Return the smallest (most-specific) existing block that contains the subnet."""
    candidates: list[IPBlock] = []
    for block in blocks:
        try:
            block_net = ipaddress.ip_network(str(block.network), strict=False)
        except ValueError:
            continue
        if subnet_net.version != block_net.version:
            continue
        if subnet_net.subnet_of(block_net):  # type: ignore[arg-type]
            candidates.append(block)
    if not candidates:
        return None
    # Most specific = highest prefixlen
    candidates.sort(
        key=lambda b: ipaddress.ip_network(str(b.network), strict=False).prefixlen,
        reverse=True,
    )
    return candidates[0]


# ── Preview ────────────────────────────────────────────────────────────────────


async def preview_import(
    db: AsyncSession,
    payload: ParsedPayload,
    *,
    space_id: uuid.UUID | None = None,
    space_name: str | None = None,
    strategy: Strategy = "fail",
) -> ImportPreview:
    space = await _resolve_space(db, space_id, space_name)
    preview = ImportPreview(space_id=str(space.id), space_name=space.name)

    existing_subnets = await _load_existing_subnets(db, space.id)
    existing_blocks = await _load_existing_blocks(db, space.id)
    existing_block_nets = {str(b.network) for b in existing_blocks}

    # Track auto-created blocks in this preview so the summary is consistent.
    planned_block_nets: set[str] = set()

    seen_networks: set[str] = set()
    for row in payload.subnets:
        raw_network = row.get("network")
        if not raw_network or not isinstance(raw_network, str):
            preview.errors.append(
                DiffRow(
                    kind="subnet",
                    action="error",
                    network=str(raw_network) if raw_network else "",
                    reason="Missing 'network' field",
                    details=row,
                )
            )
            continue
        try:
            canonical = _canon_network(raw_network.strip())
        except ValueError as exc:
            preview.errors.append(
                DiffRow(
                    kind="subnet",
                    action="error",
                    network=raw_network,
                    reason=f"Invalid CIDR: {exc}",
                    details=row,
                )
            )
            continue
        if canonical in seen_networks:
            preview.errors.append(
                DiffRow(
                    kind="subnet",
                    action="error",
                    network=canonical,
                    reason="Duplicate row in import payload",
                )
            )
            continue
        seen_networks.add(canonical)

        subnet_net = ipaddress.ip_network(canonical, strict=False)
        name = str(row.get("name") or "")

        # Parent block detection
        parent = _find_parent_block(subnet_net, existing_blocks)
        parent_network = str(parent.network) if parent else None
        if parent_network is None and canonical not in planned_block_nets:
            planned_block_nets.add(canonical)
            if canonical not in existing_block_nets:
                preview.creates.append(
                    DiffRow(
                        kind="block",
                        action="create",
                        network=canonical,
                        name=f"auto-parent for {canonical}",
                        reason="No containing block found; auto-parent will be created",
                    )
                )

        existing = existing_subnets.get(canonical)
        if existing is None:
            preview.creates.append(
                DiffRow(
                    kind="subnet",
                    action="create",
                    network=canonical,
                    name=name,
                    details={**row, "network": canonical, "parent_block": parent_network},
                )
            )
            continue

        # Subnet exists — decide based on strategy
        old_snapshot = {
            "name": existing.name,
            "description": existing.description,
            "vlan_id": existing.vlan_id,
            "vxlan_id": existing.vxlan_id,
            "gateway": str(existing.gateway) if existing.gateway else None,
        }
        diff_details = {"old": old_snapshot, "new": {**row, "network": canonical}}
        if strategy == "overwrite":
            preview.updates.append(
                DiffRow(
                    kind="subnet",
                    action="update",
                    network=canonical,
                    name=name or existing.name,
                    details=diff_details,
                )
            )
        elif strategy == "skip":
            preview.conflicts.append(
                DiffRow(
                    kind="subnet",
                    action="skip",
                    network=canonical,
                    name=existing.name,
                    reason="Already exists; strategy=skip",
                    details=diff_details,
                )
            )
        else:  # fail
            preview.conflicts.append(
                DiffRow(
                    kind="subnet",
                    action="conflict",
                    network=canonical,
                    name=existing.name,
                    reason="Already exists; commit will fail unless strategy is set",
                    details=diff_details,
                )
            )

    return preview


# ── Commit ─────────────────────────────────────────────────────────────────────


def _audit_entry(
    user: Any, action: str, resource_id: str, display: str, new_value: dict
) -> AuditLog:
    return AuditLog(
        user_id=user.id,
        user_display_name=user.display_name,
        auth_source=user.auth_source,
        action=action,
        resource_type="subnet",
        resource_id=resource_id,
        resource_display=display,
        new_value=new_value,
        result="success",
    )


async def commit_import(
    db: AsyncSession,
    payload: ParsedPayload,
    *,
    current_user: Any,
    space_id: uuid.UUID | None = None,
    space_name: str | None = None,
    strategy: Strategy = "fail",
) -> ImportResult:
    """Apply the import. All changes happen inside the caller's transaction —
    the caller (the route handler) owns the commit.
    """
    space = await _resolve_space(db, space_id, space_name)
    result_obj = ImportResult(space_id=str(space.id))

    existing_blocks = await _load_existing_blocks(db, space.id)
    existing_subnets = await _load_existing_subnets(db, space.id)

    # Pre-flight: if fail-strategy and any subnet conflicts, bail out early.
    if strategy == "fail":
        conflicts = [
            row.get("network")
            for row in payload.subnets
            if isinstance(row.get("network"), str)
            and _safe_canon(row["network"]) in existing_subnets
        ]
        if conflicts:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{len(conflicts)} subnet(s) already exist; "
                "re-run with strategy='skip' or 'overwrite'",
            )

    for row in payload.subnets:
        raw_network = row.get("network")
        if not isinstance(raw_network, str):
            result_obj.errors.append("Row missing 'network'")
            continue
        try:
            canonical = _canon_network(raw_network.strip())
        except ValueError as exc:
            result_obj.errors.append(f"{raw_network}: {exc}")
            continue

        subnet_net = ipaddress.ip_network(canonical, strict=False)
        name = str(row.get("name") or "")
        description = str(row.get("description") or "")
        vlan_id = row.get("vlan_id")
        vxlan_id = row.get("vxlan_id")
        gateway = row.get("gateway") or None
        custom_fields = row.get("custom_fields") or {}

        existing = existing_subnets.get(canonical)
        if existing is not None:
            if strategy == "skip":
                result_obj.skipped += 1
                continue
            if strategy == "overwrite":
                old = {
                    "name": existing.name,
                    "description": existing.description,
                    "vlan_id": existing.vlan_id,
                    "vxlan_id": existing.vxlan_id,
                    "gateway": str(existing.gateway) if existing.gateway else None,
                }
                existing.name = name or existing.name
                existing.description = description or existing.description
                if vlan_id is not None:
                    existing.vlan_id = vlan_id
                if vxlan_id is not None:
                    existing.vxlan_id = vxlan_id
                if gateway:
                    existing.gateway = gateway
                if custom_fields:
                    merged = dict(existing.custom_fields or {})
                    merged.update(custom_fields)
                    existing.custom_fields = merged
                db.add(
                    AuditLog(
                        user_id=current_user.id,
                        user_display_name=current_user.display_name,
                        auth_source=current_user.auth_source,
                        action="update",
                        resource_type="subnet",
                        resource_id=str(existing.id),
                        resource_display=f"{canonical} ({existing.name})",
                        old_value=old,
                        new_value={
                            "name": existing.name,
                            "description": existing.description,
                            "vlan_id": existing.vlan_id,
                            "vxlan_id": existing.vxlan_id,
                            "gateway": gateway,
                            "import": True,
                        },
                        result="success",
                    )
                )
                result_obj.updated_subnets += 1
                continue
            # strategy == "fail" handled above, but guard anyway
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Subnet {canonical} already exists",
            )

        # Find or auto-create the parent block
        parent = _find_parent_block(subnet_net, existing_blocks)
        if parent is None:
            parent = IPBlock(
                space_id=space.id,
                parent_block_id=None,
                network=canonical,
                name=f"auto:{canonical}",
                description=f"Auto-created parent block for imported subnet {canonical}",
            )
            db.add(parent)
            await db.flush()
            existing_blocks.append(parent)
            result_obj.auto_created_blocks += 1
            db.add(
                AuditLog(
                    user_id=current_user.id,
                    user_display_name=current_user.display_name,
                    auth_source=current_user.auth_source,
                    action="create",
                    resource_type="ip_block",
                    resource_id=str(parent.id),
                    resource_display=f"{canonical} (auto:{canonical})",
                    new_value={"network": canonical, "auto": True},
                    result="success",
                )
            )

        total = (
            subnet_net.num_addresses if subnet_net.prefixlen >= 31 else subnet_net.num_addresses - 2
        )
        subnet = Subnet(
            space_id=space.id,
            block_id=parent.id,
            network=canonical,
            name=name,
            description=description,
            vlan_id=vlan_id,
            vxlan_id=vxlan_id,
            gateway=gateway,
            status="active",
            total_ips=total,
            allocated_ips=0,
            utilization_percent=0.0,
            custom_fields=custom_fields,
        )
        db.add(subnet)
        await db.flush()
        existing_subnets[canonical] = subnet
        db.add(
            _audit_entry(
                current_user,
                "create",
                str(subnet.id),
                f"{canonical} ({name})",
                {
                    "network": canonical,
                    "name": name,
                    "description": description,
                    "vlan_id": vlan_id,
                    "gateway": gateway,
                    "import": True,
                },
            )
        )
        result_obj.created_subnets += 1

    logger.info(
        "ipam_import_committed",
        space_id=str(space.id),
        created=result_obj.created_subnets,
        updated=result_obj.updated_subnets,
        skipped=result_obj.skipped,
        auto_blocks=result_obj.auto_created_blocks,
    )
    return result_obj


def _safe_canon(value: str) -> str:
    try:
        return _canon_network(value.strip())
    except ValueError:
        return value


# ══ Address importer (subnet-scoped) ══════════════════════════════════════════
#
# Layered on top of the same preview/commit shape as the subnet importer so the
# frontend can reuse the diff table widget. Matching key is ``(subnet_id,
# address)``. Rows whose IP doesn't fall inside the subnet's CIDR are rejected
# as errors rather than silently routed elsewhere — migrations from another
# DDI tool almost always come as per-subnet dumps, and routing cross-subnet
# hides user mistakes.


_VALID_ADDRESS_STATUSES = frozenset(
    {"allocated", "reserved", "dhcp", "static_dhcp", "deprecated", "orphan"}
)


@dataclass
class AddressImportResult:
    subnet_id: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    dns_synced: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__


def _canon_ip(value: str) -> str:
    """Canonicalise a single IP (strip /prefix if the user pasted a host route)."""
    raw = value.strip()
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    return str(ipaddress.ip_address(raw))


async def _load_subnet(db: AsyncSession, subnet_id: uuid.UUID) -> Subnet:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target subnet not found")
    return subnet


async def _load_existing_addresses(db: AsyncSession, subnet_id: uuid.UUID) -> dict[str, IPAddress]:
    result = await db.execute(select(IPAddress).where(IPAddress.subnet_id == subnet_id))
    return {str(a.address): a for a in result.scalars().all()}


def _row_address_fields(
    row: dict[str, Any],
) -> tuple[str | None, dict[str, Any], str | None]:
    """Extract ``(canonical_ip, normalized_fields, error_reason)`` from a row.

    ``normalized_fields`` only includes keys the caller intends to write —
    omitted / blank values are left out so ``overwrite`` doesn't clobber an
    existing value with ``None`` (e.g. a hostname the user set via the UI).
    """
    raw_addr = row.get("address")
    if not raw_addr or not isinstance(raw_addr, str):
        return None, {}, "Missing 'address' / 'ip' column"
    try:
        canonical = _canon_ip(raw_addr)
    except ValueError as exc:
        return None, {}, f"Invalid IP address {raw_addr!r}: {exc}"

    fields: dict[str, Any] = {}
    if (s := row.get("status")) is not None:
        s = str(s).strip()
        if s and s not in _VALID_ADDRESS_STATUSES:
            return (
                None,
                {},
                (
                    f"status must be one of: {', '.join(sorted(_VALID_ADDRESS_STATUSES))} "
                    f"(got {s!r})"
                ),
            )
        if s:
            fields["status"] = s
    if (h := row.get("hostname")) is not None:
        h = str(h).strip() or None
        if h:
            fields["hostname"] = h
    if (m := row.get("mac_address")) is not None:
        m = str(m).strip() or None
        if m:
            fields["mac_address"] = m
    if (d := row.get("description")) is not None:
        d = str(d).strip()
        fields["description"] = d
    if (t := row.get("tags")) is not None:
        if isinstance(t, dict):
            fields["tags"] = t
    if (cf := row.get("custom_fields")) is not None and isinstance(cf, dict):
        fields["custom_fields"] = cf
    return canonical, fields, None


async def preview_address_import(
    db: AsyncSession,
    payload: ParsedPayload,
    *,
    subnet_id: uuid.UUID,
    strategy: Strategy = "fail",
) -> ImportPreview:
    subnet = await _load_subnet(db, subnet_id)
    preview = ImportPreview(space_id=str(subnet.id), space_name=str(subnet.network))

    subnet_net = ipaddress.ip_network(str(subnet.network), strict=False)
    existing = await _load_existing_addresses(db, subnet.id)

    seen: set[str] = set()
    for row in payload.addresses:
        canonical, fields, err = _row_address_fields(row)
        if err or canonical is None:
            preview.errors.append(
                DiffRow(
                    kind="address",
                    action="error",
                    network=str(row.get("address") or row.get("ip") or ""),
                    reason=err or "Invalid row",
                    details=dict(row),
                )
            )
            continue
        try:
            if ipaddress.ip_address(canonical) not in subnet_net:
                preview.errors.append(
                    DiffRow(
                        kind="address",
                        action="error",
                        network=canonical,
                        reason=f"IP is outside subnet {subnet_net}",
                    )
                )
                continue
        except ValueError:
            preview.errors.append(
                DiffRow(
                    kind="address",
                    action="error",
                    network=canonical,
                    reason="Invalid IP for subnet membership check",
                )
            )
            continue
        if canonical in seen:
            preview.errors.append(
                DiffRow(
                    kind="address",
                    action="error",
                    network=canonical,
                    reason="Duplicate row in import payload",
                )
            )
            continue
        seen.add(canonical)

        row_hostname = fields.get("hostname") or ""
        existing_ip = existing.get(canonical)
        if existing_ip is None:
            preview.creates.append(
                DiffRow(
                    kind="address",
                    action="create",
                    network=canonical,
                    name=row_hostname,
                    details={"fields": fields},
                )
            )
            continue

        old = {
            "hostname": existing_ip.hostname,
            "status": existing_ip.status,
            "mac_address": (str(existing_ip.mac_address) if existing_ip.mac_address else None),
            "description": existing_ip.description,
        }
        diff_details = {"old": old, "new": fields}
        if strategy == "overwrite":
            preview.updates.append(
                DiffRow(
                    kind="address",
                    action="update",
                    network=canonical,
                    name=row_hostname or (existing_ip.hostname or ""),
                    details=diff_details,
                )
            )
        elif strategy == "skip":
            preview.conflicts.append(
                DiffRow(
                    kind="address",
                    action="skip",
                    network=canonical,
                    name=existing_ip.hostname or "",
                    reason="Already exists; strategy=skip",
                    details=diff_details,
                )
            )
        else:  # fail
            preview.conflicts.append(
                DiffRow(
                    kind="address",
                    action="conflict",
                    network=canonical,
                    name=existing_ip.hostname or "",
                    reason="Already exists; commit will fail unless strategy is set",
                    details=diff_details,
                )
            )
    return preview


async def commit_address_import(
    db: AsyncSession,
    payload: ParsedPayload,
    *,
    current_user: Any,
    subnet_id: uuid.UUID,
    strategy: Strategy = "fail",
) -> AddressImportResult:
    """Apply the address import in the caller's transaction.

    Matching key: ``(subnet_id, address)``. After each create/update, we
    call the IPAM router's ``_sync_dns_record`` so imported rows with a
    hostname get an A + PTR record published via the same RFC 2136 path
    that the interactive UI uses. The import is equivalent to N calls to
    ``POST /ipam/addresses`` / ``PUT /ipam/addresses/{id}`` — same audit
    log, same DNS side-effects.
    """
    from app.api.v1.ipam.router import _sync_dns_record

    subnet = await _load_subnet(db, subnet_id)
    result_obj = AddressImportResult(subnet_id=str(subnet.id))
    subnet_net = ipaddress.ip_network(str(subnet.network), strict=False)
    existing = await _load_existing_addresses(db, subnet.id)

    # Pre-flight: fail-strategy bails out before mutating anything.
    if strategy == "fail":
        for row in payload.addresses:
            canonical, _, err = _row_address_fields(row)
            if err or canonical is None:
                continue
            if canonical in existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"{canonical} already exists in {subnet.network}; "
                        "re-run with strategy='skip' or 'overwrite'"
                    ),
                )

    allocated_delta = 0
    for row in payload.addresses:
        canonical, fields, err = _row_address_fields(row)
        if err or canonical is None:
            result_obj.errors.append(err or "invalid row")
            continue
        try:
            if ipaddress.ip_address(canonical) not in subnet_net:
                result_obj.errors.append(f"{canonical}: outside {subnet_net}")
                continue
        except ValueError:
            result_obj.errors.append(f"{canonical}: invalid IP")
            continue

        existing_ip = existing.get(canonical)

        if existing_ip is not None:
            if strategy == "skip":
                result_obj.skipped += 1
                continue
            if strategy == "overwrite":
                old_snapshot = {
                    "hostname": existing_ip.hostname,
                    "status": existing_ip.status,
                    "mac_address": (
                        str(existing_ip.mac_address) if existing_ip.mac_address else None
                    ),
                    "description": existing_ip.description,
                }
                had_hostname = bool(existing_ip.hostname)
                for k, v in fields.items():
                    if k == "custom_fields":
                        merged = dict(existing_ip.custom_fields or {})
                        merged.update(v)
                        existing_ip.custom_fields = merged
                    elif k == "tags":
                        merged_tags = dict(existing_ip.tags or {})
                        merged_tags.update(v)
                        existing_ip.tags = merged_tags
                    else:
                        setattr(existing_ip, k, v)
                db.add(
                    AuditLog(
                        user_id=current_user.id,
                        user_display_name=current_user.display_name,
                        auth_source=current_user.auth_source,
                        action="update",
                        resource_type="ip_address",
                        resource_id=str(existing_ip.id),
                        resource_display=f"{canonical} ({existing_ip.hostname or ''})",
                        old_value=old_snapshot,
                        new_value={**fields, "import": True},
                        result="success",
                    )
                )
                await db.flush()
                if existing_ip.hostname and not had_hostname:
                    # Hostname was just assigned — publish new DNS record.
                    try:
                        await _sync_dns_record(db, existing_ip, subnet, action="create")
                        result_obj.dns_synced += 1
                    except Exception as exc:  # noqa: BLE001
                        result_obj.errors.append(f"{canonical}: DNS sync failed: {exc}")
                elif had_hostname and "hostname" in fields:
                    # Hostname changed — republish (sync handles update via re-create).
                    try:
                        await _sync_dns_record(db, existing_ip, subnet, action="create")
                        result_obj.dns_synced += 1
                    except Exception as exc:  # noqa: BLE001
                        result_obj.errors.append(f"{canonical}: DNS sync failed: {exc}")
                result_obj.updated += 1
                continue
            # strategy == "fail" already raised above; unreachable.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{canonical} already exists",
            )

        # New row
        ip_status = fields.get("status") or "allocated"
        new_ip = IPAddress(
            subnet_id=subnet.id,
            address=canonical,
            status=ip_status,
            hostname=fields.get("hostname"),
            mac_address=fields.get("mac_address"),
            description=fields.get("description") or "",
            tags=fields.get("tags") or {},
            custom_fields=fields.get("custom_fields") or {},
        )
        db.add(new_ip)
        await db.flush()
        existing[canonical] = new_ip
        allocated_delta += 1
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="create",
                resource_type="ip_address",
                resource_id=str(new_ip.id),
                resource_display=f"{canonical} ({new_ip.hostname or ''})",
                new_value={**fields, "address": canonical, "import": True},
                result="success",
            )
        )
        if new_ip.hostname:
            try:
                await _sync_dns_record(db, new_ip, subnet, action="create")
                result_obj.dns_synced += 1
            except Exception as exc:  # noqa: BLE001
                # Don't fail the whole import — user can re-run DNS Sync after.
                result_obj.errors.append(f"{canonical}: DNS sync failed: {exc}")
        result_obj.created += 1

    # Keep the subnet's utilization counters roughly honest. The periodic
    # allocation-recount task corrects drift, but users expect the UI to
    # reflect the new row count immediately.
    if allocated_delta:
        subnet.allocated_ips = (subnet.allocated_ips or 0) + allocated_delta
        if subnet.total_ips:
            subnet.utilization_percent = round(100.0 * subnet.allocated_ips / subnet.total_ips, 2)

    logger.info(
        "ipam_address_import_committed",
        subnet_id=str(subnet.id),
        created=result_obj.created,
        updated=result_obj.updated,
        skipped=result_obj.skipped,
        dns_synced=result_obj.dns_synced,
        errors=len(result_obj.errors),
    )
    return result_obj
