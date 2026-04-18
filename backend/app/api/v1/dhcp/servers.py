"""DHCP server CRUD + sync/approve/leases."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.api.v1.dhcp._audit import write_audit
from app.core.crypto import encrypt_dict
from app.core.permissions import require_resource_permission
from app.drivers.dhcp import is_agentless, is_read_only
from app.drivers.dhcp.registry import _DRIVERS as _DHCP_DRIVERS
from app.drivers.dhcp.windows import test_winrm_credentials
from app.models.dhcp import DHCPConfigOp, DHCPLease, DHCPServer
from app.services.dhcp.config_bundle import build_config_bundle
from app.services.dhcp.pull_leases import pull_leases_from_server

router = APIRouter(
    prefix="/servers",
    tags=["dhcp"],
    dependencies=[Depends(require_resource_permission("dhcp_server"))],
)

# Sourced from the registry so new drivers (e.g. windows_dhcp) are
# accepted automatically without having to touch this allowlist.
VALID_DRIVERS = frozenset(_DHCP_DRIVERS.keys())


class WindowsCredentialsInput(BaseModel):
    """Windows DHCP admin credentials (for driver='windows_dhcp').

    Stored Fernet-encrypted on ``DHCPServer.credentials_encrypted``.
    Server never returns the password back — responses only expose
    ``has_credentials``.

    All fields are optional to support **partial updates** on edit: if the
    server already has stored credentials, sending just ``{"transport":
    "kerberos"}`` (for example) decrypts the existing blob, merges the
    transport change, and re-encrypts. On create, ``username`` + ``password``
    are still required — the create endpoint validates that explicitly.
    """

    username: str | None = None
    password: str | None = None
    winrm_port: int | None = None
    # transport: ntlm | kerberos | basic | credssp
    transport: str | None = None
    use_tls: bool | None = None
    verify_tls: bool | None = None


class ServerCreate(BaseModel):
    name: str
    description: str = ""
    driver: str = "kea"
    host: str
    port: int = 67
    roles: list[str] = []
    server_group_id: uuid.UUID | None = None
    # Only used when driver='windows_dhcp' — ignored otherwise.
    windows_credentials: WindowsCredentialsInput | None = None

    @field_validator("driver")
    @classmethod
    def _d(cls, v: str) -> str:
        if v not in VALID_DRIVERS:
            raise ValueError(f"driver must be one of {sorted(VALID_DRIVERS)}")
        return v


class ServerUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    driver: str | None = None
    host: str | None = None
    port: int | None = None
    roles: list[str] | None = None
    server_group_id: uuid.UUID | None = None
    status: str | None = None
    # Pass a full ``WindowsCredentialsInput`` to replace creds; ``null``
    # (the default) leaves them untouched. To clear, set an empty dict
    # ``{}`` — server treats it as "remove credentials".
    windows_credentials: WindowsCredentialsInput | dict[str, Any] | None = None

    @field_validator("driver")
    @classmethod
    def _d(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_DRIVERS:
            raise ValueError(f"driver must be one of {sorted(VALID_DRIVERS)}")
        return v


class ServerResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    driver: str
    host: str
    port: int
    roles: list[str]
    server_group_id: uuid.UUID | None
    status: str
    last_sync_at: datetime | None
    last_health_check_at: datetime | None
    agent_registered: bool
    agent_approved: bool
    agent_last_seen: datetime | None
    agent_version: str | None
    config_etag: str | None
    config_pushed_at: datetime | None
    has_credentials: bool
    is_agentless: bool
    is_read_only: bool
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_model(cls, s: DHCPServer) -> ServerResponse:
        agentless = is_agentless(s.driver)
        return cls(
            id=s.id,
            name=s.name,
            description=s.description,
            driver=s.driver,
            host=s.host,
            port=s.port,
            roles=list(s.roles or []),
            server_group_id=s.server_group_id,
            status=s.status,
            last_sync_at=s.last_sync_at,
            last_health_check_at=s.last_health_check_at,
            agent_registered=s.agent_registered,
            # Agentless drivers have no agent to approve, so "approval" is
            # a no-op concept for them. Return True unconditionally so the
            # UI doesn't display a bogus "pending approval" affordance on
            # windows_dhcp rows (including rows created before the
            # create-path auto-approve landed).
            agent_approved=True if agentless else s.agent_approved,
            agent_last_seen=s.agent_last_seen,
            agent_version=s.agent_version,
            config_etag=s.config_etag,
            config_pushed_at=s.config_pushed_at,
            has_credentials=bool(s.credentials_encrypted),
            is_agentless=agentless,
            is_read_only=is_read_only(s.driver),
            created_at=s.created_at,
            modified_at=s.modified_at,
        )


class LeaseResponse(BaseModel):
    id: uuid.UUID
    server_id: uuid.UUID
    scope_id: uuid.UUID | None
    ip_address: str
    mac_address: str
    hostname: str | None
    state: str
    starts_at: datetime | None
    ends_at: datetime | None
    expires_at: datetime | None
    last_seen_at: datetime

    model_config = {"from_attributes": True}

    # asyncpg decodes INET / MACADDR columns into ipaddress.IPv4Address and
    # netaddr.EUI-like objects. Coerce to str for the wire — this hit our
    # lease list 500 when the first windows_dhcp lease landed.
    @field_validator("ip_address", "mac_address", mode="before")
    @classmethod
    def _to_str(cls, v: Any) -> Any:
        return str(v) if v is not None else v


class TestWindowsCredentialsRequest(BaseModel):
    """Pre-save dry-run: test a host + creds without writing them to the DB.

    For editing an existing server, omit ``credentials`` and pass the
    ``server_id`` — the endpoint decrypts the stored credentials and runs
    the same probe. If both are omitted, the request is rejected.
    """

    host: str
    credentials: WindowsCredentialsInput | None = None
    server_id: uuid.UUID | None = None


class TestResult(BaseModel):
    ok: bool
    message: str


class SyncLeasesResponse(BaseModel):
    server_leases: int
    imported: int
    refreshed: int
    ipam_created: int
    ipam_refreshed: int
    out_of_scope: int
    scopes_imported: int = 0
    scopes_refreshed: int = 0
    scopes_skipped_no_subnet: int = 0
    pools_synced: int = 0
    statics_synced: int = 0
    errors: list[str]


@router.get("", response_model=list[ServerResponse])
async def list_servers(db: DB, _: CurrentUser) -> list[ServerResponse]:
    res = await db.execute(select(DHCPServer).order_by(DHCPServer.name))
    return [ServerResponse.from_model(s) for s in res.scalars().all()]


@router.post("", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def create_server(body: ServerCreate, db: DB, user: SuperAdmin) -> ServerResponse:
    existing = await db.execute(select(DHCPServer).where(DHCPServer.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A DHCP server with that name exists")

    payload = body.model_dump(exclude={"windows_credentials"})
    s = DHCPServer(**payload)
    if body.driver == "windows_dhcp" and body.windows_credentials is not None:
        creds = body.windows_credentials.model_dump(exclude_none=True)
        if not creds.get("username") or not creds.get("password"):
            raise HTTPException(
                status_code=400,
                detail="windows_dhcp create requires both username and password",
            )
        # Fill in sensible defaults for optional fields not set by the client.
        creds.setdefault("winrm_port", 5985)
        creds.setdefault("transport", "ntlm")
        creds.setdefault("use_tls", False)
        creds.setdefault("verify_tls", False)
        s.credentials_encrypted = encrypt_dict(creds)
    # Agentless drivers have no agent to approve; skip the pending-approval
    # dance entirely so the UI doesn't show a bogus "Approve" button.
    if is_agentless(body.driver):
        s.agent_approved = True
    db.add(s)
    await db.flush()

    audit_payload = body.model_dump(mode="json", exclude={"windows_credentials"})
    audit_payload["windows_credentials_set"] = bool(body.windows_credentials)
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="dhcp_server",
        resource_id=str(s.id),
        resource_display=s.name,
        new_value=audit_payload,
    )
    await db.commit()
    await db.refresh(s)
    return ServerResponse.from_model(s)


@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(server_id: uuid.UUID, db: DB, _: CurrentUser) -> ServerResponse:
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")
    return ServerResponse.from_model(s)


@router.put("/{server_id}", response_model=ServerResponse)
async def update_server(
    server_id: uuid.UUID, body: ServerUpdate, db: DB, user: SuperAdmin
) -> ServerResponse:
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")

    changes = body.model_dump(exclude_none=True, exclude={"windows_credentials"})
    for k, v in changes.items():
        setattr(s, k, v)

    # Credentials handling:
    #   * None → leave alone
    #   * {}   → clear
    #   * dict with any subset of fields → decrypt-merge-reencrypt (so the
    #     UI can change just the transport/port/tls without re-typing the
    #     password).
    if body.windows_credentials is not None:
        if isinstance(body.windows_credentials, WindowsCredentialsInput):
            patch = body.windows_credentials.model_dump(exclude_none=True)
            if not patch:
                # Empty WindowsCredentialsInput — treat as no-op.
                pass
            elif s.credentials_encrypted:
                from app.core.crypto import decrypt_dict  # noqa: PLC0415

                existing = decrypt_dict(s.credentials_encrypted)
                existing.update(patch)
                s.credentials_encrypted = encrypt_dict(existing)
                changes["windows_credentials_updated"] = sorted(patch.keys())
            else:
                # No stored creds — require a full username/password on first set.
                if not patch.get("username") or not patch.get("password"):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "First-time credentials require both username and "
                            "password (other fields are optional)."
                        ),
                    )
                patch.setdefault("winrm_port", 5985)
                patch.setdefault("transport", "ntlm")
                patch.setdefault("use_tls", False)
                patch.setdefault("verify_tls", False)
                s.credentials_encrypted = encrypt_dict(patch)
                changes["windows_credentials_set"] = True
        elif body.windows_credentials == {}:
            s.credentials_encrypted = None
            changes["windows_credentials_cleared"] = True

    audit_payload = body.model_dump(mode="json", exclude_none=True, exclude={"windows_credentials"})
    if "windows_credentials_set" in changes:
        audit_payload["windows_credentials_set"] = True
    if "windows_credentials_cleared" in changes:
        audit_payload["windows_credentials_cleared"] = True

    write_audit(
        db,
        user=user,
        action="update",
        resource_type="dhcp_server",
        resource_id=str(s.id),
        resource_display=s.name,
        changed_fields=list(changes.keys()),
        new_value=audit_payload,
    )
    await db.commit()
    await db.refresh(s)
    return ServerResponse.from_model(s)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(server_id: uuid.UUID, db: DB, user: SuperAdmin) -> None:
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="dhcp_server",
        resource_id=str(s.id),
        resource_display=s.name,
    )
    await db.delete(s)
    await db.commit()


@router.post("/{server_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_server(server_id: uuid.UUID, db: DB, user: SuperAdmin) -> dict[str, str]:
    """Force a config push: rebuild the bundle, enqueue an apply_config op.

    Coalesces consecutive clicks: if an ``apply_config`` op is already
    pending for this server, reuse it instead of queueing another reload.

    Rejects read-only drivers (windows_dhcp): there's no config to push.
    """
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")
    if is_read_only(s.driver):
        raise HTTPException(
            status_code=400,
            detail=f"driver {s.driver!r} is read-only; use /sync-leases instead",
        )
    bundle = await build_config_bundle(db, s)
    s.config_etag = bundle.etag
    existing = await db.execute(
        select(DHCPConfigOp).where(
            DHCPConfigOp.server_id == s.id,
            DHCPConfigOp.op_type == "apply_config",
            DHCPConfigOp.status == "pending",
        )
    )
    op = existing.scalar_one_or_none()
    if op is None:
        op = DHCPConfigOp(
            server_id=s.id,
            op_type="apply_config",
            payload={"etag": bundle.etag},
            status="pending",
        )
        db.add(op)
        await db.flush()
    else:
        op.payload = {"etag": bundle.etag}
        await db.flush()
    write_audit(
        db,
        user=user,
        action="dhcp.server.sync",
        resource_type="dhcp_server",
        resource_id=str(s.id),
        resource_display=s.name,
        new_value={"etag": bundle.etag, "op_id": str(op.id)},
    )
    await db.commit()
    return {"status": "queued", "op_id": str(op.id), "etag": bundle.etag}


@router.post("/test-windows-credentials", response_model=TestResult)
async def test_windows_credentials_endpoint(
    body: TestWindowsCredentialsRequest, db: DB, _user: SuperAdmin
) -> TestResult:
    """Dry-run WinRM probe — reach the host, run ``Get-DhcpServerVersion``.

    Two modes:
      * **Pre-save** (create/edit form) — pass plaintext ``credentials``
        and the typed ``host``. Nothing is written to the DB.
      * **Post-save** (existing server) — pass ``server_id`` only;
        stored Fernet-encrypted credentials are decrypted and used.
    """
    from app.core.crypto import decrypt_dict  # noqa: PLC0415

    if body.credentials is not None:
        creds = body.credentials.model_dump(exclude_none=True)
        if not creds.get("username") or not creds.get("password"):
            # Partial credentials (e.g. transport-only tweak) — merge with
            # stored if a server_id was also sent, else reject as ambiguous.
            if body.server_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="Partial credentials require 'server_id' to merge with stored",
                )
            s = await db.get(DHCPServer, body.server_id)
            if s is None:
                raise HTTPException(status_code=404, detail="Server not found")
            if not s.credentials_encrypted:
                raise HTTPException(
                    status_code=400,
                    detail="Server has no stored credentials to merge against",
                )
            existing = decrypt_dict(s.credentials_encrypted)
            existing.update(creds)
            creds = existing
        creds.setdefault("winrm_port", 5985)
        creds.setdefault("transport", "ntlm")
        creds.setdefault("use_tls", False)
        creds.setdefault("verify_tls", False)
        host = body.host
    elif body.server_id is not None:
        s = await db.get(DHCPServer, body.server_id)
        if s is None:
            raise HTTPException(status_code=404, detail="Server not found")
        if not s.credentials_encrypted:
            raise HTTPException(status_code=400, detail="Server has no stored credentials to test")
        creds = decrypt_dict(s.credentials_encrypted)
        host = body.host or s.host
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either 'credentials' (dry-run) or 'server_id' (stored)",
        )

    ok, msg = await test_winrm_credentials(host, creds)
    return TestResult(ok=ok, message=msg)


@router.post("/{server_id}/sync-leases", response_model=SyncLeasesResponse)
async def sync_leases_now(server_id: uuid.UUID, db: DB, user: SuperAdmin) -> SyncLeasesResponse:
    """Poll the DHCP server for current leases and reconcile into the DB.

    Only valid for agentless drivers (windows_dhcp today). Agent-based
    drivers stream lease events continuously and don't need polling.
    """
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")
    if not is_agentless(s.driver):
        raise HTTPException(
            status_code=400,
            detail=f"driver {s.driver!r} is agent-based; leases arrive via the agent",
        )
    result = await pull_leases_from_server(db, s, apply=True)
    write_audit(
        db,
        user=user,
        action="dhcp.server.sync-leases",
        resource_type="dhcp_server",
        resource_id=str(s.id),
        resource_display=s.name,
        new_value={
            "server_leases": result.server_leases,
            "imported": result.imported,
            "refreshed": result.refreshed,
            "ipam_created": result.ipam_created,
            "ipam_refreshed": result.ipam_refreshed,
            "out_of_scope": result.out_of_scope,
            "scopes_imported": result.scopes_imported,
            "scopes_refreshed": result.scopes_refreshed,
            "scopes_skipped_no_subnet": result.scopes_skipped_no_subnet,
            "pools_synced": result.pools_synced,
            "statics_synced": result.statics_synced,
            "errors": result.errors[:20],
        },
    )
    await db.commit()
    return SyncLeasesResponse(
        server_leases=result.server_leases,
        imported=result.imported,
        refreshed=result.refreshed,
        ipam_created=result.ipam_created,
        ipam_refreshed=result.ipam_refreshed,
        out_of_scope=result.out_of_scope,
        scopes_imported=result.scopes_imported,
        scopes_refreshed=result.scopes_refreshed,
        scopes_skipped_no_subnet=result.scopes_skipped_no_subnet,
        pools_synced=result.pools_synced,
        statics_synced=result.statics_synced,
        errors=result.errors,
    )


@router.post("/{server_id}/approve", response_model=ServerResponse)
async def approve_server(server_id: uuid.UUID, db: DB, user: SuperAdmin) -> ServerResponse:
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")
    s.agent_approved = True
    write_audit(
        db,
        user=user,
        action="dhcp.server.approve",
        resource_type="dhcp_server",
        resource_id=str(s.id),
        resource_display=s.name,
    )
    await db.commit()
    await db.refresh(s)
    return ServerResponse.from_model(s)


@router.get("/{server_id}/leases", response_model=list[LeaseResponse])
async def list_leases(
    server_id: uuid.UUID, db: DB, _: CurrentUser, limit: int = 500
) -> list[DHCPLease]:
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")
    res = await db.execute(
        select(DHCPLease)
        .where(DHCPLease.server_id == server_id)
        .order_by(DHCPLease.last_seen_at.desc())
        .limit(min(limit, 5000))
    )
    return list(res.scalars().all())
