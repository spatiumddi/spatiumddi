"""AS-path regexp query helper tests — issue #566 Phase 4.

Pure-function coverage for the Cisco/Juniper ``_`` boundary-token
translation (``translate_as_path_regexp``) plus a sanity check that
``as_path_regexp_clause`` rejects a malformed pattern with ``re.error``
(the router / MCP tool both catch this and turn it into a friendly
422 / tool-note rather than a raw asyncpg error).
"""

from __future__ import annotations

import re

import pytest

from app.services.looking_glass.as_path_query import (
    as_path_regexp_clause,
    translate_as_path_regexp,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # No underscore at all — passes through untouched.
        ("65001", "65001"),
        ("^65001$", "^65001$"),
        # Leading underscore only.
        ("_65001", "(^|[[:space:]])65001"),
        # Trailing underscore only.
        ("65001_", "65001([[:space:]]|$)"),
        # Both ends — the canonical "contains AS 65001 anywhere" pattern.
        ("_65001_", "(^|[[:space:]])65001([[:space:]]|$)"),
        # Internal underscore between two AS numbers.
        ("65001_65002", "65001[[:space:]]65002"),
        # Leading + internal + trailing all in one pattern.
        ("_65001_65002_", "(^|[[:space:]])65001[[:space:]]65002([[:space:]]|$)"),
    ],
)
def test_translate_as_path_regexp(raw: str, expected: str) -> None:
    assert translate_as_path_regexp(raw) == expected


def test_as_path_regexp_clause_builds_for_valid_pattern() -> None:
    # Should not raise — a well-formed pattern compiles fine under re.
    clause = as_path_regexp_clause("_65001_")
    assert clause is not None


def test_as_path_regexp_clause_raises_re_error_on_malformed_pattern() -> None:
    # Unbalanced parenthesis — the common operator typo this pre-check
    # is meant to catch before it reaches Postgres as a raw asyncpg error.
    with pytest.raises(re.error):
        as_path_regexp_clause("(unbalanced")
