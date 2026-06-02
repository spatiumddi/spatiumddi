"""Fleet-firewall policy / rule / alias CRUD (#285 Phase 3c-1).

The operator surface over the declarative policy model (3a) that the merge
engine (3b) compiles. Everything here is DARK twice over: behind the
``appliance.firewall`` feature module (the router-level ``require_module`` in
``router.py`` 404s when off) AND the ``platform_settings.firewall_enabled``
master switch (editing a policy never touches a node until enforcement is on
AND the next heartbeat renders).

Permissions reuse the existing ``appliance`` resource (read = GET, admin =
writes) — the firewall family is appliance-fleet administration, so anyone
who administers the fleet administers its firewall. (The design's separate
``firewall`` permission string is a future refinement; reusing ``appliance``
keeps the builtin-role seeds untouched.)

Builtin policies (``is_builtin=True``) lock their IDENTITY (``name`` /
``scope_kind`` / ``scope_role``) — clone to re-scope — but their RULES stay
fully editable (the whole point: operators tune role ports). The no-drop-22
DB CHECK is the floor backstop; this layer rejects it with a friendly 422.

Mutations write an ``AuditLog`` row whose ``resource_type`` is mapped in
``event_publisher._RESOURCE_NAMESPACE``, so ``firewall.{policy,rule,alias}.*``
typed webhook events fire automatically — no explicit publish call. Every
mutation also drops the merge's policy cache so the next heartbeat re-reads.

The ``effective`` + ``preview`` read surfaces (which re-derive the per-node
render inputs the heartbeat uses) land in 3c-2 alongside the heartbeat-input
extraction; this commit is the CRUD contract.
"""

from __future__ import annotations

import ipaddress
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.deps import DB, CurrentUser
from app.core.permissions import user_has_permission
from app.models.audit import AuditLog
from app.models.firewall import _POLICY_ROLES, FirewallAlias, FirewallPolicy, FirewallRule
from app.services.appliance.firewall_merge import reset_policy_cache

router = APIRouter()

FIREWALL_RESOURCE = "appliance"

_SCOPE_KINDS = frozenset({"fleet", "role", "appliance"})
_ROLES = frozenset(_POLICY_ROLES)
_SOURCE_KINDS = frozenset(
    {"any", "cidr", "alias", "cluster_peers", "pod_cidr", "service_cidr", "kubeapi", "mgmt", "vip"}
)
_PROTOCOLS = frozenset({"tcp", "udp", "icmp", "icmpv6"})
_ACTIONS = frozenset({"accept", "drop"})
_FAMILIES = frozenset({"v4", "v6", "both"})
_ALIAS_KINDS = frozenset({"port", "cidr"})

# Builtin policies accept only these; identity (name/scope_*) is locked.
_BUILTIN_MUTABLE_FIELDS = frozenset({"enabled", "priority", "description"})


# ── Permission helpers ──────────────────────────────────────────────


def _require_read(user: object) -> None:
    if not user_has_permission(user, "read", FIREWALL_RESOURCE):  # type: ignore[arg-type]
        raise HTTPException(status_code=403, detail="Permission denied: need 'read' on 'appliance'")


def _require_admin(user: object) -> None:
    if not user_has_permission(user, "admin", FIREWALL_RESOURCE):  # type: ignore[arg-type]
        raise HTTPException(
            status_code=403, detail="Permission denied: need 'admin' on 'appliance'"
        )


# ── Schemas ─────────────────────────────────────────────────────────


