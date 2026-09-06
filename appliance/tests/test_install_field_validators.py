"""The shared ``--check-field`` validator (issue #995 items 3 + 4).

Before this, ``spatium-preseed-parse`` enforced ``admin_user`` and
``timezone`` rules for the HEADLESS path and the interactive wizard
enforced neither:

  * ``ask_user_password`` defaulted an empty answer to "admin" and passed
    anything else straight to ``chroot useradd``, whose failure was
    swallowed with ``|| true``. A username with a space — or ``root`` —
    left the installed box with no sudo-capable account, while
    ``PermitRootLogin no`` locked root out of SSH. Unreachable, discovered
    after the reboot, with the installer gone.
  * ``ask_timezone`` was a free-text inputbox, and ``do_install``'s only
    response to a name it could not resolve was to log a WARN and install
    UTC. A typo produced a box silently hours out.

The fix was to let the wizard CALL the parser's rules rather than
transcribe the reserved-account list and a zone check into bash —
two copies that drift. This file tests that shared entry point, plus the
two holes the pre-existing preseed-side timezone check had.

HOW TO RUN (from the repo root):
    python3 -m pytest appliance/tests/test_install_field_validators.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from _installer_source import INSTALLER, PARSER

ZONEINFO = Path("/usr/share/zoneinfo")


def check(field: str, value: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PARSER), "--check-field", field, value],
        capture_output=True,
        text=True,
    )


# ── Exit-code contract ────────────────────────────────────────────────
#
# The wizard's `_check_field` treats these three answers differently and
# must be able to tell them apart: 0 accept, 2 reject-with-reason, and
# anything else "the validator could not run", which is accepted with a
# loud log line rather than blocking the install on a broken image.


def test_valid_value_exits_zero_and_says_nothing():
    r = check("admin_user", "netops")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == ""


def test_rejection_exits_two_with_the_reason_on_stdout():
    r = check("admin_user", "root")
    assert r.returncode == 2
    # stdout, not stderr: the wizard captures it into a whiptail box.
    assert "reserved system account" in r.stdout
    assert r.stdout.strip()


def test_unknown_field_is_a_usage_error_not_a_verdict():
    """Exit 3, so a caller typo cannot read as "the value is fine"."""
    r = check("favourite_colour", "blue")
    assert r.returncode == 3
    assert r.stdout == ""
    assert "no validator" in r.stderr


def test_missing_value_argument_is_a_usage_error():
    r = subprocess.run(
        [sys.executable, str(PARSER), "--check-field", "admin_user"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 3


def test_check_field_does_not_need_pyyaml(tmp_path):
    """Answered above the ``import yaml`` guard on purpose.

    A username prompt has no business failing because python3-yaml is
    missing from the image, and the parser exits 3 — which the wizard
    reads as "could not run" — the moment that import fails. Simulated by
    shadowing ``yaml`` with a module that raises on import.
    """
    (tmp_path / "yaml.py").write_text('raise ImportError("stubbed out")\n')
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    r = subprocess.run(
        [sys.executable, str(PARSER), "--check-field", "admin_user", "netops"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    # Control: the same shadowing DOES break the normal parse path, which
    # is what proves the shadow took effect rather than being ignored.
    r2 = subprocess.run(
        [sys.executable, str(PARSER), str(tmp_path / "x.yaml"),
         str(tmp_path / "e"), str(tmp_path / "s")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r2.returncode == 3, r2.stdout + r2.stderr


# ── admin_user ────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["admin", "netops", "_svc", "a", "ops-1", "wsacct$"])
def test_usable_usernames_accepted(name):
    assert check("admin_user", name).returncode == 0, name


@pytest.mark.parametrize(
    "name,why",
    [
        ("", "empty"),
        ("Admin", "uppercase — useradd's NAME_REGEX is lowercase-only"),
        ("bad name", "a space splits the useradd argv"),
        ("has:colon", "a colon splits the chpasswd line"),
        ("1abc", "must not start with a digit"),
        ("-abc", "must not start with a hyphen"),
        ("a" * 33, "over useradd's 32-char utmp limit"),
        ("root", "reserved — and do_install sets root's password separately"),
        ("www-data", "reserved — already exists in the image"),
        ("nobody", "reserved"),
    ],
)
def test_unusable_usernames_refused(name, why):
    r = check("admin_user", name)
    assert r.returncode == 2, f"{name!r} should be refused ({why})"
    assert "admin_user" in r.stdout


# ── timezone ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tz", ["UTC", "Europe/Berlin", "America/Toronto", "America/Argentina/Buenos_Aires"]
)
def test_real_zones_accepted(tz):
    if not (ZONEINFO / tz).is_file():
        pytest.skip(f"{tz} not present in this host's tzdata")
    assert check("timezone", tz).returncode == 0, tz


def test_plus_in_zone_name_accepted():
    """``Etc/GMT+5`` is a real zone; a regex that forgot '+' would break it."""
    if not (ZONEINFO / "Etc/GMT+5").is_file():
        pytest.skip("Etc/GMT+5 not present in this host's tzdata")
    assert check("timezone", "Etc/GMT+5").returncode == 0


@pytest.mark.parametrize("tz", ["", "Amercia/Toronto", "Not A Zone", "Europe/"])
def test_typos_refused(tz):
    assert check("timezone", tz).returncode == 2, tz


def test_traversal_refused():
    """The value is interpolated into ``ln -sf /usr/share/zoneinfo/$TZ
    /etc/localtime``, so a bare existence check — which is what the
    preseed path used to do — points the installed system's localtime at
    an arbitrary file. The shape rule runs FIRST so this never reaches
    the filesystem at all.
    """
    # ``../../../etc/passwd`` is the one that matters: /usr/share/zoneinfo
    # is three levels down, so it RESOLVES to a real file and the old
    # bare ``os.path.exists`` said yes. A two-dot version lands on
    # /usr/etc/passwd, which does not exist — it was refused before this
    # fix as well, and testing only that would have been a guard that
    # passes on the unpatched code.
    for evil in ("../../../etc/passwd", "../../etc/passwd", "UTC/../../etc/passwd"):
        r = check("timezone", evil)
        assert r.returncode == 2, evil
        assert "not a valid IANA zone name" in r.stdout, evil


def test_a_directory_under_zoneinfo_is_not_a_zone():
    """``/usr/share/zoneinfo/America`` exists. Symlinking /etc/localtime
    at a directory breaks every timestamp on the box, and the old
    ``os.path.exists`` check accepted it.
    """
    if not (ZONEINFO / "America").is_dir():
        pytest.skip("no America/ directory in this host's tzdata")
    r = check("timezone", "America")
    assert r.returncode == 2
    assert "not a known IANA zone" in r.stdout


def test_a_non_tzif_file_under_zoneinfo_is_not_a_zone():
    """``leapseconds`` / ``posixrules`` are paths, are files, and are not
    zones. Only the TZif magic separates them.
    """
    candidates = [n for n in ("leapseconds", "posixrules", "tzdata") if (ZONEINFO / n).is_file()]
    if not candidates:
        pytest.skip("no non-zone regular file in this host's tzdata to test with")
    name = candidates[0]
    if (ZONEINFO / name).read_bytes()[:4] == b"TZif":
        pytest.skip(f"{name} is a real TZif file on this host")
    r = check("timezone", name)
    assert r.returncode == 2
    assert "not a compiled zone file" in r.stdout


# ── The wizard actually calls it ──────────────────────────────────────


def test_wizard_validates_both_prompts():
    """Structural: the prompts must route through ``_check_field``.

    A perfect validator nothing calls is the state item 3 and item 4 were
    filed about, so pin the call sites too.
    """
    src = INSTALLER.read_text(encoding="utf-8")
    assert "_check_field admin_user" in src
    assert "_check_field timezone" in src


def test_check_preseed_and_the_prompts_resolve_the_parser_the_same_way():
    """One resolver. Two copies of the "next to $0, else /usr/local/bin"
    dance would let the linter and the wizard find different parsers.
    """
    src = INSTALLER.read_text(encoding="utf-8")
    assert src.count('/spatium-preseed-parse"\n') >= 1
    # Exactly one place constructs the path.
    assert src.count('parser="$(dirname "$0")/spatium-preseed-parse"') == 1
    assert "_preseed_parser_path" in src


# ── The preseed path gets the same two fixes ──────────────────────────


def _lint(body: str, tmp_path) -> subprocess.CompletedProcess:
    """Run the real linter, the way an operator does.

    ``bash``, not ``sys.executable`` — spatium-install is a shell script,
    and running it with python fails at line 33 with a SyntaxError that
    happens to be non-zero, so a test asserting "this is refused" would
    PASS for entirely the wrong reason. Which is why the pair below
    includes an accept case: it is the control that proves the refusals
    are verdicts and not crashes.
    """
    f = tmp_path / "preseed.yaml"
    f.write_text(body, encoding="utf-8")
    return subprocess.run(
        ["bash", str(INSTALLER), "--check-preseed", str(f)],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "SPATIUM_INSTALL_LOG": str(tmp_path / "install.log"),
            "SPATIUM_INSTALL_TRACE_LOG": str(tmp_path / "trace.log"),
            "HOME": str(tmp_path),
        },
    )


def _preseed(tz: str) -> str:
    return (
        "spatium_preseed:\n"
        "  role: control-plane\n"
        "  hostname: spatium-cp-1\n"
        '  admin_password: "ChangeMe!12345"\n'
        f'  timezone: "{tz}"\n'
        "  network:\n"
        "    mode: dhcp\n"
    )


def test_preseed_timezone_traversal_refused(tmp_path):
    """Was accepted before #995 item 3, and a fully-unattended install
    would then symlink /etc/localtime at /etc/passwd with nobody
    watching.

    Three levels, not two: /usr/share/zoneinfo/../../../etc/passwd
    resolves to /etc/passwd, which exists, which is what made
    ``os.path.exists`` say yes.
    """
    if not Path("/etc/passwd").exists():
        pytest.skip("no /etc/passwd to traverse to on this host")
    r = _lint(_preseed("../../../etc/passwd"), tmp_path)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "timezone" in (r.stdout + r.stderr)


def test_preseed_timezone_directory_refused(tmp_path):
    if not (ZONEINFO / "America").is_dir():
        pytest.skip("no America/ directory in this host's tzdata")
    r = _lint(_preseed("America"), tmp_path)
    assert r.returncode != 0, r.stdout + r.stderr


def test_preseed_real_timezone_still_accepted(tmp_path):
    """The guard above must not have made the ordinary case fail."""
    if not (ZONEINFO / "UTC").is_file():
        pytest.skip("no tzdata on this host")
    r = _lint(_preseed("UTC"), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
