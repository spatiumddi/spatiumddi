"""Groups admin API — full CRUD + role/user assignment.

Read access requires authentication only (the Auth Providers page and the
admin pages both need to list groups). Mutations require `admin` on the
`group` resource (or superadmin).
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import DB, CurrentUser
from app.core.permissions import require_permission
from app.models.audit import AuditLog
from app.models.auth import Group, Role, User

logger = structlog.get_logger(__name__)
router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────


class GroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    auth_source: str
    external_dn: str | None = None
    role_ids: list[uuid.UUID] = []
    user_ids: list[uuid.UUID] = []


class GroupCreate(BaseModel):
    name: str
    description: str = ""
    auth_source: str = "local"
    external_dn: str | None = None
    role_ids: list[uuid.UUID] = []
    user_ids: list[uuid.UUID] = []

    @field_validator("name")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Group name cannot be empty")
        return v

    @field_validator("auth_source")
    @classmethod
    def _source(cls, v: str) -> str:
        if v not in {"local", "ldap", "oidc", "saml"}:
            raise ValueError("auth_source must be local, ldap, oidc, or saml")
        return v


class GroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    external_dn: str | None = None
    role_ids: list[uuid.UUID] | None = None
    user_ids: list[uuid.UUID] | None = None

    @field_validator("name")
    @classmethod
    def _nonempty(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Group name cannot be empty")
        return v


# ── Helpers ───────────────────────────────────────────────────────────────────


def _to_response(g: Group) -> GroupResponse:
    return GroupResponse(
        id=g.id,
        name=g.name,
        description=g.description,
        auth_source=g.auth_source,
        external_dn=g.external_dn,
        role_ids=[r.id for r in g.roles],
        user_ids=[u.id for u in g.users],
    )


async def _load_group(db: DB, group_id: uuid.UUID) -> Group:
    stmt = (
        select(Group)
        .where(Group.id == group_id)
        .options(selectinload(Group.roles), selectinload(Group.users))
    )
    g = (await db.execute(stmt)).scalar_one_or_none()
    if g is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return g


def _audit(actor: User, action: str, group: Group, summary: str) -> AuditLog:
    return AuditLog(
        user_id=actor.id,
        user_display_name=actor.display_name,
        auth_source=actor.auth_source,
        action=action,
        resource_type="group",
        resource_id=str(group.id),
        resource_display=summary,
    )


async def _resolve_roles(db: DB, role_ids: list[uuid.UUID]) -> list[Role]:
    if not role_ids:
        return []
    stmt = select(Role).where(Role.id.in_(role_ids))
    roles = list((await db.execute(stmt)).scalars().all())
    if len(roles) != len(set(role_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more role_ids do not exist",
        )
    return roles


async def _resolve_users(db: DB, user_ids: list[uuid.UUID]) -> list[User]:
    if not user_ids:
        return []
    stmt = select(User).where(User.id.in_(user_ids))
    users = list((await db.execute(stmt)).scalars().all())
    if len(users) != len(set(user_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more user_ids do not exist",
        )
    return users


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[GroupResponse])
async def list_groups(db: DB, _: CurrentUser) -> list[GroupResponse]:
    """Listing groups is available to any authenticated user.

    The Auth Providers admin page and the Groups admin page both rely on this
    to populate pickers. Mutations require `admin` on `group`.
    """
    stmt = (
        select(Group)
        .order_by(Group.name)
        .options(selectinload(Group.roles), selectinload(Group.users))
    )
    res = await db.execute(stmt)
    return [_to_response(g) for g in res.unique().scalars().all()]


@router.post(
    "",
    response_model=GroupResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("admin", "group"))],
)
async def create_group(body: GroupCreate, current_user: CurrentUser, db: DB) -> GroupResponse:
    existing = await db.scalar(select(Group).where(Group.name == body.name))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Group name already in use"
        )

    roles = await _resolve_roles(db, body.role_ids)
    users = await _resolve_users(db, body.user_ids)

    g = Group(
        name=body.name,
        description=body.description,
        auth_source=body.auth_source,
        external_dn=body.external_dn,
    )
    g.roles = roles
    g.users = users
    db.add(g)
    await db.flush()
    db.add(_audit(current_user, "create", g, f"Created group {g.name}"))
    await db.commit()
    # Reload with eager relationships for the response
    g = await _load_group(db, g.id)
    logger.info("group_created", name=g.name, by=current_user.username)
    return _to_response(g)


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(group_id: uuid.UUID, db: DB, _: CurrentUser) -> GroupResponse:
    g = await _load_group(db, group_id)
    return _to_response(g)


@router.put(
    "/{group_id}",
    response_model=GroupResponse,
    dependencies=[Depends(require_permission("admin", "group"))],
)
async def update_group(
    group_id: uuid.UUID, body: GroupUpdate, current_user: CurrentUser, db: DB
) -> GroupResponse:
    g = await _load_group(db, group_id)

    if body.name is not None and body.name != g.name:
        dup = await db.scalar(select(Group).where(Group.name == body.name, Group.id != g.id))
        if dup is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Group name already in use"
            )
        g.name = body.name
    if body.description is not None:
        g.description = body.description
    if body.external_dn is not None:
        g.external_dn = body.external_dn
    if body.role_ids is not None:
        g.roles = await _resolve_roles(db, body.role_ids)
    if body.user_ids is not None:
        g.users = await _resolve_users(db, body.user_ids)

    db.add(_audit(current_user, "update", g, f"Updated group {g.name}"))
    await db.commit()
    g = await _load_group(db, g.id)
    return _to_response(g)


@router.delete(
    "/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("admin", "group"))],
)
async def delete_group(group_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    g = await _load_group(db, group_id)
    name = g.name
    db.add(_audit(current_user, "delete", g, f"Deleted group {name}"))
    await db.delete(g)
    await db.commit()
    logger.info("group_deleted", name=name, by=current_user.username)
