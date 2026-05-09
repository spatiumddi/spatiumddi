"""DNS configuration importer endpoints — preview + commit per source.

Phase 1 ships ``/dns/import/bind9/{preview,commit}``; Phase 2 + 3
add ``/dns/import/windows-dns/...`` and ``/dns/import/powerdns/...``
under the same shape (multipart upload + JSON commit body).

The split between preview (multipart) and commit (JSON body
carrying the previewed plan) means we don't re-upload the archive
on commit. The operator-edited per-zone conflict actions ride in
the commit body too, so the server stays stateless between the
two calls.
"""

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DB, SuperAdmin
from app.models.dns import DNSServer, DNSServerGroup
from app.services.dns_import import (
    CommitResult,
    ImportSourceError,
    WindowsDNSImportError,
    parse_bind9_archive,
    parse_windows_dns_server,
)
from app.services.dns_import.canonical import (
    ConflictAction,
    ImportedRecord,
    ImportedSOA,
    ImportedZone,
    ImportPreview,
    ZoneConflict,
)
from app.services.dns_import.commit import commit_import, detect_conflicts

logger = structlog.get_logger(__name__)
router = APIRouter()

# Match the BIND9 parser's archive cap so the multipart upload guard
# fails fast before unpack.
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


# ── Pydantic IO models ───────────────────────────────────────────────


class ImportedRecordOut(BaseModel):
    name: str
    record_type: str
    value: str
    ttl: int | None = None
    priority: int | None = None
    weight: int | None = None
    port: int | None = None


class ImportedSOAOut(BaseModel):
    primary_ns: str
    admin_email: str
    serial: int
    refresh: int
    retry: int
    expire: int
    minimum: int
    ttl: int


class ImportedZoneOut(BaseModel):
    name: str
    zone_type: str
    kind: str
    soa: ImportedSOAOut | None
    records: list[ImportedRecordOut]
    view_name: str | None = None
    forwarders: list[str] = Field(default_factory=list)
    skipped_record_types: dict[str, int] = Field(default_factory=dict)
    parse_warnings: list[str] = Field(default_factory=list)


class ZoneConflictOut(BaseModel):
    zone_name: str
    existing_zone_id: str
    existing_record_count: int
    action: Literal["skip", "overwrite", "rename"] = "skip"
    rename_to: str | None = None


class PreviewOut(BaseModel):
    """Preview response shape — also the commit request payload's
    ``plan`` field, so the UI hands back the same shape it received."""

    source: Literal["bind9", "windows_dns", "powerdns"]
    zones: list[ImportedZoneOut]
    conflicts: list[ZoneConflictOut]
    warnings: list[str]
    total_records: int
    record_type_histogram: dict[str, int]


class ConflictDecision(BaseModel):
    """Per-zone strategy from the operator."""

    action: Literal["skip", "overwrite", "rename"]
    rename_to: str | None = None


class CommitIn(BaseModel):
    target_group_id: uuid.UUID
    target_view_id: uuid.UUID | None = None
    plan: PreviewOut
    # Keyed by ImportedZone.name (FQDN as parsed). Zones the operator
    # left untouched can be omitted; the commit defaults them to
    # skip-on-conflict / create-otherwise.
    conflict_actions: dict[str, ConflictDecision] = Field(default_factory=dict)


class WindowsDNSPreviewIn(BaseModel):
    """Body shape for ``POST /dns/import/windows-dns/preview``.

    Unlike BIND9 (which takes a multipart upload), Windows DNS is a
    live pull — the operator picks a pre-registered Windows DNS
    server row and the server pulls zones + records over WinRM.
    """

    server_id: uuid.UUID
    target_group_id: uuid.UUID
    target_view_id: uuid.UUID | None = None


class WindowsDNSServerOption(BaseModel):
    """One row in the windows_dns server picker — drives the UI's
    server dropdown (filtered to ``driver=windows_dns`` rows that
    have credentials configured)."""

    id: uuid.UUID
    name: str
    host: str
    group_id: uuid.UUID
    group_name: str
    has_credentials: bool


