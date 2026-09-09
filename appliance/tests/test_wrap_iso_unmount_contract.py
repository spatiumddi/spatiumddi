"""Two contracts the #1039 unmount fix depends on, neither self-evident.

`wrap-iso.sh` and `build-slot-image.sh` share `mount-lib.sh`'s `unmount_tree`,
whose whole selling point is that it PROVES the tree is gone before a
`rm -rf`. Review of the original patch found both halves of that claim broken:

1. `cleanup()` captured `$?` itself, but over the chroot window the trap is
   COMPOSITE — `trap 'cleanup_chroot; cleanup' EXIT` — so `$?` was
   `cleanup_chroot`'s status, pinned to 0 by its own `|| true`. Every failure
   between the bind mounts and the squashfs exited 0, including the three
   refusals the patch newly added and the two that predate it. `main` exits 1
   correctly, so it was a regression, in the window that contains the guards.

2. `unmount_tree` early-returned success when its argument was not itself a
   mount point, and the `findmnt -R` proof was blind the same way — so a
   directory with live mounts beneath it reported clean. `findmnt -R` cannot
   answer that question: it resolves the path to its own mountpoint and
   reports nothing for a plain directory.

Both are asserted against the SHIPPED scripts, with the mount half exercised
against real mounts where available.

    python3 -m pytest appliance/tests/test_wrap_iso_unmount_contract.py -v
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "appliance" / "scripts"
WRAP = SCRIPTS / "wrap-iso.sh"
MOUNT_LIB = SCRIPTS / "mount-lib.sh"

pytestmark = pytest.mark.skipif(
    not WRAP.exists() or not MOUNT_LIB.exists(),
    reason="appliance scripts not present in this checkout",
)

#: `cleanup()` ends in `rm -rf --one-file-system` (#553), which is GNU-only —
#: BSD rm exits 64 on it. The builder and the CI runner are both GNU; a macOS
#: dev box is not, and without this the exit-status tests fail there for a
#: reason that has nothing to do with what they assert. Skipping beats a
#: misleading red, and the check is explicit so nobody "fixes" it by weakening
#: the assertion.
_GNU_RM = (
    subprocess.run(
        ["sh", "-c", "rm -rf --one-file-system /nonexistent-gnu-rm-probe"],
        capture_output=True,
    ).returncode
    == 0
)


def _block(path: Path, pattern: str, what: str) -> str:
    m = re.search(pattern, path.read_text(encoding="utf-8"), re.S | re.M)
    assert m, f"{path.name} no longer contains {what}"
    return m.group(0)


# ── 1. the exit status must survive the composite trap ───────────────────────


@pytest.mark.skipif(not _GNU_RM, reason="cleanup() needs GNU rm --one-file-system")
@pytest.mark.parametrize(
    ("mode", "want"),
    [("setE", 1), ("guard", 1), ("ok", 0)],
)
def test_failures_in_the_chroot_window_do_not_exit_zero(mode: str, want: int) -> None:
    """Runs the SHIPPED cleanup + chroot trap, not a paraphrase of them."""
    cleanup = _block(WRAP, r"^cleanup\(\) \{.*?^\}$", "cleanup()")
    chroot = _block(WRAP, r"^cleanup_chroot\(\) \{.*?^\}$", "cleanup_chroot()")
    composite = _block(
        WRAP, r"^trap '.*cleanup_chroot.*' EXIT$", "the composite chroot-window trap"
    )
    script = f"""
set -euo pipefail
WORKDIR=$(mktemp -d); MOUNT_DIR=
unmount_tree() {{ return 0; }}
{cleanup}
trap cleanup EXIT
{chroot}
{composite}
case "{mode}" in
  setE)  false ;;
  guard) echo "ERROR: refusing to squash" >&2; exit 1 ;;
  ok)    : ;;
esac
exit 0
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == want, (
        f"mode={mode} exited {proc.returncode}, want {want} — the composite trap "
        f"is swallowing the status again\n{proc.stdout}{proc.stderr}"
    )


def test_the_composite_trap_passes_the_status_in() -> None:
    """Structural backstop: the shape that made the bug possible must not return.

    `trap 'cleanup_chroot; cleanup' EXIT` with a `$?`-reading cleanup is the
    broken pairing; the status has to be captured before cleanup_chroot runs.
    """
    composite = _block(WRAP, r"^trap '.*cleanup_chroot.*' EXIT$", "the chroot trap")
    assert "rc=$?" in composite, (
        f"the composite trap must capture $? before cleanup_chroot: {composite}"
    )


# ── 2. unmount_tree must see mounts BELOW a plain directory ──────────────────


def test_unmount_tree_does_not_early_return_on_a_non_mountpoint() -> None:
    """The `mountpoint -q … || return 0` guard is what made it blind."""
    body = MOUNT_LIB.read_text(encoding="utf-8")
    fn = _block(MOUNT_LIB, r"^unmount_tree\(\) \{.*?^\}$", "unmount_tree()")
    assert 'mountpoint -q "$mp" 2>/dev/null || return 0' not in fn, (
        "unmount_tree early-returns success when $mp is not itself a mount "
        "point, leaving any mount beneath it live"
    )
    assert "anything_mounted_under" in body, (
        "mount-lib no longer has a /proc/mounts check that can see sub-mounts"
    )


@pytest.mark.skipif(shutil.which("mount") is None, reason="no mount(8) available")
@pytest.mark.skipif(
    subprocess.run(["sh", "-c", "[ -r /proc/mounts ]"]).returncode != 0,
    reason="no /proc/mounts (not Linux)",
)
def test_unmount_tree_clears_a_submount_under_a_plain_directory(tmp_path: Path) -> None:
    """The behavioural half — needs real mounts, so it skips off Linux/CI-root.

    Verified in a privileged container during the fix: the original left the
    tmpfs mounted and returned 0; this clears it.
    """
    target = tmp_path / "work" / "mnt"
    target.mkdir(parents=True)
    mounted = subprocess.run(
        ["mount", "-t", "tmpfs", "none", str(target)], capture_output=True, text=True
    )
    if mounted.returncode != 0:
        pytest.skip(f"cannot mount here: {mounted.stderr.strip()}")
    try:
        script = f'. "{MOUNT_LIB}"\nunmount_tree "{tmp_path / "work"}"'
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        still = subprocess.run(
            ["sh", "-c", f"grep -F ' {target} ' /proc/mounts"], capture_output=True
        )
        assert proc.returncode == 0, f"unmount_tree failed: {proc.stderr}"
        assert still.returncode != 0, "the sub-mount is still live after a success return"
    finally:
        subprocess.run(["umount", "-l", "-R", str(target)], capture_output=True)
