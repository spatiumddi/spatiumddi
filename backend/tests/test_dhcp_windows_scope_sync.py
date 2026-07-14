"""#620 — the Windows scope reconciler (``pull_leases._upsert_scope``).

Phase 1 of ``pull_leases_from_server`` — the ``get_scopes`` topology pass that
reconciles a Windows DHCP server's scopes, pools and reservations — had zero
test coverage, which is how it shipped Core-DELETEing every reservation under a
scope and re-inserting it from the wire on every beat tick. Reservations got a
fresh id each poll, so the ``ip_address`` mirror that back-links to one by id
was left pointing at a row Postgres had dropped, within minutes of an operator
creating it: the address was neither allocated nor free, and deleting the
reservation in the UI never freed it.

The fix is a diff-merge (ids stay stable), so these tests are mostly about
*what does not happen*: no id churn, no DNS writes on an unchanged poll, no
absence-delete on a wire we can't trust.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPPool, DHCPScope, DHCPServer, DHCPServerGroup, DHCPStaticAssignment
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services.dhcp.static_ipam import upsert_ipam_for_static

SUBNET = "10.20.0.0/24"
MAC_A = "aa:bb:cc:00:00:01"
MAC_B = "aa:bb:cc:00:00:02"


def wire_scope(
    *,
    statics: list[dict[str, Any]] | None = None,
    pools: list[dict[str, Any]] | None = None,
    statics_ok: bool = True,
    pools_ok: bool = True,
) -> dict[str, Any]:
    """One neutral scope dict, shaped exactly as ``windows._parse_scopes`` emits."""
    return {
        "scope_id": "10.20.0.0",
        "subnet_cidr": SUBNET,
        "name": "office",
        "description": "",
        "lease_time": 86400,
        "is_active": True,
        "options": {},
        "pools": (
            [{"start_ip": "10.20.0.100", "end_ip": "10.20.0.200", "pool_type": "dynamic"}]
            if pools is None
            else pools
        ),
        "statics": statics or [],
        "pools_ok": pools_ok,
        "statics_ok": statics_ok,
    }


def wire_static(
    mac: str = MAC_A,
    ip: str = "10.20.0.10",
    hostname: str = "printer",
    description: str = "",
) -> dict[str, Any]:
    return {
        "ip_address": ip,
        "mac_address": mac,
        "hostname": hostname,
        "client_id": mac.replace(":", "-"),
        "description": description,
    }


class _StubDriver:
    """A Windows-shaped driver: implements ``get_scopes`` (which is what gates
    Phase 1) plus the ``get_leases`` every agentless driver must have."""

    def __init__(self, scopes: list[dict[str, Any]]) -> None:
        self.scopes = scopes

    async def get_scopes(self, _server: DHCPServer) -> list[dict[str, Any]]:
        return self.scopes

    async def get_leases(self, _server: DHCPServer) -> list[dict[str, Any]]:
        return []


@dataclass
class _DNSSpy:
    """Records every ``_sync_dns_record`` call the reconcile makes.

    The whole point of merging rather than detach/re-attaching is that a steady
    poll writes no DNS at all, so "how many times was DNS touched" is the
    assertion that actually pins the behaviour down.
    """

    calls: list[str] = field(default_factory=list)


def _patch(monkeypatch: pytest.MonkeyPatch, scopes: list[dict[str, Any]]) -> _DNSSpy:
    from app.services.dhcp import pull_leases as pl

    monkeypatch.setattr(pl, "get_driver", lambda _drv: _StubDriver(scopes))
    monkeypatch.setattr(pl, "is_agentless", lambda _drv: True)

    spy = _DNSSpy()

    # Both static_ipam helpers import _sync_dns_record from the router lazily,
    # so patching it on the router module catches every call site.
    import app.api.v1.ipam.router as ipam_router

    async def _spy(_db: Any, row: Any, _subnet: Any, action: str = "create", **_kw: Any) -> None:
        spy.calls.append(f"{action}:{row.address}")

    monkeypatch.setattr(ipam_router, "_sync_dns_record", _spy)
    return spy


async def _fixture(db: AsyncSession) -> tuple[DHCPServer, DHCPScope, Subnet]:
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db.add(grp)
    await db.flush()
    srv = DHCPServer(
        name=f"s-{uuid.uuid4().hex[:6]}",
        driver="windows_dhcp",
        host="127.0.0.1",
        port=67,
        server_group_id=grp.id,
    )
    db.add(srv)
    await db.flush()
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.20.0.0/16", name="blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=SUBNET, name="office")
    db.add(subnet)
    await db.flush()
    scope = DHCPScope(group_id=grp.id, subnet_id=subnet.id, is_active=True)
    db.add(scope)
    await db.flush()
    return srv, scope, subnet


async def _make_ui_reservation(
    db: AsyncSession,
    scope: DHCPScope,
    *,
    mac: str = MAC_A,
    ip: str = "10.20.0.10",
    client_id: str | None = "wire",
) -> DHCPStaticAssignment:
    """A reservation created the way the UI creates one — mirror and all.

    ``client_id`` defaults to the form the wire reports, so a fixture is already
    in the steady state the reconciler should leave alone. Pass ``None`` for a
    genuinely UI-created reservation, which has no client_id until a poll learns
    one from the server.
    """
    st = DHCPStaticAssignment(
        scope_id=scope.id,
        ip_address=ip,
        mac_address=mac,
        hostname="printer",
        description="",
        client_id=mac.replace(":", "-") if client_id == "wire" else client_id,
    )
    db.add(st)
    await db.flush()
    await upsert_ipam_for_static(db, scope, st)
    await db.flush()
    return st


async def _mirror(db: AsyncSession, subnet: Subnet, ip: str) -> IPAddress | None:
    return (
        await db.execute(
            select(IPAddress).where(IPAddress.subnet_id == subnet.id, IPAddress.address == ip)
        )
    ).scalar_one_or_none()


# ── the bug ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_keeps_a_ui_reservations_mirror_linked(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported bug: an operator creates a reservation in the UI, the next
    Windows scope sync reads it back off the wire, and the reservation must come
    through with the same id — otherwise its IPAM mirror is left pointing at a
    dead row (not allocated, not free, not reclaimable)."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    st = await _make_ui_reservation(db_session, scope)
    original_id = st.id
    await db_session.commit()

    _patch(monkeypatch, [wire_scope(statics=[wire_static()])])
    await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    statics = list(
        (
            await db_session.execute(
                select(DHCPStaticAssignment).where(DHCPStaticAssignment.scope_id == scope.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(statics) == 1
    assert statics[0].id == original_id, "reservation was re-created — its mirror is now stranded"

    mirror = await _mirror(db_session, subnet, "10.20.0.10")
    assert mirror is not None
    assert mirror.status == "static_dhcp"
    assert mirror.static_assignment_id == str(original_id)


@pytest.mark.asyncio
async def test_repeat_sync_writes_no_dns(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll that finds nothing changed must not touch DNS. This runs on the
    beat, so a detach/re-attach repair would tear down and recreate the forward
    A record for every reservation, forever."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    await _make_ui_reservation(db_session, scope)
    await db_session.commit()

    spy = _patch(monkeypatch, [wire_scope(statics=[wire_static()])])
    for _ in range(3):
        result = await pl.pull_leases_from_server(db_session, srv, apply=True)
        await db_session.commit()

    assert spy.calls == [], f"steady-state poll churned DNS: {spy.calls}"
    assert result.statics_synced == 0
    assert result.statics_removed == 0


@pytest.mark.asyncio
async def test_learning_client_id_from_the_wire_is_not_a_dns_event(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A UI-created reservation has no client_id; Windows reports one, so the
    first poll after it is created legitimately writes to the row. That is not a
    reason to re-sync DNS — the record's name and address haven't moved."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, _subnet = await _fixture(db_session)
    st = await _make_ui_reservation(db_session, scope, client_id=None)
    await db_session.commit()

    spy = _patch(monkeypatch, [wire_scope(statics=[wire_static()])])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    await db_session.refresh(st)
    assert st.client_id == MAC_A.replace(":", "-")
    assert result.statics_synced == 1
    assert spy.calls == []


@pytest.mark.asyncio
async def test_sync_repairs_a_stranded_mirror(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installs carrying mirrors the old replace-all stranded (back-link naming a
    reservation that no longer exists) re-link themselves on the next poll — no
    operator action, no migration."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    st = await _make_ui_reservation(db_session, scope)
    mirror = await _mirror(db_session, subnet, "10.20.0.10")
    assert mirror is not None
    # Strand it exactly as the pre-fix reconciler did: mirror survives, its
    # back-link names a reservation id that is gone.
    mirror.static_assignment_id = str(uuid.uuid4())
    st.ip_address_id = None
    await db_session.commit()

    _patch(monkeypatch, [wire_scope(statics=[wire_static()])])
    await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    await db_session.refresh(mirror)
    assert mirror.static_assignment_id == str(st.id)
    assert mirror.status == "static_dhcp"


# ── mirroring Windows-side reservations ───────────────────────────────


@pytest.mark.asyncio
async def test_windows_side_reservation_gets_an_ipam_mirror(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reservation that only ever existed on the Windows box is still an
    allocation. IPAM has to show it, or it will hand the address out twice."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    await db_session.commit()

    _patch(monkeypatch, [wire_scope(statics=[wire_static(ip="10.20.0.11")])])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.statics_synced == 1
    mirror = await _mirror(db_session, subnet, "10.20.0.11")
    assert mirror is not None
    assert mirror.status == "static_dhcp"
    assert str(mirror.mac_address) == MAC_A


@pytest.mark.asyncio
async def test_relocated_reservation_keeps_its_id_and_moves_its_mirror(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reservations are matched on MAC, so moving one to a new IP updates it in
    place — the row id (and therefore the mirror's back-link) survives."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    st = await _make_ui_reservation(db_session, scope)
    original_id = st.id
    await db_session.commit()

    _patch(monkeypatch, [wire_scope(statics=[wire_static(ip="10.20.0.77")])])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    await db_session.refresh(st)
    assert st.id == original_id
    assert str(st.ip_address) == "10.20.0.77"
    assert result.statics_synced == 1

    moved = await _mirror(db_session, subnet, "10.20.0.77")
    assert moved is not None
    assert moved.static_assignment_id == str(original_id)
    # The address it left is FREE — the row is gone, not merely downgraded to
    # `allocated`. An allocated row with no owner is reclaimed by nothing (the
    # orphan sweep only looks at `static_dhcp`, the lease mirror skips rows it
    # doesn't own), so every renumber would leak one address. Asserting "not
    # static_dhcp" here is what let that through the first time.
    assert await _mirror(db_session, subnet, "10.20.0.10") is None


@pytest.mark.asyncio
async def test_cosmetic_mac_reformat_is_not_a_change(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows hands MACs back as ``AA-BB-CC-…``; Postgres stores ``aa:bb:cc:…``.
    Comparing them raw would delete and re-create the reservation on every poll."""
    from app.services.dhcp import pull_leases as pl

    wire_mac = MAC_A.upper().replace(":", "-")

    srv, scope, subnet = await _fixture(db_session)
    st = await _make_ui_reservation(db_session, scope, client_id=wire_mac)
    original_id = st.id
    await db_session.commit()

    _patch(monkeypatch, [wire_scope(statics=[wire_static(mac=wire_mac)])])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.statics_synced == 0
    assert result.statics_removed == 0
    statics = list(
        (
            await db_session.execute(
                select(DHCPStaticAssignment).where(DHCPStaticAssignment.scope_id == scope.id)
            )
        )
        .scalars()
        .all()
    )
    assert [s.id for s in statics] == [original_id]


