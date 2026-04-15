"""DHCP scope CRUD. Scoped under /subnets/{subnet_id}/dhcp-scopes and /scopes/{id}."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.api.v1.dhcp._audit import write_audit
from app.models.dhcp import DHCPScope, DHCPServer
from app.models.ipam import Subnet

router = APIRouter(tags=["dhcp"])

VALID_HOSTNAME_POLICIES = {"client", "server_name", "derived", "none"}
VALID_SYNC_MODES = {"disabled", "on_lease", "on_static_only", "ipam", "learned"}

# Frontend sends options as `[{code, value}]` (friendlier for the editor UI).
# The DB / driver expect a `{name: value}` dict. Translate at the API boundary.
_CODE_TO_NAME: dict[int, str] = {
    2: "time-offset",
    3: "routers",
    6: "dns-servers",
    15: "domain-name",
    26: "mtu",
    28: "broadcast-address",
    42: "ntp-servers",
    66: "tftp-server-name",
    67: "bootfile-name",
    119: "domain-search",
    150: "tftp-server-address",
}


def _normalize_options(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        out: dict[str, Any] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            code = entry.get("code")
            name = entry.get("name") or _CODE_TO_NAME.get(int(code)) if code else None
            if not name:
                name = f"option-{code}" if code else None
            if not name:
                continue
            out[name] = entry.get("value")
        return out
    return {}


def _normalize_sync_mode(v: str | None) -> str:
    # Accept frontend's "ipam"/"learned" shorthands.
    if v in (None, "", "ipam"):
        return "on_static_only"
    if v == "learned":
        return "on_lease"
    return v


class ScopeCreate(BaseModel):
    model_config = {"extra": "ignore"}

    server_id: uuid.UUID | None = None
    # Accept both is_active and the frontend's `enabled`.
    is_active: bool = True
    enabled: bool | None = None
    lease_time: int = 86400
    min_lease_time: int | None = None
    max_lease_time: int | None = None
    # Accept dict or list-of-{code,value}.
    options: Any = None
    ddns_enabled: bool = False
    ddns_hostname_policy: str | None = "client"
    # Accept both names; frontend sends hostname_sync_mode.
    hostname_to_ipam_sync: str = "on_static_only"
    hostname_sync_mode: str | None = None

    @field_validator("ddns_hostname_policy")
    @classmethod
    def _h(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return "client"
        if v not in VALID_HOSTNAME_POLICIES:
            raise ValueError(f"ddns_hostname_policy must be one of {sorted(VALID_HOSTNAME_POLICIES)}")
        return v


class ScopeUpdate(BaseModel):
    model_config = {"extra": "ignore"}

    is_active: bool | None = None
    enabled: bool | None = None
    lease_time: int | None = None
    min_lease_time: int | None = None
    max_lease_time: int | None = None
    options: Any = None
    ddns_enabled: bool | None = None
    ddns_hostname_policy: str | None = None
    hostname_to_ipam_sync: str | None = None
    hostname_sync_mode: str | None = None


_NAME_TO_CODE = {v: k for k, v in _CODE_TO_NAME.items()}


class ScopeResponse(BaseModel):
    id: uuid.UUID
    server_id: uuid.UUID
    subnet_id: uuid.UUID
    enabled: bool
    name: str = ""
    description: str = ""
    lease_time: int
    min_lease_time: int | None
    max_lease_time: int | None
    options: list[dict[str, Any]]
    ddns_enabled: bool
    ddns_hostname_policy: str | None
    ddns_domain_override: str | None = None
    hostname_sync_mode: str
    last_pushed_at: datetime | None
    created_at: datetime
    modified_at: datetime


def _scope_to_response(scope: DHCPScope) -> ScopeResponse:
    raw = scope.options or {}
    opts: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for name, val in raw.items():
            opts.append(
                {"code": _NAME_TO_CODE.get(name, 0), "name": name, "value": val}
            )
    elif isinstance(raw, list):
        opts = list(raw)
    return ScopeResponse(
        id=scope.id,
        server_id=scope.server_id,
        subnet_id=scope.subnet_id,
        enabled=scope.is_active,
        name="",
        description="",
        lease_time=scope.lease_time,
        min_lease_time=scope.min_lease_time,
        max_lease_time=scope.max_lease_time,
        options=opts,
        ddns_enabled=scope.ddns_enabled,
        ddns_hostname_policy=scope.ddns_hostname_policy,
        ddns_domain_override=None,
        hostname_sync_mode=scope.hostname_to_ipam_sync,
        last_pushed_at=scope.last_pushed_at,
        created_at=scope.created_at,
        modified_at=scope.modified_at,
    )


@router.get("/subnets/{subnet_id}/dhcp-scopes", response_model=list[ScopeResponse])
async def list_scopes_for_subnet(
    subnet_id: uuid.UUID, db: DB, _: CurrentUser
) -> list[ScopeResponse]:
    res = await db.execute(select(DHCPScope).where(DHCPScope.subnet_id == subnet_id))
    return [_scope_to_response(s) for s in res.unique().scalars().all()]


@router.post(
    "/subnets/{subnet_id}/dhcp-scopes",
    response_model=ScopeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_scope(
    subnet_id: uuid.UUID, body: ScopeCreate, db: DB, user: SuperAdmin
) -> DHCPScope:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=404, detail="Subnet not found")
    # Server is optional at create time — if only one DHCP server exists and none
    # was specified, bind to it automatically; otherwise require an explicit pick.
    server_id = body.server_id
    if server_id is None:
        all_servers = (await db.execute(select(DHCPServer))).scalars().all()
        if len(all_servers) == 1:
            server_id = all_servers[0].id
        else:
            raise HTTPException(
                status_code=422,
                detail="server_id is required when more than one DHCP server is registered",
            )
    srv = await db.get(DHCPServer, server_id)
    if srv is None:
        raise HTTPException(status_code=404, detail="DHCP server not found")
    existing = await db.execute(
        select(DHCPScope).where(
            DHCPScope.server_id == server_id, DHCPScope.subnet_id == subnet_id
        )
    )
    if existing.unique().scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="A scope for this server+subnet already exists"
        )
    sync_mode = _normalize_sync_mode(body.hostname_sync_mode or body.hostname_to_ipam_sync)
    if sync_mode not in VALID_SYNC_MODES - {"ipam", "learned"}:
        raise HTTPException(status_code=422, detail=f"invalid hostname sync mode: {sync_mode}")
    is_active = body.enabled if body.enabled is not None else body.is_active
    scope = DHCPScope(
        subnet_id=subnet_id,
        server_id=server_id,
        is_active=is_active,
        lease_time=body.lease_time,
        min_lease_time=body.min_lease_time,
        max_lease_time=body.max_lease_time,
        options=_normalize_options(body.options),
        ddns_enabled=body.ddns_enabled,
        ddns_hostname_policy=body.ddns_hostname_policy or "client",
        hostname_to_ipam_sync=sync_mode,
    )
    db.add(scope)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="dhcp_scope",
        resource_id=str(scope.id),
        resource_display=f"{srv.name}:{subnet.network}",
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(scope)
    return _scope_to_response(scope)


@router.get("/scopes/{scope_id}", response_model=ScopeResponse)
async def get_scope(scope_id: uuid.UUID, db: DB, _: CurrentUser) -> ScopeResponse:
    scope = await db.get(DHCPScope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
    return _scope_to_response(scope)


@router.put("/scopes/{scope_id}", response_model=ScopeResponse)
async def update_scope(
    scope_id: uuid.UUID, body: ScopeUpdate, db: DB, user: SuperAdmin
) -> ScopeResponse:
    scope = await db.get(DHCPScope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
    changes = body.model_dump(exclude_none=True)
    # Normalize the same fields the create endpoint does.
    if "enabled" in changes:
        changes["is_active"] = changes.pop("enabled")
    if "hostname_sync_mode" in changes:
        changes["hostname_to_ipam_sync"] = _normalize_sync_mode(
            changes.pop("hostname_sync_mode")
        )
    elif "hostname_to_ipam_sync" in changes:
        changes["hostname_to_ipam_sync"] = _normalize_sync_mode(
            changes["hostname_to_ipam_sync"]
        )
    if "options" in changes:
        changes["options"] = _normalize_options(changes["options"])
    for k, v in changes.items():
        setattr(scope, k, v)
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="dhcp_scope",
        resource_id=str(scope.id),
        resource_display=str(scope.id),
        changed_fields=list(changes.keys()),
        new_value=body.model_dump(mode="json", exclude_none=True),
    )
    await db.commit()
    await db.refresh(scope)
    return _scope_to_response(scope)


@router.delete("/scopes/{scope_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scope(scope_id: uuid.UUID, db: DB, user: SuperAdmin) -> None:
    scope = await db.get(DHCPScope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="dhcp_scope",
        resource_id=str(scope.id),
        resource_display=str(scope.id),
    )
    await db.delete(scope)
    await db.commit()
