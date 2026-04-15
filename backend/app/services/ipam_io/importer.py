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
from app.models.ipam import IPBlock, IPSpace, Subnet
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
