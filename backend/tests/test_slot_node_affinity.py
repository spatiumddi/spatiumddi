"""The singleton slot endpoints are node-local; on a cluster they must say so.

``/api/v1/appliance/slot-upgrade`` reads and writes the responding pod's OWN
host mounts. The api Deployment scales to the control-plane node count under
hard anti-affinity and its Service sets no session affinity, so on a cluster
consecutive calls are served by different nodes even when the caller addressed
one node's IP.

Found live on a 3-node cluster: eight consecutive GETs against ONE member's own
IP alternated between that node's real upgrade (``done``, a timestamp, 6.6 KB of
log_tail) and other nodes' idle state (``ready``, null, empty). The reads were
merely unreadable; the WRITES were worse — an apply lands on whichever node the
load balancer picked, so a rolling upgrade can upgrade one node repeatedly and
never touch the others, silently, with a 202 every time.

Two guards here: the response names the node it describes, and the writes refuse
to guess.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.appliance import slot as slot_api
from app.config import settings
from app.core.security import create_access_token, hash_password
from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    CLUSTER_ROLE_MEMBER,
    CLUSTER_ROLE_PRIMARY,
    Appliance,
)
from app.models.auth import User


async def _superadmin(db: AsyncSession) -> dict[str, str]:
    user = User(
        username=f"admin-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test Admin",
        hashed_password=hash_password("test-pw-slot"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _approve(db: AsyncSession, hostname: str,
                   cluster_role: str | None = None) -> Appliance:
    """An approved appliance. `cluster_role` None models a data-plane
    appliance or an unpromoted single node — approved, but NOT part of the
    control plane, and therefore not a source of node ambiguity."""
    row = Appliance(
        id=uuid.uuid4(),
        hostname=hostname,
        state=APPLIANCE_STATE_APPROVED,
        cluster_role=cluster_role,
        public_key_der=b"fake-key",
        public_key_fingerprint=uuid.uuid4().hex * 2,
        cert_serial="0000",
        deployment_kind="appliance",
    )
    db.add(row)
    await db.flush()
    return row


# ── the response names the node it is about ──────────────────────────────


@pytest.mark.asyncio
async def test_status_reports_the_responding_node(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _superadmin(db_session)
    monkeypatch.setattr(settings, "node_name", "ddipg-member-1")
    r = await client.get("/api/v1/appliance/slot-upgrade", headers=headers)
    assert r.status_code == 200
    # Without this an operator cannot tell whose slot state they just read,
    # and every response looks equally authoritative.
    assert r.json()["node"] == "ddipg-member-1"


@pytest.mark.asyncio
async def test_status_node_is_empty_off_cluster(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # docker-compose / non-appliance: no downward API, and only one place the
    # answer could have come from. Empty is honest; a fabricated name is not.
    headers = await _superadmin(db_session)
    monkeypatch.setattr(settings, "node_name", "")
    r = await client.get("/api/v1/appliance/slot-upgrade", headers=headers)
    assert r.status_code == 200
    assert r.json()["node"] == ""


# ── the writes refuse to act on a node nobody chose ──────────────────────


@pytest.mark.asyncio
async def test_apply_refuses_on_a_cluster(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _superadmin(db_session)
    monkeypatch.setattr(settings, "node_name", "ddipg-member-1")
    for host, role in (("ddipg-seed", CLUSTER_ROLE_PRIMARY),
                       ("ddipg-member-1", CLUSTER_ROLE_MEMBER),
                       ("ddipg-member-2", CLUSTER_ROLE_MEMBER)):
        await _approve(db_session, host, role)
    await db_session.commit()

    fired: list[str] = []
    monkeypatch.setattr(slot_api, "schedule_apply", lambda *a, **k: fired.append("apply"))

    r = await client.post(
        "/api/v1/appliance/slot-upgrade/apply",
        headers=headers,
        json={"image_url": "http://example/slot.raw.xz"},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    # The refusal has to be actionable: which node would have been hit, how
    # many there are, and where to go instead.
    assert "clustered" in detail
    assert "ddipg-member-1" in detail
    assert "3 control-plane nodes" in detail
    assert "appliances/{appliance_id}/upgrade" in detail
    # and nothing was written to any node's disk
    assert fired == []


@pytest.mark.asyncio
async def test_rollback_refuses_on_a_cluster(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _superadmin(db_session)
    for host, role in (("ddipg-seed", CLUSTER_ROLE_PRIMARY),
                       ("ddipg-member-1", CLUSTER_ROLE_MEMBER)):
        await _approve(db_session, host, role)
    await db_session.commit()
    fired: list[str] = []
    monkeypatch.setattr(
        slot_api, "schedule_rollback", lambda *a, **k: fired.append("rollback")
    )

    r = await client.post(
        "/api/v1/appliance/slot-upgrade/rollback", headers=headers, json={}
    )
    assert r.status_code == 409
    assert fired == []


@pytest.mark.asyncio
async def test_single_node_appliance_still_applies(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One approved node is the deployment this endpoint was written for:
    there is exactly one machine it could mean, and it is this one."""
    headers = await _superadmin(db_session)
    await _approve(db_session, "ddipg-seed", CLUSTER_ROLE_PRIMARY)
    await db_session.commit()
    fired: list[str] = []
    monkeypatch.setattr(slot_api, "schedule_apply", lambda *a, **k: fired.append("apply"))
    monkeypatch.setattr(slot_api, "is_apply_in_flight", lambda: False)

    r = await client.post(
        "/api/v1/appliance/slot-upgrade/apply",
        headers=headers,
        json={"image_url": "http://example/slot.raw.xz"},
    )
    assert r.status_code == 202
    assert fired == ["apply"]


@pytest.mark.asyncio
async def test_no_approved_appliances_still_applies(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh install that has not registered itself yet must not be locked
    out of its own upgrade path — the guard is about ambiguity, and zero rows
    are not ambiguous."""
    headers = await _superadmin(db_session)
    await db_session.commit()
    fired: list[str] = []
    monkeypatch.setattr(slot_api, "schedule_apply", lambda *a, **k: fired.append("apply"))
    monkeypatch.setattr(slot_api, "is_apply_in_flight", lambda: False)

    r = await client.post(
        "/api/v1/appliance/slot-upgrade/apply",
        headers=headers,
        json={"image_url": "http://example/slot.raw.xz"},
    )
    assert r.status_code == 202
    assert fired == ["apply"]


@pytest.mark.asyncio
async def test_data_plane_appliances_do_not_count_as_a_cluster(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approved != control-plane. A single unclustered node with paired
    data-plane appliances has exactly one machine this endpoint could mean,
    so it must keep working — counting approved rows would have 409'd it."""
    headers = await _superadmin(db_session)
    await _approve(db_session, "ddipg-seed", None)
    await _approve(db_session, "ddipg-dns-1", None)
    await _approve(db_session, "ddipg-dhcp-1", None)
    await db_session.commit()
    fired: list[str] = []
    monkeypatch.setattr(slot_api, "schedule_apply", lambda *a, **k: fired.append("apply"))
    monkeypatch.setattr(slot_api, "is_apply_in_flight", lambda: False)

    r = await client.post(
        "/api/v1/appliance/slot-upgrade/apply",
        headers=headers,
        json={"image_url": "http://example/slot.raw.xz"},
    )
    assert r.status_code == 202
    assert fired == ["apply"]
