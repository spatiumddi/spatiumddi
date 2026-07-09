"""Host-portable pytest for spatium-cluster-join's cluster-identity wipe (#590).

Every appliance boots with ``cluster-init: true``, so it seeds its own
single-member etcd and writes its own self-generated cluster token to
``$K3S_SERVER_DIR/token``. Joining another cluster means looking FRESH on
disk. Before #590 the wipe moved aside ``db``/``tls``/``cred`` but left the
token files behind, so k3s refused to start on the joiner with::

    failed to bootstrap cluster data: failed to reconcile with local
    datastore: bootstrap data already found and encrypted with different token

…which is what stranded a replacement node in ``cluster_join_state:
"joining"`` forever and made the documented dead-node recovery impossible.

These tests source the real script as a library (SPATIUM_CLUSTER_JOIN_LIB=1)
and drive ``backup_and_wipe_identity`` / ``restore_identity`` against a
synthetic tmp tree.

PATH OVERRIDES (each defaults to the production appliance location):
  SPATIUM_RELEASE_STATE, SPATIUM_K3S_SERVER_DIR, SPATIUM_K3S_AGENT_DIR,
  SPATIUM_K3S_NODE_PASSWORD, SPATIUM_K3S_KUBECONFIG, SPATIUM_LOG_DIR,
  SPATIUM_FLANNEL_SUBNET_ENV, SPATIUM_CNI_NETWORKS_DIR

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_cluster_join_identity.py -v

No database, no Docker, no appliance ISO, no k3s required.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent / "mkosi.extra" / "usr" / "local" / "bin" / "spatium-cluster-join"
)

# Everything under $K3S_SERVER_DIR that must not survive a join. The token
# files are the #590 regression guard; db/tls/cred were already covered.
IDENTITY_ENTRIES = ("db", "tls", "cred", "token", "agent-token", "node-token")


def _tree(tmp_path: Path) -> dict[str, Path]:
    server = tmp_path / "k3s" / "server"
    agent = tmp_path / "k3s" / "agent"
    (server / "db").mkdir(parents=True)
    (server / "tls").mkdir()
    (server / "cred").mkdir()
    (server / "db" / "state.db").write_text("etcd")
    (server / "tls" / "server-ca.crt").write_text("CA")
    (server / "cred" / "passwd").write_text("creds")
    # The three files #590 is about — each holds a self-generated cluster
    # token that must not outlive the wipe.
    (server / "token").write_text("K10self::server:ownsecret")
    (server / "agent-token").write_text("K10self::agent:ownsecret")
    (server / "node-token").write_text("K10self::node:ownsecret")

    (agent / "images").mkdir(parents=True)
    (agent / "images" / "airgap.tar").write_text("baked")
    (agent / "client-kubelet.crt").write_text("kubelet")

    node_pw = tmp_path / "node" / "password"
    node_pw.parent.mkdir(parents=True)
    node_pw.write_text("nodepw")
    kubeconfig = tmp_path / "k3s.yaml"
    kubeconfig.write_text("kubeconfig")
    return {
        "server": server,
        "agent": agent,
        "node_pw": node_pw,
        "kubeconfig": kubeconfig,
    }


def _env(tmp_path: Path, t: dict[str, Path]) -> dict[str, str]:
    return {
        **os.environ,
        "SPATIUM_CLUSTER_JOIN_LIB": "1",
        "SPATIUM_RELEASE_STATE": str(tmp_path / "release-state"),
        "SPATIUM_K3S_SERVER_DIR": str(t["server"]),
        "SPATIUM_K3S_AGENT_DIR": str(t["agent"]),
        "SPATIUM_K3S_NODE_PASSWORD": str(t["node_pw"]),
        "SPATIUM_K3S_KUBECONFIG": str(t["kubeconfig"]),
        "SPATIUM_LOG_DIR": str(tmp_path / "log"),
        # Keep the destructive pod-network cleanup off the real host.
        "SPATIUM_FLANNEL_SUBNET_ENV": str(tmp_path / "flannel" / "subnet.env"),
        "SPATIUM_CNI_NETWORKS_DIR": str(tmp_path / "cni" / "networks"),
    }


def _run(env: dict[str, str], body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"\n{body}'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_wipe_moves_every_identity_entry_aside(tmp_path: Path) -> None:
    """db/tls/cred AND the three token files leave $K3S_SERVER_DIR."""
    t = _tree(tmp_path)
    env = _env(tmp_path, t)

    proc = _run(env, 'backup_and_wipe_identity join > "$SPATIUM_RELEASE_STATE/bak_path"')
    assert proc.returncode == 0, proc.stderr

    for entry in IDENTITY_ENTRIES:
        assert not (t["server"] / entry).exists(), (
            f"{entry} survived the wipe — a stale self-generated token here is "
            "exactly what makes k3s refuse the join with 'bootstrap data already "
            "found and encrypted with different token' (#590)"
        )

    bak = Path((Path(env["SPATIUM_RELEASE_STATE"]) / "bak_path").read_text().strip())
    for entry in IDENTITY_ENTRIES:
        assert (bak / f"server-{entry}").exists(), f"{entry} was not backed up"
    # Never rm'd — the rollback path depends on these.
    assert (bak / "server-token").read_text() == "K10self::server:ownsecret"


def test_wipe_keeps_baked_airgap_images(tmp_path: Path) -> None:
    """agent/images/ holds the baked airgap tarballs — re-pulling is not an
    option on an air-gapped appliance."""
    t = _tree(tmp_path)
    proc = _run(_env(tmp_path, t), "backup_and_wipe_identity join >/dev/null")
    assert proc.returncode == 0, proc.stderr

    assert (t["agent"] / "images" / "airgap.tar").read_text() == "baked"
    assert not (t["agent"] / "client-kubelet.crt").exists()
    assert not t["node_pw"].exists()
    assert not t["kubeconfig"].exists()


def test_restore_round_trips_every_identity_entry(tmp_path: Path) -> None:
    """A failed join rolls back to the prior single-node seed — including the
    token files, or the node could not re-form its own cluster."""
    t = _tree(tmp_path)
    env = _env(tmp_path, t)

    proc = _run(
        env,
        'bak="$(backup_and_wipe_identity join)"\n' 'restore_identity "$bak"',
    )
    assert proc.returncode == 0, proc.stderr

    assert (t["server"] / "token").read_text() == "K10self::server:ownsecret"
    assert (t["server"] / "agent-token").read_text() == "K10self::agent:ownsecret"
    assert (t["server"] / "node-token").read_text() == "K10self::node:ownsecret"
    assert (t["server"] / "db" / "state.db").read_text() == "etcd"
    assert (t["server"] / "tls" / "server-ca.crt").read_text() == "CA"
    assert t["node_pw"].read_text() == "nodepw"
    assert t["kubeconfig"].read_text() == "kubeconfig"


def test_wipe_tolerates_absent_entries(tmp_path: Path) -> None:
    """A node that never seeded its own cluster has no token/db — the wipe is
    still a no-op success, and a join must not abort on it."""
    t = _tree(tmp_path)
    for entry in ("token", "agent-token", "node-token"):
        (t["server"] / entry).unlink()

    proc = _run(_env(tmp_path, t), "backup_and_wipe_identity join >/dev/null")
    assert proc.returncode == 0, proc.stderr


def test_restore_is_a_noop_without_a_backup(tmp_path: Path) -> None:
    """restore_identity is best-effort — an empty/missing backup dir must not
    fail the rollback path that calls it."""
    t = _tree(tmp_path)
    proc = _run(_env(tmp_path, t), 'restore_identity ""\nrestore_identity /nonexistent')
    assert proc.returncode == 0, proc.stderr
    # Nothing was clobbered.
    assert (t["server"] / "token").read_text() == "K10self::server:ownsecret"


def test_identity_entries_list_matches_this_test(tmp_path: Path) -> None:
    """Guard the wipe list itself: the script and this test must not drift."""
    t = _tree(tmp_path)
    proc = _run(_env(tmp_path, t), 'printf "%s\\n" $K3S_IDENTITY_ENTRIES')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == list(IDENTITY_ENTRIES)
