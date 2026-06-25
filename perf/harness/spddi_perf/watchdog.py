"""Abort-if-unhealthy watchdog (docs/PERFORMANCE_TESTING.md §7.6.4).

Polls the war-room surfaces (the poller's NDJSON tails, plus an optional direct
health GET) every ``watchdog.poll_interval_s`` and evaluates the manifest's
``abort_on`` rules. On the FIRST breach it asks the controller to throttle (back off
one phase) so the box can recover (e.g. autovacuum catches up); only a SUSTAINED
breach escalates to abort (ramp-to-zero, final snapshot, stop) — so ceiling discovery
stays safe. An aborted run is still a result: the breach snapshot is preserved.

The watchdog reads the war-room poller's surfaces (it owns the endpoint knowledge);
if those files don't exist yet it degrades to whatever it can read and never
false-aborts on missing data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .logging_util import get_logger
from .manifest import Manifest
from .runpaths import RunPaths

OK = "ok"
THROTTLE = "throttle"
ABORT = "abort"


def _last_json_line(path: Path, max_tail: int = 16384) -> dict | None:
    """Return the last complete JSON object in an NDJSON file (cheap tail read)."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - max_tail))
            chunk = f.read().decode("utf-8", errors="ignore")
    except (FileNotFoundError, OSError):
        return None
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


@dataclass
class Verdict:
    level: str = OK
    reasons: list[str] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)


class Watchdog:
    def __init__(self, m: Manifest, rp: RunPaths, *, abort_after_polls: int = 6) -> None:
        self.m = m
        self.rp = rp
        self.abort_on = m.guardrails.watchdog.abort_on or {}
        self.throttle_before_abort = m.guardrails.watchdog.throttle_before_abort
        self.abort_after_polls = abort_after_polls
        self._breach_streak = 0
        self._throttled = False
        self.log = get_logger("spddi_perf.watchdog", run_id=rp.run_id, logfile=rp.log("watchdog"))

    def _read_surfaces(self) -> dict:
        """Best-effort current view from the poller's NDJSON tails."""
        w = self.rp.warroom
        return {
            "health": _last_json_line(w("health_platform")),
            "pg_overview": _last_json_line(w("pg_overview")),
            "redis_overview": _last_json_line(w("redis_overview")),
            "celery_queues": _last_json_line(w("celery_queues")),
        }

    def _breaches(self, s: dict) -> list[str]:
        out: list[str] = []
        ao = self.abort_on

        if ao.get("health_platform_component_down"):
            h = s.get("health") or {}
            comps = h.get("components") or h.get("component_up") or {}
            down = [k for k, v in comps.items() if v in (False, "down", "red", 0)]
            if down:
                out.append(f"health_platform components down: {','.join(sorted(down))}")

        pg = s.get("pg_overview") or {}
        pct = ao.get("pg_connections_pct_of_max")
        if pct and pg.get("active_connections") is not None and pg.get("max_connections"):
            ratio = pg["active_connections"] / max(1, pg["max_connections"])
            if ratio >= pct:
                out.append(f"pg connections {ratio:.0%} ≥ {pct:.0%}")
        longest = ao.get("pg_longest_txn_s")
        if longest and pg.get("longest_txn_age_s") is not None and pg["longest_txn_age_s"] >= longest:
            out.append(f"pg longest txn {pg['longest_txn_age_s']:.0f}s ≥ {longest}s")

        r = s.get("redis_overview") or {}
        rpct = ao.get("redis_used_memory_pct")
        if rpct and r.get("used_memory") is not None and r.get("maxmemory"):
            ratio = r["used_memory"] / max(1, r["maxmemory"])
            if ratio >= rpct:
                out.append(f"redis used {ratio:.0%} ≥ {rpct:.0%}")

        q = s.get("celery_queues") or {}
        qmax = ao.get("celery_queue_depth")
        if qmax and isinstance(q.get("queues"), dict):
            total = sum(int(v) for v in q["queues"].values())
            if total >= qmax:
                out.append(f"celery queue depth {total} ≥ {qmax}")
        return out

    def evaluate(self) -> Verdict:
        s = self._read_surfaces()
        reasons = self._breaches(s)
        if not reasons:
            self._breach_streak = 0
            self._throttled = False
            return Verdict(OK, [], s)

        self._breach_streak += 1
        # First breach with throttle policy → throttle once; sustained breach → abort.
        if self.throttle_before_abort and not self._throttled and self._breach_streak < self.abort_after_polls:
            self._throttled = True
            self.log.warning("watchdog throttle: %s", "; ".join(reasons))
            return Verdict(THROTTLE, reasons, s)
        if self._breach_streak >= self.abort_after_polls or not self.throttle_before_abort:
            self.log.error("watchdog ABORT (streak=%d): %s", self._breach_streak, "; ".join(reasons))
            return Verdict(ABORT, reasons, s)
        return Verdict(THROTTLE, reasons, s)
