"""The interactive TUI app — manifest picker → live dashboard, F-key control with
default-No confirm modals. Drives the headless controller via :class:`RunMonitor`.

Run modes:
  python3 -m spddi_perf.cli tui                     # picker → start/attach a run
  python3 -m spddi_perf.cli tui --run-id <id>       # attach to a specific run
  python3 -m spddi_perf.cli tui --render-once       # render one frame + exit (no TTY needed)
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path
from typing import Callable

from rich.console import Console

from .. import manifest as manifest_mod
from ..phases import PhaseEngine
from ..runpaths import DEFAULT_RUN_ROOT
from ..workers import PERF_DIR
from . import keys, panels
from .runmon import RunMonitor

TICK_S = 0.4
MANIFEST_DIR = Path(PERF_DIR) / "manifests"


@dataclass
class UiState:
    mode: str = "picker"            # "picker" | "dashboard"
    picker_idx: int = 0
    preview: list[str] = field(default_factory=lambda: ["(Enter to preview)"])
    modal: dict | None = None       # {"title","body","action": callable}
    modal_yes: bool = False
    quit: bool = False


def _manifest_preview(path: str) -> list[str]:
    try:
        m = manifest_mod.load(path)
    except Exception as e:  # noqa: BLE001 — show the validation error to the operator
        return [f"INVALID: {e}"]
    eng = PhaseEngine(m)
    out = [
        f"name     {m.name}",
        f"profile  {m.profile_slug}",
        f"scale    {m.scale.unique_devices:,} devices · peak {m.scale.peak_active_devices:,}",
        f"lease    {m.scale.lease_time_s}s (T1=900s) · ddns={m.scale.ddns_enabled} qlog={m.scale.query_log_enabled}",
        f"topology {m.target.dhcp.topology} · reverse={m.seed.dns.reverse_zone_shape} · recursion={m.target.dns.recursion}",
        f"target   {m.target.node_ip}  ({m.target.api_base})",
        f"total    {m.total_minutes():.0f} min · {len(m.phases)} phases:",
    ]
    for w in eng.windows:
        out.append(f"   {w.phase.name:13s} {w.phase.minutes:6.0f}m  {w.phase.load}")
    out += ["", "⚠ Phase-0 + DNS-safety gates run automatically at Start (seed/provision)."]
    return out


class TuiApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.console = Console()
        self.run_root = args.run_root
        self.manifests = sorted(glob(str(MANIFEST_DIR / "*.yaml")))
        self.mon = RunMonitor(self.run_root, run_id=args.run_id, manifest_path=args.manifest)
        self.ui = UiState()
        if args.run_id:
            self.ui.mode = "dashboard"
        # default Start manifest = the one passed, else the first listed
        self.start_manifest = args.manifest or (self.manifests[0] if self.manifests else None)

    # ---- run-control extra args forwarded to the spawned controller ----
    def _run_extra(self) -> list[str]:
        extra: list[str] = []
        if self.args.time_scale and self.args.time_scale != 1.0:
            extra += ["--time-scale", str(self.args.time_scale)]
        if self.args.orchestrator_shards:
            extra += ["--orchestrator-shards", str(self.args.orchestrator_shards)]
        if self.args.perfdhcp_shards:
            extra += ["--perfdhcp-shards", str(self.args.perfdhcp_shards)]
        return extra

    # ---- render one frame (TTY-free; for --render-once + the Live loop) ----
    def _renderable(self):
        if self.ui.modal:
            m = self.ui.modal
            return panels.build_modal(m["title"], m["body"], self.ui.modal_yes)
        if self.ui.mode == "picker":
            return panels.build_picker(self.manifests, self.ui.picker_idx, self.ui.preview)
        snap = self.mon.snapshot()
        if snap is None:
            return panels.build_picker(self.manifests, self.ui.picker_idx,
                                       ["No run selected.", "Start one (F1) or attach (a)."])
        return panels.build_dashboard(snap, self.mon.controller_alive())

    def render_once(self) -> int:
        if not self.mon.run_id:
            self.mon.attach_latest()
            if self.mon.run_id:
                self.ui.mode = "dashboard"
        self.console.print(self._renderable())
        return 0

    # ---- modal helpers ----
    def _confirm(self, title: str, body: str, action: Callable[[], None]) -> None:
        self.ui.modal = {"title": title, "body": body, "action": action}
        self.ui.modal_yes = False  # default No (§9.3)

    def _close_modal(self, run: bool) -> None:
        m = self.ui.modal
        self.ui.modal = None
        if run and m:
            m["action"]()

    # ---- key handling ----
    def _handle_modal_key(self, k: str) -> None:
        if k in (keys.LEFT, keys.RIGHT, keys.TAB):
            self.ui.modal_yes = not self.ui.modal_yes
        elif k in ("y", "Y"):
            self._close_modal(True)
        elif k in ("n", "N", keys.ESC):
            self._close_modal(False)
        elif k == keys.ENTER:
            self._close_modal(self.ui.modal_yes)

    def _handle_picker_key(self, k: str) -> None:
        n = len(self.manifests)
        if k == keys.UP and n:
            self.ui.picker_idx = (self.ui.picker_idx - 1) % n
        elif k == keys.DOWN and n:
            self.ui.picker_idx = (self.ui.picker_idx + 1) % n
        elif k == keys.ENTER and n:
            self.ui.preview = _manifest_preview(self.manifests[self.ui.picker_idx])
        elif k in ("s", "S", keys.F1) and n:
            self.start_manifest = self.manifests[self.ui.picker_idx]
            self._do_start()
        elif k in ("a", "A"):
            if self.mon.attach_latest():
                self.ui.mode = "dashboard"
        elif k in ("q", "Q", keys.CTRL_C):
            self.ui.quit = True

    def _handle_dashboard_key(self, k: str) -> None:
        alive = self.mon.controller_alive()
        if k in ("s", "S", keys.F1) and not alive:
            self._do_start()
        elif k in ("p", "P", keys.F3) and self.mon.run_id:
            self._confirm("Trigger prune", "Kick the daily log-prune on the appliance now?", self.mon.trigger_prune)
        elif k in ("r", "R", keys.F4) and self.mon.run_id and not alive:
            self.mon.resume_run()
        elif k in ("x", "X", keys.F5) and self.mon.run_id and alive:
            self._confirm("Stop run", "Trip the kill-switch?\nWorkers ramp to zero and the run stops gracefully.", self.mon.stop_run)
        elif k == keys.F6 and self.mon.run_id and alive:
            self._confirm("ABORT run", "Abort now? Terminates the controller process\nand trips the kill-switch.", self.mon.abort_run)
        elif k in ("m", "M", keys.F8):
            self.ui.mode = "picker"
        elif k in ("q", "Q", keys.CTRL_C):
            if alive:
                self._confirm("Quit TUI", "The run keeps running headless.\nQuit the console?", self._set_quit)
            else:
                self.ui.quit = True

    def _set_quit(self) -> None:
        self.ui.quit = True

    def _do_start(self) -> None:
        if not self.start_manifest:
            return
        self.mon.manifest_path = self.start_manifest
        self.mon.start_run(self.start_manifest, extra_args=self._run_extra())
        self.ui.mode = "dashboard"

    def _dispatch(self, k: str) -> None:
        if self.ui.modal:
            self._handle_modal_key(k)
        elif self.ui.mode == "picker":
            self._handle_picker_key(k)
        else:
            self._handle_dashboard_key(k)

    # ---- main interactive loop ----
    def run_interactive(self) -> int:
        if not keys.is_a_tty():
            self.console.print("[red]TUI needs an interactive terminal.[/] "
                               "Use --render-once for a one-shot frame, or the headless `run` command.")
            return 2
        from rich.live import Live
        if self.ui.mode == "picker" and self.manifests:
            self.ui.preview = _manifest_preview(self.manifests[self.ui.picker_idx])
        with keys.KeyReader() as kr, Live(self._renderable(), console=self.console,
                                          screen=True, auto_refresh=False) as live:
            while not self.ui.quit:
                for k in kr.drain():
                    self._dispatch(k)
                    if self.ui.quit:
                        break
                live.update(self._renderable())
                live.refresh()
                time.sleep(TICK_S)
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spddi-perf tui", description="Interactive perf console")
    p.add_argument("--manifest", help="default manifest for Start")
    p.add_argument("--run-id", help="attach to an existing run")
    p.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    p.add_argument("--render-once", action="store_true", help="render one frame and exit (no TTY needed)")
    p.add_argument("--time-scale", type=float, default=1.0)
    p.add_argument("--orchestrator-shards", type=int, default=1)
    p.add_argument("--perfdhcp-shards", type=int, default=0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = TuiApp(args)
    if args.render_once:
        return app.render_once()
    return app.run_interactive()


if __name__ == "__main__":
    raise SystemExit(main())
