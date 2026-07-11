"""Active block sync (#601) — write-back enforcement.

Covers the pure helpers (value normalisation, active-block gate), the
per-target config-error guards, the target-driven OPNsense reconciler with
a mocked client, and the REST surface (module gating, block create/list/
lift, target arming validation).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.block_sync import NetworkBlock, NetworkBlockPush
from app.models.feature_module import FeatureModule
from app.models.ipam import IPSpace
from app.models.opnsense import OPNsenseRouter
from app.models.unifi import UnifiController
from app.services import feature_modules
from app.services.block_sync import reconcile

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _reset_module_cache():
    feature_modules.invalidate_cache()
    yield
    feature_modules.invalidate_cache()


async def _enable_module(db: AsyncSession) -> None:
    db.add(FeatureModule(id="security.block_sync", enabled=True))
    await db.flush()
    feature_modules.invalidate_cache()


async def _superadmin(db: AsyncSession) -> tuple[User, str]:
    u = User(
        username=f"a-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return u, create_access_token(str(u.id))


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _space(db: AsyncSession) -> IPSpace:
    s = IPSpace(name=f"bs-{uuid.uuid4().hex[:6]}", description="")
    db.add(s)
    await db.flush()
    return s


async def _armed_opnsense(db: AsyncSession, space: IPSpace) -> OPNsenseRouter:
    r = OPNsenseRouter(
        name=f"fw-{uuid.uuid4().hex[:6]}",
        host="10.0.0.1",
        api_key="ro",
        api_secret_encrypted=encrypt_str("ro-secret"),
        ipam_space_id=space.id,
        block_sync_enabled=True,
        block_alias_name="spatiumddi_blocked",
        block_sync_api_key="rw",
        block_sync_api_secret_encrypted=encrypt_str("rw-secret"),
    )
    db.add(r)
    await db.flush()
    return r


# ── Pure helpers ─────────────────────────────────────────────────────


def test_normalize_block_value_ip():
    assert reconcile.normalize_block_value("ip", " 10.0.0.5 ") == "10.0.0.5"


def test_normalize_block_value_mac():
    assert reconcile.normalize_block_value("mac", "AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"


@pytest.mark.parametrize("kind,value", [("ip", "not-an-ip"), ("mac", "zz"), ("bogus", "x")])
def test_normalize_block_value_rejects(kind, value):
    with pytest.raises(ValueError):
        reconcile.normalize_block_value(kind, value)


def test_block_is_active():
    now = datetime.now(UTC)
    b = NetworkBlock(kind="ip", value="10.0.0.1", enabled=True, expires_at=None)
    assert reconcile.block_is_active(b, now) is True
    b.enabled = False
    assert reconcile.block_is_active(b, now) is False
    b.enabled = True
    b.expires_at = now - timedelta(minutes=1)
    assert reconcile.block_is_active(b, now) is False
    b.expires_at = now + timedelta(minutes=1)
    assert reconcile.block_is_active(b, now) is True


def test_opnsense_config_error_paths():
    r = OPNsenseRouter(name="x", host="h", api_key="", ipam_space_id=uuid.uuid4())
    r.block_sync_enabled = False
    assert reconcile.opnsense_config_error(r) == "block sync not armed on this target"
    r.block_sync_enabled = True
    r.block_alias_name = ""
    assert "alias" in (reconcile.opnsense_config_error(r) or "")
    r.block_alias_name = "a"
    r.block_sync_api_key = ""
    assert "credential" in (reconcile.opnsense_config_error(r) or "")


def test_unifi_config_error_paths():
    c = UnifiController(name="x", ipam_space_id=uuid.uuid4())
    c.block_sync_enabled = False
    assert reconcile.unifi_config_error(c) == "block sync not armed on this target"
    c.block_sync_enabled = True
    c.block_sync_auth_kind = "api_key"
    c.block_sync_api_key_encrypted = b""
    assert "API key" in (reconcile.unifi_config_error(c) or "")


# ── Reconciler (mocked OPNsense client) ──────────────────────────────


class _FakeOPNClient:
    def __init__(self, existing: list[str]) -> None:
        self.members = list(existing)
        self.added: list[str] = []
        self.deleted: list[str] = []
        self.reconfigured = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def alias_list_addresses(self, alias: str) -> list[str]:
        return list(self.members)

    async def alias_add_address(self, alias: str, address: str) -> None:
        self.added.append(address)
        self.members.append(address)

    async def alias_delete_address(self, alias: str, address: str) -> None:
        self.deleted.append(address)
        if address in self.members:
            self.members.remove(address)

    async def alias_reconfigure(self) -> None:
        self.reconfigured += 1


class _ReconfigureFailsClient(_FakeOPNClient):
    async def alias_reconfigure(self) -> None:
        from app.services.opnsense.client import OPNsenseClientError

        raise OPNsenseClientError("reconfigure boom")


async def test_reconcile_opnsense_adds_and_lifts(db_session: AsyncSession, monkeypatch):
    space = await _space(db_session)
    router = await _armed_opnsense(db_session, space)

    active = NetworkBlock(kind="ip", value="10.0.0.5", enabled=True)
    lifted = NetworkBlock(kind="ip", value="10.0.0.9", enabled=False)
    db_session.add_all([active, lifted])
    await db_session.flush()
    # A stale push row for the now-disabled block, present on the device.
    db_session.add(
        NetworkBlockPush(
            block_id=lifted.id,
            target_kind="opnsense",
            target_id=router.id,
            push_status="pushed",
        )
    )
    await db_session.flush()

    fake = _FakeOPNClient(existing=["10.0.0.9"])
    monkeypatch.setattr(reconcile, "_opnsense_client", lambda r: fake)

    summary = await reconcile.reconcile_opnsense(db_session, router)
    assert summary.ok is True
    assert fake.added == ["10.0.0.5"]  # active block pushed
    assert fake.deleted == ["10.0.0.9"]  # lifted block removed
    assert fake.reconfigured == 1

    pushes = (await db_session.execute(select(NetworkBlockPush))).scalars().all()
    # The active block now owns a pushed row; the lifted one's row is gone.
    assert len(pushes) == 1
    assert pushes[0].block_id == active.id
    assert pushes[0].push_status == "pushed"


async def test_reconcile_opnsense_reconfigure_failure_not_pushed(
    db_session: AsyncSession, monkeypatch
):
    """A failed reconfigure must NOT mark the block converged, and the next
    pass must retry the reconfigure (#601 review #6)."""
    space = await _space(db_session)
    router = await _armed_opnsense(db_session, space)
    active = NetworkBlock(kind="ip", value="10.0.0.5", enabled=True)
    db_session.add(active)
    await db_session.flush()

    fail = _ReconfigureFailsClient(existing=[])
    monkeypatch.setattr(reconcile, "_opnsense_client", lambda r: fail)
    summary = await reconcile.reconcile_opnsense(db_session, router)
    assert summary.ok is False
    assert fail.added == ["10.0.0.5"]  # added to the alias table…
    pushes = (await db_session.execute(select(NetworkBlockPush))).scalars().all()
    assert len(pushes) == 1
    assert pushes[0].push_status == "error"  # …but NOT reported converged

    # Next pass with a healthy client retries reconfigure even though the IP is
    # already in the alias table, and confirms the push.
    ok = _FakeOPNClient(existing=["10.0.0.5"])
    monkeypatch.setattr(reconcile, "_opnsense_client", lambda r: ok)
    summary2 = await reconcile.reconcile_opnsense(db_session, router)
    assert summary2.ok is True
    assert ok.reconfigured == 1  # reconfigure retried despite no add
    pushes2 = (await db_session.execute(select(NetworkBlockPush))).scalars().all()
    assert pushes2[0].push_status == "pushed"


async def test_unifi_post_legacy_rejects_rc_error():
    """HTTP 200 with meta.rc=error must raise, not be a silent success
    (#601 review #3)."""
    import httpx

    from app.services.unifi.client import (
        UnifiClient,
        UnifiClientConfig,
        UnifiClientError,
    )

    cfg = UnifiClientConfig(
        mode="local",
        host="ctrl",
        port=443,
        cloud_host_id=None,
        verify_tls=False,
        ca_bundle_pem="",
        auth_kind="api_key",
        api_key="k",
        username="",
        password="",
    )
    client = UnifiClient(cfg)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"meta": {"rc": "error", "msg": "api.err.NoSiteContext"}, "data": []}
        )

    client._client = httpx.AsyncClient(
        base_url="https://ctrl", transport=httpx.MockTransport(_handler)
    )
    try:
        with pytest.raises(UnifiClientError):
            await client.block_client("default", "aa:bb:cc:dd:ee:ff")
    finally:
        await client._client.aclose()


