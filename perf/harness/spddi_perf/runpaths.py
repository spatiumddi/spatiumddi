"""On-disk run-directory layout (docs/PERFORMANCE_TESTING.md §7.4).

Single source of truth for *where every artifact lives*, so the controller,
war-room poller, generators, and report generator all agree without hard-coding
paths. ``run_id = <UTC-start>-<manifest.name>-<short-uuid>``.

Layout (under ``perf/run/<run_id>/``):

    manifest.resolved.yaml
    state.json
    events.ndjson
    seed-manifest.json
    setpoints/{current.json, history.ndjson}
    snapshots/{t0_baseline, t+6h, ..., final}.json
    warroom/<surface>.ndjson
    generators/{perfdhcp.shardN.stat, dnsperf.stat, orchestrator.shardN.ndjson, lifecycle.ndjson}
    logs/{controller.log, worker.<name>.log}
    report/{summary.md, criteria.json}
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .logging_util import short_uuid, utc_stamp

# Default live-run root: perf/run/  (this file is perf/harness/spddi_perf/runpaths.py)
DEFAULT_RUN_ROOT = Path(__file__).resolve().parents[2] / "run"
REPORTS_ROOT = Path(__file__).resolve().parents[2] / "reports"

_SLUG = re.compile(r"[^a-z0-9._-]+")


def make_run_id(manifest_name: str) -> str:
    slug = _SLUG.sub("-", manifest_name.strip().lower()).strip("-") or "run"
    return f"{utc_stamp()}-{slug}-{short_uuid()}"


@dataclass(frozen=True)
class RunPaths:
    """All artifact paths for a single run. Use :meth:`for_run` / :meth:`create`."""

    run_id: str
    root: Path  # perf/run/<run_id>/

    # ---- factories ----
    @classmethod
    def for_run(cls, run_id: str, run_root: Path | str = DEFAULT_RUN_ROOT) -> "RunPaths":
        return cls(run_id=run_id, root=Path(run_root) / run_id)

    @classmethod
    def create(cls, manifest_name: str, run_root: Path | str = DEFAULT_RUN_ROOT) -> "RunPaths":
        rp = cls.for_run(make_run_id(manifest_name), run_root)
        rp.ensure_dirs()
        return rp

    def ensure_dirs(self) -> None:
        for d in (self.root, self.setpoints_dir, self.snapshots_dir, self.warroom_dir,
                  self.generators_dir, self.logs_dir, self.report_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- top-level files ----
    @property
    def manifest_resolved(self) -> Path: return self.root / "manifest.resolved.yaml"
    @property
    def state(self) -> Path: return self.root / "state.json"
    @property
    def events(self) -> Path: return self.root / "events.ndjson"
    @property
    def seed_manifest(self) -> Path: return self.root / "seed-manifest.json"
    @property
    def stop_file(self) -> Path: return self.root / "STOP"  # kill-switch sentinel (§7.6.1)

    # ---- subdirs ----
    @property
    def setpoints_dir(self) -> Path: return self.root / "setpoints"
    @property
    def setpoint_current(self) -> Path: return self.setpoints_dir / "current.json"
    @property
    def setpoint_history(self) -> Path: return self.setpoints_dir / "history.ndjson"

    @property
    def snapshots_dir(self) -> Path: return self.root / "snapshots"
    def snapshot(self, name: str) -> Path: return self.snapshots_dir / f"{name}.json"

    @property
    def warroom_dir(self) -> Path: return self.root / "warroom"
    def warroom(self, surface: str) -> Path: return self.warroom_dir / f"{surface}.ndjson"

    @property
    def generators_dir(self) -> Path: return self.root / "generators"
    def generator(self, name: str) -> Path: return self.generators_dir / name
    @property
    def lifecycle(self) -> Path: return self.generators_dir / "lifecycle.ndjson"

    @property
    def logs_dir(self) -> Path: return self.root / "logs"
    def log(self, name: str) -> Path: return self.logs_dir / f"{name}.log"
    @property
    def controller_log(self) -> Path: return self.log("controller")
    def worker_log(self, worker: str) -> Path: return self.log(f"worker.{worker}")

    @property
    def report_dir(self) -> Path: return self.root / "report"

    # ---- committed report destination (perf/reports/<run_id>/) ----
    @property
    def published_report_dir(self) -> Path: return REPORTS_ROOT / self.run_id
