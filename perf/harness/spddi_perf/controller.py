"""The run controller — conductor + watchdog + checkpointer (docs §7.0/§7.1/§7.5).

A thin, crash-tolerant process (runs on lg-0, NEVER on the appliance). It owns the
lifecycle: provision → seed → t0 baseline → ramp → steady → peak → soak → drain →
collect → teardown. It drives off-box workers via the setpoint bus and supervises
them; it does no protocol I/O itself.

Degrades gracefully: if a leaf component script isn't present yet (still being built),
that worker is logged-and-skipped and the phase engine still runs + publishes
setpoints + checkpoints — so the spine is exercisable before the leaves land.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from dataclasses import dataclass

from . import checkpoint as ckpt
from . import setpoints, workers
from .logging_util import get_logger, atomic_write_json
from .manifest import Manifest, load as load_manifest, dump_resolved
from .phases import PhaseEngine
from .runpaths import RunPaths
from .setpoints import Setpoint
from .watchdog import ABORT, THROTTLE, Watchdog


@dataclass
class ControllerOptions:
    manifest_path: str
    resume_run_id: str | None = None
    run_root: str | None = None          # defaults to perf/run
    tick_seconds: float = 60.0           # test-clock seconds per tick (matches native 60s buckets)
    time_scale: float = 1.0              # wall-clock compression (>1 = faster than real time)
    no_workers: bool = False             # dry engine validation (no subprocess launches)
    max_ticks: int | None = None         # cap for testing
    orchestrator_shards: int = 1
    perfdhcp_shards: int = 0             # ceiling tool; 0 = don't launch
    skip_seed: bool = False
    skip_provision: bool = False


class Controller:
    def __init__(self, opts: ControllerOptions) -> None:
        self.opts = opts
        self.m: Manifest = load_manifest(opts.manifest_path)
        from .runpaths import DEFAULT_RUN_ROOT
        run_root = opts.run_root or DEFAULT_RUN_ROOT
        if opts.resume_run_id:
            self.rp = RunPaths.for_run(opts.resume_run_id, run_root)
        else:
            self.rp = RunPaths.create(self.m.name, run_root)
        self.rp.ensure_dirs()
        self.log = get_logger("spddi_perf.controller", run_id=self.rp.run_id,
                              logfile=self.rp.controller_log)
        self.engine = PhaseEngine(self.m)
        self.watchdog = Watchdog(self.m, self.rp)
        self.procs: dict[str, subprocess.Popen] = {}
        self._stop = False
        self._aborting = False
        self._last_phase_index = -2
        self._pruned_phases: set[str] = set()

    # ---- signals + kill-switch ----
    def _install_signals(self) -> None:
        def handler(signum, _frame):
            self.log.warning("signal %s received → graceful stop", signum)
            self._stop = True
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def _kill_switch_tripped(self) -> bool:
        return self.rp.stop_file.exists()

    # ---- worker helpers ----
    def _oneshot(self, name: str, *, timeout: float | None = None, **extra: object) -> int:
        if self.opts.no_workers:
            self.log.info("no_workers: would run oneshot %s %s", name, extra)
            return -1  # treated as "skipped" by callers (rc in (0, -1) is non-fatal)
        return workers.run_oneshot(name, run_id=self.rp.run_id, run_root=str(self.rp.root.parent),
                                   manifest=self.opts.manifest_path,
                                   logfile=self.rp.worker_log(name), timeout=timeout, **extra)

    def _launch(self, name: str, **extra: object) -> None:
        if self.opts.no_workers:
            self.log.info("no_workers: would launch %s %s", name, extra)
            return
        proc = workers.launch(name, run_id=self.rp.run_id, run_root=str(self.rp.root.parent),
                              manifest=self.opts.manifest_path,
                              logfile=self.rp.worker_log(name), **extra)
        if proc:
            self.procs[name] = proc

    def _stop_workers(self) -> None:
        for name, proc in list(self.procs.items()):
            if proc.poll() is None:
                self.log.info("terminating worker %s pid=%s", name, proc.pid)
                proc.terminate()
        deadline = time.time() + 15
        for name, proc in list(self.procs.items()):
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                self.log.warning("killing unresponsive worker %s", name)
                proc.kill()
        self.procs.clear()

    def _check_worker_deaths(self) -> None:
        for name, proc in list(self.procs.items()):
            rc = proc.poll()
            if rc is not None:
                self.log.error("worker %s died rc=%s", name, rc)
                ckpt.append_event(self.rp, "worker_died", worker=name, rc=rc)
                self.procs.pop(name, None)

    # ---- lifecycle ----
    def _provision(self, state: ckpt.RunState) -> None:
        if self.opts.skip_provision:
            return
        state.status = ckpt.STATUS_PROVISION
        ckpt.write_state(self.rp, state)
        self.log.info("provision: phase0 verify + pg_stat_statements + fleet enable "
                      "(target=%s)", self.m.target.api_base)
        # Phase-0 verification is warn-only here (operator confirms; §9 Phase 0).
        # Time-boxed so an unreachable target.node_ip can't wedge provisioning forever.
        rc = self._oneshot("phase0_verify", timeout=120)
        if rc == 124:
            self.log.error("phase0_verify TIMED OUT — is target.node_ip (%s) reachable? "
                           "Set SPDDI_PERF_NODE_IP=<appliance-ip> if the manifest still has "
                           "the placeholder.", self.m.target.node_ip)
        if rc not in (0, -1):
            ckpt.append_event(self.rp, "phase0_verify_warning", rc=rc)
        if self.m.observability.enable_pg_stat_statements:
            self._oneshot("pgss_enable", timeout=120)
        # fleet_enable brings up the data plane (bind9/kea) — surface a non-clean rc
        # so a failed bring-up is visible rather than silently treated as healthy.
        frc = self._oneshot("fleet_enable", timeout=300)
        if frc not in (0, -1):
            ckpt.append_event(self.rp, "fleet_enable_warning", rc=frc)
            self.log.warning("fleet_enable rc=%s — data plane may be incomplete "
                             "(target=%s)", frc, self.m.target.api_base)

    def _seed(self, state: ckpt.RunState) -> None:
        if self.opts.skip_seed:
            return
        state.status = ckpt.STATUS_SEED
        ckpt.write_state(self.rp, state)
        self.log.info("seed: scaffold subnets/pools/zones + bulk authoritative dataset")
        rc = self._oneshot("seed_scaffold")
        if rc not in (0, -1):
            ckpt.append_event(self.rp, "seed_failed", rc=rc)
            raise RuntimeError(f"seed_scaffold failed rc={rc}")
        # Build the in-zone-validated DNS query set the dnsperf ceiling workers read
        # (gen_dns_queryset is not a long-running worker; it runs once, post-seed).
        qrc = self._oneshot("gen_queryset")
        if qrc not in (0, -1):
            ckpt.append_event(self.rp, "gen_queryset_warning", rc=qrc)

    def _start_observers(self) -> None:
        self._launch("warroom_poller")
        self._launch("psql_probe")

    def _start_generators(self) -> None:
        for i in range(self.opts.orchestrator_shards):
            self._launch("orchestrator", shard=i, shards=self.opts.orchestrator_shards)
        if self.m.scale.operator_mutation_stream.enabled:
            self._launch("api_mutation")
        self._launch("ui_probe")

    def _on_phase_enter(self, phase_name: str, extra: dict) -> None:
        ckpt.append_event(self.rp, "phase_transition", phase=phase_name)
        self.log.info("→ phase %s", phase_name)
        if phase_name == "peak" and self.opts.perfdhcp_shards and not self.opts.no_workers:
            for i in range(self.opts.perfdhcp_shards):
                self._launch("perfdhcp", shard=i, shards=self.opts.perfdhcp_shards)
            self._launch("dnsperf")
        if extra.get("trigger_prune") and phase_name not in self._pruned_phases:
            self._pruned_phases.add(phase_name)
            self.log.info("triggering daily log-prune (manifest hook)")
            self._oneshot("trigger_prune")

    def _watch_and_sleep(self, current_sp: Setpoint, tick_wall_s: float) -> None:
        """Sleep one tick's wall time, polling the watchdog + kill-switch within it."""
        poll = max(0.5, self.m.guardrails.watchdog.poll_interval_s / max(1.0, self.opts.time_scale))
        deadline = time.time() + tick_wall_s
        throttle_factor = 1.0
        while time.time() < deadline:
            if self._stop or self._kill_switch_tripped():
                self._stop = True
                return
            self._check_worker_deaths()
            v = self.watchdog.evaluate()
            if v.level == ABORT:
                self._aborting = True
                atomic_write_json(self.rp.snapshot("ceiling"),
                                  {"reasons": v.reasons, "snapshot": v.snapshot,
                                   "setpoint": current_sp.to_dict()})
                ckpt.append_event(self.rp, "watchdog_abort", reasons=v.reasons)
                self._stop = True
                return
            if v.level == THROTTLE:
                throttle_factor = max(0.3, throttle_factor * 0.7)
                throttled = Setpoint.from_dict(current_sp.to_dict())
                throttled.new_dora_per_s *= throttle_factor
                throttled.dns_qps *= throttle_factor
                setpoints.publish(self.rp, throttled)
                ckpt.append_event(self.rp, "watchdog_throttle",
                                  factor=round(throttle_factor, 2), reasons=v.reasons)
            time.sleep(min(poll, max(0.0, deadline - time.time())))

    def _await_convergence(self, timeout_s: float = 600.0) -> None:
        """Drain: wait for dns_record_op pending → 0 (best-effort via warroom tail)."""
        from .watchdog import _last_json_line
        deadline = time.time() + timeout_s / max(1.0, self.opts.time_scale)
        while time.time() < deadline and not self._stop:
            counts = _last_json_line(self.rp.warroom("domain_counts"))
            pending = (counts or {}).get("dns_record_op_pending")
            if pending is None:
                self.log.info("drain: no domain_counts surface — skipping convergence wait")
                return
            if pending <= 0:
                self.log.info("drain: dns_record_op pending converged to 0")
                return
            self.log.info("drain: waiting for convergence, pending=%s", pending)
            time.sleep(max(1.0, 10.0 / max(1.0, self.opts.time_scale)))

    def _run_report(self) -> None:
        argv = [sys.executable, "-m", "spddi_perf.collect",
                "--run-id", self.rp.run_id, "--run-root", str(self.rp.root.parent)]
        try:
            rc = subprocess.run(argv, env=workers.child_env()).returncode
            self.log.info("report generation rc=%s", rc)
        except Exception as e:  # collect.py not built yet → graceful
            self.log.warning("report generation skipped: %s", e)

    # ---- main ----
    def run(self) -> int:
        self._install_signals()
        dump_resolved(self.m, self.rp.manifest_resolved)
        state = ckpt.read_state(self.rp) or ckpt.RunState(
            run_id=self.rp.run_id, manifest_path=self.opts.manifest_path, profile=self.m.profile_slug)
        self.log.info("run %s profile=%s total=%.0fmin tick=%ss scale=%sx workers=%s",
                      self.rp.run_id, self.m.profile_slug, self.m.total_minutes(),
                      self.opts.tick_seconds, self.opts.time_scale, not self.opts.no_workers)

        resume_tick = state.tick + 1 if (self.opts.resume_run_id and state.tick >= 0) else 0
        if resume_tick == 0:
            # Fresh run: provision + seed (these are not redone on resume).
            self._provision(state)
            self._seed(state)
        # Observers + generators must (re)start on BOTH fresh and resumed runs —
        # otherwise a resumed run republishes setpoints that no worker is reading.
        if not self.opts.no_workers:
            self._start_observers()
            self._start_generators()

        state.status = ckpt.STATUS_RUNNING
        tick = resume_tick
        tick_wall = self.opts.tick_seconds / max(1e-9, self.opts.time_scale)
        try:
            while not self.engine.is_complete(tick * self.opts.tick_seconds):
                if self.opts.max_ticks is not None and tick >= self.opts.max_ticks:
                    self.log.info("max_ticks reached (%d)", tick)
                    break
                elapsed = tick * self.opts.tick_seconds
                window, sp = self.engine.tick_setpoint(elapsed, tick)
                setpoints.publish(self.rp, sp)

                if window.index != self._last_phase_index:
                    self._last_phase_index = window.index
                    if window.phase.name == "drain":
                        state.status = ckpt.STATUS_DRAIN
                    self._on_phase_enter(window.phase.name, window.phase.extra)

                state.phase = window.phase.name
                state.phase_index = window.index
                state.tick = tick
                state.elapsed_test_s = elapsed
                ckpt.write_state(self.rp, state)

                self._watch_and_sleep(sp, tick_wall)
                if self._stop:
                    break

                if window.phase.name == "drain" and window.phase.extra.get("wait_convergence") \
                        and self.engine.is_complete((tick + 1) * self.opts.tick_seconds):
                    self._await_convergence()
                tick += 1

            # final setpoint = idle so any lingering worker ramps to zero
            setpoints.publish(self.rp, setpoints.compute_setpoint(
                self.m, phase_name="stopped", load="idle", elapsed_s=0, tick=tick))

            if self._aborting:
                state.status = ckpt.STATUS_ABORTED
            elif self._stop and not self.engine.is_complete(tick * self.opts.tick_seconds):
                state.status = ckpt.STATUS_ABORTED
            else:
                state.status = ckpt.STATUS_COLLECT
            ckpt.write_state(self.rp, state)
        finally:
            self._stop_workers()
            atomic_write_json(self.rp.snapshot("final"),
                              {"status": state.status, "ticks": tick, "run_id": self.rp.run_id})
            if state.status in (ckpt.STATUS_COLLECT, ckpt.STATUS_ABORTED):
                self._run_report()
                if state.status == ckpt.STATUS_COLLECT:
                    state.status = ckpt.STATUS_DONE
                ckpt.write_state(self.rp, state)

        self.log.info("run %s finished status=%s ticks=%d", self.rp.run_id, state.status, tick)
        return 0 if state.status == ckpt.STATUS_DONE else 1