class CommitZoneOut(BaseModel):
    zone_name: str
    action_taken: Literal["created", "overwrote", "renamed", "skipped", "failed"]
    zone_id: str | None = None
    records_created: int = 0
    records_deleted: int = 0
    error: str | None = None


class CommitOut(BaseModel):
    target_group_id: uuid.UUID
    zones: list[CommitZoneOut]
    warnings: list[str]
    total_zones_created: int
    total_zones_overwrote: int
    total_zones_renamed: int
    total_zones_skipped: int
    total_zones_failed: int
    total_records_created: int


# ── Conversion helpers (canonical IR ↔ Pydantic) ─────────────────────


def _zone_to_pydantic(z: ImportedZone) -> ImportedZoneOut:
    return ImportedZoneOut(
        name=z.name,
        zone_type=z.zone_type,
        kind=z.kind,
        soa=ImportedSOAOut(**z.soa.__dict__) if z.soa else None,
        records=[ImportedRecordOut(**r.__dict__) for r in z.records],
        view_name=z.view_name,
        forwarders=list(z.forwarders),
        skipped_record_types=dict(z.skipped_record_types),
        parse_warnings=list(z.parse_warnings),
    )


def _preview_to_pydantic(p: ImportPreview) -> PreviewOut:
    return PreviewOut(
        source=p.source,
        zones=[_zone_to_pydantic(z) for z in p.zones],
        conflicts=[
            ZoneConflictOut(
                zone_name=c.zone_name,
                existing_zone_id=c.existing_zone_id,
                existing_record_count=c.existing_record_count,
                action=c.action,
                rename_to=c.rename_to,
            )
            for c in p.conflicts
        ],
        warnings=list(p.warnings),
        total_records=p.total_records,
        record_type_histogram=dict(p.record_type_histogram),
    )


def _zone_from_pydantic(o: ImportedZoneOut) -> ImportedZone:
    return ImportedZone(
        name=o.name,
        zone_type=o.zone_type,
        kind=o.kind,
        soa=ImportedSOA(**o.soa.model_dump()) if o.soa else None,
        records=[ImportedRecord(**r.model_dump()) for r in o.records],
        view_name=o.view_name,
        forwarders=list(o.forwarders),
        skipped_record_types=dict(o.skipped_record_types),
        parse_warnings=list(o.parse_warnings),
    )


def _preview_from_pydantic(o: PreviewOut) -> ImportPreview:
    return ImportPreview(
        source=o.source,
        zones=[_zone_from_pydantic(z) for z in o.zones],
        conflicts=[
            ZoneConflict(
                zone_name=c.zone_name,
                existing_zone_id=c.existing_zone_id,
                existing_record_count=c.existing_record_count,
                action=c.action,
                rename_to=c.rename_to,
            )
            for c in o.conflicts
        ],
        warnings=list(o.warnings),
        total_records=o.total_records,
        record_type_histogram=dict(o.record_type_histogram),
    )


def _commit_result_to_pydantic(r: CommitResult) -> CommitOut:
    return CommitOut(
        target_group_id=r.target_group_id,
        zones=[
            CommitZoneOut(
                zone_name=z.zone_name,
                action_taken=z.action_taken,  # type: ignore[arg-type]
                zone_id=z.zone_id,
                records_created=z.records_created,
                records_deleted=z.records_deleted,
                error=z.error,
            )
            for z in r.zones
        ],
        warnings=list(r.warnings),
        total_zones_created=r.total_zones_created,
        total_zones_overwrote=r.total_zones_overwrote,
        total_zones_renamed=r.total_zones_renamed,
        total_zones_skipped=r.total_zones_skipped,
        total_zones_failed=r.total_zones_failed,
        total_records_created=r.total_records_created,
    )


# ── Multipart upload guard ───────────────────────────────────────────


async def _read_archive(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
        )
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    return data