# ── absence-delete + its floor guards ─────────────────────────────────


@pytest.mark.asyncio
async def test_reservation_removed_on_the_server_is_removed_here(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wire that still reports *other* reservations is trustworthy, so one that
    stopped being reported really was deleted on the server: drop it, delete its
    mirror (a freed row still renders and still counts toward utilization) and
    tear its DNS down."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    gone = await _make_ui_reservation(db_session, scope, mac=MAC_A, ip="10.20.0.10")
    await _make_ui_reservation(db_session, scope, mac=MAC_B, ip="10.20.0.11")
    await db_session.commit()

    spy = _patch(monkeypatch, [wire_scope(statics=[wire_static(mac=MAC_B, ip="10.20.0.11")])])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.statics_removed == 1
    assert await db_session.get(DHCPStaticAssignment, gone.id) is None
    assert await _mirror(db_session, subnet, "10.20.0.10") is None
    assert spy.calls == ["delete:10.20.0.10"]
    # The surviving reservation was untouched.
    assert await _mirror(db_session, subnet, "10.20.0.11") is not None


@pytest.mark.asyncio
async def test_empty_reservation_list_does_not_absence_delete(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty list from a scope we know has reservations is more likely a
    hiccup than a mass deletion (#482's reasoning, applied per scope). Keep the
    rows and tell the operator why — a stale reservation they can delete beats a
    reservation and its A record we tore down on a blip."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    st = await _make_ui_reservation(db_session, scope)
    await db_session.commit()

    spy = _patch(monkeypatch, [wire_scope(statics=[])])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.statics_removed == 0
    assert await db_session.get(DHCPStaticAssignment, st.id) is not None
    assert await _mirror(db_session, subnet, "10.20.0.10") is not None
    assert spy.calls == []
    assert any("0 reservations" in e for e in result.errors)


@pytest.mark.asyncio
async def test_failed_enumeration_does_not_absence_delete(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``statics_ok=False`` is the driver saying its reservation enumeration blew
    up. The list it handed back is meaningless — never delete against it."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    st = await _make_ui_reservation(db_session, scope)
    await db_session.commit()

    _patch(
        monkeypatch,
        [wire_scope(statics=[wire_static(mac=MAC_B, ip="10.20.0.99")], statics_ok=False)],
    )
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.statics_removed == 0
    assert await db_session.get(DHCPStaticAssignment, st.id) is not None
    assert any("enumeration failed" in e for e in result.errors)


@pytest.mark.asyncio
async def test_reservation_created_mid_poll_is_not_absence_deleted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wire is a snapshot. A reservation created after we took it cannot be
    absent from it in any meaningful sense — deleting it would destroy a
    reservation the operator just made (and that write-through already pushed to
    the server)."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    # Reported on the wire, so the absence-delete is armed for this scope.
    await _make_ui_reservation(db_session, scope, mac=MAC_B, ip="10.20.0.11")
    fresh = await _make_ui_reservation(db_session, scope, mac=MAC_A, ip="10.20.0.10")
    # Stamp it into the future: created after the snapshot the poll is about to take.
    fresh.created_at = datetime.now(UTC) + timedelta(minutes=5)
    await db_session.commit()

    _patch(monkeypatch, [wire_scope(statics=[wire_static(mac=MAC_B, ip="10.20.0.11")])])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.statics_removed == 0
    assert await db_session.get(DHCPStaticAssignment, fresh.id) is not None
    assert await _mirror(db_session, subnet, "10.20.0.10") is not None


@pytest.mark.asyncio
async def test_operator_edit_mid_poll_is_not_reverted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same reasoning for an edit: the wire we're holding predates it, so writing
    the wire's value back would revert the operator (and bounce the DNS record)."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    st = await _make_ui_reservation(db_session, scope)
    st.ip_address = "10.20.0.55"
    st.modified_at = datetime.now(UTC) + timedelta(minutes=5)
    await db_session.commit()

    _patch(monkeypatch, [wire_scope(statics=[wire_static(ip="10.20.0.10")])])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    await db_session.refresh(st)
    assert str(st.ip_address) == "10.20.0.55"
    assert result.statics_synced == 0


# ── pools ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pools_merge_in_place_and_absence_delete(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pools carry no mirror, but IPAM reads them — a manual allocation inside a
    dynamic range is refused — so they must not blink out of existence between
    polls either. Merge by range; delete only what the wire dropped."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, _subnet = await _fixture(db_session)
    keep = DHCPPool(
        scope_id=scope.id,
        start_ip="10.20.0.100",
        end_ip="10.20.0.200",
        pool_type="dynamic",
        name="",
    )
    drop = DHCPPool(
        scope_id=scope.id, start_ip="10.20.0.5", end_ip="10.20.0.6", pool_type="excluded", name=""
    )
    db_session.add_all([keep, drop])
    await db_session.flush()
    keep_id = keep.id
    await db_session.commit()

    _patch(monkeypatch, [wire_scope()])  # only the dynamic pool
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    pools = list(
        (await db_session.execute(select(DHCPPool).where(DHCPPool.scope_id == scope.id)))
        .scalars()
        .all()
    )
    assert [p.id for p in pools] == [keep_id], "the unchanged pool was re-created"
    assert result.pools_removed == 1
    assert result.pools_synced == 0


@pytest.mark.asyncio
async def test_empty_pool_list_does_not_absence_delete(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.dhcp import pull_leases as pl

    srv, scope, _subnet = await _fixture(db_session)
    db_session.add(
        DHCPPool(
            scope_id=scope.id,
            start_ip="10.20.0.100",
            end_ip="10.20.0.200",
            pool_type="dynamic",
            name="",
        )
    )
    await db_session.commit()

    _patch(monkeypatch, [wire_scope(pools=[])])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.pools_removed == 0
    pools = list(
        (await db_session.execute(select(DHCPPool).where(DHCPPool.scope_id == scope.id)))
        .scalars()
        .all()
    )
    assert len(pools) == 1
    assert any("0 pools" in e for e in result.errors)


# ── re-addressing (the (scope, address) unique index) ─────────────────


@pytest.mark.asyncio
async def test_reservations_can_swap_addresses(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two reservations trading addresses on the server is a wire whose end state
    is legal but whose every intermediate state violates uq_dhcp_static_scope_ip.
    Applying it row-by-row aborts the poll — and the wire keeps reporting the
    same thing, so it would abort every poll after it, forever."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    a = await _make_ui_reservation(db_session, scope, mac=MAC_A, ip="10.20.0.10")
    b = await _make_ui_reservation(db_session, scope, mac=MAC_B, ip="10.20.0.11")
    a_id, b_id = a.id, b.id
    await db_session.commit()

    _patch(
        monkeypatch,
        [
            wire_scope(
                statics=[
                    wire_static(mac=MAC_A, ip="10.20.0.11"),
                    wire_static(mac=MAC_B, ip="10.20.0.10"),
                ]
            )
        ],
    )
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    await db_session.refresh(a)
    await db_session.refresh(b)
    assert (a.id, str(a.ip_address)) == (a_id, "10.20.0.11")
    assert (b.id, str(b.ip_address)) == (b_id, "10.20.0.10")
    assert result.statics_removed == 0
    assert not result.errors

    # Both mirrors followed their reservation to its new address.
    at_10 = await _mirror(db_session, subnet, "10.20.0.10")
    at_11 = await _mirror(db_session, subnet, "10.20.0.11")
    assert at_10 is not None and at_10.static_assignment_id == str(b_id)
    assert at_11 is not None and at_11.static_assignment_id == str(a_id)


@pytest.mark.asyncio
async def test_reservation_renumber_chain(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hazard without a cycle: A moves onto the address B is vacating.
    Whether it aborts depends on the order the wire happens to list them in, so
    it must not depend on order at all."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    a = await _make_ui_reservation(db_session, scope, mac=MAC_A, ip="10.20.0.10")
    b = await _make_ui_reservation(db_session, scope, mac=MAC_B, ip="10.20.0.11")
    await db_session.commit()

    _patch(
        monkeypatch,
        [
            wire_scope(
                statics=[
                    wire_static(mac=MAC_A, ip="10.20.0.11"),  # onto B's address…
                    wire_static(mac=MAC_B, ip="10.20.0.12"),  # …which B is leaving
                ]
            )
        ],
    )
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    await db_session.refresh(a)
    await db_session.refresh(b)
    assert str(a.ip_address) == "10.20.0.11"
    assert str(b.ip_address) == "10.20.0.12"
    assert not result.errors
    assert await _mirror(db_session, subnet, "10.20.0.12") is not None


@pytest.mark.asyncio
async def test_hostname_change_on_the_wire_follows_into_ipam(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renamed reservation keeps its address, so it is not a 'mover' — but the
    hostname IS mirrored, and the A record is published off the mirror. Updating
    the reservation and leaving the mirror on the old name would put IPAM and DNS
    quietly out of step with the DHCP server."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    await _make_ui_reservation(db_session, scope)
    await db_session.commit()

    spy = _patch(monkeypatch, [wire_scope(statics=[wire_static(hostname="plotter")])])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.statics_synced == 1
    mirror = await _mirror(db_session, subnet, "10.20.0.10")
    assert mirror is not None
    assert mirror.hostname == "plotter"
    assert spy.calls == ["update:10.20.0.10"]


# ── regressions caught in review of the fix itself ────────────────────


@pytest.mark.asyncio
async def test_move_carries_operator_columns_to_the_new_address(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freeing the old address must not throw the operator's work away with it:
    the columns they authored ride onto the reservation and are re-applied at the
    new address."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    await _make_ui_reservation(db_session, scope)
    mirror = await _mirror(db_session, subnet, "10.20.0.10")
    assert mirror is not None
    mirror.description = "rack 4, port 12"
    mirror.tags = {"owner": "lab"}
    await db_session.commit()

    _patch(monkeypatch, [wire_scope(statics=[wire_static(ip="10.20.0.77")])])
    await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    moved = await _mirror(db_session, subnet, "10.20.0.77")
    assert moved is not None
    assert moved.description == "rack 4, port 12"
    assert moved.tags == {"owner": "lab"}


@pytest.mark.asyncio
async def test_hostname_cleared_on_the_wire_clears_in_ipam(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator clearing a reservation's Name on the server has to converge.
    `row.hostname = st.hostname or row.hostname` made an empty name
    unrepresentable — the mirror kept the old one and re-published its A record,
    forever."""
    from app.services.dhcp import pull_leases as pl

    srv, scope, subnet = await _fixture(db_session)
    await _make_ui_reservation(db_session, scope)
    await db_session.commit()

    _patch(monkeypatch, [wire_scope(statics=[wire_static(hostname="")])])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.statics_synced == 1
    mirror = await _mirror(db_session, subnet, "10.20.0.10")
    assert mirror is not None
    assert mirror.hostname in (None, "")


@pytest.mark.asyncio
async def test_pool_created_mid_poll_is_not_absence_deleted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same snapshot reasoning as reservations. A pool an operator added while the
    poll was in flight is absent from a wire that never saw it — deleting it takes
    IPAM's dynamic-range protection away, so an operator can allocate an address
    the DHCP server is actively handing out."""
    from datetime import UTC, datetime, timedelta

    from app.services.dhcp import pull_leases as pl

    srv, scope, _subnet = await _fixture(db_session)
    fresh = DHCPPool(
        scope_id=scope.id,
        start_ip="10.20.0.5",
        end_ip="10.20.0.6",
        pool_type="excluded",
        name="",
    )
    db_session.add(fresh)
    await db_session.flush()
    fresh.created_at = datetime.now(UTC) + timedelta(minutes=5)
    await db_session.commit()

    # Wire reports only the dynamic pool — the new exclusion isn't on it yet.
    _patch(monkeypatch, [wire_scope()])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.pools_removed == 0
    assert await db_session.get(DHCPPool, fresh.id) is not None
