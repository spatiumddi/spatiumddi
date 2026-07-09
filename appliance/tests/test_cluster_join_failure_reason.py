"""spatium-cluster-join failure-reason classifier (#590).

The runner can only observe "k3s didn't come Ready". The WHY lives in the
journal, and the operator only ever sees the ``.state`` sidecar's reason
field (surfaced on the Fleet row). Some join failures are PERMANENT until an
operator acts — most notably re-joining a node whose etcd membership still
exists under the same hostname, which k3s refuses with "duplicate node name
found" and which no retry can ever fix. Reporting a bare "join failed" there
leaves the operator with nothing to act on.

Observed live on a 3-node install: a member joined successfully, the join
re-fired and wiped its identity, and every subsequent attempt died in one
second with the duplicate-name fatal. The Fleet UI showed only "failed".

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_cluster_join_failure_reason.py -v

No journal, no k3s, no appliance required — the classifier reads stdin.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent
    / "mkosi.extra"
    / "usr"
    / "local"
    / "bin"
    / "spatium-cluster-join"
)


def _classify(log: str) -> str:
    """Source the real script as a library and run the classifier on `log`."""
    proc = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"\nclassify_join_failure'],
        input=log,
        env={**os.environ, "SPATIUM_CLUSTER_JOIN_LIB": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_duplicate_node_name_tells_the_operator_to_evict() -> None:
    """The exact fatal from the live cluster. This one can never succeed on
    retry — the stale etcd member has to be removed first."""
    log = (
        'time="2026-07-09T16:08:48Z" level=error msg="Shutdown request '
        'received: \\"etcd cluster join failed: duplicate node name found, '
        'please use a unique name for this node\\""'
    )
    reason = _classify(log)
    assert "already an etcd member" in reason
    assert "evict" in reason.lower()


def test_bootstrap_token_mismatch_is_named() -> None:
    """The failure the #590 issue originally reported on the replace flow."""
    log = (
        'level=fatal msg="Error: preparing server: failed to bootstrap cluster '
        'data: failed to reconcile with local datastore: bootstrap data already '
        'found and encrypted with different token"'
    )
    reason = _classify(log)
    assert "stale k3s bootstrap data" in reason


def test_removed_member_is_named() -> None:
    log = 'level=error msg="etcd error: the member has been permanently removed from the cluster"'
    reason = _classify(log)
    assert "removed from the cluster" in reason
    assert "leave first" in reason


def test_rejected_token_is_named() -> None:
    assert "rejected the join token" in _classify('msg="failed to validate token"')
    assert "rejected the join token" in _classify('msg="token CA hash does not match"')


def test_unreachable_seed_points_at_the_firewall() -> None:
    """The self-inflicted-partition shape: the seed is up, but its etcd/kubeapi
    ports are firewalled off."""
    log = 'error="dial tcp 192.168.0.133:2380: i/o timeout"'
    reason = _classify(log)
    assert "firewall" in reason
    assert "6443" in reason


def test_unknown_failure_yields_empty_so_the_caller_falls_back() -> None:
    """An unrecognised failure must not invent a reason — do_join substitutes
    the generic 'k3s did not come Ready within Ns'."""
    assert _classify('level=info msg="something entirely unremarkable"') == ""
    assert _classify("") == ""


def test_reason_is_a_single_line_with_no_tabs() -> None:
    """write_state serialises as ``state\\treason\\n`` — a tab or newline in
    the reason would corrupt the sidecar the supervisor parses."""
    logs = [
        "duplicate node name found",
        "bootstrap data already found and encrypted with different token",
        "the member has been permanently removed from the cluster",
        "failed to validate token",
        "dial tcp 10.0.0.1:2380: i/o timeout",
    ]
    for log in logs:
        reason = _classify(log)
        assert reason, f"expected a reason for {log!r}"
        assert "\t" not in reason
        assert "\n" not in reason


def _was_member(dropin_dir: Path) -> bool:
    """Run `node_was_cluster_member` against a synthetic config.yaml.d."""
    proc = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"\nnode_was_cluster_member'],
        env={
            **os.environ,
            "SPATIUM_CLUSTER_JOIN_LIB": "1",
            "SPATIUM_K3S_DROPIN_DIR": str(dropin_dir),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def test_a_standalone_node_is_not_a_member(tmp_path: Path) -> None:
    """No join drop-in → never joined. Its rollback MAY restore the node's
    own single-node bootstrap manifests."""
    dropins = tmp_path / "config.yaml.d"
    dropins.mkdir()
    (dropins / "spatium-cidrs.yaml").write_text("cluster-cidr: 10.42.0.0/16\n")
    assert _was_member(dropins) is False


def test_a_joined_node_is_a_member(tmp_path: Path) -> None:
    """REGRESSION GUARD (#590). The join drop-in is written only by a
    SUCCESSFUL join, so its presence means restore_identity would put this
    node straight back into the SHARED cluster.

    Restoring its single-node bootstrap manifests there makes its
    helm-controller apply them cluster-wide, overwriting the seed's
    HelmCharts of the same name. Observed live: a failed re-join restored a
    member's manifests, whose ``spatium-bootstrap`` values carry
    ``cnpg: false`` — which UNINSTALLED the CloudNativePG operator on a
    healthy 3-node cluster, and ``agentLanding: true``, which parked an
    nginx pod on the frontend's :80."""
    dropins = tmp_path / "config.yaml.d"
    dropins.mkdir()
    (dropins / "spatium-cluster.yaml").write_text(
        "server: https://10.0.0.1:6443\ntoken: redacted\n"
    )
    assert _was_member(dropins) is True


def test_missing_dropin_dir_is_not_a_member(tmp_path: Path) -> None:
    assert _was_member(tmp_path / "does-not-exist") is False


def test_first_match_wins_on_a_noisy_log() -> None:
    """A real journal carries connection noise alongside the fatal. The
    duplicate-name fatal is the actionable one and must not be masked by the
    i/o-timeout warnings that surround it."""
    log = "\n".join(
        [
            'msg="prober detected unhealthy status" error="dial tcp 192.168.0.125:2380: i/o timeout"',
            'msg="Shutdown request received: \\"etcd cluster join failed: duplicate node name found\\""',
            'msg="prober detected unhealthy status" error="dial tcp 192.168.0.125:2380: i/o timeout"',
        ]
    )
    assert "already an etcd member" in _classify(log)
