"""Host-portable pytest for `spatium-upgrade-slot status` (#788).

`status` used to probe BOTH slots with ``slot_version_label()``, which
mounts the slot read-only to read its ``appliance-release``. The active
slot's device is already mounted rw at ``/``, so the kernel refuses a
second read-only mount of it and the helper returned the literal string
``"unreadable"`` — meaning whichever slot was booted always displayed as
unreadable, on every appliance and every release. Operators read that as
a failed upgrade right after a successful one.

The fix routes both versions through ``sync_slot_versions()``, which
reads the active slot from the booted ``/etc/spatiumddi/appliance-release``
directly (no mount) and mounts only the inactive slot — the behaviour
APPLIANCE.md already documented.

These tests import the CLI as a module and stub the two discovery
helpers, so they need no root, no partitions and no appliance.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_slot_status_active_version.py -v

No database, no Docker, no appliance ISO required.
"""

from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).parent.parent / "mkosi.extra" / "usr" / "local" / "bin" /
    "spatium-upgrade-slot"
)

ACTIVE_VERSION = "2026.07.30-1"
INACTIVE_VERSION = "2026.07.21-1"


@pytest.fixture(scope="module")
def slot_cli():
    """Import the extensionless CLI as a module (it has a __main__ guard)."""
    loader = SourceFileLoader("spatium_upgrade_slot", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def stubbed(slot_cli, monkeypatch, tmp_path):
    """Active = root_b, inactive = root_a, with the mount probe recorded.

    ``slot_version_label`` is the mount-based probe. Recording every call
    is the whole point: calling it on the active slot is the bug.
    """
    active = {"partlabel": "root_b", "device": "/dev/nvme0n1p5", "fslabel": "root_b",
              "uuid": "bbbb"}
    inactive = {"partlabel": "root_a", "device": "/dev/nvme0n1p4", "fslabel": "root_a",
                "uuid": "aaaa"}
    probed: list[str] = []

    def _fake_probe(slot):
        probed.append(slot["device"])
        return INACTIVE_VERSION

    monkeypatch.setattr(slot_cli, "detect_active_inactive", lambda: (active, inactive))
    monkeypatch.setattr(slot_cli, "slot_version_label", _fake_probe)
    monkeypatch.setattr(slot_cli, "_read_active_appliance_version", lambda: ACTIVE_VERSION)
    monkeypatch.setattr(slot_cli, "_SLOT_VERSIONS_FILE", tmp_path / "slot-versions.json")
    return active, inactive, probed


def test_status_reports_the_active_slot_version(slot_cli, stubbed, capsys):
    _active, _inactive, _probed = stubbed

    assert slot_cli.cmd_status(None) == 0

    out = capsys.readouterr().out
    active_line = next(ln for ln in out.splitlines() if ln.startswith("Active slot:"))
    inactive_line = next(ln for ln in out.splitlines() if ln.startswith("Inactive slot:"))

    assert f"version={ACTIVE_VERSION}" in active_line
    assert f"version={INACTIVE_VERSION}" in inactive_line
    assert "unreadable" not in out


def test_status_never_mounts_the_active_slot(slot_cli, stubbed, capsys):
    """The regression guard: the booted device must never be mount-probed."""
    active, inactive, probed = stubbed

    slot_cli.cmd_status(None)
    capsys.readouterr()

    assert active["device"] not in probed
    assert probed == [inactive["device"]]


def test_status_refreshes_the_sidecar_the_ui_reads(slot_cli, stubbed, capsys):
    _active, _inactive, _probed = stubbed

    slot_cli.cmd_status(None)
    capsys.readouterr()

    written = json.loads(slot_cli._SLOT_VERSIONS_FILE.read_text())
    assert written == {"slot_a": INACTIVE_VERSION, "slot_b": ACTIVE_VERSION}


def test_status_exits_2_when_slots_are_undetectable(slot_cli, monkeypatch, capsys):
    monkeypatch.setattr(slot_cli, "detect_active_inactive", lambda: None)

    assert slot_cli.cmd_status(None) == 2
    assert "couldn't detect A/B slots" in capsys.readouterr().err
