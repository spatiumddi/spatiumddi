"""Staged-edit preview endpoint (#285 Phase 3c-2).

POST /appliance/firewall/preview overlays staged operator rules onto the live
policy set, recompiles the node body, and returns the line diff + advisory
analysis (accept↔drop conflicts, redundancy) + the OS-upgrade-in-flight
advisory. Read-only; never mutates.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.models.auth import User
from app.models.system_upgrade import SystemUpgradeRun
from app.services.appliance.firewall_merge import reset_policy_cache
from app.services.feature_modules import invalidate_cache

FW = "/api/v1/appliance/firewall"


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_policy_cache()
    invalidate_cache()
    yield
    reset_policy_cache()
    invalidate_cache()


async def _admin(db: AsyncSession) -> dict:
    u = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@x.com",
        display_name="T",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(u.id))}"}


async def _appliance(db: AsyncSession) -> Appliance:
    der = os.urandom(32)
    a = Appliance(
        id=uuid.uuid4(),
        hostname=f"n-{uuid.uuid4().hex[:6]}",
        public_key_der=der,
        public_key_fingerprint=hashlib.sha256(der).hexdigest(),
        state=APPLIANCE_STATE_APPROVED,
        deployment_kind="appliance",
        appliance_variant="application",
    )
    db.add(a)
    await db.flush()
    return a


async def test_preview_404_unknown(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin(db_session)
    r = await client.post(f"{FW}/preview", headers=h, json={"appliance_id": str(uuid.uuid4())})
    assert r.status_code == 404


async def test_preview_diff_added(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin(db_session)
    a = await _appliance(db_session)
    await db_session.commit()
    r = await client.post(
        f"{FW}/preview",
        headers=h,
        json={
            "appliance_id": str(a.id),
            "fleet_rules": [
                {"seq": 10, "action": "accept", "protocol": "udp", "ports": [514], "comment": "sl"}
            ],
        },
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert any("dport 514" in ln for ln in j["added"]) and j["removed"] == []
    assert j["staging_id"] and j["upgrade_in_flight"] is False


async def test_preview_conflict_warning(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin(db_session)
    a = await _appliance(db_session)
    await db_session.commit()
    r = await client.post(
        f"{FW}/preview",
        headers=h,
        json={
            "appliance_id": str(a.id),
            "fleet_rules": [
                {
                    "seq": 10,
                    "action": "accept",
                    "protocol": "tcp",
                    "ports": [8080],
                    "source_kind": "cidr",
                    "source_cidrs": ["10.0.0.0/8"],
                },
                {
                    "seq": 20,
                    "action": "drop",
                    "protocol": "tcp",
                    "ports": [8080],
                    "source_kind": "cidr",
                    "source_cidrs": ["10.0.0.0/8"],
                },
            ],
        },
    )
    assert r.status_code == 200, r.text
    kinds = {w["kind"] for w in r.json()["warnings"]}
    assert "conflict" in kinds, r.json()["warnings"]


async def test_preview_redundant_warning(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin(db_session)
    a = await _appliance(db_session)
    await db_session.commit()
    rule = {
        "action": "accept",
        "protocol": "tcp",
        "ports": [8080],
        "source_kind": "cidr",
        "source_cidrs": ["10.0.0.0/8"],
    }
    r = await client.post(
        f"{FW}/preview",
        headers=h,
        json={
            "appliance_id": str(a.id),
            "fleet_rules": [{"seq": 10, **rule}, {"seq": 20, **rule}],
        },
    )
    assert r.status_code == 200, r.text
    assert any(w["kind"] == "redundant" for w in r.json()["warnings"])


async def test_preview_upgrade_advisory(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin(db_session)
    a = await _appliance(db_session)
    db_session.add(SystemUpgradeRun(kind="rolling", state="running", target_version="2026.06.02-1"))
    await db_session.commit()
    r = await client.post(f"{FW}/preview", headers=h, json={"appliance_id": str(a.id)})
    assert r.status_code == 200 and r.json()["upgrade_in_flight"] is True
