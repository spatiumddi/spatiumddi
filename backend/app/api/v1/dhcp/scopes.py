"""DHCP scope CRUD. Group-centric: scopes belong to DHCPServerGroup, not
individual servers. Routes live under ``/subnets/{subnet_id}/dhcp-scopes``
(for the IPAM-side pivot) and ``/scopes/{id}``.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.api.v1.dhcp._audit import write_audit
from app.core.agent_wake import collect_wake, dhcp_group_channel
from app.core.permissions import require_resource_permission
from app.models.dhcp import DHCPScope, DHCPServerGroup
from app.models.ipam import Subnet
from app.services.ai.operations import get_operation
from app.services.ai.operations_risky import DeleteScopeArgs
from app.services.approvals.gate import gate_or_execute
from app.services.dhcp.windows_writethrough import (
    push_scope_upsert,
)
from app.services.tags import apply_tag_filter

router = APIRouter(tags=["dhcp"], dependencies=[Depends(require_resource_permission("dhcp_scope"))])

VALID_HOSTNAME_POLICIES = {"client", "server_name", "derived", "none"}
VALID_SYNC_MODES = {"disabled", "on_lease", "on_static_only", "ipam", "learned"}
# DHCPv6 operating modes (issue #52). Only meaningful for ipv6 scopes.
VALID_V6_MODES = {"stateful", "stateless", "slaac"}

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
    """Coerce a hostname→IPAM sync value to the canonical DB vocabulary
    (``disabled`` | ``on_static_only`` | ``on_lease``).

    The UI (and API) once used a separate ``none`` / ``ipam`` / ``learned``
    vocabulary that didn't round-trip: the response echoes the stored canonical
    value, which the old ``<select>`` couldn't render, so edits snapped back
    (#475). The UI now speaks the canonical vocabulary directly; these legacy
    values are still mapped in for backward-compatible API clients. Empty /
    missing defaults to ``on_static_only`` (the model default).
    """
    if not v:  # None or ""
        return "on_static_only"
    legacy = {"none": "disabled", "ipam": "on_static_only", "learned": "on_lease"}
    return legacy.get(v, v)


def _validate_relay_addresses(v: list[str] | None) -> list[str]:
    """Validate + de-dupe relay-agent IPs (issue #337).

    Each entry must parse as a bare IPv4/IPv6 address (no CIDR — Kea's
    ``relay.ip-addresses`` takes literal giaddr values). Order is
    preserved minus duplicates so the rendered config is stable.
    """
    if not v:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in v:
        addr = str(raw).strip()
        if not addr:
            continue
        try:
            normalized = str(ipaddress.ip_address(addr))
        except ValueError as exc:
            raise ValueError(f"invalid relay address: {addr!r}") from exc
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _validate_relay_family(addrs: list[str], address_family: str) -> None:
    """Reject relay addresses whose IP family doesn't match the scope's
    family. A v4 (subnet4) scope's giaddr relays must be IPv4 and a v6
    (subnet6) scope's relays IPv6 — a mismatch renders an invalid Kea
    subnet (issue #337). Format is already validated upstream; this only
    checks the family against the scope's subnet-derived address_family.
    """
    want_v6 = address_family == "ipv6"
    for raw in addrs or []:
        try:
            ip = ipaddress.ip_address(str(raw).strip())
        except ValueError:
            continue
        if (ip.version == 6) != want_v6:
            fam = "IPv6" if want_v6 else "IPv4"
            raise HTTPException(
                status_code=422,
                detail=(
                    f"relay address {raw!r} must be {fam} to match this " f"{address_family} scope"
                ),
            )


class ScopeCreate(BaseModel):
    model_config = {"extra": "ignore"}

    group_id: uuid.UUID | None = None
    name: str = ""
    description: str = ""
    is_active: bool = True
    enabled: bool | None = None
    lease_time: int = 86400
    min_lease_time: int | None = None
    max_lease_time: int | None = None
    options: Any = None
    ddns_enabled: bool = False
    ddns_hostname_policy: str | None = "client"
    hostname_to_ipam_sync: str = "on_static_only"
    hostname_sync_mode: str | None = None
    # DHCPv6 operating mode (issue #52) — ignored for v4 scopes.
    v6_address_mode: str = "stateful"
    ra_managed_flag: bool = True
    ra_other_flag: bool = True
    # Relay-agent (giaddr) IPs (issue #337) — see DHCPScope.relay_addresses.
    relay_addresses: list[str] = Field(default_factory=list)
    tags: dict[str, Any] = Field(default_factory=dict)

    @field_validator("relay_addresses")
    @classmethod
    def _relay(cls, v: list[str] | None) -> list[str]:
        return _validate_relay_addresses(v)

    @field_validator("ddns_hostname_policy")
    @classmethod
    def _h(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return "client"
        if v not in VALID_HOSTNAME_POLICIES:
            raise ValueError(
                f"ddns_hostname_policy must be one of {sorted(VALID_HOSTNAME_POLICIES)}"
            )
        return v

    @field_validator("v6_address_mode")
    @classmethod
    def _v6mode(cls, v: str | None) -> str:
        if v in (None, ""):
            return "stateful"
        if v not in VALID_V6_MODES:
            raise ValueError(f"v6_address_mode must be one of {sorted(VALID_V6_MODES)}")
        return v


class ScopeUpdate(BaseModel):
    model_config = {"extra": "ignore"}

    name: str | None = None
    description: str | None = None
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
    # PXE / iPXE profile binding (issue #51). Pass the UUID of a
    # ``DHCPPXEProfile`` in this scope's group to enable PXE; pass
    # null to detach. The bound profile's matches render as Kea
    # client-classes on the next bundle push.
    pxe_profile_id: uuid.UUID | None = None
    # Distinguish "set to null" from "field not present" — Pydantic
    # treats null + missing identically by default. We need this to
    # support detaching a previously-bound profile.
    clear_pxe_profile: bool | None = None
    # DHCPv6 operating mode (issue #52) — ignored for v4 scopes.
    v6_address_mode: str | None = None
    ra_managed_flag: bool | None = None
    ra_other_flag: bool | None = None
    # Relay-agent (giaddr) IPs (issue #337). Pass a list to replace the
    # scope's relay set (empty list clears it); omit to leave unchanged.
    relay_addresses: list[str] | None = None
    tags: dict[str, Any] | None = None

    @field_validator("v6_address_mode")
    @classmethod
    def _v6mode(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if v not in VALID_V6_MODES:
            raise ValueError(f"v6_address_mode must be one of {sorted(VALID_V6_MODES)}")
        return v

    @field_validator("relay_addresses")
    @classmethod
    def _relay(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return _validate_relay_addresses(v)


_NAME_TO_CODE = {v: k for k, v in _CODE_TO_NAME.items()}


class ScopeResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
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
    address_family: str = "ipv4"
    v6_address_mode: str = "stateful"
    ra_managed_flag: bool = True
    ra_other_flag: bool = True
    relay_addresses: list[str] = Field(default_factory=list)
    last_pushed_at: datetime | None
    tags: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    modified_at: datetime


def _scope_to_response(scope: DHCPScope) -> ScopeResponse:
    raw = scope.options or {}
    opts: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for name, val in raw.items():
            opts.append({"code": _NAME_TO_CODE.get(name, 0), "name": name, "value": val})
    elif isinstance(raw, list):
        opts = list(raw)
    return ScopeResponse(
        id=scope.id,
        group_id=scope.group_id,
        subnet_id=scope.subnet_id,
        enabled=scope.is_active,
        name=scope.name or "",
        description=scope.description or "",
        lease_time=scope.lease_time,
        min_lease_time=scope.min_lease_time,
        max_lease_time=scope.max_lease_time,
        options=opts,
        ddns_enabled=scope.ddns_enabled,
        ddns_hostname_policy=scope.ddns_hostname_policy,
        ddns_domain_override=None,
        hostname_sync_mode=scope.hostname_to_ipam_sync,
        address_family=getattr(scope, "address_family", "ipv4") or "ipv4",
        v6_address_mode=getattr(scope, "v6_address_mode", "stateful") or "stateful",
        ra_managed_flag=getattr(scope, "ra_managed_flag", True),
        ra_other_flag=getattr(scope, "ra_other_flag", True),
        relay_addresses=list(getattr(scope, "relay_addresses", None) or []),
        last_pushed_at=scope.last_pushed_at,
        tags=scope.tags or {},
        created_at=scope.created_at,
        modified_at=scope.modified_at,
    )


@router.get("/subnets/{subnet_id}/dhcp-scopes", response_model=list[ScopeResponse])
async def list_scopes_for_subnet(
    subnet_id: uuid.UUID,
    db: DB,
    _: CurrentUser,
    tag: list[str] = Query(default_factory=list),
) -> list[ScopeResponse]:
    stmt = select(DHCPScope).where(DHCPScope.subnet_id == subnet_id)
    stmt = apply_tag_filter(stmt, DHCPScope.tags, tag)
    res = await db.execute(stmt)
    return [_scope_to_response(s) for s in res.unique().scalars().all()]


@router.get("/server-groups/{group_id}/scopes", response_model=list[ScopeResponse])
async def list_scopes_for_group(
    group_id: uuid.UUID,
    db: DB,
    _: CurrentUser,
    tag: list[str] = Query(default_factory=list),
) -> list[ScopeResponse]:
    stmt = select(DHCPScope).where(DHCPScope.group_id == group_id)
    stmt = apply_tag_filter(stmt, DHCPScope.tags, tag)
    res = await db.execute(stmt)
    return [_scope_to_response(s) for s in res.unique().scalars().all()]


@router.post(
    "/subnets/{subnet_id}/dhcp-scopes",
    response_model=ScopeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_scope(
    subnet_id: uuid.UUID, body: ScopeCreate, db: DB, user: SuperAdmin
) -> ScopeResponse:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=404, detail="Subnet not found")

    # Group is required. If only one group exists and none was specified,
    # bind to it automatically; otherwise 422.
    group_id = body.group_id
    if group_id is None:
        all_groups = (await db.execute(select(DHCPServerGroup))).scalars().all()
        if len(all_groups) == 1:
            group_id = all_groups[0].id
        else:
            raise HTTPException(
                status_code=422,
                detail="group_id is required when more than one DHCP server group exists",
            )
    grp = await db.get(DHCPServerGroup, group_id)
    if grp is None:
        raise HTTPException(status_code=404, detail="DHCP server group not found")

    existing = await db.execute(
        select(DHCPScope).where(DHCPScope.group_id == group_id, DHCPScope.subnet_id == subnet_id)
    )
    if existing.unique().scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="A scope for this group+subnet already exists",
        )

    sync_mode = _normalize_sync_mode(body.hostname_sync_mode or body.hostname_to_ipam_sync)
    if sync_mode not in VALID_SYNC_MODES - {"ipam", "learned"}:
        raise HTTPException(status_code=422, detail=f"invalid hostname sync mode: {sync_mode}")
    is_active = body.enabled if body.enabled is not None else body.is_active
    try:
        _net = ipaddress.ip_network(str(subnet.network), strict=False)
        address_family = "ipv6" if isinstance(_net, ipaddress.IPv6Network) else "ipv4"
    except ValueError:
        address_family = "ipv4"
    _validate_relay_family(body.relay_addresses, address_family)
    scope = DHCPScope(
        subnet_id=subnet_id,
        group_id=group_id,
        name=(body.name or "").strip(),
        description=(body.description or "").strip(),
        is_active=is_active,
        lease_time=body.lease_time,
        min_lease_time=body.min_lease_time,
        max_lease_time=body.max_lease_time,
        options=_normalize_options(body.options),
        ddns_enabled=body.ddns_enabled,
        ddns_hostname_policy=body.ddns_hostname_policy or "client",
        hostname_to_ipam_sync=sync_mode,
        address_family=address_family,
        v6_address_mode=body.v6_address_mode,
        ra_managed_flag=body.ra_managed_flag,
        ra_other_flag=body.ra_other_flag,
        relay_addresses=body.relay_addresses,
    )
    db.add(scope)
    # The pre-check above can't see a soft-deleted scope, and even for
    # live rows a concurrent create can race it, so translate a
    # (group, subnet) unique-violation into a clean 409 (#474). Any other
    # integrity failure (FK / NOT NULL / CHECK) is unexpected — roll back
    # and let it surface as a 500 rather than masking it as a conflict.
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if "uq_dhcp_scope_group_subnet" not in str(exc.orig):
            raise
        raise HTTPException(
            status_code=409,
            detail="A scope for this group+subnet already exists",
        ) from exc
    # Push to every Windows DHCP member of the group BEFORE commit so a
    # WinRM failure rolls the DB row back.
    await push_scope_upsert(db, scope)
    collect_wake(dhcp_group_channel(group_id))
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="dhcp_scope",
        resource_id=str(scope.id),
        resource_display=f"{grp.name}:{subnet.network}",
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
    # ``exclude_unset`` (not ``exclude_none``) so an explicit null clears a
    # nullable column (e.g. resetting min_lease_time / max_lease_time to empty),
    # while a field the client didn't send stays untouched (#475). ``exclude_none``
    # silently dropped explicit nulls, so those fields could never be cleared.
    changes = body.model_dump(exclude_unset=True)
    if "enabled" in changes:
        changes["is_active"] = changes.pop("enabled")
    if "hostname_sync_mode" in changes:
        changes["hostname_to_ipam_sync"] = _normalize_sync_mode(changes.pop("hostname_sync_mode"))
    elif "hostname_to_ipam_sync" in changes:
        changes["hostname_to_ipam_sync"] = _normalize_sync_mode(changes["hostname_to_ipam_sync"])
    # Validate the resolved sync mode with the same guard as create (#475).
    if "hostname_to_ipam_sync" in changes and changes[
        "hostname_to_ipam_sync"
    ] not in VALID_SYNC_MODES - {"ipam", "learned"}:
        raise HTTPException(
            status_code=422,
            detail=f"invalid hostname sync mode: {changes['hostname_to_ipam_sync']}",
        )
    if "options" in changes:
        changes["options"] = _normalize_options(changes["options"])
    # ``clear_pxe_profile=True`` is the explicit detach signal — Pydantic
    # collapses missing + null on ``pxe_profile_id`` so we need a
    # second boolean field to disambiguate. Apply detach first; a
    # later ``pxe_profile_id`` set in the same call (operator
    # detaches one profile and binds another) still wins.
    if changes.pop("clear_pxe_profile", False):
        scope.pxe_profile_id = None
    if "relay_addresses" in changes:
        # Family must match the scope's (subnet-derived) address_family;
        # address_family itself is immutable on update (#337).
        _validate_relay_family(changes["relay_addresses"], scope.address_family or "ipv4")
    for k, v in changes.items():
        setattr(scope, k, v)
    await db.flush()
    await push_scope_upsert(db, scope)
    collect_wake(dhcp_group_channel(scope.group_id))
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


@router.delete("/scopes/{scope_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_scope(
    scope_id: uuid.UUID,
    db: DB,
    user: SuperAdmin,
    request: Request,
    permanent: bool = False,
) -> Any:
    """Delete a DHCP scope.

    Default soft-delete stamps the scope with a fresh batch UUID so it
    can be restored from /admin/trash. The Windows write-through is only
    fired on the permanent path; soft-delete leaves the scope in the
    rendered config until restoration deadline expires (the purge sweep
    triggers the actual config refresh by hard-deleting then).

    Two-person approval (#62): when the ``governance.approvals`` module is on
    and a ``delete:dhcp_scope`` policy matches, returns ``202`` with a pending
    change-request; otherwise executes inline via ``operation.apply`` exactly
    as before (route stays SuperAdmin-gated).
    """
    op = get_operation("delete_scope")
    assert op is not None  # registered at import
    args = DeleteScopeArgs(scope_id=scope_id, permanent=permanent)
    pending = await gate_or_execute(db, user, request, operation=op, args=args)
    if pending is not None:
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=pending.as_dict())
    await op.apply(db, user, args)
    return None
