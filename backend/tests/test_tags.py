"""Unit tests for the shared tag-filter helpers (issue #104).

The DB-side ``apply_tag_filter`` is exercised end-to-end by the
integration tests against the real list endpoints (``test_ipam.py``
etc.) — it doesn't earn its own integration test here, but the pure
``parse_tag_param`` contract is the kind of thing future refactors
silently break, so it lives in a tight unit-test file of its own.
"""

from __future__ import annotations

import pytest

from app.services.tags import parse_tag_param


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Bare key — no value side at all.
        ("env", ("env", None)),
        # Key:value, the operator-typed form.
        ("env:prod", ("env", "prod")),
        # Surrounding whitespace is harmless.
        ("  env  ", ("env", None)),
        ("env: prod  ", ("env", "prod")),
        # Empty value after the colon collapses to "key only" — matches
        # the find_by_tag semantic where a missing value means
        # "any value", which is what an operator typing ``env:`` by
        # accident almost certainly meant.
        ("env:", ("env", None)),
        # Values containing colons (e.g. CIDR-shaped tags) are
        # preserved by partition()'s "first separator" rule.
        ("rfc1918:10.0.0.0/8", ("rfc1918", "10.0.0.0/8")),
        ("complex:a:b:c", ("complex", "a:b:c")),
    ],
)
def test_parse_tag_param_round_trips(raw: str, expected: tuple[str, str | None]) -> None:
    assert parse_tag_param(raw) == expected
