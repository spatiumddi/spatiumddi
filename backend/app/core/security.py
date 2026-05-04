"""JWT token issuance/validation and password hashing."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.config import settings

ALGORITHM = "HS256"


# ── Passwords ──────────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── JWT ────────────────────────────────────────────────────────────────────────


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    payload: dict[str, Any] = {"sub": subject, "exp": expire, "type": "access"}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def create_refresh_token(subject: str) -> tuple[str, str]:
    """Return (raw_token, hashed_token). Store only the hash."""
    raw = secrets.token_urlsafe(48)
    hashed = _hash_token(raw)
    return raw, hashed


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate an access JWT. Raises JWTError on failure."""
    payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    if payload.get("type") != "access":
        raise JWTError("Not an access token")
    return payload


# ── MFA challenge tokens (issue #69) ──────────────────────────────────────────
#
# Short-lived JWT minted by ``/auth/login`` when a user has TOTP enabled. Only
# valid as the ``mfa_token`` in ``/auth/login/mfa``. Carries ``type="mfa"`` so
# it can never be mistaken for an access token by the auth deps. 5 min TTL
# is enough for the user to fish their phone out and type the code without
# leaving a window large enough to phish-replay.

_MFA_TOKEN_TTL_MINUTES = 5


def create_mfa_challenge_token(user_id: str) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=_MFA_TOKEN_TTL_MINUTES)
    payload: dict[str, Any] = {"sub": user_id, "exp": expire, "type": "mfa"}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_mfa_challenge_token(token: str) -> dict[str, Any]:
    """Decode a challenge token. Raises JWTError on bad signature, expired,
    or wrong type — same error class the access path uses so the login router
    can collapse the failure modes."""
    payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    if payload.get("type") != "mfa":
        raise JWTError("Not an MFA challenge token")
    return payload


# ── API Tokens ─────────────────────────────────────────────────────────────────


def generate_api_token() -> tuple[str, str, str]:
    """
    Return (full_token, prefix, hash).
    full_token is shown to the user once and never stored.
    """
    prefix = "sddi_"
    raw = prefix + secrets.token_urlsafe(40)
    hashed = _hash_token(raw)
    return raw, prefix, hashed


def hash_api_token(raw: str) -> str:
    return _hash_token(raw)


def hash_refresh_token(raw: str) -> str:
    return _hash_token(raw)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
