"""Host-portable pytest for spatium-host-migrate (#395).

These tests drive the orchestrator via a real subprocess against a fully
synthetic environment (PATCH_DIR + STATE_DIR in a tempdir, plus the
appliance-gate sidecar /etc/spatiumddi/role-config created in a tmpdir).

PATH OVERRIDES:
  spatium-host-migrate reads all of its paths via ``${SPATIUM_HOST_*:-…}``
  env overrides (defaulting to the production appliance locations), so these
  tests just point it at a tmp tree via the environment — no script rewriting.
  The overrides used: SPATIUM_HOST_PATCH_DIR, SPATIUM_HOST_TEMPLATE_VERSION_FILE,
  SPATIUM_HOST_STATE_DIR, SPATIUM_HOST_MIGRATE_LOG, SPATIUM_HOST_ROLE_CONFIG.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_host_migrate.py -v
    # or:
    pytest test_host_migrate.py -v

No database, no Docker, no appliance ISO required.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

ORCHESTRATOR = (
    Path(__file__).parent.parent / "mkosi.extra" / "usr" / "local" / "bin" /
    "spatium-host-migrate"
)
RENDERER = (
    Path(__file__).parent.parent / "mkosi.extra" / "usr" / "local" / "bin" /
    "spatium-grub-render"
)


def _run_migrate(tmp_path: Path, *, patch_dir: Path, baked_ver: str,
                 state_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the orchestrator in a synthetic tmp environment via env overrides.

    Writes the baked-version file + the role-config sidecar the appliance
    gate requires, then runs spatium-host-migrate with the SPATIUM_HOST_*
    env overrides pointing every path at the tmp tree (no script rewriting).
    Returns the CompletedProcess — callers assert on returncode.
    """
    baked_ver_file = tmp_path / "host-template-version"
    baked_ver_file.write_text(baked_ver + "\n", encoding="utf-8")
    state_dir.mkdir(parents=True, exist_ok=True)
    log_file = tmp_path / "firstboot.log"
    log_file.touch()

    # The appliance gate checks SPATIUM_HOST_ROLE_CONFIG — create it in the
    # tmp tree so the gate passes without touching the real /etc.
    role_config = tmp_path / "role-config"
    role_config.write_text("role=full-stack\n", encoding="utf-8")

    env = {
        **os.environ,
        "SPATIUM_HOST_PATCH_DIR": str(patch_dir),
        "SPATIUM_HOST_TEMPLATE_VERSION_FILE": str(baked_ver_file),
        "SPATIUM_HOST_STATE_DIR": str(state_dir),
        "SPATIUM_HOST_MIGRATE_LOG": str(log_file),
        "SPATIUM_HOST_ROLE_CONFIG": str(role_config),
    }
    return subprocess.run(
        ["sh", str(ORCHESTRATOR)],
        capture_output=True,
        text=True,
        env=env,
    )


def _read_ledger(state_dir: Path) -> dict:
    ledger_path = state_dir / "host-patches-applied.json"
    if not ledger_path.exists():
        return {}
    return json.loads(ledger_path.read_text(encoding="utf-8"))


def _read_applied_ver(state_dir: Path) -> str | None:
    ver_file = state_dir / "host-template-version"
    if not ver_file.exists():
        return None
    return ver_file.read_text(encoding="utf-8").strip()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Fresh box — no /var stamp, no ledger → patch 001 runs, ledger written,
#    host-template-version stamped.
# ─────────────────────────────────────────────────────────────────────────────

