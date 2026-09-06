#!/usr/bin/env python3
"""Fail when two renders of the same chart both claim a cluster-scoped object (#992).

The appliance chart is installed TWICE per appliance, under two release
names — ``spatium-bootstrap`` (firstboot: the supervisor + the CNPG
operator) and ``spatiumddi-appliance`` (the supervisor: the role
DaemonSets). Helm stamps ``meta.helm.sh/release-name`` on everything it
creates and refuses an install WHOLE when it meets an existing object owned
by another release.

Namespaced objects never collide, because the two releases render disjoint
workloads. Cluster-scoped ones are the trap: they carry no namespace to
keep them apart, so an object rendered by both releases makes whichever
install runs second fail with ``invalid ownership metadata`` — and under
k3s's helm-controller (``backoffLimit: 1000``) that failure is a job that
retries forever rather than an error anyone sees. #988 shipped three
PriorityClasses this way and no appliance installed after it had a single
role DaemonSet on the cluster.

So: render the chart once per release shape and compare. An object here is
a bug in the VALUES, not in the template — the fix is to give one release
``create: false`` for whatever renders it.

Usage:
    chart-cluster-scoped-collision.py <label>=<render.yaml> <label>=<render.yaml> ...
"""

from __future__ import annotations

import sys
from collections import defaultdict

import yaml

# Cluster-scoped kinds. Enumerated rather than derived: a rendered manifest
# carries no scope information (helm omits ``metadata.namespace`` on plenty
# of NAMESPACED objects too, leaving it to the release namespace), so there
# is nothing in the YAML to infer this from. Covers the built-in kinds a
# Helm chart can plausibly render; add to it rather than guessing.
CLUSTER_SCOPED = frozenset(
    {
        "APIService",
        "CSIDriver",
        "CSINode",
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "FlowSchema",
        "IngressClass",
        "MutatingWebhookConfiguration",
        "Namespace",
        "Node",
        "PersistentVolume",
        "PodSecurityPolicy",
        "PriorityClass",
        "PriorityLevelConfiguration",
        "RuntimeClass",
        "StorageClass",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
        "ValidatingWebhookConfiguration",
        "VolumeAttachment",
        "VolumeSnapshotClass",
    }
)


def _group(api_version: str) -> str:
    """``scheduling.k8s.io/v1`` → ``scheduling.k8s.io``; ``v1`` → ``""``.

    Compared on group rather than the full apiVersion so the same object at
    two API versions still reads as one object — which is what the
    apiserver, and therefore Helm's ownership check, sees.
    """
    return api_version.rsplit("/", 1)[0] if "/" in api_version else ""


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    # (group, kind, name) → [labels that rendered it]
    owners: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for arg in argv:
        label, _, path = arg.partition("=")
        if not path:
            print(f"error: expected <label>=<path>, got {arg!r}", file=sys.stderr)
            return 2
        with open(path, encoding="utf-8") as fh:
            for doc in yaml.safe_load_all(fh):
                if not isinstance(doc, dict):
                    continue
                kind = doc.get("kind")
                if kind not in CLUSTER_SCOPED:
                    continue
                name = (doc.get("metadata") or {}).get("name")
                if not name:
                    continue
                key = (_group(str(doc.get("apiVersion", ""))), str(kind), str(name))
                if label not in owners[key]:
                    owners[key].append(label)

    collisions = {k: v for k, v in owners.items() if len(v) > 1}
    if not collisions:
        total = len(owners)
        labels = ", ".join(a.partition("=")[0] for a in argv)
        print(f"   cluster-scoped: {total} object(s) across [{labels}], no collisions")
        return 0

    print("cluster-scoped object rendered by more than one release shape:", file=sys.stderr)
    for (group, kind, name), labels in sorted(collisions.items()):
        gk = f"{kind}.{group}" if group else kind
        print(f"  {gk}/{name} — rendered by: {', '.join(labels)}", file=sys.stderr)
    print(
        "\nHelm refuses an install whole when it meets a cluster-scoped object owned by\n"
        "another release. Exactly one release shape may render each of these — give the\n"
        "others a ``create: false``-style values gate (see #992, and the PriorityClass\n"
        "pair in charts/spatiumddi-appliance/templates/priorityclasses.yaml).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
