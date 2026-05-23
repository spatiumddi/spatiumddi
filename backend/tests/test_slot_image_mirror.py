"""Slot-image mirror tests (#296 Phase B).

Covers the pure-Python pieces of the mirror surface — the HMAC auth
helper + the preflight mirror disk-usage check — plus the api proxy
mode's local-FS fallback (no mirror configured) round-trip.

The end-to-end mirror integration (full PUT/GET/DELETE against a
live mirror pod) is exercised by ``make test`` against the dev compose
stack when ``SLOT_IMAGE_MIRROR_URL`` is set; that path requires real
network so it's out of scope for the unit-test pass here.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.api.v1.appliance import slot_image_mirror
from app.services.upgrades import preflight

# ── Auth helper ──────────────────────────────────────────────────────


def test_mirror_auth_token_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same (operation, image_id, secret) → same token."""
    monkeypatch.setattr(slot_image_mirror.settings, "slot_image_mirror_secret", "s3cret")
    image_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    a = slot_image_mirror.mirror_auth_token("put", image_id)
    b = slot_image_mirror.mirror_auth_token("put", image_id)
    assert a == b
    # 64-char lowercase hex.
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_mirror_auth_token_operation_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    """``put`` vs ``get`` against the same id must produce different tokens.

    Prevents a leaked PUT token from being replayed as a GET (and vice
    versa). The HMAC payload is ``<op>:<image_id>`` exactly.
    """
    monkeypatch.setattr(slot_image_mirror.settings, "slot_image_mirror_secret", "s3cret")
    image_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    put_tok = slot_image_mirror.mirror_auth_token("put", image_id)
    get_tok = slot_image_mirror.mirror_auth_token("get", image_id)
    delete_tok = slot_image_mirror.mirror_auth_token("delete", image_id)
    disk_tok = slot_image_mirror.mirror_auth_token("disk-usage", uuid.UUID(int=0))
    assert len({put_tok, get_tok, delete_tok, disk_tok}) == 4


def test_mirror_auth_token_secret_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same payload with different secret → different token."""
    image_id = uuid.UUID(int=42)
    monkeypatch.setattr(slot_image_mirror.settings, "slot_image_mirror_secret", "secret-a")
    a = slot_image_mirror.mirror_auth_token("get", image_id)
    monkeypatch.setattr(slot_image_mirror.settings, "slot_image_mirror_secret", "secret-b")
    b = slot_image_mirror.mirror_auth_token("get", image_id)
    assert a != b


def test_mirror_auth_token_unconfigured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Booting the mirror without the shared secret should fail loud
    rather than silently accepting every request."""
    monkeypatch.setattr(slot_image_mirror.settings, "slot_image_mirror_secret", "")
    with pytest.raises(RuntimeError, match="slot_image_mirror_secret"):
        slot_image_mirror.mirror_auth_token("put", uuid.UUID(int=1))


# ── _verify_auth (constant-time compare + missing-header semantics) ──


def test_verify_auth_missing_header(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException  # noqa: PLC0415

    monkeypatch.setattr(slot_image_mirror.settings, "slot_image_mirror_secret", "s3cret")
    with pytest.raises(HTTPException) as exc:
        slot_image_mirror._verify_auth("put", uuid.UUID(int=1), None)
    assert exc.value.status_code == 401


def test_verify_auth_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException  # noqa: PLC0415

    monkeypatch.setattr(slot_image_mirror.settings, "slot_image_mirror_secret", "s3cret")
    with pytest.raises(HTTPException) as exc:
        slot_image_mirror._verify_auth("put", uuid.UUID(int=1), "deadbeef" * 8)
    assert exc.value.status_code == 403


def test_verify_auth_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token minted with the same (op, id, secret) verifies."""
    monkeypatch.setattr(slot_image_mirror.settings, "slot_image_mirror_secret", "s3cret")
    image_id = uuid.UUID(int=42)
    valid = slot_image_mirror.mirror_auth_token("put", image_id)
    # Should not raise.
    slot_image_mirror._verify_auth("put", image_id, valid)


def test_verify_auth_wrong_operation_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token minted for ``put`` must not authorise a ``get`` request
    against the same image_id."""
    from fastapi import HTTPException  # noqa: PLC0415

    monkeypatch.setattr(slot_image_mirror.settings, "slot_image_mirror_secret", "s3cret")
    image_id = uuid.UUID(int=42)
    put_tok = slot_image_mirror.mirror_auth_token("put", image_id)
    with pytest.raises(HTTPException) as exc:
        slot_image_mirror._verify_auth("get", image_id, put_tok)
    assert exc.value.status_code == 403


# ── Preflight: check_mirror_disk_headroom ────────────────────────────


@pytest.mark.asyncio
async def test_mirror_preflight_no_mirror_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-instance / docker-compose shape — no mirror URL → ok."""
    monkeypatch.setattr(preflight.settings, "slot_image_mirror_url", "")
    result = await preflight.check_mirror_disk_headroom()
    assert result.level == "ok"
    assert "no mirror configured" in result.message


@pytest.mark.asyncio
async def test_mirror_preflight_unreachable_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Network failure querying the mirror → warn (not fail) so the
    rest of the preflight runs."""
    monkeypatch.setattr(
        preflight.settings,
        "slot_image_mirror_url",
        "http://nonexistent.invalid",
    )
    monkeypatch.setattr(preflight.settings, "slot_image_mirror_secret", "s3cret")

    class _FailingClient:
        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, *args: Any, **kwargs: Any) -> Any:
            raise httpx.ConnectError("simulated")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _FailingClient())
    result = await preflight.check_mirror_disk_headroom()
    assert result.level == "warn"
    assert "could not reach mirror" in result.message


