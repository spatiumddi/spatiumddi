"""REST + MCP tests for Scheduled Wake-on-LAN — Phase 1 (issue #586).

Covers the HTTP surface at ``/api/v1/wake-scheduler`` and the Operator
Copilot ``preview_wol_schedule_targets`` tool:

* the happy path — create a schedule → preview its targets → run it now →
  list the run history — with the actual magic-packet send patched so no UDP
  ever leaves the process.
* RBAC — an unauthorized principal (no ``use_network_tools`` grant, not a
  superadmin) is refused 403 on every gated endpoint.
* MCP — ``preview_wol_schedule_targets`` returns the resolved fleet plus the
  built-in gate verdict at the next fire.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import Group, Role, User
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.wol_schedule import WolRun, WolRunTarget, WolSchedule
from app.services.ai.tools import REGISTRY  # noqa: F401 — triggers registration
from app.services.ai.tools.wol_scheduler import PreviewWolScheduleTargetsArgs

_BASE = "/api/v1/wake-scheduler"


# ── Builders ──────────────────────────────────────────────────────────


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


async def _plain_user(db: AsyncSession) -> tuple[User, str]:
    """A user with no roles / permissions — every gate must refuse them."""
    u = User(
        username=f"user-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="User",
        hashed_password=hash_password("x"),
        is_superadmin=False,
    )
    db.add(u)
    await db.flush()
    return u, create_access_token(str(u.id))


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _wakeable_host(db: AsyncSession) -> Subnet:
    space = IPSpace(name=f"space-{uuid.uuid4().hex[:6]}")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.20.0.0/24", name="blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id, block_id=block.id, network="10.20.0.0/24", name="net", kind="unicast"
    )
    db.add(subnet)
    await db.flush()
    db.add(
        IPAddress(
            subnet_id=subnet.id,
            address="10.20.0.5",
            mac_address="aa:bb:cc:dd:ee:05",
            tags={"wake": "nightly"},
            hostname="lab-pc-1",
        )
    )
    await db.flush()
    return subnet


def _fake_ok_send() -> AsyncMock:
    """Awaitable stand-in for ``wake_from_server`` with call assertions."""
    return AsyncMock(return_value=SimpleNamespace(sent=True, ran_from="server"))


def _create_body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "nightly-lab",
        "target_selector": {"mode": "address_tags", "tags": ["wake:nightly"]},
        "schedule_cron": "0 7 * * *",
        "timezone": "UTC",
        "repeat_count": 1,
        "repeat_interval_ms": 0,
        "stagger_ms": 0,
        "port": 9,
    }
    body.update(kw)
    return body


# ══════════════════════════════════════════════════════════════════════
# Happy path — create → preview → run-now → list runs
# ══════════════════════════════════════════════════════════════════════


async def test_create_persists_and_audits(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _superadmin(db_session)
    await _wakeable_host(db_session)

    r = await client.post(f"{_BASE}/schedules", json=_create_body(), headers=_hdr(token))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "nightly-lab"
    assert body["schedule_cron"] == "0 7 * * *"
    # A cron-driven schedule has next_run_at computed on create.
    assert body["next_run_at"] is not None
    assert body["target_selector"]["mode"] == "address_tags"

    rows = (
        (await db_session.execute(select(AuditLog).where(AuditLog.resource_type == "wol_schedule")))
        .scalars()
        .all()
    )
    assert any(a.action == "create" for a in rows)


async def test_manual_only_schedule_has_null_next_run(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    r = await client.post(
        f"{_BASE}/schedules", json=_create_body(schedule_cron=None), headers=_hdr(token)
    )
    assert r.status_code == 201, r.text
    assert r.json()["next_run_at"] is None  # manual-only → never swept


async def test_full_happy_path_create_preview_run_list(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    await _wakeable_host(db_session)

    # 1. Create.
    r = await client.post(f"{_BASE}/schedules", json=_create_body(), headers=_hdr(token))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    # 2. Preview the saved schedule's targets.
    r = await client.post(f"{_BASE}/schedules/{sid}/preview-targets", headers=_hdr(token))
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["wake_count"] == 1
    assert preview["matched_count"] == 1
    assert preview["mac_less_count"] == 0
    assert preview["next_run_at"] is not None
    assert preview["gate_verdict"] is None  # no blackout / term → would fire
    assert preview["sample"][0]["mac"] == "aa:bb:cc:dd:ee:05"
    assert preview["sample"][0]["broadcast"] == "10.20.0.255"

    # 3. Run it now (packet send patched).
    with patch("app.services.wol.wake_from_server", _fake_ok_send()):
        r = await client.post(f"{_BASE}/schedules/{sid}/run-now", headers=_hdr(token))
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["trigger"] == "manual"
    assert run["status"] == "ok"
    assert run["sent_count"] == 1
    assert run["target_count"] == 1
    run_id = run["id"]

    # 4. List runs — the fire shows up.
    r = await client.get(f"{_BASE}/runs?schedule_id={sid}", headers=_hdr(token))
    assert r.status_code == 200, r.text
    runs = r.json()
    assert len(runs) == 1
    assert runs[0]["id"] == run_id

    # 5. Run detail carries the per-host outcome.
    r = await client.get(f"{_BASE}/runs/{run_id}", headers=_hdr(token))
    assert r.status_code == 200, r.text
    detail = r.json()
    assert len(detail["targets"]) == 1
    t = detail["targets"][0]
    assert t["sent"] is True
    assert t["mac"] == "aa:bb:cc:dd:ee:05"


async def test_preview_unsaved_selector(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _superadmin(db_session)
    await _wakeable_host(db_session)
    r = await client.post(
        f"{_BASE}/preview-targets",
        json={"target_selector": {"mode": "address_tags", "tags": ["wake:nightly"]}},
        headers=_hdr(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wake_count"] == 1
    # Unsaved selector has no persisted cron/gate.
    assert body["next_run_at"] is None
    assert body["gate_verdict"] is None


async def test_create_rejects_bad_cron(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _superadmin(db_session)
    r = await client.post(
        f"{_BASE}/schedules", json=_create_body(schedule_cron="not a cron"), headers=_hdr(token)
    )
    assert r.status_code == 422, r.text


# ══════════════════════════════════════════════════════════════════════
# RBAC — an unauthorized principal is refused
# ══════════════════════════════════════════════════════════════════════


async def test_rbac_403_on_list(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _plain_user(db_session)
    r = await client.get(f"{_BASE}/schedules", headers=_hdr(token))
    assert r.status_code == 403, r.text


async def test_rbac_403_on_create(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _plain_user(db_session)
    r = await client.post(f"{_BASE}/schedules", json=_create_body(), headers=_hdr(token))
    assert r.status_code == 403, r.text


async def test_rbac_403_on_run_now(client: AsyncClient, db_session: AsyncSession) -> None:
    # Seed a schedule as superadmin, then attempt to fire it as an
    # unauthorized principal — the write gate must refuse before any dispatch.
    _, admin_token = await _superadmin(db_session)
    await _wakeable_host(db_session)
    r = await client.post(f"{_BASE}/schedules", json=_create_body(), headers=_hdr(admin_token))
    sid = r.json()["id"]

    _, token = await _plain_user(db_session)
    with patch("app.services.wol.wake_from_server", _fake_ok_send()) as send:
        r = await client.post(f"{_BASE}/schedules/{sid}/run-now", headers=_hdr(token))
    assert r.status_code == 403, r.text
    send.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# MCP — preview_wol_schedule_targets returns fleet + gate verdict
# ══════════════════════════════════════════════════════════════════════


async def _mcp_preview(db: AsyncSession, user: User, schedule_id: uuid.UUID) -> dict[str, Any]:
    tool = REGISTRY.get("preview_wol_schedule_targets")
    assert tool is not None
    assert tool.module == "tools.wake_scheduler"
    return await tool.executor(db, user, PreviewWolScheduleTargetsArgs(schedule_id=schedule_id))


async def test_mcp_preview_returns_fleet_and_clear_verdict(db_session: AsyncSession) -> None:
    owner, _ = await _superadmin(db_session)
    await _wakeable_host(db_session)
    # A next fire on a plain, non-blackout day → the gate would let it through.
    schedule = WolSchedule(
        name="nightly-lab",
        target_selector={"mode": "address_tags", "tags": ["wake:nightly"]},
        schedule_cron="0 7 * * *",
        timezone="UTC",
        vantage={"kind": "server", "id": None},
        created_by_user_id=owner.id,
        next_run_at=datetime(2026, 6, 1, 7, 0, tzinfo=UTC),
    )
    db_session.add(schedule)
    await db_session.flush()

    out = await _mcp_preview(db_session, owner, schedule.id)

    assert out["name"] == "nightly-lab"
    assert out["wake_count"] == 1
    assert out["matched_count"] == 1
    assert out["mac_less_count"] == 0
    assert out["gate_verdict"] is None
    assert out["would_fire"] is True
    assert out["sample"][0]["mac"] == "aa:bb:cc:dd:ee:05"
    assert out["sample"][0]["hostname"] == "lab-pc-1"


async def test_mcp_preview_reports_holiday_gate_verdict(db_session: AsyncSession) -> None:
    owner, _ = await _superadmin(db_session)
    await _wakeable_host(db_session)
    fire = datetime(2026, 12, 25, 7, 0, tzinfo=UTC)
    schedule = WolSchedule(
        name="holiday-lab",
        target_selector={"mode": "address_tags", "tags": ["wake:nightly"]},
        schedule_cron="0 7 * * *",
        timezone="UTC",
        blackout_dates=["2026-12-25"],  # the next fire lands on a blackout
        vantage={"kind": "server", "id": None},
        created_by_user_id=owner.id,
        next_run_at=fire,
    )
    db_session.add(schedule)
    await db_session.flush()

    out = await _mcp_preview(db_session, owner, schedule.id)

    # Fleet still resolves (the hosts exist), but the gate would suppress it.
    assert out["wake_count"] == 1
    assert out["gate_verdict"] == "holiday"
    assert out["would_fire"] is False


async def test_mcp_preview_unknown_schedule_returns_error(db_session: AsyncSession) -> None:
    owner, _ = await _superadmin(db_session)
    out = await _mcp_preview(db_session, owner, uuid.uuid4())
    assert "error" in out


# ══════════════════════════════════════════════════════════════════════
# Schema — empty-tags tag-mode selector is rejected (422)
# ══════════════════════════════════════════════════════════════════════


async def test_create_rejects_empty_tags_for_tag_mode(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    r = await client.post(
        f"{_BASE}/schedules",
        json=_create_body(target_selector={"mode": "address_tags", "tags": []}),
        headers=_hdr(token),
    )
    # An empty-tags tag-mode selector would resolve to every host in scope —
    # the schema validator must 422 it before it can be stored.
    assert r.status_code == 422, r.text


async def test_preview_selector_rejects_empty_tags(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    r = await client.post(
        f"{_BASE}/preview-targets",
        json={"target_selector": {"mode": "subnet_tags", "tags": [""]}},
        headers=_hdr(token),
    )
    assert r.status_code == 422, r.text


# ══════════════════════════════════════════════════════════════════════
# Re-enable recomputes next_run_at into the future (no stale immediate fire)
# ══════════════════════════════════════════════════════════════════════


async def test_reenable_recomputes_next_run_to_future(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _superadmin(db_session)
    await _wakeable_host(db_session)

    # 1. Create a cron schedule → next_run_at computed into the future.
    r = await client.post(f"{_BASE}/schedules", json=_create_body(), headers=_hdr(token))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    # 2. Disable it (update does NOT recompute while enabled=False).
    r = await client.patch(f"{_BASE}/schedules/{sid}", json={"enabled": False}, headers=_hdr(token))
    assert r.status_code == 200, r.text

    # 3. Simulate the schedule sitting disabled across a cron slot — its
    #    next_run_at is now a stale, past timestamp.
    row = await db_session.get(WolSchedule, uuid.UUID(sid))
    assert row is not None
    row.next_run_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()

    # 4. Re-enable → update_schedule MUST recompute next_run_at forward so the
    #    very next sweep doesn't fire an immediate unintended fleet wake.
    r = await client.patch(f"{_BASE}/schedules/{sid}", json={"enabled": True}, headers=_hdr(token))
    assert r.status_code == 200, r.text
    nxt = r.json()["next_run_at"]
    assert nxt is not None
    parsed = datetime.fromisoformat(nxt)
    assert parsed > datetime.now(UTC)  # future, not the stale past slot


# ══════════════════════════════════════════════════════════════════════
# RBAC scope — preview / run-detail / MCP must not leak host detail the
# CALLER can't read, even for a broader-owned schedule
# ══════════════════════════════════════════════════════════════════════


async def _named_subnet(db: AsyncSession, network: str) -> Subnet:
    space = IPSpace(name=f"space-{uuid.uuid4().hex[:6]}")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=network, name="blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id, block_id=block.id, network=network, name="net", kind="unicast"
    )
    db.add(subnet)
    await db.flush()
    return subnet


async def _tagged_host(
    db: AsyncSession, subnet: Subnet, address: str, mac: str, hostname: str
) -> IPAddress:
    ip = IPAddress(
        subnet_id=subnet.id,
        address=address,
        mac_address=mac,
        tags={"wake": "nightly"},
        hostname=hostname,
    )
    db.add(ip)
    await db.flush()
    return ip


async def _scoped_caller(db: AsyncSession, *, readable_subnet_id: uuid.UUID) -> tuple[User, str]:
    """A non-superadmin who may ``read use_network_tools`` (so the read gates
    admit them) but may read ONLY one subnet — never the schedule owner's."""
    user = User(
        username=f"dept-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="Dept Operator",
        hashed_password=hash_password("x"),
        is_superadmin=False,
    )
    role = Role(
        name=f"role-{uuid.uuid4().hex[:8]}",
        permissions=[
            {"action": "read", "resource_type": "use_network_tools"},
            {"action": "read", "resource_type": "subnet", "resource_id": str(readable_subnet_id)},
        ],
    )
    group = Group(name=f"grp-{uuid.uuid4().hex[:8]}")
    group.roles = [role]
    user.groups = [group]
    db.add_all([role, group, user])
    await db.flush()
    return user, create_access_token(str(user.id))


