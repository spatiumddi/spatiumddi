"""Helpers for safely capturing request metadata (client IP, user agent)
into audit-log + session rows.

The User-Agent header is fully attacker-controlled, so anything derived
from it that lands in a log or DB row must be sanitised first — otherwise
a crafted UA can inject control characters / newlines for log forging or
break downstream rendering (#9).
"""

from __future__ import annotations

import ipaddress
import re

from fastapi import Request

# Strip C0 controls + DEL. Newlines/tabs in particular enable log forging
# when the value is later written to a text log.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Matches the user_agent / audit columns (String(500)). Truncate well
# inside that so a multi-byte tail can't overflow the column.
_MAX_USER_AGENT_LEN = 500


def clean_user_agent(raw: str | None) -> str | None:
    """Strip control characters and clamp the User-Agent to the column
    width. Returns ``None`` for missing / empty-after-cleaning input."""
    if not raw:
        return None
    cleaned = _CONTROL_CHARS.sub("", raw).strip()
    return cleaned[:_MAX_USER_AGENT_LEN] or None


def client_ip(request: Request) -> str | None:
    """The raw peer address as uvicorn resolved it.

    ⚠️ Spoofable for security decisions on the shipped nginx topology: the
    API runs uvicorn with ``--proxy-headers --forwarded-allow-ips *``, so
    ``request.client.host`` is derived from the client-supplied
    ``X-Forwarded-For`` chain and can be forged (#626). Fine for coarse
    observability; use :func:`get_trusted_client_ip` for anything that
    gates access (rate limits, source-IP allowlists, provenance).
    """
    return request.client.host if request.client else None


def get_trusted_client_ip(request: Request) -> str | None:
    """The client source IP to trust for security decisions.

    The shipped nginx configs set ``X-Real-IP: $remote_addr`` as an
    *overwrite* the client cannot influence (any client-supplied
    ``X-Real-IP`` is discarded by ``proxy_set_header``), whereas uvicorn's
    ``--forwarded-allow-ips *`` makes ``request.client.host`` follow the
    attacker-controlled ``X-Forwarded-For`` chain (#626). So prefer
    ``X-Real-IP`` and fall back to the peer address only for
    direct-to-uvicorn (no-nginx) deployments — where there is no untrusted
    proxy in front and thus nothing to spoof past. A present-but-malformed
    ``X-Real-IP`` (not a valid IP) also falls back rather than poisoning a
    downstream allowlist / rate-limit key.
    """
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        candidate = real_ip.strip()
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass  # malformed header — fall through to the peer address
    return request.client.host if request.client else None