def test_fresh_run_applies_patch_and_stamps_version(tmp_path: Path) -> None:
    """First run on a fresh box: patch runs, ledger written, version stamped."""
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"

    # Create a simple no-op patch that exits 0.
    patch = patch_dir / "001-noop.sh"
    patch.write_text("#!/bin/sh\n# test no-op patch\nexit 0\n", encoding="utf-8")
    patch.chmod(0o755)

    result = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)

    assert result.returncode == 0, (
        f"Expected returncode 0, got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # Ledger must record the patch as ok.
    ledger = _read_ledger(state_dir)
    assert ledger.get("last_reconcile_ok") is True, f"last_reconcile_ok is not True: {ledger}"
    assert ledger.get("template_version_applied") == "1"
    patches = ledger.get("patches", {})
    assert "001-noop" in patches, f"001-noop not in patches: {patches}"
    assert patches["001-noop"]["ok"] is True

    # Applied-version file must be stamped.
    applied = _read_applied_ver(state_dir)
    assert applied == "1", f"host-template-version stamp expected '1', got {applied!r}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Second run, same version, ledger ok → no-op.
# ─────────────────────────────────────────────────────────────────────────────

def test_second_run_same_version_noop(tmp_path: Path) -> None:
    """When the ledger already marks all patches ok and version matches, loop is a no-op."""
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # A patch that exits 0.
    patch = patch_dir / "001-noop.sh"
    patch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    patch.chmod(0o755)

    # First run — gets ledger + stamp written.
    r1 = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)
    assert r1.returncode == 0, f"First run failed: {r1.stderr}"

    # Second run — same version, same ledger.
    r2 = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)
    assert r2.returncode == 0, f"Second run failed: {r2.stderr}"

    ledger_after_second = _read_ledger(state_dir)
    # last_reconcile_ok still true.
    assert ledger_after_second.get("last_reconcile_ok") is True
    # Patch still ok.
    assert ledger_after_second["patches"]["001-noop"]["ok"] is True
    # Applied version unchanged.
    assert _read_applied_ver(state_dir) == "1"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Failure path — patch returning non-zero → ledger records ok=false +
#    fail_count, version NOT stamped, orchestrator returns 1.
# ─────────────────────────────────────────────────────────────────────────────

def test_failing_patch_records_failure_and_blocks_stamp(tmp_path: Path) -> None:
    """A patch that exits non-zero: ledger ok=false, fail_count=1, version not stamped."""
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"

    # A patch that deliberately fails.
    patch = patch_dir / "001-fail.sh"
    patch.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
    patch.chmod(0o755)

    result = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)

    assert result.returncode == 1, (
        f"Expected returncode 1 from failing patch, got {result.returncode}"
    )

    ledger = _read_ledger(state_dir)
    assert ledger.get("last_reconcile_ok") is False
    patches = ledger.get("patches", {})
    assert "001-fail" in patches
    patch_entry = patches["001-fail"]
    assert patch_entry["ok"] is False
    assert patch_entry.get("fail_count", 0) >= 1, "fail_count not incremented"
    assert "error" in patch_entry, "error field missing on failed patch entry"

    # Version must NOT be stamped.
    assert _read_applied_ver(state_dir) is None, (
        "host-template-version was stamped even though a patch failed"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Self-heal — a previously-failed patch is retried on the next run.
# ─────────────────────────────────────────────────────────────────────────────

def test_failed_patch_retried_on_next_run(tmp_path: Path) -> None:
    """A patch that failed last boot must be retried next run, even if version matches."""
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"

    # First: patch fails.
    fail_patch = patch_dir / "001-conditional.sh"
    fail_patch.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fail_patch.chmod(0o755)

    r1 = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)
    assert r1.returncode == 1, "Expected failure on first run"

    ledger = _read_ledger(state_dir)
    assert ledger["patches"]["001-conditional"]["ok"] is False
    assert ledger["patches"]["001-conditional"]["fail_count"] == 1

    # Second: now make the patch succeed.
    fail_patch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    r2 = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)
    assert r2.returncode == 0, (
        f"Expected success on second run after patch fixed. stderr: {r2.stderr}"
    )

    ledger2 = _read_ledger(state_dir)
    assert ledger2["patches"]["001-conditional"]["ok"] is True, (
        "Patch should be marked ok after successful retry"
    )
    assert ledger2["patches"]["001-conditional"]["fail_count"] == 0, (
        "fail_count should be reset to 0 on successful apply"
    )
    assert ledger2.get("last_reconcile_ok") is True
    assert _read_applied_ver(state_dir) == "1"


# ─────────────────────────────────────────────────────────────────────────────
# 5. fail_count increments across multiple failures.
# ─────────────────────────────────────────────────────────────────────────────

