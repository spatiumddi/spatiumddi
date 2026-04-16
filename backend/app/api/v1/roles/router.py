"""Roles admin API — CRUD + clone.

Built-in roles (`is_builtin=True`) cannot be deleted and their permissions
are overwritten on every boot to follow the shipped defaults. Admins who
need to tweak a built-in role should clone it first.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.api.deps import DB, CurrentUser
from app.core.permissions import require_permission
from app.models.audit import AuditLog
from app.models.auth import Role, User

logger = structlog.get_logger(__name__)
router = APIRouter()


_VALID_ACTIONS = {"read", "write", "delete", "admin", "*"}


# ── Schemas ───────────────────────────────────────────────────────────────────


class PermissionEntry(BaseModel):
    action: str
    resource_type: str
    resource_id: str | None = None

    @field_validator("action")
    @classmethod
    def _valid_action(cls, v: str) -> str:
        v = v.strip()
        if v not in _VALID_ACTIONS:
            raise ValueError(
                f"action must be one of {sorted(_VALID_ACTIONS)}; see docs/PERMISSIONS.md"
            )
        return v

    @field_validator("resource_type")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("resource_type cannot be empty; use '*' for wildcard")
        return v


class RoleResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    is_builtin: bool
    permissions: list[dict[str, Any]]


class RoleCreate(BaseModel):
    name: str
    description: str = ""
    permissions: list[PermissionEntry] = []

    @field_validator("name")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Role name cannot be empty")
        return v


class RoleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    permissions: list[PermissionEntry] | None = None

    @field_validator("name")
    @classmethod
    def _nonempty(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Role name cannot be empty")
        return v


class RoleClone(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Clone name cannot be empty")
        return v


# ── Helpers ───────────────────────────────────────────────────────────────────


def _to_response(r: Role) -> RoleResponse:
    return RoleResponse(
        id=r.id,
        name=r.name,
        description=r.description,
        is_builtin=r.is_builtin,
        permissions=list(r.permissions or []),
    )


def _perm_list_to_dicts(perms: list[PermissionEntry]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in perms:
        entry: dict[str, Any] = {
            "action": p.action,
            "resource_type": p.resource_type,
        }
        if p.resource_id:
            entry["resource_id"] = p.resource_id
        out.append(entry)
    return out


def _audit(actor: User, action: str, role: Role, summary: str) -> AuditLog:
    return AuditLog(
        user_id=actor.id,
        user_display_name=actor.display_name,
        auth_source=actor.auth_source,
        action=action,
        resource_type="role",
        resource_id=str(role.id),
        resource_display=summary,
    )


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[RoleResponse])
async def list_roles(db: DB, _: CurrentUser) -> list[RoleResponse]:
    """Any authenticated user may list roles (the Groups admin page needs
    this to populate the role picker). Mutations require admin on `role`."""
    res = await db.execute(select(Role).order_by(Role.is_builtin.desc(), Role.name))
    return [_to_response(r) for r in res.scalars().all()]


@router.post(
    "",
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("admin", "role"))],
)
async def create_role(body: RoleCreate, current_user: CurrentUser, db: DB) -> RoleResponse:
    existing = await db.scalar(select(Role).where(Role.name == body.name))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Role name already in use"
        )
    r = Role(
        name=body.name,
        description=body.description,
        is_builtin=False,
        permissions=_perm_list_to_dicts(body.permissions),
    )
    db.add(r)
    await db.flush()
    db.add(_audit(current_user, "create", r, f"Created role {r.name}"))
    await db.commit()
    await db.refresh(r)
    logger.info("role_created", name=r.name, by=current_user.username)
    return _to_response(r)


@router.get("/{role_id}", response_model=RoleResponse)
async def get_role(role_id: uuid.UUID, db: DB, _: CurrentUser) -> RoleResponse:
    r = await db.get(Role, role_id)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    return _to_response(r)


@router.put(
    "/{role_id}",
    response_model=RoleResponse,
    dependencies=[Depends(require_permission("admin", "role"))],
)
async def update_role(
    role_id: uuid.UUID, body: RoleUpdate, current_user: CurrentUser, db: DB
) -> RoleResponse:
    r = await db.get(Role, role_id)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    if r.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Built-in roles cannot be edited directly. Clone this role first.",
        )
    if body.name is not None and body.name != r.name:
        dup = await db.scalar(select(Role).where(Role.name == body.name, Role.id != r.id))
        if dup is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Role name already in use"
            )
        r.name = body.name
    if body.description is not None:
        r.description = body.description
    if body.permissions is not None:
        r.permissions = _perm_list_to_dicts(body.permissions)
    db.add(_audit(current_user, "update", r, f"Updated role {r.name}"))
    await db.commit()
    await db.refresh(r)
    return _to_response(r)


@router.delete(
    "/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("admin", "role"))],
)
async def delete_role(role_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    r = await db.get(Role, role_id)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    if r.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Built-in roles cannot be deleted. Clone and delete the clone instead.",
        )
    name = r.name
    db.add(_audit(current_user, "delete", r, f"Deleted role {name}"))
    await db.delete(r)
    await db.commit()
    logger.info("role_deleted", name=name, by=current_user.username)


@router.post(
    "/{role_id}/clone",
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("admin", "role"))],
)
async def clone_role(
    role_id: uuid.UUID, body: RoleClone, current_user: CurrentUser, db: DB
) -> RoleResponse:
    src = await db.get(Role, role_id)
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    existing = await db.scalar(select(Role).where(Role.name == body.name))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Role name already in use"
        )
    clone = Role(
        name=body.name,
        description=f"Cloned from '{src.name}'",
        is_builtin=False,
        permissions=list(src.permissions or []),
    )
    db.add(clone)
    await db.flush()
    db.add(_audit(current_user, "create", clone, f"Cloned role {src.name} → {clone.name}"))
    await db.commit()
    await db.refresh(clone)
    return _to_response(clone)
