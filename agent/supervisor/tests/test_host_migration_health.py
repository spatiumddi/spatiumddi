"""Host-migration reconcile health rollup (#395).

``read_host_migration_health()`` reads the ``host-patches-applied.json``
ledger written by ``spatium-host-migrate`` and surfaces, on the supervisor
heartbeat, an entry per patch whose ``ok`` field is ``False`` so the Fleet
UI shows a failed grub.cfg re-render (or any future numbered host-patch)
instead of it silently blocking the slot commit. A box with every patch
applied returns ``{}`` — which clears any stale server-side entry, mirroring
``read_host_config_health()`` (#387). These tests cover the ledger → rollup
mapping, the ``fail_count`` → ``attempts`` surfacing, and the synthetic
``reconcile`` entry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spatium_supervisor import appliance_state


@pytest.fixture
def ledger_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    p = tmp_path / "host-patches-applied.json"
    monkeypatch.setattr(appliance_state, "_HOST_PATCHES_LEDGER", p)
    return p


def _write(ledger_path: Path, data: dict) -> None:
    ledger_path.write_text(json.dumps(data), encoding="utf-8")


def test_no_ledger_returns_empty(ledger_path: Path) -> None:
    # File absent → nothing to report (box never booted a #395 slot).
    assert not ledger_path.exists()
    assert appliance_state.read_host_migration_health() == {}


def test_all_patches_ok_returns_empty(ledger_path: Path) -> None:
    # Healthy box clears any stale server entry with {}.
    _write(
        ledger_path,
        {
            "last_reconcile_ok": True,
            "template_version_applied": "1",
            "patches": {
                "001-grub-render": {
                    "applied_at": "2026-06-12T00:00:00+00:00",
                    "ok": True,
                    "fail_count": 0,
                }
            },
        },
    )
    assert appliance_state.read_host_migration_health() == {}


def test_failing_patch_surfaces_with_fail_count_as_attempts(ledger_path: Path) -> None:
    _write(
        ledger_path,
        {
            "last_reconcile_ok": False,
            "patches": {
                "001-grub-render": {
                    "applied_at": "2026-06-12T01:02:03+00:00",
                    "ok": False,
                    "fail_count": 3,
                    "error": "exit 2",
                }
            },
        },
    )
    out = appliance_state.read_host_migration_health()
    assert out == {
        "001-grub-render": {
            "state": "failing",
            "attempts": 3,  # real consecutive-boot count from the ledger
            "at": "2026-06-12T01:02:03+00:00",
            "error": "exit 2",
        }
    }


def test_missing_fail_count_floors_attempts_at_one(ledger_path: Path) -> None:
    # A freshly-failed patch (or a pre-fail_count ledger) reports 1, never 0.
    _write(
        ledger_path,
        {
            "last_reconcile_ok": False,
            "patches": {"001-grub-render": {"applied_at": "x", "ok": False}},
        },
    )
    out = appliance_state.read_host_migration_health()
    assert out["001-grub-render"]["attempts"] == 1


def test_ok_patches_omitted_only_failing_surface(ledger_path: Path) -> None:
    _write(
        ledger_path,
        {
            "last_reconcile_ok": False,
            "patches": {
                "001-grub-render": {"ok": True, "fail_count": 0, "applied_at": "a"},
                "002-something": {
                    "ok": False,
                    "fail_count": 1,
                    "applied_at": "b",
                    "error": "exit 1",
                },
            },
        },
    )
    out = appliance_state.read_host_migration_health()
    assert set(out) == {"002-something"}


def test_synthetic_reconcile_entry_when_reconcile_failed_no_patch_flagged(
    ledger_path: Path,
) -> None:
    # last_reconcile_ok False but every patch ok (e.g. the version-stamp write
    # failed) → surface one honest synthetic entry rather than a silent {}.
    _write(
        ledger_path,
        {
            "last_reconcile_ok": False,
            "last_reconcile_at": "2026-06-12T09:00:00+00:00",
            "patches": {
                "001-grub-render": {"ok": True, "fail_count": 0, "applied_at": "a"}
            },
        },
    )
    out = appliance_state.read_host_migration_health()
    assert out == {
        "reconcile": {
            "state": "failing",
            "attempts": 1,
            "at": "2026-06-12T09:00:00+00:00",
        }
    }


def test_malformed_ledger_returns_empty(ledger_path: Path) -> None:
    ledger_path.write_text("{ not json", encoding="utf-8")
    assert appliance_state.read_host_migration_health() == {}


def test_non_dict_ledger_returns_empty(ledger_path: Path) -> None:
    ledger_path.write_text("[]", encoding="utf-8")
    assert appliance_state.read_host_migration_health() == {}
