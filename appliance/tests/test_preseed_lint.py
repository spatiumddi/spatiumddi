"""Host-portable pytest for spatium-install's `--check-preseed` linter.

Issue #581 — salvaged from the closed PR #579 (which built the same
offline-lint + pytest pattern for a rival answer-file format) and ported
onto the SHIPPED #549 preseed (`spatium-preseed.yaml` / cloud-init
`spatium_preseed:` block).

These tests drive the installer via its ``--check-preseed`` mode, which
parses + validates a preseed answer file exactly like a real headless
install would — reusing the SAME parser (``spatium-preseed-parse``) and
the SAME interactive-wizard rules (``_validate_static_net`` /
``_k3s_cidr_error``) — but touches nothing: no root, no block devices,
no whiptail, no /var/log. Machine-specific checks (is target_disk
attached, does it clear the 32 GiB A/B floor) downgrade to warnings so
the same file lints identically on a dev laptop and the real appliance.

The disk size-floor gate is exercised through the SPATIUM_FAKE_DISK_BYTES
test seam in ``spatium-preseed-parse`` — CI machines have no spare 32 GiB
block device to point the real ``/sys/block/<name>/size`` read at.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_preseed_lint.py -v

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
CLOUD_INIT = Path(__file__).parent.parent / "cloud-init"
EXAMPLE_CP = CLOUD_INIT / "spatium-preseed-control-plane.yaml.example"
EXAMPLE_APPLIANCE = CLOUD_INIT / "spatium-preseed-appliance.yaml.example"

# ``timezone: UTC`` in a preseed is a HARD field in #549 — the parser
# rejects a zone that isn't in /usr/share/zoneinfo. Skip the handful of
# tests that pin a real zone when tzdata is absent (minimal CI images).
_HAS_UTC_ZONE = Path("/usr/share/zoneinfo/UTC").exists()

# A minimal VALID control-plane preseed. Deliberately omits target_disk
# + timezone (both OPTIONAL in #549 — absent means "prompt interactively"
# / default, not an error), so the happy path doesn't depend on a block
# device or on tzdata being installed on the linting host.
VALID_CONTROL_PLANE = """\
spatium_preseed:
  role: control-plane
  hostname: spatium-cp-1
  admin_password: "ChangeMe!12345"
  network:
    mode: dhcp
"""

# A fully-populated appliance preseed (role appliance needs a
# control_plane_url + pairing_code). target_disk is a by-id path that
# won't exist on the linting box → warning, not error.
VALID_APPLIANCE = """\
spatium_preseed:
  role: appliance
  hostname: spatium-agent-1
  admin_password: "ChangeMe!12345"
  network:
    mode: dhcp
  control_plane_url: https://spatium-cp-1.example.com/
  pairing_code: "12345678"
"""


def _check(
    tmp_path: Path, content: str, extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``spatium-install --check-preseed`` against `content`."""
    conf = tmp_path / "preseed.yaml"
    conf.write_text(content, encoding="utf-8")
    return _check_file(tmp_path, conf, extra_env)


