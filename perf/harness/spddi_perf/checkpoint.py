"""Run-state checkpointing (docs/PERFORMANCE_TESTING.md §7.4).

``state.json`` is written temp-then-rename every tick so a crash never leaves it
half-written; ``--resume`` reads it back and continues from the recorded tick (the
curve is a pure function of tick, so resumption is deterministic). ``events.ndjson``
is the append-only audit of phase transitions, throttles, aborts, warnings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .logging_util import append_ndjson, atomic_write_json, read_json, utc_now_iso
from .runpaths import RunPaths

# Lifecycle statuses (the report uses these to distinguish FAIL from INVALID, §7.6.5).
STATUS_PROVISION = "provisioning"
STATUS_SEED = "seeding"
STATUS_RUNNING = "running"
STATUS_DRAIN = "draining"
STATUS_COLLECT = "collecting"
STATUS_DONE = "done"
STATUS_ABORTED = "aborted"      # watchdog protected the box / kill-switch / SLO guardrail
STATUS_INVALID = "invalid"      # SUT failed on its own (CNPG OOM, reboot) — re-run, not a verdict


@dataclass
class RunState:
    run_id: str
    manifest_path: str
    profile: str
    started_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    status: str = STATUS_PROVISION
    phase: str = ""
    phase_index: int = -1
    tick: int = -1
    elapsed_test_s: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunState":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


def write_state(rp: RunPaths, state: RunState) -> None:
    state.updated_at = utc_now_iso()
    atomic_write_json(rp.state, state.to_dict())


def read_state(rp: RunPaths) -> RunState | None:
    d = read_json(rp.state)
    return RunState.from_dict(d) if d else None


def append_event(rp: RunPaths, kind: str, **fields: Any) -> None:
    """Record a run event (phase_transition, watchdog_throttle, abort, warning, ...)."""
    append_ndjson(rp.events, {"kind": kind, **fields})
