"""Shared AS-path regex query helper for the Looking Glass RIB (#566 Phase 4).

Used by both ``GET /looking-glass/routes?as_path_regexp=`` (see
``app.api.v1.looking_glass.router.list_routes``) and the ``find_bgp_routes``
Operator Copilot tool (``app.services.ai.tools.bgp_lg``) so the Cisco/
Juniper ``_`` boundary-token translation lives in exactly one place.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Text, cast, func

from app.models.bgp_looking_glass import BGPLGRoute


def translate_as_path_regexp(raw: str) -> str:
    """Translate ``_`` boundary tokens (Cisco/Juniper AS-path regexp
    convention) into a Postgres POSIX ERE fragment matching against the
    space-delimited, space-bounded rendering of ``as_path`` (see
    :func:`as_path_regexp_clause`).

    * A leading ``_`` -> ``(^|[[:space:]])``
    * A trailing ``_`` -> ``([[:space:]]|$)``
    * Any other ``_`` -> ``[[:space:]]``

    Everything else in ``raw`` passes through untouched — operators may use
    plain POSIX ERE (Postgres's regex engine, not PCRE) alongside ``_``.
    """
    n = len(raw)
    out: list[str] = []
    for i, ch in enumerate(raw):
        if ch != "_":
            out.append(ch)
        elif i == 0:
            out.append("(^|[[:space:]])")
        elif i == n - 1:
            out.append("([[:space:]]|$)")
        else:
            out.append("[[:space:]]")
    return "".join(out)


def as_path_regexp_clause(pattern: str) -> Any:
    """Build the WHERE clause for an AS-path regexp match against
    ``BGPLGRoute.as_path``.

    Raises ``re.error`` if the translated pattern doesn't compile under
    Python's ``re`` — a friendly pre-check (the grammars aren't perfectly
    equivalent, but this catches the common unbalanced paren/bracket
    mistake before it reaches Postgres as a raw asyncpg error). Callers
    should catch ``re.error`` and turn it into a 422 / tool-note.
    """
    translated = translate_as_path_regexp(pattern)
    re.compile(translated)  # raises re.error on malformed input
    # Render as_path (JSONB "[64500, 65001]") to a clean space-delimited string
    # "64500 65001": strip the brackets/commas to spaces, collapse the runs
    # (``, `` -> two spaces, plus the bracket spaces) and trim the ends. Without
    # the collapse+trim the leading "[" and trailing "]" leave boundary spaces
    # that break ``^``/``$`` (origin/tail) anchoring — a pattern like ``_65001$``
    # would never match because the text ends in a space, not the last ASN.
    path_text = func.trim(
        func.regexp_replace(
            func.translate(cast(BGPLGRoute.as_path, Text), "[],", "   "),
            "[[:space:]]+",
            " ",
            "g",
        )
    )
    return path_text.op("~")(translated)


__all__ = ["as_path_regexp_clause", "translate_as_path_regexp"]
