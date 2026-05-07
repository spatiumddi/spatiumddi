"""Sanitized error classifier for BGP upstream failures.

Both :mod:`app.services.bgp.ripestat` and
:mod:`app.services.bgp.peeringdb` return a soft
``{"available": False, "error": "..."}`` shape on failure. The raw
``str(exc)`` from ``httpx`` leaks internal context (full URLs in
``HTTPStatusError``, system hostnames in connect errors, …) to the
HTTP response, which CodeQL's ``py/stack-trace-exposure`` flags.

This helper maps the exception to a small, fixed vocabulary so the
client sees a stable category and the leaked detail stays in the
server-side structured log.
"""

from __future__ import annotations

import httpx


def classify_http_error(exc: Exception) -> str:
    """Map an upstream-fetch exception to a categorical reason.

    The vocabulary is intentionally small: clients render it in a
    "${source} unavailable: ${reason}" banner, not for branching.
    """
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return "rate_limited"
        if 500 <= status < 600:
            return "upstream_error"
        if 400 <= status < 500:
            return "upstream_rejected"
        return "upstream_error"
    if isinstance(exc, httpx.ConnectError):
        return "connect_failed"
    if isinstance(exc, httpx.HTTPError):
        return "network_error"
    return "unavailable"
