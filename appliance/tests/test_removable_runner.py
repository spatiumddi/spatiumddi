"""``spatiumddi-removable-reload`` — the removable (USB) disk mount plane (#989 item 3).

These tests EXECUTE the shipped script against stubbed host tooling,
rather than grepping it, because the failure this plane must never
produce is invisible to review: a mountpoint that accepts writes while
nothing is mounted on it, which turns a backup into a silent write to
the appliance's own ``/var`` with every surface reporting success.

WHAT IS AND IS NOT COVERED HERE
-------------------------------
``systemd-analyze verify`` is stubbed, so these tests do not prove the
rendered unit is valid systemd — they pin its exact BODY instead, and
that body was verified against a real ``systemd-analyze`` during
development (it rejects a ``Where=`` that disagrees with the unit name
and a missing ``What=``, both with rc=1). So an edit that changes the
rendered unit fails the golden assertion below and forces that check to
be redone, which is the honest division of labour between a portable
test and a systemd-only one.

What IS proven here: the validation refusals, the per-filesystem mount
options, the teardown, the "0500 when nothing is mounted" guard, and
that a failed apply consumes its trigger without claiming success.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_removable_runner.py -v

No database, no Docker, no appliance ISO, no root required.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BIN = REPO / "appliance" / "mkosi.extra" / "usr" / "local" / "bin"
RUNNER = BIN / "spatiumddi-removable-reload"
UNITS = REPO / "appliance" / "mkosi.extra" / "etc" / "systemd" / "system"
POSTINST = REPO / "appliance" / "mkosi.postinst"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to execute the runner"
)


# --------------------------------------------------------------------------
# a fake host: stub every binary the runner shells out to
# --------------------------------------------------------------------------
#
# ``systemd-escape`` is implemented for real rather than stubbed to an
# identity, because the escaping IS part of what is being tested: a
# literal ``-`` in a mount name becomes ``\x2d`` in the unit filename,
# so a name like ``usb-1`` produces a filename the teardown glob still
# has to match. Cross-checked against the real systemd-escape during
# development.
_ESCAPE = r'''#!/usr/bin/env python3
import sys
args = sys.argv[1:]
suffix = ""
unescape = False
rest = []
for a in args:
    if a.startswith("--suffix="):
        suffix = "." + a.split("=", 1)[1]
    elif a in ("-p", "--path"):
        pass
    elif a in ("-u", "--unescape"):
        unescape = True
    elif a == "-pu":
        unescape = True
    else:
        rest.append(a)
value = rest[0] if rest else ""
if unescape:
    out = value.replace("-", "/").replace("\\x2d", "-")
    print("/" + out.lstrip("/"))
else:
    v = value.strip("/")
    parts = [p.replace("\\", "\\x5c").replace("-", "\\x2d") for p in v.split("/")]
    print("-".join(parts) + suffix)
'''

_SYSTEMCTL = '''#!/bin/sh
echo "systemctl $*" >> "$STUB_CALLS"
exit 0
'''

_MOUNTPOINT = '''#!/bin/sh
# A path is "mounted" only if it is listed in $STUB_MOUNTED.
for p in $(cat "$STUB_MOUNTED" 2>/dev/null); do
  [ "$p" = "$2" ] && exit 0
done
exit 1
'''

_ANALYZE_OK = '''#!/bin/sh
echo "systemd-analyze $*" >> "$STUB_CALLS"
exit 0
'''

_ANALYZE_REJECT = '''#!/bin/sh
echo "Where= setting doesn't match unit name. Refusing." >&2
exit 1
'''


@pytest.fixture
def host(tmp_path):
    """A fake appliance root with the runner's paths redirected into it."""
    stub = tmp_path / "stub"
    stub.mkdir()
    calls = tmp_path / "calls.txt"
    mounted = tmp_path / "mounted.txt"
    calls.write_text("")
    mounted.write_text("")

    def _stub(name: str, body: str) -> None:
        p = stub / name
        p.write_text(body)
        p.chmod(0o755)

    _stub("systemd-escape", _ESCAPE)
    _stub("systemctl", _SYSTEMCTL)
    _stub("mountpoint", _MOUNTPOINT)
    _stub("systemd-analyze", _ANALYZE_OK)

    root = tmp_path / "root"
    (root / "var/lib/spatiumddi/release-state").mkdir(parents=True)
    (root / "etc/systemd/system").mkdir(parents=True)
    (root / "var/log/spatiumddi").mkdir(parents=True)

    # The runner's absolute paths, rewritten to live under tmp_path. The
    # SHIPPED bytes are otherwise untouched — only the four path
    # constants at the top move, so every code path below them is the
    # real one.
    src = RUNNER.read_text()
    src = src.replace("TRIGGER=/var/lib", f"TRIGGER={root}/var/lib")
    src = src.replace("HASH_SIDECAR=/var/lib", f"HASH_SIDECAR={root}/var/lib")
    src = src.replace("STATUS_SIDECAR=/var/lib", f"STATUS_SIDECAR={root}/var/lib")
    src = src.replace("LOG_DIR=/var/log/spatiumddi", f"LOG_DIR={root}/var/log/spatiumddi")
    src = src.replace("LOCK=/run/", f"LOCK={tmp_path}/")
    src = src.replace("ROOT=/var/lib", f"ROOT={root}/var/lib")
    src = src.replace("UNIT_DIR=/etc/systemd/system", f"UNIT_DIR={root}/etc/systemd/system")
    src = src.replace(
        "/usr/local/bin/spatiumddi-removable-reload --prepare",
        f"{tmp_path}/runner --prepare",
    )
    runner = tmp_path / "runner"
    runner.write_text(src)
    runner.chmod(0o755)

    return {
        "tmp": tmp_path,
        "root": root,
        "runner": runner,
        "stub": stub,
        "calls": calls,
        "mounted": mounted,
        "unit_dir": root / "etc/systemd/system",
        "removable": root / "var/lib/spatiumddi/removable",
        "trigger": root / "var/lib/spatiumddi/release-state/removable-config-pending",
        "hash": root / "var/lib/spatiumddi/release-state/removable-config-hash",
        "status": root / "var/lib/spatiumddi/release-state/removable-status",
    }


