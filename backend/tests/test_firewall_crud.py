"""Fleet-firewall policy/rule/alias CRUD API (#285 Phase 3c-1).

Covers the operator contract: scope-shape validation, the builtin identity
lock (rules stay editable), the no-drop-22 floor at the API layer, scope
uniqueness, audit rows + the audit→event-type mapping, and the
require_module 404 gate. The test DB has NO seeded builtins (conftest builds
the schema via create_all, not the seed migration), so user policies are
created freely without colliding with the seeded role policies.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.firewall import FirewallPolicy
from app.services.event_publisher import _audit_to_event_type
from app.services.feature_modules import invalidate_cache, set_module_enabled

FW = "/api/v1/appliance/firewall"


@pytest.fixture(autouse=True)
def _reset_module_cache():
    invalidate_cache()
    yield
    invalidate_cache()


async def _user(db: AsyncSession, *, superadmin: bool = True) -> tuple[User, dict]:
    u = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="T",
        hashed_password=hash_password("x"),
        is_superadmin=superadmin,
    )
    db.add(u)
    await db.flush()
    return u, {"Authorization": f"Bearer {create_access_token(str(u.id))}"}


# ── Policy CRUD ──────────────────────────────────────────────────────


async def test_policy_crud_lifecycle(client: AsyncClient, db_session: AsyncSession) -> None:
    _, h = await _user(db_session)
    r = await client.post(
        f"{FW}/policies",
        headers=h,
        json={"name": "my-custom", "scope_kind": "role", "scope_role": "custom"},
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["is_builtin"] is False

    _r = await client.get(f"{FW}/policies", headers=h)
    assert _r.status_code == 200
    _r = await client.get(f"{FW}/policies/{pid}", headers=h)
    assert _r.status_code == 200

    r = await client.patch(f"{FW}/policies/{pid}", headers=h, json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False

    _r = await client.delete(f"{FW}/policies/{pid}", headers=h)
    assert _r.status_code == 204
    _r = await client.get(f"{FW}/policies/{pid}", headers=h)
    assert _r.status_code == 404


async def test_create_bad_scope_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    _, h = await _user(db_session)
    # fleet must not carry a role
    r = await client.post(
        f"{FW}/policies",
        headers=h,
        json={"name": "x", "scope_kind": "fleet", "scope_role": "dhcp"},
    )
    assert r.status_code == 422
    # role must be a known role token
    r = await client.post(
        f"{FW}/policies",
        headers=h,
        json={"name": "x", "scope_kind": "role", "scope_role": "bogus"},
    )
    assert r.status_code == 422


async def test_scope_uniqueness_409(client: AsyncClient, db_session: AsyncSession) -> None:
    _, h = await _user(db_session)
    j = {"name": "f1", "scope_kind": "fleet"}
    _r = await client.post(f"{FW}/policies", headers=h, json=j)
    assert _r.status_code == 201
    r = await client.post(f"{FW}/policies", headers=h, json={"name": "f2", "scope_kind": "fleet"})
    assert r.status_code == 409


async def test_builtin_identity_locked(client: AsyncClient, db_session: AsyncSession) -> None:
    _, h = await _user(db_session)
    pid = (
        await client.post(
            f"{FW}/policies",
            headers=h,
            json={"name": "seeded", "scope_kind": "role", "scope_role": "dns-bind9"},
        )
    ).json()["id"]
    # Promote to builtin behind the API's back to exercise the lock.
    await db_session.execute(
        update(FirewallPolicy).where(FirewallPolicy.id == uuid.UUID(pid)).values(is_builtin=True)
    )
    await db_session.commit()

    # identity field rejected
    r = await client.patch(f"{FW}/policies/{pid}", headers=h, json={"name": "renamed"})
    assert r.status_code == 400 and "Clone" in r.json()["detail"]
    # mutable field allowed
    _r = await client.patch(f"{FW}/policies/{pid}", headers=h, json={"enabled": False})
    assert _r.status_code == 200
    # delete refused
    _r = await client.delete(f"{FW}/policies/{pid}", headers=h)
    assert _r.status_code == 400


# ── Rules ────────────────────────────────────────────────────────────


async def _policy(client: AsyncClient, h: dict, role: str = "custom") -> str:
    return (
        await client.post(
            f"{FW}/policies",
            headers=h,
            json={"name": f"p-{role}", "scope_kind": "role", "scope_role": role},
        )
    ).json()["id"]


async def test_rule_no_drop_22_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    _, h = await _user(db_session)
    pid = await _policy(client, h)
    r = await client.post(
        f"{FW}/policies/{pid}/rules",
        headers=h,
        json={"seq": 10, "action": "drop", "protocol": "tcp", "ports": [22]},
    )
    assert r.status_code == 422


async def test_rule_crud_and_bulk_replace(client: AsyncClient, db_session: AsyncSession) -> None:
    _, h = await _user(db_session)
    pid = await _policy(client, h)
    # add one
    r = await client.post(
        f"{FW}/policies/{pid}/rules",
        headers=h,
        json={"seq": 10, "action": "accept", "protocol": "udp", "ports": [53]},
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    # duplicate seq → 409
    _r = await client.post(
        f"{FW}/policies/{pid}/rules",
        headers=h,
        json={"seq": 10, "protocol": "tcp", "ports": [53]},
    )
    assert _r.status_code == 409
    # patch
    _r = await client.patch(
        f"{FW}/policies/{pid}/rules/{rid}",
        headers=h,
        json={"seq": 10, "protocol": "tcp", "ports": [53]},
    )
    assert _r.status_code == 200
    # bulk replace (single audit row)
    r = await client.put(
        f"{FW}/policies/{pid}/rules",
        headers=h,
        json={
            "rules": [
                {"seq": 10, "protocol": "udp", "ports": [53]},
                {"seq": 20, "protocol": "tcp", "ports": [53]},
            ]
        },
    )
    assert r.status_code == 200 and len(r.json()["rules"]) == 2
    # delete one
    new_rid = r.json()["rules"][0]["id"]
    _r = await client.delete(f"{FW}/policies/{pid}/rules/{new_rid}", headers=h)
    assert _r.status_code == 204


async def test_bulk_replace_dup_seq_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    _, h = await _user(db_session)
    pid = await _policy(client, h)
    r = await client.put(
        f"{FW}/policies/{pid}/rules",
        headers=h,
        json={
            "rules": [
                {"seq": 10, "protocol": "udp", "ports": [53]},
                {"seq": 10, "protocol": "tcp", "ports": [53]},
            ]
        },
    )
    assert r.status_code == 422


# ── Aliases ──────────────────────────────────────────────────────────


async def test_alias_crud_and_family_split(client: AsyncClient, db_session: AsyncSession) -> None:
    _, h = await _user(db_session)
    # v6 in v4_members rejected (family split at rest)
    r = await client.post(
        f"{FW}/aliases",
        headers=h,
        json={"name": "bad", "kind": "cidr", "v4_members": ["2001:db8::/64"]},
    )
    assert r.status_code == 422
    # valid
    r = await client.post(
        f"{FW}/aliases",
        headers=h,
        json={
            "name": "mgmt",
            "kind": "cidr",
            "v4_members": ["10.0.0.0/8"],
            "v6_members": ["2001:db8::/64"],
        },
    )
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    _r = await client.get(f"{FW}/aliases", headers=h)
    assert _r.json()[0]["name"] == "mgmt"
    _r = await client.patch(
        f"{FW}/aliases/{aid}", headers=h, json={"v4_members": ["172.16.0.0/12"]}
    )
    assert _r.status_code == 200
    _r = await client.delete(f"{FW}/aliases/{aid}", headers=h)
    assert _r.status_code == 204


# ── Audit + event wiring ─────────────────────────────────────────────


async def test_audit_row_written_and_event_type(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, h = await _user(db_session)
    await _policy(client, h, role="observer")
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.resource_type == "firewall_policy")
            )
        )
        .scalars()
        .all()
    )
    assert any(r.action == "create" for r in rows)
    # The namespace mapping the auto-fire listener uses (proves typed events flow).
    assert _audit_to_event_type("create", "firewall_policy") == "firewall.policy.created"
    assert _audit_to_event_type("update", "firewall_rule") == "firewall.rule.updated"
    assert _audit_to_event_type("delete", "firewall_alias") == "firewall.alias.deleted"


# ── Permissions + module gate ────────────────────────────────────────


async def test_requires_auth(client: AsyncClient) -> None:
    _r = await client.get(f"{FW}/policies")
    assert _r.status_code == 401


async def test_non_admin_cannot_write(client: AsyncClient, db_session: AsyncSession) -> None:
    # A non-superadmin with no appliance grant can't write (admin) — read 403 too.
    _, h = await _user(db_session, superadmin=False)
    r = await client.post(f"{FW}/policies", headers=h, json={"name": "x", "scope_kind": "fleet"})
    assert r.status_code == 403


async def test_module_gate_404(client: AsyncClient, db_session: AsyncSession) -> None:
    u, h = await _user(db_session)
    await set_module_enabled(db_session, "appliance.firewall", False, user_id=u.id)
    await db_session.commit()
    invalidate_cache()
    try:
        _r = await client.get(f"{FW}/policies", headers=h)
        assert _r.status_code == 404
    finally:
        invalidate_cache()