def test_fail_count_increments_across_multiple_failures(tmp_path: Path) -> None:
    """Each failed run must increment fail_count in the ledger."""
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"

    fail_patch = patch_dir / "001-alwaysfail.sh"
    fail_patch.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    fail_patch.chmod(0o755)

    for expected_count in range(1, 4):
        result = _run_migrate(
            tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir
        )
        assert result.returncode == 1
        ledger = _read_ledger(state_dir)
        actual = ledger["patches"]["001-alwaysfail"]["fail_count"]
        assert actual == expected_count, (
            f"After {expected_count} failure(s), expected fail_count={expected_count}, "
            f"got {actual}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6. New patch added with bumped version — runs; already-ok patches skipped.
# ─────────────────────────────────────────────────────────────────────────────

def test_new_patch_runs_when_version_bumped(tmp_path: Path) -> None:
    """Adding a second patch with a bumped baked-version causes it to run.

    The already-ok 001 patch must be skipped (ledger says ok); 002 must run.
    """
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"

    # First boot — only patch 001.
    patch1 = patch_dir / "001-first.sh"
    patch1.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    patch1.chmod(0o755)

    r1 = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)
    assert r1.returncode == 0

    # Add a second patch + bump version to 2.
    patch2 = patch_dir / "002-second.sh"
    sentinel = tmp_path / "sentinel-002-ran"
    patch2.write_text(
        f"#!/bin/sh\ntouch {sentinel}\nexit 0\n",
        encoding="utf-8",
    )
    patch2.chmod(0o755)

    r2 = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="2", state_dir=state_dir)
    assert r2.returncode == 0, f"Second run failed: {r2.stderr}"

    # 002 must have run (sentinel exists).
    assert sentinel.exists(), "002 patch sentinel not created — patch did not run"

    # Ledger must record both as ok.
    ledger = _read_ledger(state_dir)
    assert ledger["patches"]["001-first"]["ok"] is True
    assert ledger["patches"]["002-second"]["ok"] is True
    assert ledger.get("template_version_applied") == "2"
    assert _read_applied_ver(state_dir) == "2"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Already-ok patches are NOT re-run (idempotence gate in the ledger).
# ─────────────────────────────────────────────────────────────────────────────

def test_already_ok_patch_skipped_on_version_bump(tmp_path: Path) -> None:
    """An already-ok patch must not re-run even when the baked version bumps.

    Correct: the loop skips ledger-ok patches regardless of version.
    """
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"

    sentinel = tmp_path / "run-count"
    sentinel.write_text("0", encoding="utf-8")

    # A patch that increments a counter file.
    counter_patch = patch_dir / "001-counter.sh"
    counter_patch.write_text(
        textwrap.dedent(f"""\
            #!/bin/sh
            n=$(cat {sentinel} 2>/dev/null || echo 0)
            n=$((n + 1))
            printf '%s\\n' "$n" > {sentinel}
            exit 0
        """),
        encoding="utf-8",
    )
    counter_patch.chmod(0o755)

    # Run twice with bumped versions.
    _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)
    _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="2", state_dir=state_dir)

    count = int(sentinel.read_text(encoding="utf-8").strip())
    assert count == 1, (
        f"001-counter ran {count} time(s); expected exactly 1 (second run should skip it)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 8. Patch ordering — lexical sort (001 before 002 before 010 etc.)
# ─────────────────────────────────────────────────────────────────────────────

def test_patches_run_in_lexical_order(tmp_path: Path) -> None:
    """Patches must run in NNN-sorted order (001 → 002 → 003)."""
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"
    order_log = tmp_path / "order.log"

    for n in ("003", "001", "002"):
        p = patch_dir / f"{n}-test.sh"
        p.write_text(
            f"#!/bin/sh\necho {n} >> {order_log}\nexit 0\n", encoding="utf-8"
        )
        p.chmod(0o755)

    result = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)
    assert result.returncode == 0

    ran_order = order_log.read_text(encoding="utf-8").strip().split("\n")
    assert ran_order == ["001", "002", "003"], (
        f"Patches ran in wrong order: {ran_order}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 9. Stop-at-first-failure — subsequent patches must NOT run after a failure.
# ─────────────────────────────────────────────────────────────────────────────

def test_stop_at_first_failure(tmp_path: Path) -> None:
    """When patch 001 fails, patch 002 must NOT run."""
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"

    sentinel = tmp_path / "sentinel-002-ran"

    p1 = patch_dir / "001-fail.sh"
    p1.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    p1.chmod(0o755)

    p2 = patch_dir / "002-should-not-run.sh"
    p2.write_text(
        f"#!/bin/sh\ntouch {sentinel}\nexit 0\n", encoding="utf-8"
    )
    p2.chmod(0o755)

    result = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)
    assert result.returncode == 1
    assert not sentinel.exists(), (
        "002 patch ran even though 001 failed — stop-at-first-failure violated"
    )

    ledger = _read_ledger(state_dir)
    assert "002-should-not-run" not in ledger.get("patches", {}), (
        "002 should have no ledger entry if it never ran"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 10. Empty patch directory — exits 0 cleanly (no patches = nothing to do).
#     The version IS stamped because the loop succeeded vacuously.
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_patch_dir_succeeds(tmp_path: Path) -> None:
    """An empty patch directory should result in immediate clean exit."""
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"

    result = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)
    assert result.returncode == 0, (
        f"Expected 0 from empty patch dir, got {result.returncode}"
    )
    ledger = _read_ledger(state_dir)
    assert ledger.get("last_reconcile_ok") is True
    assert _read_applied_ver(state_dir) == "1"


