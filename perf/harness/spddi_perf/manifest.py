"""Run-manifest schema — load + validate (docs/PERFORMANCE_TESTING.md §7.3).

The manifest is the single source of truth for one run; the controller stamps a
resolved copy into the artifact bundle. Secrets are NEVER in the manifest — the
superadmin token and psql DSN are referenced by env-var *name* (non-negotiable #6).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1

# Env overrides so an operator can point any manifest at their appliance WITHOUT
# editing the committed placeholder (SPDDI_PERF_NODE_IP=192.168.0.125 → api_base too).
ENV_NODE_IP = "SPDDI_PERF_NODE_IP"
ENV_API_BASE = "SPDDI_PERF_API_BASE"


class ManifestError(ValueError):
    """Raised when a manifest is structurally invalid or internally inconsistent."""


@dataclass
class DhcpTarget:
    port: int = 67
    topology: str = "broadcast"      # "relay" | "broadcast" (Phase-0 decision, §3.1.2)
    giaddr: list[str] = field(default_factory=list)  # one per subnet when relay
    # Egress interface for the broadcast DISCOVER (perf #454). On a multi-homed
    # load-gen box ``255.255.255.255`` won't necessarily leave the NIC facing the
    # appliance, so the orchestrator binds the socket to this device
    # (SO_BINDTODEVICE). Empty = let the kernel route (single-homed boxes).
    # ``SPDDI_PERF_DHCP_IFACE`` overrides at runtime.
    iface: str = ""


@dataclass
class DnsTarget:
    port: int = 53
    driver: str = "bind9"            # "bind9" | "powerdns"
    recursion: bool = False          # MUST be False for the test (§4.9)


@dataclass
class Target:
    node_ip: str = ""
    api_base: str = ""
    dhcp: DhcpTarget = field(default_factory=DhcpTarget)
    dns: DnsTarget = field(default_factory=DnsTarget)
    appliance: dict[str, Any] = field(default_factory=dict)  # expected_version, cnpg_instances, ...


@dataclass
class OperatorStream:
    enabled: bool = True
    sustained_per_s: float = 3.0
    burst_per_s: float = 50.0


@dataclass
class Scale:
    unique_devices: int = 250_000
    peak_active_devices: int = 150_000
    students: int = 50_000
    lease_time_s: int = 7200         # T1 is 900s HARDCODED regardless (confirm Phase 0)
    ddns_enabled: bool = True
    query_log_enabled: bool = False
    hostname_fraction: float = 0.7   # fraction of devices that publish DDNS (§3.2 lever)
    operator_mutation_stream: OperatorStream = field(default_factory=OperatorStream)


@dataclass
class SeedDns:
    forward_zones: list[str] = field(default_factory=lambda: ["campus.example.edu"])
    reverse_zone_shape: str = "per-octet"   # "per-octet" | "single" (§0.A)
    reverse_zones: list[str] = field(default_factory=list)
    authoritative_records: int = 250_000    # §4.9 Layer 1 — large authoritative dataset


@dataclass
class Seed:
    ip_block: str = "10.0.0.0/8"
    subnets: dict[str, Any] = field(default_factory=lambda: {"count": 8, "prefix": 16, "pool_fraction": 0.90})
    dns: SeedDns = field(default_factory=SeedDns)
    statics: int = 0
    relay_addresses_per_scope: bool = False


@dataclass
class Diurnal:
    # 24 hourly arrival weights (0..1 of peak); overrides the canonical shape if set.
    arrival_weights: list[float] = field(default_factory=list)
    dns_qps_sustained_peak: int = 18_000
    dns_qps_burst_ceiling: int = 120_000
    dora_per_s_peak: float = 12.5


@dataclass
class Phase:
    name: str
    minutes: float
    load: str = "steady"             # idle|ramp|steady|peak|diurnal
    extra: dict[str, Any] = field(default_factory=dict)  # from/to/probe_ceiling/...


@dataclass
class Slo:
    slo_thresholds_version: str = "v1-proposed"
    dhcp_ack_p99_ms: float = 50.0
    dns_resolve_p99_ms: float = 20.0
    lease_to_ipam_to_dns_p95_s: float = 10.0
    api_5xx_rate_max: float = 0.005
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Watchdog:
    poll_interval_s: float = 5.0
    throttle_before_abort: bool = True
    abort_on: dict[str, Any] = field(default_factory=dict)


@dataclass
class Guardrails:
    kill_switch_file: str = "run/STOP"
    max_dora_per_s: float = 400.0
    max_dns_qps: float = 150_000.0
    max_lease_events_per_s: float = 400.0
    watchdog: Watchdog = field(default_factory=Watchdog)


@dataclass
class Observability:
    superadmin_token_env: str = "SPDDI_PERF_ADMIN_TOKEN"
    psql_dsn_env: str = "SPDDI_PERF_PSQL_DSN"
    enable_pg_stat_statements: bool = True
    poll: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiskBudget:
    est_24h_gb: float = 28.0
    required_pv_gb: float = 64.0


@dataclass
class Manifest:
    schema_version: int
    run: dict[str, Any]
    target: Target
    scale: Scale
    seed: Seed
    diurnal: Diurnal
    phases: list[Phase]
    slo: Slo
    guardrails: Guardrails
    observability: Observability
    disk_budget: DiskBudget
    raw: dict[str, Any] = field(default_factory=dict)  # the verbatim parsed YAML

    @property
    def name(self) -> str:
        return str(self.run.get("name", "run"))

    @property
    def profile_slug(self) -> str:
        """Profile identity for the artifact dir / regression key (§8.1)."""
        lease = "short-lease" if self.scale.lease_time_s <= 7200 else "realistic-lease"
        ddns = "ddns-on" if self.scale.ddns_enabled else "ddns-off"
        qlog = "qlog-on" if self.scale.query_log_enabled else "qlog-off"
        stretch = "-300k" if self.scale.unique_devices >= 300_000 else ""
        return f"{lease}-{ddns}-{qlog}{stretch}"

    def total_minutes(self) -> float:
        return sum(p.minutes for p in self.phases)


def _as_target(d: dict[str, Any]) -> Target:
    dhcp = d.get("dhcp", {}) or {}
    dns = d.get("dns", {}) or {}
    return Target(
        node_ip=d.get("node_ip", ""),
        api_base=d.get("api_base", ""),
        dhcp=DhcpTarget(port=dhcp.get("port", 67), topology=dhcp.get("topology", "broadcast"),
                        giaddr=list(dhcp.get("giaddr", []) or []),
                        iface=os.environ.get("SPDDI_PERF_DHCP_IFACE", dhcp.get("iface", "")) or ""),
        dns=DnsTarget(port=dns.get("port", 53), driver=dns.get("driver", "bind9"),
                      recursion=bool(dns.get("recursion", False))),
        appliance=d.get("appliance", {}) or {},
    )


def _as_scale(d: dict[str, Any]) -> Scale:
    op = d.get("operator_mutation_stream", {}) or {}
    return Scale(
        unique_devices=int(d.get("unique_devices", 250_000)),
        peak_active_devices=int(d.get("peak_active_devices", 150_000)),
        students=int(d.get("students", 50_000)),
        lease_time_s=int(d.get("lease_time_s", 7200)),
        ddns_enabled=bool(d.get("ddns_enabled", True)),
        query_log_enabled=bool(d.get("query_log_enabled", False)),
        hostname_fraction=float(d.get("hostname_fraction", 0.7)),
        operator_mutation_stream=OperatorStream(
            enabled=bool(op.get("enabled", True)),
            sustained_per_s=float(op.get("sustained_per_s", 3.0)),
            burst_per_s=float(op.get("burst_per_s", 50.0)),
        ),
    )


def _as_seed(d: dict[str, Any]) -> Seed:
    dns = d.get("dns", {}) or {}
    return Seed(
        ip_block=d.get("ip_block", "10.0.0.0/8"),
        subnets=d.get("subnets", {"count": 8, "prefix": 16, "pool_fraction": 0.90}),
        dns=SeedDns(
            forward_zones=list(dns.get("forward_zones", ["campus.example.edu"])),
            reverse_zone_shape=dns.get("reverse_zone_shape", "per-octet"),
            reverse_zones=list(dns.get("reverse_zones", []) or []),
            authoritative_records=int(dns.get("authoritative_records", 250_000)),
        ),
        statics=int(d.get("statics", 0)),
        relay_addresses_per_scope=bool(d.get("relay_addresses_per_scope", False)),
    )


def _as_phases(items: list[dict[str, Any]]) -> list[Phase]:
    out: list[Phase] = []
    for it in items:
        known = {"name", "minutes", "load"}
        out.append(Phase(
            name=it["name"], minutes=float(it["minutes"]), load=it.get("load", "steady"),
            extra={k: v for k, v in it.items() if k not in known},
        ))
    return out


def from_dict(d: dict[str, Any]) -> Manifest:
    g = d.get("guardrails", {}) or {}
    wd = g.get("watchdog", {}) or {}
    o = d.get("observability", {}) or {}
    db = d.get("disk_budget", {}) or {}
    diu = d.get("diurnal", {}) or {}
    slo = d.get("slo", {}) or {}
    known_slo = {"slo_thresholds_version", "dhcp_ack_p99_ms", "dns_resolve_p99_ms",
                 "lease_to_ipam_to_dns_p95_s", "api_5xx_rate_max"}
    return Manifest(
        schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        run=d.get("run", {}) or {},
        target=_as_target(d.get("target", {}) or {}),
        scale=_as_scale(d.get("scale", {}) or {}),
        seed=_as_seed(d.get("seed", {}) or {}),
        diurnal=Diurnal(
            arrival_weights=list(diu.get("arrival_weights", []) or []),
            dns_qps_sustained_peak=int(diu.get("dns_qps_sustained_peak", 18_000)),
            dns_qps_burst_ceiling=int(diu.get("dns_qps_burst_ceiling", 120_000)),
            dora_per_s_peak=float(diu.get("dora_per_s_peak", 12.5)),
        ),
        phases=_as_phases(d.get("phases", []) or []),
        slo=Slo(
            slo_thresholds_version=slo.get("slo_thresholds_version", "v1-proposed"),
            dhcp_ack_p99_ms=float(slo.get("dhcp_ack_p99_ms", 50.0)),
            dns_resolve_p99_ms=float(slo.get("dns_resolve_p99_ms", 20.0)),
            lease_to_ipam_to_dns_p95_s=float(slo.get("lease_to_ipam_to_dns_p95_s", 10.0)),
            api_5xx_rate_max=float(slo.get("api_5xx_rate_max", 0.005)),
            extra={k: v for k, v in slo.items() if k not in known_slo},
        ),
        guardrails=Guardrails(
            kill_switch_file=g.get("kill_switch_file", "run/STOP"),
            max_dora_per_s=float(g.get("max_dora_per_s", 400.0)),
            max_dns_qps=float(g.get("max_dns_qps", 150_000.0)),
            max_lease_events_per_s=float(g.get("max_lease_events_per_s", 400.0)),
            watchdog=Watchdog(
                poll_interval_s=float(wd.get("poll_interval_s", 5.0)),
                throttle_before_abort=bool(wd.get("throttle_before_abort", True)),
                abort_on=wd.get("abort_on", {}) or {},
            ),
        ),
        observability=Observability(
            superadmin_token_env=o.get("superadmin_token_env", "SPDDI_PERF_ADMIN_TOKEN"),
            psql_dsn_env=o.get("psql_dsn_env", "SPDDI_PERF_PSQL_DSN"),
            enable_pg_stat_statements=bool(o.get("enable_pg_stat_statements", True)),
            poll=o.get("poll", {}) or {},
        ),
        disk_budget=DiskBudget(
            est_24h_gb=float(db.get("est_24h_gb", 28.0)),
            required_pv_gb=float(db.get("required_pv_gb", 64.0)),
        ),
        raw=d,
    )


def validate(m: Manifest) -> list[str]:
    """Return a list of human-readable problems; empty == valid."""
    problems: list[str] = []
    if m.schema_version != SCHEMA_VERSION:
        problems.append(f"schema_version {m.schema_version} != supported {SCHEMA_VERSION}")
    if not m.target.node_ip:
        problems.append("target.node_ip is required")
    if not m.target.api_base:
        problems.append("target.api_base is required")
    if m.target.dns.recursion:
        problems.append("target.dns.recursion MUST be false (DNS safety, §4.9)")
    if m.scale.unique_devices < m.scale.peak_active_devices:
        problems.append("scale.unique_devices must be >= peak_active_devices")
    if not (0.0 <= m.scale.hostname_fraction <= 1.0):
        problems.append("scale.hostname_fraction must be in [0,1]")
    if m.target.dhcp.topology not in ("relay", "broadcast"):
        problems.append("target.dhcp.topology must be 'relay' or 'broadcast'")
    if m.target.dhcp.topology == "relay":
        n_subnets = int(m.seed.subnets.get("count", 0))
        if len(m.target.dhcp.giaddr) != n_subnets:
            problems.append(
                f"relay topology needs one giaddr per subnet: "
                f"{len(m.target.dhcp.giaddr)} giaddrs vs {n_subnets} subnets (§3.1.2)")
        if not m.seed.relay_addresses_per_scope:
            problems.append("relay topology requires seed.relay_addresses_per_scope=true")
    if m.seed.dns.reverse_zone_shape not in ("per-octet", "single"):
        problems.append("seed.dns.reverse_zone_shape must be 'per-octet' or 'single'")
    if m.diurnal.arrival_weights and len(m.diurnal.arrival_weights) != 24:
        problems.append("diurnal.arrival_weights, if set, must have exactly 24 entries")
    if not m.phases:
        problems.append("at least one phase is required")
    return problems


def apply_env_overrides(m: Manifest) -> Manifest:
    """Apply ``SPDDI_PERF_NODE_IP`` / ``SPDDI_PERF_API_BASE`` over the manifest target,
    so an operator never has to edit a committed placeholder to point at their box."""
    node_ip = os.environ.get(ENV_NODE_IP, "").strip()
    api_base = os.environ.get(ENV_API_BASE, "").strip()
    tgt = m.raw.setdefault("target", {})
    if node_ip:
        m.target.node_ip = node_ip
        tgt["node_ip"] = node_ip
        if not api_base:
            api_base = f"https://{node_ip}/api"
    if api_base:
        m.target.api_base = api_base
        tgt["api_base"] = api_base
    return m


def load(path: str | Path) -> Manifest:
    """Load + validate a manifest YAML. Raises :class:`ManifestError` if invalid."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: top-level YAML must be a mapping")
    m = apply_env_overrides(from_dict(data))
    problems = validate(m)
    if problems:
        raise ManifestError(f"{path} is invalid:\n  - " + "\n  - ".join(problems))
    return m


def dump_resolved(m: Manifest, path: str | Path) -> None:
    """Write the verbatim parsed manifest to ``path`` (the pinned, resolved copy)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(m.raw, f, sort_keys=False, default_flow_style=False)
