"""Unit coverage for the security-hardening fixes from the Copilot review.

Pure / deterministic surfaces only — the Redis-backed throttles
(login_rate_limited, mfa_challenge_consume) fail open and are exercised
here only on their no-Redis / no-jti fallback paths.
"""

from __future__ import annotations

import pytest

from app.api.v1.users.router import CreateUserRequest
from app.config import Settings
from app.core.auth_throttle import mfa_challenge_consume
from app.core.request_meta import clean_user_agent


# ── #9 User-Agent sanitisation ──────────────────────────────────────────
def test_clean_user_agent_strips_control_chars() -> None:
    assert clean_user_agent("Mozilla/5.0\r\nInjected: evil") == "Mozilla/5.0Injected: evil"


def test_clean_user_agent_truncates() -> None:
    out = clean_user_agent("A" * 900)
    assert out is not None and len(out) == 500


def test_clean_user_agent_empty_is_none() -> None:
    assert clean_user_agent(None) is None
    assert clean_user_agent("   ") is None
    assert clean_user_agent("\x00\x01") is None


# ── #1 CORS origins parsing ─────────────────────────────────────────────
def test_cors_origins_default_wildcard() -> None:
    assert Settings(cors_origins="*").cors_origins_list == ["*"]
    assert Settings(cors_origins="").cors_origins_list == ["*"]


def test_cors_origins_explicit_list() -> None:
    s = Settings(cors_origins="https://a.example.com, https://b.example.com ,")
    assert s.cors_origins_list == ["https://a.example.com", "https://b.example.com"]


# ── #14 email validation ────────────────────────────────────────────────
@pytest.mark.parametrize("good", ["a@b.co", "ops.team@ddi.example.com"])
def test_create_user_accepts_valid_email(good: str) -> None:
    req = CreateUserRequest(username="u", email=good, display_name="U", password="longenough")
    assert req.email == good


@pytest.mark.parametrize("bad", ["not-an-email", "a@b", "@b.co", "a b@c.co", "a@@b.co"])
def test_create_user_rejects_bad_email(bad: str) -> None:
    with pytest.raises(ValueError):
        CreateUserRequest(username="u", email=bad, display_name="U", password="longenough")


# ── #7 MFA replay guard fail-open ───────────────────────────────────────
@pytest.mark.asyncio
async def test_mfa_consume_missing_jti_allows() -> None:
    # A legacy challenge token without a jti can't be tracked — allow it
    # (preserves the prior stateless behaviour rather than locking out).
    assert await mfa_challenge_consume(None) is True
    assert await mfa_challenge_consume("") is True
