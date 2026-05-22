"""Helpers for safely capturing request metadata (client IP, user agent)
into audit-log + session rows.

The User-Agent header is fully attacker-controlled, so anything derived
from it that lands in a log or DB row must be sanitised first — otherwise
a crafted UA can inject control characters / newlines for log forging or
break downstream rendering (#9).
"""

from __future__ import annotations

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
    return request.client.host if request.client else None
