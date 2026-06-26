#!/usr/bin/env python3
"""flamethrower cache-buster / negative-path DNS sub-run (docs §4.3, §4.9).

Long-running worker, but BOUNDED — this is NOT the main plateau (§4.3: "Bounded, not
the main plateau"). It wraps DNS-OARC ``flamethrower`` to drive a UNIQUE-QNAME FLOOD
*UNDER A SEEDED ZONE* using flamethrower's built-in ``numbername`` / ``randomlabel``
query generators, which stress BIND's NXDOMAIN / negative path harder than the
Zipfian steady set (where most names are cache-warm).

§4.9 SAFETY: the generated unique labels are prepended to the **seeded base zone**
(``-g numbername`` against ``<random>.<base-zone>``), so every query is in-zone →
authoritative NXDOMAIN, NEVER an out-of-zone REFUSED/leak. The base zone is taken
from the seed-manifest (or the run manifest's first forward zone) and is the SAME
zone the §4.9 Layer-2 validator guards. We DO NOT pass apex/external bases.

The run is bounded by ``--runtime-s`` (and the kill-switch). It re-trues nothing to
the setpoint bus — it's a fixed-rate negative-path burst the controller launches and
tears down inside a sub-window. It still honors the kill-switch + SIGTERM (contract).

flamethrower flag mapping (DNS-OARC flamethrower):
    -g <generator>   query generator: 'numbername' (sequential) or 'randomlabel'
    -r <qtype>       record type (A for the NXDOMAIN flood)
    -q <count>       queries per concurrent sender before exit (0 = unlimited)
    -Q <qps>         target qps (rate limit) — bounds the flood
    -c <senders>     concurrent senders
    -l <ms>          runtime limit in milliseconds (the hard bound)
    -p <port>        server port
    -P udp|tcp       protocol
    <server> [base]  target IP + the base name the generator appends labels to

CLI (registry contract + extras):
    --run-id --run-root --manifest          (contract)
    --seed-manifest PATH    optional; defaults to rp.seed_manifest
    --base-zone NAME        override the seeded base zone the flood targets
    --generator {numbername,randomlabel}    default randomlabel (max cache-bust)
    --qps N                 rate cap (default: manifest dns_qps_sustained_peak)
    --senders N             concurrent senders (default 8)
    --runtime-s S           hard bound (default 120s)
    --tcp                   force TCP
    --flamethrower-bin      binary (default 'flamethrower')

Runs OFF-BOX where flamethrower is installed; missing binary → non-zero exit, no
fabricated data.

Grounding (real SpatiumDDI shapes — referenced, not called here):
  * bind9 on REAL udp/tcp :53 on node IP — docs §2.1
  * the seeded base zone is the same set the validator enforces — see
    gen_dns_queryset.SeedModel.in_zone (§4.9 Layer 2)
"""

from __future__ import annotations

import argparse
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import spddi_perf.manifest as manifest_mod
from spddi_perf.logging_util import append_ndjson, atomic_write_json, get_logger, log_event, read_json
from spddi_perf.runpaths import RunPaths

SERVICE = "spddi-perf-flamethrower"


class StopFlag:
    def __init__(self) -> None:
        self.stop = False

    def trip(self, *_a) -> None:
        self.stop = True


def _install_signal_handlers(flag: StopFlag) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, flag.trip)


def _which(binary: str) -> str | None:
    return shutil.which(binary) or (binary if Path(binary).exists() else None)


def resolve_base_zone(args, rp: RunPaths, m: manifest_mod.Manifest, log) -> str | None:
    """The seeded base zone unique labels are prepended to (§4.9 in-zone by construction)."""
    if args.base_zone:
        return args.base_zone.rstrip(".").lower()
    sm_path = Path(args.seed_manifest) if args.seed_manifest else rp.seed_manifest
    sm = read_json(sm_path) if sm_path.exists() else None
    if isinstance(sm, dict) and sm.get("dns", {}).get("forward_zones"):
        return str(sm["dns"]["forward_zones"][0]).rstrip(".").lower()
    if m.seed.dns.forward_zones:
        return m.seed.dns.forward_zones[0].rstrip(".").lower()
    log_event(log, 50, "no seeded forward zone — cannot build a safe flood base (§4.9)")
    return None


def build_flamethrower_argv(*, binary: str, node_ip: str, port: int, base_zone: str,
                            generator: str, qps: int, senders: int, runtime_s: float,
                            tcp: bool) -> list[str]:
    runtime_ms = int(max(1.0, runtime_s) * 1000)
    argv = [binary,
            "-g", generator,        # numbername | randomlabel (unique qnames)
            "-r", "A",              # A-record NXDOMAIN flood
            "-Q", str(int(max(1, qps))),
            "-c", str(max(1, senders)),
            "-l", str(runtime_ms),
            "-p", str(port),
            "-P", "tcp" if tcp else "udp",
            node_ip,
            base_zone]              # the generator appends random labels UNDER this
    return argv


