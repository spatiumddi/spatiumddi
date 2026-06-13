"""#408 — SSO operators can re-confirm sensitive reveals via TOTP.

Historically the reveal endpoints (agent keys, SNMP community, appliance
kubeconfig, pairing codes) hard-rejected any ``auth_source != "local"``
account because they re-verified a *local password* an SSO user doesn't
have. #408 unifies re-confirmation through ``reverify_operator``: local
users use a password (or TOTP if enrolled), external-auth users use TOTP
— and MFA enrolment is now open to every auth source.

Covers:

* The ``reverify_operator`` contract (unit).
* MFA enrolment open to an external-auth (OIDC) user (integration).
* An SSO superadmin revealing agent keys with a TOTP code (integration),
  and the no-MFA dead-end returning MFA_REQUIRED.
"""

from __future__ import annotations

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.services.mfa import encrypt_secret, generate_secret
from app.services.reauth import ReauthOutcome, reverify_operator

# ── reverify_operator unit contract ────────────────────────────────


def _user(
    *,
    auth_source: str = "local",
    password: str | None = None,
    totp_secret: str | None = None,
) -> User:
    u = User(
        username="u",
        email="u@example.com",
        display_name="u",
        auth_source=auth_source,
        hashed_password=hash_password(password) if password else None,
    )
    if totp_secret is not None:
        u.totp_enabled = True
        u.totp_secret_encrypted = encrypt_secret(totp_secret)
    return u


def test_local_password_ok() -> None:
    assert reverify_operator(_user(password="pw"), password="pw") is ReauthOutcome.OK


def test_local_wrong_password_is_bad_credential() -> None:
    out = reverify_operator(_user(password="pw"), password="nope")
    assert out is ReauthOutcome.BAD_CREDENTIAL


def test_local_user_may_use_totp_when_enrolled() -> None:
    secret = generate_secret()
    u = _user(password="pw", totp_secret=secret)
    out = reverify_operator(u, totp_code=pyotp.TOTP(secret).now())
    assert out is ReauthOutcome.OK


def test_sso_user_with_totp_ok() -> None:
    secret = generate_secret()
    u = _user(auth_source="oidc", totp_secret=secret)
    out = reverify_operator(u, totp_code=pyotp.TOTP(secret).now())
    assert out is ReauthOutcome.OK


def test_sso_user_without_mfa_requires_enrolment() -> None:
    # Even with a password supplied, an SSO account has no local password
    # to check against — the helper steers them to enrol MFA.
    out = reverify_operator(_user(auth_source="oidc"), password="anything")
    assert out is ReauthOutcome.MFA_REQUIRED


def test_sso_user_wrong_totp_is_bad_credential() -> None:
    secret = generate_secret()
    u = _user(auth_source="saml", totp_secret=secret)
    assert reverify_operator(u, totp_code="000000") is ReauthOutcome.BAD_CREDENTIAL


# ── integration: SSO enrol MFA → reveal agent keys with TOTP ────────


async def _sso_superadmin(db: AsyncSession, username: str = "ssoadmin") -> tuple[User, str]:
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username,
        hashed_password=None,  # external-auth — no local password
        auth_source="oidc",
        is_superadmin=True,
    )
    user.groups = []
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


@pytest.mark.asyncio
async def test_sso_user_can_enrol_mfa_and_reveal(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """#408 end-to-end: an OIDC superadmin enrols TOTP (was local-only) and
    then reveals the agent bootstrap keys with a TOTP code."""
    _, token = await _sso_superadmin(db_session)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    # Enrolment is now open to external-auth users.
    begin = await client.post("/api/v1/auth/mfa/enroll/begin", headers=headers)
    assert begin.status_code == 200, begin.text
    secret = begin.json()["secret"]
    verify = await client.post(
        "/api/v1/auth/mfa/enroll/verify",
        headers=headers,
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert verify.status_code == 204, verify.text

    # Reveal with a current TOTP code → accepted.
    reveal = await client.post(
        "/api/v1/admin/agent-keys/reveal",
        headers=headers,
        json={"totp_code": pyotp.TOTP(secret).now()},
    )
    assert reveal.status_code == 200, reveal.text

    # A wrong TOTP is rejected (the gate still requires a real credential).
    bad = await client.post(
        "/api/v1/admin/agent-keys/reveal",
        headers=headers,
        json={"totp_code": "000000"},
    )
    assert bad.status_code == 403, bad.text


@pytest.mark.asyncio
async def test_sso_user_without_mfa_is_told_to_enrol(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """#408 — an SSO superadmin who hasn't enrolled MFA gets a clear
    'enrol MFA' 403 instead of a dead-end password rejection."""
    _, token = await _sso_superadmin(db_session, username="ssonomfa")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/admin/agent-keys/reveal",
        headers=headers,
        json={"password": "irrelevant"},
    )
    assert resp.status_code == 403, resp.text
    assert "mfa" in resp.json()["detail"].lower()
