"""Nmap on-demand scan API.

Mounted at ``/api/v1/nmap``. Phase 1 surface only — no scheduled
scans, no triggers (those are tracked separately as roadmap
follow-ups). Endpoints:

* ``POST /scans`` — kick a new scan, returns the row in ``queued``
  state and dispatches a Celery task. 202 Accepted.
* ``GET /scans`` — paginated list, filterable by ``ip_address_id`` /
  ``target_ip`` / ``status``.
* ``GET /scans/{id}`` — full record (incl. raw XML on completion).
* ``GET /scans/{id}/stream`` — Server-Sent Events relaying the live
  ``raw_stdout`` buffer. Auth via ``?token=<jwt-or-api-token>`` query
  arg because ``EventSource`` can't set Authorization headers.
* ``DELETE /scans/{id}`` — operator cancel; flips status to
  ``cancelled`` so the runner self-terminates on its next poll.

All endpoints gate on the ``manage_nmap_scans`` permission. The
seeded "Network Editor" builtin role gets it (see
``app.main._BUILTIN_ROLES``).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from jose import JWTError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DB, CurrentUser
from app.core.permissions import require_permission, user_has_permission
from app.core.security import decode_access_token
from app.db import get_db
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.ipam import IPAddress
from app.models.nmap import NmapScan
from app.services.nmap import NmapArgError, build_argv

from .schemas import (
    NmapScanCreate,
    NmapScanListResponse,
    NmapScanRead,
    NmapSummary,
)

logger = structlog.get_logger(__name__)

PERMISSION = "manage_nmap_scans"

router = APIRouter(tags=["nmap"])
# Each endpoint declares its own permission dep (which transitively
# requires auth). No router-level ``Depends(get_current_user)`` —
# that would 401 the SSE stream before its query-token resolver runs,
# since EventSource can't send Authorization headers.


# ── Helpers ─────────────────────────────────────────────────────────


def _to_read(row: NmapScan, *, include_raw: bool = False) -> NmapScanRead:
    summary = None
    if row.summary_json:
        try:
            summary = NmapSummary.model_validate(row.summary_json)
        except Exception:  # noqa: BLE001 — best-effort
            summary = None
    return NmapScanRead(
        id=row.id,
        target_ip=str(row.target_ip),
        ip_address_id=row.ip_address_id,
        preset=row.preset,
        port_spec=row.port_spec,
        extra_args=row.extra_args,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_seconds=row.duration_seconds,
        exit_code=row.exit_code,
        command_line=row.command_line,
        error_message=row.error_message,
        summary=summary,
        raw_xml=row.raw_xml if include_raw else None,
        raw_stdout=row.raw_stdout if include_raw else None,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        modified_at=row.modified_at,
    )


async def _audit(
    db: AsyncSession,
    *,
    user: Any,
    action: str,
    scan_id: uuid.UUID,
    target: str,
    new_value: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            user_display_name=user.display_name if user else "system",
            auth_source=getattr(user, "auth_source", "local") or "local",
            action=action,
            resource_type="nmap_scan",
            resource_id=str(scan_id),
            resource_display=f"nmap:{target}",
            new_value=new_value,
        )
    )


# ── CRUD ────────────────────────────────────────────────────────────


@router.post(
    "/scans",
    response_model=NmapScanRead,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission("write", PERMISSION))],
)
async def create_scan(body: NmapScanCreate, db: DB, current_user: CurrentUser) -> NmapScanRead:
    # Resolve / validate target_ip + ip_address_id consistency.
    target_ip = body.target_ip.strip()
    ip_address_id = body.ip_address_id
    if ip_address_id is not None:
        ip_row = await db.get(IPAddress, ip_address_id)
        if ip_row is None:
            raise HTTPException(status_code=422, detail="ip_address_id not found")
        # If only ip_address_id was provided (target_ip is the IPAM
        # row's address), prefer the canonical INET form from the row.
        if not target_ip:
            target_ip = str(ip_row.address)

    if not target_ip:
        raise HTTPException(status_code=422, detail="target_ip is required")

    # Pre-validate by building the argv now — surfaces NmapArgError
    # before we persist a doomed row.
    try:
        build_argv(target_ip, body.preset, body.port_spec, body.extra_args)
    except NmapArgError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    scan = NmapScan(
        target_ip=target_ip,
        ip_address_id=ip_address_id,
        preset=body.preset,
        port_spec=body.port_spec,
        extra_args=body.extra_args,
        status="queued",
        created_by_user_id=current_user.id,
    )
    db.add(scan)
    await db.flush()
    await _audit(
        db,
        user=current_user,
        action="create",
        scan_id=scan.id,
        target=target_ip,
        new_value={
            "preset": body.preset,
            "port_spec": body.port_spec,
            "extra_args": body.extra_args,
            "target_ip": target_ip,
        },
    )
    await db.commit()
    await db.refresh(scan)

    # Dispatch celery task. Broker outage shouldn't 500 the request —
    # mirror snmp_poll_now's tolerance: log + leave row in queued so
    # the operator can re-trigger.
    try:
        from app.tasks.nmap import run_scan_task  # noqa: PLC0415

        run_scan_task.delay(str(scan.id))
    except Exception as exc:  # noqa: BLE001 — broker down
        logger.warning("nmap_scan_dispatch_failed", scan_id=str(scan.id), error=str(exc))

    return _to_read(scan)


@router.get(
    "/scans",
    response_model=NmapScanListResponse,
    dependencies=[Depends(require_permission("read", PERMISSION))],
)
async def list_scans(
    db: DB,
    current_user: CurrentUser,  # noqa: ARG001 — gate handled by dep
    ip_address_id: uuid.UUID | None = Query(None),
    target_ip: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> NmapScanListResponse:
    base = select(NmapScan)
    if ip_address_id is not None:
        base = base.where(NmapScan.ip_address_id == ip_address_id)
    if target_ip:
        base = base.where(NmapScan.target_ip == target_ip)
    if status_filter:
        base = base.where(NmapScan.status == status_filter)

    count_stmt = select(func.count()).select_from(base.subquery())
    total = int((await db.execute(count_stmt)).scalar_one())

    stmt = base.order_by(NmapScan.created_at.desc()).limit(page_size).offset((page - 1) * page_size)
    rows = list((await db.execute(stmt)).scalars().all())
    items = [_to_read(r) for r in rows]
    return NmapScanListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get(
    "/scans/{scan_id}",
    response_model=NmapScanRead,
    dependencies=[Depends(require_permission("read", PERMISSION))],
)
async def get_scan(
    scan_id: uuid.UUID, db: DB, current_user: CurrentUser  # noqa: ARG001
) -> NmapScanRead:
    row = await db.get(NmapScan, scan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    return _to_read(row, include_raw=True)


@router.delete(
    "/scans/{scan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("delete", PERMISSION))],
)
async def cancel_scan(scan_id: uuid.UUID, db: DB, current_user: CurrentUser) -> None:
    """Cancel an in-flight scan or hard-delete a finished one.

    A queued / running scan is marked ``cancelled`` so the runner sees
    the state change on its next DB read and self-terminates. A scan
    in any terminal state (completed / failed / cancelled / timeout)
    is removed entirely — that's the "delete old scan" path.
    """
    row = await db.get(NmapScan, scan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    target = str(row.target_ip)

    if row.status in ("queued", "running"):
        row.status = "cancelled"
        if row.finished_at is None:
            row.finished_at = datetime.now(UTC)
        await _audit(
            db,
            user=current_user,
            action="cancel",
            scan_id=row.id,
            target=target,
        )
        await db.commit()
        return

    await _audit(
        db,
        user=current_user,
        action="delete",
        scan_id=row.id,
        target=target,
    )
    await db.delete(row)
    await db.commit()


# ── SSE stream ──────────────────────────────────────────────────────


async def _resolve_user_from_query_token(db: AsyncSession, token: str) -> User:
    """Validate a JWT or API token passed as a query parameter.

    EventSource can't set ``Authorization`` headers, so the SSE
    endpoint accepts ``?token=<...>``. We re-implement the relevant
    branches of :func:`app.api.deps.get_current_user` here rather
    than reach into Security() — that dep is wired to the Bearer
    extractor which won't see a query arg.
    """
    if token.startswith("sddi_"):
        # API tokens — re-use the deps helper.
        from app.api.deps import _resolve_api_token  # noqa: PLC0415

        return await _resolve_api_token(db, token)

    try:
        payload = decode_access_token(token)
        user_id: str = payload["sub"]
    except (JWTError, KeyError) as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")
    return user


@router.get("/scans/{scan_id}/stream")
async def stream_scan(
    scan_id: uuid.UUID,
    request: Request,  # noqa: ARG001 — required so FastAPI doesn't auto-resolve as body
    token: Annotated[str, Query(...)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """Server-Sent Events relay for a running scan.

    Auth is via ``?token=`` query arg (EventSource can't send
    Authorization headers). Each ``data:`` frame carries one line of
    nmap stdout. On terminal status we emit one final
    ``event: done`` frame and close.
    """
    user = await _resolve_user_from_query_token(db, token)
    if not user_has_permission(user, "read", PERMISSION):
        raise HTTPException(status_code=403, detail="Permission denied")

    row = await db.get(NmapScan, scan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scan not found")

    async def _event_stream() -> Any:
        # Re-import inside the coroutine so the streaming generator
        # owns its own engine — the request session is closed as soon
        # as FastAPI returns the StreamingResponse.
        from app.services.nmap.runner import stream_scan_lines  # noqa: PLC0415

        # Initial heartbeat so the browser knows the connection is open.
        yield ": connected\n\n"
        deadline = time.monotonic() + 600.0
        try:
            async for line in stream_scan_lines(scan_id, poll_interval=0.5, cap_seconds=600.0):
                if time.monotonic() > deadline:
                    yield "event: done\ndata: timeout\n\n"
                    return
                if line.startswith("__DONE__:"):
                    final_status = line[len("__DONE__:") :].strip()
                    yield f"event: done\ndata: {final_status}\n\n"
                    return
                # SSE ``data:`` frames must not contain raw newlines.
                payload = line.rstrip("\n").rstrip("\r")
                # Skip empty heartbeat lines from nmap (rare but
                # possible mid-stats); send a comment so the connection
                # stays warm without polluting the visible stream.
                if not payload:
                    yield ": tick\n\n"
                    continue
                yield f"data: {payload}\n\n"
        except asyncio.CancelledError:
            # Client disconnected — exit quietly.
            return

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(_event_stream(), media_type="text/event-stream", headers=headers)


__all__ = ["router"]
