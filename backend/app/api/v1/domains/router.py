"""Domain registration tracking — CRUD + synchronous WHOIS refresh.

Mounted at ``/api/v1/domains``. All endpoints gate on
``manage_domains`` (admin-only). Distinct from ``/api/v1/dns/zones``:
this surface tracks the registration side (registrar, expiry,
nameservers as advertised by the registry) versus the records the
operator serves.

The scheduled refresh (``app.tasks.domain_whois_refresh``) and the
alert-rule types listed in issue #87 are deferred to follow-up PRs
so cadence + rate-limits can be tuned in isolation. The synchronous
``POST /domains/{id}/refresh-whois`` endpoint here is enough to
exercise the full RDAP path on demand.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select

from app.api.deps import DB, CurrentUser
from app.core.permissions import require_permission, user_has_permission
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.domain import Domain
from app.models.settings import PlatformSettings
from app.services.domain_refresh import build_refresh_audit_payload, refresh_one_domain
from app.services.rdap import compute_nameserver_drift, derive_whois_state

logger = structlog.get_logger(__name__)

PERMISSION = "manage_domains"

router = APIRouter(
    tags=["domains"],
    dependencies=[Depends(require_permission("read", PERMISSION))],
)

# ── Constants ───────────────────────────────────────────────────────

# Hard cap on the bulk-delete payload to keep the endpoint from
# becoming a denial-of-service vector. Mirrors the cap used elsewhere
# (nmap scans, etc).
_BULK_DELETE_CAP = 500

_VALID_WHOIS_STATES = frozenset({"ok", "drift", "expiring", "expired", "unreachable", "unknown"})


# ── Schemas ─────────────────────────────────────────────────────────


def _normalize_name(raw: str) -> str:
    """Lowercase + trailing-dot strip + whitespace trim.

    The DB unique index keys off ``name`` directly, so callers must
    not be allowed to insert two rows for ``Example.COM.`` and
    ``example.com``. Empty string is rejected by the field validator.
    """
    if not isinstance(raw, str):
        raise ValueError("name must be a string")
    cleaned = raw.strip().rstrip(".").lower()
    if not cleaned:
        raise ValueError("name must not be empty")
    return cleaned


def _normalize_nameservers(values: list[str] | None) -> list[str]:
    """Same normalisation as :func:`_normalize_name`, applied to every
    expected-NS entry. De-duplicates while preserving sorted order
    so equality checks against ``actual_nameservers`` are stable.
    """
    if not values:
        return []
    out: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            continue
        s = v.strip().rstrip(".").lower()
        if s:
            out.add(s)
    return sorted(out)


class DomainCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    expected_nameservers: list[str] = Field(default_factory=list)
    tags: dict[str, Any] = Field(default_factory=dict)
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        return _normalize_name(v)

    @field_validator("expected_nameservers")
    @classmethod
    def _v_ns(cls, v: list[str]) -> list[str]:
        return _normalize_nameservers(v)


class DomainUpdate(BaseModel):
    # Name is editable — operators occasionally need to fix the apex
    # they registered the row under (typo, transfer to a different
    # apex). Uniqueness check happens in the handler.
    name: str | None = Field(None, min_length=1, max_length=255)
    expected_nameservers: list[str] | None = None
    tags: dict[str, Any] | None = None
    custom_fields: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _normalize_name(v)

    @field_validator("expected_nameservers")
    @classmethod
    def _v_ns(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return _normalize_nameservers(v)


class DomainRead(BaseModel):
    id: uuid.UUID
    name: str
    registrar: str | None
    registrant_org: str | None
    registered_at: datetime | None
    expires_at: datetime | None
    last_renewed_at: datetime | None
    expected_nameservers: list[str]
    actual_nameservers: list[str]
    nameserver_drift: bool
    dnssec_signed: bool
    whois_last_checked_at: datetime | None
    whois_state: str
    whois_data: dict[str, Any] | None
    next_check_at: datetime | None
    tags: dict[str, Any]
    custom_fields: dict[str, Any]
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


class DomainListResponse(BaseModel):
    items: list[DomainRead]
    total: int
    page: int
    page_size: int


class BulkDeleteRequest(BaseModel):
    ids: list[uuid.UUID] = Field(..., max_length=_BULK_DELETE_CAP)


class BulkDeleteResponse(BaseModel):
    deleted: int


# ── Pure helpers ────────────────────────────────────────────────────


def _audit(
    db: Any,
    *,
    user: User | None,
    action: str,
    domain_id: uuid.UUID,
    domain_name: str,
    changed_fields: list[str] | None = None,
    new_value: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            user_display_name=user.display_name if user else "system",
            auth_source=getattr(user, "auth_source", "local") or "local",
            action=action,
            resource_type="domain",
            resource_id=str(domain_id),
            resource_display=domain_name,
            changed_fields=changed_fields,
            new_value=new_value,
        )
    )


def _to_read(d: Domain) -> DomainRead:
    return DomainRead(
        id=d.id,
        name=d.name,
        registrar=d.registrar,
        registrant_org=d.registrant_org,
        registered_at=d.registered_at,
        expires_at=d.expires_at,
        last_renewed_at=d.last_renewed_at,
        expected_nameservers=list(d.expected_nameservers or []),
        actual_nameservers=list(d.actual_nameservers or []),
        nameserver_drift=bool(d.nameserver_drift),
        dnssec_signed=bool(d.dnssec_signed),
        whois_last_checked_at=d.whois_last_checked_at,
        whois_state=d.whois_state,
        whois_data=d.whois_data,
        next_check_at=d.next_check_at,
        tags=dict(d.tags or {}),
        custom_fields=dict(d.custom_fields or {}),
        created_at=d.created_at,
        modified_at=d.modified_at,
    )


# ── Endpoints ───────────────────────────────────────────────────────


@router.get("/domains", response_model=DomainListResponse)
async def list_domains(
    db: DB,
    current_user: CurrentUser,
    whois_state: str | None = Query(None),
    expiring_within_days: int | None = Query(None, ge=0, le=3650),
    search: str | None = Query(None, min_length=1, max_length=255),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> DomainListResponse:
    if not user_has_permission(current_user, "read", PERMISSION):
        raise HTTPException(status_code=403, detail="Permission denied")

    if whois_state is not None and whois_state not in _VALID_WHOIS_STATES:
        raise HTTPException(
            status_code=422,
            detail=f"whois_state must be one of: {sorted(_VALID_WHOIS_STATES)}",
        )

    base = select(Domain)
    if whois_state is not None:
        base = base.where(Domain.whois_state == whois_state)
    if expiring_within_days is not None:
        cutoff = datetime.now(UTC) + timedelta(days=expiring_within_days)
        base = base.where(Domain.expires_at.is_not(None)).where(Domain.expires_at <= cutoff)
    if search:
        # Substring match on name or registrar — registrar can be NULL
        # so the OR has to tolerate that. Postgres ``ILIKE`` keeps it
        # case-insensitive without forcing the caller to lowercase.
        like = f"%{search.strip().lower()}%"
        base = base.where(
            or_(
                func.lower(Domain.name).like(like),
                func.lower(func.coalesce(Domain.registrar, "")).like(like),
            )
        )

    count_stmt = select(func.count()).select_from(base.subquery())
    total = int((await db.execute(count_stmt)).scalar_one())

    stmt = base.order_by(Domain.name).limit(page_size).offset((page - 1) * page_size)
    rows = list((await db.execute(stmt)).scalars().all())
    return DomainListResponse(
        items=[_to_read(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/domains",
    response_model=DomainRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_domain(body: DomainCreate, db: DB, current_user: CurrentUser) -> DomainRead:
    if not user_has_permission(current_user, "write", PERMISSION):
        raise HTTPException(status_code=403, detail="Permission denied")

    existing = await db.execute(select(Domain).where(Domain.name == body.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="A domain with that name already exists")

    d = Domain(
        name=body.name,
        expected_nameservers=body.expected_nameservers,
        tags=body.tags or {},
        custom_fields=body.custom_fields or {},
    )
    db.add(d)
    await db.flush()

    _audit(
        db,
        user=current_user,
        action="create",
        domain_id=d.id,
        domain_name=d.name,
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(d)
    return _to_read(d)


@router.get("/domains/{domain_id}", response_model=DomainRead)
async def get_domain(domain_id: uuid.UUID, db: DB, current_user: CurrentUser) -> DomainRead:
    if not user_has_permission(current_user, "read", PERMISSION):
        raise HTTPException(status_code=403, detail="Permission denied")
    d = await db.get(Domain, domain_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Domain not found")
    return _to_read(d)


@router.put("/domains/{domain_id}", response_model=DomainRead)
async def update_domain(
    domain_id: uuid.UUID,
    body: DomainUpdate,
    db: DB,
    current_user: CurrentUser,
) -> DomainRead:
    if not user_has_permission(current_user, "write", PERMISSION):
        raise HTTPException(status_code=403, detail="Permission denied")

    d = await db.get(Domain, domain_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Domain not found")

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return _to_read(d)

    # Name uniqueness — only check when it actually changed.
    if "name" in changes and changes["name"] != d.name:
        existing = await db.execute(
            select(Domain.id).where(Domain.name == changes["name"], Domain.id != d.id)
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="A domain with that name already exists")

    for k, v in changes.items():
        setattr(d, k, v)

    # If expected_nameservers changed AND we already have an
    # actual_nameservers snapshot, recompute drift on the fly so the
    # list-page badge reflects the operator's edit immediately —
    # without forcing a full RDAP round trip.
    if "expected_nameservers" in changes and d.actual_nameservers is not None:
        d.nameserver_drift = compute_nameserver_drift(
            d.expected_nameservers, d.actual_nameservers
        )
        # And refresh the derived state label (preserving expiry
        # buckets if applicable).
        d.whois_state = derive_whois_state(
            rdap_returned_data=d.whois_last_checked_at is not None,
            expires_at=d.expires_at,
            expected_nameservers=d.expected_nameservers,
            actual_nameservers=d.actual_nameservers or [],
        )

    _audit(
        db,
        user=current_user,
        action="update",
        domain_id=d.id,
        domain_name=d.name,
        changed_fields=list(changes.keys()),
        new_value=body.model_dump(mode="json", exclude_unset=True),
    )
    await db.commit()
    await db.refresh(d)
    return _to_read(d)


@router.delete("/domains/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_domain(domain_id: uuid.UUID, db: DB, current_user: CurrentUser) -> None:
    if not user_has_permission(current_user, "delete", PERMISSION):
        raise HTTPException(status_code=403, detail="Permission denied")
    d = await db.get(Domain, domain_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Domain not found")
    _audit(
        db,
        user=current_user,
        action="delete",
        domain_id=d.id,
        domain_name=d.name,
    )
    await db.delete(d)
    await db.commit()


@router.post(
    "/domains/{domain_id}/refresh-whois",
    response_model=DomainRead,
)
async def refresh_whois(
    domain_id: uuid.UUID,
    db: DB,
    current_user: CurrentUser,
) -> DomainRead:
    """Synchronously hit RDAP for this domain, write the response back,
    and return the updated row.

    Treats ``lookup_domain`` returning ``None`` as the "unreachable"
    bucket — we still update ``whois_last_checked_at`` so operators
    see when we last tried (and the deferred scheduled task can
    use that for rate-limit pacing).
    """
    if not user_has_permission(current_user, "write", PERMISSION):
        raise HTTPException(status_code=403, detail="Permission denied")

    d = await db.get(Domain, domain_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Domain not found")

    # Push the next scheduled poll out by the configured cadence so a
    # manual refresh + the beat tick don't double-poll the registry
    # back-to-back.
    ps = await db.get(PlatformSettings, 1)
    interval_hours = (
        ps.domain_whois_interval_hours
        if ps is not None and ps.domain_whois_interval_hours
        else 24
    )

    result = await refresh_one_domain(d, interval_hours=interval_hours)

    _audit(
        db,
        user=current_user,
        action="refresh_whois",
        domain_id=d.id,
        domain_name=d.name,
        new_value=build_refresh_audit_payload(d, result),
    )
    await db.commit()
    await db.refresh(d)
    return _to_read(d)


@router.post("/domains/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_domains(
    body: BulkDeleteRequest,
    db: DB,
    current_user: CurrentUser,
) -> BulkDeleteResponse:
    if not user_has_permission(current_user, "delete", PERMISSION):
        raise HTTPException(status_code=403, detail="Permission denied")
    if not body.ids:
        return BulkDeleteResponse(deleted=0)
    if len(body.ids) > _BULK_DELETE_CAP:
        # Pydantic's ``max_length`` already rejects this with a 422 —
        # this branch is defensive in case the client sends exactly
        # the cap and we want a cleaner message later.
        raise HTTPException(
            status_code=422,
            detail=f"bulk-delete cap is {_BULK_DELETE_CAP} ids per call",
        )

    rows_res = await db.execute(select(Domain).where(Domain.id.in_(body.ids)))
    rows = list(rows_res.scalars().all())
    if not rows:
        return BulkDeleteResponse(deleted=0)

    for d in rows:
        _audit(
            db,
            user=current_user,
            action="delete",
            domain_id=d.id,
            domain_name=d.name,
        )

    await db.execute(sa_delete(Domain).where(Domain.id.in_([d.id for d in rows])))
    await db.commit()
    return BulkDeleteResponse(deleted=len(rows))


__all__ = ["router", "derive_whois_state"]
