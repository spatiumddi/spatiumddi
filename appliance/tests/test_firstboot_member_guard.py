"""spatiumddi-firstboot must not re-seed manifests on a joined member (#590).

``spatiumddi-firstboot`` runs on EVERY boot and has no systemd ordering
against ``k3s.service`` — both start together (observed live: both at
17:52:25). On a node that has joined another node's cluster it would
re-render its own single-node manifests into
``/var/lib/rancher/k3s/server/manifests/`` while k3s comes up, and k3s's
deploy controller applies them CLUSTER-WIDE. The ``Addon`` CR is one
cluster-scoped object per filename, so whichever node writes last wins.

Observed live on a 3-node appliance after a simultaneous hard power cut of
all three VMs: every node raced, a member's ``spatium-bootstrap.yaml`` won,
and its values (``cnpg: false`` + ``agentLanding: true``) UNINSTALLED the
CloudNativePG operator on a healthy cluster and left an unschedulable nginx
pod contending for the frontend's :80. The Postgres ``Cluster`` CR went on
reporting "Cluster in healthy state" because no operator remained to update
it — a silent loss of failover, which is exactly the class of fault #590
exists to remove.

``spatium-cluster-join`` already moves these manifests aside at join time.
These guards are what stop firstboot from putting them back.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_firstboot_member_guard.py -v

No k3s, no appliance required — the predicate reads a path we override.
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
    / "spatiumddi-firstboot"
)

JOIN_SCRIPT = (
    Path(__file__).parent.parent
    / "mkosi.extra"
    / "usr"
    / "local"
    / "bin"
    / "spatium-cluster-join"
)


def _is_member(dropin: Path) -> bool:
    """Source the real script as a library and run its member predicate."""
    proc = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"\nnode_is_cluster_member'],
        env={
            **os.environ,
            "SPATIUM_FIRSTBOOT_LIB": "1",
            "SPATIUM_K3S_JOIN_DROPIN": str(dropin),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode in (0, 1), proc.stderr
    return proc.returncode == 0


def test_lib_guard_runs_no_boot_side_effects(tmp_path: Path) -> None:
    """Sourcing with SPATIUM_FIRSTBOOT_LIB=1 must return before the script
    starts mkdir'ing state dirs and redirecting stdout into the boot log —
    otherwise these tests would scribble on the developer's machine."""
    proc = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"\necho SOURCED_CLEANLY'],
        env={**os.environ, "SPATIUM_FIRSTBOOT_LIB": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    # stdout still ours: the script's `exec >>"$LOG"` never ran.
    assert "SOURCED_CLEANLY" in proc.stdout


def test_a_standalone_node_is_not_a_member(tmp_path: Path) -> None:
    """No join drop-in → this node owns its own cluster and MUST render its
    bootstrap manifests, or nothing ever deploys on a fresh install."""
    assert _is_member(tmp_path / "spatium-cluster.yaml") is False


def test_a_joined_node_is_a_member(tmp_path: Path) -> None:
    """The drop-in is written only by a SUCCESSFUL join."""
    dropin = tmp_path / "spatium-cluster.yaml"
    dropin.write_text("server: https://10.0.0.1:6443\ntoken: redacted\n")
    assert _is_member(dropin) is True


def _sourced_var(script: Path, lib_env: str, var: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", f'source "{script}"\nprintf %s "${var}"'],
        env={**os.environ, lib_env: "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_predicate_matches_the_join_scripts_own_dropin_path() -> None:
    """REGRESSION GUARD. firstboot's predicate and spatium-cluster-join's
    JOIN_DROPIN must resolve to the same file. If the join script ever renames
    its drop-in, firstboot silently reverts to 'never a member' and starts
    clobbering the seed's releases again on every member reboot.

    Resolved by sourcing both scripts rather than grepping for a literal — the
    join script composes its path from a dir + basename, so no literal exists.
    """
    firstboot_path = _sourced_var(SCRIPT, "SPATIUM_FIRSTBOOT_LIB", "K3S_JOIN_DROPIN")
    join_path = _sourced_var(JOIN_SCRIPT, "SPATIUM_CLUSTER_JOIN_LIB", "JOIN_DROPIN")
    assert firstboot_path, "firstboot exposes no K3S_JOIN_DROPIN"
    assert (
        firstboot_path == join_path
    ), f"firstboot watches {firstboot_path!r} but the join writes {join_path!r}"


def _writer_guarded(marker: str) -> bool:
    """True when `marker` (a line that WRITES into the k3s auto-deploy dir)
    is preceded by a node_is_cluster_member guard with no intervening writer.

    Structural, deliberately: the writers are inline in a boot script that
    also configures swap, docker and k3s, so exercising them for real would
    mean booting an appliance. The predicate itself is covered behaviourally
    above; this asserts it is actually wired in front of each writer.
    """
    lines = SCRIPT.read_text().splitlines()
    idx = next(i for i, ln in enumerate(lines) if marker in ln)
    window = lines[max(0, idx - 12) : idx + 1]
    return any("node_is_cluster_member" in ln for ln in window)


def test_tls_secret_writer_is_guarded() -> None:
    """On a member the ``[ ! -f "$TLS_CERT_MANIFEST" ]`` test is ALWAYS true
    (the join moved that manifest aside), so an unguarded firstboot mints a
    fresh self-signed cert every reboot and overwrites the cluster's shared
    ``spatium-appliance-tls`` Secret — silently replacing an operator-uploaded
    or ACME-issued cert with a throwaway one."""
    assert _writer_guarded('elif [ ! -f "$TLS_CERT_MANIFEST" ]; then')


def test_bootstrap_manifest_render_is_guarded() -> None:
    """The render must dispatch on BOOTSTRAP_ROLE, which a member pins to
    ``joined-member`` so no role branch renders. It must NOT dispatch on
    APPLIANCE_ROLE_VAL, which stays ``application`` on a promoted member and
    would render the agentLanding/no-cnpg values into the shared cluster."""
    body = SCRIPT.read_text()
    assert 'case "$BOOTSTRAP_ROLE" in' in body
    assert "    joined-member)" in body
    assert 'case "$APPLIANCE_ROLE_VAL" in' not in body
    # …and the pin happens between the assignment and the dispatch.
    between = body[
        body.index('BOOTSTRAP_ROLE="$APPLIANCE_ROLE_VAL"') : body.index(
            'case "$BOOTSTRAP_ROLE" in'
        )
    ]
    assert "node_is_cluster_member" in between
    assert 'BOOTSTRAP_ROLE="joined-member"' in between


def test_member_guard_skips_rather_than_deletes() -> None:
    """A manifest REMOVED while k3s runs fires fsnotify → k3s deletes the
    Addon → the wrangler ``on-helm-chart-remove`` finalizer UNINSTALLS the
    release, taking the supervisor and CNPG operator with it. firstboot races
    k3s, so it may only ever skip writing. (``spatium-cluster-join`` is
    allowed to move manifests aside because it stops k3s first.)"""
    body = SCRIPT.read_text()
    idx = body.index("    joined-member)")
    branch = body[idx : body.index(";;", idx)]
    for verb in ("rm ", "mv ", "unlink"):
        assert verb not in branch, f"joined-member branch must not {verb.strip()!r}"


def test_join_script_stops_k3s_before_moving_manifests() -> None:
    """The asymmetry above only holds while do_join keeps stopping k3s first."""
    body = JOIN_SCRIPT.read_text()
    do_join = body[body.index("do_join()") :]
    stop = do_join.index("systemctl stop k3s")
    move = do_join.index('mv -f "$K3S_MANIFESTS"/spatium-*.yaml')
    assert stop < move, "do_join must stop k3s before moving manifests aside"