class RuleIn(BaseModel):
    seq: int = Field(..., ge=0)
    action: str = "accept"
    protocol: str
    ports: list[int] = Field(default_factory=list)
    source_kind: str = "any"
    source_cidrs: list[str] = Field(default_factory=list)
    source_alias: str | None = None
    family: str = "both"
    comment: str | None = Field(None, max_length=120)
    render_guard: dict[str, Any] | None = None
    enabled: bool = True

    @field_validator("action")
    @classmethod
    def _v_action(cls, v: str) -> str:
        if v not in _ACTIONS:
            raise ValueError(f"action must be one of {sorted(_ACTIONS)}")
        return v

    @field_validator("protocol")
    @classmethod
    def _v_proto(cls, v: str) -> str:
        if v not in _PROTOCOLS:
            raise ValueError(f"protocol must be one of {sorted(_PROTOCOLS)}")
        return v

    @field_validator("source_kind")
    @classmethod
    def _v_skind(cls, v: str) -> str:
        if v not in _SOURCE_KINDS:
            raise ValueError(f"source_kind must be one of {sorted(_SOURCE_KINDS)}")
        return v

    @field_validator("family")
    @classmethod
    def _v_family(cls, v: str) -> str:
        if v not in _FAMILIES:
            raise ValueError(f"family must be one of {sorted(_FAMILIES)}")
        return v

    @field_validator("ports")
    @classmethod
    def _v_ports(cls, v: list[int]) -> list[int]:
        for p in v:
            if not (0 <= p <= 65535):
                raise ValueError(f"port out of range: {p}")
        return v

    @model_validator(mode="after")
    def _v_shape(self) -> RuleIn:
        # Floor backstop (the DB CHECK is authoritative; this is the friendly 422).
        if self.action == "drop" and 22 in self.ports:
            raise ValueError("a rule may not DROP port 22 (ssh) — the mgmt floor is un-removable")
        if self.source_kind == "cidr":
            if not self.source_cidrs:
                raise ValueError("source_kind='cidr' requires source_cidrs")
            for c in self.source_cidrs:
                try:
                    ipaddress.ip_network(c, strict=False)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"invalid CIDR {c!r}: {exc}") from exc
        if self.source_kind == "alias" and not self.source_alias:
            raise ValueError("source_kind='alias' requires source_alias")
        return self


class RuleResponse(RuleIn):
    id: uuid.UUID
    policy_id: uuid.UUID

    model_config = {"from_attributes": True}


class PolicyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    scope_kind: str
    scope_role: str | None = None
    scope_appliance_id: uuid.UUID | None = None
    enabled: bool = True
    priority: int = 100

    @field_validator("scope_kind")
    @classmethod
    def _v_kind(cls, v: str) -> str:
        if v not in _SCOPE_KINDS:
            raise ValueError(f"scope_kind must be one of {sorted(_SCOPE_KINDS)}")
        return v

    @model_validator(mode="after")
    def _v_scope(self) -> PolicyCreate:
        if self.scope_kind == "fleet":
            if self.scope_role is not None or self.scope_appliance_id is not None:
                raise ValueError("fleet scope takes neither scope_role nor scope_appliance_id")
        elif self.scope_kind == "role":
            if self.scope_role not in _ROLES:
                raise ValueError(f"scope_role must be one of {sorted(_ROLES)}")
            if self.scope_appliance_id is not None:
                raise ValueError("role scope takes no scope_appliance_id")
        elif self.scope_kind == "appliance":
            if self.scope_appliance_id is None:
                raise ValueError("appliance scope requires scope_appliance_id")
            if self.scope_role is not None:
                raise ValueError("appliance scope takes no scope_role")
        return self


class PolicyUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    enabled: bool | None = None
    priority: int | None = None


class PolicyResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    scope_kind: str
    scope_role: str | None
    scope_appliance_id: uuid.UUID | None
    enabled: bool
    is_builtin: bool
    priority: int
    rules: list[RuleResponse]

    model_config = {"from_attributes": True}


class RulesBulkReplace(BaseModel):
    rules: list[RuleIn]


class AliasCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    kind: str
    port_members: list[int] = Field(default_factory=list)
    v4_members: list[str] = Field(default_factory=list)
    v6_members: list[str] = Field(default_factory=list)
    description: str | None = Field(None, max_length=2000)

    @field_validator("kind")
    @classmethod
    def _v_kind(cls, v: str) -> str:
        if v not in _ALIAS_KINDS:
            raise ValueError(f"kind must be one of {sorted(_ALIAS_KINDS)}")
        return v

    @model_validator(mode="after")
    def _v_members(self) -> AliasCreate:
        for fam, members, want in (("v4", self.v4_members, 4), ("v6", self.v6_members, 6)):
            for c in members:
                try:
                    net = ipaddress.ip_network(c, strict=False)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"invalid {fam} CIDR {c!r}: {exc}") from exc
                if net.version != want:
                    raise ValueError(f"{c!r} is not a v{want} network (family-split is at rest)")
        for p in self.port_members:
            if not (0 <= p <= 65535):
                raise ValueError(f"port out of range: {p}")
        return self