async def _load_scoped(db: AsyncSession, user_id: uuid.UUID) -> User:
    return (
        await db.execute(
            select(User)
            .options(selectinload(User.groups).selectinload(Group.roles))
            .where(User.id == user_id)
        )
    ).scalar_one()


async def _admin_schedule_over_two_subnets(
    db: AsyncSession,
) -> tuple[WolSchedule, Subnet, Subnet, IPAddress, IPAddress]:
    """A superadmin-owned schedule whose tag matches one host in subnet A and
    one in subnet B."""
    admin, _ = await _superadmin(db)
    subnet_a = await _named_subnet(db, "10.20.0.0/24")
    subnet_b = await _named_subnet(db, "10.30.0.0/24")
    host_a = await _tagged_host(db, subnet_a, "10.20.0.5", "aa:bb:cc:dd:ee:0a", "a-host")
    host_b = await _tagged_host(db, subnet_b, "10.30.0.5", "aa:bb:cc:dd:ee:0b", "b-host")
    schedule = WolSchedule(
        name="fleet-wake",
        target_selector={"mode": "address_tags", "tags": ["wake:nightly"]},
        schedule_cron="0 7 * * *",
        timezone="UTC",
        vantage={"kind": "server", "id": None},
        created_by_user_id=admin.id,
        next_run_at=datetime(2026, 6, 1, 7, 0, tzinfo=UTC),
    )
    db.add(schedule)
    await db.flush()
    return schedule, subnet_a, subnet_b, host_a, host_b


