"""#862 — the agent long-poll's ETag was not a valid entity-tag.

``GET /api/v1/{dns,dhcp,looking-glass}/agents/config`` is a conditional
long-poll: the server hands back an ETag, the agent echoes it as
``If-None-Match``, and the request parks on a held 304. That contract ran on
a bare ``sha256:<hex>`` — unquoted, so malformed per RFC 9110 §8.8.3, and
*strong*, so nginx's gzip filter deleted it outright rather than lie about a
body it had just transformed.

Both nginx configs gzip ``application/json``, so any client sending
``Accept-Encoding: gzip`` (python-requests does by default) got a 200 with
no ETag and nothing to condition the next poll on. Reproduced live on the
compose stack before the fix — identity → header present, gzip → header
absent — and re-run after it with a weak tag, which nginx preserves.

The bare value stays internal; only the wire form changed. These tests pin
the two halves that makes safe: a valid weak tag out, and every spelling
accepted back in — so an agent still holding the old bare value matches on
its first poll after upgrade instead of re-downloading a full bundle.
"""

from __future__ import annotations

import pytest

from app.core.http_etag import etag_matches, format_etag

_V = "sha256:200dc207ac9b0d1e931172a7e7d62271425ed33e450606ef5aa73b5483ca817e"


# ── Emission ────────────────────────────────────────────────────────────────


def test_formats_as_a_weak_quoted_entity_tag() -> None:
    """Quoted because RFC 9110 requires it; weak because nginx drops a strong
    validator through gzip, and because gzipped and identity really are
    different representations of the same payload."""
    assert format_etag(_V) == f'W/"{_V}"'


def test_format_is_idempotent() -> None:
    """A double-format must not produce ``W/"W/"…""`` — the 304 path and the
    200 path both format, and a future refactor could route one through the
    other."""
    assert format_etag(format_etag(_V)) == f'W/"{_V}"'


# ── Matching ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("header", "why"),
    [
        (f'W/"{_V}"', "what a conformant client echoes back"),
        (f'"{_V}"', "strong-quoted, e.g. an intermediary that re-tagged it"),
        (_V, "THE upgrade case: an agent still holding the old bare value"),
        (f'  W/"{_V}"  ', "surrounding whitespace is legal"),
        (f'W/"other", W/"{_V}"', "comma-separated list, ours second"),
        ("*", "means 'if any representation exists' per spec"),
    ],
)
def test_every_spelling_of_the_current_tag_matches(header: str, why: str) -> None:
    assert etag_matches(header, _V), why


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "   ",
        'W/"sha256:deadbeef"',
        'W/"other", "another"',
        # A prefix must not match: truncation is exactly how a broken
        # intermediary would corrupt this, and answering 304 to a truncated
        # tag would park an agent on a bundle it never received.
        f'W/"{_V[:20]}"',
    ],
)
def test_non_matching_headers_do_not_match(header: str | None) -> None:
    assert not etag_matches(header, _V)


def test_round_trip_through_the_wire_form() -> None:
    """The property that actually matters: what we emit, we accept."""
    assert etag_matches(format_etag(_V), _V)


def test_bare_round_trip_still_works() -> None:
    """The pre-#862 wire form must keep matching, or every agent in the fleet
    re-downloads a full bundle on the first poll after an upgrade."""
    assert etag_matches(_V, _V)
