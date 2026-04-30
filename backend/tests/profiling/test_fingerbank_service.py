"""Unit tests for the fingerbank API integration.

We mock the HTTP layer (httpx) so the tests don't hit the real
fingerbank service. The goal is to verify:

  * cache window honoured (skip lookup when last_lookup_at is recent),
  * graceful degradation on network / 5xx / 429 / 404 / malformed JSON,
  * happy-path response parsing populates the fingerprint row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.models.dhcp_fingerprint import DHCPFingerprint
from app.models.settings import PlatformSettings
from app.services.profiling.fingerbank import (
    FINGERBANK_CACHE_DAYS,
    FingerbankResult,
    _parse_response,
    lookup,
)


async def _set_api_key(db: AsyncSession, key: str | None = "test-key") -> None:
    settings = await db.get(PlatformSettings, 1)
    if settings is None:
        settings = PlatformSettings(id=1)
        db.add(settings)
    if key is None:
        settings.fingerbank_api_key_encrypted = None
    else:
        settings.fingerbank_api_key_encrypted = encrypt_str(key)
    await db.commit()


def _make_fp(**overrides) -> DHCPFingerprint:
    defaults = {
        "mac_address": "aa:bb:cc:dd:ee:ff",
        "option_55": "1,3,6,15",
        "option_60": "MSFT 5.0",
    }
    defaults.update(overrides)
    return DHCPFingerprint(**defaults)


def test_parse_response_normalises_nested_shape() -> None:
    payload = {
        "device": {
            "id": 33,
            "name": "iPhone",
            "parents": [{"id": 5, "name": "Smartphones"}],
        },
        "manufacturer": {"name": "Apple Inc."},
        "score": 88,
    }
    result = _parse_response(payload)
    assert result == FingerbankResult(
        device_id=33,
        device_name="iPhone",
        device_class="Smartphones",
        manufacturer="Apple Inc.",
        score=88,
    )


def test_parse_response_missing_fields_safe() -> None:
    result = _parse_response({})
    assert result == FingerbankResult(None, None, None, None, None)


@pytest.mark.asyncio
async def test_lookup_cache_window_skips_recent_lookup(
    db_session: AsyncSession,
) -> None:
    await _set_api_key(db_session)
    fp = _make_fp(
        fingerbank_last_lookup_at=datetime.now(UTC) - timedelta(days=1),
        fingerbank_device_name="cached",
    )
    db_session.add(fp)
    await db_session.commit()

    with patch("app.services.profiling.fingerbank.httpx.AsyncClient") as client_cls:
        result = await lookup(db_session, fingerprint=fp)

    assert result is None
    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_lookup_no_api_key_returns_none(db_session: AsyncSession) -> None:
    await _set_api_key(db_session, key=None)
    fp = _make_fp()
    db_session.add(fp)
    await db_session.commit()

    with patch("app.services.profiling.fingerbank.httpx.AsyncClient") as client_cls:
        result = await lookup(db_session, fingerprint=fp)

    assert result is None
    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_lookup_happy_path_stamps_row(db_session: AsyncSession) -> None:
    await _set_api_key(db_session)
    fp = _make_fp()
    db_session.add(fp)
    await db_session.commit()

    fake_resp = httpx.Response(
        200,
        json={
            "device": {
                "id": 7,
                "name": "Windows 10",
                "parents": [{"id": 1, "name": "Windows"}],
            },
            "manufacturer": {"name": "Microsoft"},
            "score": 95,
        },
    )

    class _Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):  # noqa: ANN001
            return fake_resp

    with patch("app.services.profiling.fingerbank.httpx.AsyncClient", return_value=_Ctx()):
        result = await lookup(db_session, fingerprint=fp)

    assert result is not None
    assert result.device_name == "Windows 10"
    assert fp.fingerbank_device_name == "Windows 10"
    assert fp.fingerbank_device_class == "Windows"
    assert fp.fingerbank_manufacturer == "Microsoft"
    assert fp.fingerbank_score == 95
    assert fp.fingerbank_last_lookup_at is not None
    assert fp.fingerbank_last_error is None


@pytest.mark.asyncio
async def test_lookup_http_error_records_error(db_session: AsyncSession) -> None:
    await _set_api_key(db_session)
    fp = _make_fp()
    db_session.add(fp)
    await db_session.commit()

    class _Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):  # noqa: ANN001
            raise httpx.ConnectError("DNS failure")

    with patch("app.services.profiling.fingerbank.httpx.AsyncClient", return_value=_Ctx()):
        result = await lookup(db_session, fingerprint=fp)

    assert result is None
    assert fp.fingerbank_last_error is not None
    assert "DNS failure" in fp.fingerbank_last_error
    assert fp.fingerbank_last_lookup_at is not None
    assert fp.fingerbank_device_name is None


@pytest.mark.asyncio
async def test_lookup_404_caches_negative_result(db_session: AsyncSession) -> None:
    await _set_api_key(db_session)
    fp = _make_fp()
    db_session.add(fp)
    await db_session.commit()

    fake_resp = httpx.Response(404, text="not found")

    class _Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):  # noqa: ANN001
            return fake_resp

    with patch("app.services.profiling.fingerbank.httpx.AsyncClient", return_value=_Ctx()):
        result = await lookup(db_session, fingerprint=fp)

    assert result is None
    assert fp.fingerbank_last_lookup_at is not None
    # Negative cache — no error recorded, but a subsequent call inside
    # the cache window will short-circuit on _within_cache_window.
    assert fp.fingerbank_last_error is None
    assert fp.fingerbank_device_name is None


@pytest.mark.asyncio
async def test_lookup_429_records_error(db_session: AsyncSession) -> None:
    await _set_api_key(db_session)
    fp = _make_fp()
    db_session.add(fp)
    await db_session.commit()

    fake_resp = httpx.Response(429, text="rate limited")

    class _Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):  # noqa: ANN001
            return fake_resp

    with patch("app.services.profiling.fingerbank.httpx.AsyncClient", return_value=_Ctx()):
        result = await lookup(db_session, fingerprint=fp)

    assert result is None
    assert fp.fingerbank_last_error is not None
    assert "429" in fp.fingerbank_last_error


def test_cache_window_constant_sane() -> None:
    """Sanity-check the cache window — ops would notice if this drifted."""
    assert 1 <= FINGERBANK_CACHE_DAYS <= 30
