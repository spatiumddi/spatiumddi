"""NAT mapping CRUD.

NAT mappings are operator-curated metadata describing 1:1 NAT, PAT,
or hide-NAT bindings between internal and external IPs. SpatiumDDI
doesn't render or push the rules anywhere — the value is purely
visibility: an IP row in IPAM gets a ``nat_mapping_count`` field so
operators can see at a glance whether an address is one side of a
known mapping, with a tooltip listing the names.

Three kinds:

  * ``1to1`` — static one-to-one. Requires ``internal_ip`` AND
    ``external_ip``; forbids port ranges.
  * ``pat``  — port-based NAT. Requires ``internal_ip`` AND
    ``external_ip`` AND at least one of the port range pairs.
  * ``hide`` — many-to-one masquerade. Requires ``internal_subnet_id``
    AND ``external_ip``; forbids port ranges.

Validation is enforced at the Pydantic schema level so the API never
gets to write a half-formed row. Audit goes through the standard
``write_audit`` helper for parity with the rest of IPAM.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import String, cast, func, select

from app.api.deps import DB, CurrentUser
from app.api.v1.dhcp._audit import write_audit
from app.core.permissions import require_resource_permission
from app.models.ipam import NATMapping

router = APIRouter(
    prefix="/nat-mappings",
    tags=["ipam"],
    dependencies=[Depends(require_resource_permission("nat_mapping"))],
)

_VALID_KINDS = frozenset({"1to1", "pat", "hide"})
_VALID_PROTOS = frozenset({"tcp", "udp", "any"})


def _validate_port(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if not (0 <= value <= 65535):
        raise ValueError(f"{name} must be in 0..65535")
    return value


class NATMappingBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    kind: str
    internal_ip: str | None = None
    internal_subnet_id: uuid.UUID | None = None
    internal_port_start: int | None = None
    internal_port_end: int | None = None
    external_ip: str | None = None
    external_port_start: int | None = None
    external_port_end: int | None = None
    protocol: str = "any"
    device_label: str | None = None
    description: str | None = None
    tags: list[Any] = []
    custom_fields: dict[str, Any] = {}

    @field_validator("kind")
    @classmethod
    def _kind_valid(cls, v: str) -> str:
        if v not in _VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)}")
        return v

    @field_validator("protocol")
    @classmethod
    def _proto_valid(cls, v: str) -> str:
        if v not in _VALID_PROTOS:
            raise ValueError(f"protocol must be one of {sorted(_VALID_PROTOS)}")
        return v

    @field_validator(
        "internal_port_start", "internal_port_end", "external_port_start", "external_port_end"
    )
    @classmethod
    def _port_range(cls, v: int | None) -> int | None:
        return _validate_port(v, "port")

    @model_validator(mode="after")
    def _shape(self) -> NATMappingBase:
        kind = self.kind
        ports_present = any(
            p is not None
            for p in (
                self.internal_port_start,
                self.internal_port_end,
                self.external_port_start,
                self.external_port_end,
            )
        )

        if kind == "1to1":
            if not self.internal_ip or not self.external_ip:
                raise ValueError("kind='1to1' requires internal_ip and external_ip")
            if ports_present:
                raise ValueError("kind='1to1' does not allow port ranges")
        elif kind == "pat":
            if not self.internal_ip or not self.external_ip:
                raise ValueError("kind='pat' requires internal_ip and external_ip")
            if not ports_present:
                raise ValueError("kind='pat' requires at least one port range")
        elif kind == "hide":
            if not self.internal_subnet_id or not self.external_ip:
                raise ValueError("kind='hide' requires internal_subnet_id and external_ip")
            if ports_present:
                raise ValueError("kind='hide' does not allow port ranges")

        # Range sanity — start ≤ end when both set.
        for s, e, label in (
            (self.internal_port_start, self.internal_port_end, "internal"),
            (self.external_port_start, self.external_port_end, "external"),
        ):
            if s is not None and e is not None and s > e:
                raise ValueError(f"{label}_port_start must be <= {label}_port_end")
        return self


class NATMappingCreate(NATMappingBase):
    pass


class NATMappingUpdate(BaseModel):
    """All fields optional — PATCH-style update.

    Validation of the kind-specific shape happens after the merge in the
    handler, so partial updates that toggle ``kind`` still get fully
    validated. Validating the patch alone would force callers to re-send
    every field on every change.
    """

    name: str | None = None
    kind: str | None = None
    internal_ip: str | None = None
    internal_subnet_id: uuid.UUID | None = None
    internal_port_start: int | None = None
    internal_port_end: int | None = None
    external_ip: str | None = None
    external_port_start: int | None = None
    external_port_end: int | None = None
    protocol: str | None = None
    device_label: str | None = None
    description: str | None = None
    tags: list[Any] | None = None
    custom_fields: dict[str, Any] | None = None

    @field_validator("kind")
    @classmethod
    def _kind_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)}")
        return v

    @field_validator("protocol")
    @classmethod
    def _proto_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_PROTOS:
            raise ValueError(f"protocol must be one of {sorted(_VALID_PROTOS)}")
        return v


class NATMappingResponse(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    internal_ip: str | None
    internal_subnet_id: uuid.UUID | None
    internal_port_start: int | None
    internal_port_end: int | None
    external_ip: str | None
    external_port_start: int | None
    external_port_end: int | None
    protocol: str
    device_label: str | None
    description: str | None
    tags: list[Any]
    custom_fields: dict[str, Any]
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("internal_ip", "external_ip", mode="before")
    @classmethod
    def _coerce_inet(cls, v: Any) -> Any:
        return str(v) if v is not None else v


class NATMappingPage(BaseModel):
    total: int
    page: int
    per_page: int
    items: list[NATMappingResponse]


@router.get("", response_model=NATMappingPage)
async def list_nat_mappings(
    db: DB,
    _: CurrentUser,
    kind: str | None = Query(None),
    internal_ip: str | None = Query(None),
    external_ip: str | None = Query(None),
    q: str | None = Query(None, description="Substring match on name / description"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
) -> NATMappingPage:
    base = select(NATMapping)
    if kind is not None:
        if kind not in _VALID_KINDS:
            raise HTTPException(
                status_code=422, detail=f"kind must be one of {sorted(_VALID_KINDS)}"
            )
        base = base.where(NATMapping.kind == kind)
    if internal_ip is not None:
        base = base.where(NATMapping.internal_ip == internal_ip)
    if external_ip is not None:
        base = base.where(NATMapping.external_ip == external_ip)
    if q:
        like = f"%{q}%"
        base = base.where(
            cast(NATMapping.name, String).ilike(like)
            | cast(func.coalesce(NATMapping.description, ""), String).ilike(like)
        )

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                base.order_by(NATMapping.name).offset((page - 1) * per_page).limit(per_page)
            )
        )
        .scalars()
        .all()
    )
    return NATMappingPage(
        total=int(total or 0),
        page=page,
        per_page=per_page,
        items=[NATMappingResponse.model_validate(r) for r in rows],
    )


@router.post("", response_model=NATMappingResponse, status_code=status.HTTP_201_CREATED)
async def create_nat_mapping(
    body: NATMappingCreate, db: DB, user: CurrentUser
) -> NATMappingResponse:
    row = NATMapping(**body.model_dump())
    db.add(row)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="nat_mapping",
        resource_id=str(row.id),
        resource_display=row.name,
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(row)
    return NATMappingResponse.model_validate(row)


@router.get("/{mapping_id}", response_model=NATMappingResponse)
async def get_nat_mapping(mapping_id: uuid.UUID, db: DB, _: CurrentUser) -> NATMappingResponse:
    row = await db.get(NATMapping, mapping_id)
    if row is None:
        raise HTTPException(status_code=404, detail="NAT mapping not found")
    return NATMappingResponse.model_validate(row)


@router.patch("/{mapping_id}", response_model=NATMappingResponse)
async def update_nat_mapping(
    mapping_id: uuid.UUID, body: NATMappingUpdate, db: DB, user: CurrentUser
) -> NATMappingResponse:
    row = await db.get(NATMapping, mapping_id)
    if row is None:
        raise HTTPException(status_code=404, detail="NAT mapping not found")

    patch = body.model_dump(exclude_unset=True)
    if not patch:
        return NATMappingResponse.model_validate(row)

    # Build the merged-state shape and re-run kind-aware validation so a
    # partial update that toggles ``kind`` still gets the full check.
    merged = {
        "name": patch.get("name", row.name),
        "kind": patch.get("kind", row.kind),
        "internal_ip": patch.get(
            "internal_ip", str(row.internal_ip) if row.internal_ip is not None else None
        ),
        "internal_subnet_id": patch.get("internal_subnet_id", row.internal_subnet_id),
        "internal_port_start": patch.get("internal_port_start", row.internal_port_start),
        "internal_port_end": patch.get("internal_port_end", row.internal_port_end),
        "external_ip": patch.get(
            "external_ip", str(row.external_ip) if row.external_ip is not None else None
        ),
        "external_port_start": patch.get("external_port_start", row.external_port_start),
        "external_port_end": patch.get("external_port_end", row.external_port_end),
        "protocol": patch.get("protocol", row.protocol),
        "device_label": patch.get("device_label", row.device_label),
        "description": patch.get("description", row.description),
        "tags": patch.get("tags", row.tags),
        "custom_fields": patch.get("custom_fields", row.custom_fields),
    }
    try:
        NATMappingBase.model_validate(merged)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    for key, value in patch.items():
        setattr(row, key, value)

    write_audit(
        db,
        user=user,
        action="update",
        resource_type="nat_mapping",
        resource_id=str(row.id),
        resource_display=row.name,
        changed_fields=list(patch.keys()),
        new_value=patch,
    )
    await db.commit()
    await db.refresh(row)
    return NATMappingResponse.model_validate(row)


@router.delete("/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_nat_mapping(mapping_id: uuid.UUID, db: DB, user: CurrentUser) -> None:
    row = await db.get(NATMapping, mapping_id)
    if row is None:
        raise HTTPException(status_code=404, detail="NAT mapping not found")
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="nat_mapping",
        resource_id=str(row.id),
        resource_display=row.name,
    )
    await db.delete(row)
    await db.commit()