# ── REST surface ─────────────────────────────────────────────────────


async def test_module_gated_404_when_off(client: AsyncClient, db_session: AsyncSession):
    _u, token = await _superadmin(db_session)
    await db_session.commit()
    resp = await client.get("/api/v1/block-sync/blocks", headers=_hdr(token))
    assert resp.status_code == 404


async def test_create_list_lift_block(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    # No armed targets → the create commits without touching any device.
    monkeypatch.setattr(
        "app.tasks.block_sync.reconcile_target_now.delay",
        lambda *a, **k: None,
    )
    await _enable_module(db_session)
    _u, token = await _superadmin(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/block-sync/blocks",
        headers=_hdr(token),
        json={"kind": "mac", "value": "AA:BB:CC:DD:EE:01", "reason": "quarantine"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "mac"
    assert body["value"] == "aa:bb:cc:dd:ee:01"
    assert body["enabled"] is True
    block_id = body["id"]

    lst = await client.get("/api/v1/block-sync/blocks", headers=_hdr(token))
    assert lst.status_code == 200
    assert any(b["id"] == block_id for b in lst.json())

    lifted = await client.delete(f"/api/v1/block-sync/blocks/{block_id}", headers=_hdr(token))
    assert lifted.status_code == 200
    assert lifted.json()["enabled"] is False


async def test_create_block_rejects_bad_value(client: AsyncClient, db_session: AsyncSession):
    await _enable_module(db_session)
    _u, token = await _superadmin(db_session)
    await db_session.commit()
    resp = await client.post(
        "/api/v1/block-sync/blocks",
        headers=_hdr(token),
        json={"kind": "ip", "value": "not-an-ip"},
    )
    assert resp.status_code == 422


async def test_arm_opnsense_requires_creds(client: AsyncClient, db_session: AsyncSession):
    await _enable_module(db_session)
    space = await _space(db_session)
    router = OPNsenseRouter(
        name="fw1",
        host="10.0.0.1",
        api_key="ro",
        api_secret_encrypted=encrypt_str("s"),
        ipam_space_id=space.id,
    )
    db_session.add(router)
    await db_session.flush()
    _u, token = await _superadmin(db_session)
    await db_session.commit()

    # Arming enabled without alias + write creds → 422.
    resp = await client.put(
        f"/api/v1/block-sync/targets/opnsense/{router.id}",
        headers=_hdr(token),
        json={"block_sync_enabled": True},
    )
    assert resp.status_code == 422

    # With alias + creds → armed.
    resp = await client.put(
        f"/api/v1/block-sync/targets/opnsense/{router.id}",
        headers=_hdr(token),
        json={
            "block_sync_enabled": True,
            "block_alias_name": "spatiumddi_blocked",
            "block_sync_api_key": "rw",
            "block_sync_api_secret": "rw-secret",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["block_sync_enabled"] is True
    assert body["write_credentials_present"] is True


async def test_arm_unifi_rejects_cloud_user_password(client: AsyncClient, db_session: AsyncSession):
    """Cloud-mode UniFi + user_password must be rejected at arm time so a
    silently-inert target can't be armed (#601 review #7)."""
    await _enable_module(db_session)
    space = await _space(db_session)
    c = UnifiController(name="ctrl1", mode="cloud", cloud_host_id="abc", ipam_space_id=space.id)
    db_session.add(c)
    await db_session.flush()
    _u, token = await _superadmin(db_session)
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/block-sync/targets/unifi/{c.id}",
        headers=_hdr(token),
        json={
            "block_sync_enabled": True,
            "block_sync_auth_kind": "user_password",
            "block_sync_username": "admin",
            "block_sync_password": "pw",
        },
    )
    assert resp.status_code == 422
    assert "cloud" in resp.text.lower()


def test_gateable_pairs_excludes_cross_pairs():
    """Widening GATEABLE_ACTIONS/RESOURCE_TYPES must not admit cross pairs
    that map to no registered op (#601 review #2)."""
    from app.services.approvals.policy import gateable_pairs

    pairs = gateable_pairs()
    assert ("admin", "manage_block_sync") in pairs
    assert ("delete", "subnet") in pairs
    # Cross pairs the two independent sets would allow but no op declares:
    assert ("admin", "subnet") not in pairs
    assert ("delete", "manage_block_sync") not in pairs