# ── Endpoints ────────────────────────────────────────────────────────


@router.post("/bind9/preview", response_model=PreviewOut)
async def bind9_preview(
    current_user: SuperAdmin,
    db: DB,
    file: UploadFile = File(
        ..., description="ZIP or tar(.gz/.bz2/.xz) archive containing named.conf + zone files"
    ),
    target_group_id: uuid.UUID = Form(..., description="DNS server group the import will land in"),
    target_view_id: uuid.UUID | None = Form(default=None),
) -> PreviewOut:
    """Parse the uploaded BIND9 archive and return the would-create
    plan + per-zone conflict status.

    Side-effect-free: no DB writes, no audit row. The operator can
    re-upload as many times as they want while iterating on the
    archive contents. Only the commit endpoint mutates state.
    """

    data = await _read_archive(file)
    try:
        preview = parse_bind9_archive(data)
    except ImportSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Conflict detection runs against the target group + view here so
    # the UI's per-zone strategy picker has accurate data. Re-checked
    # at commit time in case the world moved.
    zone_names = [z.name if z.name.endswith(".") else z.name + "." for z in preview.zones]
    zone_names = [n.lower() for n in zone_names]
    preview.conflicts = await detect_conflicts(
        db,
        zone_names=zone_names,
        target_group_id=target_group_id,
        target_view_id=target_view_id,
    )

    logger.info(
        "dns_import_bind9_preview",
        zone_count=len(preview.zones),
        record_count=preview.total_records,
        conflict_count=len(preview.conflicts),
        warning_count=len(preview.warnings),
        target_group_id=str(target_group_id),
        target_view_id=str(target_view_id) if target_view_id else None,
        user=current_user.display_name,
    )
    return _preview_to_pydantic(preview)


