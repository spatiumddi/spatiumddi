"""#964 — the zone-scoped ``DNSRecordOp`` sweep lives in ONE helper, and its
name-scoping under split-horizon is a documented decision, not an accident.

``DNSRecordOp`` carries ``(server_id, zone_name)`` and no view discriminator,
while ``DNSZone`` is unique on ``(group_id, view_id, name)`` — so two zones
of the same name in sibling views share a queue. ``queued_zone_ops_where``
records why that is accepted (the op payload carries no view either, and the
ConfigBundle re-render converges both views). These tests pin:

  * the helper's behaviour — queued states swept, history kept, other
    servers untouched, the count agrees with the sweep;
  * the documented over-sweep, so a future change to it is deliberate;
  * that the three call sites use the helper and carry no copy of the
    predicate — the shape #951 copied verbatim from zone_move.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dns import DNSRecordOp, DNSServer, DNSServerGroup, DNSView, DNSZone
from app.services.dns.record_ops import (
    QUEUED_OP_STATES,
    count_queued_zone_ops,
    sweep_zone_ops,
)

_APP = Path(__file__).resolve().parents[1] / "app"
_CALL_SITES = (
    _APP / "services" / "dns" / "zone_move.py",
    _APP / "services" / "ai" / "operations_risky.py",
)


async def _group_with_servers(db: AsyncSession, n: int) -> tuple[DNSServerGroup, list[DNSServer]]:
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    servers = [
        DNSServer(
            group_id=grp.id,
            driver="bind9",
            host=f"10.0.0.{i + 1}",
            name=f"srv{i}-{uuid.uuid4().hex[:4]}",
            is_primary=(i == 0),
            is_enabled=True,
        )
        for i in range(n)
    ]
    db.add_all(servers)
    await db.flush()
    return grp, servers


async def _zone(
    db: AsyncSession, grp: DNSServerGroup, name: str, view_id: uuid.UUID | None = None
) -> DNSZone:
    zone = DNSZone(
        group_id=grp.id,
        view_id=view_id,
        name=name,
        zone_type="primary",
        kind="forward",
        primary_ns=f"ns1.{name}",
        admin_email=f"admin.{name}",
    )
    db.add(zone)
    await db.flush()
    return zone


def _op(server: DNSServer, zone_name: str, state: str) -> DNSRecordOp:
    return DNSRecordOp(
        server_id=server.id,
        zone_name=zone_name,
        op="create",
        record={"name": "www", "type": "A", "value": "10.0.0.1"},
        state=state,
    )


async def _states(db: AsyncSession, server: DNSServer) -> list[str]:
    rows = (
        (
            await db.execute(
                select(DNSRecordOp)
                .where(DNSRecordOp.server_id == server.id)
                .order_by(DNSRecordOp.state)
            )
        )
        .scalars()
        .all()
    )
    return sorted(o.state for o in rows)


@pytest.mark.asyncio
async def test_sweep_discards_queued_keeps_history_and_other_servers(
    db_session: AsyncSession,
) -> None:
    grp, (a, b) = await _group_with_servers(db_session, 2)
    zone = await _zone(db_session, grp, f"z{uuid.uuid4().hex[:6]}.example.")
    other = await _zone(db_session, grp, f"o{uuid.uuid4().hex[:6]}.example.")
    for state in ("pending", "in_flight", "applied", "failed"):
        db_session.add(_op(a, zone.name, state))
    db_session.add(_op(a, other.name, "pending"))  # a different zone, same server
    db_session.add(_op(b, zone.name, "pending"))  # same zone, server NOT in scope
    await db_session.flush()

    assert set(QUEUED_OP_STATES) == {"pending", "in_flight"}
    assert await count_queued_zone_ops(db_session, zone, [a.id]) == 2
    assert await sweep_zone_ops(db_session, zone, [a.id]) == 2

    assert await _states(db_session, a) == ["applied", "failed", "pending"]  # other zone's
    assert await _states(db_session, b) == ["pending"]
    # An empty server list is a no-op, never an unscoped sweep.
    assert await count_queued_zone_ops(db_session, zone, []) == 0
    assert await sweep_zone_ops(db_session, zone, []) == 0
    assert await _states(db_session, b) == ["pending"]


@pytest.mark.asyncio
async def test_sweep_is_name_scoped_across_sibling_views_by_decision(
    db_session: AsyncSession,
) -> None:
    """The documented over-sweep (#964). Under split-horizon the same name
    exists once per view; the op queue cannot tell them apart, and neither can
    the agent applying the op, so a sweep for one view's zone also discards
    its sibling's queued ops. The bundle re-render converges both. If this
    test starts failing because the sweep became view-exact, update the
    decision in ``queued_zone_ops_where`` in the same change."""
    grp, (a,) = await _group_with_servers(db_session, 1)
    internal = DNSView(group_id=grp.id, name="internal", order=0)
    external = DNSView(group_id=grp.id, name="external", order=1)
    db_session.add_all([internal, external])
    await db_session.flush()
    name = f"split{uuid.uuid4().hex[:6]}.example."
    z_int = await _zone(db_session, grp, name, view_id=internal.id)
    z_ext = await _zone(db_session, grp, name, view_id=external.id)
    assert z_int.id != z_ext.id and z_int.name == z_ext.name
    db_session.add(_op(a, name, "pending"))
    db_session.add(_op(a, name, "in_flight"))
    await db_session.flush()

    assert await count_queued_zone_ops(db_session, z_int, [a.id]) == 2
    assert await sweep_zone_ops(db_session, z_int, [a.id]) == 2
    assert await count_queued_zone_ops(db_session, z_ext, [a.id]) == 0


def test_call_sites_use_the_helper_and_carry_no_copy_of_the_predicate() -> None:
    """Three sites carried the same three-clause predicate verbatim (#951
    copied it from zone_move). It lives in ``record_ops`` now; a re-inlined
    copy would silently fork the #964 decision."""
    pattern = re.compile(r"DNSRecordOp\.zone_name\s*==|sa_delete\(DNSRecordOp\)")
    for path in _CALL_SITES:
        src = path.read_text(encoding="utf-8")
        assert "sweep_zone_ops" in src, f"{path.name} no longer routes through sweep_zone_ops"
        hits = [m.group(0) for m in pattern.finditer(src)]
        assert hits == [], f"{path.name} carries its own copy of the sweep predicate: {hits}"