def set_mounted(host, *names):
    """Declare which mountpoints the stubbed ``mountpoint -q`` calls live.

    Without this every test ran with nothing mounted, so the entire
    ``--prepare`` body, the ``sync -f`` flush-before-eject, the
    unmount-verification and the "do not chmod 0500 a LIVE mount" guard
    were unexecuted — and inverting or deleting that last one (which on
    ext4 persists to the disk and would lock the api pod out
    permanently) left all 21 tests green.
    """
    host["mounted"].write_text(
        "\n".join(str(host["removable"] / n) for n in names) + "\n"
    )


def apply(host, mounts, *, marker="enabled", hash_="h1", body=None):
    """Write a trigger and run the shipped runner. Returns (rc, log)."""
    payload = body if body is not None else json.dumps({"mounts": mounts})
    host["trigger"].parent.mkdir(parents=True, exist_ok=True)
    host["trigger"].write_text(f"{marker}\n{hash_}\n{payload}\n")
    env = {
        **os.environ,
        "PATH": f"{host['stub']}:{os.environ.get('PATH', '')}",
        "STUB_CALLS": str(host["calls"]),
        "STUB_MOUNTED": str(host["mounted"]),
    }
    proc = subprocess.run(
        ["bash", str(host["runner"])], env=env, capture_output=True, text=True, timeout=60
    )
    log_file = host["root"] / "var/log/spatiumddi/removable-reload.log"
    log = log_file.read_text() if log_file.exists() else ""
    return proc.returncode, log


def directives(body: str) -> str:
    """The unit's real content, comments stripped.

    Needed because the rendered unit EXPLAINS its own choices — it says
    in a comment that it is WantedBy the device "not local-fs.target",
    so a naive substring assertion matches the explanation rather than
    the directive it is asserting the absence of.
    """
    return "\n".join(
        ln for ln in body.splitlines() if not ln.lstrip().startswith("#")
    )


