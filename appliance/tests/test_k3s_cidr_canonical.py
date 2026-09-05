"""The wizard records the k3s CIDRs the cluster will actually use (#974).

``_k3s_cidr_error`` parses the operator's pod / service CIDRs with
``ipaddress.ip_network(..., strict=False)``, which masks off host bits
for the purposes of its overlap checks and then throws the masked value
away: the operator's literal string was what reached
``config.yaml.d``'s ``cluster-cidr`` / ``service-cidr``. Type
``10.42.0.5/16`` and the file recorded a network that was never the one
the cluster ran.

Untidy before k3s v1.36; risky after. Kubernetes 1.36 enables
``StrictIPCIDRValidation`` by default — core-API IP/CIDR fields reject
host-bits-set values — and the apiserver derives a ``ServiceCIDR``
object from ``--service-cluster-ip-range``. Writing the canonical form
removes the question rather than betting on how that derivation
normalises.

Two halves, because either alone can pass while the bug is live:

  * a behavioural test of ``_k3s_cidr_canonical`` (extracted from the
    script and run under real bash), and
  * a structural test that every point which commits a CIDR to the
    installer's state goes through it — a correct helper nobody calls
    changes nothing.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_k3s_cidr_canonical.py -v
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALLER = (
    Path(__file__).parent.parent / "mkosi.extra" / "usr" / "local" / "bin" / "spatium-install"
)


def _script() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _extract_function(name: str) -> str:
    """Pull one shell function out of the installer by brace matching.

    Cheaper and less brittle than sourcing the whole script, which runs
    top-level assignments and expects an appliance.
    """
    text = _script()
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + 3]


# ── Behaviour ───────────────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
@pytest.mark.parametrize(
    ("typed", "want"),
    [
        # The defaults are already canonical and must survive untouched.
        ("10.42.0.0/16", "10.42.0.0/16"),
        ("10.43.0.0/16", "10.43.0.0/16"),
        # Host bits set — the case the whole change is about.
        ("10.42.0.5/16", "10.42.0.0/16"),
        ("192.168.0.5/24", "192.168.0.0/24"),
        ("172.16.255.255/12", "172.16.0.0/12"),
        # A bare address is a /32 and is already its own network.
        ("10.0.0.1/32", "10.0.0.1/32"),
        # Unparseable input passes through untouched: _k3s_cidr_error is
        # what refuses it, and a canonicaliser that swallowed the value
        # would hide the operator's typo behind an empty string. Leading
        # zeros are in this class — Python's ipaddress rejects them.
        ("010.1.1.1/24", "010.1.1.1/24"),
        ("not-a-cidr", "not-a-cidr"),
        ("", ""),
    ],
)
def test_canonicalises(typed: str, want: str) -> None:
    fn = _extract_function("_k3s_cidr_canonical")
    out = subprocess.run(
        ["bash", "-c", f'{fn}\n_k3s_cidr_canonical "$1"', "_", typed],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout == want


# ── Wiring ──────────────────────────────────────────────────────────────


def _assignments(var: str) -> list[str]:
    """Every line assigning ``var``, excluding the initial default."""
    pattern = re.compile(rf"^\s*{re.escape(var)}=(.+)$")
    out = []
    for line in _script().splitlines():
        m = pattern.match(line)
        if m:
            out.append(m.group(1).strip())
    return out


@pytest.mark.parametrize("var", ["K3S_POD_CIDR", "K3S_SERVICE_CIDR"])
def test_every_commit_point_canonicalises(var: str) -> None:
    """A raw assignment is how the bug comes back.

    Scoped to assignments whose right-hand side references a shell
    variable, because those are the ones that can carry operator input;
    a literal default cannot, and is checked separately below.
    """
    raw = [
        a for a in _assignments(var) if "$" in a and "_k3s_cidr_canonical" not in a
    ]
    assert not raw, (
        f"{var} is assigned from operator input without canonicalising: {raw}. "
        "Route it through _k3s_cidr_canonical, or the literal string typed at "
        "the prompt reaches config.yaml (#974)."
    )


@pytest.mark.parametrize(
    ("var", "want"),
    [("K3S_POD_CIDR", '"10.42.0.0/16"'), ("K3S_SERVICE_CIDR", '"10.43.0.0/16"')],
)
def test_the_default_is_already_canonical(var: str, want: str) -> None:
    assert want in _assignments(var)


def test_config_yaml_is_written_from_those_variables() -> None:
    """Pins the link between the canonicalised state and the k3s config.

    Without this the wiring test above could pass while the config
    drop-in interpolated something else entirely.
    """
    text = _script()
    assert "cluster-cidr: $K3S_POD_CIDR" in text
    assert "service-cidr: $K3S_SERVICE_CIDR" in text
