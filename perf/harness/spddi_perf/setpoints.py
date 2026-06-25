"""The setpoint bus — the file-based loose coupling between controller and workers.

The controller samples the §1.3 diurnal curve (scaled to the manifest) on a 60s tick
and publishes one :class:`Setpoint` to ``run/<run_id>/setpoints/current.json`` (plus
an append to ``history.ndjson``). Every worker (perfdhcp shard, dnsperf runner, the
orchestrator) reads ``current.json`` keyed by ``tick`` and trues-up its offered rate.
A worker reading a stale tick logs ``setpoint_lag`` — the leading indicator that a
load-gen box is saturating (so generator saturation is never mistaken for an SUT
bottleneck). Shape matches docs/PERFORMANCE_TESTING.md §7.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import canonical
from .logging_util import append_ndjson, atomic_write_json, read_json, utc_now_iso
from .manifest import Guardrails, Manifest
from .runpaths import RunPaths

# Hours on the §1.3 curve that anchor the named "steady" / "peak" loads.
STEADY_HOUR = 10.0   # mid-morning plateau
PEAK_HOUR = 8.0      # 08:00 surge peak
DEFAULT_NXDOMAIN_FRAC = 0.02   # §1.7 deliberate in-zone miss slice


@dataclass
class Setpoint:
    tick: int
    phase: str
    new_dora_per_s: float
    renew_per_s: float
    active_devices: int
    dns_qps: float
    nxdomain_frac: float = DEFAULT_NXDOMAIN_FRAC
    ddns_enabled: bool = True
    query_log_enabled: bool = False
    operator_mutation_per_s: float = 0.0
    ts: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "tick": self.tick,
            "phase": self.phase,
            "dhcp": {
                "new_dora_per_s": round(self.new_dora_per_s, 3),
                "renew_per_s": round(self.renew_per_s, 3),
                "active_devices": int(self.active_devices),
            },
            "dns": {"qps": round(self.dns_qps, 1), "nxdomain_frac": self.nxdomain_frac},
            "ddns_enabled": self.ddns_enabled,
            "query_log_enabled": self.query_log_enabled,
            "operator_mutation_per_s": round(self.operator_mutation_per_s, 3),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Setpoint":
        dhcp = d.get("dhcp", {})
        dns = d.get("dns", {})
        return cls(
            tick=int(d["tick"]),
            phase=d.get("phase", ""),
            new_dora_per_s=float(dhcp.get("new_dora_per_s", 0.0)),
            renew_per_s=float(dhcp.get("renew_per_s", 0.0)),
            active_devices=int(dhcp.get("active_devices", 0)),
            dns_qps=float(dns.get("qps", 0.0)),
            nxdomain_frac=float(dns.get("nxdomain_frac", DEFAULT_NXDOMAIN_FRAC)),
            ddns_enabled=bool(d.get("ddns_enabled", True)),
            query_log_enabled=bool(d.get("query_log_enabled", False)),
            operator_mutation_per_s=float(d.get("operator_mutation_per_s", 0.0)),
            ts=d.get("ts", utc_now_iso()),
        )

    @property
    def lease_events_per_s(self) -> float:
        return self.new_dora_per_s + self.renew_per_s

    def clamp(self, g: Guardrails) -> "Setpoint":
        """Apply the manifest hard max-rate caps so a typo/runaway can't DoS the NIC."""
        self.new_dora_per_s = min(self.new_dora_per_s, g.max_dora_per_s)
        self.dns_qps = min(self.dns_qps, g.max_dns_qps)
        # Cap lease-events: trim renewals first (DORA is the realism-critical signal).
        if self.lease_events_per_s > g.max_lease_events_per_s:
            self.renew_per_s = max(0.0, g.max_lease_events_per_s - self.new_dora_per_s)
        return self


