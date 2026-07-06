"""Host-portable pytest for spatium-install's unattended answer-file path.

These tests drive the installer via its ``--check-answers`` mode, which
parses + validates an answer file exactly like an unattended install
would but touches nothing — no root, no block devices, no whiptail.
Machine-specific checks (is the target disk attached, does the NIC
exist) downgrade to warnings in this mode; everything else (the
confirm_wipe consent gate, role / hostname / network / pairing-code /
CIDR rules) validates for real.

The disk size-floor gate is exercised through the SPATIUM_FAKE_DISK_BYTES
test seam documented in the script — CI machines have no spare 32 GiB
block device to point the real ``blockdev --getsize64`` at.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_unattended_install.py -v

No database, no Docker, no appliance ISO required.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

INSTALLER = (
    Path(__file__).parent.parent / "mkosi.extra" / "usr" / "local" / "bin" /
    "spatium-install"
)
EXAMPLE = (
    Path(__file__).parent.parent / "cloud-init" / "spatium-install.conf.example"
)

# A minimal valid control-plane answer file. target_disk points at a
# device that won't exist on the test machine — --check-answers
# downgrades that to a warning, which is what we want here.
VALID_CONTROL_PLANE = """\
confirm_wipe: true
role: control-plane
target_disk: /dev/vda
hostname: spatium-cp-1
admin_password: "ChangeMe!12345"
network_mode: dhcp
"""

VALID_APPLIANCE = """\
confirm_wipe: true
role: appliance
target_disk: /dev/vda
hostname: spatium-agent-1
admin_password: "ChangeMe!12345"
network_mode: dhcp
control_plane_url: https://spatium-cp-1.example.com
bootstrap_pairing_code: "12345678"
"""


def _check(tmp_path: Path, content: str, extra_env: dict[str, str] | None = None):
    """Run spatium-install --check-answers against `content`; return CompletedProcess."""
    conf = tmp_path / "answers.conf"
    conf.write_text(content, encoding="utf-8")
    env = {
        # Keep the installer's log plumbing away from /var/log.
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "SPATIUM_INSTALL_LOG": str(tmp_path / "install.log"),
        "SPATIUM_INSTALL_TRACE_LOG": str(tmp_path / "trace.log"),
        "HOME": str(tmp_path),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(INSTALLER), "--check-answers", str(conf)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _errors(result: subprocess.CompletedProcess) -> str:
    return result.stdout + result.stderr


# ─────────────────────────────────────────────────────────────────────────────
# 1. The happy paths.
# ─────────────────────────────────────────────────────────────────────────────

def test_valid_control_plane_accepted(tmp_path: Path) -> None:
    r = _check(tmp_path, VALID_CONTROL_PLANE)
    assert r.returncode == 0, _errors(r)
    assert "OK:" in r.stdout
    assert "role=control-plane" in r.stdout


def test_valid_appliance_accepted(tmp_path: Path) -> None:
    r = _check(tmp_path, VALID_APPLIANCE)
    assert r.returncode == 0, _errors(r)


def test_valid_static_network_accepted(tmp_path: Path) -> None:
    r = _check(
        tmp_path,
        VALID_CONTROL_PLANE.replace(
            "network_mode: dhcp",
            "network_mode: static\n"
            "network_interface: eth0\n"
            "network_ip: 10.3.3.139\n"
            "network_prefix: 24\n"
            "network_gateway: 10.3.3.1\n"
            "network_dns: 1.1.1.1 1.0.0.1",
        ),
    )
    assert r.returncode == 0, _errors(r)


def test_absent_disk_is_warning_not_error_in_check_mode(tmp_path: Path) -> None:
    """--check-answers runs on machines that aren't the install target."""
    r = _check(tmp_path, VALID_CONTROL_PLANE)
    assert r.returncode == 0, _errors(r)
    assert "WARN" in r.stdout
    assert "not present on this machine" in r.stdout


# ─────────────────────────────────────────────────────────────────────────────
# 2. The confirm_wipe consent gate — the headline safety property.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value",
    ["false", "yes", "True", "TRUE", "1", ""],
    ids=["false", "yes", "True-capitalised", "TRUE", "one", "empty"],
)
def test_confirm_wipe_must_be_exactly_true(tmp_path: Path, value: str) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "confirm_wipe: true", f"confirm_wipe: {value}"
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1, (
        f"confirm_wipe: {value!r} must be refused — only the exact string "
        f"'true' is consent to erase a disk"
    )
    assert "confirm_wipe" in _errors(r)


def test_confirm_wipe_missing_refused(tmp_path: Path) -> None:
    content = "\n".join(
        line for line in VALID_CONTROL_PLANE.splitlines()
        if not line.startswith("confirm_wipe")
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "confirm_wipe" in _errors(r)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Role validation.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["", "full-stack", "worker", "Control-Plane"])
def test_bad_role_refused(tmp_path: Path, role: str) -> None:
    content = VALID_CONTROL_PLANE.replace("role: control-plane", f"role: {role}")
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "role" in _errors(r)


