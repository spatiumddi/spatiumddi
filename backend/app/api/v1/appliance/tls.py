"""Appliance Web UI certificate management — Phase 4b.1.

Mounted at ``/api/v1/appliance/tls``. Operators paste cert + private
key PEM, the server validates the pair, stores it (key Fernet-encrypted
at rest), and exposes activate / delete. Phase 4b.2 will materialize
the active row into ``/etc/nginx/certs/active.{pem,key}`` and reload
nginx; 4b.1 just persists, so the surface is testable in dev before
the appliance-side wiring lands.

All mutating routes require ``admin`` on ``appliance``; reads accept
``read``. The "Appliance Operator" built-in role grants admin;
superadmin bypasses both.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DB, CurrentUser
from app.core.crypto import encrypt_str
from app.core.permissions import require_permission
from app.models.appliance import (
    CERT_SOURCE_UPLOADED,
    ApplianceCertificate,
)
from app.models.audit import AuditLog
from app.services.appliance.tls import (
    TLSValidationError,
    parse_pem_certificate,
    validate_key_matches_cert,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


# ── Response shapes ─────────────────────────────────────────────────


class CertificateSummary(BaseModel):
    """Cert metadata only — never includes cert_pem or the key.

    Used for list responses where we don't want to ship the full PEM
    for every row. The single-cert GET returns the cert PEM as a
    separate field (key still never leaves the server).
    """

    id: uuid.UUID
    name: str
    source: str
    is_active: bool
    activated_at: datetime | None
    subject_cn: str
    issuer_cn: str
    sans: list[str]
    fingerprint_sha256: str
    valid_from: datetime
    valid_to: datetime
    notes: str | None
    created_at: datetime
    created_by_user_id: uuid.UUID | None


class CertificateDetail(CertificateSummary):
    cert_pem: str


def _to_summary(row: ApplianceCertificate) -> CertificateSummary:
    return CertificateSummary(
        id=row.id,
        name=row.name,
        source=row.source,
        is_active=row.is_active,
        activated_at=row.activated_at,
        subject_cn=row.subject_cn,
        issuer_cn=row.issuer_cn,
        sans=row.sans_json,
        fingerprint_sha256=row.fingerprint_sha256,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        notes=row.notes,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
    )


# ── Request bodies ──────────────────────────────────────────────────


class CertificateUpload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    cert_pem: str = Field(min_length=1)
    key_pem: str = Field(min_length=1)
    notes: str | None = Field(default=None, max_length=2000)
    # If true, the new cert becomes the active one (clears all other
    # rows' is_active flag). If false, it lands inactive — operator
    # has to hit /activate to flip it. Default true since the most
    # common path is "I uploaded a renewal, use it now".
    activate: bool = True


# ── Endpoints ───────────────────────────────────────────────────────


@router.get(
    "",
    response_model=list[CertificateSummary],
    dependencies=[Depends(require_permission("read", "appliance"))],
    summary="List Web UI certificates",
)
async def list_certificates(db: DB) -> list[CertificateSummary]:
    result = await db.execute(
        select(ApplianceCertificate).order_by(
            ApplianceCertificate.is_active.desc(),
            ApplianceCertificate.created_at.desc(),
        )
    )
    return [_to_summary(row) for row in result.scalars().all()]


@router.get(
    "/{cert_id:uuid}",
    response_model=CertificateDetail,
    dependencies=[Depends(require_permission("read", "appliance"))],
    summary="Get one certificate (with PEM, never key)",
)
async def get_certificate(cert_id: uuid.UUID, db: DB) -> CertificateDetail:
    row = await db.get(ApplianceCertificate, cert_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "certificate not found")
    summary = _to_summary(row)
    return CertificateDetail(**summary.model_dump(), cert_pem=row.cert_pem)


@router.post(
    "/upload",
    response_model=CertificateSummary,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Upload a PEM certificate + private key",
)
async def upload_certificate(
    body: CertificateUpload,
    db: DB,
    user: CurrentUser,
) -> CertificateSummary:
    """Parse, validate, store. 422 on any parse / mismatch failure.

    The key is Fernet-encrypted before persistence. The cert PEM is
    stored verbatim — public material, no need to encrypt.
    """
    try:
        info = parse_pem_certificate(body.cert_pem)
        validate_key_matches_cert(body.cert_pem, body.key_pem)
    except TLSValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    # Name uniqueness — separate check so we return a clean 409 rather
    # than letting the unique constraint surface as a generic 500.
    existing = await db.execute(
        select(ApplianceCertificate).where(ApplianceCertificate.name == body.name)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"a certificate named '{body.name}' already exists",
        )

    row = ApplianceCertificate(
        name=body.name,
        source=CERT_SOURCE_UPLOADED,
        cert_pem=body.cert_pem,
        key_encrypted=encrypt_str(body.key_pem),
        is_active=False,
        subject_cn=info.subject_cn,
        sans_json=info.sans,
        issuer_cn=info.issuer_cn,
        fingerprint_sha256=info.fingerprint_sha256,
        valid_from=info.valid_from,
        valid_to=info.valid_to,
        notes=body.notes,
        created_by_user_id=user.id,
    )
    db.add(row)
    await db.flush()

    if body.activate:
        await _activate_only(db, row)

    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="create",
            resource_type="appliance_certificate",
            resource_id=str(row.id),
            resource_display=row.name,
            new_value={
                "name": row.name,
                "source": row.source,
                "subject_cn": row.subject_cn,
                "fingerprint_sha256": row.fingerprint_sha256,
                "valid_to": row.valid_to.isoformat(),
                "is_active": row.is_active,
            },
            result="success",
        )
    )
    await db.commit()
    await db.refresh(row)
    logger.info(
        "appliance_cert_uploaded",
        cert_id=str(row.id),
        name=row.name,
        subject_cn=row.subject_cn,
        is_active=row.is_active,
    )
    return _to_summary(row)


@router.post(
    "/{cert_id:uuid}/activate",
    response_model=CertificateSummary,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Make this certificate the active one",
)
async def activate_certificate(
    cert_id: uuid.UUID,
    db: DB,
    user: CurrentUser,
) -> CertificateSummary:
    row = await db.get(ApplianceCertificate, cert_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "certificate not found")

    # Refuse to activate an expired cert — nginx would happily serve
    # it but every client would refuse the connection, and the
    # operator wouldn't see why until they tried to load the UI. A
    # 422 with a clear message is friendlier.
    if row.valid_to < datetime.now(tz=row.valid_to.tzinfo):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"certificate expired on {row.valid_to.isoformat()}; upload a renewal first",
        )

    await _activate_only(db, row)
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="activate_certificate",
            resource_type="appliance_certificate",
            resource_id=str(row.id),
            resource_display=row.name,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(row)
    logger.info("appliance_cert_activated", cert_id=str(row.id), name=row.name)
    return _to_summary(row)


@router.delete(
    "/{cert_id:uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Delete a certificate",
)
async def delete_certificate(
    cert_id: uuid.UUID,
    db: DB,
    user: CurrentUser,
) -> None:
    row = await db.get(ApplianceCertificate, cert_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "certificate not found")
    if row.is_active:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this certificate is currently active — activate a different one first",
        )

    name = row.name
    await db.delete(row)
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="delete",
            resource_type="appliance_certificate",
            resource_id=str(cert_id),
            resource_display=name,
            result="success",
        )
    )
    await db.commit()
    logger.info("appliance_cert_deleted", cert_id=str(cert_id), name=name)


# ── Internal helpers ────────────────────────────────────────────────


async def _activate_only(db: DB, target: ApplianceCertificate) -> None:
    """Flip ``is_active=true`` on ``target``, false on everything else.

    Done in one transaction so we never have two active rows or zero
    active rows visible to a concurrent read. Caller commits.
    """
    from sqlalchemy import update

    now = datetime.now(tz=target.valid_to.tzinfo)
    await db.execute(
        update(ApplianceCertificate)
        .where(ApplianceCertificate.id != target.id)
        .values(is_active=False)
    )
    target.is_active = True
    target.activated_at = now