async def test_preview_targets_clamps_host_detail_to_caller_scope(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    schedule, _sub_a, sub_b, _host_a, _host_b = await _admin_schedule_over_two_subnets(db_session)
    caller, token = await _scoped_caller(db_session, readable_subnet_id=sub_b.id)
    await db_session.commit()

    r = await client.post(f"{_BASE}/schedules/{schedule.id}/preview-targets", headers=_hdr(token))
    assert r.status_code == 200, r.text
    body = r.json()

    # Owner-scoped counts stay intact (fire-time parity — both hosts wake).
    assert body["wake_count"] == 2
    # But the caller (subnet-B only) sees host DETAIL for subnet B alone —
    # never the subnet-A host's address / mac / hostname.
    assert len(body["sample"]) == 1
    hostnames = {s["hostname"] for s in body["sample"]}
    assert hostnames == {"b-host"}
    macs = {s["mac"] for s in body["sample"]}
    assert "aa:bb:cc:dd:ee:0a" not in macs  # subnet-A MAC never leaked
    addresses = {s["address"] for s in body["sample"]}
    assert "10.20.0.5" not in addresses


async def test_get_run_clamps_targets_to_caller_scope(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    schedule, sub_a, sub_b, host_a, host_b = await _admin_schedule_over_two_subnets(db_session)
    # A completed run with one per-host target row in each subnet.
    run = WolRun(
        schedule_id=schedule.id,
        trigger="schedule",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        status="ok",
        target_count=2,
        sent_count=2,
    )
    db_session.add(run)
    await db_session.flush()
    for sub, host, addr, mac in (
        (sub_a, host_a, "10.20.0.5", "aa:bb:cc:dd:ee:0a"),
        (sub_b, host_b, "10.30.0.5", "aa:bb:cc:dd:ee:0b"),
    ):
        db_session.add(
            WolRunTarget(
                run_id=run.id,
                ip_address_id=host.id,
                address=addr,
                mac=mac,
                subnet_id=sub.id,
                broadcast="255.255.255.255",
                sent=True,
            )
        )
    await db_session.flush()
    run_id = run.id
    _caller, token = await _scoped_caller(db_session, readable_subnet_id=sub_b.id)
    await db_session.commit()

    r = await client.get(f"{_BASE}/runs/{run_id}", headers=_hdr(token))
    assert r.status_code == 200, r.text
    detail = r.json()
    # Only the subnet-B target row is returned; the subnet-A host detail is gone.
    assert len(detail["targets"]) == 1
    assert detail["targets"][0]["subnet_id"] == str(sub_b.id)
    macs = {t["mac"] for t in detail["targets"]}
    assert "aa:bb:cc:dd:ee:0a" not in macs


async def test_mcp_preview_clamps_sample_to_caller_scope(db_session: AsyncSession) -> None:
    schedule, _sub_a, sub_b, _host_a, _host_b = await _admin_schedule_over_two_subnets(db_session)
    caller, _token = await _scoped_caller(db_session, readable_subnet_id=sub_b.id)
    # Reload with groups/roles eager-loaded for the resolver's sync RBAC walk.
    caller = await _load_scoped(db_session, caller.id)

    out = await _mcp_preview(db_session, caller, schedule.id)

    # Owner-scoped counts (both hosts) but caller-clamped sample (subnet B only).
    assert out["wake_count"] == 2
    assert out["sample_scoped_to_caller"] is True
    assert len(out["sample"]) == 1
    assert out["sample"][0]["hostname"] == "b-host"
    assert out["sample"][0]["mac"] == "aa:bb:cc:dd:ee:0b"
    macs = {s["mac"] for s in out["sample"]}
    assert "aa:bb:cc:dd:ee:0a" not in macs
