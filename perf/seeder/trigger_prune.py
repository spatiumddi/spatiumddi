#!/usr/bin/env python3
"""trigger_prune — kick the daily log-prune NOW (§7.6.7 manual-prune hook).

``app.tasks.prune_logs.prune_log_entries`` is scheduled ``run_every=24h`` from
worker boot, NOT at a wall-clock time (celery_app.py:259-261). In a fresh-boot 24h
run it may fire AFTER tEnd, so the harness must trigger it at a chosen in-window
time to exercise the single-unchunked-DELETE prune path (prune_logs.py:42-45) under
load — the §7.6.7 "kick the prune now" hook.

NO first-class trigger endpoint exists today (grep of backend/app/api/v1 + tasks +
celery_app is clean — there's no admin "run task" route). So this is a BEST-EFFORT
``kubectl exec`` into a worker pod that runs ``celery ... call`` for the task. It
records an ``open_item`` recommending a first-class "kick prune" endpoint be added.

GROUNDING (real backend / infra — cited inline):
  * task name ``app.tasks.prune_logs.prune_log_entries`` — prune_logs.py:58.
  * scheduled run_every=24h (not wall-clock) — celery_app.py:259-261.
  * no admin trigger endpoint exists — open_item below.
  * worker Deployment ``<release>-worker`` (label component=worker), runs
    ``celery -A app.celery_app``, consumes the ``default`` queue —
    charts/spatiumddi/templates/worker.yaml:5,44-52; values.yaml:260.

Extras (optional, env- or flag-driven; defaults match the appliance chart):
  --namespace        k8s namespace (default $SPDDI_PERF_K8S_NAMESPACE or 'spatiumddi')
  --worker-selector  pod label selector (default 'component=worker')
  --kubectl          kubectl binary / wrapper (default $SPDDI_PERF_KUBECTL or 'kubectl')

Usage:  python3 trigger_prune.py --run-id <id> --run-root <path> --manifest <path>
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from typing import Any

import spddi_perf.manifest as manifest_mod
from spddi_perf.logging_util import atomic_write_json, get_logger, log_event, utc_now_iso
from spddi_perf.runpaths import RunPaths

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

TASK = "app.tasks.prune_logs.prune_log_entries"
QUEUE = "default"

OPEN_ITEM = (
    "No first-class 'kick prune' endpoint exists — prune_log_entries is "
    "schedule-only (celery_app.py:259, run_every=24h from worker boot). This hook "
    "falls back to `kubectl exec ... celery call`. Recommend adding an admin "
    "POST /api/v1/admin/maintenance/run-task (or /admin/postgres/prune-logs) that "
    "dispatches the task via .apply_async() so the harness has an API-only path."
)


def _run(cmd: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _find_worker_pod(kubectl: str, ns: str, selector: str, log: logging.Logger) -> str | None:
    cmd = [kubectl, "-n", ns, "get", "pods", "-l", selector,
           "--field-selector=status.phase=Running",
           "-o", "jsonpath={.items[0].metadata.name}"]
    proc = _run(cmd)
    if proc.returncode != 0:
        log_event(log, logging.WARNING, "find_worker_pod_failed",
                  rc=proc.returncode, stderr=proc.stderr.strip()[:300])
        return None
    name = proc.stdout.strip()
    return name or None


def _celery_call(kubectl: str, ns: str, pod: str, log: logging.Logger) -> tuple[bool, str]:
    # `celery -A app.celery_app call <task> --queue default` enqueues the task on
    # the worker's broker; the worker (which consumes `default`) picks it up.
    cmd = [kubectl, "-n", ns, "exec", pod, "-c", "worker", "--",
           "celery", "-A", "app.celery_app", "call", TASK, "--queue", QUEUE]
    proc = _run(cmd, timeout=90.0)
    out = (proc.stdout + proc.stderr).strip()
    ok = proc.returncode == 0
    log_event(log, logging.INFO if ok else logging.WARNING, "celery_call",
              rc=proc.returncode, output=out[:400])
    return ok, out


def run(rp: RunPaths, m: manifest_mod.Manifest, log: logging.Logger,
        kubectl: str, ns: str, selector: str) -> int:
    result: dict[str, Any] = {
        "ts": utc_now_iso(), "run_id": rp.run_id, "task": TASK, "queue": QUEUE,
        "method": "kubectl-exec-celery-call", "open_item": OPEN_ITEM,
        "namespace": ns, "selector": selector,
    }
    rc = 0

    if not shutil.which(kubectl) and "/" not in kubectl:
        log.error("kubectl (%r) not found on PATH — cannot trigger prune off-box", kubectl)
        result["error"] = f"{kubectl} not found"
        rc = 7
    else:
        pod = _find_worker_pod(kubectl, ns, selector, log)
        result["worker_pod"] = pod
        if not pod:
            log.error("no running worker pod matched %r in ns %r", selector, ns)
            result["error"] = "no worker pod found"
            rc = 8
        else:
            ok, out = _celery_call(kubectl, ns, pod, log)
            result["triggered"] = ok
            result["output"] = out[:1000]
            if not ok:
                rc = 9

    result["rc"] = rc
    atomic_write_json(rp.snapshot("trigger_prune"), result)
    log_event(log, logging.INFO, "trigger_prune_done", rc=rc, triggered=result.get("triggered"))
    # Always log the open_item so it's never lost even when the trigger succeeds.
    log.warning("open_item: %s", OPEN_ITEM)
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Manually kick the daily log-prune Celery task.")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--namespace",
                    default=os.environ.get("SPDDI_PERF_K8S_NAMESPACE", "spatiumddi"))
    ap.add_argument("--worker-selector", default="component=worker")
    ap.add_argument("--kubectl", default=os.environ.get("SPDDI_PERF_KUBECTL", "kubectl"))
    args = ap.parse_args(argv)

    rp = RunPaths.for_run(args.run_id, args.run_root)
    rp.ensure_dirs()
    log = get_logger("spddi_perf.seeder.prune", run_id=args.run_id,
                     logfile=rp.worker_log("trigger_prune"))
    m = manifest_mod.load(args.manifest)
    try:
        return run(rp, m, log, args.kubectl, args.namespace, args.worker_selector)
    except Exception as exc:  # noqa: BLE001
        log.exception("trigger_prune failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
