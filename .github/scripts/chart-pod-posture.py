#!/usr/bin/env python3
"""Pod-posture gate for the rendered charts (#983).

Two properties that are trivially forgotten on a NEW workload and invisible
once forgotten, because nothing fails — the pod schedules and runs, just
without the protection:

  seccomp        every pod template carries ``securityContext.seccompProfile``.
                 Kubernetes runs a container ``Unconfined`` unless asked;
                 docker-compose applies the runtime's default profile to every
                 service. A workload that omits this runs with FEWER syscall
                 restrictions under Kubernetes than under Compose.

  priority       every pod template carries a non-empty ``priorityClassName``.
                 With every pod at priority 0, kubelet eviction under memory
                 pressure picks the pod that grew the most rather than the one
                 that matters least. Only asserted for renders that opted in
                 (``--require-priority``), because the umbrella chart's default
                 is deliberately empty: naming a PriorityClass that does not
                 exist makes the apiserver reject the pod outright, so a
                 bring-your-own cluster must not get one by default.
                 A workload that is MEANT to sit at priority 0 is named in
                 ``--allow-no-priority``, so the decision is visible at the
                 call site and a NEW workload still fails the gate.

Both are checked against the RENDERED manifests rather than the templates: a
``with`` guard on the wrong values path renders nothing and reads fine in the
template. Reference: the #917 lesson that a guard which inspects intent instead
of output reports clean while the defect ships.

Usage:
    chart-pod-posture.py [--require-priority] [--allow-no-priority a,b] file...
"""

from __future__ import annotations

import sys

import yaml

# Workload kinds whose spec carries a pod template, and the path to it.
POD_TEMPLATE_PATHS: dict[str, tuple[str, ...]] = {
    "Deployment": ("spec", "template"),
    "StatefulSet": ("spec", "template"),
    "DaemonSet": ("spec", "template"),
    "Job": ("spec", "template"),
    "ReplicaSet": ("spec", "template"),
    "CronJob": ("spec", "jobTemplate", "spec", "template"),
}

# CRs that manage their own pods, with the field on the CR that carries each
# property through to them. CNPG's ``Cluster`` is the only one either chart
# renders today.
MANAGED_POD_CRS: dict[str, dict[str, tuple[str, ...]]] = {
    "postgresql.cnpg.io/Cluster": {
        "seccomp": ("spec", "seccompProfile"),
        "priority": ("spec", "priorityClassName"),
    },
}


def dig(obj, path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def main(argv: list[str]) -> int:
    require_priority = "--require-priority" in argv
    exempt: set[str] = set()
    args = argv[1:]
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--allow-no-priority":
            i += 1
            if i >= len(args):
                print("--allow-no-priority needs a comma-separated list", file=sys.stderr)
                return 2
            exempt.update(x for x in args[i].split(",") if x)
        elif not a.startswith("--"):
            files.append(a)
        i += 1
    if not files:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2

    problems: list[str] = []
    checked = 0

    for path in files:
        with open(path, encoding="utf-8") as fh:
            docs = [d for d in yaml.safe_load_all(fh) if isinstance(d, dict)]
        for doc in docs:
            kind = doc.get("kind")
            api = str(doc.get("apiVersion", ""))
            group = api.rsplit("/", 1)[0] if "/" in api else ""
            name = dig(doc, ("metadata", "name")) or "<unnamed>"
            where = f"{path}: {kind}/{name}"

            cr_key = f"{group}/{kind}"
            if cr_key in MANAGED_POD_CRS:
                checked += 1
                fields = MANAGED_POD_CRS[cr_key]
                if not dig(doc, fields["seccomp"]):
                    problems.append(f"{where}: no {'.'.join(fields['seccomp'])}")
                if require_priority and name not in exempt and not dig(doc, fields["priority"]):
                    problems.append(f"{where}: no {'.'.join(fields['priority'])}")
                continue

            if kind not in POD_TEMPLATE_PATHS:
                continue
            pod = dig(doc, POD_TEMPLATE_PATHS[kind] + ("spec",))
            if pod is None:
                problems.append(f"{where}: pod template has no spec")
                continue
            checked += 1

            profile = dig(pod, ("securityContext", "seccompProfile", "type"))
            if not profile:
                problems.append(f"{where}: pod securityContext has no seccompProfile.type")

            if require_priority and name not in exempt and not pod.get("priorityClassName"):
                problems.append(
                    f"{where}: no priorityClassName "
                    "(add one, or name it in --allow-no-priority to record the decision)"
                )

    if problems:
        print(f"pod-posture: {len(problems)} problem(s) across {checked} workload(s)", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    print(f"pod-posture: {checked} workload(s) OK" + (" (priority required)" if require_priority else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
