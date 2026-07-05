"""BGP Looking Glass tests — issue #566 Phase 1+2 (+ Phase 6 additions).

Covers the correctness-critical RIB reconcile (``ingest_routes`` — the
zero-wire floor guard, absence-withdraw, idempotency, re-announce), the peer
CRUD surface (Fernet ``md5_password`` never echoed, audit row written), the
feature-module gate, the routes search filters, and the agent register +
routes-push end-to-end path.

Phase 6 additions (bottom of file): vpnv4/vpnv6 address-family acceptance,
the VRF Route-Target cross-check (``matched_vrf_id`` precedence over the
plain IPAM-effective match, the re-resolve sweep, the
``/vrf-rt-matches/{vrf_id}`` endpoint), and the multicast <-> BGP
reachability cross-reference.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.bgp_looking_glass import BGPLGPeer, BGPLGRoute, LookingGlassCollector
from app.models.feature_module import FeatureModule
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.multicast import MulticastDomain, MulticastGroup, MulticastMembership
from app.models.vrf import VRF
from app.services import feature_modules
from app.services.ai.tools.bgp_lg import (
    FindMulticastBgpReachabilityArgs,
    FindVrfLearnedRoutesArgs,
    find_multicast_bgp_reachability,
    find_vrf_learned_routes,
)
from app.services.looking_glass import ipam_link
from app.services.looking_glass.config_bundle import build_lg_config_bundle
from app.services.looking_glass.reachability import multicast_bgp_reachability
from app.services.looking_glass.routes_ingest import ingest_routes
from app.tasks.looking_glass import _reresolve_route_links_async

# ── helpers ────────────────────────────────────────────────────────────


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"lg-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="LG Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_collector(db: AsyncSession, **kw) -> LookingGlassCollector:
    col = LookingGlassCollector(
        name=kw.pop("name", f"col-{uuid.uuid4().hex[:8]}"),
        agent_id=kw.pop("agent_id", uuid.uuid4().hex),
        agent_registered=True,
        status="active",
        **kw,
    )
    db.add(col)
    await db.flush()
    return col


async def _make_peer(db: AsyncSession, collector: LookingGlassCollector, **kw) -> BGPLGPeer:
    peer = BGPLGPeer(
        name=kw.pop("name", f"peer-{uuid.uuid4().hex[:8]}"),
        collector_id=collector.id,
        local_asn=kw.pop("local_asn", 65000),
        peer_asn=kw.pop("peer_asn", 65001),
        peer_address=kw.pop("peer_address", "192.0.2.1"),
        **kw,
    )
    db.add(peer)
    await db.flush()
    return peer


def _route(prefix: str, next_hop: str = "192.0.2.1", **kw) -> dict:
    return {"prefix": prefix, "next_hop": next_hop, **kw}


# ── ingest_routes — the correctness-critical reconcile ──────────────────


@pytest.mark.asyncio
async def test_ingest_inserts_then_idempotent(db_session: AsyncSession) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)

    wire = [_route("10.0.0.0/24", origin_asn=65001), _route("10.0.1.0/24", origin_asn=65001)]
    r1 = await ingest_routes(db_session, peer, wire, snapshot=True)
    assert r1.imported == 2 and r1.refreshed == 0 and r1.withdrawn == 0

    # A second identical snapshot is a pure no-op insert-wise (idempotent #9).
    r2 = await ingest_routes(db_session, peer, wire, snapshot=True)
    assert r2.imported == 0 and r2.refreshed == 2 and r2.withdrawn == 0

    live = (
        await db_session.execute(
            select(func.count()).select_from(BGPLGRoute).where(BGPLGRoute.peer_id == peer.id)
        )
    ).scalar_one()
    assert live == 2


@pytest.mark.asyncio
async def test_ingest_absence_withdraws_missing_prefix(db_session: AsyncSession) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    await ingest_routes(
        db_session,
        peer,
        [_route("10.0.0.0/24", origin_asn=65001), _route("10.0.1.0/24", origin_asn=65001)],
        snapshot=True,
    )

    # Second snapshot drops 10.0.1.0/24 → it must be marked withdrawn, not deleted.
    r = await ingest_routes(
        db_session, peer, [_route("10.0.0.0/24", origin_asn=65001)], snapshot=True
    )
    assert r.withdrawn == 1

    gone = (
        await db_session.execute(
            select(BGPLGRoute).where(
                BGPLGRoute.peer_id == peer.id, BGPLGRoute.prefix == "10.0.1.0/24"
            )
        )
    ).scalar_one()
    assert gone.withdrawn_at is not None  # soft-marked, still present
    assert gone.flap_count == 1


@pytest.mark.asyncio
async def test_zero_wire_floor_guard_skips_mass_withdraw(db_session: AsyncSession) -> None:
    """An empty snapshot from an established peer (prefixes_received>0) must NOT
    withdraw the whole RIB — the #482 zero-wire floor guard."""
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    await ingest_routes(db_session, peer, [_route("10.0.0.0/24", origin_asn=65001)], snapshot=True)

    peer.prefixes_received = 5  # collector reported it has learned prefixes
    await db_session.flush()

    r = await ingest_routes(db_session, peer, [], snapshot=True)
    assert r.withdrawn == 0  # guard fired
    assert any("skipping absence-withdraw" in e for e in r.errors)

    still = (
        await db_session.execute(select(BGPLGRoute).where(BGPLGRoute.peer_id == peer.id))
    ).scalar_one()
    assert still.withdrawn_at is None  # survived the empty poll


