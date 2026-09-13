#!/usr/bin/env python3
"""Refuse a docs link that escapes ``docs/`` (#1070).

The Jekyll site published to www.spatiumddi.com carries ``docs/`` and
nothing else. A relative link out of it — ``../backend/app/models/dns.py``,
``../CONTRIBUTING.md``, ``../.github/workflows/ci.yml`` — resolves fine on
GitHub, where ``docs/`` sits beside the code, and 404s for every reader of
the published site. There were 92 of them when this was written, and the
failure is silent in both directions: nothing errors at build time, and
the author checking their work on GitHub sees a working link.

Source links are still welcome. They just have to be absolute, so they
work in both places:

    [record_ops.py](https://github.com/spatiumddi/spatiumddi/blob/main/backend/app/services/dns/record_ops.py)

Run with --list to see every offender.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
REPO = "https://github.com/spatiumddi/spatiumddi"

INLINE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
REFDEF = re.compile(r"^\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
# A path inside a fenced example is prose about the repo, not a link.
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def offenders() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for md in sorted(DOCS.rglob("*.md")):
        text = FENCE.sub("", md.read_text(encoding="utf-8", errors="replace"))
        for href in INLINE.findall(text) + REFDEF.findall(text):
            h = href.split("#")[0].strip()
            if not h or h.startswith(("http://", "https://", "mailto:")):
                continue
            target = (md.parent / h).resolve()
            try:
                target.relative_to(DOCS)
            except ValueError:
                out.append((md.relative_to(ROOT), href))
    return out


def main() -> int:
    found = offenders()
    if "--list" in sys.argv:
        for md, href in found:
            print(f"{md}: {href}")
        return 0
    if not found:
        n = len(list(DOCS.rglob("*.md")))
        print(f"OK — {n} docs page(s) scanned, no link escapes docs/.")
        return 0
    print(f"ERROR: {len(found)} docs link(s) point outside docs/ and will 404")
    print("       on the published site (they only work on GitHub):\n")
    for md, href in found[:40]:
        print(f"  {md}: {href}")
    if len(found) > 40:
        print(f"  … and {len(found) - 40} more (run with --list)")
    print(f"\nUse an absolute URL instead: {REPO}/blob/main/<path>")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
