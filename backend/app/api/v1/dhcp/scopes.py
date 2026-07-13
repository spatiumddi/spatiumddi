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
from app.core.dns_names import validate_fqdn
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
# Fields on ScopeUpdate an explicit ``null`` may CLEAR (#475). Every other
# nullable column keeps its ``exclude_none`` behaviour — a stray null is dropped
# rather than applied — so a NOT-NULL column can't 500 on ``setattr(None)`` and a
# partial-body client can't silently wipe a column it didn't mean to touch.
NULLABLE_CLEARABLE_SCOPE_FIELDS = {"min_lease_time", "max_lease_time"}
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


# Legacy / alternate option names that collapse onto a canonical name.
# The frontend historically sent option 6 as the IANA name
# ``domain-name-servers`` while the canonical stored vocabulary (and the
# Kea driver's option-name map) is ``dns-servers`` (#583). Normalise on
# write so new rows store canonically, and recognise the alias on read so
# already-persisted rows still resolve to code 6 in ``_scope_to_response``
# rather than falling through to code 0 / the custom-options bucket.
_OPTION_NAME_ALIASES: dict[str, str] = {"domain-name-servers": "dns-servers"}


def validate_domain_options(
    opts: dict[str, Any], *, previous: dict[str, Any] | None = None
) -> None:
    """Validate the FQDN-valued DHCP options (issue #597); raise 422 on a bad one.

    ``domain-name`` (option 15) is a single FQDN; ``domain-search``
    (option 119) is a list of FQDNs. Both render straight into the Kea
    config, so a malformed value would break it or ship a bad search suffix.
    Empty / whitespace-only entries are rejected too (a blank search suffix
    is meaningless). A value identical to ``previous`` is skipped, so an
    update that merely round-trips a grandfathered value doesn't block the
    edit (validate-on-*change*, matching the issue's report-don't-break stance).
    """
    prev = previous or {}
    try:
        dn = opts.get("domain-name")
        if isinstance(dn, str) and dn != prev.get("domain-name"):
            if not dn.strip():
                raise ValueError("domain-name option must not be blank")
            validate_fqdn(dn, field="domain-name option")
        ds = opts.get("domain-search")
        if isinstance(ds, list) and ds != prev.get("domain-search"):
            for d in ds:
                if not str(d).strip():
                    raise ValueError("domain-search option contains a blank entry")
                validate_fqdn(str(d), field="domain-search option")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _normalize_options(raw: Any) -> dict[str, Any]:
    """Normalize option shape (name aliases, list→dict). Does NOT validate —
    callers run ``validate_domain_options`` so the create/update paths can
    apply different only-on-change gating."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {_OPTION_NAME_ALIASES.get(str(k), str(k)): v for k, v in raw.items()}
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
            name = _OPTION_NAME_ALIASES.get(name, name)
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
    # When False, this scope's dynamic-pool lease mirrors are excluded from the
    # IPAM↔DNS drift check (ephemeral leases don't read as "out of sync").
    dns_track_dynamic_leases: bool = True
    # DHCPv6 operating mode (issue #52) — ignored for v4 scopes.
    v6_address_mode: str = "stateful"
    ra_managed_flag: bool = True
    ra_other_flag: bool = True
    # IPv6 Router Advertisement management (issue #524) — v6 scopes only.
    ra_enabled: bool = False
    ra_mo_override: bool = False
    ra_router_lifetime: int = 1800
    ra_max_interval: int = 600
    ra_prefix_valid_lifetime: int = 86400
    ra_prefix_preferred_lifetime: int = 14400
    ra_prefix_on_link: bool = True
    ra_prefix_autonomous: bool = True
    ra_interface: str = ""
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
    dns_track_dynamic_leases: bool | None = None
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
    # IPv6 Router Advertisement management (issue #524) — v6 scopes only.
    ra_enabled: bool | None = None
    ra_mo_override: bool | None = None
    ra_router_lifetime: int | None = None
    ra_max_interval: int | None = None
    ra_prefix_valid_lifetime: int | None = None
    ra_prefix_preferred_lifetime: int | None = None
    ra_prefix_on_link: bool | None = None
    ra_prefix_autonomous: bool | None = None
    ra_interface: str | None = None
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
# Existing rows may still be stored under the legacy alias (#583); map it
# to code 6 on readback so the DNS Servers field populates on edit.
for _alias, _canon in _OPTION_NAME_ALIASES.items():
    if _canon in _NAME_TO_CODE:
        _NAME_TO_CODE[_alias] = _NAME_TO_CODE[_canon]


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
    dns_track_dynamic_leases: bool = True
    address_family: str = "ipv4"
    v6_address_mode: str = "stateful"
    ra_managed_flag: bool = True
    ra_other_flag: bool = True
    ra_enabled: bool = False
    ra_mo_override: bool = False
    ra_router_lifetime: int = 1800
    ra_max_interval: int = 600
    ra_prefix_valid_lifetime: int = 86400
    ra_prefix_preferred_lifetime: int = 14400
    ra_prefix_on_link: bool = True
    ra_prefix_autonomous: bool = True
    ra_interface: str = ""
    relay_addresses: list[str] = Field(default_factory=list)
    # PXE / iPXE profile binding (issue #51). Echoed so the scope edit
    # form can pre-select the bound profile — without it the picker always
    # reset to "(none)" and a save silently detached the profile (#583).
    pxe_profile_id: uuid.UUID | None = None
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
        dns_track_dynamic_leases=getattr(scope, "dns_track_dynamic_leases", True),
        address_family=getattr(scope, "address_family", "ipv4") or "ipv4",
        v6_address_mode=getattr(scope, "v6_address_mode", "stateful") or "stateful",
        ra_managed_flag=getattr(scope, "ra_managed_flag", True),
        ra_other_flag=getattr(scope, "ra_other_flag", True),
        ra_enabled=getattr(scope, "ra_enabled", False),
        ra_mo_override=getattr(scope, "ra_mo_override", False),
        ra_router_lifetime=getattr(scope, "ra_router_lifetime", 1800),
        ra_max_interval=getattr(scope, "ra_max_interval", 600),
        ra_prefix_valid_lifetime=getattr(scope, "ra_prefix_valid_lifetime", 86400),
        ra_prefix_preferred_lifetime=getattr(scope, "ra_prefix_preferred_lifetime", 14400),
        ra_prefix_on_link=getattr(scope, "ra_prefix_on_link", True),
        ra_prefix_autonomous=getattr(scope, "ra_prefix_autonomous", True),
        ra_interface=getattr(scope, "ra_interface", "") or "",
        relay_addresses=list(getattr(scope, "relay_addresses", None) or []),
        pxe_profile_id=scope.pxe_profile_id,
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
    _create_options = _normalize_options(body.options)
    validate_domain_options(_create_options)  # always validate on create (#597)
    scope = DHCPScope(
        subnet_id=subnet_id,
        group_id=group_id,
        name=(body.name or "").strip(),
        description=(body.description or "").strip(),
        is_active=is_active,
        lease_time=body.lease_time,
        min_lease_time=body.min_lease_time,
        max_lease_time=body.max_lease_time,
        options=_create_options,
        ddns_enabled=body.ddns_enabled,
        ddns_hostname_policy=body.ddns_hostname_policy or "client",
        hostname_to_ipam_sync=sync_mode,
        dns_track_dynamic_leases=body.dns_track_dynamic_leases,
        address_family=address_family,
        v6_address_mode=body.v6_address_mode,
        ra_managed_flag=body.ra_managed_flag,
        ra_other_flag=body.ra_other_flag,
        ra_enabled=body.ra_enabled,
        ra_mo_override=body.ra_mo_override,
        ra_router_lifetime=body.ra_router_lifetime,
        ra_max_interval=body.ra_max_interval,
        ra_prefix_valid_lifetime=body.ra_prefix_valid_lifetime,
        ra_prefix_preferred_lifetime=body.ra_prefix_preferred_lifetime,
        ra_prefix_on_link=body.ra_prefix_on_link,
        ra_prefix_autonomous=body.ra_prefix_autonomous,
        ra_interface=body.ra_interface,
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
    # ``exclude_unset`` (not ``exclude_none``) so an explicit null can clear a
    # nullable column (e.g. resetting min_lease_time / max_lease_time to empty),
    # while a field the client didn't send stays untouched (#475). But keep a
    # null only for the fields we explicitly allow to clear — otherwise an
    # explicit null on a NOT-NULL column (name / description / is_active) would
    # 500 on commit, and a null a partial-body client sent for an unmanaged
    # nullable column would silently wipe it (both were dropped under
    # ``exclude_none``).
    changes = {
        k: v
        for k, v in body.model_dump(exclude_unset=True).items()
        if v is not None or k in NULLABLE_CLEARABLE_SCOPE_FIELDS
    }
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
        normalized = _normalize_options(changes["options"])
        # Validate only domain options that CHANGED from the stored value
        # (issue #597 review) — the scope form round-trips the full options
        # dict, so re-validating an unchanged grandfathered value would block
        # an unrelated edit.
        validate_domain_options(normalized, previous=scope.options or {})
        changes["options"] = normalized
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

    Default soft-delete stamps the scope — plus its pools and reservations,
    which are cascade children (#617) — with a fresh batch UUID, so the whole
    set restores together from /admin/trash.

    Soft-delete means *stop serving immediately*, on every backend. The scope
    drops out of the rendered ConfigBundle at once (the global ``deleted_at IS
    NULL`` filter hides it) and ``collect_wake`` pushes agents to re-poll, so
    Kea members converge within seconds; the Windows write-through fires on this
    path too, so agentless members converge as well (#616). The purge sweep
    later hard-deletes the rows; it is not what makes the config change.

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