@router.post("/bind9/commit", response_model=CommitOut)
async def bind9_commit(
    current_user: SuperAdmin,
    db: DB,
    body: CommitIn = Body(...),
) -> CommitOut:
    """Apply a previously-previewed BIND9 import.

    Per-zone savepoints — a parse / FK error on zone N rolls back N
    but keeps zones 1..N-1. Each successful zone gets a single
    audit_log row tagged ``import_source=bind9`` in ``new_value``.
    """

    if body.plan.source != "bind9":
        raise HTTPException(
            status_code=400,
            detail=f"Plan source mismatch: endpoint=bind9 plan={body.plan.source}",
        )

    preview = _preview_from_pydantic(body.plan)
    actions: dict[str, tuple[ConflictAction, str | None]] = {
        zone_name: (decision.action, decision.rename_to)
        for zone_name, decision in body.conflict_actions.items()
    }

    try:
        result = await commit_import(
            db,
            preview=preview,
            target_group_id=body.target_group_id,
            target_view_id=body.target_view_id,
            conflict_actions=actions,
            current_user=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    logger.info(
        "dns_import_bind9_commit",
        target_group_id=str(body.target_group_id),
        zones_created=result.total_zones_created,
        zones_overwrote=result.total_zones_overwrote,
        zones_renamed=result.total_zones_renamed,
        zones_skipped=result.total_zones_skipped,
        zones_failed=result.total_zones_failed,
        records_created=result.total_records_created,
        user=current_user.display_name,
    )
    return _commit_result_to_pydantic(result)


# ── Windows DNS endpoints (Phase 2) ──────────────────────────────────


@router.get("/windows-dns/servers", response_model=list[WindowsDNSServerOption])
async def windows_dns_servers(
    _: SuperAdmin,
    db: DB,
) -> list[WindowsDNSServerOption]:
    """List every ``driver=windows_dns`` server with its group, for
    the UI's server picker.

    Returns the server's ``has_credentials`` flag so the picker can
    grey out servers that haven't had WinRM creds configured yet —
    Path B requires them, and the UI shouldn't let the operator
    pick a server we know will fail at preview time.
    """

    rows = (
        await db.execute(
            select(DNSServer, DNSServerGroup)
            .join(DNSServerGroup, DNSServer.group_id == DNSServerGroup.id)
            .where(DNSServer.driver == "windows_dns")
            .order_by(DNSServerGroup.name, DNSServer.name)
        )
    ).all()
    return [
        WindowsDNSServerOption(
            id=server.id,
            name=server.name,
            host=server.host or "",
            group_id=group.id,
            group_name=group.name,
            has_credentials=bool(server.credentials_encrypted),
        )
        for (server, group) in rows
    ]


@router.post("/windows-dns/preview", response_model=PreviewOut)
async def windows_dns_preview(
    current_user: SuperAdmin,
    db: DB,
    body: WindowsDNSPreviewIn = Body(...),
) -> PreviewOut:
    """Live-pull every zone + record from a Windows DNS server.

    Validates the server row + WinRM creds before delegating to
    :func:`parse_windows_dns_server`. The pull blocks until every
    zone's records have been walked — a 50-zone server takes a few
    seconds; a 5000-zone server takes minutes. The UI shows a
    progress spinner during the wait.
    """

    server = (
        await db.execute(select(DNSServer).where(DNSServer.id == body.server_id))
    ).scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404, detail=f"DNS server {body.server_id} not found")
    if server.driver != "windows_dns":
        raise HTTPException(
            status_code=400,
            detail=f"Server {server.name!r} is driver {server.driver!r}; expected windows_dns",
        )
    if not server.credentials_encrypted:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Server {server.name!r} has no WinRM credentials configured. "
                "Add them via the DNS server modal before importing."
            ),
        )

    try:
        preview = await parse_windows_dns_server(server)
    except WindowsDNSImportError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    zone_names = [(z.name if z.name.endswith(".") else z.name + ".").lower() for z in preview.zones]
    preview.conflicts = await detect_conflicts(
        db,
        zone_names=zone_names,
        target_group_id=body.target_group_id,
        target_view_id=body.target_view_id,
    )

    logger.info(
        "dns_import_windows_dns_preview",
        server_id=str(body.server_id),
        zone_count=len(preview.zones),
        record_count=preview.total_records,
        conflict_count=len(preview.conflicts),
        warning_count=len(preview.warnings),
        target_group_id=str(body.target_group_id),
        target_view_id=str(body.target_view_id) if body.target_view_id else None,
        user=current_user.display_name,
    )
    return _preview_to_pydantic(preview)


@router.post("/windows-dns/commit", response_model=CommitOut)
async def windows_dns_commit(
    current_user: SuperAdmin,
    db: DB,
    body: CommitIn = Body(...),
) -> CommitOut:
    """Apply a previously-previewed Windows DNS import.

    Identical pipeline as the BIND9 commit — re-detect conflicts,
    per-zone savepoints, audit log per zone — just dispatched via
    a different endpoint so the operator-side UI is keyed by source.
    """

    if body.plan.source != "windows_dns":
        raise HTTPException(
            status_code=400,
            detail=f"Plan source mismatch: endpoint=windows_dns plan={body.plan.source}",
        )

    preview = _preview_from_pydantic(body.plan)
    actions: dict[str, tuple[ConflictAction, str | None]] = {
        zone_name: (decision.action, decision.rename_to)
        for zone_name, decision in body.conflict_actions.items()
    }

    try:
        result = await commit_import(
            db,
            preview=preview,
            target_group_id=body.target_group_id,
            target_view_id=body.target_view_id,
            conflict_actions=actions,
            current_user=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    logger.info(
        "dns_import_windows_dns_commit",
        target_group_id=str(body.target_group_id),
        zones_created=result.total_zones_created,
        zones_overwrote=result.total_zones_overwrote,
        zones_renamed=result.total_zones_renamed,
        zones_skipped=result.total_zones_skipped,
        zones_failed=result.total_zones_failed,
        records_created=result.total_records_created,
        user=current_user.display_name,
    )
    return _commit_result_to_pydantic(result)
