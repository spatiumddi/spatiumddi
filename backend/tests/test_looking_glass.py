"""BGP Looking Glass tests — issue #566 Phase 1+2.

Covers the correctness-critical RIB reconcile (``ingest_routes`` — the
zero-wire floor guard, absence-withdraw, idempotency, re-announce), the peer
CRUD surface (Fernet ``md5_password`` never echoed, audit row written), the
feature-module gate, the routes search filters, and the agent register +
routes-push end-to-end path.
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
from app.services import feature_modules
from app.services.looking_glass.config_bundle import build_lg_config_bundle
from app.services.looking_glass.routes_ingest import ingest_routes

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