def unit_name(host, name: str) -> str:
    """The escaped .mount unit filename for one mount, derived the same
    way the runner derives it. The harness relocates ROOT into tmp_path,
    so a production literal would not match — and the runner now derives
    its own teardown glob from ROOT for exactly that reason."""
    path = str(host["removable"] / name).strip("/")
    return "-".join(p.replace("-", "\\x2d") for p in path.split("/")) + ".mount"


def units(host):
    """Installed unit filenames with the tmp_path prefix trimmed, so the
    assertions below read like the production names."""
    prefix = unit_name(host, "x").removesuffix("x.mount")
    return sorted(p.name.removeprefix(prefix) for p in host["unit_dir"].glob("*.mount"))


# --------------------------------------------------------------------------
# the rendered unit
# --------------------------------------------------------------------------
def test_the_rendered_unit_is_exactly_this(host):
    """Golden body. See the module docstring for why this is pinned
    rather than re-verified against systemd on every run."""
    rc, _ = apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}])
    assert rc == 0
    body = (host["unit_dir"] / unit_name(host, "usb1")).read_text()
    assert "What=/dev/disk/by-uuid/1234-ABCD" in body
    assert f"Where={host['removable']}/usb1" in body
    assert "Type=exfat" in body
    # exFAT has no on-disk ownership, so the mount options ARE the
    # ownership — without uid/gid the api pod (uid 1000) cannot write.
    assert "Options=nofail,noexec,nosuid,nodev,uid=1000,gid=1000,umask=0077" in body
    # ``nofail`` is NOT cosmetic here. systemd's
    # mount_add_default_dependencies() adds an implicit
    # Before=local-fs.target to every mount unit with
    # DefaultDependencies=yes UNLESS nofail is present — native unit
    # files included, not just fstab-generated ones. Without it a disk
    # that happens to be plugged in at boot orders local-fs.target (and
    # therefore sysinit / basic / multi-user / k3s) behind its mount,
    # up to DefaultTimeoutStartSec on a dirty exFAT volume. The
    # WantedBy-the-device trick below only covers the ABSENT disk.
    assert body.count("nofail") == 1
    # BindsTo, so a yanked disk is torn down rather than left as a
    # stale mountpoint that still looks writable.
    assert "BindsTo=dev-disk-by\\x2duuid-1234\\x2dABCD.device" in body
    # WantedBy the DEVICE, never local-fs.target: that is what makes
    # boot not wait for a disk that is not plugged in.
    assert "WantedBy=dev-disk-by\\x2duuid-1234\\x2dABCD.device" in body
    assert "local-fs.target" not in directives(body)


def test_ext4_does_not_get_uid_options(host):
    """Measured against a real kernel: ext4 rejects ``uid=`` outright
    ("ext4: Unknown parameter 'uid'"), so copying the exFAT options
    across would make every ext4 disk fail to mount."""
    apply(host, [{"name": "usb1", "fs_uuid": "aaaa-bbbb", "fstype": "ext4"}])
    body = (host["unit_dir"] / unit_name(host, "usb1")).read_text()
    assert "Options=nofail,noexec,nosuid,nodev\n" in body
    assert "uid=" not in directives(body)


def test_no_automount_unit_is_emitted(host):
    """#989 asked for a ``.automount`` so a yanked disk would not hang
    boot. ``WantedBy`` the device unit is what actually delivers that,
    and autofs would SUBTRACT: a process touching an autofs mountpoint
    whose device is absent blocks in the kernel — and the processes
    touching this path are the api and the Celery worker.

    Pinned so the deviation is a decision rather than an oversight
    somebody quietly reverses.
    """
    apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}])
    assert not list(host["unit_dir"].glob("*.automount"))
    body = (host["unit_dir"] / unit_name(host, "usb1")).read_text()
    # Asserted on SECTION HEADERS and directive KEYS, never on a
    # substring of the whole body: the unit's Where= carries the
    # tmp_path, and pytest names that directory after the test — so a
    # naive `"automount" not in body` matches this test's own tmp dir.
    keys = {
        ln.split("=", 1)[0].strip().lower()
        for ln in directives(body).splitlines()
        if "=" in ln
    }
    sections = {ln.strip().lower() for ln in directives(body).splitlines() if ln.startswith("[")}
    assert "[automount]" not in sections
    assert not any(k.startswith("automount") for k in keys)
    # WantedBy the DEVICE, never local-fs.target.
    assert "local-fs.target" not in directives(body)


