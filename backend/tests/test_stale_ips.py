"""Stale-IP report + bulk-deprecate + hygiene alert (issue #45)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.alerts import AlertRule
from app.models.auth import User
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services.alerts import _matching_stale_ip_count_subjects
from app.services.ipam.stale_ips import (
    build_stale_ip_report,
    count_stale_per_subnet,
    select_stale_ip_ids,
)

NOW = datetime.now(UTC)
STALE = NOW - timedelta(days=100)  # older than the 90-day default window
FRESH = NOW - timedelta(days=10)  # well inside the window


async def _make_user(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_subnet(db: AsyncSession, cidr: str = "192.0.2.0/24") -> Subnet:
    space = IPSpace(name=f"stale-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=cidr, name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=cidr, name="s")
    db.add(subnet)
    await db.flush()
    return subnet


async def _seed_mix(db: AsyncSession, subnet: Subnet) -> dict[str, IPAddress]:
    """Seed the canonical mix the report has to discriminate."""
    rows = {
        # allocated + last seen long ago → STALE (the target)
        "stale": IPAddress(
            subnet_id=subnet.id, address="192.0.2.10", status="allocated", last_seen_at=STALE
        ),
        # allocated + seen recently → not stale
        "fresh": IPAddress(
            subnet_id=subnet.id, address="192.0.2.11", status="allocated", last_seen_at=FRESH
        ),
        # allocated + never seen → only counts with include_never_seen
        "never": IPAddress(subnet_id=subnet.id, address="192.0.2.12", status="allocated"),
        # reserved + stale → deliberately held, never in the report
        "reserved": IPAddress(
            subnet_id=subnet.id, address="192.0.2.13", status="reserved", last_seen_at=STALE
        ),
        # DHCP-lease mirror + stale → owned by DHCP, always excluded
        "lease": IPAddress(
            subnet_id=subnet.id,
            address="192.0.2.14",
            status="allocated",
            last_seen_at=STALE,
            auto_from_lease=True,
        ),
    }
    db.add_all(list(rows.values()))
    await db.flush()
    return rows


# ── Service: report ──────────────────────────────────────────────────


async def test_report_only_stale_allocated(db_session: AsyncSession) -> None:
    subnet = await _make_subnet(db_session)
    await _seed_mix(db_session, subnet)
    report = await build_stale_ip_report(db_session, stale_days=90)
    addrs = {e["address"] for e in report["entries"]}
    assert addrs == {"192.0.2.10"}  # only the stale allocated row
    assert report["total"] == 1
    assert report["entries"][0]["days_stale"] >= 99


async def test_report_include_never_seen(db_session: AsyncSession) -> None:
    subnet = await _make_subnet(db_session)
    await _seed_mix(db_session, subnet)
    report = await build_stale_ip_report(db_session, stale_days=90, include_never_seen=True)
    addrs = {e["address"] for e in report["entries"]}
    assert addrs == {"192.0.2.10", "192.0.2.12"}  # stale + never-seen
    # Never-seen sorts first (NULLS FIRST on ascending last_seen_at).
    assert report["entries"][0]["address"] == "192.0.2.12"
    assert report["entries"][0]["days_stale"] is None


async def test_report_scoped_by_space(db_session: AsyncSession) -> None:
    a = await _make_subnet(db_session, "192.0.2.0/24")
    b = await _make_subnet(db_session, "198.51.100.0/24")
    db_session.add_all(
        [
            IPAddress(subnet_id=a.id, address="192.0.2.10", status="allocated", last_seen_at=STALE),
            IPAddress(
                subnet_id=b.id, address="198.51.100.10", status="allocated", last_seen_at=STALE
            ),
        ]
    )
    await db_session.flush()
    report = await build_stale_ip_report(db_session, stale_days=90, space_id=a.space_id)
    assert {e["address"] for e in report["entries"]} == {"192.0.2.10"}


async def test_select_ids_and_count(db_session: AsyncSession) -> None:
    subnet = await _make_subnet(db_session)
    rows = await _seed_mix(db_session, subnet)
    ids = await select_stale_ip_ids(db_session, stale_days=90)
    assert ids == [rows["stale"].id]
    counts = await count_stale_per_subnet(db_session, stale_days=90)
    assert counts == {subnet.id: 1}
    # include_never_seen folds in the never-seen row
    counts2 = await count_stale_per_subnet(db_session, stale_days=90, include_never_seen=True)
    assert counts2 == {subnet.id: 2}


# ── Endpoints ──────────────────────────────────────────────────────────


async def test_endpoint_report_and_deprecate_selected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session)
    h = {"Authorization": f"Bearer {token}"}
    subnet = await _make_subnet(db_session)
    rows = await _seed_mix(db_session, subnet)
    await db_session.commit()

    r = await client.get("/api/v1/ipam/reports/stale-ips", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["entries"][0]["address"] == "192.0.2.10"

    # Deprecate the selected stale row + a reserved row (should be skipped).
    r = await client.post(
        "/api/v1/ipam/reports/stale-ips/deprecate",
        headers=h,
        json={"ip_ids": [str(rows["stale"].id), str(rows["reserved"].id)]},
    )
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["deprecated_count"] == 1
    assert str(rows["reserved"].id) in res["skipped"]

    refreshed = (
        await db_session.execute(select(IPAddress).where(IPAddress.id == rows["stale"].id))
    ).scalar_one()
    assert refreshed.status == "deprecated"
    assert refreshed.user_modified_at is not None
    # No longer surfaces in the report.
    r = await client.get("/api/v1/ipam/reports/stale-ips", headers=h)
    assert r.json()["total"] == 0


async def test_endpoint_deprecate_all_matching(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session)
    h = {"Authorization": f"Bearer {token}"}
    subnet = await _make_subnet(db_session)
    db_session.add_all(
        [
            IPAddress(
                subnet_id=subnet.id, address="192.0.2.10", status="allocated", last_seen_at=STALE
            ),
            IPAddress(
                subnet_id=subnet.id, address="192.0.2.11", status="allocated", last_seen_at=STALE
            ),
            IPAddress(
                subnet_id=subnet.id, address="192.0.2.12", status="allocated", last_seen_at=FRESH
            ),
        ]
    )
    await db_session.commit()

    r = await client.post(
        "/api/v1/ipam/reports/stale-ips/deprecate",
        headers=h,
        json={"all_matching": True, "stale_days": 90, "space_id": str(subnet.space_id)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["deprecated_count"] == 2
    assert r.json()["capped"] is False


async def test_endpoint_deprecate_requires_target(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/v1/ipam/reports/stale-ips/deprecate", headers=h, json={})
    assert r.status_code == 422


async def test_endpoint_deprecate_explicit_id_rechecks_staleness(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # A row that went live after the report loaded (fresh last_seen) must be
    # skipped even when its id is posted explicitly — the server re-checks
    # against the request's window rather than trusting the client selection.
    _, token = await _make_user(db_session)
    h = {"Authorization": f"Bearer {token}"}
    subnet = await _make_subnet(db_session)
    stale = IPAddress(
        subnet_id=subnet.id, address="192.0.2.10", status="allocated", last_seen_at=STALE
    )
    fresh = IPAddress(
        subnet_id=subnet.id, address="192.0.2.11", status="allocated", last_seen_at=FRESH
    )
    db_session.add_all([stale, fresh])
    await db_session.commit()

    r = await client.post(
        "/api/v1/ipam/reports/stale-ips/deprecate",
        headers=h,
        json={"ip_ids": [str(stale.id), str(fresh.id)], "stale_days": 90},
    )
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["deprecated_count"] == 1
    assert str(fresh.id) in res["skipped"]


# ── Alert rule ─────────────────────────────────────────────────────────


async def test_stale_ip_count_alert_fires_over_threshold(db_session: AsyncSession) -> None:
    subnet = await _make_subnet(db_session)
    db_session.add_all(
        [
            IPAddress(
                subnet_id=subnet.id, address="192.0.2.10", status="allocated", last_seen_at=STALE
            ),
            IPAddress(
                subnet_id=subnet.id, address="192.0.2.11", status="allocated", last_seen_at=STALE
            ),
        ]
    )
    await db_session.flush()

    # threshold 2 → fires (2 stale ≥ 2)
    rule = AlertRule(
        name="hygiene", rule_type="stale_ip_count", threshold_percent=2, threshold_days=90
    )
    matches = await _matching_stale_ip_count_subjects(db_session, rule)
    assert len(matches) == 1
    assert str(subnet.id) == matches[0][0]

    # threshold 3 → quiet (only 2 stale)
    rule.threshold_percent = 3
    assert await _matching_stale_ip_count_subjects(db_session, rule) == []