@pytest.mark.asyncio
async def test_reannounce_clears_withdrawn(db_session: AsyncSession) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    await ingest_routes(db_session, peer, [_route("10.0.0.0/24", origin_asn=65001)], snapshot=True)
    await ingest_routes(db_session, peer, [], snapshot=True)  # withdraw (prefixes_received==0)

    row = (
        await db_session.execute(select(BGPLGRoute).where(BGPLGRoute.peer_id == peer.id))
    ).scalar_one()
    assert row.withdrawn_at is not None

    # Re-announce → withdrawn_at cleared.
    await ingest_routes(db_session, peer, [_route("10.0.0.0/24", origin_asn=65001)], snapshot=True)
    await db_session.refresh(row)
    assert row.withdrawn_at is None


# ── peer CRUD — Fernet secret handling + audit ──────────────────────────


@pytest.mark.asyncio
async def test_peer_crud_fernet_and_audit(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    col = await _make_collector(db_session)
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {token}"}

    # Create with an MD5 password — response must NOT echo the plaintext.
    resp = await client.post(
        "/api/v1/looking-glass/peers",
        headers=hdr,
        json={
            "name": "Core-1",
            "collector_id": str(col.id),
            "local_asn": 65000,
            "peer_asn": 65001,
            "peer_address": "192.0.2.1",
            "md5_password": "s3cr3t",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    peer_id = body["id"]
    assert body["md5_password_set"] is True
    assert "md5_password" not in body and "s3cr3t" not in resp.text

    # Stored ciphertext, never plaintext.
    row = (
        await db_session.execute(select(BGPLGPeer).where(BGPLGPeer.id == uuid.UUID(peer_id)))
    ).scalar_one()
    assert row.md5_password_encrypted is not None
    assert b"s3cr3t" not in row.md5_password_encrypted

    # A mutation wrote an audit row.
    n_audit = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    assert n_audit >= 1

    # PATCH without md5_password keeps it; explicit "" clears it.
    resp = await client.patch(
        f"/api/v1/looking-glass/peers/{peer_id}", headers=hdr, json={"description": "edge"}
    )
    assert resp.status_code == 200 and resp.json()["md5_password_set"] is True
    resp = await client.patch(
        f"/api/v1/looking-glass/peers/{peer_id}", headers=hdr, json={"md5_password": ""}
    )
    assert resp.status_code == 200 and resp.json()["md5_password_set"] is False

    resp = await client.delete(f"/api/v1/looking-glass/peers/{peer_id}", headers=hdr)
    assert resp.status_code in (200, 204)


# ── feature-module gate ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_module_gate_404_when_disabled(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    db_session.add(FeatureModule(id="network.looking_glass", enabled=False))
    await db_session.flush()
    feature_modules.invalidate_cache()
    try:
        resp = await client.get(
            "/api/v1/looking-glass/peers", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 404
    finally:
        feature_modules.invalidate_cache()


# ── routes search filters ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_routes_search_filters(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    await ingest_routes(
        db_session,
        peer,
        [
            _route("10.0.0.0/24", origin_asn=65001),
            _route("10.0.1.0/24", origin_asn=65002),
        ],
        snapshot=True,
    )
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {token}"}

    resp = await client.get("/api/v1/looking-glass/routes", headers=hdr)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2 and len(body["items"]) == 2

    resp = await client.get("/api/v1/looking-glass/routes?origin_asn=65002", headers=hdr)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1 and items[0]["prefix"] == "10.0.1.0/24"


@pytest.mark.asyncio
async def test_routes_as_path_regexp_filter(client: AsyncClient, db_session: AsyncSession) -> None:
    """#566 Phase 4 — the ``_`` boundary-token AS-path regexp filter."""
    _, token = await _make_admin(db_session)
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    await ingest_routes(
        db_session,
        peer,
        [
            # 65001 appears as origin (last hop).
            _route("10.0.0.0/24", origin_asn=65001, as_path=[65003, 65001]),
            # 65001 appears mid-path only, not as origin.
            _route("10.0.1.0/24", origin_asn=65002, as_path=[65001, 65002]),
            # 65001 doesn't appear at all.
            _route("10.0.2.0/24", origin_asn=65004, as_path=[65004]),
        ],
        snapshot=True,
    )
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {token}"}

    # "_65001_" — anywhere in the path — matches the first two routes.
    resp = await client.get("/api/v1/looking-glass/routes?as_path_regexp=_65001_", headers=hdr)
    assert resp.status_code == 200, resp.text
    prefixes = {r["prefix"] for r in resp.json()["items"]}
    assert prefixes == {"10.0.0.0/24", "10.0.1.0/24"}

    # A pattern with no matches at all.
    resp = await client.get("/api/v1/looking-glass/routes?as_path_regexp=_65999_", headers=hdr)
    assert resp.status_code == 200
    assert resp.json()["items"] == []

    # "_65001$" — origin (last hop) only — matches ONLY the route where 65001
    # is the origin, not the one where it's mid-path. Regression: the rendered
    # path text must be trimmed/collapsed so the trailing "]" space doesn't
    # defeat the "$" anchor.
    resp = await client.get("/api/v1/looking-glass/routes?as_path_regexp=_65001$", headers=hdr)
    assert resp.status_code == 200, resp.text
    assert {r["prefix"] for r in resp.json()["items"]} == {"10.0.0.0/24"}

    # "^65001" — first hop only — matches ONLY the route starting with 65001.
    resp = await client.get("/api/v1/looking-glass/routes?as_path_regexp=^65001", headers=hdr)
    assert resp.status_code == 200
    assert {r["prefix"] for r in resp.json()["items"]} == {"10.0.1.0/24"}


@pytest.mark.asyncio
async def test_routes_as_path_regexp_malformed_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A malformed regexp (unbalanced paren) 422s instead of 500ing."""
    _, token = await _make_admin(db_session)
    hdr = {"Authorization": f"Bearer {token}"}
    resp = await client.get("/api/v1/looking-glass/routes?as_path_regexp=(unbalanced", headers=hdr)
    assert resp.status_code == 422, resp.text


# ── agent register + routes push (end-to-end, real JWT) ─────────────────


@pytest.mark.asyncio
async def test_agent_register_and_push(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setenv("LG_AGENT_KEY", "bootstrap-secret")

    # Bad key → 401.
    bad = await client.post(
        "/api/v1/looking-glass/agents/register",
        headers={"X-LG-Agent-Key": "wrong"},
        json={"hostname": "collector-1", "fingerprint": "fp-1"},
    )
    assert bad.status_code == 401

    # Good key → token + collector_id.
    resp = await client.post(
        "/api/v1/looking-glass/agents/register",
        headers={"X-LG-Agent-Key": "bootstrap-secret"},
        json={"hostname": "collector-1", "fingerprint": "fp-1", "version": "0.1"},
    )
    assert resp.status_code == 200, resp.text
    reg = resp.json()
    token = reg["agent_token"]
    collector_id = reg["collector_id"]

    # A peer under that collector (direct insert — operator would normally POST it).
    peer = await _make_peer(
        db_session, await db_session.get(LookingGlassCollector, uuid.UUID(collector_id))
    )
    await db_session.flush()

    # Push a snapshot with the agent JWT → routes land.
    push = await client.post(
        "/api/v1/looking-glass/agents/routes",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "peer_id": str(peer.id),
            "snapshot": True,
            "routes": [{"prefix": "203.0.113.0/24", "next_hop": "192.0.2.1", "origin_asn": 65001}],
        },
    )
    assert push.status_code in (200, 201), push.text

    landed = (
        await db_session.execute(select(BGPLGRoute).where(BGPLGRoute.peer_id == peer.id))
    ).scalar_one()
    assert str(landed.prefix) == "203.0.113.0/24"


# ── review-fix regressions ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_dedups_duplicate_wire_key(db_session: AsyncSession) -> None:
    """#1 — two wire entries sharing (prefix,next_hop) must not double-insert
    and blow up on the unique constraint."""
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    r = await ingest_routes(
        db_session,
        peer,
        [
            _route("10.0.0.0/24", origin_asn=65001, local_pref=100),
            _route("10.0.0.0/24", origin_asn=65001, local_pref=200),  # dup key
        ],
        snapshot=True,
    )
    assert r.imported == 1  # collapsed, no UniqueViolation
    row = (
        await db_session.execute(select(BGPLGRoute).where(BGPLGRoute.peer_id == peer.id))
    ).scalar_one()
    assert row.local_pref == 200  # last write wins


@pytest.mark.asyncio
async def test_ingest_skips_malformed_prefix(db_session: AsyncSession) -> None:
    """#4 — a malformed CIDR (host bits set / garbage) is skipped, not a 500,
    and valid rows in the same push still land."""
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    r = await ingest_routes(
        db_session,
        peer,
        [
            _route("10.0.0.1/24", origin_asn=65001),  # host bits set — invalid CIDR
            _route("not-an-ip"),  # garbage
            _route("10.0.2.0/24", origin_asn=65001),  # valid
        ],
        snapshot=True,
    )
    assert r.imported == 1
    assert any("malformed" in e for e in r.errors)
    row = (
        await db_session.execute(select(BGPLGRoute).where(BGPLGRoute.peer_id == peer.id))
    ).scalar_one()
    assert str(row.prefix) == "10.0.2.0/24"


@pytest.mark.asyncio
async def test_heartbeat_clears_uptime_on_flap_down(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    """#9 — a heartbeat reporting a non-established state clears uptime_started_at."""
    from datetime import UTC, datetime

    monkeypatch.setenv("LG_AGENT_KEY", "bootstrap-secret")
    reg = (
        await client.post(
            "/api/v1/looking-glass/agents/register",
            headers={"X-LG-Agent-Key": "bootstrap-secret"},
            json={"hostname": "c1", "fingerprint": "fp"},
        )
    ).json()
    token, cid = reg["agent_token"], reg["collector_id"]
    col = await db_session.get(LookingGlassCollector, uuid.UUID(cid))
    peer = await _make_peer(db_session, col, session_state="established")
    peer.uptime_started_at = datetime.now(UTC)
    await db_session.commit()

    hb = await client.post(
        "/api/v1/looking-glass/agents/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"peers": [{"peer_id": str(peer.id), "session_state": "active"}]},
    )
    assert hb.status_code == 200, hb.text
    await db_session.refresh(peer)
    assert peer.session_state == "active"
    assert peer.uptime_started_at is None  # cleared


@pytest.mark.asyncio
async def test_duplicate_peer_address_409(client: AsyncClient, db_session: AsyncSession) -> None:
    """#10 — a second peer with the same collector+peer_address is rejected 409."""
    _, token = await _make_admin(db_session)
    col = await _make_collector(db_session)
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {token}"}
    body = {
        "name": "A",
        "collector_id": str(col.id),
        "local_asn": 65000,
        "peer_asn": 65001,
        "peer_address": "192.0.2.9",
    }
    assert (
        await client.post("/api/v1/looking-glass/peers", headers=hdr, json=body)
    ).status_code in (
        200,
        201,
    )
    dup = await client.post("/api/v1/looking-glass/peers", headers=hdr, json={**body, "name": "B"})
    assert dup.status_code == 409, dup.text


@pytest.mark.asyncio
async def test_disabling_peer_withdraws_routes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#11 — disabling a peer marks its learned routes withdrawn."""
    _, token = await _make_admin(db_session)
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    await ingest_routes(db_session, peer, [_route("10.9.0.0/24", origin_asn=65001)], snapshot=True)
    await db_session.commit()

    resp = await client.patch(
        f"/api/v1/looking-glass/peers/{peer.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": False},
    )
    assert resp.status_code == 200, resp.text
    row = (
        await db_session.execute(select(BGPLGRoute).where(BGPLGRoute.peer_id == peer.id))
    ).scalar_one()
    assert row.withdrawn_at is not None


@pytest.mark.asyncio
async def test_disabled_collector_bundle_is_empty(db_session: AsyncSession) -> None:
    """#12 — a collector toggled enabled=False renders zero peers so sessions drop."""
    col = await _make_collector(db_session, enabled=False)
    await _make_peer(db_session, col)
    await db_session.flush()
    bundle = await build_lg_config_bundle(db_session, col)
    assert bundle.peers == ()


# ── Phase 6 — vpnv4/vpnv6 address families ───────────────────────────────


@pytest.mark.asyncio
async def test_peer_accepts_vpnv4_rejects_evpn(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    col = await _make_collector(db_session)
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/looking-glass/peers",
        headers=hdr,
        json={
            "name": "vpn-peer",
            "collector_id": str(col.id),
            "local_asn": 65000,
            "peer_asn": 65001,
            "peer_address": "192.0.2.20",
            "address_families": ["vpnv4", "vpnv6"],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["address_families"] == ["vpnv4", "vpnv6"]

    resp = await client.post(
        "/api/v1/looking-glass/peers",
        headers=hdr,
        json={
            "name": "evpn-peer",
            "collector_id": str(col.id),
            "local_asn": 65000,
            "peer_asn": 65001,
            "peer_address": "192.0.2.21",
            "address_families": ["evpn"],
        },
    )
    assert resp.status_code == 422, resp.text


# ── Phase 6 — route_distinguisher joins route identity ───────────────────


@pytest.mark.asyncio
async def test_rd_identity_no_collision(db_session: AsyncSession) -> None:
    """Two VRFs' overlapping (prefix, next_hop) must NOT collide when their
    route_distinguisher differs — the core correctness fix behind widening
    uq_bgp_lg_route to include route_distinguisher (issue #566 Phase 6)."""
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)

    result = await ingest_routes(
        db_session,
        peer,
        [
            _route("10.50.0.0/24", origin_asn=65001, route_distinguisher="65001:1"),
            _route("10.50.0.0/24", origin_asn=65001, route_distinguisher="65001:2"),
        ],
        snapshot=True,
    )
    assert result.imported == 2  # NOT silently overwritten into one row

    rows = (
        (
            await db_session.execute(
                select(BGPLGRoute).where(
                    BGPLGRoute.peer_id == peer.id, BGPLGRoute.prefix == "10.50.0.0/24"
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.route_distinguisher for r in rows} == {"65001:1", "65001:2"}


@pytest.mark.asyncio
async def test_rd_ingest_idempotent(db_session: AsyncSession) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    wire = [_route("10.51.0.0/24", origin_asn=65001, route_distinguisher="65001:1")]

    r1 = await ingest_routes(db_session, peer, wire, snapshot=True)
    assert r1.imported == 1 and r1.refreshed == 0

    r2 = await ingest_routes(db_session, peer, wire, snapshot=True)
    assert r2.imported == 0 and r2.refreshed == 1  # re-ingest refreshes, doesn't duplicate


# ── Phase 6 — VRF Route-Target cross-check ───────────────────────────────


async def _make_vrf(db: AsyncSession, **kw) -> VRF:
    vrf = VRF(
        name=kw.pop("name", f"vrf-{uuid.uuid4().hex[:8]}"),
        import_targets=kw.pop("import_targets", []),
        export_targets=kw.pop("export_targets", []),
        **kw,
    )
    db.add(vrf)
    await db.flush()
    return vrf


@pytest.mark.asyncio
async def test_vrf_rt_match_sets_matched_vrf_id(db_session: AsyncSession) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    vrf = await _make_vrf(db_session, import_targets=["65001:100"])

    result = await ingest_routes(
        db_session,
        peer,
        [
            _route("10.0.0.0/24", origin_asn=65001, ext_communities=["65001:100"]),
            # Vendor-style "target:" label prefix — exercises normalize_rt.
            _route("10.0.1.0/24", origin_asn=65001, ext_communities=["target:65001:100"]),
            # No matching RT at all.
            _route("10.0.2.0/24", origin_asn=65001, ext_communities=["65099:1"]),
        ],
        snapshot=True,
    )
    assert result.imported == 3

    rows = {
        str(r.prefix): r.matched_vrf_id
        for r in (await db_session.execute(select(BGPLGRoute).where(BGPLGRoute.peer_id == peer.id)))
        .scalars()
        .all()
    }
    assert rows["10.0.0.0/24"] == vrf.id
    assert rows["10.0.1.0/24"] == vrf.id
    assert rows["10.0.2.0/24"] is None


@pytest.mark.asyncio
async def test_vrf_rt_match_precedence_over_ipam_effective(db_session: AsyncSession) -> None:
    """A Route-Target hit against VRF B wins even when the route's prefix
    falls inside an IPAM block/space whose vrf_id points at a different
    VRF A (the plain IPAM-effective match)."""
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    vrf_a = await _make_vrf(db_session, name="vrf-a-ipam")
    vrf_b = await _make_vrf(db_session, name="vrf-b-rt", import_targets=["65001:200"])

    space = IPSpace(name=f"space-{uuid.uuid4().hex[:8]}", vrf_id=vrf_a.id)
    db_session.add(space)
    await db_session.flush()
    block = IPBlock(space_id=space.id, name="b", network="10.9.0.0/16", vrf_id=vrf_a.id)
    db_session.add(block)
    await db_session.flush()
    ipam_link._clear_cache_for_test()  # force a fresh IPAM scan for this test's fixtures

    await ingest_routes(
        db_session,
        peer,
        [_route("10.9.1.0/24", origin_asn=65001, ext_communities=["65001:200"])],
        snapshot=True,
    )
    row = (
        await db_session.execute(select(BGPLGRoute).where(BGPLGRoute.peer_id == peer.id))
    ).scalar_one()
    assert row.matched_block_id == block.id  # IPAM linkage still resolved…
    assert row.matched_vrf_id == vrf_b.id  # …but the RT match wins over vrf_a.


@pytest.mark.asyncio
async def test_reresolve_sweep_applies_vrf_rt_match(db_session: AsyncSession) -> None:
    """A VRF created AFTER a route's last ingest still converges via the
    periodic re-resolve sweep — same precedence rule as ingest time."""
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    await ingest_routes(
        db_session,
        peer,
        [_route("10.20.0.0/24", origin_asn=65001, ext_communities=["65005:300"])],
        snapshot=True,
    )
    row = (
        await db_session.execute(select(BGPLGRoute).where(BGPLGRoute.peer_id == peer.id))
    ).scalar_one()
    assert row.matched_vrf_id is None  # no VRF existed yet at ingest time

    vrf = await _make_vrf(db_session, import_targets=["65005:300"])
    await db_session.commit()  # the sweep opens its own engine/connection

    stats = await _reresolve_route_links_async()
    assert stats["checked"] >= 1
    assert stats["changed"] >= 1

    await db_session.refresh(row)
    assert row.matched_vrf_id == vrf.id


@pytest.mark.asyncio
async def test_vrf_rt_matches_endpoint(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    vrf = await _make_vrf(db_session, import_targets=["65001:100"], export_targets=["65001:200"])
    await ingest_routes(
        db_session,
        peer,
        [
            _route("10.30.0.0/24", origin_asn=65001, ext_communities=["65001:100"]),
            _route("10.30.1.0/24", origin_asn=65001, ext_communities=["65001:100"]),
            _route("10.30.2.0/24", origin_asn=65001, ext_communities=["65001:200"]),
        ],
        snapshot=True,
    )
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {token}"}

    resp = await client.get(f"/api/v1/looking-glass/vrf-rt-matches/{vrf.id}", headers=hdr)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["matched_route_count"] == 3
    by_rt = {
        (row["route_target"], row["kind"]): row["matched_route_count"]
        for row in body["route_targets"]
    }
    assert by_rt[("65001:100", "import")] == 2
    assert by_rt[("65001:200", "export")] == 1

    # 404 for an unknown VRF id.
    resp = await client.get(f"/api/v1/looking-glass/vrf-rt-matches/{uuid.uuid4()}", headers=hdr)
    assert resp.status_code == 404


# ── Phase 6 — multicast <-> BGP reachability cross-reference ─────────────


@pytest.mark.asyncio
async def test_multicast_bgp_reachability_domain_rp(db_session: AsyncSession) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    domain = MulticastDomain(
        name=f"dom-{uuid.uuid4().hex[:8]}",
        pim_mode="sparse",
        rendezvous_point_address="192.168.50.1",
    )
    db_session.add(domain)
    await db_session.flush()

    # No covering route yet.
    result = await multicast_bgp_reachability(db_session)
    unreachable = next(d for d in result.domains if d.domain_id == domain.id)
    assert unreachable.covering_route is None

    await ingest_routes(
        db_session, peer, [_route("192.168.50.0/24", origin_asn=65001)], snapshot=True
    )
    result = await multicast_bgp_reachability(db_session)
    reachable = next(d for d in result.domains if d.domain_id == domain.id)
    assert reachable.covering_route is not None
    assert str(reachable.covering_route.prefix) == "192.168.50.0/24"


@pytest.mark.asyncio
async def test_multicast_bgp_reachability_group_source_subnet(
    db_session: AsyncSession,
) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    space = IPSpace(name=f"mc-space-{uuid.uuid4().hex[:8]}")
    db_session.add(space)
    await db_session.flush()
    block = IPBlock(space_id=space.id, name="b", network="10.40.0.0/16")
    db_session.add(block)
    await db_session.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, name="s", network="10.40.1.0/24")
    db_session.add(subnet)
    await db_session.flush()
    ip = IPAddress(subnet_id=subnet.id, address="10.40.1.5", status="allocated")
    db_session.add(ip)
    await db_session.flush()

    mgroup = MulticastGroup(space_id=space.id, address="239.1.1.1", name="mg")
    db_session.add(mgroup)
    await db_session.flush()
    db_session.add(MulticastMembership(group_id=mgroup.id, ip_address_id=ip.id, role="producer"))
    await db_session.flush()

    # A /32 route sitting on the subnet's base address must NOT count as
    # covering the WHOLE subnet (negative case).
    await ingest_routes(db_session, peer, [_route("10.40.1.0/32", origin_asn=65001)], snapshot=True)
    result = await multicast_bgp_reachability(db_session)
    group_result = next(g for g in result.groups if g.group_id == mgroup.id)
    assert group_result.covering_route is None

    # A route covering the whole /24 counts.
    await ingest_routes(
        db_session, peer, [_route("10.40.0.0/16", origin_asn=65001)], snapshot=False
    )
    result = await multicast_bgp_reachability(db_session)
    group_result = next(g for g in result.groups if g.group_id == mgroup.id)
    assert group_result.covering_route is not None
    assert str(group_result.covering_route.prefix) == "10.40.0.0/16"


@pytest.mark.asyncio
async def test_multicast_reachability_endpoint(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    domain = MulticastDomain(
        name=f"dom-{uuid.uuid4().hex[:8]}",
        pim_mode="sparse",
        rendezvous_point_address="192.168.60.1",
    )
    db_session.add(domain)
    await db_session.flush()
    await ingest_routes(
        db_session, peer, [_route("192.168.60.0/24", origin_asn=65001)], snapshot=True
    )
    await db_session.commit()

    resp = await client.get(
        "/api/v1/looking-glass/multicast-reachability",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    dom = next(d for d in body["domains"] if d["domain_id"] == str(domain.id))
    assert dom["covering_route"]["prefix"] == "192.168.60.0/24"


# ── Phase 6 — Operator Copilot tools ──────────────────────────────────────


@pytest.mark.asyncio
async def test_find_vrf_learned_routes_tool(db_session: AsyncSession) -> None:
    user, _ = await _make_admin(db_session)
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    vrf = await _make_vrf(db_session, import_targets=["65001:900"])
    await ingest_routes(
        db_session,
        peer,
        [_route("10.60.0.0/24", origin_asn=65001, ext_communities=["65001:900"])],
        snapshot=True,
    )

    out = await find_vrf_learned_routes(db_session, user, FindVrfLearnedRoutesArgs(vrf_id=vrf.id))
    assert out["count"] == 1
    assert out["routes"][0]["prefix"] == "10.60.0.0/24"


@pytest.mark.asyncio
async def test_find_multicast_bgp_reachability_tool(db_session: AsyncSession) -> None:
    user, _ = await _make_admin(db_session)
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    domain = MulticastDomain(
        name=f"dom-{uuid.uuid4().hex[:8]}",
        pim_mode="sparse",
        rendezvous_point_address="192.168.70.1",
    )
    db_session.add(domain)
    await db_session.flush()
    await ingest_routes(
        db_session, peer, [_route("192.168.70.0/24", origin_asn=65001)], snapshot=True
    )

    out = await find_multicast_bgp_reachability(
        db_session, user, FindMulticastBgpReachabilityArgs()
    )
    dom = next(d for d in out["domains"] if d["domain_id"] == str(domain.id))
    assert dom["reachable"] is True
    assert dom["covering_route"]["prefix"] == "192.168.70.0/24"
