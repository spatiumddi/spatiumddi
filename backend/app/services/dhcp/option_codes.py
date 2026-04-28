"""DHCP option-code library.

Loads ``app/data/dhcp_option_codes.json`` once per process and exposes
search + lookup helpers. The catalog is static — RFC 2132 + IANA
``bootp-dhcp-parameters`` entries that operators actually configure —
so we keep it as JSON shipped with the app rather than a DB table.

Used by:
  - ``GET /api/v1/dhcp/option-codes`` — frontend autocomplete on the
    custom-options row of ``DHCPOptionsEditor``.
  - Future fingerprinting / template authoring helpers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "dhcp_option_codes.json"


@dataclass(frozen=True)
class DHCPOptionCodeDef:
    code: int
    name: str
    kind: str
    description: str
    rfc: str | None


@lru_cache(maxsize=1)
def load_catalog() -> list[DHCPOptionCodeDef]:
    raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    out: list[DHCPOptionCodeDef] = []
    for o in raw.get("options", []):
        out.append(
            DHCPOptionCodeDef(
                code=int(o["code"]),
                name=str(o["name"]),
                kind=str(o.get("kind", "binary")),
                description=str(o.get("description", "")),
                rfc=o.get("rfc"),
            )
        )
    out.sort(key=lambda d: d.code)
    return out


def search(q: str | None = None) -> list[DHCPOptionCodeDef]:
    """Substring search against code (numeric prefix) + name + description.

    Empty / None ``q`` returns the full catalog. Matches are case-
    insensitive on name + description; numeric ``q`` matches when
    ``str(code)`` starts with the digits.
    """
    catalog = load_catalog()
    if not q:
        return list(catalog)
    needle = q.strip().lower()
    if not needle:
        return list(catalog)
    out: list[DHCPOptionCodeDef] = []
    for d in catalog:
        if needle.isdigit():
            if str(d.code).startswith(needle):
                out.append(d)
                continue
        if needle in d.name.lower() or needle in d.description.lower():
            out.append(d)
    return out


def get_by_code(code: int) -> DHCPOptionCodeDef | None:
    for d in load_catalog():
        if d.code == code:
            return d
    return None


def get_by_name(name: str) -> DHCPOptionCodeDef | None:
    needle = name.strip().lower()
    for d in load_catalog():
        if d.name.lower() == needle:
            return d
    return None
