"""ACME (DNS-01 provider) credential generation + verification.

Separate from ``app.core.security`` because the ACME protocol is its
own auth path — it doesn't intersect with JWTs or API tokens. The
credential shape mirrors acme-dns:

- ``username``: 40-char URL-safe random string (shown once, used as
  the ``X-Api-User`` header value).
- ``password``: 40-char URL-safe random string (shown once, used as
  the ``X-Api-Key`` header value).
- ``subdomain``: UUID4 (the DNS label the client CNAMEs to).

We store only the bcrypt hash of the password. The ``username`` is
stored in plaintext because it's the index we look up on auth — it
has enough entropy (~240 bits) that just having the username without
the password is useless for attack.

Why bcrypt, not scrypt? The rest of the auth codebase uses bcrypt
(`app.core.security.hash_password`). Both are adequate for a 40-char
random password — an attacker who steals the DB still has to brute-
force ~240 bits of entropy, which no KDF makes tractable.
"""

from __future__ import annotations

import secrets
import uuid

import bcrypt


def generate_acme_credentials() -> tuple[str, str, str]:
    """Return ``(username, password, subdomain)`` for a new ACME account.

    Called once at registration. The plaintext credentials are
    returned to the client in the response body and never persisted.
    """
    username = secrets.token_urlsafe(30)[:40]
    password = secrets.token_urlsafe(30)[:40]
    subdomain = str(uuid.uuid4())
    return username, password, subdomain


def hash_acme_password(password: str) -> str:
    """bcrypt-hash an ACME password for storage."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_acme_password(password: str, stored_hash: str) -> bool:
    """Constant-time verify. Returns False on any malformed hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


__all__ = [
    "generate_acme_credentials",
    "hash_acme_password",
    "verify_acme_password",
]
