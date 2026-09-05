"""Every hostname in the backend is documented in docs/PRIVACY.md (#976).

SpatiumDDI's privacy statement claims something specific and checkable:
the software makes exactly one outbound connection nobody configured (a
daily anonymous release check against GitHub), and every other host it
can reach is listed with its default and its payload. A claim like that
rots the first time somebody adds a convenience fetch — the statement
does not become vague, it becomes *false*, which is worse than never
having made it.

So this is the guard, in the shape of ``lint_untyped_routes.py`` and
``test_response_media_types.py``: scan ``backend/app`` for hostname
literals and require each one to appear on the page. A new outbound host
fails CI until somebody writes down what it sends and when.

**Both lists live in PRIVACY.md, not here.** A host that is *not* a
connection — a documentation link, a homepage shown in the UI, an
``example.com`` placeholder — goes in that page's appendix rather than
into an allowlist in this file. That costs a line of prose and buys the
property that matters: filing a real endpoint under "not a connection"
is a lie a human has to type into the document readers actually read,
instead of a quiet entry in a test fixture.

**What the guard cannot see**, stated in PRIVACY.md §8 as well:

* hosts assembled at runtime from operator input (``https://{host}/…``)
  — which is the point, those are the operator's own endpoints;
* the feed catalogues under ``backend/app/data/*.json`` (blocklist
  sources, resolver presets), which are covered as a category by the
  blocklist row and are all opt-in downloads.
"""

from __future__ import annotations

import pathlib
import re

import pytest

# Spelled as an explicit repo-root escape (``parents[2]``) rather than
# ``backend/``.parent, because test_ci_backend_relevant.py scans for exactly
# that spelling to prove every cross-boundary read is declared in the CI
# must-run manifest — reaching the repo root by a route its regex cannot see
# would silently drop docs/PRIVACY.md out of the gate that runs this test.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_APP = _BACKEND / "app"
_PRIVACY = _REPO_ROOT / "docs" / "PRIVACY.md"

# The dev container copies only ``backend/`` into the image, so this skips
# there and runs for real in CI, which tests from a full checkout. Same
# convention as test_spatium_console.py and test_openapi_export.py.
pytestmark = pytest.mark.skipif(
    not _PRIVACY.exists(),
    reason="docs/PRIVACY.md not present in this checkout",
)

# The character class deliberately excludes ``{`` and ``$``, so an
# interpolated ``https://{host}/api`` yields no match at all rather than a
# bogus one.
_URL_RE = re.compile(r"https?://([A-Za-z0-9._-]+)")

# A match has to look like a real hostname before we demand it be
# documented. This drops the debris of scanning source text: ``https://...``
# in a docstring ellipsis, a bare ``https://localhost``, a loopback literal,
# and the truncated fragments left by a URL hard-wrapped across two lines.
_HOSTNAME_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}$")


def _hosts_in_source() -> dict[str, set[str]]:
    """Map each hostname literal under ``backend/app`` to the files using it."""
    found: dict[str, set[str]] = {}
    for path in sorted(_APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for host in _URL_RE.findall(text):
            host = host.lower().rstrip(".")
            if not _HOSTNAME_RE.match(host):
                continue
            found.setdefault(host, set()).add(str(path.relative_to(_BACKEND)))
    return found


def test_every_backend_hostname_is_in_the_privacy_statement() -> None:
    """A hostname the backend knows about is either a documented connection
    or a documented non-connection. There is no third category."""
    privacy = _PRIVACY.read_text(encoding="utf-8").lower()
    undocumented = {
        host: sorted(files) for host, files in _hosts_in_source().items() if host not in privacy
    }
    assert not undocumented, (
        "hostname(s) in backend/app that docs/PRIVACY.md does not mention: "
        + "; ".join(f"{h} ({', '.join(f)})" for h, f in sorted(undocumented.items()))
        + ". If it is an outbound connection, add a row to the table in §3 with its "
        "default and what it sends. If it is a documentation link or a placeholder, "
        "add it to the appendix. Do not add an allowlist to this test."
    )


def test_exactly_one_connection_is_enabled_by_default() -> None:
    """§3.1 stays a one-row table.

    The headline sentence — "no outbound connection you did not configure,
    with one exception" — is only true while this table has one row in it,
    and the README, the docs hero and the Settings copy all repeat it. Per
    CLAUDE.md non-negotiable #17 a second default-on connection needs an
    issue and a decision; this makes adding one a deliberate act rather
    than a table edit nobody noticed.
    """
    text = _PRIVACY.read_text(encoding="utf-8")
    section = text.split("### 3.1 Enabled by default", 1)
    assert len(section) == 2, "PRIVACY.md lost its '### 3.1 Enabled by default' heading"
    body = section[1].split("###", 1)[0]

    rows = [
        line
        for line in body.splitlines()
        if line.startswith("|") and not re.fullmatch(r"[|\s:-]+", line)
    ]
    # Header row + the single data row.
    assert len(rows) == 2, (
        f"§3.1 has {len(rows) - 1} default-on connection(s), expected 1. Adding one "
        "is a product decision (CLAUDE.md non-negotiable #17), and the README, the "
        "docs hero and Settings → Platform all state there is exactly one."
    )
    assert "api.github.com" in rows[1], "the one default-on row should be the release check"
