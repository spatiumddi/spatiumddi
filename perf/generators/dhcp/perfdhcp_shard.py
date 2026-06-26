#!/usr/bin/env python3
"""perfdhcp shard — raw Kea DHCPv4 ceiling probe (docs/PERFORMANCE_TESTING.md §3.0/§3.1).

This is the protocol-tier ceiling tool: it wraps the Debian ``kea`` package's
``/usr/sbin/perfdhcp`` (NOT in the appliance Alpine image — §3.0/§3.1) and offers a
raw DORA (or renew-only) rate at the appliance's Kea, parsing perfdhcp's periodic
``-t`` stats into the harness setpoint-driven artifact bundle. It answers "is Kea the
protocol bottleneck?" (§3.0 — almost certainly no) and gives the raw DHCP-ACK latency
baseline (b) and the protocol-tier ceiling (c). It is NOT the realistic load — the
asyncio orchestrator owns the 24h lifecycle / IPAM / DDNS firehose.

CLI contract (perf/harness/spddi_perf/workers.py REGISTRY → "perfdhcp"):

    python3 perfdhcp_shard.py --run-id <id> --run-root <path> --manifest <path> \
        [--shard N --shards K] [--mode dora|renew] [--clients R] [--report-interval S] \
        [--retune-interval S] [--probe-rate R]

Long-running worker behaviour (contract §4):
  * Each loop reads the setpoint (``setpoints.read_current(rp)``) and trues-up the
    offered DORA rate to ``sp.dhcp.new_dora_per_s``, sharded so this process offers
    only its 1/K slice. A new offered rate restarts the bounded perfdhcp child with
    the new ``-r``.
  * STOPS cleanly on the kill-switch (``rp.stop_file``), on a STALE setpoint (tick
    stops advancing for >~3 ticks → controller gone → fail safe to OFF, never
    full-blast), or on SIGTERM/SIGINT.

Identity isolation (§3.2, §4.5): perfdhcp synthesises its own client MACs internally
(``-b mac=<base>`` increments the low bytes per simulated client). We pin each shard's
MAC base into a DISJOINT slice of the locally-administered OUI space using
``spddi_perf.fleet.device_mac`` / ``shard_indices`` so perfdhcp identities NEVER
collide with the orchestrator's per-device identities or with sibling shards. The
orchestrator owns the low-index space contiguously; perfdhcp shards are pushed into a
high, reserved window (see ``PERFDHCP_INDEX_BASE``).

Grounding (real backend, cited file:line):
  * Kea selects the subnet for a relayed packet by its ``giaddr`` matching the scope's
    ``relay.ip-addresses`` — backend/app/drivers/dhcp/kea.py:236-241. So in relay
    topology we run one perfdhcp child per giaddr (each child's -T template stamps that
    giaddr). In broadcast topology there's one directly-attached scope and we target
    the node directly with no template.
  * The DHCP socket type the group renders (``raw`` for direct/broadcast, ``udp`` for
    relay) is backend/app/drivers/dhcp/kea.py:319-324 (``bundle.dhcp_socket_type``).
  * Lease-events ingestion cap is 500/POST (agent batches 100): the renew-only mode's
    purpose is to drive the Kea CSV-tail → /lease-events ingestion until it falls
    behind — backend/app/api/v1/dhcp/agents.py:151 (``max_length=500``) and the POST
    handler backend/app/api/v1/dhcp/agents.py:739. Watch ``lease_events_buffer_trimmed``
    in the war-room (§3.6). perfdhcp itself never calls the REST API — it speaks raw
    DHCPv4 on the wire to ``m.target.node_ip:m.target.dhcp.port``.

perfdhcp flag semantics grounded on §3.1 of the doc (Kea perfdhcp ARM):
  -4              : DHCPv4 mode.
  -r <rate>       : offered DORA (4-way) rate / s — the CEILING knob (§3.1).
  -R <num>        : distinct simulated clients (unique MAC/client-id). Memory grows
                    with cardinality and degrades past stability — §1.9 hard gate to
                    smoke-calibrate max stable clients/proc before the 300k run.
  -p <period-s>   : bounded probe duration. We restart per retune window.
  -t <interval-s> : periodic intermediate stats line (the latency histogram source).
  -T <template>   : packet template file (relay: carries the per-subnet giaddr).
  -b mac=<base>   : base MAC; perfdhcp increments it per simulated client.
  -n <num>        : total exchanges (renew-only phase-1 grab of N leases).
  -f <renew-rate> : send RENEW (REQUEST in RENEWING) at this rate (renew-only phase-2).
  -W <wait-s>     : wait between the two -f/-p phases (used in renew handoff).
  <server>        : unicast target (relay) — omitted for broadcast (perfdhcp -4
                    broadcasts on the local L2 when no server is given).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import spddi_perf.manifest as manifest_mod
import spddi_perf.setpoints as setpoints_mod
from spddi_perf import fleet
from spddi_perf.logging_util import append_ndjson, log_event
from spddi_perf.logging_util import get_logger
from spddi_perf.runpaths import RunPaths

SERVICE = "perfdhcp-shard"
# /usr/sbin/perfdhcp is where the Debian ``kea`` package installs it (§3.1). Allow an
# env override for non-standard installs / test stubs (still off-box, no secret).
PERFDHCP_BIN = os.environ.get("SPDDI_PERF_PERFDHCP_BIN", "/usr/sbin/perfdhcp")

# How many missed setpoint ticks before we conclude the controller is gone and fail
# safe to OFF (contract §4). The controller publishes on a 60s tick (§7.1), so 3 ticks
# is ~180s of no fresh setpoint.
STALE_TICK_LIMIT = 3

# Reserved window of the 24-bit device-index space for perfdhcp-synthesised clients,
# so they never collide with the orchestrator (which packs from index 0 upward) nor
# with sibling shards. perfdhcp increments its base MAC's low bytes per client, so each
# shard gets a contiguous block of size >= its (smoke-calibrated) -R cardinality.
#
# Sizing: the orchestrator's headline/stretch fleet is <= 300k devices (low indices
# 0..299,999). We start the perfdhcp window at index 4,194,304 (0x400000) — well clear
# of the orchestrator's 300k with a large margin — and give each shard a 200,000-wide
# block (>> any sane smoke-calibrated -R, §1.9). That leaves room for
# (0x1000000 - 0x400000) / 200000 ≈ 62 shards before the 24-bit OUI space is exhausted,
# which fails loud (fleet.device_mac raises) rather than wrapping into another shard.
PERFDHCP_INDEX_BASE = 0x400000          # 4,194,304 — above the orchestrator's <=300k
PERFDHCP_SHARD_BLOCK = 200_000          # per-shard MAC block (>> calibrated -R)
PERFDHCP_MAX_SHARDS = ((1 << 24) - PERFDHCP_INDEX_BASE) // PERFDHCP_SHARD_BLOCK  # ~62

# A retune only restarts perfdhcp when the sharded offered rate changes by more than
# this fraction — avoids thrashing the child on diurnal-curve micro-steps.
RETUNE_REL_THRESHOLD = 0.05


# ── perfdhcp intermediate / final stats parsing ──────────────────────────────────
#
# perfdhcp's intermediate report (emitted every -t seconds) looks like, per the Kea
# ARM "Statistics" section:
#
#   sent: 4123; received: 4119 (rcvd/sent: 99.90%)
#   sent: 8240; received: 8231 (rcvd/sent: 99.89%)
#
# and the FINAL summary block (printed on exit) carries the latency + outcome detail:
#
#   ***Rate statistics***
#   Rate: 4012.3 4-way exchanges/second, expected rate: 4000.0
#   ***Statistics***
#   total: 240000
#   ...
#   min delay: 0.000312 s
#   avg delay: 0.001204 s
#   max delay: 0.018221 s
#   std dev: 0.000884 s
#   ...
#   95th percentile: 0.002981 s
#   99th percentile: 0.006144 s
#   ...
#   declined: 12
#   reclaimed: 0
#   orphans: 3
#   ...
#
# perfdhcp does not emit a p50 line directly, so we surface avg-delay as the p50
# proxy and label it as such. We parse whatever lines appear; absent fields stay None.
_RE_SENT_RECV = re.compile(
    r"sent:\s*(\d+);\s*received:\s*(\d+)", re.IGNORECASE)
_RE_RATE = re.compile(
    r"Rate:\s*([\d.]+)\s*4-way exchanges/second", re.IGNORECASE)
_RE_AVG_DELAY = re.compile(r"avg delay:\s*([\d.]+)\s*s", re.IGNORECASE)
_RE_MAX_DELAY = re.compile(r"max delay:\s*([\d.]+)\s*s", re.IGNORECASE)
_RE_P95 = re.compile(r"95th percentile:\s*([\d.]+)\s*s", re.IGNORECASE)
_RE_P99 = re.compile(r"99th percentile:\s*([\d.]+)\s*s", re.IGNORECASE)
_RE_DECLINED = re.compile(r"declined:\s*(\d+)", re.IGNORECASE)
_RE_ORPHANS = re.compile(r"orphans:\s*(\d+)", re.IGNORECASE)
# perfdhcp counts unanswered exchanges that timed out as the gap between sent/received
# in the intermediate line; the final block also reports total dropped.
_RE_DROPPED = re.compile(r"dropped:\s*(\d+)", re.IGNORECASE)


@dataclass
class StatSnapshot:
    """One emitted stat record (mix of intermediate + final fields)."""

    offered_rate: float = 0.0
    achieved_rate: float | None = None
    ack_p50_ms: float | None = None      # perfdhcp avg-delay proxy (no native p50)
    ack_p95_ms: float | None = None
    ack_p99_ms: float | None = None
    ack_max_ms: float | None = None
    declines: int = 0
    timeouts: int = 0                    # derived sent-received gap (unanswered)
    retransmits: int = 0                 # perfdhcp doesn't expose; kept for schema parity
    orphans: int = 0
    sent: int = 0
    received: int = 0

    def to_record(self, *, shard: int) -> dict[str, object]:
        return {
            "shard": shard,
            "offered_rate": round(self.offered_rate, 3),
            "achieved_rate": (round(self.achieved_rate, 3)
                              if self.achieved_rate is not None else None),
            "ack_p50_ms": self.ack_p50_ms,
            "ack_p95_ms": self.ack_p95_ms,
            "ack_p99_ms": self.ack_p99_ms,
            "ack_max_ms": self.ack_max_ms,
            "declines": self.declines,
            "timeouts": self.timeouts,
            "retransmits": self.retransmits,
            "orphans": self.orphans,
            "sent": self.sent,
            "received": self.received,
        }


def _s_to_ms(val: str) -> float:
    return round(float(val) * 1000.0, 3)


def parse_perfdhcp_line(line: str, snap: StatSnapshot) -> bool:
    """Fold one perfdhcp stdout line into ``snap`` in place. Returns True if it was an
    intermediate ``sent:/received:`` line (one periodic tick → flush a record)."""
    is_tick = False
    m = _RE_SENT_RECV.search(line)
    if m:
        snap.sent = int(m.group(1))
        snap.received = int(m.group(2))
        snap.timeouts = max(0, snap.sent - snap.received)
        is_tick = True
    m = _RE_RATE.search(line)
    if m:
        snap.achieved_rate = float(m.group(1))
    m = _RE_AVG_DELAY.search(line)
    if m:
        snap.ack_p50_ms = _s_to_ms(m.group(1))
    m = _RE_MAX_DELAY.search(line)
    if m:
        snap.ack_max_ms = _s_to_ms(m.group(1))
    m = _RE_P95.search(line)
    if m:
        snap.ack_p95_ms = _s_to_ms(m.group(1))
    m = _RE_P99.search(line)
    if m:
        snap.ack_p99_ms = _s_to_ms(m.group(1))
    m = _RE_DECLINED.search(line)
    if m:
        snap.declines = int(m.group(1))
    m = _RE_ORPHANS.search(line)
    if m:
        snap.orphans = int(m.group(1))
    m = _RE_DROPPED.search(line)
    if m:
        # dropped exchanges are unanswered → fold into timeouts if larger.
        snap.timeouts = max(snap.timeouts, int(m.group(1)))
    return is_tick


# ── giaddr / topology resolution ─────────────────────────────────────────────────


@dataclass
class ShardPlan:
    """The per-loop offered-rate plan for this shard, split across giaddr children."""

    topology: str
    server: str          # unicast target (relay) or "" (broadcast → no server arg)
    port: int
    giaddrs: list[str]   # one child per giaddr in relay; [""] in broadcast
    clients: int
    report_interval: int
    mac_base: str        # this shard's reserved base MAC
    templates: dict[str, str] = field(default_factory=dict)  # giaddr → template path


def resolve_plan(
    m: manifest_mod.Manifest, *, shard: int, shards: int, clients: int,
    report_interval: int, template_dir: str,
) -> ShardPlan:
    """Resolve topology + giaddr assignment for this shard.

    Relay (§3.1.2): Kea selects the scope by ``giaddr`` (kea.py:236-241), so we run one
    perfdhcp child per giaddr. To keep each shard offering only its 1/K slice we
    PARTITION the 8 giaddrs across the K shards round-robin; a shard gets the giaddrs
    whose index ``g`` satisfies ``g % shards == shard`` (via fleet.shard_indices over
    the giaddr count). If a shard would get zero giaddrs (shards > 8) it still gets at
    least one (its ``shard % len(giaddr)``) so no shard idles.

    Broadcast: a single directly-attached scope; we target the node directly with no
    template and one (broadcast) child.
    """
    topo = m.target.dhcp.topology
    port = m.target.dhcp.port
    node = m.target.node_ip
    # Reserve a disjoint MAC block per shard from the high OUI window. Fail loud (not
    # wrap) if the shard count exceeds what the 24-bit OUI space holds, or if -R would
    # spill past a shard's block into the next shard's MACs.
    if shard >= PERFDHCP_MAX_SHARDS:
        raise ValueError(
            f"shard {shard} exceeds the perfdhcp MAC-window capacity "
            f"({PERFDHCP_MAX_SHARDS} shards of {PERFDHCP_SHARD_BLOCK} MACs each); "
            f"reduce --shards or grow PERFDHCP_SHARD_BLOCK/lower PERFDHCP_INDEX_BASE.")
    if clients > PERFDHCP_SHARD_BLOCK:
        raise ValueError(
            f"--clients {clients} > per-shard MAC block {PERFDHCP_SHARD_BLOCK}: "
            f"perfdhcp's MAC increments would spill into the next shard's identities "
            f"(§3.2 collision). Smoke-calibrate -R below the block (§1.9 gate).")
    base_index = PERFDHCP_INDEX_BASE + shard * PERFDHCP_SHARD_BLOCK
    mac_base = fleet.device_mac(base_index)

    if topo == "broadcast":
        # perfdhcp -4 with no <server> broadcasts on the local L2 (§3.1).
        return ShardPlan(topology="broadcast", server="", port=port, giaddrs=[""],
                         clients=clients, report_interval=report_interval,
                         mac_base=mac_base)

    # relay: assign this shard's slice of the 8 giaddrs.
    all_giaddrs = list(m.target.dhcp.giaddr)
    if not all_giaddrs:
        raise ValueError("relay topology but manifest carries no giaddr list (§3.1.2)")
    n = len(all_giaddrs)
    if shards <= n:
        mine = [all_giaddrs[i] for i in fleet.shard_indices(n, shard % n, shards)]
    else:
        # More shards than giaddrs: each shard owns exactly one giaddr (round-robin).
        mine = [all_giaddrs[shard % n]]
    if not mine:
        mine = [all_giaddrs[shard % n]]

    # Map each giaddr to its template file under template_dir (built by build_template.py).
    templates: dict[str, str] = {}
    for g in mine:
        templates[g] = os.path.join(template_dir, f"giaddr-{g}.hex")
    return ShardPlan(topology="relay", server=node, port=port, giaddrs=mine,
                     clients=clients, report_interval=report_interval,
                     mac_base=mac_base, templates=templates)


# ── perfdhcp child management ─────────────────────────────────────────────────────


def build_perfdhcp_argv(
    plan: ShardPlan, *, giaddr: str, offered_rate: float, mode: str,
    probe_period: int, renew_rate: float,
) -> list[str]:
    """Construct the perfdhcp argv for one child (one giaddr in relay; broadcast=one).

    All flags grounded on §3.1 of the doc. The intermediate -t stats line is the
    latency-histogram source we parse.
    """
    argv: list[str] = [PERFDHCP_BIN, "-4"]
    # Offered rate (ceiling knob, §3.1). Renew-only phase-2 uses -f to send RENEWs.
    if mode == "renew":
        # Phase-1: grab `clients` leases at the offered DORA rate; phase-2: re-REQUEST
        # them at `renew_rate` (RENEWING). perfdhcp's -f sends renews at the given rate
        # alongside the 4-way exchange stream; combined with a short -W handoff this
        # proves the Kea-side renewal ACK rate + agent CSV-tail ingestion lag (§3.1).
        argv += ["-r", str(max(1, int(round(offered_rate))))]
        argv += ["-f", str(max(1, int(round(renew_rate))))]
    else:
        argv += ["-r", str(max(1, int(round(offered_rate))))]
    argv += ["-R", str(plan.clients)]
    argv += ["-p", str(probe_period)]
    argv += ["-t", str(plan.report_interval)]
    argv += ["-b", f"mac={plan.mac_base}"]  # this shard's reserved MAC block (§3.2)
    if plan.topology == "relay":
        tmpl = plan.templates.get(giaddr)
        if tmpl and os.path.exists(tmpl):
            argv += ["-T", tmpl]  # carries the per-subnet giaddr (§3.1.2)
        else:
            # No template on disk → fall back to perfdhcp's relayed-mode flag so the
            # giaddr still routes; build_template.py generates the .hex templates.
            argv += ["-A", "1"]  # encapsulate as relayed (RAI) — see relay_templates/README.md
        argv += [f"{plan.server}"]
        argv += ["-p", str(probe_period)]  # (kept above; harmless duplicate-safe)
    # broadcast: omit <server> entirely (§3.1).
    # De-dupe a possible duplicate -p that the relay branch could append:
    out: list[str] = []
    seen_p = False
    i = 0
    while i < len(argv):
        if argv[i] == "-p":
            if seen_p:
                i += 2
                continue
            seen_p = True
            out += [argv[i], argv[i + 1]]
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


class PerfdhcpChild:
    """One running perfdhcp subprocess + a stdout-reader thread that folds stat lines
    into NDJSON + the generator stat file."""

    def __init__(self, argv: list[str], *, giaddr: str, offered_rate: float,
                 shard: int, ndjson_path, stat_path, logger):
        self.argv = argv
        self.giaddr = giaddr
        self.offered_rate = offered_rate
        self.shard = shard
        self.ndjson_path = ndjson_path
        self.stat_path = stat_path
        self.logger = logger
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._snap = StatSnapshot(offered_rate=offered_rate)

    def start(self) -> None:
        log_event(self.logger, 20, "perfdhcp_child_start", giaddr=self.giaddr or "broadcast",
                  offered_rate=self.offered_rate, argv=" ".join(self.argv))
        self.proc = subprocess.Popen(
            self.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.rstrip("\n")
            if not line:
                continue
            is_tick = parse_perfdhcp_line(line, self._snap)
            if is_tick:
                rec = self._snap.to_record(shard=self.shard)
                rec["giaddr"] = self.giaddr or "broadcast"
                # rp.generator stat file: one NDJSON stream the war-room/report read.
                append_ndjson(self.stat_path, dict(rec))
                # The canonical generator NDJSON (contract output) — same schema with
                # the §3.5 field names spelled out.
                append_ndjson(self.ndjson_path, {
                    "shard": self.shard,
                    "giaddr": self.giaddr or "broadcast",
                    "offered_rate": rec["offered_rate"],
                    "achieved_rate": rec["achieved_rate"],
                    "ack_p50_ms": rec["ack_p50_ms"],
                    "ack_p95_ms": rec["ack_p95_ms"],
                    "ack_p99_ms": rec["ack_p99_ms"],
                    "declines": rec["declines"],
                    "timeouts": rec["timeouts"],
                    "retransmits": rec["retransmits"],
                })

    def stop(self, *, grace: float = 5.0) -> None:
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)  # perfdhcp prints its final summary
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        # Flush a final record (final summary lines carry percentiles).
        if self._reader:
            self._reader.join(timeout=2.0)
        final = self._snap.to_record(shard=self.shard)
        final["giaddr"] = self.giaddr or "broadcast"
        final["final"] = True
        append_ndjson(self.stat_path, dict(final))

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)


# ── the worker loop ───────────────────────────────────────────────────────────────


class Stopper:
    """SIGTERM/SIGINT-aware stop flag."""

    def __init__(self) -> None:
        self.stop = False
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, *_a) -> None:
        self.stop = True


def _sharded_rate(sp: setpoints_mod.Setpoint, shards: int, n_children: int) -> float:
    """This shard's share of the offered DORA rate, further split across its children.

    Each perfdhcp child offers ``new_dora_per_s / shards / n_children``. Renewals are
    the orchestrator's job (self-scheduled at 900s, §3.2); perfdhcp's renew-only mode
    uses an explicit ``--probe-rate`` instead, not the diurnal renew setpoint.
    """
    per_shard = sp.new_dora_per_s / max(1, shards)
    return per_shard / max(1, n_children)


def run(args: argparse.Namespace) -> int:
    rp = RunPaths.for_run(args.run_id, args.run_root)
    logger = get_logger(SERVICE, service=SERVICE, run_id=args.run_id,
                        logfile=str(rp.worker_log(f"perfdhcp.shard{args.shard}")))
    m = manifest_mod.load(args.manifest)

    if not (shutil.which(PERFDHCP_BIN) or os.path.exists(PERFDHCP_BIN)):
        log_event(logger, 40, "perfdhcp_missing",
                  detail=("/usr/sbin/perfdhcp not found — install the Debian `kea` "
                          "package on this load-gen box (§3.1). perfdhcp is NOT in "
                          "the appliance Alpine image."),
                  bin=PERFDHCP_BIN)
        return 2

    template_dir = args.template_dir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "relay_templates")
    plan = resolve_plan(
        m, shard=args.shard, shards=args.shards, clients=args.clients,
        report_interval=args.report_interval, template_dir=template_dir)

    log_event(logger, 20, "perfdhcp_plan", topology=plan.topology, server=plan.server,
              port=plan.port, giaddrs=plan.giaddrs, clients=plan.clients,
              mac_base=plan.mac_base, mode=args.mode, shard=args.shard, shards=args.shards)

    if args.mode == "renew" and args.clients > 50_000:
        log_event(logger, 30, "cardinality_uncalibrated",
                  detail=("perfdhcp -R cardinality MUST be smoke-calibrated before the "
                          "300k orchestrator run (§1.9 gate) — high -R degrades "
                          "perfdhcp's per-client state stability."),
                  clients=args.clients)

    stat_path = rp.generator(f"perfdhcp.shard{args.shard}.stat")
    ndjson_path = rp.generator(f"perfdhcp.shard{args.shard}.ndjson")
    stopper = Stopper()

    children: list[PerfdhcpChild] = []
    last_offered: float | None = None
    last_seen_tick: int | None = None
    stale_count = 0
    probe_period = args.retune_interval  # each child runs one retune window then restarts

    def stop_all() -> None:
        for c in children:
            c.stop()
        children.clear()

    def start_for_rate(per_child_rate: float) -> None:
        stop_all()
        if per_child_rate <= 0:
            log_event(logger, 20, "offered_off", reason="rate<=0")
            return
        for g in plan.giaddrs:
            argv = build_perfdhcp_argv(
                plan, giaddr=g, offered_rate=per_child_rate, mode=args.mode,
                probe_period=probe_period, renew_rate=args.probe_rate)
            child = PerfdhcpChild(
                argv, giaddr=g, offered_rate=per_child_rate, shard=args.shard,
                ndjson_path=ndjson_path, stat_path=stat_path, logger=logger)
            child.start()
            children.append(child)

    try:
        while not stopper.stop:
            # Kill-switch (contract §4).
            if rp.stop_file.exists():
                log_event(logger, 20, "kill_switch", file=str(rp.stop_file))
                break

            sp = setpoints_mod.read_current(rp)
            if sp is None:
                # No setpoint yet — stay OFF, wait for the controller's first publish.
                log_event(logger, 20, "no_setpoint_yet")
                time.sleep(2.0)
                continue

            # Stale-setpoint fail-safe: if the tick stops advancing, the controller is
            # gone → fail safe to OFF (never full-blast). (contract §4)
            if last_seen_tick is not None and sp.tick == last_seen_tick:
                stale_count += 1
                if stale_count > STALE_TICK_LIMIT:
                    log_event(logger, 40, "setpoint_stale_fail_off",
                              tick=sp.tick, stale_ticks=stale_count)
                    stop_all()
                    break
            else:
                stale_count = 0
            last_seen_tick = sp.tick

            if args.mode == "renew":
                # Renew-only: ignore the diurnal DORA setpoint; offer a fixed grab rate
                # then re-REQUEST. The offered DORA grab rate = --probe-rate; renew rate
                # = --probe-rate too (combined -r/-f). The setpoint is still read so the
                # stale-fail-off path stays armed.
                target_per_child = args.probe_rate / max(1, len(plan.giaddrs))
            else:
                target_per_child = _sharded_rate(sp, args.shards, len(plan.giaddrs))
                # Manifest hard cap already applied in sp.clamp() by the controller, but
                # belt-and-braces: never exceed the per-shard slice of max_dora_per_s.
                cap = m.guardrails.max_dora_per_s / max(1, args.shards) / max(1, len(plan.giaddrs))
                target_per_child = min(target_per_child, cap)

            # Retune only on a meaningful change OR if a child died (restart it).
            need_restart = (
                last_offered is None
                or any(not c.alive() for c in children)
                or (last_offered > 0 and abs(target_per_child - last_offered)
                    / last_offered > RETUNE_REL_THRESHOLD)
                or (last_offered == 0 and target_per_child > 0)
                or (last_offered > 0 and target_per_child <= 0)
            )
            if need_restart:
                log_event(logger, 20, "retune", from_rate=last_offered,
                          to_rate=round(target_per_child, 3), tick=sp.tick,
                          children=len(plan.giaddrs))
                start_for_rate(target_per_child)
                last_offered = target_per_child

            time.sleep(min(5.0, max(1.0, args.report_interval)))
    finally:
        stop_all()
        log_event(logger, 20, "perfdhcp_shard_exit", shard=args.shard)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="perfdhcp shard — raw Kea v4 DORA/renew ceiling probe (§3.0/§3.1).")
    # Foundation CLI contract (workers.py).
    p.add_argument("--run-id", required=True, help="run id")
    p.add_argument("--run-root", required=True, help="run root dir (perf/run)")
    p.add_argument("--manifest", required=True, help="path to the run manifest YAML")
    # Sharding (workers.py passes --shard/--shards for sharded generators).
    p.add_argument("--shard", type=int, default=0, help="this shard index (0-based)")
    p.add_argument("--shards", type=int, default=1, help="total shard count")
    # perfdhcp tuning.
    p.add_argument("--mode", choices=["dora", "renew"], default="dora",
                   help="dora = full 4-way ceiling; renew = grab-then-re-REQUEST (§3.1)")
    p.add_argument("--clients", type=int, default=50_000,
                   help="perfdhcp -R simulated clients per child (§1.9 smoke-calibrate)")
    p.add_argument("--report-interval", type=int, default=1,
                   help="perfdhcp -t periodic stats interval (s)")
    p.add_argument("--retune-interval", type=int, default=60,
                   help="bounded perfdhcp -p probe window per retune (s)")
    p.add_argument("--probe-rate", type=float, default=2000.0,
                   help="renew-only mode offered/renew rate per child (DORA setpoint "
                        "ignored in renew mode)")
    p.add_argument("--template-dir", default=None,
                   help="dir of giaddr-<ip>.hex relay templates (default: ./relay_templates)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
