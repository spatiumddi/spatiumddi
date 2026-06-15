"""#59 Phase 2 — appliance-host vantage dispatch + create branch.

Covers the DB-poll dispatch service (claim / progress / finalize) and the
create-capture appliance branch (approved-gate, queued, NOT Celery-
dispatched — the supervisor polls). The cert-authed supervisor endpoints
are thin wrappers over these + the already-tested cert auth; we assert the
poll endpoint rejects a non-cert caller.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    APPLIANCE_STATE_PENDING_APPROVAL,
    Appliance,
)
from app.models.auth import User
from app.models.pcap import PacketCapture
from app.services import feature_modules
from app.services.appliance import pcap_capture


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


async def _appliance(db: AsyncSession, *, state: str = APPLIANCE_STATE_APPROVED) -> Appliance:
    a = Appliance(
        hostname=f"appl-{uuid.uuid4().hex[:6]}",
        public_key_der=b"\x00" * 44,
        public_key_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        state=state,
    )
    db.add(a)
    await db.flush()
    return a


async def _queued_appliance_capture(db: AsyncSession, appliance_id: uuid.UUID) -> PacketCapture:
    cap = PacketCapture(
        vantage_kind="appliance",
        appliance_id=appliance_id,
        vantage_label="appl",
        interface="eth0",
        bpf_filter="port 53",
        snaplen=256,
        max_duration_s=30,
        status="queued",
    )
    db.add(cap)
    await db.flush()
    return cap


# ── dispatch service ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_next_claims_and_marks_running(db_session: AsyncSession) -> None:
    appl = await _appliance(db_session)
    cap = await _queued_appliance_capture(db_session, appl.id)
    await db_session.commit()

    cmd = await pcap_capture.claim_next(db_session, appl.id)
    assert cmd is not None
    assert cmd["capture_id"] == str(cap.id)
    assert cmd["interface"] == "eth0"
    assert cmd["bpf_filter"] == "port 53"
    await db_session.refresh(cap)
    assert cap.status == "running"
    assert cap.started_at is not None

    # Second claim finds nothing (already running, not queued).
    assert await pcap_capture.claim_next(db_session, appl.id) is None


@pytest.mark.asyncio
async def test_claim_next_scoped_to_appliance(db_session: AsyncSession) -> None:
    a1 = await _appliance(db_session)
    a2 = await _appliance(db_session)
    await _queued_appliance_capture(db_session, a1.id)
    await db_session.commit()
    # a2 has no queued captures.
    assert await pcap_capture.claim_next(db_session, a2.id) is None


@pytest.mark.asyncio
async def test_record_progress_and_cancel(db_session: AsyncSession) -> None:
    appl = await _appliance(db_session)
    cap = await _queued_appliance_capture(db_session, appl.id)
    cap.status = "running"
    await db_session.commit()

    cancel = await pcap_capture.record_progress(
        db_session, cap.id, packets=10, bytes_captured=2048, elapsed_s=3.0
    )
    assert cancel is False
    await db_session.refresh(cap)
    assert cap.bytes_captured == 2048
    assert cap.packets_captured == 10

    # Operator cancels → progress returns True.
    cap.status = "cancelled"
    await db_session.commit()
    assert (
        await pcap_capture.record_progress(
            db_session, cap.id, packets=None, bytes_captured=4096, elapsed_s=5.0
        )
        is True
    )


@pytest.mark.asyncio
async def test_finalize_completed_and_cancel_wins(db_session: AsyncSession) -> None:
    appl = await _appliance(db_session)
    cap = await _queued_appliance_capture(db_session, appl.id)
    cap.status = "running"
    await db_session.commit()

    status = await pcap_capture.finalize_capture(
        db_session,
        cap.id,
        pcap_path="/var/lib/spatiumddi/pcaps/x.pcap",
        pcap_size_bytes=4096,
        pcap_sha256="abc",
        packet_count=42,
        metadata={"stop_reason": "completed"},
        error=None,
    )
    assert status == "completed"
    await db_session.refresh(cap)
    assert cap.status == "completed"
    assert cap.packets_captured == 42
    assert cap.pcap_path.endswith("x.pcap")

    # A cancelled row stays cancelled even if a late upload finalizes.
    cap2 = await _queued_appliance_capture(db_session, appl.id)
    cap2.status = "cancelled"
    await db_session.commit()
    status2 = await pcap_capture.finalize_capture(
        db_session,
        cap2.id,
        pcap_path="/x.pcap",
        pcap_size_bytes=1,
        pcap_sha256="z",
        packet_count=1,
        metadata={},
        error=None,
    )
    assert status2 == "cancelled"
    await db_session.refresh(cap2)
    assert cap2.status == "cancelled"


@pytest.mark.asyncio
async def test_finalize_failed(db_session: AsyncSession) -> None:
    appl = await _appliance(db_session)
    cap = await _queued_appliance_capture(db_session, appl.id)
    cap.status = "running"
    await db_session.commit()
    status = await pcap_capture.finalize_capture(
        db_session,
        cap.id,
        pcap_path=None,
        pcap_size_bytes=None,
        pcap_sha256=None,
        packet_count=None,
        metadata={"stop_reason": "error"},
        error="host capture failed",
    )
    assert status == "failed"
    await db_session.refresh(cap)
    assert cap.status == "failed"
    assert "failed" in (cap.error_message or "")


# ── create-capture appliance branch ──────────────────────────────────


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_create_appliance_requires_appliance_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    r = await client.post(
        "/api/v1/pcap/captures",
        json={"vantage_kind": "appliance", "max_duration_s": 30},
        headers=_hdr(token),
    )
    assert r.status_code == 422
    assert "appliance_id is required" in r.json()["detail"]


async def test_create_appliance_rejects_unapproved(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    appl = await _appliance(db_session, state=APPLIANCE_STATE_PENDING_APPROVAL)
    await db_session.commit()
    r = await client.post(
        "/api/v1/pcap/captures",
        json={"vantage_kind": "appliance", "appliance_id": str(appl.id), "max_duration_s": 30},
        headers=_hdr(token),
    )
    assert r.status_code == 422
    assert "not approved" in r.json()["detail"]


async def test_create_appliance_queues_without_dispatch(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    appl = await _appliance(db_session)
    await db_session.commit()
    with patch("app.tasks.pcap.run_capture_task.delay") as delay:
        r = await client.post(
            "/api/v1/pcap/captures",
            json={
                "vantage_kind": "appliance",
                "appliance_id": str(appl.id),
                "interface": "eth0",
                "bpf_filter": "port 67 or port 68",
                "max_duration_s": 30,
            },
            headers=_hdr(token),
        )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["vantage_kind"] == "appliance"
    assert body["status"] == "queued"
    # Appliance vantage is NOT Celery-dispatched — the supervisor polls.
    delay.assert_not_called()


async def test_supervisor_pcap_poll_requires_cert(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # No cert headers → 403 (the supervisor channel is cert-only).
    r = await client.post("/api/v1/appliance/supervisor/pcap/poll")
    assert r.status_code == 403