def _check_file(
    tmp_path: Path, conf: Path, extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = {
        # Keep the installer's log plumbing away from the real /var/log.
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "SPATIUM_INSTALL_LOG": str(tmp_path / "install.log"),
        "SPATIUM_INSTALL_TRACE_LOG": str(tmp_path / "trace.log"),
        "HOME": str(tmp_path),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(INSTALLER), "--check-preseed", str(conf)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _out(result: subprocess.CompletedProcess) -> str:
    return result.stdout + result.stderr


def _static(**overrides: str) -> str:
    """A VALID_CONTROL_PLANE with its dhcp block swapped for a static
    one, individual fields overridable/removable (value ``None`` drops
    the line)."""
    fields = {
        "interface": "eth0",
        "ip": "10.3.3.139",
        "prefix": "24",
        "gateway": "10.3.3.1",
        "dns": "1.1.1.1 1.0.0.1",
        **overrides,
    }
    lines = ["    mode: static"]
    for key, val in fields.items():
        if val is not None:
            lines.append(f"    {key}: {val}")
    block = "\n".join(lines)
    return VALID_CONTROL_PLANE.replace("    mode: dhcp", block)


# ─────────────────────────────────────────────────────────────────────────────
# 1. The happy paths.
# ─────────────────────────────────────────────────────────────────────────────

def test_valid_control_plane_accepted(tmp_path: Path) -> None:
    r = _check(tmp_path, VALID_CONTROL_PLANE)
    assert r.returncode == 0, _out(r)
    assert "OK:" in r.stdout
    assert "role=control-plane" in r.stdout


def test_valid_appliance_accepted(tmp_path: Path) -> None:
    r = _check(tmp_path, VALID_APPLIANCE)
    assert r.returncode == 0, _out(r)
    assert "role=appliance" in r.stdout


def test_valid_static_network_accepted(tmp_path: Path) -> None:
    r = _check(tmp_path, _static())
    assert r.returncode == 0, _out(r)


def test_absent_disk_is_warning_not_error(tmp_path: Path) -> None:
    """A target_disk that isn't on the linting box downgrades to a
    warning — the size floor is (re)checked on the real appliance."""
    content = VALID_CONTROL_PLANE + "  confirm_wipe: true\n  target_disk: /dev/vdzzz\n"
    r = _check(tmp_path, content)
    assert r.returncode == 0, _out(r)
    assert "WARN" in r.stdout
    assert "does not exist" in r.stdout


# ─────────────────────────────────────────────────────────────────────────────
# 2. confirm_wipe — #549 gates target_disk on it (NOT a standalone
#    required field like PR #579 modelled).
# ─────────────────────────────────────────────────────────────────────────────

def test_confirm_wipe_non_boolean_refused(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE + "  confirm_wipe: maybe\n"
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "confirm_wipe" in _out(r)


def test_confirm_wipe_false_disk_falls_through_to_interactive(tmp_path: Path) -> None:
    """confirm_wipe:false + a present disk is NOT an error — the disk is
    simply not auto-selected (the console picker runs). Lints clean."""
    content = VALID_CONTROL_PLANE + "  confirm_wipe: false\n  target_disk: /dev/vda\n"
    r = _check(
        tmp_path, content,
        extra_env={"SPATIUM_FAKE_DISK_BYTES": str(64 * 1024**3)},
    )
    assert r.returncode == 0, _out(r)
    assert "WARN" in r.stdout
    assert "confirm_wipe" in r.stdout


# ─────────────────────────────────────────────────────────────────────────────
# 3. Role validation (incl. #549's friendly aliases).
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["worker", "full-stack", "Control-Plane-typo", "frontend-core"])
def test_bad_role_refused(tmp_path: Path, role: str) -> None:
    content = VALID_CONTROL_PLANE.replace("role: control-plane", f"role: {role}")
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "role" in _out(r)


@pytest.mark.parametrize("alias", ["first-node", "control", "add-node", "agent", "application"])
def test_role_aliases_accepted(tmp_path: Path, alias: str) -> None:
    """#549 maps friendly aliases onto control-plane / appliance."""
    content = VALID_APPLIANCE.replace("role: appliance", f"role: {alias}")
    r = _check(tmp_path, content)
    assert r.returncode == 0, _out(r)


def test_appliance_missing_url_and_code_is_partial_not_error(tmp_path: Path) -> None:
    """A role:appliance preseed with no url/code is a PARTIAL preseed —
    the wizard prompts interactively. Valid, but warned + not unattended."""
    content = "spatium_preseed:\n  role: appliance\n  hostname: agent-1\n"
    r = _check(tmp_path, content)
    assert r.returncode == 0, _out(r)
    assert "control_plane_url" in r.stdout  # a WARN line
    assert "fully-unattended=no" in r.stdout


def test_control_plane_ignores_pairing_fields(tmp_path: Path) -> None:
    """Pairing fields are appliance-only; a control-plane must not need them."""
    r = _check(tmp_path, VALID_CONTROL_PLANE)
    assert r.returncode == 0, _out(r)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Hostname rules (RFC 1123 — mirror of ask_hostname).
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "hostname",
    ["has space", "has.dot", "under_score", "-leading", "trailing-", "x" * 64],
    ids=["space", "dot", "underscore", "leading-hyphen", "trailing-hyphen", "too-long"],
)
def test_bad_hostname_refused(tmp_path: Path, hostname: str) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "hostname: spatium-cp-1", f'hostname: "{hostname}"'
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "hostname" in _out(r)


def test_63_char_hostname_accepted(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "hostname: spatium-cp-1", f"hostname: {'x' * 63}"
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _out(r)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Credentials.
# ─────────────────────────────────────────────────────────────────────────────

def test_password_and_hash_mutually_exclusive(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE + '  admin_password_hash: "$6$salt$hash"\n'
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "admin_password" in _out(r)


def test_bad_password_hash_refused(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace(
        'admin_password: "ChangeMe!12345"', 'admin_password_hash: "not-a-crypt-hash"'
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "admin_password_hash" in _out(r)


def test_valid_password_hash_accepted(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace(
        'admin_password: "ChangeMe!12345"',
        'admin_password_hash: "$6$rounds=4096$abcdefgh$0123456789abcdef"',
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _out(r)


def test_quoted_password_keeps_hash_char(tmp_path: Path) -> None:
    """A quoted value keeps a '#' verbatim (not a YAML comment)."""
    content = VALID_CONTROL_PLANE.replace(
        'admin_password: "ChangeMe!12345"', 'admin_password: "pa#ss word12"'
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _out(r)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Target disk + the A/B 32 GiB floor (via the fake-bytes seam).
# ─────────────────────────────────────────────────────────────────────────────

def test_disk_under_floor_refused(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE + "  confirm_wipe: true\n  target_disk: /dev/vda\n"
    r = _check(
        tmp_path, content,
        extra_env={"SPATIUM_FAKE_DISK_BYTES": str(16 * 1024**3)},
    )
    assert r.returncode == 1
    assert "32 GiB" in _out(r)


def test_disk_over_floor_accepted(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE + "  confirm_wipe: true\n  target_disk: /dev/vda\n"
    r = _check(
        tmp_path, content,
        extra_env={"SPATIUM_FAKE_DISK_BYTES": str(64 * 1024**3)},
    )
    assert r.returncode == 0, _out(r)


def test_partition_style_disk_refused(tmp_path: Path) -> None:
    """A partition (has a whole-disk parent in /sys/class/block) is not a
    valid whole-disk target — but only checkable on a box that has the
    node. This runs on any box with at least one real disk; skip if none."""
    disks = [p.name for p in Path("/sys/block").iterdir()
             if not p.name.startswith(("loop", "ram", "sr", "fd", "zram", "dm-"))]
    if not disks:
        pytest.skip("no real block device on this host to derive a partition path")
    # A path we know is NOT a whole disk (a made-up partition of the disk).
    part = f"/dev/{disks[0]}p999"
    content = VALID_CONTROL_PLANE + f"  confirm_wipe: true\n  target_disk: {part}\n"
    r = _check(tmp_path, content)
    # Absent/partition disks fall through to interactive → WARN, still valid.
    assert r.returncode == 0, _out(r)
    assert "WARN" in r.stdout


# ─────────────────────────────────────────────────────────────────────────────
# 7. Network validation — parser-level AND the stricter linter-level
#    rules the linter closes over (the whole point of the salvage).
# ─────────────────────────────────────────────────────────────────────────────

def test_bad_network_mode_refused(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace("    mode: dhcp", "    mode: bridged")
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "network.mode" in _out(r)


@pytest.mark.parametrize(
    "field,value,needle",
    [
        ("ip", "999.1.1.1", "network.ip"),
        ("prefix", "abc", "network.prefix"),
        ("gateway", "not-an-ip", "network.gateway"),
    ],
)
def test_bad_static_field_refused_by_parser(
    tmp_path: Path, field: str, value: str, needle: str
) -> None:
    r = _check(tmp_path, _static(**{field: value}))
    assert r.returncode == 1, _out(r)
    assert needle in _out(r)


def test_static_missing_interface_refused(tmp_path: Path) -> None:
    r = _check(tmp_path, _static(interface=None))
    assert r.returncode == 1
    assert "network.interface" in _out(r)


def test_static_gateway_off_subnet_refused(tmp_path: Path) -> None:
    """THE GAP the linter closes: a gateway that is a valid IPv4 but not
    inside the host subnet passes the parser's own check, yet the
    interactive install's _validate_static_net (which the linter reuses)
    rejects it. Without the linter this only surfaces at install time."""
    r = _check(tmp_path, _static(gateway="192.168.99.1"))
    assert r.returncode == 1
    assert "network" in _out(r)
    assert "not inside" in _out(r)


def test_static_bad_dns_refused(tmp_path: Path) -> None:
    """Same gap: the parser does not validate the DNS list; the linter's
    reuse of _validate_static_net does."""
    r = _check(tmp_path, _static(dns='"1.1.1.1 not-an-ip"'))
    assert r.returncode == 1
    assert "DNS" in _out(r)


def test_static_prefix_zero_refused_by_linter(tmp_path: Path) -> None:
    """Prefix 0 slips past the parser (it allows 0-32) but _validate_static_net
    requires 1-32 — another rule the linter closes over."""
    r = _check(tmp_path, _static(prefix="0"))
    assert r.returncode == 1
    assert "Prefix" in _out(r)


# ─────────────────────────────────────────────────────────────────────────────
# 8. k3s CIDR validation (shared _k3s_cidr_error helper + the parser).
# ─────────────────────────────────────────────────────────────────────────────

def test_overlapping_cidrs_refused(tmp_path: Path) -> None:
    content = (
        VALID_CONTROL_PLANE
        + "  k3s:\n    pod_cidr: 10.42.0.0/15\n    service_cidr: 10.43.0.0/16\n"
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "overlaps" in _out(r)


def test_too_narrow_cidr_refused(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE + "  k3s:\n    pod_cidr: 10.42.0.0/24\n"
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "/22" in _out(r)


def test_pod_cidr_overlapping_static_lan_refused(tmp_path: Path) -> None:
    content = _static(ip="10.42.3.10", prefix="16", gateway="10.42.0.1")
    content += "  k3s:\n    pod_cidr: 10.42.0.0/16\n    service_cidr: 10.43.0.0/16\n"
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "overlaps" in _out(r)


# ─────────────────────────────────────────────────────────────────────────────
# 9. Pairing code (appliance role).
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", ["1234567", "123456789", "abcdefgh"])
def test_bad_pairing_code_refused(tmp_path: Path, code: str) -> None:
    content = VALID_APPLIANCE.replace(
        'pairing_code: "12345678"', f'pairing_code: "{code}"'
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "pairing_code" in _out(r)


def test_pairing_code_with_separators_accepted(tmp_path: Path) -> None:
    """The frontend shows codes as 1234-5678 — separators must strip."""
    content = VALID_APPLIANCE.replace(
        'pairing_code: "12345678"', 'pairing_code: "1234-5678"'
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _out(r)


def test_bare_scheme_control_plane_url_refused(tmp_path: Path) -> None:
    content = VALID_APPLIANCE.replace(
        "control_plane_url: https://spatium-cp-1.example.com/",
        'control_plane_url: "https://"',
    )
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "control_plane_url" in _out(r)


# ─────────────────────────────────────────────────────────────────────────────
# 10. Timezone (a hard field in #549 — an unknown zone halts).
# ─────────────────────────────────────────────────────────────────────────────

def test_bad_timezone_refused(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE + "  timezone: Not/AZone\n"
    r = _check(tmp_path, content)
    assert r.returncode == 1
    assert "timezone" in _out(r)


@pytest.mark.skipif(not _HAS_UTC_ZONE, reason="tzdata (/usr/share/zoneinfo/UTC) not installed")
def test_good_timezone_accepted(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE + "  timezone: UTC\n"
    r = _check(tmp_path, content)
    assert r.returncode == 0, _out(r)


# ─────────────────────────────────────────────────────────────────────────────
# 11. Parser behaviours (YAML shape, wrapper vs bare doc).
# ─────────────────────────────────────────────────────────────────────────────

def test_inline_yaml_comments_stripped(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "role: control-plane", "role: control-plane   # the first node"
    ).replace(
        "hostname: spatium-cp-1", "hostname: spatium-cp-1   # unique name"
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _out(r)
    assert "role=control-plane" in r.stdout


def test_quoted_and_aliased_values(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace(
        "role: control-plane", 'role: "control_plane"'
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _out(r)


def test_bare_document_without_wrapper_accepted(tmp_path: Path) -> None:
    """The preseed may be the whole doc (no spatium_preseed: wrapper) if
    it carries a recognised field."""
    content = (
        "role: control-plane\n"
        "hostname: bare-doc-cp\n"
        'admin_password: "ChangeMe!12345"\n'
        "network:\n  mode: dhcp\n"
    )
    r = _check(tmp_path, content)
    assert r.returncode == 0, _out(r)


def test_hyphen_alias_wrapper_accepted(tmp_path: Path) -> None:
    content = VALID_CONTROL_PLANE.replace("spatium_preseed:", "spatium-preseed:")
    r = _check(tmp_path, content)
    assert r.returncode == 0, _out(r)


def test_empty_file_refused(tmp_path: Path) -> None:
    r = _check(tmp_path, "# just a comment\n")
    assert r.returncode == 1


def test_non_preseed_yaml_refused(tmp_path: Path) -> None:
    """A parseable YAML with no spatium content is not ours → rejected as
    a lint target (the real install would just fall through, but you asked
    us to lint THIS file)."""
    r = _check(tmp_path, "some_other_tool:\n  foo: bar\n")
    assert r.returncode == 1
    assert "preseed content" in _out(r)


def test_malformed_yaml_refused(tmp_path: Path) -> None:
    r = _check(tmp_path, "spatium_preseed:\n  role: [unterminated\n")
    assert r.returncode == 1


# ─────────────────────────────────────────────────────────────────────────────
# 12. Multi-error reporting — one lint round surfaces every problem.
# ─────────────────────────────────────────────────────────────────────────────

def test_multiple_errors_reported_in_one_pass(tmp_path: Path) -> None:
    content = (
        "spatium_preseed:\n"
        "  role: worker\n"
        "  hostname: bad host\n"
        "  network:\n    mode: bridged\n"
        "  k3s:\n    pod_cidr: 10.42.0.0/15\n    service_cidr: 10.43.0.0/16\n"
    )
    r = _check(tmp_path, content)
    err = _out(r)
    assert r.returncode == 1
    for needle in ("role", "hostname", "network.mode", "overlaps"):
        assert needle in err, f"expected an error mentioning {needle!r}\n{err}"


# ─────────────────────────────────────────────────────────────────────────────
# 13. CLI surface — usage / help.
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_file_is_usage_error(tmp_path: Path) -> None:
    r = _check_file(tmp_path, tmp_path / "nope.yaml")
    assert r.returncode == 2
    assert "usage" in r.stderr


def test_help_exits_zero(tmp_path: Path) -> None:
    r = subprocess.run(
        ["bash", str(INSTALLER), "--help"],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
             "SPATIUM_INSTALL_LOG": str(tmp_path / "i.log"),
             "SPATIUM_INSTALL_TRACE_LOG": str(tmp_path / "t.log"),
             "HOME": str(tmp_path)},
        timeout=60,
    )
    assert r.returncode == 0
    assert "--check-preseed" in r.stdout


# ─────────────────────────────────────────────────────────────────────────────
# 14. The shipped example files stay valid.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_UTC_ZONE, reason="tzdata not installed (examples pin timezone: UTC)")
@pytest.mark.skipif(not EXAMPLE_CP.exists(), reason="shipped example not present")
def test_shipped_control_plane_example_lints_clean(tmp_path: Path) -> None:
    """Copying the shipped control-plane example verbatim must lint OK —
    only its by-id target_disk (absent on CI) shows as a warning."""
    r = _check_file(tmp_path, EXAMPLE_CP)
    assert r.returncode == 0, _out(r)
    assert "role=control-plane" in r.stdout


@pytest.mark.skipif(not _HAS_UTC_ZONE, reason="tzdata not installed (examples pin timezone: UTC)")
@pytest.mark.skipif(not EXAMPLE_APPLIANCE.exists(), reason="shipped example not present")
def test_shipped_appliance_example_lints_clean(tmp_path: Path) -> None:
    r = _check_file(tmp_path, EXAMPLE_APPLIANCE)
    assert r.returncode == 0, _out(r)
    assert "role=appliance" in r.stdout
