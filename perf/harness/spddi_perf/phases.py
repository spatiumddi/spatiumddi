"""Phase engine (docs/PERFORMANCE_TESTING.md §7.2).

Maps the manifest's ordered phases onto a test clock and, for any tick, produces the
setpoint by resolving the phase's ``load`` mode (idle / ramp / steady / peak / diurnal)
plus the ceiling-probe and operator-ramp levers. The test clock starts at local 00:00
so the diurnal soak's natural surge falls ~T+7..T+8 (§1.8).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import setpoints
from .manifest import Manifest, Phase
from .setpoints import Setpoint

# Defaults for the peak ceiling probe (multiply offered rate from 1.0 → max across peak).
DEFAULT_PROBE_MAX = 1.5


@dataclass
class PhaseWindow:
    phase: Phase
    index: int
    start_s: float   # test-clock start (inclusive)
    end_s: float     # test-clock end (exclusive)

    def position(self, elapsed_s: float) -> float:
        span = max(1e-9, self.end_s - self.start_s)
        return max(0.0, min(1.0, (elapsed_s - self.start_s) / span))


class PhaseEngine:
    """Resolves elapsed test-seconds → (active phase, setpoint)."""

    def __init__(self, m: Manifest) -> None:
        self.m = m
        self.windows: list[PhaseWindow] = []
        t = 0.0
        for i, ph in enumerate(m.phases):
            dur = ph.minutes * 60.0
            self.windows.append(PhaseWindow(ph, i, t, t + dur))
            t += dur
        self.total_s = t

    def window_at(self, elapsed_s: float) -> PhaseWindow:
        for w in self.windows:
            if w.start_s <= elapsed_s < w.end_s:
                return w
        return self.windows[-1]  # past the end → clamp to last (drain)

    def tick_setpoint(self, elapsed_s: float, tick: int) -> tuple[PhaseWindow, Setpoint]:
        w = self.window_at(elapsed_s)
        ph = w.phase
        pos = w.position(elapsed_s)

        multiplier = 1.0
        if ph.extra.get("probe_ceiling"):
            probe_max = float(ph.extra.get("probe_max", DEFAULT_PROBE_MAX))
            multiplier = 1.0 + pos * (probe_max - 1.0)

        operator_per_s = None
        if ph.extra.get("operator_ramp"):
            op = self.m.scale.operator_mutation_stream
            operator_per_s = op.sustained_per_s + pos * (op.burst_per_s - op.sustained_per_s)

        sp = setpoints.compute_setpoint(
            self.m,
            phase_name=ph.name,
            load=ph.load,
            elapsed_s=elapsed_s,
            tick=tick,
            phase_pos=pos,
            ramp_from=ph.extra.get("from"),
            ramp_to=ph.extra.get("to"),
            multiplier=multiplier,
            operator_per_s=operator_per_s,
        )
        return w, sp

    def is_complete(self, elapsed_s: float) -> bool:
        return elapsed_s >= self.total_s
