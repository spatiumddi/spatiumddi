"""The device-mapper teardown is scoped to the target disk (issue #995 item 10).

Before this, the pre-partition cleanup ran::

    for dm in $(dmsetup ls --target linear | awk '{print $1}'); do
        dmsetup remove "$dm" || true
    done

— every linear device-mapper map on the machine, not just the ones
backed by the disk about to be wiped. Installing the appliance onto one
disk of a box that also has LVM on another (the ordinary case when
someone adds SpatiumDDI to existing storage) tore down volume groups the
operator intended to keep, yanking live filesystems out from under
whatever was using them.

``_dm_maps_on_target`` is pure text processing over ``lsblk`` and
``dmsetup`` output, so unlike the rest of ``do_install`` it can be
exercised for real: the two commands are stubbed and the function is
extracted and run. That matters more here than anywhere else in the
installer, because the failure mode of getting it wrong is destroying
somebody else's data, and a structural "the string 'deps' appears in the
file" test would not have caught an inverted comparison.

HOW TO RUN (from the repo root):
    python3 -m pytest appliance/tests/test_install_dm_scope.py -v
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from _installer_source import CODE, INSTALLER, extract_fn as _extract


# A box with two disks and a stacked target:
#
#   /dev/sda  sda1 sda2 sda3       <- the install target
#             sda3 is a LUKS PV -> map "crypt_t"      (/dev/dm-0)
#             VG on that        -> map "vgt-lv"       (/dev/dm-1)
#   /dev/sdb  sdb1               <- the operator's OTHER disk
#             sdb1 is a PV      -> map "vgkeep-data"  (/dev/dm-2)
#
# The stack is the interesting part: vgt-lv's only dependency is dm-0, so
# a one-level check would leave it in place, `dmsetup remove crypt_t`
# would fail on the busy device, and the wipe would abort.
#
# lsblk WITHOUT -d walks holders, not just partitions ("-d, --nodeps —
# don't print slaves or holders"), so the dm-N rows below are not
# decoration: they are the input the first cut of this helper was not
# written against, and their absence from this fixture is exactly why
# eight passing tests were exercising a function that returned nothing on
# real hardware. TYPE is carried because the seed has to tell a partition
# from a holder.
TOPOLOGY = {
    "sda": ["sda", "sda1", "sda2", "sda3", "dm-0", "dm-1"],
    "sdb": ["sdb", "sdb1", "dm-2"],
}
MAPS = {
    "crypt_t": ("dm-0", "sda3"),
    "vgt-lv": ("dm-1", "dm-0"),
    "vgkeep-data": ("dm-2", "sdb1"),
}


def _stub_dir(tmp_path: Path, maps: dict, topology: dict) -> Path:
    d = tmp_path / "stub"
    d.mkdir()

    # `lsblk -nro KNAME <disk>` — one kernel name per row, holders included.
    lsblk = ["#!/bin/sh", 'disk=$(eval echo \\$$#)', "case \"$disk\" in"]
    for disk, knames in topology.items():
        lsblk.append(f"  /dev/{disk}) printf '%s\\n' " + " ".join(knames) + " ;;")
    lsblk += ["  *) exit 1 ;;", "esac"]
    (d / "lsblk").write_text("\n".join(lsblk) + "\n")

    ls_out = "".join(
        f"{n}\\t(253:{dm.split('-')[1]})\\n" for n, (dm, _d) in maps.items()
    )
    deps = "\n".join(
        f"    {name}) echo '1 dependencies  : ({dep})' ;;"
        for name, (_dm, dep) in maps.items()
    )
    (d / "dmsetup").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        if [ "$1" = "ls" ]; then printf '{ls_out}'; exit 0; fi
        if [ "$1" = "deps" ]; then
          name=$(eval echo \\$$#)
          case "$name" in
        {deps}
          esac
          exit 0
        fi
        exit 0
        """))

    for f in d.iterdir():
        f.chmod(0o755)
    return d


def _run(tmp_path: Path, target: str, maps=None, topology=None) -> list[str]:
    stub = _stub_dir(tmp_path, maps or MAPS, topology or TOPOLOGY)
    script = tmp_path / "run.sh"
    script.write_text(
        "set -uo pipefail\n"
        # log() writes to $INSTALL_LOG in the real script, never stdout —
        # stub it so a depth-bound warning cannot land in the map list.
        'log() { echo "LOG: $*" >&2; }\n'
        + _extract("_dm_maps_on_target")
        + f'\n_dm_maps_on_target "{target}"\n'
    )
    r = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={"PATH": f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


@pytest.fixture(autouse=True)
def _needs_bash():
    if not shutil.which("bash"):
        pytest.skip("bash not available")


def test_maps_backed_by_the_target_are_listed(tmp_path):
    assert set(_run(tmp_path, "/dev/sda")) == {"crypt_t", "vgt-lv"}


def test_another_disks_volume_group_is_left_alone(tmp_path):
    """The whole point. Installing to /dev/sda must not report — and so
    must not remove — the map backed by /dev/sdb."""
    assert "vgkeep-data" not in _run(tmp_path, "/dev/sda")


def test_the_scoping_works_in_the_other_direction_too(tmp_path):
    """Control for the test above: with sdb as the target, sdb's map IS
    listed and sda's are not. Without this, a function that returned a
    hardcoded list would pass the first two tests.
    """
    assert _run(tmp_path, "/dev/sdb") == ["vgkeep-data"]


def test_stacked_maps_are_ordered_outermost_first(tmp_path):
    """``dmsetup remove`` refuses a device another map sits on, so the LV
    has to come before the crypt device it depends on."""
    out = _run(tmp_path, "/dev/sda")
    assert out.index("vgt-lv") < out.index("crypt_t"), out


def test_a_disk_with_no_maps_yields_nothing(tmp_path):
    topology = dict(TOPOLOGY, sdc=["sdc", "sdc1"])
    assert _run(tmp_path, "/dev/sdc", topology=topology) == []


def test_an_unknown_disk_yields_nothing(tmp_path):
    """lsblk exits non-zero. The safe answer is an empty list: the maps
    stay, the following `wipefs` fails on the busy device, and the
    install aborts before writing anything. Silence costs an aborted
    install; the old behaviour cost someone else's volume group.
    """
    assert _run(tmp_path, "/dev/sdz") == []


def test_no_duplicates_across_closure_passes(tmp_path):
    """The closure re-scans until it stops finding new maps; a map must
    not be emitted once per pass or `dmsetup remove` runs twice."""
    out = _run(tmp_path, "/dev/sda")
    assert len(out) == len(set(out)), out


def test_three_deep_stack_is_fully_walked(tmp_path):
    maps = {
        "crypt_t": ("dm-0", "sda3"),
        "vgt-lv": ("dm-1", "dm-0"),
        "vgt-snap": ("dm-2", "dm-1"),
        "vgkeep-data": ("dm-3", "sdb1"),
    }
    out = _run(tmp_path, "/dev/sda", maps=maps)
    assert set(out) == {"crypt_t", "vgt-lv", "vgt-snap"}
    assert out.index("vgt-snap") < out.index("vgt-lv") < out.index("crypt_t"), out


def test_the_unscoped_loop_is_gone(tmp_path):
    """Structural backstop: the old brute-force line must not come back.

    The executable tests above prove the new helper is right; they cannot
    prove it is the thing do_install calls.
    """
    # Comment-stripped: the helper's own docstring quotes the old line to
    # explain what it replaced, and matching that is a false positive.
    code = CODE
    assert "dmsetup ls --target linear" not in code, (
        "the unscoped device-mapper teardown is back"
    )
    assert "_dm_maps_on_target" in code
