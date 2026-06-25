"""perf #454 — DNS bulk record-create fast-path + `is_recursive` recursion gate.

Two independent fixes surfaced running the perf suite (#452) against a real
appliance:

* **Bulk create** (`POST .../records/bulk-create`): the seeder did one POST per
  record; each singular create commits + bumps the zone SOA serial (one row, so
  concurrent creates serialize on it) + hashes the audit chain → ~6 records/s.
  The bulk path bumps the serial once, enqueues all ops in one batch, and commits
  once. Validates: records land, the serial bumps exactly once, one op row per
  record per enabled agent-based server, within-batch de-dupe, the size cap.

* **Recursion gate**: the bind9 renderer keyed `recursion yes|no;` off
  ``DNSServerOptions.recursion_enabled`` (default True), ignoring the DNS group's
  ``is_recursive``. So a group created ``is_recursive=False`` (the §4.9
  authoritative-only intent) silently stayed an open recursive resolver. The
  group flag must now force recursion off.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dns import (
    DNSRecord,
    DNSRecordOp,
    DNSServer,
    DNSServerGroup,
    DNSServerOptions,
    DNSZone,
)
from app.services.dns.agent_config import build_config_bundle


async def _admin_headers(db: AsyncSession) -> dict[str, str]:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _bind9_group(
    db: AsyncSession, *, is_recursive: bool = True, recursion_enabled: bool = True
) -> tuple[DNSServerGroup, DNSZone, DNSServer]:
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", is_recursive=is_recursive)
    db.add(grp)
    await db.flush()
    server = DNSServer(
        group_id=grp.id,
        driver="bind9",
        host="bind9.example.com",
        name=f"srv-{uuid.uuid4().hex[:6]}",
        is_primary=True,
        is_enabled=True,
    )
    db.add(server)
    db.add(DNSServerOptions(group_id=grp.id, recursion_enabled=recursion_enabled))
    zone = DNSZone(
        group_id=grp.id,
        name="campus.example.edu.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.campus.example.edu.",
        admin_email="admin.campus.example.edu.",
    )
    db.add(zone)
    await db.flush()
    return grp, zone, server


# ── Bulk create ──────────────────────────────────────────────────────────


async def test_bulk_create_inserts_all_with_single_serial_bump(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp, zone, server = await _bind9_group(db_session)
    serial_before = zone.last_serial
    await db_session.commit()

    records = [
        {"name": f"dev-{i:05d}", "record_type": "A", "value": f"10.0.0.{i % 250 + 1}"}
        for i in range(50)
    ]
    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records/bulk-create",
        headers=h,
        json={"records": records},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] == 50
    assert body["skipped"] == []
    assert body["target_serial"] is not None

    # All 50 records present in the zone.
    rows = (
        (await db_session.execute(select(DNSRecord).where(DNSRecord.zone_id == zone.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 50

    # Serial bumped exactly once (not 50 times).
    await db_session.refresh(zone)
    assert zone.last_serial == body["target_serial"]
    assert zone.last_serial > serial_before

    # One pending op row per record for the single enabled agent-based server.
    ops = (
        (await db_session.execute(select(DNSRecordOp).where(DNSRecordOp.server_id == server.id)))
        .scalars()
        .all()
    )
    assert len(ops) == 50
    assert all(o.op == "create" and o.state == "pending" for o in ops)
    assert all(o.target_serial == body["target_serial"] for o in ops)


async def test_bulk_create_dedupes_within_batch(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp, zone, _ = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records/bulk-create",
        headers=h,
        json={
            "records": [
                {"name": "dup", "record_type": "A", "value": "10.0.0.1"},
                {"name": "dup", "record_type": "A", "value": "10.0.0.1"},  # exact dup
                {"name": "dup", "record_type": "A", "value": "10.0.0.2"},  # diff value: kept
            ]
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] == 2
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["reason"] == "duplicate within batch"


async def test_bulk_create_rejects_empty_and_oversize(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp, zone, _ = await _bind9_group(db_session)
    await db_session.commit()

    base = f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records/bulk-create"
    empty = await client.post(base, headers=h, json={"records": []})
    assert empty.status_code == 422, empty.text

    over = await client.post(
        base,
        headers=h,
        json={
            "records": [
                {"name": f"d{i}", "record_type": "A", "value": "10.0.0.1"} for i in range(2001)
            ]
        },
    )
    assert over.status_code == 422, over.text


async def test_bulk_create_srv_normalizes_struct_fields(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp, zone, _ = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records/bulk-create",
        headers=h,
        json={
            "records": [
                {
                    "name": "_sip._tcp",
                    "record_type": "SRV",
                    "value": "sip.campus.example.edu.",
                    "priority": 10,
                    "weight": 20,
                    "port": 5060,
                },
            ]
        },
    )
    assert r.status_code == 201, r.text
    rec = (
        (await db_session.execute(select(DNSRecord).where(DNSRecord.zone_id == zone.id)))
        .scalars()
        .one()
    )
    assert (rec.priority, rec.weight, rec.port) == (10, 20, 5060)


# ── Recursion gate (perf #454) ───────────────────────────────────────────


async def test_group_is_recursive_false_forces_recursion_off(
    db_session: AsyncSession,
) -> None:
    # Options say recursion is ON, but the group is authoritative-only — the
    # group flag must win so the rendered config gets ``recursion no;``.
    _grp, _zone, server = await _bind9_group(db_session, is_recursive=False, recursion_enabled=True)
    await db_session.commit()

    bundle = await build_config_bundle(db_session, server)
    assert bundle["options"]["recursion_enabled"] is False


async def test_group_is_recursive_true_respects_options(
    db_session: AsyncSession,
) -> None:
    _grp, _zone, server = await _bind9_group(db_session, is_recursive=True, recursion_enabled=True)
    await db_session.commit()
    bundle = await build_config_bundle(db_session, server)
    assert bundle["options"]["recursion_enabled"] is True

    # And options can still turn it off on a recursive group.
    _grp2, _zone2, server2 = await _bind9_group(
        db_session, is_recursive=True, recursion_enabled=False
    )
    await db_session.commit()
    bundle2 = await build_config_bundle(db_session, server2)
    assert bundle2["options"]["recursion_enabled"] is False
