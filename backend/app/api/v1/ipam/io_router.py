"""IPAM import / export endpoints — thin wrappers around ``app.services.ipam_io``."""

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response

from app.api.deps import DB, CurrentUser
from app.services.ipam_io import (
    commit_address_import,
    commit_import,
    export_subtree,
    parse_payload,
    preview_address_import,
    preview_import,
)

logger = structlog.get_logger(__name__)
router = APIRouter()


# Upload size guard: 25 MB is plenty for IPAM imports.
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
        )
    return data


@router.post("/import/preview")
async def import_preview(
    current_user: CurrentUser,
    db: DB,
    file: UploadFile = File(...),
    space_id: uuid.UUID | None = Form(default=None),
    space_name: str | None = Form(default=None),
    strategy: Literal["skip", "overwrite", "fail"] = Form(default="fail"),
) -> dict:
    """Dry-run an import and return the would-create / would-update / conflict diff."""
    data = await _read_upload(file)
    payload = parse_payload(data, file.filename or "", file.content_type)
    preview = await preview_import(
        db,
        payload,
        space_id=space_id,
        space_name=space_name,
        strategy=strategy,
    )
    logger.info(
        "ipam_import_preview",
        space_id=preview.space_id,
        creates=len(preview.creates),
        updates=len(preview.updates),
        conflicts=len(preview.conflicts),
        errors=len(preview.errors),
        user=current_user.display_name,
    )
    return preview.as_dict()


@router.post("/import/commit")
async def import_commit(
    current_user: CurrentUser,
    db: DB,
    file: UploadFile = File(...),
    space_id: uuid.UUID | None = Form(default=None),
    space_name: str | None = Form(default=None),
    strategy: Literal["skip", "overwrite", "fail"] = Form(default="fail"),
) -> dict:
    """Commit the import in a single transaction. Writes audit entries per mutation."""
    data = await _read_upload(file)
    payload = parse_payload(data, file.filename or "", file.content_type)
    result = await commit_import(
        db,
        payload,
        current_user=current_user,
        space_id=space_id,
        space_name=space_name,
        strategy=strategy,
    )
    await db.commit()
    return result.as_dict()


@router.post("/import/preview-json")
async def import_preview_json(
    current_user: CurrentUser,
    db: DB,
    body: dict = Body(...),
) -> dict:
    """JSON-body variant of preview — useful for programmatic clients that do not
    want to send multipart/form-data.

    Body shape::

        {
          "space_id": "…" | null,
          "space_name": "…" | null,
          "strategy": "skip" | "overwrite" | "fail",
          "payload": { "subnets": [...] }   // or { "spaces": [...], ... }
        }
    """
    from app.services.ipam_io.parser import ParsedPayload

    raw = body.get("payload")
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Missing 'payload' object")
    parsed = ParsedPayload(
        spaces=list(raw.get("spaces") or []),
        blocks=list(raw.get("blocks") or []),
        subnets=list(raw.get("subnets") or []),
        addresses=list(raw.get("addresses") or []),
    )
    space_id = body.get("space_id")
    preview = await preview_import(
        db,
        parsed,
        space_id=uuid.UUID(space_id) if space_id else None,
        space_name=body.get("space_name"),
        strategy=body.get("strategy", "fail"),
    )
    return preview.as_dict()


@router.post("/import/addresses/preview")
async def import_addresses_preview(
    current_user: CurrentUser,
    db: DB,
    file: UploadFile = File(...),
    subnet_id: uuid.UUID = Form(...),
    strategy: Literal["skip", "overwrite", "fail"] = Form(default="fail"),
) -> dict:
    """Dry-run a subnet-scoped IP address import.

    Accepts CSV / JSON / XLSX with an ``address`` (or ``ip``) column plus
    any of ``hostname``, ``mac_address``, ``description``, ``status``,
    ``tags``, ``custom_fields``. Any unrecognised columns become
    ``custom_fields`` entries so migrations from other DDI tools don't
    need column renaming.
    """
    data = await _read_upload(file)
    payload = parse_payload(data, file.filename or "", file.content_type)
    preview = await preview_address_import(
        db,
        payload,
        subnet_id=subnet_id,
        strategy=strategy,
    )
    logger.info(
        "ipam_address_import_preview",
        subnet_id=str(subnet_id),
        creates=len(preview.creates),
        updates=len(preview.updates),
        conflicts=len(preview.conflicts),
        errors=len(preview.errors),
        user=current_user.display_name,
    )
    return preview.as_dict()


@router.post("/import/addresses/commit")
async def import_addresses_commit(
    current_user: CurrentUser,
    db: DB,
    file: UploadFile = File(...),
    subnet_id: uuid.UUID = Form(...),
    strategy: Literal["skip", "overwrite", "fail"] = Form(default="fail"),
) -> dict:
    """Commit a subnet-scoped IP address import. Same transaction as the
    audit trail it writes — a DNS-sync failure on a single row surfaces
    as a non-fatal error in the response, not a rolled-back import.
    """
    data = await _read_upload(file)
    payload = parse_payload(data, file.filename or "", file.content_type)
    result = await commit_address_import(
        db,
        payload,
        current_user=current_user,
        subnet_id=subnet_id,
        strategy=strategy,
    )
    await db.commit()
    return result.as_dict()


@router.get("/export")
async def export_endpoint(
    current_user: CurrentUser,
    db: DB,
    space_id: uuid.UUID | None = Query(default=None),
    block_id: uuid.UUID | None = Query(default=None),
    subnet_id: uuid.UUID | None = Query(default=None),
    format: Literal["csv", "json", "xlsx"] = Query(default="csv"),
    include_addresses: bool = Query(default=False),
) -> Response:
    """Export a subtree. Exactly one of space_id/block_id/subnet_id must be set."""
    data, content_type, filename = await export_subtree(
        db,
        space_id=space_id,
        block_id=block_id,
        subnet_id=subnet_id,
        format=format,
        include_addresses=include_addresses,
    )
    logger.info(
        "ipam_export",
        space_id=str(space_id) if space_id else None,
        block_id=str(block_id) if block_id else None,
        subnet_id=str(subnet_id) if subnet_id else None,
        format=format,
        bytes=len(data),
        user=current_user.display_name,
    )
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
