"""Run monitor + control surface for the TUI.

Reads everything the headless path writes (state.json, the current setpoint, the
war-room NDJSON tails, events, generator stats) into a single :class:`RunSnapshot`,
and exposes the SAME control primitives the CLI uses: spawn a run, trip the
kill-switch, abort the controller process, resume, trigger a prune. No new control
path — Start literally shells out to ``spddi_perf.cli run``.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import checkpoint as ckpt
from .. import workers
from ..logging_util import read_json
from ..runpaths import RunPaths
from ..watchdog import _last_json_line


def _tail_ndjson(path: Path, n: int) -> list[dict]:
    """Return the last ``n`` JSON objects from an NDJSON file (best effort)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    import json
    out: list[dict] = []
    for line in lines[-n:]:
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


@dataclass
class RunSnapshot:
    run_id: str
    state: dict = field(default_factory=dict)
    setpoint: dict = field(default_factory=dict)
    health: dict = field(default_factory=dict)
    pg_overview: dict = field(default_factory=dict)
    pg_connections: dict = field(default_factory=dict)
    pg_tables: dict = field(default_factory=dict)
    pg_locks: dict = field(default_factory=dict)
    redis_overview: dict = field(default_factory=dict)
    celery_queues: dict = field(default_factory=dict)
    domain_counts: dict = field(default_factory=dict)
    metrics_dns: dict = field(default_factory=dict)
    metrics_dhcp: dict = field(default_factory=dict)
    generators: dict = field(default_factory=dict)  # name -> latest stat dict
    events: list = field(default_factory=list)


class RunMonitor:
    def __init__(self, run_root: str | Path, run_id: str | None = None,
                 manifest_path: str | None = None) -> None:
        self.run_root = str(run_root)
        self.run_id = run_id
        self.manifest_path = manifest_path
        self._proc: subprocess.Popen | None = None  # the controller, if WE started it

    # ---- run discovery ----
    def latest_run_id(self) -> str | None:
        root = Path(self.run_root)
        if not root.exists():
            return None
        runs = [d for d in root.iterdir() if d.is_dir() and (d / "state.json").exists()]
        return sorted(runs, key=lambda d: d.name)[-1].name if runs else None

    def attach_latest(self) -> str | None:
        self.run_id = self.latest_run_id()
        return self.run_id

    @property
    def rp(self) -> RunPaths | None:
        return RunPaths.for_run(self.run_id, self.run_root) if self.run_id else None

    # ---- snapshot ----
    def snapshot(self) -> RunSnapshot | None:
        rp = self.rp
        if not rp:
            return None
        w = rp.warroom
        gens: dict = {}
        gdir = rp.generators_dir
        if gdir.exists():
            for f in sorted(gdir.glob("*.ndjson")):
                last = _last_json_line(f)
                if last:
                    gens[f.stem] = last
        return RunSnapshot(
            run_id=self.run_id or "",
            state=read_json(rp.state) or {},
            setpoint=read_json(rp.setpoint_current) or {},
            health=_last_json_line(w("health_platform")) or {},
            pg_overview=_last_json_line(w("pg_overview")) or {},
            pg_connections=_last_json_line(w("pg_connections")) or {},
            pg_tables=_last_json_line(w("pg_tables")) or {},
            pg_locks=_last_json_line(w("pg_locks")) or {},
            redis_overview=_last_json_line(w("redis_overview")) or {},
            celery_queues=_last_json_line(w("celery_queues")) or {},
            domain_counts=_last_json_line(w("domain_counts")) or {},
            metrics_dns=_last_json_line(w("metrics_dns")) or {},
            metrics_dhcp=_last_json_line(w("metrics_dhcp")) or {},
            generators=gens,
            events=_tail_ndjson(rp.events, 12),
        )

    # ---- control (the same primitives the CLI uses) ----
    def controller_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start_run(self, manifest_path: str, *, extra_args: list[str] | None = None) -> str | None:
        """Spawn `spddi_perf.cli run` and resolve the new run_id from the run dir."""
        before = set(p.name for p in Path(self.run_root).glob("*")) if Path(self.run_root).exists() else set()
        argv = [sys.executable, "-m", "spddi_perf.cli", "run",
                "--manifest", manifest_path, "--run-root", self.run_root]
        argv += extra_args or []
        self.manifest_path = manifest_path
        self._proc = subprocess.Popen(argv, env=workers.child_env(),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Poll briefly for the freshly-created run dir.
        for _ in range(50):
            time.sleep(0.1)
            now = set(p.name for p in Path(self.run_root).glob("*")) if Path(self.run_root).exists() else set()
            new = sorted(now - before)
            if new:
                self.run_id = new[-1]
                return self.run_id
        return None

    def resume_run(self) -> str | None:
        if not self.run_id:
            return None
        state = ckpt.read_state(self.rp)  # type: ignore[arg-type]
        manifest = (state.manifest_path if state else None) or self.manifest_path
        if not manifest:
            return None
        argv = [sys.executable, "-m", "spddi_perf.cli", "resume",
                "--run-id", self.run_id, "--run-root", self.run_root]
        self._proc = subprocess.Popen(argv, env=workers.child_env(),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self.run_id

    def stop_run(self) -> bool:
        """Trip the kill-switch (graceful ramp-to-zero). Works even on a run we didn't start."""
        rp = self.rp
        if not rp:
            return False
        rp.stop_file.touch()
        return True

    def abort_run(self) -> bool:
        """Hard stop: kill-switch + terminate the controller process if we own it."""
        ok = self.stop_run()
        if self.controller_alive():
            self._proc.terminate()  # type: ignore[union-attr]
        return ok

    def trigger_prune(self) -> int:
        rp = self.rp
        if not rp or not self.manifest_path:
            return -1
        return workers.run_oneshot("trigger_prune", run_id=self.run_id, run_root=self.run_root,
                                   manifest=self.manifest_path, logfile=rp.worker_log("trigger_prune"))
