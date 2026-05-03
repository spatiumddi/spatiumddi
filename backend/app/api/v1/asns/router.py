"""ASN management CRUD — Phase 1 of issue #85.

The autonomous-system row is the foundation; the RDAP refresh job,
RPKI ROA pull job, dashboard summary widget, alert rules, and the
BGP-relationship FKs on Router / IPSpace / IPBlock / VRF all land in
follow-up issues. This router ships only the writeable AS surface
plus list filtering — everything else can layer on without breaking
the API contract.

Permissions: every endpoint is gated on the ``manage_asns``
resource permission (admin-only via the seeded ``Network Editor``
builtin role; superadmins always pass). Each mutation writes an
``audit_log`` row before commit per CLAUDE.md non-negotiable #4.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String, func, or_, select

from app.api.deps import DB, CurrentUser
from app.api.v1.asns._audit import write_audit
from app.core.permissions import require_resource_permission
from app.models.asn import ASN
from app.services.asns.classifier import REGISTRIES, classify_asn

# Router-level permission gate — covers GET (read), POST/PUT (write),
# DELETE on every endpoint mounted below.
router = APIRouter(
    tags=["asns"],
    dependencies=[Depends(require_resource_permission("manage_asns"))],
)


# ── Pydantic schemas ─────────────────────────────────────────────────


# 1..2_147_483_647 fits in a signed int32 (16-bit AS range plus the
# lower half of 32-bit AS space); 2_147_483_648..4_294_967_295 needs
# a Python int — Pydantic v2 happily accepts either when the field
# is typed ``int``. Range validation is in the validator below so
# the error message lists the valid bounds in one place.
_AS_MIN = 1
_AS_MAX = 4_294_967_295

_WHOIS_STATES = frozenset({"ok", "drift", "unreachable", "n/a"})


class ASNCreate(BaseModel):
    number: int = Field(..., description="32-bit AS number (1..4_294_967_295)")
    name: str = Field("", max_length=255)
    description: str = ""
    holder_org: str | None = Field(default=None, max_length=512)
    tags: dict[str, Any] = Field(default_factory=dict)
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("number")
    @classmethod
    def _v_number(cls, v: int) -> int:
        if not (_AS_MIN <= v <= _AS_MAX):
            raise ValueError(
                f"AS number must be between {_AS_MIN} and {_AS_MAX} (32-bit range; "
                "0 is reserved per RFC 7607 and not allowed)"
            )
        return v


class ASNUpdate(BaseModel):
    """Partial update.

    ``number`` is immutable — a different AS is a different row, full
    stop, and changing it would invalidate every cached classification
    + WHOIS snapshot. ``kind`` and ``registry`` are derived; rejecting
    edits on them keeps the data clean. ``whois_*`` is owned by the
    refresh job — surfaced read-only here.
    """

    name: str | None = None
    description: str | None = None
    holder_org: str | None = None
    tags: dict[str, Any] | None = None
    custom_fields: dict[str, Any] | None = None


class ASNRead(BaseModel):
    id: uuid.UUID
    number: int
    name: str
    description: str
    kind: str
    holder_org: str | None
    registry: str
    whois_last_checked_at: datetime | None
    whois_data: dict[str, Any] | None
    whois_state: str
    tags: dict[str, Any]
    custom_fields: dict[str, Any]
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


class ASNListResponse(BaseModel):
    items: list[ASNRead]
    total: int
    limit: int
    offset: int


class ASNBulkDelete(BaseModel):
    ids: list[uuid.UUID] = Field(..., max_length=500)


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("", response_model=ASNListResponse)
async def list_asns(
    db: DB,
    _: CurrentUser,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    kind: Literal["public", "private"] | None = Query(default=None),
    registry: str | None = Query(default=None, description="RIR code, or `unknown`"),
    whois_state: Literal["ok", "drift", "unreachable", "n/a"] | None = Query(default=None),
    search: str | None = Query(
        default=None,
        description="Free-text match against number / name / holder_org (case-insensitive substring).",
    ),
) -> ASNListResponse:
    if registry is not None and registry not in REGISTRIES:
        raise HTTPException(
            status_code=422,
            detail=f"registry must be one of {sorted(REGISTRIES)}",
        )

    stmt = select(ASN)
    if kind is not None:
        stmt = stmt.where(ASN.kind == kind)
    if registry is not None:
        stmt = stmt.where(ASN.registry == registry)
    if whois_state is not None:
        stmt = stmt.where(ASN.whois_state == whois_state)
    if search:
        # Case-insensitive substring on the three operator-facing
        # fields. ``number`` is BigInteger so we cast to text for the
        # ILIKE match — small price for not having to remember whether
        # to type "AS65001" or just "65001" in the filter box.
        needle = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                func.cast(ASN.number, type_=String).ilike(needle),
                ASN.name.ilike(needle),
                ASN.holder_org.ilike(needle),
            )
        )

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    stmt = stmt.order_by(ASN.number.asc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()

    return ASNListResponse(
        items=[ASNRead.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=ASNRead, status_code=status.HTTP_201_CREATED)
async def create_asn(body: ASNCreate, db: DB, user: CurrentUser) -> ASNRead:
    existing = await db.scalar(select(ASN).where(ASN.number == body.number))
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"AS{body.number} is already tracked (id={existing.id})",
        )

    kind, registry = classify_asn(body.number)
    # Public rows land as ``n/a`` until the RDAP refresh job (follow-up
    # issue) flips them to ``ok`` / ``drift`` / ``unreachable``. Private
    # rows stay ``n/a`` permanently — the refresh job skips them.
    whois_state = "n/a"

    row = ASN(
        number=body.number,
        name=body.name,
        description=body.description,
        kind=kind,
        registry=registry,
        holder_org=body.holder_org,
        whois_state=whois_state,
        tags=body.tags or {},
        custom_fields=body.custom_fields or {},
    )
    db.add(row)
    await db.flush()

    write_audit(
        db,
        user=user,
        action="create",
        resource_type="asn",
        resource_id=str(row.id),
        resource_display=f"AS{row.number}" + (f" ({row.name})" if row.name else ""),
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(row)
    return ASNRead.model_validate(row)


@router.get("/{asn_id}", response_model=ASNRead)
async def get_asn(asn_id: uuid.UUID, db: DB, _: CurrentUser) -> ASNRead:
    row = await db.get(ASN, asn_id)
    if row is None:
        raise HTTPException(status_code=404, detail="ASN not found")
    return ASNRead.model_validate(row)


@router.put("/{asn_id}", response_model=ASNRead)
async def update_asn(asn_id: uuid.UUID, body: ASNUpdate, db: DB, user: CurrentUser) -> ASNRead:
    row = await db.get(ASN, asn_id)
    if row is None:
        raise HTTPException(status_code=404, detail="ASN not found")

    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(row, k, v)

    write_audit(
        db,
        user=user,
        action="update",
        resource_type="asn",
        resource_id=str(row.id),
        resource_display=f"AS{row.number}",
        changed_fields=list(changes.keys()),
        new_value=body.model_dump(mode="json", exclude_unset=True),
    )
    await db.commit()
    await db.refresh(row)
    return ASNRead.model_validate(row)


@router.delete("/{asn_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asn(asn_id: uuid.UUID, db: DB, user: CurrentUser) -> None:
    row = await db.get(ASN, asn_id)
    if row is None:
        raise HTTPException(status_code=404, detail="ASN not found")

    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="asn",
        resource_id=str(row.id),
        resource_display=f"AS{row.number}",
    )
    # CASCADE removes ``asn_rpki_roa`` rows; the BGP-relationship FKs
    # land in follow-ups with their own ON-DELETE policy decisions.
    await db.delete(row)
    await db.commit()


@router.post("/bulk-delete")
async def bulk_delete_asns(body: ASNBulkDelete, db: DB, user: CurrentUser) -> dict[str, Any]:
    """Delete up to 500 ASNs in a single round-trip.

    Returns a small summary so the UI can render a bulk-results
    modal — same shape used by other bulk endpoints in the project.
    Missing IDs (already deleted, or never existed) come back in
    ``not_found`` rather than 404-ing the whole call so the operator
    isn't punished for a partial selection.
    """
    if not body.ids:
        return {"deleted": 0, "not_found": []}

    rows = (await db.execute(select(ASN).where(ASN.id.in_(body.ids)))).scalars().all()
    found_ids = {r.id for r in rows}
    not_found = [str(i) for i in body.ids if i not in found_ids]

    for r in rows:
        write_audit(
            db,
            user=user,
            action="delete",
            resource_type="asn",
            resource_id=str(r.id),
            resource_display=f"AS{r.number}",
        )
        await db.delete(r)

    await db.commit()
    return {"deleted": len(rows), "not_found": not_found}


__all__ = ["router"]