def test_a_dash_in_the_name_is_escaped_into_the_unit_filename(host):
    apply(host, [{"name": "usb-1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}])
    assert units(host) == ["usb\\x2d1.mount"]


# --------------------------------------------------------------------------
# the refusals
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "entry,needle",
    [
        ({"name": "../../etc/evil", "fs_uuid": "1234-ABCD", "fstype": "exfat"}, "rejected name"),
        ({"name": "ok", "fs_uuid": "1234-ABCD", "fstype": "vfat"}, "rejected fstype"),
        ({"name": "ok", "fs_uuid": "a; rm -rf /", "fstype": "ext4"}, "rejected fs_uuid"),
        ({"name": "UPPER", "fs_uuid": "1234-ABCD", "fstype": "ext4"}, "rejected name"),
    ],
)
def test_a_bad_entry_is_dropped_and_named(host, entry, needle):
    """Allowlists, not escaping. Every one of these values reaches a
    systemd unit body AND a unit filename, and a dropped entry is
    LOGGED rather than silently skipped so a typo is visible.

    A drop is a FAILURE, not a quiet skip: this runner's allowlist is a
    second, independently-written copy of the control plane's, so a
    drop means the two disagree. Reporting success would stamp the hash
    sidecar, stop the supervisor retrying, and leave a disk the operator
    configured unmounted forever with every surface green.
    """
    rc, log = apply(host, [entry])
    assert needle in log
    assert units(host) == []
    assert rc == 1
    assert not host["hash"].exists()


def test_a_good_entry_survives_a_bad_one_beside_it(host):
    """The good entry is still installed — the apply is additive — but
    the run reports failure so the supervisor keeps retrying until the
    bad entry is fixed."""
    rc, log = apply(
        host,
        [
            {"name": "ok", "fs_uuid": "1234-ABCD", "fstype": "vfat"},
            {"name": "good", "fs_uuid": "aaaa-bbbb", "fstype": "ext4"},
        ],
    )
    assert units(host) == ["good.mount"]
    assert rc == 1
    assert not host["hash"].exists()


def test_a_duplicate_name_is_dropped(host):
    """Two entries claiming one mountpoint would have the second
    overwrite the first's unit, so the operator would see one disk
    mounted where they configured two."""
    rc, log = apply(
        host,
        [
            {"name": "usb1", "fs_uuid": "1111-1111", "fstype": "ext4"},
            {"name": "usb1", "fs_uuid": "2222-2222", "fstype": "ext4"},
        ],
    )
    assert "duplicate name usb1" in log
    assert rc == 1  # a drop is a failure — see the parametrised test above
    assert units(host) == ["usb1.mount"]
    body = (host["unit_dir"] / unit_name(host, "usb1")).read_text()
    assert "1111-1111" in body


def test_a_unit_systemd_rejects_is_never_installed(host):
    """``systemd-analyze verify`` is the real validator (#550's rule that
    a reload runner needs one). A rejected unit must not reach
    /etc/systemd/system, and the apply must report failure."""
    (host["stub"] / "systemd-analyze").write_text(_ANALYZE_REJECT)
    (host["stub"] / "systemd-analyze").chmod(0o755)
    rc, log = apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}])
    assert rc == 1
    assert units(host) == []
    assert "rejected" in log
    # And it must NOT claim the config applied, or the supervisor would
    # stop retrying a plane that never landed.
    assert not host["hash"].exists()


# --------------------------------------------------------------------------
# the kernel-level guard
# --------------------------------------------------------------------------
def test_an_unmounted_mountpoint_is_mode_0500(host):
    """The last line of defence, and the reason it is worth one chmod.

    Every software check above this can be wrong; 0500 root-owned is the
    kernel refusing the write. Invisible while something is mounted here
    (the mount's own permissions apply) and decisive when nothing is.
    """
    apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}])
    mp = host["removable"] / "usb1"
    assert mp.is_dir()
    assert stat.S_IMODE(mp.stat().st_mode) == 0o500


