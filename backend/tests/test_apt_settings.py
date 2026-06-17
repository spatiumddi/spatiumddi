"""Appliance APT host-config (issue #155) — renderer + bundle + API.

Covers the pure renderers (sources.list / proxy / auth), the
``apt_bundle`` shape + hash, the validate endpoint's structural checks,
and the PUT → GET round-trip with secret redaction (armoured key text +
auth passwords fold into ``*_set`` booleans, never returned plaintext).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.settings import DEFAULT_APT_SOURCES, PlatformSettings
from app.services.appliance.apt import (
    apt_bundle,
    render_auth_conf,
    render_proxy_conf,
    render_sources_list,
)

pytestmark = pytest.mark.asyncio


async def _superadmin(db: AsyncSession) -> tuple[User, str]:
    u = User(
        username=f"admin-{uuid.uuid4().hex[:8]}",
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


def _shim(**kw) -> PlatformSettings:
    s = PlatformSettings(id=0)
    s.apt_managed = kw.get("managed", True)
    s.apt_sources = kw.get("sources", [dict(x) for x in DEFAULT_APT_SOURCES])
    s.apt_gpg_keys = kw.get("gpg_keys", [])
    s.apt_proxy_http = kw.get("proxy_http", "")
    s.apt_proxy_https = kw.get("proxy_https", "")
    s.apt_proxy_no_proxy = kw.get("no_proxy", "")
    s.apt_auth = kw.get("auth", [])
    s.apt_unattended_upgrades_enabled = kw.get("unattended", True)
    return s


# ── (a) pure renderers ──────────────────────────────────────────────


def test_render_sources_list_emits_enabled_only_with_signed_by() -> None:
    s = _shim(
        sources=[
            {
                "name": "Internal",
                "uri": "https://mirror.lan/debian",
                "suites": "trixie",
                "components": "main",
                "signed_by_key_id": "lan-key",
                "enabled": True,
            },
            {
                "name": "Disabled",
                "uri": "https://nope.lan/debian",
                "suites": "trixie",
                "components": "main",
                "signed_by_key_id": "",
                "enabled": False,
            },
        ]
    )
    out = render_sources_list(s)
    assert "https://mirror.lan/debian trixie main" in out
    assert "[signed-by=/etc/apt/keyrings/spatiumddi-lan-key.asc]" in out
    # The disabled source is omitted.
    assert "nope.lan" not in out


def test_render_proxy_conf_includes_no_proxy_direct() -> None:
    s = _shim(proxy_http="http://proxy.lan:3128/", no_proxy="localhost, mirror.lan")
    out = render_proxy_conf(s)
    assert 'Acquire::http::Proxy "http://proxy.lan:3128/";' in out
    assert 'Acquire::http::Proxy::localhost "DIRECT";' in out
    assert 'Acquire::http::Proxy::mirror.lan "DIRECT";' in out
    # No proxy configured → empty string (runner removes the file).
    assert render_proxy_conf(_shim()) == ""


def test_apt_bundle_disabled_when_unmanaged() -> None:
    b = apt_bundle(_shim(managed=False))
    assert b["enabled"] is False
    assert b["config_hash"] == ""
    # Managed flips it on with a stable non-empty hash.
    b2 = apt_bundle(_shim(managed=True))
    assert b2["enabled"] is True
    assert len(b2["config_hash"]) == 64
    # Deterministic — same settings → same hash.
    assert apt_bundle(_shim(managed=True))["config_hash"] == b2["config_hash"]


def test_render_auth_conf_skips_entries_without_password() -> None:
    # password_enc None → entry skipped (can't render a netrc line).
    s = _shim(auth=[{"machine": "mirror.lan", "login": "bob", "password_enc": None}])
    assert render_auth_conf(s) == ""


# ── (b) validate endpoint ───────────────────────────────────────────


async def test_validate_flags_no_enabled_sources(client: AsyncClient, db_session):
    _, token = await _superadmin(db_session)
    r = await client.post(
        "/api/v1/settings/apt/validate",
        headers=_hdr(token),
        json={"apt_sources": []},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is False
    assert any("no enabled sources" in e.lower() for e in body["errors"])


async def test_validate_warns_on_missing_signing_key(client: AsyncClient, db_session):
    _, token = await _superadmin(db_session)
    r = await client.post(
        "/api/v1/settings/apt/validate",
        headers=_hdr(token),
        json={
            "apt_sources": [
                {
                    "name": "Internal",
                    "uri": "https://mirror.lan/debian",
                    "suites": "trixie",
                    "components": "main",
                    "signed_by_key_id": "ghost-key",
                    "enabled": True,
                }
            ],
            "apt_gpg_key_ids": [],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True  # structurally valid; missing key is a warning
    assert any("NO_PUBKEY" in w for w in body["warnings"])
    assert "mirror.lan/debian" in body["sources_list_preview"]


async def test_validate_rejects_bad_uri_scheme(client: AsyncClient, db_session):
    _, token = await _superadmin(db_session)
    r = await client.post(
        "/api/v1/settings/apt/validate",
        headers=_hdr(token),
        json={"apt_sources": [{"uri": "ftp-bad://x", "suites": "trixie", "enabled": True}]},
    )
    # The AptSourceUpdate validator rejects the scheme → 422 at the schema.
    assert r.status_code == 422


async def test_put_rejects_path_injection_in_signed_by(client: AsyncClient, db_session):
    """signed_by_key_id flows into the keyring path — a separator/escape
    must be rejected at the schema (not sanitised silently)."""
    _, token = await _superadmin(db_session)
    r = await client.put(
        "/api/v1/settings",
        headers=_hdr(token),
        json={
            "apt_managed": True,
            "apt_sources": [
                {
                    "uri": "https://mirror.lan/debian",
                    "suites": "trixie",
                    "signed_by_key_id": "../../etc/evil",
                    "enabled": True,
                }
            ],
        },
    )
    assert r.status_code == 422


async def test_put_rejects_proxy_with_quote(client: AsyncClient, db_session):
    _, token = await _superadmin(db_session)
    r = await client.put(
        "/api/v1/settings",
        headers=_hdr(token),
        json={"apt_proxy_http": 'http://h" ;}'},
    )
    assert r.status_code == 422


# ── (c) PUT → GET redaction round-trip ──────────────────────────────


async def test_put_then_get_redacts_secrets(client: AsyncClient, db_session):
    _, token = await _superadmin(db_session)
    h = _hdr(token)

    r = await client.put(
        "/api/v1/settings",
        headers=h,
        json={
            "apt_managed": True,
            "apt_sources": [
                {
                    "name": "Internal",
                    "uri": "https://mirror.lan/debian",
                    "suites": "trixie",
                    "components": "main",
                    "signed_by_key_id": "lan-key",
                    "enabled": True,
                }
            ],
            "apt_gpg_keys": [
                {
                    "key_id": "lan-key",
                    "comment": "LAN mirror",
                    "armoured_text": "-----BEGIN PGP PUBLIC KEY BLOCK-----\nabc\n-----END PGP PUBLIC KEY BLOCK-----",
                }
            ],
            "apt_auth": [{"machine": "mirror.lan", "login": "bob", "password": "s3cret"}],
            "apt_proxy_http": "http://proxy.lan:3128/",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["apt_managed"] is True
    # Secrets folded to booleans — never the armoured text / password.
    assert body["apt_gpg_keys"][0]["armoured_text_set"] is True
    assert "armoured_text" not in body["apt_gpg_keys"][0]
    assert body["apt_auth"][0]["password_set"] is True
    assert "password" not in body["apt_auth"][0]

    # The rendered bundle reflects the stored (decrypted) config.
    settings = await db_session.get(PlatformSettings, 1)
    assert settings is not None
    bundle = apt_bundle(settings)
    assert bundle["enabled"] is True
    assert "lan-key" in bundle["keyrings"]
    assert "BEGIN PGP PUBLIC KEY BLOCK" in bundle["keyrings"]["lan-key"]
    assert "machine mirror.lan login bob password s3cret" in bundle["auth_conf"]

    # NN #4 — the APT change wrote a dedicated audit row, and it never
    # records the armoured key text or the password.
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.resource_type == "platform_settings",
                    AuditLog.resource_id == "apt",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    nv = rows[0].new_value or {}
    assert nv.get("managed") is True
    assert nv.get("gpg_key_count") == 1
    blob = repr(nv)
    assert "s3cret" not in blob and "PGP PUBLIC KEY" not in blob


async def test_put_preserves_secret_on_omit(client: AsyncClient, db_session):
    """Editing a GPG key's comment without re-pasting the armoured text
    preserves the stored ciphertext (merge-by-key_id)."""
    _, token = await _superadmin(db_session)
    h = _hdr(token)
    await client.put(
        "/api/v1/settings",
        headers=h,
        json={
            "apt_managed": True,
            "apt_gpg_keys": [{"key_id": "k1", "comment": "first", "armoured_text": "KEYBODY"}],
        },
    )
    # Re-PUT with no armoured_text → preserve.
    await client.put(
        "/api/v1/settings",
        headers=h,
        json={"apt_gpg_keys": [{"key_id": "k1", "comment": "renamed"}]},
    )
    settings = await db_session.get(PlatformSettings, 1)
    assert settings is not None
    bundle = apt_bundle(settings)
    assert bundle["keyrings"].get("k1") == "KEYBODY"
