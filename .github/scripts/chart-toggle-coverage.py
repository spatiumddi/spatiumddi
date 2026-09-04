#!/usr/bin/env python3
"""Fail if a chart template is gated on a values key no render flips (#966).

The render gate in ``charts-render-check.sh`` templates each chart with a
hand-maintained list of ``--set`` toggles. Hand-maintained lists drift: the
first version of that list missed nine umbrella gates — the CNPG ``Cluster``
and Redis Sentinel HA shapes among them — so the job that exists to render
every template was rendering a subset and would have stayed green while a
broken HA template shipped. This script closes the loop.

Usage::

    chart-toggle-coverage.py <chart dir> [--set key=value ...]

It greps the chart's own templates for ``.Values.<path>.enabled`` and
``.Values.<path>.kind`` references and, for each, checks that at least one
render exercises the non-default branch:

* an ``.enabled`` key whose ``values.yaml`` default is ``false`` must appear
  in a ``--set`` as ``true`` (default-``true`` keys render on their own);
* a ``.kind`` key must appear in a ``--set`` with a value different from its
  default, since ``kind`` selects between whole templates.

Pass every ``--set`` from every render of that chart, together. Exit 1 with
one line per uncovered key.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

_REF = re.compile(r"\.Values\.((?:[A-Za-z0-9_]+\.)+)(enabled|kind)\b")


def _lookup(values: dict[str, Any], dotted: str) -> Any:
    node: Any = values
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    chart = Path(argv[1])
    flipped: dict[str, str] = {}
    args = iter(argv[2:])
    for a in args:
        if a == "--set":
            k, _, v = next(args).partition("=")
            flipped[k] = v
    values = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8")) or {}

    refs: set[tuple[str, str]] = set()
    for tpl in sorted((chart / "templates").rglob("*.yaml")) + sorted(
        (chart / "templates").rglob("*.tpl")
    ):
        for m in _REF.finditer(tpl.read_text(encoding="utf-8")):
            refs.add((m.group(1).rstrip("."), m.group(2)))

    missing: list[str] = []
    for path, leaf in sorted(refs):
        key = f"{path}.{leaf}"
        default = _lookup(values, key)
        if leaf == "enabled":
            if default is True or flipped.get(key) == "true":
                continue
            missing.append(f"{key} (default {default!r}) is never set to true")
        else:
            if key in flipped and flipped[key] != str(default):
                continue
            missing.append(f"{key} (default {default!r}) is never rendered with another value")
    for line in missing:
        print(f"{chart.name}: {line}", file=sys.stderr)
    if missing:
        print(
            f"{len(missing)} template gate(s) never exercised by the render matrix", file=sys.stderr
        )
        return 1
    print(f"{chart.name}: all {len(refs)} template gates covered")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
