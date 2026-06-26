#!/usr/bin/env python3
"""dnsperf / resperf raw-QPS DNS load worker (docs/PERFORMANCE_TESTING.md §4.3, §4.8).

Long-running worker. Two modes:

  --mode dnsperf  (default)
      Wraps ``dnsperf -Q`` for a SUSTAINED qps plateau driven by the setpoint bus
      (``setpoint.dns_qps``). Re-trues the offered rate to the current setpoint each
      window, parses the RCODE breakdown + latency percentiles, and writes one NDJSON
      record per window to ``rp.generator('dnsperf.stat')``.

  --mode resperf
      Wraps ``resperf`` for the RAMP-TO-FAILURE ceiling probe (§4.3 preferred ceiling
      tool). Ramps the send rate linearly to ``-m max`` and reports the QPS at which
      ``answered < sent`` (the protocol ceiling) from the resperf CSV. One-shot-ish:
      runs the ramp once, records the ceiling, then idles honoring the kill-switch.

Both target ``m.target.node_ip:53`` (bind9 on REAL :53, §2.1) and replay the in-zone
query file produced by ``gen_dns_queryset.py`` (default: the steady file under the
run's ``generators/dns/``). A ``--cold`` flag swaps to the NXDOMAIN-heavy file; a
``--tcp`` flag forces the TCP sub-run (BIND's TCP path is a separate, lower ceiling,
§4.3).

§4.8 metric contract — RCODE breakdown is NOERROR / NXDOMAIN / SERVFAIL / **REFUSED**.
**REFUSED is a DISTINCT, should-be-ZERO counter**: a non-zero value means an
out-of-zone query escaped the §4.9 Layer-2 validator (a leak/bug) — we log it at
CRITICAL and stamp ``refused_alert: true`` on the record.

CLI (registry contract + extras):
    --run-id --run-root --manifest          (contract)
    --queries PATH         query file to replay (default: <run>/generators/dns/queries.steady.txt)
    --cold                 use the .cold.txt (NXDOMAIN-heavy) file instead
    --mode {dnsperf,resperf}    default dnsperf
    --tcp                  force TCP (BIND TCP-path ceiling sub-run)
    --clients N            concurrent client threads (dnsperf -c / resperf -c)
    --window-s S           per-window run length for the dnsperf plateau (default 30)
    --resperf-max-qps N    resperf ramp ceiling (-m); default from manifest burst ceiling
    --resperf-ramp-s S     resperf ramp duration (-r); default 180
    --dnsperf-bin / --resperf-bin   binary names/paths (default 'dnsperf' / 'resperf')

This runs OFF-BOX on the load-gen VM where dnsperf/resperf are installed; if the
binary is missing it logs an error and exits non-zero (it does not fabricate data).

Grounding (real SpatiumDDI shapes — referenced for cross-check, not called here):
  * bind9 listens on REAL udp/tcp :53 on the node IP — docs §2.1 (dns-bind9.yaml:50-57)
  * server-side cross-check feed (war-room owns the poll): GET /metrics/dns/timeseries
    — backend/app/api/v1/metrics/router.py:92 (DNSTimeseries: queries/noerror/
    nxdomain/servfail/rate_dropped) — a client-vs-server gap = pre-BIND NIC/kernel drop (§4.8)
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import spddi_perf.manifest as manifest_mod
import spddi_perf.setpoints as setpoints
from spddi_perf.logging_util import append_ndjson, atomic_write_json, get_logger, log_event
from spddi_perf.runpaths import RunPaths

SERVICE = "spddi-perf-dnsperf"

# A setpoint whose tick hasn't advanced for this many polls → controller gone →
# fail safe to OFF (§ contract item 4). 3 ticks ≈ 3 windows of staleness.
# Fail safe to OFF only when the tick hasn't advanced for this much WALL time —
# NOT after N polls (windows are shorter than the 60s tick, so same-tick reads are
# normal steady-state, not a dead controller).
STALE_TIMEOUT_S = 180.0

# dnsperf prints the RCODE breakdown inline on one "Response codes:" line:
#   "Response codes: NOERROR 150000 (83.34%), NXDOMAIN 29978 (16.66%), REFUSED 1 (0.00%)"
# Match each "<RCODE-NAME> <count> (" anywhere — but restrict to the known DNS RCODE
# token set so we don't false-match "request 41" / "response 96" in the packet-size
# line. (Covers the standard rcodes a recursion-off authoritative BIND can emit.)
_RCODE_NAMES = ("NOERROR", "FORMERR", "SERVFAIL", "NXDOMAIN", "NOTIMP", "REFUSED",
                "YXDOMAIN", "YXRRSET", "NXRRSET", "NOTAUTH", "NOTZONE", "BADVERS")
_RCODE_RE = re.compile(r"\b(" + "|".join(_RCODE_NAMES) + r")\s+(\d+)\s*\(")
# Latency summary lines from dnsperf (-S off; we parse the end block):
_QPS_RE = re.compile(r"Queries per second:\s+([\d.]+)")
_SENT_RE = re.compile(r"Queries sent:\s+(\d+)")
_COMPLETED_RE = re.compile(r"Queries completed:\s+(\d+)\s*\(([\d.]+)%\)")
_LOST_RE = re.compile(r"Queries lost:\s+(\d+)")
_LAT_AVG_RE = re.compile(r"Average Latency.*?:\s+([\d.]+)")
_LAT_MAX_RE = re.compile(r"Latency.*?max\s+([\d.]+)")
# dnsperf with --latency-histogram or -v prints percentiles when given -S; we instead
# rely on the latency stats block. (resperf CSV carries its own latency column.)


# ================================================================================
# Stop conditions
# ================================================================================
class StopFlag:
    def __init__(self) -> None:
        self.stop = False

    def trip(self, *_a) -> None:
        self.stop = True


def _install_signal_handlers(flag: StopFlag) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, flag.trip)


def _should_stop(flag: StopFlag, rp: RunPaths) -> bool:
    return flag.stop or rp.stop_file.exists()


# ================================================================================
# dnsperf invocation + parsing
# ================================================================================
def _which(binary: str) -> str | None:
    return shutil.which(binary) or (binary if Path(binary).exists() else None)


def build_dnsperf_argv(*, binary: str, node_ip: str, port: int, queries: Path,
                       qps: float, clients: int, window_s: float, tcp: bool) -> list[str]:
    """``dnsperf -Q`` for a sustained-qps plateau window (§4.3).

    -s server  -p port  -d datafile  -Q max-qps  -l limit-seconds  -c clients
    -m tcp (force TCP).  -S 1 prints a periodic + final stats block.
    """
    argv = [binary, "-s", node_ip, "-p", str(port), "-d", str(queries),
            "-Q", str(int(max(1, qps))), "-l", str(int(max(1, window_s))),
            "-c", str(max(1, clients)), "-S", "1"]
    if tcp:
        argv += ["-m", "tcp"]
    return argv


def build_resperf_argv(*, binary: str, node_ip: str, port: int, queries: Path,
                       max_qps: int, ramp_s: int, clients: int, csv_path: Path,
                       tcp: bool) -> list[str]:
    """``resperf`` ramp-to-failure (§4.3): -m max-qps -r ramp-seconds -P csv-out."""
    argv = [binary, "-s", node_ip, "-p", str(port), "-d", str(queries),
            "-m", str(max_qps), "-r", str(ramp_s), "-c", str(max(1, clients)),
            "-P", str(csv_path)]
    # NOTE: resperf's -m is the max-qps ceiling (set above), NOT a transport flag —
    # appending "-m tcp" would clobber the numeric ceiling with a string. resperf has
    # no TCP mode; the TCP-path ceiling is measured with dnsperf -m tcp (§4.3), so the
    # `tcp` flag is intentionally ignored here.
    return argv


def parse_dnsperf_output(text: str) -> dict:
    """Parse dnsperf's final stats block into the §4.8 metric shape."""
    rcodes = {m.group(1): int(m.group(2)) for m in _RCODE_RE.finditer(text)}

    def _f(rx, default=None):
        m = rx.search(text)
        return float(m.group(1)) if m else default

    def _i(rx, default=0):
        m = rx.search(text)
        return int(m.group(1)) if m else default

    sent = _i(_SENT_RE)
    completed_m = _COMPLETED_RE.search(text)
    completed = int(completed_m.group(1)) if completed_m else 0
    lost = _i(_LOST_RE)
    achieved = _f(_QPS_RE, 0.0) or 0.0

    # Normalize the RCODE buckets §4.8 cares about; REFUSED is its OWN counter.
    rc = {
        "NOERROR": rcodes.get("NOERROR", 0),
        "NXDOMAIN": rcodes.get("NXDOMAIN", 0),
        "SERVFAIL": rcodes.get("SERVFAIL", 0),
        "REFUSED": rcodes.get("REFUSED", 0),
        "OTHER": sum(v for k, v in rcodes.items()
                     if k not in ("NOERROR", "NXDOMAIN", "SERVFAIL", "REFUSED")),
    }
    return {
        "achieved_qps": round(achieved, 1),
        "sent": sent,
        "completed": completed,
        "lost": lost,
        "timeouts": lost,   # dnsperf "lost" == timed-out/dropped
        "latency_avg_ms": round((_f(_LAT_AVG_RE, 0.0) or 0.0) * 1000.0, 3),
        "latency_max_ms": round((_f(_LAT_MAX_RE, 0.0) or 0.0) * 1000.0, 3),
        "rcode": rc,
    }


def parse_resperf_csv(csv_path: Path) -> dict:
    """Parse the resperf CSV → ceiling (first row where answered < sent).

    resperf CSV columns: time, target_qps, actual_qps, responses_per_sec,
    failures_per_sec, avg_latency (versions vary; we read by header when present,
    else by position).
    """
    if not csv_path.exists():
        return {"ceiling_qps": None, "rows": 0, "note": "resperf csv not produced"}
    rows: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        sample = f.read(4096)
        f.seek(0)
        has_header = any(c.isalpha() for c in sample.split("\n", 1)[0])
        if has_header:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append({k.strip().lower(): v for k, v in r.items()})
        else:
            for r in csv.reader(f):
                if len(r) >= 4:
                    rows.append({"time": r[0], "target_qps": r[1],
                                 "actual_qps": r[2], "responses_per_sec": r[3]})

    def _num(d, *keys):
        for k in keys:
            if k in d and d[k] not in (None, ""):
                try:
                    return float(d[k])
                except ValueError:
                    # Non-numeric cell (e.g. a header re-emitted mid-CSV); skip
                    # it and try the next candidate key.
                    pass
        return None

    ceiling = None
    max_answered = 0.0
    for r in rows:
        sent = _num(r, "actual_qps", "target_qps", "sent_per_sec")
        answered = _num(r, "responses_per_sec", "answered_per_sec", "completed_per_sec")
        if sent is None or answered is None:
            continue
        max_answered = max(max_answered, answered)
        # First sustained shortfall (answered < 98% sent) marks the cliff.
        if ceiling is None and sent > 0 and answered < 0.98 * sent and sent > 1000:
            ceiling = answered
    return {
        "ceiling_qps": round(ceiling, 1) if ceiling is not None else round(max_answered, 1),
        "max_answered_qps": round(max_answered, 1),
        "rows": len(rows),
        "csv": str(csv_path),
    }