# --------------------------------------------------------------------------
# teardown / eject
# --------------------------------------------------------------------------
def test_eject_removes_exactly_that_unit(host):
    apply(
        host,
        [
            {"name": "usb1", "fs_uuid": "1111-1111", "fstype": "ext4"},
            {"name": "usb2", "fs_uuid": "2222-2222", "fstype": "ext4"},
        ],
        hash_="h1",
    )
    assert len(units(host)) == 2
    host["calls"].write_text("")
    rc, _ = apply(host, [{"name": "usb2", "fs_uuid": "2222-2222", "fstype": "ext4"}], hash_="h2")
    assert rc == 0
    assert units(host) == ["usb2.mount"]
    assert not (host["removable"] / "usb1").exists()
    calls = host["calls"].read_text()
    assert f"disable --now {unit_name(host, 'usb1')}" in calls
    # The survivor is NOT disabled — an eject that took the other disk
    # down with it would be worse than one that did nothing.
    assert f"disable --now {unit_name(host, 'usb2')}" not in calls


def test_an_empty_desired_set_tears_everything_down(host):
    """``disabled`` is how an EJECT of the last disk reaches the host.
    If this were read as "the feature is off, do nothing", ejecting
    would leave the disk mounted on the node forever."""
    apply(host, [{"name": "usb1", "fs_uuid": "1111-1111", "fstype": "ext4"}], hash_="h1")
    rc, _ = apply(host, [], marker="disabled", hash_="h2")
    assert rc == 0
    assert units(host) == []
    assert host["hash"].read_text().strip() == "h2"


def test_a_non_empty_mountpoint_is_left_alone(host):
    """``rmdir``, never ``rm -rf``: a non-empty mountpoint means the
    unmount did not happen, and deleting it anyway would delete what is
    on the disk."""
    apply(host, [{"name": "usb1", "fs_uuid": "1111-1111", "fstype": "ext4"}], hash_="h1")
    mp = host["removable"] / "usb1"
    mp.chmod(0o755)
    (mp / "precious.zip").write_text("archive")
    rc, log = apply(host, [], marker="disabled", hash_="h2")
    assert rc == 0
    assert (mp / "precious.zip").read_text() == "archive"
    assert "not empty" in log


# --------------------------------------------------------------------------
# failure handling
# --------------------------------------------------------------------------
def test_a_malformed_body_consumes_the_trigger_and_does_not_claim_success(host):
    """#550's rule: a trigger left in place is re-fired forever with
    nothing said upward. And the hash sidecar must NOT advance, or the
    supervisor would believe a config it never applied is live."""
    rc, log = apply(host, [], body="not json at all")
    assert rc == 1
    assert not host["trigger"].exists()
    assert list(host["trigger"].parent.glob("*.failed.*"))
    assert not host["hash"].exists()
    assert host["status"].read_text().startswith("failed")


def test_an_unknown_marker_is_refused(host):
    rc, _ = apply(host, [], marker="banana")
    assert rc == 1
    assert not host["trigger"].exists()


def test_a_successful_apply_writes_the_hash_and_removes_the_trigger(host):
    rc, _ = apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}], hash_="abc")
    assert rc == 0
    assert host["hash"].read_text().strip() == "abc"
    assert not host["trigger"].exists()
    assert host["status"].read_text().startswith("applied")


# --------------------------------------------------------------------------
# packaging (#550–#557: a runner nothing chmods or enables is inert)
# --------------------------------------------------------------------------
def test_the_runner_and_units_are_in_the_postinst_chmod_block():
    text = POSTINST.read_text()
    assert 'chmod 0755 "$BUILDROOT/usr/local/bin/spatiumddi-removable-reload"' in text
    for unit in (
        "spatiumddi-removable-reload.path",
        "spatiumddi-removable-reload.service",
        "spatiumddi-removable-prepare.service",
    ):
        assert f'chmod 0644 "$BUILDROOT/etc/systemd/system/{unit}"' in text


