"""``spddi-perf`` CLI — the operator entrypoint.

    python3 -m spddi_perf.cli <command> [options]

Commands:
  validate  --manifest M                 load + validate a manifest; print the resolved plan
  run       --manifest M [opts]          full run (provision → seed → phases → drain → report)
  smoke     [opts]                       run perf/manifests/smoke.yaml (the gated predecessor)
  resume    --run-id ID [opts]           continue a crashed/paused run from its last tick
  status    [--run-id ID]                print state.json (latest run if --run-id omitted)
  stop      [--run-id ID]                trip the kill-switch (graceful ramp-to-zero)
  seed      --manifest M                 run only the seeder oneshots (provision + seed)
  report    --run-id ID [--baseline ID]  (re)generate the run report off-box
  tui       [--manifest M] [--run-id ID]  interactive console (§9.3) — drives a run live
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import checkpoint as ckpt
from . import workers
from .controller import Controller, ControllerOptions
from .manifest import load as load_manifest
from .phases import PhaseEngine
from .runpaths import DEFAULT_RUN_ROOT, RunPaths

SMOKE_MANIFEST = str(Path(workers.PERF_DIR) / "manifests" / "smoke.yaml")


def _add_run_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    p.add_argument("--tick-seconds", type=float, default=60.0)
    p.add_argument("--time-scale", type=float, default=1.0,
                   help="wall-clock compression; >1 runs faster than real time")
    p.add_argument("--no-workers", action="store_true",
                   help="dry engine validation: publish setpoints + checkpoint, launch nothing")
    p.add_argument("--max-ticks", type=int, default=None)
    p.add_argument("--orchestrator-shards", type=int, default=1)
    p.add_argument("--perfdhcp-shards", type=int, default=0)
    p.add_argument("--skip-seed", action="store_true")
    p.add_argument("--skip-provision", action="store_true")


def _opts_from_args(manifest_path: str, a: argparse.Namespace, resume_run_id: str | None = None) -> ControllerOptions:
    return ControllerOptions(
        manifest_path=manifest_path, resume_run_id=resume_run_id, run_root=a.run_root,
        tick_seconds=a.tick_seconds, time_scale=a.time_scale, no_workers=a.no_workers,
        max_ticks=a.max_ticks, orchestrator_shards=a.orchestrator_shards,
        perfdhcp_shards=a.perfdhcp_shards, skip_seed=a.skip_seed, skip_provision=a.skip_provision)


def _latest_run_id(run_root: str) -> str | None:
    root = Path(run_root)
    if not root.exists():
        return None
    runs = [d for d in root.iterdir() if d.is_dir() and (d / "state.json").exists()]
    if not runs:
        return None
    return sorted(runs, key=lambda d: d.name)[-1].name


def cmd_validate(a: argparse.Namespace) -> int:
    m = load_manifest(a.manifest)
    eng = PhaseEngine(m)
    print(f"manifest: {a.manifest}")
    print(f"  name={m.name} profile={m.profile_slug} schema_v={m.schema_version}")
    print(f"  scale: {m.scale.unique_devices} devices, peak {m.scale.peak_active_devices} online, "
          f"lease {m.scale.lease_time_s}s (T1=900s), ddns={m.scale.ddns_enabled}, qlog={m.scale.query_log_enabled}")
    print(f"  topology={m.target.dhcp.topology} reverse_zones={m.seed.dns.reverse_zone_shape} "
          f"recursion={m.target.dns.recursion}")
    print(f"  total={m.total_minutes():.0f} min across {len(m.phases)} phases:")
    for w in eng.windows:
        print(f"    {w.phase.name:14s} {w.phase.minutes:6.0f}min  load={w.phase.load:8s} "
              f"[{w.start_s/3600:5.2f}h → {w.end_s/3600:5.2f}h]  {w.phase.extra or ''}")
    print("VALID")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    return Controller(_opts_from_args(a.manifest, a)).run()


def cmd_smoke(a: argparse.Namespace) -> int:
    return Controller(_opts_from_args(SMOKE_MANIFEST, a)).run()


def cmd_resume(a: argparse.Namespace) -> int:
    rp = RunPaths.for_run(a.run_id, a.run_root)
    state = ckpt.read_state(rp)
    if not state:
        print(f"no state.json for run {a.run_id}", file=sys.stderr)
        return 2
    return Controller(_opts_from_args(state.manifest_path, a, resume_run_id=a.run_id)).run()


def cmd_status(a: argparse.Namespace) -> int:
    run_id = a.run_id or _latest_run_id(a.run_root)
    if not run_id:
        print("no runs found", file=sys.stderr)
        return 2
    state = ckpt.read_state(RunPaths.for_run(run_id, a.run_root))
    if not state:
        print(f"no state.json for run {run_id}", file=sys.stderr)
        return 2
    d = state.to_dict()
    print(f"run {d['run_id']}")
    for k in ("status", "phase", "phase_index", "tick", "elapsed_test_s", "profile", "updated_at"):
        print(f"  {k:14s} {d.get(k)}")
    return 0


def cmd_stop(a: argparse.Namespace) -> int:
    run_id = a.run_id or _latest_run_id(a.run_root)
    if not run_id:
        print("no runs found", file=sys.stderr)
        return 2
    rp = RunPaths.for_run(run_id, a.run_root)
    rp.stop_file.touch()
    print(f"kill-switch tripped for {run_id} ({rp.stop_file})")
    return 0


def cmd_seed(a: argparse.Namespace) -> int:
    o = _opts_from_args(a.manifest, a)
    o.max_ticks = 0  # provision + seed only, no phase loop
    return Controller(o).run()


def cmd_report(a: argparse.Namespace) -> int:
    import subprocess
    argv = [sys.executable, "-m", "spddi_perf.collect", "--run-id", a.run_id, "--run-root", a.run_root]
    if a.baseline:
        argv += ["--baseline", a.baseline]
    return subprocess.run(argv, env=workers.child_env()).returncode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spddi-perf", description="SpatiumDDI performance test harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate")
    v.add_argument("--manifest", required=True)
    v.set_defaults(fn=cmd_validate)

    r = sub.add_parser("run")
    r.add_argument("--manifest", required=True)
    _add_run_opts(r)
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("smoke")
    _add_run_opts(s)
    s.set_defaults(fn=cmd_smoke)

    rs = sub.add_parser("resume")
    rs.add_argument("--run-id", required=True)
    _add_run_opts(rs)
    rs.set_defaults(fn=cmd_resume)

    st = sub.add_parser("status")
    st.add_argument("--run-id")
    st.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    st.set_defaults(fn=cmd_status)

    sp = sub.add_parser("stop")
    sp.add_argument("--run-id")
    sp.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    sp.set_defaults(fn=cmd_stop)

    sd = sub.add_parser("seed")
    sd.add_argument("--manifest", required=True)
    _add_run_opts(sd)
    sd.set_defaults(fn=cmd_seed)

    rp = sub.add_parser("report")
    rp.add_argument("--run-id", required=True)
    rp.add_argument("--baseline")
    rp.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    rp.set_defaults(fn=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # `tui` is intercepted before argparse so the interactive console owns its own
    # args and `rich` stays an optional dependency (only this subcommand needs it).
    if raw and raw[0] == "tui":
        try:
            from . import tui
        except ImportError as e:
            print(f"the TUI needs 'rich' — pip install rich  ({e})", file=sys.stderr)
            return 2
        return tui.main(raw[1:])
    args = build_parser().parse_args(raw)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
