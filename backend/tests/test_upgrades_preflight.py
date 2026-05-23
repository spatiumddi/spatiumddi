"""Unit tests for the multi-node rolling-upgrade preflight checks
(issue #296 Phase A).

Pure-Python — every check is independent + most don't actually need
the DB or kubeapi. We exercise:

* ``check_version_path`` — CalVer parse + forward-jump + 90-day-gap.
* ``check_disk_headroom`` — shutil.disk_usage threshold math.
* ``_parse_calver`` — tag parser corner cases.
* ``run_all`` overall verdict logic (worst-level wins, can_start
  derivation).
* Lease state parsing via ``mutex._parse_lease`` — RFC3339 +
  expiration math without touching kubeapi.

The DB-touching ``check_replication_lag`` + kubeapi-touching
``check_quorum`` + ``check_inflight_conflict`` are exercised under
integration tests once Phase D's full orchestrator path lands. Phase
A's read-only-only scope means a unit-tested aggregator + clean
fallback-on-unreachable paths are enough here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.services.upgrades import mutex, preflight

# ── check_version_path ────────────────────────────────────────────────


def test_version_path_forward_jump_ok() -> None:
    r = preflight.check_version_path(
        target_version="2026.06.01-1",
        current_version="2026.05.22-2",
    )
    assert r.level == "ok"
    assert r.detail["gap_days"] >= 0


def test_version_path_backwards_fails() -> None:
    r = preflight.check_version_path(
        target_version="2026.04.16-1",
        current_version="2026.05.22-2",
    )
    assert r.level == "fail"
    assert "not newer" in r.message


def test_version_path_same_version_fails() -> None:
    # Same tag => not strictly newer, refuse.
    r = preflight.check_version_path(
        target_version="2026.05.22-2",
        current_version="2026.05.22-2",
    )
    assert r.level == "fail"


def test_version_path_dev_current_warns() -> None:
    r = preflight.check_version_path(
        target_version="2026.06.01-1",
        current_version="dev",
    )
    assert r.level == "warn"
    assert "dev" in r.message


def test_version_path_unparseable_target_fails() -> None:
    r = preflight.check_version_path(
        target_version="latest",
        current_version="2026.05.22-2",
    )
    assert r.level == "fail"
    assert "parse" in r.message.lower()


def test_version_path_large_gap_warns() -> None:
    # ~6 months out should trip the >90-day warning.
    r = preflight.check_version_path(
        target_version="2026.12.01-1",
        current_version="2026.04.01-1",
    )
    assert r.level == "warn"
    assert r.detail["gap_days"] > 90


def test_version_path_minor_bump_ok() -> None:
    # Same-day -2 release is a forward jump (the -N segment increments).
    r = preflight.check_version_path(
        target_version="2026.05.22-3",
        current_version="2026.05.22-2",
    )
    assert r.level == "ok"


def test_parse_calver_corner_cases() -> None:
    assert preflight._parse_calver("2026.05.22-1") == (2026, 5, 22, 1)
    assert preflight._parse_calver("dev") is None
    assert preflight._parse_calver("2026-05-22") is None
    assert preflight._parse_calver("2026.5.22-1") is None  # need 2-digit month
    assert preflight._parse_calver("v2026.05.22-1") is None


# ── check_disk_headroom ───────────────────────────────────────────────


def test_disk_headroom_plenty() -> None:
    # Use the local /tmp which is always >5 GiB free on a dev box.
    r = preflight.check_disk_headroom(
        var_path="/tmp",
        slot_image_size_bytes=1024,
        safety_margin_bytes=1024,
    )
    assert r.level == "ok"
    assert r.detail["needed_bytes"] == 2048


def test_disk_headroom_insufficient() -> None:
    # Demand 1 PiB; any real path will refuse.
    r = preflight.check_disk_headroom(
        var_path="/tmp",
        slot_image_size_bytes=1024**5,
        safety_margin_bytes=0,
    )
    assert r.level == "fail"
    assert r.detail["needed_bytes"] == 1024**5


def test_disk_headroom_missing_path_warns() -> None:
    # disk_usage on a non-existent path raises OSError; we surface as warn.
    r = preflight.check_disk_headroom(
        var_path="/does/not/exist/anywhere/2026",
        slot_image_size_bytes=1,
        safety_margin_bytes=1,
    )
    assert r.level == "warn"
    assert "stat" in r.message


# ── mutex._parse_lease (no kubeapi) ───────────────────────────────────


def test_parse_lease_empty_body() -> None:
    s = mutex._parse_lease(None)
    assert s.held is False
    assert s.holder is None
    assert s.transitions == 0


def test_parse_lease_held_recent() -> None:
    # Fresh renewTime — should NOT be expired.
    from datetime import UTC, datetime  # noqa: PLC0415

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "spec": {
            "holderIdentity": "api-0",
            "renewTime": now,
            "leaseDurationSeconds": 60,
            "leaseTransitions": 3,
        }
    }
    s = mutex._parse_lease(body)
    assert s.holder == "api-0"
    assert s.held is True
    assert s.expired is False
    assert s.transitions == 3


def test_parse_lease_expired() -> None:
    # renewTime 10 minutes ago + 60s duration -> expired.
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    stale = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "spec": {
            "holderIdentity": "api-0",
            "renewTime": stale,
            "leaseDurationSeconds": 60,
        }
    }
    s = mutex._parse_lease(body)
    assert s.holder == "api-0"
    assert s.expired is True
    assert s.held is False  # expired => not held


def test_parse_lease_unparseable_time_treated_as_expired() -> None:
    body = {
        "spec": {
            "holderIdentity": "api-0",
            "renewTime": "not-a-timestamp",
        }
    }
    s = mutex._parse_lease(body)
    assert s.expired is True


# ── run_all aggregator (with mocked individual checks) ────────────────


@pytest.mark.asyncio
async def test_run_all_overall_ok() -> None:
    """All-ok individual results => overall ok + can_start True."""
    ok = preflight.PreflightResult(name="x", level="ok", message="fine", detail={})
    with (
        patch.object(preflight, "check_inflight_conflict", return_value=ok),
        patch.object(preflight, "check_disk_headroom", return_value=ok),
        patch.object(preflight, "check_version_path", return_value=ok),
        patch.object(preflight, "check_quorum", return_value=ok),
    ):
        # check_replication_lag is async; mock by patching the symbol
        async def _ok_async(**kw: Any) -> preflight.PreflightResult:
            return ok

        with patch.object(preflight, "check_replication_lag", _ok_async):
            report = await preflight.run_all(target_version="2026.06.01-1")
    assert report.overall == "ok"
    assert report.can_start is True
    assert len(report.results) == 5


@pytest.mark.asyncio
async def test_run_all_overall_warn() -> None:
    """One warn + rest ok => overall warn + can_start True."""
    ok = preflight.PreflightResult("x", "ok", "fine", {})
    warn = preflight.PreflightResult("y", "warn", "watch out", {})
    with (
        patch.object(preflight, "check_inflight_conflict", return_value=ok),
        patch.object(preflight, "check_disk_headroom", return_value=warn),
        patch.object(preflight, "check_version_path", return_value=ok),
        patch.object(preflight, "check_quorum", return_value=ok),
    ):

        async def _ok_async(**kw: Any) -> preflight.PreflightResult:
            return ok

        with patch.object(preflight, "check_replication_lag", _ok_async):
            report = await preflight.run_all(target_version="2026.06.01-1")
    assert report.overall == "warn"
    assert report.can_start is True


@pytest.mark.asyncio
async def test_run_all_overall_fail() -> None:
    """One fail anywhere => overall fail + can_start False."""
    ok = preflight.PreflightResult("x", "ok", "fine", {})
    bad = preflight.PreflightResult("y", "fail", "no", {})
    with (
        patch.object(preflight, "check_inflight_conflict", return_value=bad),
        patch.object(preflight, "check_disk_headroom", return_value=ok),
        patch.object(preflight, "check_version_path", return_value=ok),
        patch.object(preflight, "check_quorum", return_value=ok),
    ):

        async def _ok_async(**kw: Any) -> preflight.PreflightResult:
            return ok

        with patch.object(preflight, "check_replication_lag", _ok_async):
            report = await preflight.run_all(target_version="2026.06.01-1")
    assert report.overall == "fail"
    assert report.can_start is False


@pytest.mark.asyncio
async def test_run_all_to_dict_shape() -> None:
    """PreflightReport.to_dict produces a JSON-able envelope."""
    ok = preflight.PreflightResult("x", "ok", "fine", {"k": 1})
    with (
        patch.object(preflight, "check_inflight_conflict", return_value=ok),
        patch.object(preflight, "check_disk_headroom", return_value=ok),
        patch.object(preflight, "check_version_path", return_value=ok),
        patch.object(preflight, "check_quorum", return_value=ok),
    ):

        async def _ok_async(**kw: Any) -> preflight.PreflightResult:
            return ok

        with patch.object(preflight, "check_replication_lag", _ok_async):
            report = await preflight.run_all(target_version="2026.06.01-1")
    out = report.to_dict()
    assert set(out) == {
        "target_version",
        "current_version",
        "overall",
        "can_start",
        "results",
    }
    assert isinstance(out["results"], list)
    assert {"name", "level", "message", "detail"} <= set(out["results"][0])
