#!/usr/bin/env python3
"""Refuse a rendered chart in which any container would run BestEffort (#965).

Reads one or more ``helm template`` streams (files, or stdin when given ``-``)
and fails if a Pod template — Deployment / StatefulSet / DaemonSet / Job /
CronJob, or a bare Pod — carries a container or init container with no
``resources.requests.cpu`` *and* ``memory``.

Why a render-time check and not a review rule: BestEffort is the lowest CFS
weight on the node, and #952 / #953 / #965 were three rounds of finding the
serving pods the control plane could starve. Two of the three #965 workloads
had been left out of #953 on purpose and one by omission; nothing but reading
every template again would have told them apart. This does that on every PR.

A ``with .Values.x.resources`` guard that tests the wrong path renders a pod
with no ``resources:`` at all and passes ``helm lint`` and ``kubeconform``
both — which is the shape this script exists to catch.

Exit 0 when every container has both requests, 1 with one line per offender.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from typing import Any

import yaml

_POD_TEMPLATE_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet"}


def _pod_specs(doc: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    kind = doc.get("kind")
    name = f"{kind}/{doc.get('metadata', {}).get('name', '?')}"
    spec = doc.get("spec") or {}
    if kind == "Pod":
        yield name, spec
    elif kind in _POD_TEMPLATE_KINDS:
        yield name, (spec.get("template") or {}).get("spec") or {}
    elif kind == "CronJob":
        job = (spec.get("jobTemplate") or {}).get("spec") or {}
        yield name, (job.get("template") or {}).get("spec") or {}


def _offenders(docs: Iterable[dict[str, Any] | None]) -> list[str]:
    out: list[str] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for name, pod in _pod_specs(doc):
            for field in ("initContainers", "containers"):
                for c in pod.get(field) or []:
                    requests = ((c.get("resources") or {}).get("requests")) or {}
                    missing = [k for k in ("cpu", "memory") if not requests.get(k)]
                    if missing:
                        out.append(
                            f"{name} {field[:-1]} {c.get('name', '?')}: "
                            f"no requests.{' / requests.'.join(missing)} — BestEffort"
                        )
    return out


def main(argv: list[str]) -> int:
    paths = argv[1:] or ["-"]
    failures: list[str] = []
    for path in paths:
        stream = sys.stdin if path == "-" else open(path, encoding="utf-8")  # noqa: SIM115
        with stream:
            failures.extend(f"{path}: {line}" for line in _offenders(yaml.safe_load_all(stream)))
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        print(f"{len(failures)} container(s) would run BestEffort", file=sys.stderr)
        return 1
    print("no BestEffort containers")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
