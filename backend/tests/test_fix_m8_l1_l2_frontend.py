"""Regression tests for SECURITY #400 / GHSA-mj4g-hw3m-62rm — cluster M8.

M8: the nmap live-scan SSE endpoint used to authenticate ONLY via a
``?token=<jwt>`` query argument. Tokens in URLs leak through nginx /
proxy access logs, browser history, and the ``Referer`` header. The
fix switches the frontend consumer to ``fetch()`` + ``ReadableStream``
so the token rides the ``Authorization: Bearer`` header, and the
server (``app.api.v1.nmap.router._extract_stream_token``) now prefers
that header — keeping ``?token=`` only as a back-compat fallback.

These tests pin the resolver's precedence + failure behaviour so a
future refactor can't silently re-open the URL-token-only path.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, Request

from app.api.v1.nmap.router import _extract_stream_token


def _request(headers: dict[str, str] | None = None) -> Request:
    """Build a minimal Starlette Request carrying the given headers.

    ``_extract_stream_token`` only ever reads ``request.headers``, so a
    bare ASGI ``http`` scope with an encoded header list is enough — no
    app, DB, or event loop required.
    """
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/nmap/scans/x/stream",
        "headers": raw,
    }
    return Request(scope)


def test_authorization_header_is_preferred_over_query_token() -> None:
    # When both are present the header wins — the legacy URL token is
    # never the one that gets used if a header is supplied.
    req = _request({"Authorization": "Bearer header-token"})
    assert _extract_stream_token(req, "query-token") == "header-token"


def test_authorization_header_is_case_insensitive() -> None:
    # HTTP header names are case-insensitive; the scheme match must be too.
    req = _request({"authorization": "bearer mixed-case-token"})
    assert _extract_stream_token(req, None) == "mixed-case-token"


def test_query_token_is_accepted_as_back_compat_fallback() -> None:
    # No header → fall back to the query arg so older clients / bookmarks
    # keep working.
    req = _request({})
    assert _extract_stream_token(req, "query-token") == "query-token"


def test_blank_bearer_header_falls_through_to_query_token() -> None:
    # A header of literally ``Bearer `` (empty token) must not be taken as
    # a valid credential — fall through to the query arg.
    req = _request({"Authorization": "Bearer    "})
    assert _extract_stream_token(req, "query-token") == "query-token"


def test_non_bearer_authorization_scheme_is_ignored() -> None:
    # A Basic / other scheme isn't a bearer token; fall back to the query.
    req = _request({"Authorization": "Basic Zm9vOmJhcg=="})
    assert _extract_stream_token(req, "query-token") == "query-token"


def test_missing_both_header_and_query_raises_401() -> None:
    # No header AND no query token → 401, not a silent anonymous stream.
    req = _request({})
    with pytest.raises(HTTPException) as exc:
        _extract_stream_token(req, None)
    assert exc.value.status_code == 401


def test_empty_query_token_with_no_header_raises_401() -> None:
    # ``?token=`` with an empty value is not a credential.
    req = _request({})
    with pytest.raises(HTTPException) as exc:
        _extract_stream_token(req, "")
    assert exc.value.status_code == 401