# ─────────────────────────────────────────────────────────────────────────────
# 11. Ledger shape — verify required keys are present on success.
# ─────────────────────────────────────────────────────────────────────────────

def test_ledger_shape_on_success(tmp_path: Path) -> None:
    """On success, the ledger must contain all required top-level keys."""
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"

    p = patch_dir / "001-noop.sh"
    p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    p.chmod(0o755)

    result = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)
    assert result.returncode == 0

    ledger = _read_ledger(state_dir)
    for key in ("last_reconcile_at", "last_reconcile_ok", "template_version_applied", "patches"):
        assert key in ledger, f"Required key {key!r} missing from ledger"

    patch_entry = ledger["patches"]["001-noop"]
    for key in ("applied_at", "ok", "fail_count"):
        assert key in patch_entry, f"Required patch key {key!r} missing from ledger entry"


def test_ledger_shape_on_failure(tmp_path: Path) -> None:
    """On failure, the ledger must contain required keys including the error field."""
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"

    p = patch_dir / "001-fail.sh"
    p.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    p.chmod(0o755)

    result = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)
    assert result.returncode == 1

    ledger = _read_ledger(state_dir)
    assert "last_reconcile_at" in ledger
    assert ledger["last_reconcile_ok"] is False
    # template_version_applied must NOT be set on failure.
    assert "template_version_applied" not in ledger or ledger["template_version_applied"] == ""

    patch_entry = ledger["patches"]["001-fail"]
    assert patch_entry["ok"] is False
    assert "error" in patch_entry
    assert patch_entry["fail_count"] >= 1


# ─────────────────────────────────────────────────────────────────────────────
# 12. GUARD-RAIL — broken renderer: ledger records failure, version not stamped.
#
# This uses a stub patch that mimics what the real 001-grub-render.sh does
# (exec spatium-grub-render) but with a stub that always exits non-zero.
# It proves the orchestrator correctly propagates the renderer failure into
# the ledger and blocks the version stamp.
# ─────────────────────────────────────────────────────────────────────────────

def test_broken_renderer_stub_fails_ledger_and_blocks_stamp(tmp_path: Path) -> None:
    """A patch that wraps a broken renderer (exits non-zero) must fail the ledger
    and leave the version stamp unwritten.
    """
    patch_dir = tmp_path / "host-patches"
    patch_dir.mkdir()
    state_dir = tmp_path / "state"

    # Stub renderer that always fails.
    stub_renderer = tmp_path / "stub-grub-render"
    stub_renderer.write_text("#!/bin/sh\nexec false\n", encoding="utf-8")
    stub_renderer.chmod(0o755)

    # Patch that execs the stub renderer (mirrors 001-grub-render.sh which does
    # `exec /usr/local/bin/spatium-grub-render`).
    patch = patch_dir / "001-grub-render.sh"
    patch.write_text(
        f"#!/bin/sh\nexec {stub_renderer}\n", encoding="utf-8"
    )
    patch.chmod(0o755)

    result = _run_migrate(tmp_path, patch_dir=patch_dir, baked_ver="1", state_dir=state_dir)
    assert result.returncode == 1, (
        "Expected failure when stub renderer exits non-zero"
    )

    ledger = _read_ledger(state_dir)
    assert ledger.get("last_reconcile_ok") is False
    assert ledger["patches"]["001-grub-render"]["ok"] is False
    assert _read_applied_ver(state_dir) is None, (
        "Version stamp must not be written when the renderer fails"
    )
