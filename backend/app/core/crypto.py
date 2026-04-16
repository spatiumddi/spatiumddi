"""Symmetric encryption for secrets stored at rest (LDAP bind passwords,
OIDC client secrets, SAML private keys).

Uses Fernet (AES-128-CBC + HMAC-SHA256). The key is derived from
``settings.credential_encryption_key`` when provided, otherwise from
``settings.secret_key`` via SHA-256. Changing either without first
re-encrypting existing rows will render stored secrets unreadable.
"""

from __future__ import annotations

import base64
import hashlib
import json
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    raw = settings.credential_encryption_key.strip()
    if raw:
        try:
            return Fernet(raw.encode())
        except (ValueError, Exception):
            # Explicit key is malformed — fall through to derived key so the
            # service still runs. Admins should set a valid Fernet key generated
            # via `Fernet.generate_key()`.
            pass
    digest = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_str(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt_str(token: bytes) -> str:
    try:
        return _fernet().decrypt(token).decode()
    except InvalidToken as exc:
        raise ValueError("encrypted value could not be decrypted") from exc


def encrypt_dict(data: dict) -> bytes:
    return encrypt_str(json.dumps(data, separators=(",", ":"), sort_keys=True))


def decrypt_dict(token: bytes) -> dict:
    return json.loads(decrypt_str(token))
