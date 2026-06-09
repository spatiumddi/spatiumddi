"""Per-subnet utilization history endpoint (#44)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.ipam import IPBlock, IPSpace, Subnet, SubnetUtilizationHistory


async def _admin_token(db: AsyncSession) -> str:
    user = User(
        username=f"uh-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Util Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


async def _subnet(db: AsyncSession) -> Subnet:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network="10.0.0.0/24",
        name="s",
        total_ips=254,
        allocated_ips=100,
    )
    db.add(subnet)
    await db.flush()
    return subnet


async def test_utilization_history_window_and_shape(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _admin_token(db_session)
    subnet = await _subnet(db_session)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            SubnetUtilizationHistory(
                subnet_id=subnet.id,
                sampled_at=now - timedelta(days=2),
                allocated_ips=50,
                total_ips=254,
            ),
            SubnetUtilizationHistory(
                subnet_id=subnet.id,
                sampled_at=now - timedelta(days=1),
                allocated_ips=100,
                total_ips=254,
            ),
            # Outside the 90-day window — must be excluded.
            SubnetUtilizationHistory(
                subnet_id=subnet.id,
                sampled_at=now - timedelta(days=200),
                allocated_ips=10,
                total_ips=254,
            ),
        ]
    )
    await db_session.commit()

    r = await client.get(
        f"/api/v1/ipam/subnets/{subnet.id}/utilization-history?days=90",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    pts = r.json()
    assert len(pts) == 2  # the 200-day-old sample is filtered out
    # Ordered ascending by sampled_at.
    assert pts[0]["allocated_ips"] == 50
    assert pts[1]["allocated_ips"] == 100
    # utilization_percent is computed from allocated/total.
    assert pts[1]["utilization_percent"] == round(100 / 254 * 100, 2)


async def test_utilization_history_empty(client: AsyncClient, db_session: AsyncSession) -> None:
    token = await _admin_token(db_session)
    subnet = await _subnet(db_session)
    await db_session.commit()
    r = await client.get(
        f"/api/v1/ipam/subnets/{subnet.id}/utilization-history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == []
