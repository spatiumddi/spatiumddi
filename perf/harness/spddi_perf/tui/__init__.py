"""Interactive TUI console for the perf harness (docs/PERFORMANCE_TESTING.md §9.3).

A `rich`-based console — modeled on the appliance's Talos-style console — that DRIVES
the existing headless controller + setpoint bus rather than reimplementing any run
logic. It spawns/attaches a run, reads the run-dir artifacts on a tick, and offers an
F-key control strip with default-No confirm modals.

  python3 -m spddi_perf.cli tui [--manifest M] [--run-id ID] [--render-once]

No new control path: Start spawns `spddi_perf.cli run`, Stop trips the kill-switch
file, Abort terminates the controller process — exactly what the CLI already does.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    # Imported lazily so `import spddi_perf.cli` never hard-requires `rich`.
    from .app import main as _main
    return _main(argv)