def _scale_factors(m: Manifest) -> tuple[float, float, float]:
    """(online_scale, arrival_scale, qps_scale) mapping the 250k reference to this run."""
    online_scale = m.scale.peak_active_devices / canonical.PEAK_ONLINE
    arrival_scale = m.scale.unique_devices / canonical.D_TOTAL_HEADLINE
    qps_scale = m.diurnal.dns_qps_sustained_peak / canonical.DNS_QPS_SUSTAINED_PEAK
    return online_scale, arrival_scale, qps_scale


def _scaled_point(m: Manifest, hour: float) -> dict[str, float]:
    """The reference curve at ``hour`` scaled to this manifest. renew recomputed."""
    online_scale, arrival_scale, qps_scale = _scale_factors(m)
    ref = canonical.reference_point(hour)
    online = ref["online"] * online_scale
    return {
        "online": online,
        "dora_per_s": ref["dora_per_s"] * arrival_scale,
        "renew_per_s": canonical.renew_per_s(online),
        "dns_qps": ref["dns_qps"] * qps_scale,
    }


def _load_targets(m: Manifest, load: str, elapsed_s: float) -> dict[str, float]:
    """Resolve a named load mode to absolute (online, dora_per_s, dns_qps)."""
    if load == "idle":
        return {"online": 0.0, "dora_per_s": 0.0, "renew_per_s": 0.0, "dns_qps": 0.0}
    if load == "steady":
        return _scaled_point(m, STEADY_HOUR)
    if load == "peak":
        return _scaled_point(m, PEAK_HOUR)
    if load == "diurnal":
        hour = (elapsed_s / 3600.0) % 24.0
        return _scaled_point(m, hour)
    raise ValueError(f"unknown load mode: {load!r}")


def _lerp(a: dict[str, float], b: dict[str, float], t: float) -> dict[str, float]:
    t = max(0.0, min(1.0, t))
    return {k: a[k] * (1.0 - t) + b[k] * t for k in a}


def compute_setpoint(
    m: Manifest,
    *,
    phase_name: str,
    load: str,
    elapsed_s: float,
    tick: int,
    phase_pos: float = 1.0,
    ramp_from: str | None = None,
    ramp_to: str | None = None,
    multiplier: float = 1.0,
    operator_per_s: float | None = None,
) -> Setpoint:
    """Produce the setpoint for one tick.

    ``load='ramp'`` lerps between ``ramp_from`` and ``ramp_to`` by ``phase_pos`` (0..1).
    ``multiplier`` scales the offered rates (the ceiling-probe lever in the peak phase).
    ``operator_per_s`` overrides the operator-mutation stream rate (burst in peak).
    """
    if load == "ramp":
        a = _load_targets(m, ramp_from or "idle", elapsed_s)
        b = _load_targets(m, ramp_to or "steady", elapsed_s)
        tgt = _lerp(a, b, phase_pos)
    else:
        tgt = _load_targets(m, load, elapsed_s)

    online = tgt["online"]
    op_rate = operator_per_s
    if op_rate is None:
        op = m.scale.operator_mutation_stream
        op_rate = op.sustained_per_s if op.enabled else 0.0

    sp = Setpoint(
        tick=tick,
        phase=phase_name,
        new_dora_per_s=tgt["dora_per_s"] * multiplier,
        renew_per_s=canonical.renew_per_s(online),  # always online/900, never lerped
        active_devices=int(round(online)),
        dns_qps=tgt["dns_qps"] * multiplier,
        ddns_enabled=m.scale.ddns_enabled,
        query_log_enabled=m.scale.query_log_enabled,
        operator_mutation_per_s=op_rate,
    )
    return sp.clamp(m.guardrails)


def publish(rp: RunPaths, sp: Setpoint) -> None:
    """Atomically write ``current.json`` and append to ``history.ndjson``."""
    d = sp.to_dict()
    atomic_write_json(rp.setpoint_current, d)
    append_ndjson(rp.setpoint_history, d)


def read_current(rp: RunPaths) -> Setpoint | None:
    """Workers call this each loop to fetch the latest setpoint (None if not yet set)."""
    d = read_json(rp.setpoint_current)
    return Setpoint.from_dict(d) if d else None