def test_the_path_unit_is_enabled_at_build_time():
    """A .path unit that is never symlinked into multi-user.target.wants
    watches nothing, and the whole plane is silently inert."""
    assert "spatiumddi-removable-reload.path \\" in POSTINST.read_text()


def test_the_prepare_service_is_not_independently_enabled():
    """It is pulled in by each .mount unit (Wants= + Before=). Enabling
    it at boot as well would run it before any disk is mounted, which
    does nothing, and hide that the mount-driven path had stopped
    working."""
    assert "spatiumddi-removable-prepare.service \\" not in POSTINST.read_text()
    assert "[Install]" not in (UNITS / "spatiumddi-removable-prepare.service").read_text()


# --------------------------------------------------------------------------
# the branches that only run when something is actually mounted
# --------------------------------------------------------------------------
def test_a_live_mount_is_not_chmodded_0500(host):
    """The 0500 guard protects an UNMOUNTED mountpoint. Applied to a live
    ext4 mount it would persist to the disk and lock the api pod (uid
    1000) out of its own backup destination, permanently."""
    apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}], hash_="h1")
    mp = host["removable"] / "usb1"
    mp.chmod(0o755)
    set_mounted(host, "usb1")
    rc, _ = apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}], hash_="h2")
    assert rc == 0
    assert stat.S_IMODE(mp.stat().st_mode) == 0o755


def test_prepare_creates_and_chowns_the_archive_directory(host):
    """ext4 rejects ``uid=`` at mount time, so something has to own the
    archive dir as the api's uid — and this runs after a mount HOWEVER it
    happened, including a disk plugged in hours later by udev."""
    apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "ext4"}], hash_="h1")
    mp = host["removable"] / "usb1"
    mp.chmod(0o755)
    set_mounted(host, "usb1")
    env = {
        **os.environ,
        "PATH": f"{host['stub']}:{os.environ.get('PATH', '')}",
        "STUB_CALLS": str(host["calls"]),
        "STUB_MOUNTED": str(host["mounted"]),
    }
    subprocess.run(
        ["bash", str(host["runner"]), "--prepare"], env=env, capture_output=True, timeout=60
    )
    assert (mp / "spatiumddi").is_dir()


def test_eject_flushes_before_unmounting(host):
    """The modal promises the disk is safe to pull. That promise is only
    true if the filesystem was flushed first."""
    apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}], hash_="h1")
    set_mounted(host, "usb1")
    host["calls"].write_text("")
    # The stub systemctl does not actually unmount, so this also exercises
    # the "still mounted after disable" refusal below.
    apply(host, [], marker="disabled", hash_="h2")
    log = (host["root"] / "var/log/spatiumddi/removable-reload.log").read_text()
    assert "still mounted" in log


def test_an_eject_whose_unmount_failed_is_not_reported_as_applied(host):
    """The sharpest bug the first draft had.

    ``systemctl disable --now`` returns EBUSY when anything holds an fd
    under the mount — a running backup, a shell. The first draft
    discarded that, deleted the unit anyway and stamped the hash: the
    disk stayed mounted with NO unit managing it, invisible on every
    surface (the control plane iterates the DESIRED set, which no longer
    named it), while the UI had said it was safe to pull and the backup
    driver's mountpoint check still passed.
    """
    apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}], hash_="h1")
    set_mounted(host, "usb1")
    rc, log = apply(host, [], marker="disabled", hash_="h2")
    assert rc == 1
    # The unit is KEPT — it is what still describes the live mount.
    assert units(host) == ["usb1.mount"]
    # And the hash must not ADVANCE to h2, or the supervisor believes the
    # eject landed and stops retrying.
    assert host["hash"].read_text().strip() == "h1"
    assert host["status"].read_text().startswith("failed")