@pytest.mark.asyncio
async def test_mirror_preflight_plenty(monkeypatch: pytest.MonkeyPatch) -> None:
    """100 GiB free + need 5 GiB → ok."""
    monkeypatch.setattr(preflight.settings, "slot_image_mirror_url", "http://mirror")
    monkeypatch.setattr(preflight.settings, "slot_image_mirror_secret", "s3cret")

    class _OkClient:
        async def __aenter__(self) -> _OkClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, *args: Any, **kwargs: Any) -> httpx.Response:
            req = httpx.Request("GET", args[0] if args else "http://mirror")
            return httpx.Response(
                200,
                json={
                    "path": "/var/lib/spatiumddi/slot-images",
                    "free_bytes": 100 * 1024**3,
                    "total_bytes": 128 * 1024**3,
                    "used_bytes": 28 * 1024**3,
                },
                request=req,
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _OkClient())
    result = await preflight.check_mirror_disk_headroom()
    assert result.level == "ok"
    assert result.detail["free_bytes"] == 100 * 1024**3


@pytest.mark.asyncio
async def test_mirror_preflight_insufficient_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 GiB free + need 5 GiB → fail."""
    monkeypatch.setattr(preflight.settings, "slot_image_mirror_url", "http://mirror")
    monkeypatch.setattr(preflight.settings, "slot_image_mirror_secret", "s3cret")

    class _SmallClient:
        async def __aenter__(self) -> _SmallClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, *args: Any, **kwargs: Any) -> httpx.Response:
            req = httpx.Request("GET", args[0] if args else "http://mirror")
            return httpx.Response(
                200,
                json={
                    "path": "/var/lib/spatiumddi/slot-images",
                    "free_bytes": 3 * 1024**3,
                    "total_bytes": 8 * 1024**3,
                    "used_bytes": 5 * 1024**3,
                },
                request=req,
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _SmallClient())
    result = await preflight.check_mirror_disk_headroom()
    assert result.level == "fail"


# ── run_all ships the new check ──────────────────────────────────────


@pytest.mark.asyncio
async def test_run_all_includes_mirror_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The aggregator includes ``mirror_disk_headroom`` so the UI's
    preflight panel always shows the row (even on docker-compose
    where it short-circuits to ok)."""
    monkeypatch.setattr(preflight.settings, "slot_image_mirror_url", "")
    ok = preflight.PreflightResult("x", "ok", "fine", {})
    with (
        patch.object(preflight, "check_inflight_conflict", return_value=ok),
        patch.object(preflight, "check_disk_headroom", return_value=ok),
        patch.object(preflight, "check_version_path", return_value=ok),
        patch.object(preflight, "check_quorum", return_value=ok),
    ):

        async def _ok_async(**kw: Any) -> preflight.PreflightResult:
            return ok

        with patch.object(preflight, "check_replication_lag", _ok_async):
            report = await preflight.run_all(target_version="2026.06.01-1")

    names = [r.name for r in report.results]
    assert "mirror_disk_headroom" in names
    # We didn't mock check_mirror_disk_headroom — it ran for real and
    # took the no-mirror short-circuit branch.
    mirror_row = next(r for r in report.results if r.name == "mirror_disk_headroom")
    assert mirror_row.level == "ok"
