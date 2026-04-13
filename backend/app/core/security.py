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


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