# ================================================================================
# Run loops
# ================================================================================
def run_dnsperf_loop(args, rp: RunPaths, m: manifest_mod.Manifest, log,
                     flag: StopFlag, queries: Path, binary: str) -> int:
    """Sustained-qps plateau loop driven by the setpoint bus."""
    stat_path = rp.generator("dnsperf.stat")
    last_tick = -1
    last_tick_change = time.monotonic()
    window_idx = 0

    while not _should_stop(flag, rp):
        sp = setpoints.read_current(rp)
        if sp is None:
            # Controller hasn't published yet — wait, do NOT blast (contract item 4).
            log_event(log, 20, "no setpoint yet — idling", window=window_idx)
            time.sleep(min(5.0, args.window_s))
            continue

        # Stale-setpoint fail-safe: the tick advances every 60s but our window is
        # shorter, so same-tick reads are NORMAL. Only fail safe to OFF when the tick
        # hasn't advanced for STALE_TIMEOUT_S of WALL time (controller truly gone).
        now = time.monotonic()
        if sp.tick != last_tick:
            last_tick = sp.tick
            last_tick_change = now
        elif now - last_tick_change > STALE_TIMEOUT_S:
            log_event(log, 40, "setpoint stale — failing safe to OFF (controller gone?)",
                      tick=sp.tick, stale_for_s=round(now - last_tick_change, 1))
            time.sleep(min(5.0, args.window_s))
            continue

        target_qps = max(1.0, sp.dns_qps)
        # Clamp to the guardrail (defense in depth; setpoint is already clamped).
        target_qps = min(target_qps, m.guardrails.max_dns_qps)

        argv = build_dnsperf_argv(
            binary=binary, node_ip=m.target.node_ip, port=m.target.dns.port,
            queries=queries, qps=target_qps, clients=args.clients,
            window_s=args.window_s, tcp=args.tcp)
        log_event(log, 20, "dnsperf window start", window=window_idx, tick=sp.tick,
                  phase=sp.phase, target_qps=round(target_qps, 1), tcp=args.tcp,
                  argv=" ".join(argv))
        t0 = time.time()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=args.window_s + 30)
        except subprocess.TimeoutExpired:
            log_event(log, 40, "dnsperf window timed out", window=window_idx)
            window_idx += 1
            continue
        except FileNotFoundError:
            log_event(log, 50, "dnsperf binary not found — cannot generate load",
                      binary=binary)
            return 4

        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        parsed = parse_dnsperf_output(out)
        dur = time.time() - t0

        rec = _build_record(sp, target_qps, parsed, dur, tcp=args.tcp,
                            mode="dnsperf", window=window_idx, log=log)
        append_ndjson(stat_path, rec)
        window_idx += 1

    log_event(log, 20, "dnsperf loop stopped", windows=window_idx,
              reason="stop_file" if rp.stop_file.exists() else "signal")
    return 0


