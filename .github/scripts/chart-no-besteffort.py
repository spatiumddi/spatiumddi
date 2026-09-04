#!/usr/bin/env python3
"""Refuse a rendered chart in which a serving container would run BestEffort (#965).

Reads one or more ``helm template`` streams (files, or stdin when given ``-``)
and fails if a Pod template — Deployment / StatefulSet / DaemonSet / Job /
CronJob, or a bare Pod — carries a container with neither a request nor a
limit for CPU, or for memory (each is reported separately; a container with
``limits`` and no ``requests`` passes, since Kubernetes defaults the request
to the limit). Init containers are exempt: they finish before the pod
serves, so their CFS weight cannot starve anything that matters.

The check is per serving container, deliberately stricter than the pod-level
QoS class: the CFS weight that decides who gets the CPU under contention is
the container cgroup's own, so one sidecar with no request is at weight 2
whatever its neighbours declare.

Why a render-time check and not a review rule: #952 / #953 / #965 were three
rounds of finding the serving pods the control plane could starve. Two of
the three #965 workloads had been left out of #953 on purpose and one by
omission; nothing but reading every template again would have told them
apart. This does that on every PR. A ``with .Values.x.resources`` guard that
tests the wrong path renders a pod with no ``resources:`` at all and passes
``helm lint`` and ``kubeconform`` both — which is the shape this exists to
catch.

Exit 0 when every serving container is covered, 1 with one line per offender.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from typing import Any

import yaml

_POD_TEMPLATE_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}


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
            for c in pod.get("containers") or []:
                res = c.get("resources") or {}
                requests = res.get("requests") or {}
                limits = res.get("limits") or {}
                missing = [k for k in ("cpu", "memory") if not (requests.get(k) or limits.get(k))]
                if missing:
                    out.append(
                        f"{name} container {c.get('name', '?')}: "
                        f"no request or limit for {' / '.join(missing)} — BestEffort"
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
