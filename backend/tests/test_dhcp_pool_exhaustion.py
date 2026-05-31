"""DHCP per-pool occupancy + dhcp_pool_exhaustion alert (issue #339).

Occupancy is computed live from active DHCPLease rows inside a dynamic
pool's range; the alert fires when occupancy reaches threshold_percent OR
free addresses drop below min_free_addresses, and resolves when it recovers.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.alerts import AlertEvent, AlertRule
from app.models.auth import User
from app.models.dhcp import DHCPLease, DHCPPool, DHCPScope, DHCPServer, DHCPServerGroup
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.services.alerts import RULE_TYPE_DHCP_POOL_EXHAUSTION, evaluate_all
from app.services.dhcp.pool_occupancy import (
    compute_pool_occupancy,
    compute_pool_occupancy_batch,
    pool_total_addresses,
)

CIDR = "10.60.0.0/24"
POOL_START = "10.60.0.10"
POOL_END = "10.60.0.19"  # 10 addresses inclusive


async def _scope_and_server(db: AsyncSession) -> tuple[DHCPScope, DHCPServer]:
    space = IPSpace(name=f"s-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=CIDR, name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=CIDR, name="s")
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add_all([subnet, grp])
    await db.flush()
    scope = DHCPScope(group_id=grp.id, subnet_id=subnet.id, name="scope-a")
    server = DHCPServer(name=f"kea-{uuid.uuid4().hex[:6]}", host="10.0.0.1", driver="kea")
    db.add_all([scope, server])
    await db.flush()
    return scope, server


async def _pool(db: AsyncSession, scope: DHCPScope, pool_type: str = "dynamic") -> DHCPPool:
    pool = DHCPPool(
        scope_id=scope.id,
        name="pool-a",
        start_ip=POOL_START,
        end_ip=POOL_END,
        pool_type=pool_type,
    )
    db.add(pool)
    await db.flush()
    return pool


async def _lease(
    db: AsyncSession,
    server: DHCPServer,
    scope: DHCPScope,
    last_octet: int,
    *,
    state: str = "active",
) -> None:
    db.add(
        DHCPLease(
            server_id=server.id,
            scope_id=scope.id,
            ip_address=f"10.60.0.{last_octet}",
            mac_address=f"02:00:00:00:00:{last_octet:02x}",
            state=state,
        )
    )


# ── Occupancy computation ─────────────────────────────────────────────────


def test_pool_total_addresses() -> None:
    assert pool_total_addresses("10.60.0.10", "10.60.0.19") == 10
    assert pool_total_addresses("10.60.0.10", "10.60.0.10") == 1
    # Inverted / malformed / mixed-family → 0 (can't divide by it).
    assert pool_total_addresses("10.60.0.19", "10.60.0.10") == 0
    assert pool_total_addresses("nonsense", "10.60.0.10") == 0
    assert pool_total_addresses("10.60.0.10", "2001:db8::1") == 0


async def test_compute_pool_occupancy_counts_active_in_range(db_session: AsyncSession) -> None:
    scope, server = await _scope_and_server(db_session)
    pool = await _pool(db_session, scope)
    # 3 active leases inside the pool range…
    for octet in (10, 11, 12):
        await _lease(db_session, server, scope, octet)
    # …one outside the range (ignored)…
    await _lease(db_session, server, scope, 50)
    # …and one released inside the range (ignored).
    await _lease(db_session, server, scope, 13, state="released")
    await db_session.flush()

    occ = await compute_pool_occupancy(db_session, pool)
    assert occ.total == 10
    assert occ.assigned == 3
    assert occ.free == 7
    assert round(occ.percent, 1) == 30.0


async def test_compute_pool_occupancy_dedupes_ha_peers(db_session: AsyncSession) -> None:
    # An HA pair reports the same lease IP from two servers — one assignment.
    scope, server_a = await _scope_and_server(db_session)
    server_b = DHCPServer(name=f"kea-{uuid.uuid4().hex[:6]}", host="10.0.0.2", driver="kea")
    db_session.add(server_b)
    await db_session.flush()
    pool = await _pool(db_session, scope)
    await _lease(db_session, server_a, scope, 10)
    await _lease(db_session, server_b, scope, 10)
    await db_session.flush()

    occ = await compute_pool_occupancy(db_session, pool)
    assert occ.assigned == 1


async def test_compute_pool_occupancy_invalid_range_is_zero(db_session: AsyncSession) -> None:
    # An inverted range yields total 0; occupancy must short-circuit to (0, 0)
    # and never count leases — a bad pool can't make occupancy blow up.
    scope, server = await _scope_and_server(db_session)
    bad = DHCPPool(
        scope_id=scope.id,
        name="inverted",
        start_ip="10.60.0.19",
        end_ip="10.60.0.10",
        pool_type="dynamic",
    )
    db_session.add(bad)
    await db_session.flush()
    for octet in (10, 11, 12):
        await _lease(db_session, server, scope, octet)
    await db_session.flush()

    occ = await compute_pool_occupancy(db_session, bad)
    assert occ.total == 0
    assert occ.assigned == 0


async def test_compute_pool_occupancy_batch_matches_single(db_session: AsyncSession) -> None:
    scope, server = await _scope_and_server(db_session)
    pool = await _pool(db_session, scope)
    # A second dynamic pool in the same scope, different sub-range.
    pool2 = DHCPPool(
        scope_id=scope.id,
        name="pool-b",
        start_ip="10.60.0.20",
        end_ip="10.60.0.29",
        pool_type="dynamic",
    )
    db_session.add(pool2)
    await db_session.flush()
    for octet in (10, 11, 12, 20):  # 3 in pool, 1 in pool2
        await _lease(db_session, server, scope, octet)
    await db_session.flush()

    batch = await compute_pool_occupancy_batch(db_session, [pool, pool2])
    assert batch[pool.id].assigned == 3
    assert batch[pool.id].total == 10
    assert batch[pool2.id].assigned == 1
    # Batch result matches the per-pool helper exactly.
    single = await compute_pool_occupancy(db_session, pool)
    assert (batch[pool.id].assigned, batch[pool.id].total) == (single.assigned, single.total)


# ── Alert evaluator ───────────────────────────────────────────────────────


async def _rule(db: AsyncSession, **kw: object) -> AlertRule:
    rule = AlertRule(
        name=f"pool-{uuid.uuid4().hex[:6]}",
        rule_type=RULE_TYPE_DHCP_POOL_EXHAUSTION,
        enabled=True,
        severity="warning",
        **kw,
    )
    db.add(rule)
    await db.flush()
    return rule


async def _open_events(db: AsyncSession, rule: AlertRule) -> list[AlertEvent]:
    rows = (
        (
            await db.execute(
                select(AlertEvent).where(
                    AlertEvent.rule_id == rule.id, AlertEvent.resolved_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def test_alert_fires_on_percent_then_resolves(db_session: AsyncSession) -> None:
    scope, server = await _scope_and_server(db_session)
    await _pool(db_session, scope)
    rule = await _rule(db_session, threshold_percent=80)
    # 9 of 10 leased = 90% ≥ 80 → fire.
    for octet in range(10, 19):
        await _lease(db_session, server, scope, octet)
    await db_session.commit()

    await evaluate_all(db_session)
    events = await _open_events(db_session, rule)
    assert len(events) == 1
    assert events[0].subject_type == "dhcp_pool"
    assert "90.0%" in events[0].message

    # Drop to 4 leases = 40% < 80 → resolve.
    leases = (await db_session.execute(select(DHCPLease))).scalars().all()
    for lease in leases[:5]:
        await db_session.delete(lease)
    await db_session.commit()

    await evaluate_all(db_session)
    assert await _open_events(db_session, rule) == []


async def test_alert_fires_on_min_free(db_session: AsyncSession) -> None:
    scope, server = await _scope_and_server(db_session)
    await _pool(db_session, scope)
    # Percent unset; fire purely on the free-address floor.
    rule = await _rule(db_session, threshold_percent=None, min_free_addresses=5)
    # 6 of 10 leased → 4 free < 5 → fire.
    for octet in range(10, 16):
        await _lease(db_session, server, scope, octet)
    await db_session.commit()

    await evaluate_all(db_session)
    events = await _open_events(db_session, rule)
    assert len(events) == 1
    assert "4 free" in events[0].message


async def test_alert_ignores_non_dynamic_pools(db_session: AsyncSession) -> None:
    scope, server = await _scope_and_server(db_session)
    await _pool(db_session, scope, pool_type="reserved")
    rule = await _rule(db_session, threshold_percent=10)
    for octet in range(10, 19):
        await _lease(db_session, server, scope, octet)
    await db_session.commit()

    await evaluate_all(db_session)
    # Reserved pools never hand out leases → never alert.
    assert await _open_events(db_session, rule) == []


# ── MCP tool ───────────────────────────────────────────────────────────────


async def test_find_dhcp_pool_occupancy_tool(db_session: AsyncSession) -> None:
    from app.services.ai.tools.dhcp import FindDHCPPoolOccupancyArgs, find_dhcp_pool_occupancy

    scope, server = await _scope_and_server(db_session)
    pool = await _pool(db_session, scope)
    for octet in (10, 11, 12, 13, 14):  # 5/10 = 50%
        await _lease(db_session, server, scope, octet)
    await db_session.commit()

    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="t",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db_session.add(user)
    await db_session.flush()

    rows = await find_dhcp_pool_occupancy(db_session, user, FindDHCPPoolOccupancyArgs())
    mine = [r for r in rows if r["pool_id"] == str(pool.id)]
    assert len(mine) == 1
    assert mine[0]["assigned"] == 5
    assert mine[0]["total"] == 10
    assert mine[0]["free"] == 5
    assert mine[0]["occupancy_percent"] == 50.0

    # min_percent filter excludes it.
    filtered = await find_dhcp_pool_occupancy(
        db_session, user, FindDHCPPoolOccupancyArgs(min_percent=60)
    )
    assert all(r["pool_id"] != str(pool.id) for r in filtered)