def run_resperf_once(args, rp: RunPaths, m: manifest_mod.Manifest, log,
                     flag: StopFlag, queries: Path, binary: str) -> int:
    """Ramp-to-failure ceiling probe (§4.3). Runs the ramp once, records the cliff,
    then idles honoring the kill-switch (so the controller's peak phase owns its
    lifetime like the dnsperf worker)."""
    max_qps = args.resperf_max_qps or m.diurnal.dns_qps_burst_ceiling
    max_qps = int(min(max_qps, m.guardrails.max_dns_qps))
    csv_path = rp.generator(f"resperf{'.tcp' if args.tcp else ''}.csv")
    argv = build_resperf_argv(
        binary=binary, node_ip=m.target.node_ip, port=m.target.dns.port,
        queries=queries, max_qps=max_qps, ramp_s=args.resperf_ramp_s,
        clients=args.clients, csv_path=csv_path, tcp=args.tcp)
    log_event(log, 20, "resperf ramp start", max_qps=max_qps, ramp_s=args.resperf_ramp_s,
              tcp=args.tcp, argv=" ".join(argv))

    if _should_stop(flag, rp):
        return 0
    t0 = time.time()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=args.resperf_ramp_s + 120)
    except subprocess.TimeoutExpired:
        log_event(log, 40, "resperf ramp timed out", max_qps=max_qps)
        return 0
    except FileNotFoundError:
        log_event(log, 50, "resperf binary not found — cannot probe ceiling", binary=binary)
        return 4

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    ceiling = parse_resperf_csv(csv_path)
    # resperf prints a "Maximum throughput" summary line too — capture it raw.
    summary_m = re.search(r"Maximum throughput:\s+([\d.]+)", out)
    if summary_m:
        ceiling["resperf_reported_max_qps"] = round(float(summary_m.group(1)), 1)

    result = {
        "mode": "resperf",
        "tcp": args.tcp,
        "duration_s": round(time.time() - t0, 1),
        "max_qps_target": max_qps,
        "ramp_s": args.resperf_ramp_s,
        **ceiling,
    }
    atomic_write_json(rp.snapshot(f"resperf_ceiling{'_tcp' if args.tcp else ''}"), result)
    append_ndjson(rp.generator("dnsperf.stat"), {**result, "kind": "resperf_ceiling"})
    log_event(log, 20, "resperf ceiling recorded", **{k: result[k] for k in
              ("ceiling_qps", "max_answered_qps", "rows") if k in result})

    # Idle until kill-switch / signal (the controller tears us down at phase exit).
    while not _should_stop(flag, rp):
        time.sleep(2.0)
    return 0


