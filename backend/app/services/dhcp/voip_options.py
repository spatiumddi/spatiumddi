"""Curated per-vendor VoIP DHCP-option catalog (issue #112 phase 1).

Loads ``app/data/dhcp_voip_options.json`` once per process. Used by:

- ``GET /api/v1/dhcp/voip-options`` — frontend phone-profile editor
  uses this to render the vendor-grouped option picker
- Phone-profile starter-pack seeding endpoint — pulls the option set
  per vendor from here so the seed pack stays in sync with the catalog

Static JSON shipped with the app (same model as the 95-entry DHCP
option-code library). Operators can override per-row values when they
build their own phone profile, but the curated entries get them 90%
of the way there with a single click.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "dhcp_voip_options.json"


@dataclass(frozen=True)
class VoIPVendorOption:
    code: int
    name: str
    kind: str
    use: str


@dataclass(frozen=True)
class VoIPVendor:
    vendor: str
    match_hint: str
    description: str
    options: tuple[VoIPVendorOption, ...]


@lru_cache(maxsize=1)
def load_catalog() -> list[VoIPVendor]:
    raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    out: list[VoIPVendor] = []
    for v in raw.get("vendors", []):
        opts = tuple(
            VoIPVendorOption(
                code=int(o["code"]),
                name=str(o["name"]),
                kind=str(o.get("kind", "string")),
                use=str(o.get("use", "")),
            )
            for o in v.get("options", [])
        )
        out.append(
            VoIPVendor(
                vendor=str(v["vendor"]),
                match_hint=str(v.get("match_hint", "")),
                description=str(v.get("description", "")),
                options=opts,
            )
        )
    out.sort(key=lambda d: d.vendor.lower())
    return out


def get_vendor(name: str) -> VoIPVendor | None:
    needle = name.strip().lower()
    for v in load_catalog():
        if v.vendor.lower() == needle:
            return v
    return None


__all__ = ["VoIPVendor", "VoIPVendorOption", "load_catalog", "get_vendor"]
