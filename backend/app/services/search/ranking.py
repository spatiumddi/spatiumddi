"""Relevance scoring for global search (issue #879).

Search v1 concatenated per-type result lists in a fixed order and cut the
combined list at ``limit``. Two things went wrong with that:

* **Order.** A weak substring hit on an IP *space* name outranked an exact
  ``dns_record`` FQDN match purely because spaces were queried earlier.
* **Truncation.** Each type ran ``LIMIT n`` with no ``ORDER BY``, so on a
  type with thousands of substring matches the exact hit was frequently not
  in the rows the database chose to return at all. No amount of re-sorting
  in Python can recover a row that was never fetched — which is why the
  ranking has to exist in SQL, before the limit, and not only here.

So there are two halves, and they must agree:

* :func:`sql_rank` builds the ``ORDER BY`` expression the query uses to pick
  *which* rows come back.
* :func:`pick_match` re-derives the same quality bucket in Python to decide
  *which field* matched, for the ``matched_field`` hint the UI shows.

The buckets are deliberately far apart (100 / 60 / 25) relative to the type
weights (0–12), so quality always dominates type: an exact match on a
lowly-weighted type still beats a substring match on a favoured one. That
ordering property is what the issue asked for, and it is pinned by a test.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import case, func

__all__ = [
    "EXACT",
    "PREFIX",
    "SUBSTRING",
    "TYPE_WEIGHTS",
    "escape_like",
    "like_pattern",
    "match_quality",
    "pick_match",
    "sql_rank",
    "total_score",
]

# Match-quality buckets. Gaps are wide on purpose — see module docstring.
EXACT = 100
PREFIX = 60
SUBSTRING = 25

# Per-type nudge applied on top of the quality bucket, used only to break
# ties *within* a bucket. Ordered by how specific a hit of that type usually
# is: an operator who types a string that exactly matches a DNS record wanted
# that record; one whose string happens to appear in a space description
# almost never did.
TYPE_WEIGHTS: dict[str, int] = {
    "ip_address": 12,
    "dns_record": 11,
    "dhcp_reservation": 10,
    "subnet": 9,
    "dns_zone": 9,
    "dhcp_scope": 8,
    "block": 7,
    "vlan": 7,
    "device": 6,
    "dns_view": 5,
    "dns_blocklist": 5,
    "space": 4,
    "dns_group": 4,
    "dns_server": 4,
    "dhcp_server": 4,
    "circuit": 3,
    "site": 3,
    "user": 2,
    "group": 2,
    "appliance": 2,
}


def escape_like(value: str) -> str:
    r"""Escape ``LIKE`` metacharacters so a query is matched literally.

    Search v1 interpolated the raw query into ``%{q}%``, so an operator
    searching for the string ``50%`` matched every row in the table and one
    typing ``a_b`` matched ``axb``. Both read as "search is broken" rather
    than as a wildcard feature nobody documented.

    The backslash is escaped first, or escaping the others would then be
    re-escaped by this function's own output.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_pattern(value: str, *, prefix: bool = False) -> str:
    """``%value%`` (or ``value%``) with metacharacters neutralised."""
    escaped = escape_like(value)
    return f"{escaped}%" if prefix else f"%{escaped}%"


def sql_rank(q: str, *columns: Any) -> Any:
    """An integer relevance expression over ``columns``, for ``ORDER BY``.

    Every column contributes its own bucket and the row takes the best one,
    so a row whose *name* matches exactly is not dragged down by a
    *description* that merely contains the term.

    NULL columns score 0 rather than NULL: ``CASE`` falls through to
    ``ELSE`` when its conditions are NULL, which keeps ``GREATEST`` from
    having to reason about NULLs at all.
    """
    lowered = q.lower()
    branches = [
        case(
            (func.lower(col) == lowered, EXACT),
            (func.lower(col).like(like_pattern(lowered, prefix=True), escape="\\"), PREFIX),
            (func.lower(col).like(like_pattern(lowered), escape="\\"), SUBSTRING),
            else_=0,
        )
        for col in columns
    ]
    if len(branches) == 1:
        return branches[0]
    return func.greatest(*branches)


def match_quality(q_lower: str, value: str | None) -> int:
    """Python twin of one :func:`sql_rank` branch."""
    if not value:
        return 0
    low = value.lower()
    if low == q_lower:
        return EXACT
    if low.startswith(q_lower):
        return PREFIX
    if q_lower in low:
        return SUBSTRING
    return 0


def pick_match(q: str, candidates: Sequence[tuple[str, str | None]]) -> tuple[int, str | None]:
    """Best (quality, field-name) over ``(field_name, value)`` pairs.

    Ties keep the earliest candidate, so callers order the sequence by how
    much they want that field named in the UI — ``hostname`` before
    ``description``, say.
    """
    best_score = 0
    best_field: str | None = None
    for field_name, value in candidates:
        score = match_quality(q.lower(), value)
        if score > best_score:
            best_score, best_field = score, field_name
    return best_score, best_field


def total_score(quality: int, result_type: str) -> int:
    """Combine a quality bucket with its type weight."""
    return quality + TYPE_WEIGHTS.get(result_type, 0)