def test_appliance_missing_pairing_code_refused(tmp_path: Path) -> None:
    content = "\n".join(
        line for line in VALID_APPLIANCE.splitlines()
        if not line.startswith("bootstrap_pairing_code")
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "bootstrap_pairing_code" in _errors(r)


def test_appliance_missing_control_plane_url_refused(tmp_path: Path) -> None:
    content = "\n".join(
        line for line in VALID_APPLIANCE.splitlines()
        if not line.startswith("control_plane_url")
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "control_plane_url" in _errors(r)


@pytest.mark.parametrize("code", ["1234567", "123456789", "abcdefgh"])
def test_appliance_bad_pairing_code_refused(tmp_path: Path, code: str) -> None:
    content = VALID_APPLIANCE.replace(
        'bootstrap_pairing_code: "12345678"',
        f'bootstrap_pairing_code: "{code}"',
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "bootstrap_pairing_code" in _errors(r)


def test_appliance_pairing_code_with_separators_accepted(tmp_path: Path) -> None:
    """The frontend displays codes as 1234-5678 — separators must strip."""
    content = VALID_APPLIANCE.replace(
        'bootstrap_pairing_code: "12345678"',
        'bootstrap_pairing_code: "1234-5678"',
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _errors(r)


def test_control_plane_ignores_pairing_fields(tmp_path: Path) -> None:
    """Pairing fields are appliance-role-only; control-plane must not require them."""
    r = _check(tmp_path, VALID_CONTROL_PLANE)
    assert r.returncode == 0, _errors(r)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Hostname rules (mirror of ask_hostname's RFC 1123 sanity check).
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "hostname",
    ["", "has space", "has.dot", "under_score", "-leading", "trailing-", "x" * 64],
    ids=["empty", "space", "dot", "underscore", "leading-hyphen",
         "trailing-hyphen", "too-long"],
)
def test_bad_hostname_refused(tmp_path: Path, hostname: str) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "hostname: spatium-cp-1", f"hostname: {hostname}"
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "hostname" in _errors(r)


def test_63_char_hostname_accepted(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "hostname: spatium-cp-1", f"hostname: {'x' * 63}"
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _errors(r)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Credentials.
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_admin_password_refused(tmp_path: Path) -> None:
    content = "\n".join(
        line for line in VALID_CONTROL_PLANE.splitlines()
        if not line.startswith("admin_password")
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "admin_password" in _errors(r)


def test_password_with_hash_survives_quoting(tmp_path: Path) -> None:
    """A quoted value keeps a '#' verbatim (not treated as a comment)."""
    content = VALID_CONTROL_PLANE.replace(
        'admin_password: "ChangeMe!12345"',
        'admin_password: "pa#ss word12"',
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _errors(r)


def test_bad_admin_user_refused(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE + "admin_user: bad user name\n"
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "admin_user" in _errors(r)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Target disk.
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_target_disk_refused(tmp_path: Path) -> None:
    content = "\n".join(
        line for line in VALID_CONTROL_PLANE.splitlines()
        if not line.startswith("target_disk")
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "target_disk" in _errors(r)


def test_disk_under_floor_refused(tmp_path: Path) -> None:
    """16 GiB disk is under the 32 GiB A/B floor (via the fake-bytes test seam)."""
    r = _check(
        tmp_path, VALID_CONTROL_PLANE,
        extra_env={"SPATIUM_FAKE_DISK_BYTES": str(16 * 1024**3)},
    )
    assert r.returncode == 1
    assert "32 GiB" in _errors(r)


def test_disk_over_floor_accepted(tmp_path: Path) -> None:
    r = _check(
        tmp_path, VALID_CONTROL_PLANE,
        extra_env={"SPATIUM_FAKE_DISK_BYTES": str(64 * 1024**3)},
    )
    assert r.returncode == 0, _errors(r)


def test_control_plane_under_recommended_warns_but_passes(tmp_path: Path) -> None:
    """Between the 32 GiB floor and the 40 GiB recommendation: warn, don't block."""
    r = _check(
        tmp_path, VALID_CONTROL_PLANE,
        extra_env={"SPATIUM_FAKE_DISK_BYTES": str(36 * 1024**3)},
    )
    assert r.returncode == 0, _errors(r)
    assert "recommended" in r.stdout


def test_target_disk_auto_is_check_mode_warning(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "target_disk: /dev/vda", "target_disk: auto"
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _errors(r)
    assert "auto" in r.stdout


# ─────────────────────────────────────────────────────────────────────────────
# 7. Network validation.
# ─────────────────────────────────────────────────────────────────────────────

def test_bad_network_mode_refused(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "network_mode: dhcp", "network_mode: bridged"
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "network_mode" in _errors(r)


@pytest.mark.parametrize(
    "field,value",
    [
        ("network_ip", ""),
        ("network_ip", "999.1.1.1"),
        ("network_prefix", "0"),
        ("network_prefix", "33"),
        ("network_prefix", "abc"),
        ("network_gateway", ""),
        ("network_gateway", "not-an-ip"),
        ("network_dns", "8.8.8.8 bad-dns"),
    ],
)
def test_bad_static_fields_refused(tmp_path: Path, field: str, value: str) -> None:
    static = {
        "network_interface": "eth0",
        "network_ip": "10.3.3.139",
        "network_prefix": "24",
        "network_gateway": "10.3.3.1",
        "network_dns": "1.1.1.1",
    }
    static[field] = value
    lines = "\n".join(f"{k}: {v}" for k, v in static.items() if v != "")
    content = VALID_CONTROL_PLANE.replace(
        "network_mode: dhcp", "network_mode: static\n" + lines
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1, f"{field}={value!r} should refuse\n{_errors(r)}"
    assert field.split("_")[0] in _errors(r)


def test_static_missing_interface_refused(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "network_mode: dhcp",
        "network_mode: static\nnetwork_ip: 10.3.3.139\n"
        "network_prefix: 24\nnetwork_gateway: 10.3.3.1",
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "network_interface" in _errors(r)


# ─────────────────────────────────────────────────────────────────────────────
# 8. k3s CIDR validation (shared helper with the interactive wizard).
# ─────────────────────────────────────────────────────────────────────────────

def test_overlapping_cidrs_refused(tmp_path: Path) -> None:
    content = (
        VALID_CONTROL_PLANE
        + "k3s_pod_cidr: 10.42.0.0/15\nk3s_service_cidr: 10.43.0.0/16\n"
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "overlaps" in _errors(r)


def test_too_narrow_cidr_refused(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE + "k3s_pod_cidr: 10.42.0.0/24\n"
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "/22" in _errors(r)


def test_pod_cidr_overlapping_static_lan_refused(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "network_mode: dhcp",
        "network_mode: static\nnetwork_interface: eth0\n"
        "network_ip: 10.42.3.10\nnetwork_prefix: 16\n"
        "network_gateway: 10.42.0.1\nnetwork_dns: 1.1.1.1",
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "overlaps" in _errors(r)


# ─────────────────────────────────────────────────────────────────────────────
# 9. Parser behaviors (get_answer).
# ─────────────────────────────────────────────────────────────────────────────

def test_inline_comments_stripped(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "role: control-plane", "role: control-plane   # the first node"
    ).replace(
        "hostname: spatium-cp-1", "hostname: spatium-cp-1        # unique name"
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _errors(r)
    assert "role=control-plane" in r.stdout


def test_quoted_values_and_whitespace(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "role: control-plane", 'role: "control-plane"'
    ).replace(
        "hostname: spatium-cp-1", "hostname:    'spatium-cp-1'   "
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _errors(r)


def test_multiple_errors_reported_in_one_pass(tmp_path: Path) -> None:
    """One lint round should surface every problem, not just the first."""
    r = _check(tmp_path, "role: bogus\nnetwork_mode: bogus\n")
    err = _errors(r)
    assert r.returncode == 1
    for needle in ("confirm_wipe", "role", "hostname", "admin_password",
                   "target_disk", "network_mode"):
        assert needle in err, f"expected an error mentioning {needle!r}\n{err}"


def test_empty_file_refused_with_usage_of_required_fields(tmp_path: Path) -> None:
    r = _check(tmp_path, "# just a comment\n")
    assert r.returncode == 1


def test_missing_file_is_usage_error(tmp_path: Path) -> None:
    r = subprocess.run(
        ["bash", str(INSTALLER), "--check-answers", str(tmp_path / "nope.conf")],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
             "SPATIUM_INSTALL_LOG": str(tmp_path / "i.log"),
             "SPATIUM_INSTALL_TRACE_LOG": str(tmp_path / "t.log")},
        timeout=60,
    )
    assert r.returncode == 2
    assert "usage" in r.stderr


# ─────────────────────────────────────────────────────────────────────────────
# 10. The shipped example file stays honest.
# ─────────────────────────────────────────────────────────────────────────────

def test_example_file_refused_as_shipped(tmp_path: Path) -> None:
    """The example ships confirm_wipe: false — it must NOT validate as-is.

    This is deliberate: copying the example verbatim onto a seed disk
    must never be sufficient consent to erase a disk.
    """
    r = subprocess.run(
        ["bash", str(INSTALLER), "--check-answers", str(EXAMPLE)],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
             "SPATIUM_INSTALL_LOG": str(tmp_path / "i.log"),
             "SPATIUM_INSTALL_TRACE_LOG": str(tmp_path / "t.log")},
        timeout=60,
    )
    assert r.returncode == 1
    err = r.stdout + r.stderr
    assert "confirm_wipe" in err
    # confirm_wipe must be the ONLY error — everything else in the
    # example must stay valid so a one-line flip makes it installable.
    assert err.count("ERROR:") == 1, (
        f"the shipped example has drifted — expected exactly one error "
        f"(confirm_wipe), got:\n{err}"
    )


def test_example_file_valid_once_confirmed(tmp_path: Path) -> None:
    content = EXAMPLE.read_text(encoding="utf-8").replace(
        "confirm_wipe: false", "confirm_wipe: true"
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _errors(r)
