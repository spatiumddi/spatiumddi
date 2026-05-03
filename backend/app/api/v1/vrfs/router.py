"""VRF CRUD API (issue #86, phase 1).

Replaces the freeform ``vrf_name`` / ``route_distinguisher`` /
``route_targets`` columns on :class:`app.models.ipam.IPSpace` with a
proper relational entity. The freeform columns are still on the
table for one release cycle so operators can verify the migration;
new writes should set ``IPSpace.vrf_id`` (and / or
``IPBlock.vrf_id``) instead of the freeform fields.

Permission gate: ``manage_vrfs`` (resource type ``vrf``). Maps to
the standard HTTP-method action grammar via
:func:`require_resource_permission`. Superadmin always bypasses.

Out of scope for phase 1:

* VRF detail page tabs (per-VRF list of IP spaces / blocks).
* Cross-cutting validation against the VRF's ASN — added when the
  ASN model lands (issue #85).
* The follow-up migration that drops the deprecated freeform
  columns. That ships in the next release.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select

from app.api.deps import DB, CurrentUser
from app.core.permissions import require_resource_permission
from app.models.audit import AuditLog
from app.models.ipam import IPBlock, IPSpace
from app.models.vrf import VRF

logger = structlog.get_logger(__name__)
router = APIRouter(dependencies=[Depends(require_resource_permission("vrf"))])


# ── Validation helpers ────────────────────────────────────────────────────────

# RD / RT format. Two flavours, both ``X:N``:
#   * ``ASN:N``   — e.g. ``65000:100``       (ASN portion is a uint)
#   * ``IP:N``    — e.g. ``192.0.2.1:100``   (ASN portion is dotted IPv4)
# Stored verbatim — we don't canonicalise (vendor opinions disagree).
_RD_RT_RE = re.compile(r"^(\d+|(\d+\.){3}\d+):\d+$")
_BULK_DELETE_CAP = 500


def _validate_rd(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    if not _RD_RT_RE.match(v):
        raise ValueError(
            "route_distinguisher must match 'ASN:N' or 'IP:N' "
            "(e.g. '65000:100' or '192.0.2.1:100')"
        )
    return v


def _validate_rt_list(values: list[str] | None) -> list[str]:
    out: list[str] = []
    if not values:
        return out
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        if not _RD_RT_RE.match(v):
            raise ValueError(
                f"route target '{v}' must match 'ASN:N' or 'IP:N' "
                "(e.g. '65000:100' or '192.0.2.1:100')"
            )
        out.append(v)
    return out


def _audit(
    user: Any,
    action: str,
    resource_id: str,
    resource_display: str,
    *,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> AuditLog:
    return AuditLog(
        user_id=user.id,
        user_display_name=user.display_name,
        auth_source=user.auth_source,
        action=action,
        resource_type="vrf",
        resource_id=resource_id,
        resource_display=resource_display,
        old_value=old_value,
        new_value=new_value,
        result="success",
    )


# ── Schemas ───────────────────────────────────────────────────────────────────


class VRFCreate(BaseModel):
    name: str
    description: str = ""
    asn_id: uuid.UUID | None = None
    route_distinguisher: str | None = None
    import_targets: list[str] = []
    export_targets: list[str] = []
    tags: dict[str, Any] = {}
    custom_fields: dict[str, Any] = {}

    @field_validator("name")
    @classmethod
    def _name_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("route_distinguisher")
    @classmethod
    def _rd(cls, v: str | None) -> str | None:
        return _validate_rd(v)

    @field_validator("import_targets", "export_targets", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            # Be liberal with what we accept on the wire — the UI
            # sends a comma-separated string when the operator types
            # in the form. Normalise to a list before per-item RT
            # validation runs in ``_after_validate``.
            return [s.strip() for s in v.split(",") if s.strip()]
        return list(v)

    @field_validator("import_targets", "export_targets")
    @classmethod
    def _rt_items(cls, v: list[str]) -> list[str]:
        return _validate_rt_list(v)


class VRFUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    asn_id: uuid.UUID | None = None
    route_distinguisher: str | None = None
    import_targets: list[str] | None = None
    export_targets: list[str] | None = None
    tags: dict[str, Any] | None = None
    custom_fields: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name cannot be empty")
        return v

    @field_validator("route_distinguisher")
    @classmethod
    def _rd(cls, v: str | None) -> str | None:
        return _validate_rd(v)

    @field_validator("import_targets", "export_targets", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> list[str] | None:
        if v is None:
            return None
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return list(v)

    @field_validator("import_targets", "export_targets")
    @classmethod
    def _rt_items(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return _validate_rt_list(v)


class VRFResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    asn_id: uuid.UUID | None
    route_distinguisher: str | None
    import_targets: list[str]
    export_targets: list[str]
    tags: dict[str, Any]
    custom_fields: dict[str, Any]
    created_at: datetime
    modified_at: datetime
    space_count: int = 0
    block_count: int = 0

    model_config = {"from_attributes": True}

    @field_validator("import_targets", "export_targets", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> list[str]:
        return v if isinstance(v, list) else []

    @field_validator("tags", "custom_fields", mode="before")
    @classmethod
    def _coerce_dict(cls, v: Any) -> dict:
        return v if isinstance(v, dict) else {}


class BulkDeleteRequest(BaseModel):
    ids: list[uuid.UUID] = Field(..., max_length=_BULK_DELETE_CAP)
    force: bool = False


class BulkDeleteResponse(BaseModel):
    deleted: int
    detached_spaces: int
    detached_blocks: int
    not_found: list[uuid.UUID]
    refused: list[dict[str, Any]]


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _fetch_counts(db, vrf_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
    """Return a {vrf_id: (space_count, block_count)} map for the given VRFs.

    Issued as two grouped COUNT queries rather than a per-row N+1.
    """
    if not vrf_ids:
        return {}
    space_rows = (
        await db.execute(
            select(IPSpace.vrf_id, func.count(IPSpace.id))
            .where(IPSpace.vrf_id.in_(vrf_ids))
            .group_by(IPSpace.vrf_id)
        )
    ).all()
    block_rows = (
        await db.execute(
            select(IPBlock.vrf_id, func.count(IPBlock.id))
            .where(IPBlock.vrf_id.in_(vrf_ids))
            .group_by(IPBlock.vrf_id)
        )
    ).all()
    out: dict[uuid.UUID, tuple[int, int]] = {vid: (0, 0) for vid in vrf_ids}
    for vid, n in space_rows:
        out[vid] = (n, out[vid][1])
    for vid, n in block_rows:
        out[vid] = (out[vid][0], n)
    return out


def _to_response(v: VRF, space_count: int = 0, block_count: int = 0) -> dict[str, Any]:
    return {
        "id": v.id,
        "name": v.name,
        "description": v.description,
        "asn_id": v.asn_id,
        "route_distinguisher": v.route_distinguisher,
        "import_targets": list(v.import_targets or []),
        "export_targets": list(v.export_targets or []),
        "tags": dict(v.tags or {}),
        "custom_fields": dict(v.custom_fields or {}),
        "created_at": v.created_at,
        "modified_at": v.modified_at,
        "space_count": space_count,
        "block_count": block_count,
    }


async def _name_conflict(db, name: str, exclude_id: uuid.UUID | None = None) -> VRF | None:
    q = select(VRF).where(VRF.name == name)
    if exclude_id is not None:
        q = q.where(VRF.id != exclude_id)
    return await db.scalar(q)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("", response_model=list[VRFResponse])
async def list_vrfs(
    current_user: CurrentUser,
    db: DB,
    asn_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None, description="Substring match against name or RD"),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    q = select(VRF).order_by(VRF.name)
    if asn_id is not None:
        q = q.where(VRF.asn_id == asn_id)
    if search:
        like = f"%{search}%"
        q = q.where(
            or_(
                VRF.name.ilike(like),
                VRF.route_distinguisher.ilike(like),
            )
        )
    q = q.limit(limit).offset(offset)
    rows = list((await db.execute(q)).scalars().all())
    counts = await _fetch_counts(db, [r.id for r in rows])
    return [_to_response(r, *counts.get(r.id, (0, 0))) for r in rows]


@router.post("", response_model=VRFResponse, status_code=status.HTTP_201_CREATED)
async def create_vrf(body: VRFCreate, current_user: CurrentUser, db: DB) -> dict[str, Any]:
    if await _name_conflict(db, body.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"VRF with name '{body.name}' already exists",
        )
    v = VRF(
        name=body.name,
        description=body.description,
        asn_id=body.asn_id,
        route_distinguisher=body.route_distinguisher,
        import_targets=body.import_targets,
        export_targets=body.export_targets,
        tags=body.tags,
        custom_fields=body.custom_fields,
    )
    db.add(v)
    await db.flush()
    db.add(
        _audit(
            current_user,
            "create",
            str(v.id),
            v.name,
            new_value=body.model_dump(mode="json"),
        )
    )
    await db.commit()
    await db.refresh(v)
    return _to_response(v)


@router.get("/{vrf_id}", response_model=VRFResponse)
async def get_vrf(vrf_id: uuid.UUID, current_user: CurrentUser, db: DB) -> dict[str, Any]:
    v = await db.get(VRF, vrf_id)
    if v is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VRF not found")
    counts = await _fetch_counts(db, [v.id])
    return _to_response(v, *counts.get(v.id, (0, 0)))


@router.put("/{vrf_id}", response_model=VRFResponse)
async def update_vrf(
    vrf_id: uuid.UUID, body: VRFUpdate, current_user: CurrentUser, db: DB
) -> dict[str, Any]:
    v = await db.get(VRF, vrf_id)
    if v is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VRF not found")
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] != v.name:
        if await _name_conflict(db, changes["name"], exclude_id=v.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"VRF with name '{changes['name']}' already exists",
            )
    old = {
        "name": v.name,
        "description": v.description,
        "asn_id": str(v.asn_id) if v.asn_id else None,
        "route_distinguisher": v.route_distinguisher,
        "import_targets": list(v.import_targets or []),
        "export_targets": list(v.export_targets or []),
        "tags": dict(v.tags or {}),
        "custom_fields": dict(v.custom_fields or {}),
    }
    for field, value in changes.items():
        setattr(v, field, value)
    db.add(
        _audit(
            current_user,
            "update",
            str(v.id),
            v.name,
            old_value=old,
            new_value=changes,
        )
    )
    await db.commit()
    await db.refresh(v)
    counts = await _fetch_counts(db, [v.id])
    return _to_response(v, *counts.get(v.id, (0, 0)))


@router.delete("/{vrf_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vrf(
    vrf_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
    force: bool = Query(False, description="Detach linked spaces / blocks (FK SET NULL)"),
) -> None:
    v = await db.get(VRF, vrf_id)
    if v is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VRF not found")
    counts = await _fetch_counts(db, [v.id])
    sc, bc = counts.get(v.id, (0, 0))
    if (sc or bc) and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete VRF '{v.name}': still referenced by "
                f"{sc} IP space{'s' if sc != 1 else ''} and "
                f"{bc} IP block{'s' if bc != 1 else ''}. "
                "Reassign or pass force=true to detach (FK ON DELETE SET NULL)."
            ),
        )
    name = v.name
    db.add(
        _audit(
            current_user,
            "delete",
            str(v.id),
            name,
            old_value={
                "name": name,
                "route_distinguisher": v.route_distinguisher,
                "linked_spaces": sc,
                "linked_blocks": bc,
                "force": bool(force and (sc or bc)),
            },
        )
    )
    await db.delete(v)
    await db.commit()


@router.post("/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_vrfs(
    body: BulkDeleteRequest, current_user: CurrentUser, db: DB
) -> BulkDeleteResponse:
    """Bulk-delete VRFs.

    Without ``force``, refuses any VRF with a linked IPSpace or
    IPBlock and returns the refusal in ``refused``. With ``force``,
    every linked row's ``vrf_id`` falls back to NULL via the FK's
    ``ON DELETE SET NULL`` clause.
    """
    if not body.ids:
        return BulkDeleteResponse(
            deleted=0, detached_spaces=0, detached_blocks=0, not_found=[], refused=[]
        )
    if len(body.ids) > _BULK_DELETE_CAP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"bulk-delete capped at {_BULK_DELETE_CAP} ids per call",
        )
    rows = list((await db.execute(select(VRF).where(VRF.id.in_(body.ids)))).scalars().all())
    found_ids = {r.id for r in rows}
    not_found = [vid for vid in body.ids if vid not in found_ids]

    counts = await _fetch_counts(db, list(found_ids))
    deleted = 0
    detached_spaces = 0
    detached_blocks = 0
    refused: list[dict[str, Any]] = []
    for v in rows:
        sc, bc = counts.get(v.id, (0, 0))
        if (sc or bc) and not body.force:
            refused.append(
                {
                    "id": str(v.id),
                    "name": v.name,
                    "linked_spaces": sc,
                    "linked_blocks": bc,
                }
            )
            continue
        detached_spaces += sc
        detached_blocks += bc
        db.add(
            _audit(
                current_user,
                "delete",
                str(v.id),
                v.name,
                old_value={
                    "name": v.name,
                    "linked_spaces": sc,
                    "linked_blocks": bc,
                    "force": bool(body.force and (sc or bc)),
                    "bulk": True,
                },
            )
        )
        await db.delete(v)
        deleted += 1
    await db.commit()
    return BulkDeleteResponse(
        deleted=deleted,
        detached_spaces=detached_spaces,
        detached_blocks=detached_blocks,
        not_found=not_found,
        refused=refused,
    )
