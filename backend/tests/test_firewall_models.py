"""FirewallPolicy/Rule/Alias DB constraints (#285 Phase 3a).

The scope-shape + no-drop-22 + uniqueness guards are enforced at the DB
layer (a malformed policy/rule must never persist). One constraint per
test so a failed flush doesn't poison the shared session.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.firewall import FirewallPolicy, FirewallRule


async def _expect_integrity(db: AsyncSession, obj: object) -> None:
    db.add(obj)
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_scope_shape_fleet_with_role_rejected(db_session: AsyncSession) -> None:
    await _expect_integrity(
        db_session,
        FirewallPolicy(name="bad", scope_kind="fleet", scope_role="dhcp"),
    )


async def test_scope_shape_role_without_role_rejected(db_session: AsyncSession) -> None:
    await _expect_integrity(
        db_session, FirewallPolicy(name="bad", scope_kind="role", scope_role=None)
    )


async def test_scope_shape_appliance_without_id_rejected(db_session: AsyncSession) -> None:
    await _expect_integrity(
        db_session,
        FirewallPolicy(name="bad", scope_kind="appliance", scope_appliance_id=None),
    )


async def test_valid_scopes_accepted(db_session: AsyncSession) -> None:
    db_session.add_all(
        [
            FirewallPolicy(name="fleet", scope_kind="fleet"),
            FirewallPolicy(name="role", scope_kind="role", scope_role="dns-bind9"),
            # control-plane is a valid scope_role (merge-internal key, not a node label).
            FirewallPolicy(name="cp", scope_kind="role", scope_role="control-plane"),
        ]
    )
    await db_session.flush()


async def test_second_fleet_policy_rejected(db_session: AsyncSession) -> None:
    db_session.add(FirewallPolicy(name="fleet1", scope_kind="fleet"))
    await db_session.flush()
    await _expect_integrity(db_session, FirewallPolicy(name="fleet2", scope_kind="fleet"))


async def test_duplicate_role_policy_rejected(db_session: AsyncSession) -> None:
    db_session.add(FirewallPolicy(name="dns1", scope_kind="role", scope_role="dns-bind9"))
    await db_session.flush()
    await _expect_integrity(
        db_session, FirewallPolicy(name="dns2", scope_kind="role", scope_role="dns-bind9")
    )


async def test_rule_drop_ssh_rejected(db_session: AsyncSession) -> None:
    p = FirewallPolicy(name="r", scope_kind="role", scope_role="custom")
    db_session.add(p)
    await db_session.flush()
    await _expect_integrity(
        db_session,
        FirewallRule(policy_id=p.id, seq=10, action="drop", protocol="tcp", ports=[22]),
    )


async def test_rule_accept_ssh_allowed(db_session: AsyncSession) -> None:
    # The floor protection only blocks DROP on 22 — an accept is fine.
    p = FirewallPolicy(name="r2", scope_kind="role", scope_role="custom")
    db_session.add(p)
    await db_session.flush()
    db_session.add(
        FirewallRule(policy_id=p.id, seq=10, action="accept", protocol="tcp", ports=[22])
    )
    await db_session.flush()


async def test_duplicate_rule_seq_rejected(db_session: AsyncSession) -> None:
    p = FirewallPolicy(name="r3", scope_kind="role", scope_role="custom")
    db_session.add(p)
    await db_session.flush()
    db_session.add(
        FirewallRule(policy_id=p.id, seq=10, action="accept", protocol="tcp", ports=[80])
    )
    await db_session.flush()
    await _expect_integrity(
        db_session,
        FirewallRule(policy_id=p.id, seq=10, action="accept", protocol="udp", ports=[53]),
    )


async def test_appliance_scoped_policy_accepted(db_session: AsyncSession) -> None:
    import hashlib
    import os

    from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance

    der = os.urandom(32)
    a = Appliance(
        id=uuid.uuid4(),
        hostname="n1",
        public_key_der=der,
        public_key_fingerprint=hashlib.sha256(der).hexdigest(),
        state=APPLIANCE_STATE_APPROVED,
        deployment_kind="appliance",
    )
    db_session.add(a)
    await db_session.flush()
    db_session.add(FirewallPolicy(name="ovr", scope_kind="appliance", scope_appliance_id=a.id))
    await db_session.flush()
