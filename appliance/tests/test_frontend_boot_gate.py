"""The SPA fallback must fall back to the initialising page (#767).

``frontend/public/_starting.html`` exists, and both nginx configs declare
``error_page 502 504 @starting`` — but ``location /`` resolves
``index.html`` straight off local disk, so it has no upstream to fail on
and that error_page could never fire for it. An operator browsing a
cold-booting appliance therefore got the SPA shell plus a wall of failing
XHRs, never the "SpatiumDDI is initialising" page. The fix gives
``location /`` an ``auth_request`` sub-request against the api Service.

There are TWO hand-maintained copies of this config — the image's own
template and the appliance's TLS-fronted ConfigMap — and nothing but
review keeps them in step. These guards fail if either one loses the
gate, or if they drift apart on the directives that make it work.

HOW TO RUN (from the repo root):
    python3 -m pytest appliance/tests/test_frontend_boot_gate.py -v

No appliance, no nginx, no cluster — this reads the two config files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
IMAGE_TEMPLATE = REPO / "frontend" / "default.conf.template"
APPLIANCE_TEMPLATE = REPO / "charts" / "spatiumddi" / "templates" / "frontend-tls-config.yaml"
STARTING_PAGE = REPO / "frontend" / "public" / "_starting.html"

CONFIGS = pytest.mark.parametrize(
    "path", [IMAGE_TEMPLATE, APPLIANCE_TEMPLATE], ids=["image-template", "appliance-tls-configmap"]
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _spa_fallback_block(text: str) -> str:
    """The body of the ``location /`` block (the SPA fallback).

    Matched by its ``try_files … /index.html`` marker so the far more
    numerous ``location /api/`` etc. blocks can't be picked up by
    accident.
    """
    for match in re.finditer(r"location\s+/\s*\{(.*?)\n\s*\}", text, re.S):
        if "/index.html" in match.group(1):
            return match.group(1)
    raise AssertionError("no SPA-fallback `location / { … /index.html }` block found")


def test_starting_page_is_shipped() -> None:
    """Vite copies frontend/public/ verbatim into the image's web root."""
    assert STARTING_PAGE.is_file()
    body = _text(STARTING_PAGE)
    # The page has to re-check on its own — nothing else clears it.
    assert 'http-equiv="refresh"' in body


@CONFIGS
def test_spa_fallback_is_gated_on_the_api(path: Path) -> None:
    """``location /`` must probe the api, or it can never 502."""
    block = _spa_fallback_block(_text(path))
    assert "auth_request /_api_up;" in block, (
        f"{path.name}: the SPA fallback serves index.html off disk, so without "
        "the auth_request probe it has no upstream to fail on and the "
        "initialising page is unreachable at `/`"
    )


@CONFIGS
def test_spa_fallback_error_page_covers_auth_request_500(path: Path) -> None:
    """auth_request collapses "upstream unreachable" into 500, not 502."""
    block = _spa_fallback_block(_text(path))
    match = re.search(r"error_page\s+([\d\s]+?)(?:=\d+\s+)?@starting;", block)
    assert match, f"{path.name}: SPA fallback has no error_page → @starting"
    codes = set(match.group(1).split())
    assert "500" in codes, (
        f"{path.name}: 500 missing — auth_request reports an unreachable "
        f"upstream as 500, so {sorted(codes)} would let bare nginx 500 HTML "
        "reach the operator instead of the initialising page"
    )


@CONFIGS
def test_probe_location_fails_fast(path: Path) -> None:
    """A wedged api or resolver must not stall every page load.

    ``resolver_timeout`` is the one that bites: it is NOT covered by the
    ``proxy_*`` timeouts and defaults to 30 s, so an unreachable DNS
    server would hang the browser on every navigation.
    """
    text = _text(path)
    match = re.search(r"location\s+=\s+/_api_up\s*\{(.*?)\n\s*\}", text, re.S)
    assert match, f"{path.name}: no `location = /_api_up` probe block"
    block = match.group(1)
    assert "internal;" in block, f"{path.name}: probe must not be externally callable"
    assert "/health/live" in block, f"{path.name}: probe must hit the api liveness route"
    for directive in ("resolver_timeout", "proxy_connect_timeout", "proxy_read_timeout"):
        assert re.search(rf"{directive}\s+\d+s;", block), f"{path.name}: {directive} unset"


def test_both_configs_serve_the_same_starting_file() -> None:
    """The two copies must not drift on which file they fall back to."""
    for path in (IMAGE_TEMPLATE, APPLIANCE_TEMPLATE):
        assert "try_files /_starting.html" in _text(path), path.name