class AliasUpdate(BaseModel):
    port_members: list[int] | None = None
    v4_members: list[str] | None = None
    v6_members: list[str] | None = None
    description: str | None = Field(None, max_length=2000)


class AliasResponse(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    port_members: list[int]
    v4_members: list[str]
    v6_members: list[str]
    description: str | None
    is_builtin: bool

    model_config = {"from_attributes": True}


# ── Policy CRUD ─────────────────────────────────────────────────────


async def _get_policy(db: DB, policy_id: uuid.UUID) -> FirewallPolicy:
    # populate_existing so a re-query after a mutation refreshes the
    # identity-map instance's rules collection (rather than returning a stale
    # one) — matters when a long-lived session issues several ops in a row.
    p = (
        (
            await db.execute(
                select(FirewallPolicy)
                .where(FirewallPolicy.id == policy_id)
                .options(selectinload(FirewallPolicy.rules))
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if p is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    return p


@router.get("/policies", response_model=list[PolicyResponse])
async def list_policies(
    db: DB,
    current_user: CurrentUser,
    scope_kind: str | None = Query(None),
    scope_role: str | None = Query(None),
    scope_appliance_id: uuid.UUID | None = Query(None),
) -> list[FirewallPolicy]:
    _require_read(current_user)
    q = (
        select(FirewallPolicy)
        .options(selectinload(FirewallPolicy.rules))
        .order_by(FirewallPolicy.scope_kind, FirewallPolicy.scope_role, FirewallPolicy.name)
    )
    if scope_kind:
        q = q.where(FirewallPolicy.scope_kind == scope_kind)
    if scope_role:
        q = q.where(FirewallPolicy.scope_role == scope_role)
    if scope_appliance_id:
        q = q.where(FirewallPolicy.scope_appliance_id == scope_appliance_id)
    return list((await db.execute(q)).scalars().all())


@router.post("/policies", response_model=PolicyResponse, status_code=status.HTTP_201_CREATED)
async def create_policy(body: PolicyCreate, db: DB, current_user: CurrentUser) -> FirewallPolicy:
    _require_admin(current_user)
    # Friendly pre-check for the scope-uniqueness constraints (one fleet
    # singleton / one policy per role / one override per appliance) so a
    # collision returns 409, not a raw IntegrityError 500.
    dup = select(FirewallPolicy.id)
    if body.scope_kind == "fleet":
        dup = dup.where(FirewallPolicy.scope_kind == "fleet")
    elif body.scope_kind == "role":
        dup = dup.where(FirewallPolicy.scope_role == body.scope_role)
    else:
        dup = dup.where(
            FirewallPolicy.scope_kind == "appliance",
            FirewallPolicy.scope_appliance_id == body.scope_appliance_id,
        )
    if (await db.execute(dup)).first() is not None:
        raise HTTPException(
            status_code=409, detail=f"a {body.scope_kind} policy for this scope already exists"
        )
    p = FirewallPolicy(**body.model_dump(), is_builtin=False, updated_by_id=current_user.id)
    db.add(p)
    db.add(
        AuditLog(
            action="create",
            resource_type="firewall_policy",
            resource_id=str(p.id),
            resource_display=body.name,
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
            new_value={
                "name": body.name,
                "scope_kind": body.scope_kind,
                "scope_role": body.scope_role,
            },
        )
    )
    await db.commit()
    reset_policy_cache()
    return await _get_policy(db, p.id)


@router.get("/policies/{policy_id}", response_model=PolicyResponse)
async def get_policy(policy_id: uuid.UUID, db: DB, current_user: CurrentUser) -> FirewallPolicy:
    _require_read(current_user)
    return await _get_policy(db, policy_id)


@router.patch("/policies/{policy_id}", response_model=PolicyResponse)
async def update_policy(
    policy_id: uuid.UUID, body: PolicyUpdate, db: DB, current_user: CurrentUser
) -> FirewallPolicy:
    _require_admin(current_user)
    p = await _get_policy(db, policy_id)
    payload = body.model_dump(exclude_unset=True)
    if p.is_builtin:
        offending = sorted(set(payload) - _BUILTIN_MUTABLE_FIELDS)
        if offending:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Built-in policies only accept updates to {sorted(_BUILTIN_MUTABLE_FIELDS)}; "
                    f"rejected: {offending}. Clone the policy first to change identity fields."
                ),
            )
    changed: dict[str, Any] = {}
    for key, value in payload.items():
        old = getattr(p, key)
        if old != value:
            changed[key] = {"old": str(old)[:200], "new": str(value)[:200]}
            setattr(p, key, value)
    if changed:
        p.updated_by_id = current_user.id
        db.add(
            AuditLog(
                action="update",
                resource_type="firewall_policy",
                resource_id=str(p.id),
                resource_display=p.name,
                user_id=current_user.id,
                user_display_name=current_user.username,
                result="success",
                changed_fields=list(changed),
                new_value=changed,
            )
        )
    await db.commit()
    reset_policy_cache()
    return await _get_policy(db, policy_id)


@router.delete("/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(policy_id: uuid.UUID, db: DB, current_user: CurrentUser) -> None:
    _require_admin(current_user)
    p = await _get_policy(db, policy_id)
    if p.is_builtin:
        raise HTTPException(
            status_code=400,
            detail="Built-in policies cannot be deleted. Disable instead (PATCH enabled=false).",
        )
    name = p.name
    db.add(
        AuditLog(
            action="delete",
            resource_type="firewall_policy",
            resource_id=str(p.id),
            resource_display=name,
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
        )
    )
    await db.execute(delete(FirewallPolicy).where(FirewallPolicy.id == policy_id))
    await db.commit()
    reset_policy_cache()


# ── Rule CRUD (rules editable even on builtins) ─────────────────────


def _rule_kwargs(r: RuleIn) -> dict[str, Any]:
    return r.model_dump()


@router.put("/policies/{policy_id}/rules", response_model=PolicyResponse)
async def replace_rules(
    policy_id: uuid.UUID, body: RulesBulkReplace, db: DB, current_user: CurrentUser
) -> FirewallPolicy:
    """Bulk-replace a policy's rules in one shot — single audit row."""
    _require_admin(current_user)
    p = await _get_policy(db, policy_id)
    seqs = [r.seq for r in body.rules]
    if len(seqs) != len(set(seqs)):
        raise HTTPException(status_code=422, detail="duplicate seq in rules")
    await db.execute(delete(FirewallRule).where(FirewallRule.policy_id == p.id))
    for r in body.rules:
        db.add(FirewallRule(policy_id=p.id, **_rule_kwargs(r)))
    db.add(
        AuditLog(
            action="update",
            resource_type="firewall_rule",
            resource_id=str(p.id),
            resource_display=f"{p.name} rules",
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
            changed_fields=["rules"],
            new_value={"count": len(body.rules)},
        )
    )
    await db.commit()
    reset_policy_cache()
    return await _get_policy(db, policy_id)


@router.post(
    "/policies/{policy_id}/rules", response_model=RuleResponse, status_code=status.HTTP_201_CREATED
)
async def add_rule(
    policy_id: uuid.UUID, body: RuleIn, db: DB, current_user: CurrentUser
) -> FirewallRule:
    _require_admin(current_user)
    p = await _get_policy(db, policy_id)
    if any(r.seq == body.seq for r in p.rules):
        raise HTTPException(status_code=409, detail=f"seq {body.seq} already exists in this policy")
    rule = FirewallRule(policy_id=p.id, **_rule_kwargs(body))
    db.add(rule)
    db.add(
        AuditLog(
            action="create",
            resource_type="firewall_rule",
            resource_id=str(p.id),
            resource_display=f"{p.name} seq {body.seq}",
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
            new_value={"seq": body.seq, "protocol": body.protocol, "action": body.action},
        )
    )
    await db.commit()
    await db.refresh(rule)
    reset_policy_cache()
    return rule


@router.patch("/policies/{policy_id}/rules/{rule_id}", response_model=RuleResponse)
async def update_rule(
    policy_id: uuid.UUID, rule_id: uuid.UUID, body: RuleIn, db: DB, current_user: CurrentUser
) -> FirewallRule:
    _require_admin(current_user)
    rule = await db.get(FirewallRule, rule_id)
    if rule is None or rule.policy_id != policy_id:
        raise HTTPException(status_code=404, detail="Rule not found")
    for key, value in _rule_kwargs(body).items():
        setattr(rule, key, value)
    db.add(
        AuditLog(
            action="update",
            resource_type="firewall_rule",
            resource_id=str(policy_id),
            resource_display=f"rule {rule_id}",
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
            changed_fields=["rule"],
        )
    )
    await db.commit()
    await db.refresh(rule)
    reset_policy_cache()
    return rule


@router.delete("/policies/{policy_id}/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    policy_id: uuid.UUID, rule_id: uuid.UUID, db: DB, current_user: CurrentUser
) -> None:
    _require_admin(current_user)
    rule = await db.get(FirewallRule, rule_id)
    if rule is None or rule.policy_id != policy_id:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.add(
        AuditLog(
            action="delete",
            resource_type="firewall_rule",
            resource_id=str(policy_id),
            resource_display=f"rule {rule_id}",
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
        )
    )
    await db.execute(delete(FirewallRule).where(FirewallRule.id == rule_id))
    await db.commit()
    reset_policy_cache()


# ── Alias CRUD ──────────────────────────────────────────────────────


@router.get("/aliases", response_model=list[AliasResponse])
async def list_aliases(db: DB, current_user: CurrentUser) -> list[FirewallAlias]:
    _require_read(current_user)
    return list(
        (await db.execute(select(FirewallAlias).order_by(FirewallAlias.name))).scalars().all()
    )


@router.post("/aliases", response_model=AliasResponse, status_code=status.HTTP_201_CREATED)
async def create_alias(body: AliasCreate, db: DB, current_user: CurrentUser) -> FirewallAlias:
    _require_admin(current_user)
    a = FirewallAlias(**body.model_dump(), is_builtin=False)
    db.add(a)
    db.add(
        AuditLog(
            action="create",
            resource_type="firewall_alias",
            resource_id=str(a.id),
            resource_display=body.name,
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
            new_value={"name": body.name, "kind": body.kind},
        )
    )
    await db.commit()
    await db.refresh(a)
    reset_policy_cache()
    return a


@router.patch("/aliases/{alias_id}", response_model=AliasResponse)
async def update_alias(
    alias_id: uuid.UUID, body: AliasUpdate, db: DB, current_user: CurrentUser
) -> FirewallAlias:
    _require_admin(current_user)
    a = await db.get(FirewallAlias, alias_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Alias not found")
    payload = body.model_dump(exclude_unset=True)
    # Re-validate CIDR family-split on member edits via AliasCreate's validator.
    if any(k in payload for k in ("v4_members", "v6_members", "port_members")):
        AliasCreate(
            name=a.name,
            kind=a.kind,
            port_members=payload.get("port_members", a.port_members),
            v4_members=payload.get("v4_members", a.v4_members),
            v6_members=payload.get("v6_members", a.v6_members),
        )
    for key, value in payload.items():
        setattr(a, key, value)
    db.add(
        AuditLog(
            action="update",
            resource_type="firewall_alias",
            resource_id=str(a.id),
            resource_display=a.name,
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
            changed_fields=list(payload),
        )
    )
    await db.commit()
    await db.refresh(a)
    reset_policy_cache()
    return a


@router.delete("/aliases/{alias_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alias(alias_id: uuid.UUID, db: DB, current_user: CurrentUser) -> None:
    _require_admin(current_user)
    a = await db.get(FirewallAlias, alias_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Alias not found")
    if a.is_builtin:
        raise HTTPException(status_code=400, detail="Built-in aliases cannot be deleted.")
    name = a.name
    db.add(
        AuditLog(
            action="delete",
            resource_type="firewall_alias",
            resource_id=str(a.id),
            resource_display=name,
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
        )
    )
    await db.execute(delete(FirewallAlias).where(FirewallAlias.id == alias_id))
    await db.commit()
    reset_policy_cache()
