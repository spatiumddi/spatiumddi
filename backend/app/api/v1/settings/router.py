"""Platform settings — singleton read/write (superadmin only for writes)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select

from app.api.deps import DB, CurrentUser
from app.core.permissions import user_has_permission
from app.models.audit_forward import AuditForwardTarget
from app.models.oui import OUIVendor
from app.models.settings import PlatformSettings
from app.services import audit_forward as audit_forward_svc

logger = structlog.get_logger(__name__)
router = APIRouter()

_SINGLETON_ID = 1


# ── Schema ─────────────────────────────────────────────────────────────────────


class SettingsResponse(BaseModel):
    app_title: str
    app_base_url: str
    ip_allocation_strategy: str
    session_timeout_minutes: int
    auto_logout_minutes: int
    utilization_warn_threshold: int
    utilization_critical_threshold: int
    utilization_max_prefix_ipv4: int
    utilization_max_prefix_ipv6: int
    subnet_tree_default_expanded_depth: int
    discovery_scan_enabled: bool
    discovery_scan_interval_minutes: int
    github_release_check_enabled: bool
    dns_default_ttl: int
    dns_default_zone_type: str
    dns_default_dnssec_validation: str
    dns_recursive_by_default: bool
    dns_auto_sync_enabled: bool
    dns_auto_sync_interval_minutes: int
    dns_auto_sync_delete_stale: bool
    dns_auto_sync_last_run_at: datetime | None
    dns_pull_from_server_enabled: bool
    dns_pull_from_server_interval_minutes: int
    dns_pull_from_server_last_run_at: datetime | None
    dhcp_pull_leases_enabled: bool
    dhcp_pull_leases_interval_seconds: int
    dhcp_pull_leases_last_run_at: datetime | None
    audit_forward_syslog_enabled: bool
    audit_forward_syslog_host: str
    audit_forward_syslog_port: int
    audit_forward_syslog_protocol: str
    audit_forward_syslog_facility: int
    audit_forward_webhook_enabled: bool
    audit_forward_webhook_url: str
    audit_forward_webhook_auth_header: str
    dhcp_default_dns_servers: list[str]
    dhcp_default_domain_name: str
    dhcp_default_domain_search: list[str]
    dhcp_default_ntp_servers: list[str]
    dhcp_default_lease_time: int
    oui_lookup_enabled: bool
    oui_update_interval_hours: int
    oui_last_updated_at: datetime | None
    integration_kubernetes_enabled: bool
    integration_docker_enabled: bool

    model_config = {"from_attributes": True}


class SettingsUpdate(BaseModel):
    app_title: str | None = None
    app_base_url: str | None = None
    ip_allocation_strategy: str | None = None
    session_timeout_minutes: int | None = None
    auto_logout_minutes: int | None = None
    utilization_warn_threshold: int | None = None
    utilization_critical_threshold: int | None = None
    utilization_max_prefix_ipv4: int | None = None
    utilization_max_prefix_ipv6: int | None = None
    subnet_tree_default_expanded_depth: int | None = None
    discovery_scan_enabled: bool | None = None
    discovery_scan_interval_minutes: int | None = None
    github_release_check_enabled: bool | None = None
    dns_default_ttl: int | None = None
    dns_default_zone_type: str | None = None
    dns_default_dnssec_validation: str | None = None
    dns_recursive_by_default: bool | None = None
    dns_auto_sync_enabled: bool | None = None
    dns_auto_sync_interval_minutes: int | None = None
    dns_auto_sync_delete_stale: bool | None = None
    dns_pull_from_server_enabled: bool | None = None
    dns_pull_from_server_interval_minutes: int | None = None
    dhcp_pull_leases_enabled: bool | None = None
    dhcp_pull_leases_interval_seconds: int | None = None
    audit_forward_syslog_enabled: bool | None = None
    audit_forward_syslog_host: str | None = None
    audit_forward_syslog_port: int | None = None
    audit_forward_syslog_protocol: str | None = None
    audit_forward_syslog_facility: int | None = None
    audit_forward_webhook_enabled: bool | None = None
    audit_forward_webhook_url: str | None = None
    audit_forward_webhook_auth_header: str | None = None
    dhcp_default_dns_servers: list[str] | None = None
    dhcp_default_domain_name: str | None = None
    dhcp_default_domain_search: list[str] | None = None
    dhcp_default_ntp_servers: list[str] | None = None
    dhcp_default_lease_time: int | None = None
    oui_lookup_enabled: bool | None = None
    oui_update_interval_hours: int | None = None
    integration_kubernetes_enabled: bool | None = None
    integration_docker_enabled: bool | None = None

    @field_validator("ip_allocation_strategy")
    @classmethod
    def validate_strategy(cls, v: str | None) -> str | None:
        if v is not None and v not in ("sequential", "random"):
            raise ValueError("ip_allocation_strategy must be 'sequential' or 'random'")
        return v

    @field_validator("session_timeout_minutes")
    @classmethod
    def validate_session_timeout(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("Must be >= 0 (0 = no timeout)")
        return v

    @field_validator(
        "discovery_scan_interval_minutes",
        "dns_auto_sync_interval_minutes",
        "dns_pull_from_server_interval_minutes",
        "oui_update_interval_hours",
    )
    @classmethod
    def validate_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("Must be >= 1")
        return v

    @field_validator("dhcp_pull_leases_interval_seconds")
    @classmethod
    def validate_dhcp_pull_seconds(cls, v: int | None) -> int | None:
        # Beat ticks every 10 s — anything below that can't be honoured.
        if v is not None and v < 10:
            raise ValueError("Must be >= 10 (Celery beat ticks every 10 seconds)")
        return v

    @field_validator("utilization_warn_threshold", "utilization_critical_threshold")
    @classmethod
    def validate_threshold(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 100):
            raise ValueError("Threshold must be between 0 and 100")
        return v

    @field_validator("utilization_max_prefix_ipv4")
    @classmethod
    def validate_max_prefix_v4(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 32):
            raise ValueError("Max IPv4 prefix must be 0–32")
        return v

    @field_validator("utilization_max_prefix_ipv6")
    @classmethod
    def validate_max_prefix_v6(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 128):
            raise ValueError("Max IPv6 prefix must be 0–128")
        return v

    @field_validator("audit_forward_syslog_protocol")
    @classmethod
    def validate_syslog_protocol(cls, v: str | None) -> str | None:
        if v is not None and v not in ("udp", "tcp"):
            raise ValueError("syslog_protocol must be 'udp' or 'tcp'")
        return v

    @field_validator("audit_forward_syslog_port")
    @classmethod
    def validate_syslog_port(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 65535):
            raise ValueError("Port must be 1–65535")
        return v

    @field_validator("audit_forward_syslog_facility")
    @classmethod
    def validate_syslog_facility(cls, v: int | None) -> int | None:
        # RFC 5424 §6.2.1 — facility is 0–23.
        if v is not None and not (0 <= v <= 23):
            raise ValueError("Syslog facility must be 0–23 (RFC 5424)")
        return v


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _get_or_create(db: DB) -> PlatformSettings:
    settings = await db.get(PlatformSettings, _SINGLETON_ID)
    if settings is None:
        settings = PlatformSettings(id=_SINGLETON_ID)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


_USER_SETTABLE_FIELDS = set(SettingsUpdate.model_fields.keys())


def _column_defaults() -> dict[str, Any]:
    """Introspect the model's `default=` kwargs so the UI has a single source
    of truth for "reset to defaults" — the same values Postgres would insert
    for a fresh row. Only user-settable fields (those present on
    `SettingsUpdate`) are returned; server-managed columns like
    `*_last_run_at` are omitted."""
    out: dict[str, Any] = {}
    for col in PlatformSettings.__table__.columns:
        if col.name not in _USER_SETTABLE_FIELDS:
            continue
        d = col.default
        if d is None:
            continue
        arg = d.arg
        if callable(arg):
            try:
                out[col.name] = arg({})
            except TypeError:
                out[col.name] = arg()
        else:
            out[col.name] = arg
    return out


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.get("", response_model=SettingsResponse)
async def get_settings(current_user: CurrentUser, db: DB) -> PlatformSettings:
    return await _get_or_create(db)


@router.get("/defaults")
async def get_settings_defaults(current_user: CurrentUser) -> dict[str, Any]:
    return _column_defaults()


@router.put("", response_model=SettingsResponse)
async def update_settings(
    body: SettingsUpdate, current_user: CurrentUser, db: DB
) -> PlatformSettings:
    # Superadmin passes via user_has_permission shortcut; users with an
    # explicit `write`/`admin` grant on `settings` also pass.
    if not user_has_permission(current_user, "write", "settings"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: need 'write' on 'settings'",
        )

    settings = await _get_or_create(db)
    changes = body.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(settings, field, value)

    await db.commit()
    await db.refresh(settings)
    logger.info("platform_settings_updated", user=current_user.username, changes=changes)
    return settings


# ── OUI vendor database ───────────────────────────────────────────────────────
#
# Opt-in feature controlled by ``oui_lookup_enabled``. These endpoints let
# the Settings UI show the vendor-count + last-updated timestamp and kick
# off a manual refresh without waiting for the hourly beat tick.


class OUIStatusResponse(BaseModel):
    enabled: bool
    interval_hours: int
    last_updated_at: datetime | None
    vendor_count: int


class OUIRefreshResponse(BaseModel):
    status: str  # "queued" | "disabled"
    task_id: str | None = None


class OUITaskStatusResponse(BaseModel):
    """Shape returned by the polling endpoint the refresh modal hits.

    ``state`` mirrors Celery's task states (``PENDING``, ``STARTED``,
    ``SUCCESS``, ``FAILURE``, ``RETRY``). When ``state == "SUCCESS"`` the
    ``result`` field carries the diff counters emitted by the task's
    return value. When ``state == "FAILURE"`` the ``error`` field holds
    the exception repr — enough context for the modal to display
    without leaking internal traces to non-admin users (the endpoint is
    already admin-scoped).
    """

    task_id: str
    state: str
    ready: bool
    result: dict[str, Any] | None = None
    error: str | None = None


@router.get("/oui/status", response_model=OUIStatusResponse)
async def get_oui_status(current_user: CurrentUser, db: DB) -> OUIStatusResponse:
    ps = await _get_or_create(db)
    count = (await db.execute(select(func.count(OUIVendor.prefix)))).scalar_one()
    return OUIStatusResponse(
        enabled=ps.oui_lookup_enabled,
        interval_hours=ps.oui_update_interval_hours,
        last_updated_at=ps.oui_last_updated_at,
        vendor_count=int(count),
    )


@router.post(
    "/oui/refresh",
    response_model=OUIRefreshResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_oui_refresh(current_user: CurrentUser, db: DB) -> OUIRefreshResponse:
    if not user_has_permission(current_user, "write", "settings"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: need 'write' on 'settings'",
        )

    ps = await _get_or_create(db)
    if not ps.oui_lookup_enabled:
        return OUIRefreshResponse(status="disabled")

    # Deferred import so the web process doesn't pull the celery task graph
    # into its startup path.
    from app.tasks.oui_update import update_oui_database_now  # noqa: PLC0415

    result = update_oui_database_now.delay()
    logger.info("oui_refresh_triggered", user=current_user.username, task_id=result.id)
    return OUIRefreshResponse(status="queued", task_id=result.id)


@router.get("/oui/refresh/{task_id}", response_model=OUITaskStatusResponse)
async def get_oui_refresh_status(task_id: str, current_user: CurrentUser) -> OUITaskStatusResponse:
    """Poll an in-flight OUI refresh task.

    Celery's ``AsyncResult`` is backed by Redis (the configured
    ``CELERY_RESULT_BACKEND``) and returns ``PENDING`` for unknown task
    IDs, which is indistinguishable from "queued but not picked up
    yet" — the UI treats both the same. A ``task_id`` from a previous
    restart will stay ``PENDING`` forever; the modal caps its poll at
    a timeout to cover that case.
    """
    # Deferred import keeps the router lightweight.
    from celery.result import AsyncResult  # noqa: PLC0415

    from app.celery_app import celery_app  # noqa: PLC0415

    async_result = AsyncResult(task_id, app=celery_app)
    state = async_result.state
    payload = OUITaskStatusResponse(
        task_id=task_id,
        state=state,
        ready=async_result.ready(),
    )
    if state == "SUCCESS":
        raw = async_result.result
        payload.result = raw if isinstance(raw, dict) else {"value": str(raw)}
    elif state == "FAILURE":
        payload.error = repr(async_result.result) if async_result.result else "task failed"
    return payload


# ── Audit forward targets (multi-target + multi-format) ───────────────────

_VALID_KINDS = {"syslog", "webhook"}
_VALID_FORMATS = {
    "rfc5424_json",
    "rfc5424_cef",
    "rfc5424_leef",
    "rfc3164",
    "json_lines",
}
_VALID_PROTOCOLS = {"udp", "tcp", "tls"}
_VALID_SEVERITIES = {"info", "warn", "error", "denied"}


class AuditTargetBody(BaseModel):
    """Create / update body. Webhook-only or syslog-only fields are
    ignored for the other kind, so a single shape fits both."""

    name: str
    enabled: bool = True
    kind: str
    format: str = "rfc5424_json"
    # syslog
    host: str = ""
    port: int = 514
    protocol: str = "udp"
    facility: int = 16
    ca_cert_pem: str | None = None
    # webhook
    url: str = ""
    auth_header: str = ""
    # filter
    min_severity: str | None = None
    resource_types: list[str] | None = None

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str) -> str:
        if v not in _VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)}")
        return v

    @field_validator("format")
    @classmethod
    def _valid_format(cls, v: str) -> str:
        if v not in _VALID_FORMATS:
            raise ValueError(f"format must be one of {sorted(_VALID_FORMATS)}")
        return v

    @field_validator("protocol")
    @classmethod
    def _valid_protocol(cls, v: str) -> str:
        if v not in _VALID_PROTOCOLS:
            raise ValueError(f"protocol must be one of {sorted(_VALID_PROTOCOLS)}")
        return v

    @field_validator("min_severity")
    @classmethod
    def _valid_severity(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if v not in _VALID_SEVERITIES:
            raise ValueError(f"min_severity must be one of {sorted(_VALID_SEVERITIES)}")
        return v


class AuditTargetResponse(BaseModel):
    id: str
    name: str
    enabled: bool
    kind: str
    format: str
    host: str
    port: int
    protocol: str
    facility: int
    ca_cert_pem: str | None
    url: str
    # Redact auth_header — we return whether it's set, never the value.
    auth_header_set: bool
    min_severity: str | None
    resource_types: list[str] | None
    created_at: datetime
    modified_at: datetime


def _target_to_response(t: AuditForwardTarget) -> AuditTargetResponse:
    return AuditTargetResponse(
        id=str(t.id),
        name=t.name,
        enabled=t.enabled,
        kind=t.kind,
        format=t.format,
        host=t.host,
        port=t.port,
        protocol=t.protocol,
        facility=t.facility,
        ca_cert_pem=t.ca_cert_pem,
        url=t.url,
        auth_header_set=bool(t.auth_header),
        min_severity=t.min_severity,
        resource_types=t.resource_types,
        created_at=t.created_at,
        modified_at=t.modified_at,
    )


def _apply_body(t: AuditForwardTarget, body: AuditTargetBody) -> None:
    t.name = body.name
    t.enabled = body.enabled
    t.kind = body.kind
    t.format = body.format
    t.host = body.host
    t.port = body.port
    t.protocol = body.protocol
    t.facility = body.facility
    t.ca_cert_pem = body.ca_cert_pem
    t.url = body.url
    t.auth_header = body.auth_header
    t.min_severity = body.min_severity
    t.resource_types = body.resource_types


@router.get("/audit-forward-targets", response_model=list[AuditTargetResponse])
async def list_audit_targets(current_user: CurrentUser, db: DB) -> list[AuditTargetResponse]:
    if not current_user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    res = await db.execute(select(AuditForwardTarget).order_by(AuditForwardTarget.name))
    return [_target_to_response(t) for t in res.scalars().all()]


@router.post(
    "/audit-forward-targets",
    response_model=AuditTargetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_audit_target(
    body: AuditTargetBody, current_user: CurrentUser, db: DB
) -> AuditTargetResponse:
    if not current_user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    row = AuditForwardTarget()
    _apply_body(row, body)
    db.add(row)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — name collisions land here
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"create failed: {exc}") from exc
    await db.refresh(row)
    return _target_to_response(row)


@router.put("/audit-forward-targets/{target_id}", response_model=AuditTargetResponse)
async def update_audit_target(
    target_id: str, body: AuditTargetBody, current_user: CurrentUser, db: DB
) -> AuditTargetResponse:
    if not current_user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    row = await db.get(AuditForwardTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")
    _apply_body(row, body)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"update failed: {exc}") from exc
    await db.refresh(row)
    return _target_to_response(row)


@router.delete("/audit-forward-targets/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_audit_target(target_id: str, current_user: CurrentUser, db: DB) -> None:
    if not current_user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    row = await db.get(AuditForwardTarget, target_id)
    if row is None:
        return
    await db.delete(row)
    await db.commit()


@router.post("/audit-forward-targets/{target_id}/test")
async def test_audit_target(target_id: str, current_user: CurrentUser, db: DB) -> dict[str, Any]:
    """Send a synthetic event to one target and report success / error.

    The event is flagged ``action="test_forward"`` so the operator can
    filter it out in the collector if they want. Doesn't land in
    ``audit_log`` — this is explicit probe traffic, not an audit.
    """
    if not current_user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    row = await db.get(AuditForwardTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")

    now = datetime.utcnow()
    payload: dict[str, Any] = {
        "id": "test-" + str(row.id),
        "timestamp": now.isoformat() + "Z",
        "action": "test_forward",
        "resource_type": "audit_forward_target",
        "resource_id": str(row.id),
        "resource_display": row.name,
        "result": "success",
        "user_id": str(current_user.id),
        "user_display_name": current_user.display_name,
        "auth_source": "local",
        "changed_fields": [],
        "old_value": None,
        "new_value": None,
    }
    target_dict = {
        "name": row.name,
        "kind": row.kind,
        "format": row.format,
        "host": row.host,
        "port": row.port,
        "protocol": row.protocol,
        "facility": row.facility,
        "ca_cert_pem": row.ca_cert_pem,
        "url": row.url,
        "auth_header": row.auth_header or "",
        "min_severity": None,  # ignore filter on a probe
        "resource_types": None,
    }
    try:
        await audit_forward_svc._deliver_to_target(target_dict, payload)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"delivery failed: {exc}") from exc
    return {"status": "ok", "target": row.name}
