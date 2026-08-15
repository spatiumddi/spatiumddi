"""Regression: ``GET /api/v1/admin/postgres/schema-health`` must never 500.

#565 moved the cached alembic-head reader from ``app.api.health``
(private ``_expected_alembic_head``) to ``app.core.schema_check``
(public ``expected_alembic_head``), but the schema-health panel's lazy
import kept the old path — so from that commit on, the endpoint raised
``ImportError`` and answered 500 on every call, on every appliance
(observed live: 13/13 conformance runs red on three consecutive QA
builds). The endpoint's own contract (its docstring and error handling)
is that failures are *reported in the body*, never raised.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User


async def _make_superadmin(db: AsyncSession) -> str:
    user = User(
        username=f"sh-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Schema-Health Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


@pytest.mark.asyncio
async def test_schema_health_answers_200_not_500(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The panel answers 200 with a well-formed verdict body.

    Whether the test DB's ``alembic_version`` exists or not, every
    failure mode is a *reported* status — the pre-fix ImportError was
    the only way this endpoint could 500, and this pins it closed.
    """
    token = await _make_superadmin(db_session)
    resp = await client.get(
        "/api/v1/admin/postgres/schema-health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] in ("ok", "behind", "error")
    assert body["detail"]


def test_verify_password_is_total() -> None:
    """A candidate password can only ever be WRONG, never an exception.

    bcrypt.checkpw raises ValueError on >72-byte input and on NUL bytes;
    request schemas allow up to 256 chars, so every login-shaped endpoint
    500'd on those inputs instead of answering 401/403.
    """
    from app.core.security import hash_password, verify_password

    hashed = hash_password("correct horse")
    assert verify_password("correct horse", hashed) is True
    assert verify_password("x" * 100, hashed) is False
    assert verify_password("nul\x00byte", hashed) is False
    assert verify_password("correct horse", "not-a-bcrypt-hash") is False


def test_hash_password_is_total_too() -> None:
    """Review catch: verify_password became total but its sibling did not.

    bcrypt.hashpw raises above 72 bytes exactly as checkpw does, and nothing
    bounded the input — so create-user / reset-password / change-password
    still 500'd on the very input class the verify fix was meant to close.
    Both halves must agree on the boundary or a long password would hash
    fine and then never verify.
    """
    from app.core.security import hash_password, verify_password

    long_pw = "x" * 200
    hashed = hash_password(long_pw)  # must not raise
    assert verify_password(long_pw, hashed) is True
    # and the truncation is consistent: the first 72 bytes decide identity
    assert verify_password("x" * 72, hashed) is True


def test_password_policy_rejects_over_length_in_bytes() -> None:
    """The API layer must answer before the hasher does — and in BYTES,
    because that is what bcrypt measures. A 40-character password of 2-byte
    characters is over the line while a 71-character ASCII one is not."""
    from app.services.password_policy import PasswordPolicy, validate

    policy = PasswordPolicy(
        min_length=8,
        require_uppercase=False,
        require_lowercase=False,
        require_digit=False,
        require_symbol=False,
        history_count=0,
        max_age_days=0,
    )
    assert validate("x" * 71, policy).ok is True
    over_ascii = validate("x" * 73, policy)
    assert over_ascii.ok is False
    assert any("72 bytes" in e for e in over_ascii.errors)
    # 40 two-byte characters = 80 bytes: rejected despite being 40 chars
    over_utf8 = validate("é" * 40, policy)
    assert over_utf8.ok is False
