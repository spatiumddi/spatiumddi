"""Operator re-confirmation for sensitive actions (#408).

Sensitive surfaces (secret reveals, and — later — destructive ops) re-confirm
the operator's identity right before proceeding. Historically each did a local
``verify_password`` and hard-rejected any ``auth_source != "local"`` account —
so an OIDC / SAML / LDAP / RADIUS / TACACS+ superadmin (who has no local
password) could never reveal an appliance kubeconfig, a pairing code, an agent
bootstrap key, or the SNMP community.

This helper unifies the re-confirmation:

* **Local users** (have a password): a correct password OR a correct TOTP code
  (when MFA is enrolled) passes — the password stays the primary, TOTP is an
  added option.
* **External-auth users** (no local password): a correct TOTP code passes. MFA
  enrolment is now open to every auth source (#408), so an SSO superadmin can
  enrol TOTP and then re-confirm with it. If they have NOT enrolled, the helper
  returns ``MFA_REQUIRED`` so the caller can tell them to enrol rather than
  dead-ending on a password they don't have.

The helper is intentionally stateless: it verifies a live TOTP code only (no
recovery-code consumption, which would mutate the user row) — recovery codes
are the lost-authenticator path for *login*, not per-action re-confirmation.
A single-use replay guard on the reveal TOTP (mirroring the login MFA
challenge) is a possible follow-up; the reveal endpoints are superadmin-gated
and audited, so a 30 s TOTP window is low-risk in the meantime.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from app.core.security import verify_password
from app.services.mfa import decrypt_secret, verify_totp

if TYPE_CHECKING:
    from app.models.auth import User


class ReauthOutcome(Enum):
    """Result of an operator re-confirmation attempt. Compared by identity
    (``is``) at the call sites, so plain ``Enum`` (no ``str`` mixin)."""

    OK = "ok"
    BAD_CREDENTIAL = "bad_credential"  # wrong password / wrong or missing TOTP
    MFA_REQUIRED = "mfa_required"  # external-auth user with no MFA enrolled


def _totp_ok(user: User, code: str | None) -> bool:
    """True iff ``code`` is a currently-valid TOTP for an MFA-enrolled user."""
    if not code or not user.totp_enabled or user.totp_secret_encrypted is None:
        return False
    try:
        return verify_totp(decrypt_secret(user.totp_secret_encrypted), code)
    except Exception:  # noqa: BLE001 — a decrypt/parse failure is just "no"
        return False


def reverify_operator(
    user: User,
    *,
    password: str | None = None,
    totp_code: str | None = None,
) -> ReauthOutcome:
    """Re-confirm ``user`` for a sensitive action. See module docstring.

    Never raises on a bad credential — returns an outcome so the caller keeps
    its own audit-on-denial + friction-sleep behaviour.
    """
    has_local_password = bool(user.auth_source == "local" and user.hashed_password)
    if has_local_password:
        assert user.hashed_password is not None  # narrowed by has_local_password
        if password and verify_password(password, user.hashed_password):
            return ReauthOutcome.OK
        if _totp_ok(user, totp_code):
            return ReauthOutcome.OK
        return ReauthOutcome.BAD_CREDENTIAL

    # External-auth (or a local account with no password set) — TOTP only.
    if not user.totp_enabled:
        return ReauthOutcome.MFA_REQUIRED
    if _totp_ok(user, totp_code):
        return ReauthOutcome.OK
    return ReauthOutcome.BAD_CREDENTIAL
