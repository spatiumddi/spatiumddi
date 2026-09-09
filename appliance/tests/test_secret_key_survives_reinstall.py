"""SECRET_KEY must survive a helm uninstall/reinstall (#1042).

`app/core/crypto.py` derives the at-rest Fernet key from SECRET_KEY whenever
no explicit CREDENTIAL_ENCRYPTION_KEY is set — which is every appliance. So
the chart-owned Secret carrying `secret-key` is the root of everything
`encrypt_str` ever wrote: the appliance CA private key, appliance certs,
integration credentials, OIDC/SAML secrets — plus every JWT.

A k3s HelmChart defaults to `failurePolicy: reinstall`, documented as "a
clean uninstall and reinstall of the chart". On three ddi-pg slot-upgrade
walks helm-controller took that path, the unannotated Secret was deleted, the
reinstall's `lookup` found nothing, `randAlphaNum 64` minted a new key, and
cluster member approval started answering 500 with
`ValueError: encrypted value could not be decrypted` — while the control
plane still reported healthy. The Postgres and Redis secrets survived the
same event purely because they carry `helm.sh/resource-policy: keep`.

Two independent guards, because each closes the hole for a different
deployment shape: the annotation protects anyone whose chart owns the Secret
(plain Kubernetes / Helm), and `failurePolicy: abort` stops the appliance
taking the destructive path at all.

    python3 -m pytest appliance/tests/test_secret_key_survives_reinstall.py -v
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
FIRSTBOOT = REPO / "appliance" / "mkosi.extra" / "usr" / "local" / "bin" / "spatiumddi-firstboot"
CHART_TEMPLATES = REPO / "charts" / "spatiumddi" / "templates"

pytestmark = pytest.mark.skipif(
    not FIRSTBOOT.exists(), reason="appliance tree not present in this checkout"
)


# ── the chart half ───────────────────────────────────────────────────────────


def _generated_credential_secrets() -> list[Path]:
    """Secret templates that MINT a credential when none exists.

    Those are exactly the ones a delete-and-recreate silently rotates. Found
    by their generator rather than by name, so a fourth one cannot be added
    without either carrying the annotation or failing this test.
    """
    found = []
    for path in sorted(CHART_TEMPLATES.glob("*.yaml")):
        body = path.read_text(encoding="utf-8")
        if "kind: Secret" in body and re.search(r"\brandAlphaNum\b", body):
            found.append(path)
    return found


#: The annotation as a real YAML entry, with the value that matters. Matching
#: the key name anywhere in the file passed with the whole `annotations:` block
#: deleted, because this template's own header prose explains the annotation —
#: proven, and exactly the vacuous-guard shape these tests exist to prevent.
_KEEP_ENTRY = re.compile(r'^\s*"?helm\.sh/resource-policy"?\s*:\s*keep\s*$', re.M)

#: Helm's `{{/* … */}}` comments, stripped before matching so prose about the
#: annotation can never satisfy the assertion.
_HELM_COMMENT = re.compile(r"\{\{-?/\*.*?\*/-?\}\}", re.S)


def _body_without_comments(path: Path) -> str:
    body = _HELM_COMMENT.sub("", path.read_text(encoding="utf-8"))
    return "\n".join(
        ln for ln in body.splitlines() if not ln.lstrip().startswith("#")
    )


def test_generated_credential_secrets_are_kept_across_uninstall() -> None:
    missing = [
        p.name
        for p in _generated_credential_secrets()
        if not _KEEP_ENTRY.search(_body_without_comments(p))
    ]
    assert not missing, (
        "these templates mint a credential but do not carry "
        f"`helm.sh/resource-policy: keep` as a real annotation: {missing}"
    )


def test_the_app_secret_is_one_of_them() -> None:
    """Negative control on the finder itself.

    If `secret.yaml` ever stops being detected — renamed generator, moved
    file — the test above would pass by looking at nothing, which is the
    failure mode this whole change exists to stop.
    """
    names = [p.name for p in _generated_credential_secrets()]
    assert "secret.yaml" in names, f"SECRET_KEY's template is not being checked: {names}"


# ── the appliance half ───────────────────────────────────────────────────────


def _render_control_helmchart() -> dict:
    """Execute firstboot's own renderer and parse the manifest it emits."""
    src = FIRSTBOOT.read_text(encoding="utf-8")
    fn = re.search(r"^_render_control_helmchart\(\) \{.*?^\}$", src, re.S | re.M)
    assert fn, "firstboot no longer defines _render_control_helmchart"
    script = f"""
        set -euo pipefail
        {fn.group(0)}
        SPATIUMDDI_VERSION=0.0.0-test
        DNS_AGENT_KEY_VAL=x
        DHCP_AGENT_KEY_VAL=x
        LG_AGENT_KEY_VAL=x
        APPLIANCE_HOSTNAME_VAL=test
        APPLIANCE_HOST_IPS_VAL=10.0.0.1
        INITIAL_NTP_SERVERS_VAL=""
        CHART_TGZ=/nonexistent
        _render_control_helmchart "Y2hhcnQ="
    """
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    ).stdout
    return yaml.safe_load(out)


def test_the_control_helmchart_does_not_set_abort() -> None:
    """`abort` was considered and REJECTED — this pins the decision (#1042).

    It looks like the safer setting and is not. spatiumddi-helm-stuck-recover
    only acts on a HelmChart carrying a `Failed` condition, and only after a
    600 s latch on a 5 min tick — so a release left `pending-upgrade` by the
    slot reboot, which is the scenario #1042 was found in, may never qualify.
    `abort` there risks an indefinite outage with no API and no UI, where the
    default `reinstall` recovers in seconds.

    What made `reinstall` dangerous was the missing
    `helm.sh/resource-policy: keep`, not the reinstall: with it the Secret
    survives the uninstall and the reinstall's `lookup` finds the same key.
    Disruptive and self-healing is fine; lossy was not.
    """
    manifest = _render_control_helmchart()
    assert manifest["spec"].get("failurePolicy") != "abort", (
        "failurePolicy: abort defers recovery to a timer that may never fire "
        "for a pending-upgrade release — see this test's docstring"
    )


def test_values_content_still_parses() -> None:
    """Cheap structural check on the manifest the appliance actually applies."""
    values = yaml.safe_load(_render_control_helmchart()["spec"]["valuesContent"])
    assert isinstance(values, dict) and values, "firstboot rendered no values"
