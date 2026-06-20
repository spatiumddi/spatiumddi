"""Address sets CRUD (issue #103).

A named, RBAC-scoped slice of a subnet's address space. Granting
``write``/``admin`` on a single ``address_set`` id lets a department
admin edit just their range of a subnet without holding subnet-wide
write — the gate that consults these rows lives in the IPAM address
handlers (``app.api.v1.ipam.router``).

This surface CRUD-manages the set rows themselves. Read is subnet-wide
(any ``read`` on ``address_set``); writes require ``admin`` on the
``address_set`` type (create) or on the specific row id (update/delete).
Every mutation writes an ``AuditLog`` row before commit (non-negotiable
#4). The whole router gates behind the ``ipam.address_sets`` feature
module at the v1 include (non-negotiable #14).
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from app.api.deps import DB, CurrentUser
from app.core.permissions import require_any_resource_permission, user_has_permission
from app.models.address_set import ADDRESS_SET_RANGE_KINDS, AddressSet
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.ipam import Subnet

logger = structlog.get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_any_resource_permission("address_set"))])


# ── Schemas ──────────────────────────────────────────────────────────────


class AddressSetResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    subnet_id: uuid.UUID
    customer_id: uuid.UUID | None
    site_id: uuid.UUID | None
    range_kind: str
    start_address: str | None
    end_address: str | None
    explicit_addresses: list[str]
    tags: dict[str, Any]
    custom_fields: dict[str, Any]
    created_at: datetime
    modified_at: datetime


class AddressSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    subnet_id: uuid.UUID
    customer_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    range_kind: str = "contiguous"
    start_address: str | None = None
    end_address: str | None = None
    explicit_addresses: list[str] = Field(default_factory=list)
    tags: dict[str, Any] = Field(default_factory=dict)
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_shape(self) -> AddressSetCreate:
        _validate_range_shape(
            self.range_kind, self.start_address, self.end_address, self.explicit_addresses
        )
        return self


class AddressSetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    customer_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    range_kind: str | None = None
    start_address: str | None = None
    end_address: str | None = None
    explicit_addresses: list[str] | None = None
    tags: dict[str, Any] | None = None
    custom_fields: dict[str, Any] | None = None


# ── Helpers ──────────────────────────────────────────────────────────────


def _validate_range_shape(
    range_kind: str,
    start_address: str | None,
    end_address: str | None,
    explicit_addresses: list[str],
) -> None:
    """Validate the contiguous/explicit shape (parse-only — no subnet check)."""
    if range_kind not in ADDRESS_SET_RANGE_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"range_kind must be one of {sorted(ADDRESS_SET_RANGE_KINDS)}",
        )
    if range_kind == "contiguous":
        if not start_address or not end_address:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="contiguous range requires start_address and end_address",
            )
        try:
            s = ipaddress.ip_address(start_address)
            e = ipaddress.ip_address(end_address)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid start/end address: {exc}",
            ) from exc
        if s.version != e.version:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="start_address and end_address must be the same IP family",
            )
        if int(s) > int(e):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="start_address must be <= end_address",
            )
    else:  # explicit
        if not explicit_addresses:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="explicit range requires a non-empty explicit_addresses list",
            )
        for raw in explicit_addresses:
            try:
                ipaddress.ip_address(raw)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"invalid address in explicit_addresses: {raw}",
                ) from exc


async def _validate_within_subnet(
    db: DB,
    subnet_id: uuid.UUID,
    *,
    range_kind: str,
    start_address: str | None,
    end_address: str | None,
    explicit_addresses: list[str],
) -> Subnet:
    """Confirm the subnet exists and the range falls within its CIDR."""
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found.")
    net = ipaddress.ip_network(str(subnet.network), strict=False)
    if range_kind == "contiguous":
        addrs = [start_address, end_address]
    else:
        addrs = list(explicit_addresses)
    for raw in addrs:
        if raw is None:
            continue
        if ipaddress.ip_address(raw) not in net:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"address {raw} is outside subnet {subnet.network}",
            )
    return subnet


def _to_response(row: AddressSet) -> AddressSetResponse:
    return AddressSetResponse(
        id=row.id,
        name=row.name,
        description=row.description or "",
        subnet_id=row.subnet_id,
        customer_id=row.customer_id,
        site_id=row.site_id,
        range_kind=row.range_kind,
        start_address=str(row.start_address) if row.start_address is not None else None,
        end_address=str(row.end_address) if row.end_address is not None else None,
        explicit_addresses=list(row.explicit_addresses or []),
        tags=dict(row.tags or {}),
        custom_fields=dict(row.custom_fields or {}),
        created_at=row.created_at,
        modified_at=row.modified_at,
    )


def _snapshot(row: AddressSet) -> dict[str, Any]:
    return {
        "name": row.name,
        "description": row.description,
        "subnet_id": str(row.subnet_id),
        "customer_id": str(row.customer_id) if row.customer_id else None,
        "site_id": str(row.site_id) if row.site_id else None,
        "range_kind": row.range_kind,
        "start_address": str(row.start_address) if row.start_address is not None else None,
        "end_address": str(row.end_address) if row.end_address is not None else None,
        "explicit_addresses": list(row.explicit_addresses or []),
        "tags": dict(row.tags or {}),
        "custom_fields": dict(row.custom_fields or {}),
    }


def _audit(
    db: DB,
    *,
    user: User,
    action: str,
    row: AddressSet,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action=action,
            resource_type="address_set",
            resource_id=str(row.id),
            resource_display=row.name,
            old_value=old_value,
            new_value=new_value,
        )
    )


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("", response_model=list[AddressSetResponse])
async def list_address_sets(
    current_user: CurrentUser,
    db: DB,
    subnet_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> list[AddressSetResponse]:
    stmt = select(AddressSet)
    if subnet_id is not None:
        stmt = stmt.where(AddressSet.subnet_id == subnet_id)
    if customer_id is not None:
        stmt = stmt.where(AddressSet.customer_id == customer_id)
    if site_id is not None:
        stmt = stmt.where(AddressSet.site_id == site_id)
    if search:
        stmt = stmt.where(AddressSet.name.ilike(f"%{search}%"))
    stmt = stmt.order_by(AddressSet.name).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_response(r) for r in rows]


@router.get("/{set_id}", response_model=AddressSetResponse)
async def get_address_set(
    set_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
) -> AddressSetResponse:
    row = await db.get(AddressSet, set_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address set not found.")
    return _to_response(row)


@router.post("", response_model=AddressSetResponse, status_code=status.HTTP_201_CREATED)
async def create_address_set(
    body: AddressSetCreate,
    current_user: CurrentUser,
    db: DB,
) -> AddressSetResponse:
    if not user_has_permission(current_user, "admin", "address_set"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: need 'admin' on address_set to create.",
        )
    await _validate_within_subnet(
        db,
        body.subnet_id,
        range_kind=body.range_kind,
        start_address=body.start_address,
        end_address=body.end_address,
        explicit_addresses=body.explicit_addresses,
    )
    # Friendly 409 rather than leaking the unique-constraint error.
    existing = (
        await db.execute(
            select(AddressSet.id).where(
                AddressSet.subnet_id == body.subnet_id,
                AddressSet.name == body.name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'An address set named "{body.name}" already exists on this subnet.',
        )

    row = AddressSet(
        name=body.name,
        description=body.description or "",
        subnet_id=body.subnet_id,
        customer_id=body.customer_id,
        site_id=body.site_id,
        range_kind=body.range_kind,
        start_address=body.start_address,
        end_address=body.end_address,
        explicit_addresses=list(body.explicit_addresses),
        tags=dict(body.tags),
        custom_fields=dict(body.custom_fields),
    )
    db.add(row)
    await db.flush()
    _audit(db, user=current_user, action="create", row=row, new_value=_snapshot(row))
    await db.commit()
    await db.refresh(row)
    logger.info(
        "address_set.created",
        address_set_id=str(row.id),
        subnet_id=str(row.subnet_id),
        range_kind=row.range_kind,
    )
    return _to_response(row)


@router.put("/{set_id}", response_model=AddressSetResponse)
async def update_address_set(
    set_id: uuid.UUID,
    body: AddressSetUpdate,
    current_user: CurrentUser,
    db: DB,
) -> AddressSetResponse:
    row = await db.get(AddressSet, set_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address set not found.")
    if not user_has_permission(current_user, "admin", "address_set", row.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: need 'admin' on this address set.",
        )

    old_value = _snapshot(row)
    data = body.model_dump(exclude_unset=True)

    # Resolve the post-update range shape so it can be validated as a whole.
    new_range_kind = data.get("range_kind", row.range_kind)
    new_start = (
        data["start_address"]
        if "start_address" in data
        else (str(row.start_address) if row.start_address is not None else None)
    )
    new_end = (
        data["end_address"]
        if "end_address" in data
        else (str(row.end_address) if row.end_address is not None else None)
    )
    new_explicit = (
        data["explicit_addresses"]
        if "explicit_addresses" in data
        else list(row.explicit_addresses or [])
    )
    range_touched = any(
        k in data for k in ("range_kind", "start_address", "end_address", "explicit_addresses")
    )
    if range_touched:
        _validate_range_shape(new_range_kind, new_start, new_end, new_explicit)
        await _validate_within_subnet(
            db,
            row.subnet_id,
            range_kind=new_range_kind,
            start_address=new_start,
            end_address=new_end,
            explicit_addresses=new_explicit,
        )

    if "name" in data and data["name"] != row.name:
        clash = (
            await db.execute(
                select(AddressSet.id).where(
                    AddressSet.subnet_id == row.subnet_id,
                    AddressSet.name == data["name"],
                    AddressSet.id != row.id,
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'An address set named "{data["name"]}" already exists on this subnet.',
            )

    for field in (
        "name",
        "description",
        "customer_id",
        "site_id",
        "range_kind",
        "start_address",
        "end_address",
        "explicit_addresses",
        "tags",
        "custom_fields",
    ):
        if field in data:
            setattr(row, field, data[field])
    # Contiguous sets never carry an explicit list, and vice-versa — keep
    # the inactive shape's columns clean so reads don't surface stale data.
    if row.range_kind == "explicit":
        row.start_address = None
        row.end_address = None
    elif row.range_kind == "contiguous":
        row.explicit_addresses = []

    _audit(
        db,
        user=current_user,
        action="update",
        row=row,
        old_value=old_value,
        new_value=_snapshot(row),
    )
    await db.commit()
    await db.refresh(row)
    logger.info("address_set.updated", address_set_id=str(row.id))
    return _to_response(row)


@router.delete("/{set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_address_set(
    set_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
) -> None:
    row = await db.get(AddressSet, set_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address set not found.")
    if not user_has_permission(current_user, "admin", "address_set", row.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: need 'admin' on this address set.",
        )
    _audit(db, user=current_user, action="delete", row=row, old_value=_snapshot(row))
    await db.delete(row)
    await db.commit()
    logger.info("address_set.deleted", address_set_id=str(set_id))
