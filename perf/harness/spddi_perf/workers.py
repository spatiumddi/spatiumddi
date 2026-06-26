"""Worker launch registry — the CLI contract between the controller and the leaf
components (seeder / generators / war-room / report).

Components are coupled to the harness ONLY through (1) this argv contract and (2) the
setpoint-bus files. Every worker is invoked as::

    python3 <perf/.../script.py> --run-id <id> --run-root <path> --manifest <path> [extra]

and is expected to:
  * construct ``RunPaths.for_run(run_id, run_root)`` to find its output paths,
  * (long-running workers) poll ``setpoints.read_current(rp)`` each loop and true-up,
  * read any secret (admin token / psql DSN) from the env var NAMED in the manifest,
  * write structured JSON logs to stderr (the controller tees them to a logfile).

Missing component scripts degrade gracefully (logged WARNING, treated as skipped) so
the harness is runnable while the leaf components are still being built.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .logging_util import get_logger

PERF_DIR = Path(__file__).resolve().parents[2]            # .../perf
HARNESS_DIR = PERF_DIR / "harness"                        # on PYTHONPATH for `import spddi_perf`

ONESHOT = "oneshot"
LONGRUNNING = "longrunning"


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    script: str        # path relative to perf/
    kind: str          # ONESHOT | LONGRUNNING


# The registry. Script paths are the contract the parallel component build targets.
REGISTRY: dict[str, WorkerSpec] = {
    # --- pre-load oneshots (provision + seed) ---
    "phase0_verify": WorkerSpec("phase0_verify", "seeder/phase0_verify.py", ONESHOT),
    "pgss_enable":   WorkerSpec("pgss_enable",   "seeder/pgss_enable.py",   ONESHOT),
    "fleet_enable":  WorkerSpec("fleet_enable",  "seeder/fleet_enable.py",  ONESHOT),
    "seed_scaffold": WorkerSpec("seed_scaffold", "seeder/seed_scaffold.py", ONESHOT),
    # generate (+ §4.9 in-zone-validate) the DNS query set the dnsperf workers consume;
    # runs after seed_scaffold because it reads the seed-manifest's real zones.
    "gen_queryset": WorkerSpec("gen_queryset", "generators/dns/gen_dns_queryset.py", ONESHOT),
    # --- mid-run oneshot hook ---
    "trigger_prune": WorkerSpec("trigger_prune", "seeder/trigger_prune.py", ONESHOT),
    # --- long-running observers ---
    "warroom_poller": WorkerSpec("warroom_poller", "warroom/poller.py",    LONGRUNNING),
    "psql_probe":     WorkerSpec("psql_probe",     "warroom/psql_probe.py", LONGRUNNING),
    # --- long-running generators (the realistic diurnal load) ---
    "orchestrator":   WorkerSpec("orchestrator",   "generators/orchestrator/device_fleet.py", LONGRUNNING),
    "api_mutation":   WorkerSpec("api_mutation",   "generators/orchestrator/api_mutation_stream.py", LONGRUNNING),
    "ui_probe":       WorkerSpec("ui_probe",       "generators/orchestrator/synthetic_ui_probe.py", LONGRUNNING),
    # --- bounded ceiling tools (launched in the peak phase) ---
    "perfdhcp":       WorkerSpec("perfdhcp",       "generators/dhcp/perfdhcp_shard.py", LONGRUNNING),
    "dnsperf":        WorkerSpec("dnsperf",        "generators/dns/dnsperf_runner.py", LONGRUNNING),
}

_log = get_logger("spddi_perf.workers")


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    parts = [str(HARNESS_DIR)] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    # Workers are launched with stderr→logfile (the oneshot/longrunning helpers
    # redirect the child's stdout+stderr into the per-worker logfile). Their
    # get_logger must therefore NOT also add its own FileHandler to that same
    # file, or every JSON line lands twice (perf #454). This marker tells
    # get_logger to stay stderr-only so the redirect is the sole file writer.
    env["SPDDI_PERF_LOG_VIA_STDERR"] = "1"
    return env


def _argv(spec: WorkerSpec, *, run_id: str, run_root: str, manifest: str, **extra: object) -> list[str]:
    argv = [sys.executable, str(PERF_DIR / spec.script),
            "--run-id", run_id, "--run-root", run_root, "--manifest", manifest]
    for k, v in extra.items():
        if v is None:
            continue
        argv += [f"--{k.replace('_', '-')}", str(v)]
    return argv


def script_exists(name: str) -> bool:
    return (PERF_DIR / REGISTRY[name].script).exists()


def run_oneshot(name: str, *, run_id: str, run_root: str, manifest: str,
                logfile: str | os.PathLike[str] | None = None, timeout: float | None = None,
                **extra: object) -> int:
    """Run a oneshot worker to completion. Returns its exit code (or -1 if missing)."""
    spec = REGISTRY[name]
    if not script_exists(name):
        _log.warning("oneshot %s skipped — script not found at %s", name, spec.script)
        return -1
    argv = _argv(spec, run_id=run_id, run_root=run_root, manifest=manifest, **extra)
    out = open(logfile, "a") if logfile else None
    try:
        rc = subprocess.run(argv, env=child_env(), stdout=out, stderr=subprocess.STDOUT,
                            timeout=timeout).returncode
        _log.info("oneshot %s exited rc=%s", name, rc)
        return rc
    except subprocess.TimeoutExpired:
        # A hung oneshot (e.g. wrong/unreachable target.node_ip) must NOT wedge the
        # controller in "provisioning" forever — fail loudly with rc=124.
        _log.error("oneshot %s timed out after %ss (check target.node_ip + connectivity)",
                   name, timeout)
        return 124
    finally:
        if out:
            out.close()


def launch(name: str, *, run_id: str, run_root: str, manifest: str,
           logfile: str | os.PathLike[str] | None = None, **extra: object) -> subprocess.Popen | None:
    """Launch a long-running worker. Returns the Popen (or None if the script is absent)."""
    spec = REGISTRY[name]
    if not script_exists(name):
        _log.warning("worker %s not launched — script not found at %s", name, spec.script)
        return None
    argv = _argv(spec, run_id=run_id, run_root=run_root, manifest=manifest, **extra)
    # Open the logfile inside a context manager so the parent-side fd is always
    # closed (even if Popen raises). The child keeps its own duplicated descriptor
    # for the stdout/stderr redirect, so functionality is unchanged.
    if logfile:
        with open(logfile, "a") as out:
            proc = subprocess.Popen(argv, env=child_env(), stdout=out, stderr=subprocess.STDOUT)
    else:
        proc = subprocess.Popen(argv, env=child_env(), stdout=None, stderr=subprocess.STDOUT)
    _log.info("launched worker %s pid=%s", name, proc.pid)
    return proc
