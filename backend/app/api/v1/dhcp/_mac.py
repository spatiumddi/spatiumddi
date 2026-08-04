"""Shared MAC-address canonicalization for the DHCP endpoints.

Accept the common operator-entered shapes and canonicalize to the
colon-separated lowercase form the DB stores. We reject anything that
doesn't parse to 12 hex chars rather than letting a malformed row reach
the agent and blow up its config.

Lifted verbatim out of ``mac_blocks.py``, which has always validated this
way, because ``statics.py`` did not: ``StaticCreate.mac_address`` was a
bare ``str``, the column is ``MACADDR``, and ``_conflict_check`` compares
against it — so a typo'd MAC reached Postgres and came back to the
operator as a 500 "Internal Server Error" in the reservation form.
"""

from __future__ import annotations

import re

_MAC_DELIMS = re.compile(r"[:\-.\s]")


def canonicalize_mac(raw: str) -> str:
    cleaned = _MAC_DELIMS.sub("", raw.strip()).lower()
    if len(cleaned) != 12 or not all(c in "0123456789abcdef" for c in cleaned):
        raise ValueError("mac_address must be 12 hex chars (any common separator allowed)")
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))