def _build_record(sp, target_qps, parsed, dur, *, tcp, mode, window, log) -> dict:
    """Assemble the §4.8 NDJSON record + the REFUSED zero-counter alert."""
    rc = parsed["rcode"]
    refused = rc.get("REFUSED", 0)
    refused_alert = refused > 0
    if refused_alert:
        # §4.8 / §4.9: REFUSED must be ZERO. Non-zero = out-of-zone leak/bug.
        log_event(log, 50, "REFUSED != 0 — OUT-OF-ZONE QUERY DETECTED (§4.9 leak/bug!)",
                  refused=refused, window=window, mode=mode)
    achieved = parsed["achieved_qps"]
    return {
        "kind": "dnsperf_window",
        "mode": mode,
        "window": window,
        "tick": sp.tick,
        "phase": sp.phase,
        "tcp": tcp,
        "duration_s": round(dur, 1),
        "offered_qps": round(target_qps, 1),
        "achieved_qps": achieved,
        "achieved_frac": round(achieved / target_qps, 4) if target_qps else 0.0,
        "sent": parsed["sent"],
        "completed": parsed["completed"],
        "timeouts": parsed["timeouts"],
        "latency_avg_ms": parsed["latency_avg_ms"],
        "latency_max_ms": parsed["latency_max_ms"],
        # §4.8 percentile fields — dnsperf's plain stats block reports avg/max only;
        # full p50/p95/p99/p999 require -S with histogram parsing which varies by
        # build. We surface what dnsperf gives and leave the percentile keys present
        # (None) so downstream report code has a stable schema.
        "p50_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "p999_ms": None,
        "max_ms": parsed["latency_max_ms"],
        "rcode": rc,
        "refused": refused,
        "refused_alert": refused_alert,   # §8.3 b6a — should-be-zero gate
    }


# ================================================================================
# CLI
# ================================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="dnsperf/resperf raw-QPS DNS load worker (§4.3 / §4.8).")
    p.add_argument("--run-id", required=True)
    p.add_argument("--run-root", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--queries", default=None,
                   help="query file (default: <run>/generators/dns/queries.steady.txt)")
    p.add_argument("--cold", action="store_true",
                   help="replay the NXDOMAIN-heavy .cold.txt file instead of steady")
    p.add_argument("--mode", choices=("dnsperf", "resperf"), default="dnsperf")
    p.add_argument("--tcp", action="store_true", help="force TCP (BIND TCP-path ceiling)")
    p.add_argument("--clients", type=int, default=16)
    p.add_argument("--window-s", type=float, default=30.0,
                   help="dnsperf per-window run length (default 30)")
    p.add_argument("--resperf-max-qps", type=int, default=0,
                   help="resperf ramp ceiling -m (default: manifest dns_qps_burst_ceiling)")
    p.add_argument("--resperf-ramp-s", type=int, default=180,
                   help="resperf ramp duration -r (default 180)")
    p.add_argument("--dnsperf-bin", default="dnsperf")
    p.add_argument("--resperf-bin", default="resperf")
    return p.parse_args(argv)


def _resolve_queries(args, rp: RunPaths) -> Path:
    if args.queries:
        return Path(args.queries)
    base = rp.generators_dir / "dns" / "queries"
    return Path(str(base) + (".cold.txt" if args.cold else ".steady.txt"))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rp = RunPaths.for_run(args.run_id, args.run_root)
    log = get_logger("dnsperf_runner", service=SERVICE, run_id=args.run_id,
                     logfile=rp.worker_log("dnsperf"))
    m = manifest_mod.load(args.manifest)

    flag = StopFlag()
    _install_signal_handlers(flag)

    queries = _resolve_queries(args, rp)
    if not queries.exists():
        log_event(log, 50, "query file missing — run gen_dns_queryset.py first",
                  queries=str(queries))
        return 2

    binary_name = args.resperf_bin if args.mode == "resperf" else args.dnsperf_bin
    binary = _which(binary_name)
    if not binary:
        log_event(log, 50, "load binary not found on PATH (install dnsperf/resperf on "
                  "this load-gen box)", binary=binary_name, mode=args.mode)
        return 4

    log_event(log, 20, "dnsperf_runner start", mode=args.mode, queries=str(queries),
              node_ip=m.target.node_ip, port=m.target.dns.port, tcp=args.tcp,
              cold=args.cold, binary=binary)

    if args.mode == "resperf":
        return run_resperf_once(args, rp, m, log, flag, queries, binary)
    return run_dnsperf_loop(args, rp, m, log, flag, queries, binary)


if __name__ == "__main__":
    sys.exit(main())