# flamethrower prints a final summary; parse what we can (versions vary in format).
_TOTAL_RE = re.compile(r"total[^0-9]*([\d,]+)", re.IGNORECASE)
_NXDOMAIN_RE = re.compile(r"NXDOMAIN[^0-9]*([\d,]+)", re.IGNORECASE)
_REFUSED_RE = re.compile(r"REFUSED[^0-9]*([\d,]+)", re.IGNORECASE)
_TIMEOUT_RE = re.compile(r"timeout[s]?[^0-9]*([\d,]+)", re.IGNORECASE)
_QPS_RE = re.compile(r"([\d.]+)\s*(?:qps|queries/sec|r/s)", re.IGNORECASE)


def _num(rx, text):
    m = rx.search(text)
    return int(m.group(1).replace(",", "")) if m else None


def parse_flamethrower_output(text: str) -> dict:
    refused = _num(_REFUSED_RE, text)
    qps_m = _QPS_RE.search(text)
    return {
        "total": _num(_TOTAL_RE, text),
        "nxdomain": _num(_NXDOMAIN_RE, text),
        "refused": refused if refused is not None else 0,
        "timeouts": _num(_TIMEOUT_RE, text),
        "achieved_qps": float(qps_m.group(1)) if qps_m else None,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="flamethrower cache-buster / NXDOMAIN negative-path sub-run "
                    "(§4.3, bounded; in-zone by construction §4.9).")
    p.add_argument("--run-id", required=True)
    p.add_argument("--run-root", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--seed-manifest", default=None,
                   help="seeder seed-manifest.json (default: rp.seed_manifest)")
    p.add_argument("--base-zone", default=None,
                   help="seeded base zone to flood under (default: first forward zone)")
    p.add_argument("--generator", choices=("numbername", "randomlabel"),
                   default="randomlabel")
    p.add_argument("--qps", type=int, default=0,
                   help="rate cap (default: manifest dns_qps_sustained_peak)")
    p.add_argument("--senders", type=int, default=8)
    p.add_argument("--runtime-s", type=float, default=120.0,
                   help="hard bound on the flood (default 120)")
    p.add_argument("--tcp", action="store_true")
    p.add_argument("--flamethrower-bin", default="flamethrower")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rp = RunPaths.for_run(args.run_id, args.run_root)
    log = get_logger("flamethrower_runner", service=SERVICE, run_id=args.run_id,
                     logfile=rp.worker_log("flamethrower"))
    m = manifest_mod.load(args.manifest)

    flag = StopFlag()
    _install_signal_handlers(flag)

    base_zone = resolve_base_zone(args, rp, m, log)
    if not base_zone:
        return 2

    binary = _which(args.flamethrower_bin)
    if not binary:
        log_event(log, 50, "flamethrower not found on PATH (install on the load-gen box)",
                  binary=args.flamethrower_bin)
        return 4

    qps = args.qps or m.diurnal.dns_qps_sustained_peak
    qps = int(min(qps, m.guardrails.max_dns_qps))

    if flag.stop or rp.stop_file.exists():
        log_event(log, 20, "kill-switch set before start — exiting")
        return 0

    argv2 = build_flamethrower_argv(
        binary=binary, node_ip=m.target.node_ip, port=m.target.dns.port,
        base_zone=base_zone, generator=args.generator, qps=qps,
        senders=args.senders, runtime_s=args.runtime_s, tcp=args.tcp)
    log_event(log, 20, "flamethrower flood start", base_zone=base_zone,
              generator=args.generator, qps=qps, senders=args.senders,
              runtime_s=args.runtime_s, tcp=args.tcp, argv=" ".join(argv2))

    t0 = time.time()
    try:
        proc = subprocess.Popen(argv2, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
    except FileNotFoundError:
        log_event(log, 50, "flamethrower binary vanished", binary=binary)
        return 4

    # Poll for the kill-switch while the bounded flood runs; terminate early if set.
    out_chunks: list[str] = []
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        if flag.stop or rp.stop_file.exists():
            log_event(log, 30, "kill-switch tripped — terminating flamethrower")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
        # Hard safety bound in case -l is ignored by an old build.
        if time.time() - t0 > args.runtime_s + 30:
            log_event(log, 40, "flamethrower exceeded runtime bound — killing")
            proc.kill()
            break
        time.sleep(1.0)

    try:
        remaining = proc.communicate(timeout=10)[0]
        if remaining:
            out_chunks.append(remaining)
    except Exception:  # noqa: BLE001
        pass
    out = "".join(out_chunks)
    parsed = parse_flamethrower_output(out)

    refused = parsed.get("refused", 0) or 0
    refused_alert = refused > 0
    if refused_alert:
        # §4.9: even the cache-buster is in-zone by construction → REFUSED must be 0.
        log_event(log, 50, "REFUSED != 0 in cache-buster flood — OUT-OF-ZONE LEAK (§4.9)!",
                  refused=refused)

    result = {
        "kind": "flamethrower_flood",
        "base_zone": base_zone,
        "generator": args.generator,
        "tcp": args.tcp,
        "duration_s": round(time.time() - t0, 1),
        "offered_qps": qps,
        "senders": args.senders,
        **parsed,
        "refused": refused,
        "refused_alert": refused_alert,
    }
    atomic_write_json(rp.snapshot(f"flamethrower{'_tcp' if args.tcp else ''}"), result)
    append_ndjson(rp.generator("flamethrower.stat"), result)
    log_event(log, 20, "flamethrower flood complete", total=parsed.get("total"),
              nxdomain=parsed.get("nxdomain"), refused=refused,
              achieved_qps=parsed.get("achieved_qps"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
