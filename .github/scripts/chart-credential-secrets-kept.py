#!/usr/bin/env python3
"""Every rendered Secret carrying a generated credential must be kept (#1042).

A k3s HelmChart defaults to ``failurePolicy: reinstall`` — "a clean uninstall
and reinstall of the chart" — and an unannotated Secret does not survive the
uninstall half. The chart then mints a fresh one on install, which for the
Secret holding SECRET_KEY orphaned everything ``encrypt_str`` had written: the
appliance CA private key, appliance certs, integration credentials, OIDC/SAML
secrets, every JWT. Observed on three slot-upgrade walks; the Postgres and
Redis secrets came through the same event purely because they carry
``helm.sh/resource-policy: keep``.

This runs on the RENDERED manifest, which is the half a template-text check
cannot do: it sees the annotation as Helm actually emits it, so a value typo,
a stray quote, or an annotation hidden behind a condition that is false in
this value set all fail here.

Usage: chart-credential-secrets-kept.py <rendered.yaml>
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

#: Key names whose value the chart GENERATES when absent. A Secret holding one
#: of these cannot be recreated without rotating it, so losing it is data loss
#: rather than an inconvenience.
GENERATED_KEYS = {"secret-key", "password", "redis-password"}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1])
    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]

    secrets = [d for d in docs if d.get("kind") == "Secret"]
    checked = 0
    bad: list[str] = []
    for doc in secrets:
        keys = set(doc.get("stringData") or {}) | set(doc.get("data") or {})
        if not (keys & GENERATED_KEYS):
            continue
        checked += 1
        name = doc["metadata"]["name"]
        ann = doc["metadata"].get("annotations") or {}
        if ann.get("helm.sh/resource-policy") != "keep":
            bad.append(f"{name} (keys={sorted(keys & GENERATED_KEYS)}, annotations={ann})")

    if bad:
        print(f"✗ {path.name}: credential Secret(s) missing "
              f"`helm.sh/resource-policy: keep` — a reinstall rotates them:",
              file=sys.stderr)
        for b in bad:
            print(f"    {b}", file=sys.stderr)
        return 1

    # A render with no credential Secret at all is normal (appliance shapes,
    # external-DB shapes). Saying so keeps "checked nothing" distinguishable
    # from "checked and clean" in the log.
    print(f"credential secrets kept: {checked} of {len(secrets)} Secret(s) carried a generated key")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
