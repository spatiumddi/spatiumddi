"""Platform settings — singleton read/write (superadmin only for writes)."""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator

from app.api.deps import DB, CurrentUser
from app.core.permissions import user_has_permission
from app.models.settings import PlatformSettings

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
    dhcp_pull_leases_interval_minutes: int
    dhcp_pull_leases_last_run_at: datetime | None
    dhcp_default_dns_servers: list[str]
    dhcp_default_domain_name: str
    dhcp_default_domain_search: list[str]
    dhcp_default_ntp_servers: list[str]
    dhcp_default_lease_time: int

    model_config = {"from_attributes": True}


class SettingsUpdate(BaseModel):
    app_title: str | None = None
    app_base_url: str | None = None
    ip_allocation_strategy: str | None = None
    session_timeout_minutes: int | None = None
    auto_logout_minutes: int | None = None
    utilization_warn_threshold: int | None = None
    utilization_critical_threshold: int | None = None
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
    dhcp_pull_leases_interval_minutes: int | None = None
    dhcp_default_dns_servers: list[str] | None = None
    dhcp_default_domain_name: str | None = None
    dhcp_default_domain_search: list[str] | None = None
    dhcp_default_ntp_servers: list[str] | None = None
    dhcp_default_lease_time: int | None = None

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
        "dhcp_pull_leases_interval_minutes",
    )
    @classmethod
    def validate_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("Must be >= 1")
        return v

    @field_validator("utilization_warn_threshold", "utilization_critical_threshold")
    @classmethod
    def validate_threshold(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 100):
            raise ValueError("Threshold must be between 0 and 100")
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


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.get("", response_model=SettingsResponse)
async def get_settings(current_user: CurrentUser, db: DB) -> PlatformSettings:
    return await _get_or_create(db)


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
