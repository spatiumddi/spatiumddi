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


def test_generated_credential_secrets_are_kept_across_uninstall() -> None:
    missing = [
        p.name
        for p in _generated_credential_secrets()
        if "helm.sh/resource-policy" not in p.read_text(encoding="utf-8")
    ]
    assert not missing, (
        "these templates mint a credential but do not carry "
        f"`helm.sh/resource-policy: keep`, so a reinstall rotates it: {missing}"
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


def test_control_helmchart_never_takes_the_destructive_default() -> None:
    """`reinstall` uninstalls a stateful release; `abort` leaves it repairable.

    Recovery is already explicit and logged — spatiumddi-helm-stuck-recover
    clears the install Job and the `sh.helm.release.v1.*` tracking secrets so
    helm-controller retries — so losing the self-healing reinstall costs
    nothing here. `retry` would be acceptable too; `reinstall` is not.
    """
    manifest = _render_control_helmchart()
    policy = manifest["spec"].get("failurePolicy")
    assert policy in ("abort", "retry"), (
        "spatium-control must not use the CRD default `reinstall`, which "
        f"uninstalls the release that owns SECRET_KEY (got {policy!r})"
    )


def test_failure_policy_is_on_the_spec_not_in_the_values() -> None:
    """A values key named failurePolicy would be silently inert."""
    manifest = _render_control_helmchart()
    values = yaml.safe_load(manifest["spec"]["valuesContent"])
    assert "failurePolicy" not in values, "failurePolicy belongs on spec, not in valuesContent"
