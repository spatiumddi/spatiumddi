"""spddi_perf — shared core for the SpatiumDDI performance test suite.

This package is the *contract spine* for the whole suite. Every off-box component
(seeder, generators, war-room poller, controller, report generator) imports from
here so they agree on:

  * canonical  — the §0.A single-source-of-truth numbers + the §1.3 diurnal curve
  * manifest   — the run-manifest YAML schema (load + validate)
  * setpoints  — the file-based setpoint bus (the loose coupling between controller
                 and workers; §7.1)
  * runpaths   — the on-disk run-directory layout (§7.4)
  * logging_util — structured JSON logging (non-negotiable #7) + atomic writes

Components are coupled through *files* (the setpoint JSON + the run dir), not Python
imports of each other — so they can be built and run independently. The only shared
Python is this package.

Put ``perf/harness`` on ``PYTHONPATH`` to import it (the Makefile + README do this).
See docs/PERFORMANCE_TESTING.md for the full design.
"""

from __future__ import annotations

__version__ = "0.1.0"
SERVICE = "spddi-perf"

__all__ = ["__version__", "SERVICE"]
