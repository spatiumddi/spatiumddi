"""#59 — packet-capture HTTP API tests (server vantage, Phase 1).

No tcpdump is spawned — the Celery dispatch is patched, so we verify the
router contract: create persists a queued row + audits, the appliance
vantage is rejected (Phase 2), list/get/cancel/bulk-delete work, the
download endpoint guards return 404 (never 500) when the artifact is
absent, the module gate 404s when disabled, and the permission gate 403s.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import Group, Role, User
from app.models.feature_module import FeatureModule
from app.models.pcap import PacketCapture
from app.services import feature_modules


@pytest.fixture(autouse=True)
def _reset_module_cache() -> None:
    feature_modules.invalidate_cache()
    yield
    feature_modules.invalidate_cache()


async def _superadmin(db: AsyncSession) -> tuple[User, str]:
    u = User(
        username=f"admin-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return u, create_access_token(str(u.id))


async def _user_with_perm(db: AsyncSession, perm: dict | None) -> tuple[User, str]:
    u = User(
        username=f"user-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="User",
        hashed_password=hash_password("x"),
        is_superadmin=False,
    )
    db.add(u)
    await db.flush()
    if perm is not None:
        role = Role(name=f"r-{uuid.uuid4().hex[:6]}", description="", permissions=[perm])
        db.add(role)
        await db.flush()
        group = Group(name=f"g-{uuid.uuid4().hex[:6]}", description="")
        group.roles = [role]
        group.users = [u]
        db.add(group)
        await db.flush()
    return u, create_access_token(str(u.id))


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _make_capture(
    db: AsyncSession, *, status: str = "completed", **kw: object
) -> PacketCapture:
    cap = PacketCapture(
        vantage_kind="server",
        vantage_label="control plane",
        interface="any",
        status=status,
        **kw,  # type: ignore[arg-type]
    )
    db.add(cap)
    await db.flush()
    return cap


# ── create ───────────────────────────────────────────────────────────


async def test_create_server_capture_queues_and_audits(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    with patch("app.tasks.pcap.run_capture_task.delay") as delay:
        r = await client.post(
            "/api/v1/pcap/captures",
            json={"interface": "any", "bpf_filter": "port 53", "max_duration_s": 30},
            headers=_hdr(token),
        )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["vantage_kind"] == "server"
    assert body["bpf_filter"] == "port 53"
    delay.assert_called_once()

    # audit row written
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.resource_type == "packet_capture")
            )
        )
        .scalars()
        .all()
    )
    assert any(a.action == "create" for a in rows)


async def test_create_appliance_vantage_rejected_phase1(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    r = await client.post(
        "/api/v1/pcap/captures",
        json={"vantage_kind": "appliance", "max_duration_s": 30},
        headers=_hdr(token),
    )
    assert r.status_code == 422
    assert "appliance" in r.json()["detail"].lower()


async def test_create_rejects_no_stop_condition(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    r = await client.post(
        "/api/v1/pcap/captures",
        json={
            "interface": "any",
            "max_duration_s": None,
            "max_packets": None,
            "max_bytes": None,
        },
        headers=_hdr(token),
    )
    assert r.status_code == 422


async def test_create_rejects_bad_bpf(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _superadmin(db_session)
    r = await client.post(
        "/api/v1/pcap/captures",
        json={"bpf_filter": "port 80; rm -rf /", "max_duration_s": 10},
        headers=_hdr(token),
    )
    assert r.status_code == 422


# ── list / get / cancel / bulk-delete ────────────────────────────────


async def test_list_and_get(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _superadmin(db_session)
    cap = await _make_capture(db_session, status="running")
    await db_session.commit()

    r = await client.get("/api/v1/pcap/captures", headers=_hdr(token))
    assert r.status_code == 200
    assert any(i["id"] == str(cap.id) for i in r.json()["items"])

    r = await client.get(f"/api/v1/pcap/captures/{cap.id}", headers=_hdr(token))
    assert r.status_code == 200
    assert r.json()["status"] == "running"


async def test_cancel_running_capture(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _superadmin(db_session)
    cap = await _make_capture(db_session, status="running")
    await db_session.commit()

    r = await client.delete(f"/api/v1/pcap/captures/{cap.id}", headers=_hdr(token))
    assert r.status_code == 204
    await db_session.refresh(cap)
    assert cap.status == "cancelled"


async def test_bulk_delete(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _superadmin(db_session)
    c1 = await _make_capture(db_session, status="completed")
    c2 = await _make_capture(db_session, status="running")
    await db_session.commit()

    r = await client.post(
        "/api/v1/pcap/captures/bulk-delete",
        json={"capture_ids": [str(c1.id), str(c2.id)]},
        headers=_hdr(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == 1
    assert body["cancelled"] == 1


# ── download guards ──────────────────────────────────────────────────


async def test_download_404_before_completion(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    cap = await _make_capture(db_session, status="running")
    await db_session.commit()
    r = await client.get(f"/api/v1/pcap/captures/{cap.id}/download", headers=_hdr(token))
    assert r.status_code == 404


async def test_download_404_structured_when_artifact_missing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    cap = await _make_capture(
        db_session, status="completed", pcap_path="/nonexistent/x.pcap", pcap_size_bytes=10
    )
    await db_session.commit()
    r = await client.get(f"/api/v1/pcap/captures/{cap.id}/download", headers=_hdr(token))
    assert r.status_code == 404
    assert "no longer on disk" in r.json()["detail"]


async def test_download_streams_pcap_and_audits(
    client: AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    _, token = await _superadmin(db_session)
    f = tmp_path / "cap.pcap"
    f.write_bytes(b"\xd4\xc3\xb2\xa1pcapbytes")
    cap = await _make_capture(
        db_session,
        status="completed",
        pcap_path=str(f),
        pcap_size_bytes=f.stat().st_size,
        finished_at=datetime.now(UTC),
    )
    await db_session.commit()

    r = await client.get(f"/api/v1/pcap/captures/{cap.id}/download", headers=_hdr(token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/vnd.tcpdump.pcap"
    assert ".pcap" in r.headers.get("content-disposition", "")
    assert r.content == b"\xd4\xc3\xb2\xa1pcapbytes"

    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.resource_type == "packet_capture")
            )
        )
        .scalars()
        .all()
    )
    assert any(a.action == "download" for a in rows)


# ── interfaces ───────────────────────────────────────────────────────


async def test_interfaces_server(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _superadmin(db_session)
    r = await client.get("/api/v1/pcap/interfaces?vantage=server", headers=_hdr(token))
    assert r.status_code == 200
    body = r.json()
    assert "any" in body["interfaces"]
    assert body["note"]


# ── gating: module + permission ──────────────────────────────────────


async def test_module_disabled_404(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _superadmin(db_session)
    db_session.add(FeatureModule(id="tools.pcap", enabled=False))
    await db_session.commit()
    feature_modules.invalidate_cache()
    r = await client.get("/api/v1/pcap/captures", headers=_hdr(token))
    assert r.status_code == 404


async def test_permission_denied_without_perm(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _user_with_perm(db_session, None)
    await db_session.commit()
    r = await client.get("/api/v1/pcap/captures", headers=_hdr(token))
    assert r.status_code == 403


async def test_permission_granted_with_perm(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _user_with_perm(
        db_session, {"action": "read", "resource_type": "manage_packet_capture"}
    )
    await db_session.commit()
    r = await client.get("/api/v1/pcap/captures", headers=_hdr(token))
    assert r.status_code == 200