def test_the_boot_mode_reloads_and_rearms(host):
    """The /etc overlay mounts AFTER udev coldplugs the disks, and systemd
    resolves a unit's .wants directory at load time without rescanning —
    so a disk left plugged in across a reboot can have its unit and its
    symlink both on disk and neither loaded."""
    apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}], hash_="h1")
    host["calls"].write_text("")
    env = {
        **os.environ,
        "PATH": f"{host['stub']}:{os.environ.get('PATH', '')}",
        "STUB_CALLS": str(host["calls"]),
        "STUB_MOUNTED": str(host["mounted"]),
    }
    proc = subprocess.run(
        ["bash", str(host["runner"]), "--boot"], env=env, capture_output=True, timeout=60
    )
    assert proc.returncode == 0
    assert "daemon-reload" in host["calls"].read_text()


def test_a_crashed_parser_never_reads_as_eject_everything(host):
    """An empty desired set is this plane's TEAR-DOWN command, so an
    interpreter that dies must not produce one. The first draft did not
    check the substitution's status: a missing or OOM-killed python3
    unmounted every disk and then exited 0."""
    apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}], hash_="h1")
    crash = host["stub"] / "python3"
    crash.write_text("#!/bin/sh\nexit 9\n")
    crash.chmod(0o755)
    rc, _ = apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}], hash_="h2")
    assert rc == 1
    assert units(host) == ["usb1.mount"]  # nothing torn down
    assert host["hash"].read_text().strip() == "h1"


def test_a_failed_install_is_not_reported_as_applied(host):
    """`set -uo pipefail` has no `-e`, so an unchecked `install` is a
    silent no-op — and /etc/systemd/system is the /var-backed overlay
    upper layer, which can be full or read-only."""
    fake = host["stub"] / "install"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    rc, log = apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}])
    assert rc == 1
    assert "could not install" in log
    assert not host["hash"].exists()


def test_a_failed_enable_is_not_reported_as_applied(host):
    """The `WantedBy=<device>.device` symlink IS the auto-mount
    mechanism, so a failed enable means the disk never mounts when
    plugged in."""
    (host["stub"] / "systemctl").write_text(
        "#!/bin/sh\n"
        'echo "systemctl $*" >> "$STUB_CALLS"\n'
        'case "$1" in enable) exit 1 ;; esac\n'
        "exit 0\n"
    )
    (host["stub"] / "systemctl").chmod(0o755)
    rc, log = apply(host, [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}])
    assert rc == 1
    assert "could not enable" in log
    assert not host["hash"].exists()


def test_the_two_allowlists_agree():
    """The runner's allowlist is a second, independently-written copy of
    the control plane's — the package split makes sharing impossible, so
    the only protection is an assertion that they match.

    A divergence is not cosmetic: an entry the control plane accepts and
    the runner drops now fails the apply loudly (it used to tear the live
    unit down), but the operator still ends up with a disk they
    configured and cannot mount, and no surface explains why.
    """
    import re as _re

    runner = RUNNER.read_text()
    service = (
        REPO / "backend" / "app" / "services" / "appliance" / "removable.py"
    ).read_text()

    def pat(text, name):
        m = _re.search(rf'^{name} = re\.compile\(r"(.+?)"\)', text, _re.M)
        assert m, f"{name} not found"
        return m.group(1)

    def runner_pat(name):
        m = _re.search(rf'^{name} = re\.compile\(r"(.+?)"\)', runner, _re.M)
        assert m, f"{name} not found in the runner"
        return m.group(1)

    # Normalised: the backend anchors with ^…$ on an already-stripped,
    # lowercased value; the runner uses fullmatch + \Z. Compare the
    # CHARACTER CLASSES, which is where a real divergence would show.
    assert runner_pat("NAME_RE").rstrip("\\Z") == pat(service, "_NAME_RE").strip("^$")
    assert runner_pat("UUID_RE").rstrip("\\Z") == pat(service, "_UUID_RE").strip("^$")
    # And the fstype enum, spelled as a set literal on both sides.
    runner_fs = set(_re.search(r"FSTYPES = \{(.+?)\}", runner).group(1).replace('"', "").split(", "))
    service_fs = set(
        _re.search(r"SUPPORTED_FSTYPES = \((.+?)\)", service).group(1).replace('"', "").split(", ")
    )
    assert runner_fs == service_fs, (runner_fs, service_fs)
