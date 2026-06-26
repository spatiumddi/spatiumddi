#!/usr/bin/env python3
"""The diurnal device-fleet orchestrator — the realistic 24h load (docs §3.2/§3.4/§4.5).

A single asyncio worker process modeling a disjoint shard of the 250-300k device
population as independent state machines driven by the §1 diurnal curve, generating
**real DHCPv4 packets** to Kea (so the full agent -> control-plane -> IPAM -> DDNS ->
DB path is exercised as production would), plus each ONLINE device's **DNS query
stream** (dnspython async, Poisson draws from a Zipfian name set), plus the
**propagation-lag probe** on a 1-in-1000 sampled arrival (lease -> IPAM mirror ->
A/PTR resolves, single-clock).

Per-device FSM (§3.2):

    OFFLINE -> DISCOVERING -(DORA)-> ONLINE -> RENEWING -ACK-> ONLINE
                                       |  (T1 fails) -> REBINDING -ACK-> ONLINE
                                       |  departure -> DEPARTING -> LEFT
    LEFT -> (re-arrival) -> DISCOVERING ...

HARD FSM CONTRACTS (the report turns these into named correctness FAILs):
  * T1 = 900s: RENEWING re-REQUESTs the CURRENT lease/IP (NOT a fresh DISCOVER); a
    renewal that lands on a different IP is a correctness FAIL (§3.2 / H3).
  * ~5% of departures send an explicit DHCPRELEASE; 95% leave silently (reaped by the
    server-side sweep). (§3.2)
  * DDNS short-circuit: renewals must NOT re-publish DNS — the short-circuit ratio on
    renewals must be ~0 (§3.4 #2 / §4.6); we only DDNS-couple on first DORA.

Scaling note (open_item): a single process is CPU-bound at the surge peak. This is a
correct, runnable, **shardable** first-cut — run K shards (K ~ vCPU) over a disjoint
MAC index range via ``--shard N --shards K`` (and disjoint from perfdhcp's range, by
convention perfdhcp owns the top of the index space). Multi-box if one box can't
sustain the peak. The device population is modeled as state structs on a timer-wheel
scheduler (NOT one coroutine per device) so a shard holds ~tens of thousands of
devices without 150k live coroutines.

CLI contract (workers.py REGISTRY -> orchestrator):
    device_fleet.py --run-id <id> --run-root <path> --manifest <path> [--shard N --shards K]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import signal
import socket
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

# Dual-mode imports: the controller launches this as a bare script (no parent
# package), but it's also importable as ``generators.orchestrator.device_fleet``.
# Put our own dir on sys.path so the sibling modules resolve either way.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spddi_perf.manifest as manifest_mod  # noqa: E402
import spddi_perf.setpoints as setpoints_mod  # noqa: E402
from spddi_perf import canonical, fleet  # noqa: E402
from spddi_perf.logging_util import append_ndjson, get_logger, read_json, utc_now_iso  # noqa: E402
from spddi_perf.runpaths import RunPaths  # noqa: E402

import dhcp_packet as dp  # noqa: E402
from lifecycle_log import LatencyAccumulator, LifecycleLog  # noqa: E402

# dnspython is an off-box runtime dep (perf/requirements.txt: dnspython>=2.6); resolve
# it once at import so the per-query hot path doesn't re-run the import machinery. When
# absent (bare env) the DNS query stream + the IPAM->DNS propagation leg no-op cleanly.
try:
    import dns.asyncquery as _dns_aq  # type: ignore
    import dns.message as _dns_msg  # type: ignore
    import dns.rcode as _dns_rcode  # type: ignore

    _HAVE_DNSPYTHON = True
except Exception:  # pragma: no cover - exercised only in a bare env
    _dns_aq = _dns_msg = _dns_rcode = None  # type: ignore
    _HAVE_DNSPYTHON = False

SERVICE = "orchestrator"

# --- timings (verified §0.A) ---
T1_RENEW_S = canonical.T1_RENEW_S      # 900
T2_REBIND_S = canonical.T2_REBIND_S    # 1800
DORA_TIMEOUT_S = 4.0                    # OFFER/ACK wait before retransmit/timeout
RENEW_TIMEOUT_S = 4.0
MAX_DORA_RETRIES = 3
RELEASE_FRACTION = 0.05                 # §3.2: ~5% explicit RELEASE, 95% silent
PROPAGATION_SAMPLE = 1000              # 1-in-1000 arrivals get the propagation probe
SCHED_TICK_S = 0.05                     # timer-wheel resolution
STATS_INTERVAL_S = 10.0                # per-shard NDJSON cadence (§3.5)
STALE_TICKS = 3                         # setpoint staleness fail-safe (3 missed ticks)
SETPOINT_TICK_S = 60.0                 # controller publishes on a 60s tick
DNS_QPS_ACTIVE = 1.0                    # §1.7 per active-online device
ZIPF_S = 1.0                            # §1.7 Zipfian popularity exponent


class DState(Enum):
    OFFLINE = "OFFLINE"
    DISCOVERING = "DISCOVERING"
    ONLINE = "ONLINE"
    RENEWING = "RENEWING"
    REBINDING = "REBINDING"
    DEPARTING = "DEPARTING"
    LEFT = "LEFT"


@dataclass
class Device:
    index: int
    mac: str
    client_id_bytes: bytes
    hostname: str | None
    subnet_idx: int
    state: DState = DState.OFFLINE
    xid: int = 0
    leased_ip: str | None = None
    server_id: str | None = None
    lease_time: int = 7200
    # pre-built byte templates (built lazily on first need)
    discover_tpl: bytes | None = None
    # timing for latency attribution
    tx_at: float = 0.0
    dora_retries: int = 0
    # propagation-probe membership
    probe: bool = False


@dataclass
class SubnetInfo:
    idx: int
    cidr: str
    network: int          # network address as int
    pool_first: int       # first usable pool IP as int
    pool_last: int        # last usable pool IP as int
    giaddr: str | None    # relay giaddr (None in broadcast topology)


@dataclass
class Counters:
    dora_sent: int = 0
    dora_ack: int = 0
    foreign_ack: int = 0   # ACKs whose IP is outside every seeded subnet (perf #454 — wrong DHCP server answered)
    nak: int = 0
    timeout: int = 0
    decline: int = 0
    renew_sent: int = 0
    renew_ack: int = 0
    rebind_sent: int = 0
    rebind_ack: int = 0
    departures: int = 0
    releases: int = 0
    lapses: int = 0
    rearrivals: int = 0
    dns_sent: int = 0
    dns_ok: int = 0
    dns_timeout: int = 0
    # DDNS short-circuit accounting: writes on first DORA vs (incorrect) writes on renew
    ddns_first_publish: int = 0
    ddns_renew_writes: int = 0   # MUST stay 0 (H3) — incremented only if a renewal IP-changes
    renew_ip_changed: int = 0    # named correctness FAIL signal


def _derive_subnets(m: manifest_mod.Manifest, rp: RunPaths, log: Any) -> list[SubnetInfo]:
    """Resolve the seeded subnets — prefer seed-manifest.json, else derive from manifest.

    The seeder records authoritative CIDRs/pool ranges in ``rp.seed_manifest``. When
    it hasn't run yet (smoke / dry build) we derive the same deterministic layout from
    ``seed.ip_block`` + ``seed.subnets`` so the orchestrator is self-contained.
    """
    import ipaddress

    seed = read_json(rp.seed_manifest) or {}
    seeded = seed.get("subnets") or []
    giaddrs = list(m.target.dhcp.giaddr or [])
    relay = m.target.dhcp.topology == "relay"
    out: list[SubnetInfo] = []

    if seeded:
        for i, s in enumerate(seeded):
            cidr = s.get("cidr") or s.get("network")
            net = ipaddress.ip_network(cidr, strict=False)
            first = s.get("pool_first")
            last = s.get("pool_last")
            pf = int(ipaddress.ip_address(first)) if first else int(net.network_address) + 1
            pl = int(ipaddress.ip_address(last)) if last else int(net.broadcast_address) - 1
            out.append(SubnetInfo(
                idx=i, cidr=str(net), network=int(net.network_address),
                pool_first=pf, pool_last=pl,
                giaddr=(giaddrs[i] if relay and i < len(giaddrs) else None),
            ))
        log.info("derived %d subnets from seed-manifest", len(out),
                 extra={"fields": {"event": "subnets_from_seed", "count": len(out)}})
        return out

    # Fallback: carve seed.subnets.count subnets of /prefix from the block.
    block = ipaddress.ip_network(m.seed.ip_block, strict=False)
    count = int(m.seed.subnets.get("count", 8))
    prefix = int(m.seed.subnets.get("prefix", 16))
    pool_frac = float(m.seed.subnets.get("pool_fraction", 0.90))
    subs = list(block.subnets(new_prefix=prefix))[:count]
    for i, net in enumerate(subs):
        usable = max(1, net.num_addresses - 2)
        pool_size = int(usable * pool_frac)
        pf = int(net.network_address) + 1
        pl = pf + pool_size - 1
        out.append(SubnetInfo(
            idx=i, cidr=str(net), network=int(net.network_address),
            pool_first=pf, pool_last=pl,
            giaddr=(giaddrs[i] if relay and i < len(giaddrs) else None),
        ))
    log.info("derived %d subnets from manifest block %s", len(out), m.seed.ip_block,
             extra={"fields": {"event": "subnets_from_manifest", "count": len(out)}})
    return out


class _ZipfNames:
    """Pre-built Zipfian name set per subnet (§1.7: top ~1% absorbs ~50% of queries).

    Names are deterministic from the device fleet so >=95% hit real records:
    forward FQDNs for hostname-bearing device indices + a small NXDOMAIN slice of
    random labels UNDER a seeded zone (authoritative NXDOMAIN, never REFUSED — H4).
    """

    def __init__(self, m: manifest_mod.Manifest, indices: list[int]) -> None:
        zone = (m.seed.dns.forward_zones or ["campus.example.edu"])[0]
        self.zone = zone.rstrip(".")
        self.nxdomain_frac = setpoints_mod.DEFAULT_NXDOMAIN_FRAC
        # Forward names for the hostname-bearing fraction (DDNS-published) + a few
        # always-present seeded service names for SRV/MX coverage.
        names = [fleet.forward_fqdn(fleet.client_hostname(i), self.zone) for i in indices]
        names += [f"_ldap._tcp.{self.zone}", f"_kerberos._udp.{self.zone}",
                  f"mail.{self.zone}", self.zone]
        self.names = names or [self.zone]
        n = len(self.names)
        # Zipf weights (rank^-s) precomputed into a cumulative table for fast draw.
        weights = [1.0 / ((r + 1) ** ZIPF_S) for r in range(n)]
        total = sum(weights)
        cum, acc = [], 0.0
        for w in weights:
            acc += w / total
            cum.append(acc)
        self._cum = cum

    def draw(self, rng: random.Random) -> tuple[str, str, bool]:
        """Return (qname, qtype, expect_nxdomain)."""
        if rng.random() < self.nxdomain_frac:
            # deliberate-miss: random label UNDER the seeded zone (authoritative NXDOMAIN)
            return (f"nx-{rng.randrange(1 << 30)}.{self.zone}", "A", True)
        r = rng.random()
        lo, hi = 0, len(self._cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self._cum[mid] < r:
                lo = mid + 1
            else:
                hi = mid
        name = self.names[lo]
        # qtype mix approximating §1.7 (A-dominant, some PTR/AAAA/SRV).
        qroll = rng.random()
        if name.startswith("_"):
            return (name, "SRV", False)
        if qroll < 0.65:
            return (name, "A", False)
        if qroll < 0.80:
            return (name, "AAAA", False)
        return (name, "A", False)


class Orchestrator:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rp = RunPaths.for_run(args.run_id, args.run_root)
        self.m = manifest_mod.load(args.manifest)
        self.shard = int(args.shard)
        self.shards = int(args.shards)
        self.log = get_logger(
            f"{SERVICE}.shard{self.shard}", service=SERVICE, run_id=args.run_id,
            logfile=str(self.rp.worker_log(f"orchestrator.shard{self.shard}")),
        )
        self.stats_path = self.rp.generator(f"orchestrator.shard{self.shard}.ndjson")
        self.lifecycle = LifecycleLog(self.rp, self.shard)
        self.rng = random.Random(0xC0FFEE ^ self.shard)

        self.node_ip = self.m.target.node_ip
        self.dhcp_port = self.m.target.dhcp.port
        self.relay = self.m.target.dhcp.topology == "relay"

        self.subnets = _derive_subnets(self.m, self.rp, self.log)
        self.n_subnets = len(self.subnets)
        # perf #454 — foreign-responder guard. On a shared LAN the site's
        # production DHCP server can win the broadcast race and ACK from a
        # different subnet; a lease outside every seeded subnet means we're
        # measuring the wrong server. Precompute the seeded networks once.
        import ipaddress as _ipa  # noqa: PLC0415
        self._seeded_nets = [_ipa.ip_network(s.cidr, strict=False) for s in self.subnets]
        self._foreign_warned = False

        # Disjoint device index range for this shard (sharded over unique_devices).
        self.indices = list(fleet.shard_indices(self.m.scale.unique_devices, self.shard, self.shards))
        self.hostname_fraction = self.m.scale.hostname_fraction

        # Latency accumulators (DORA + renew SEPARATELY, DNS resolve, both prop legs).
        self.lat_dora = LatencyAccumulator("dhcp_dora_ack")
        self.lat_renew = LatencyAccumulator("dhcp_renew_ack")
        self.lat_dns = LatencyAccumulator("dns_resolve")
        self.lat_prop_ipam = LatencyAccumulator("propagation_lease_to_ipam")
        self.lat_prop_dns = LatencyAccumulator("propagation_ipam_to_dns")

        self.counters = Counters()
        self.devices: dict[int, Device] = {}
        self.online_set: set[int] = set()
        # pending DHCP exchanges keyed by xid -> device index (recv correlation)
        self.pending: dict[int, int] = {}
        # timer wheel: due_time -> list[(index, action)]
        self._timers: list[tuple[float, int, str]] = []  # min-heap of (when, index, action)

        self._stop = asyncio.Event()
        self._sock: socket.socket | None = None
        self._last_seen_tick = -1
        self._tick_seen_at = time.monotonic()
        self._arrival_accum = 0.0
        self._dns_accum = 0.0
        self._zipf: dict[int, _ZipfNames] = {}
        self._sched_lag_max = 0.0
        self._probe_counter = 0

        # Build identity for our shard's devices (lazy template build on demand).
        for idx in self.indices:
            mac = fleet.device_mac(idx)
            cid = bytes.fromhex(fleet.client_id_for_mac(mac).replace(":", ""))
            has_host = (idx % 100) < int(round(self.hostname_fraction * 100))
            host = fleet.client_hostname(idx) if has_host else None
            self.devices[idx] = Device(
                index=idx, mac=mac, client_id_bytes=cid, hostname=host,
                subnet_idx=fleet.assign_subnet(idx, self.n_subnets),
            )
        # Per-subnet Zipf name sets from the hostname-bearing indices in that subnet.
        for s in self.subnets:
            hidx = [d.index for d in self.devices.values()
                    if d.subnet_idx == s.idx and d.hostname]
            self._zipf[s.idx] = _ZipfNames(self.m, hidx)

    # ---------------- socket ----------------
    def _open_socket(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Multi-homed egress (perf #454). In broadcast topology the DISCOVER goes
        # to 255.255.255.255; on a box with several NICs (docker bridges, tailscale,
        # …) the kernel won't necessarily send it out the one facing the appliance.
        # Bind the socket to the configured device so it does. SO_BINDTODEVICE needs
        # CAP_NET_RAW (the load-gen already runs as root for the :67/:68 bind).
        iface = getattr(self.m.target.dhcp, "iface", "") or ""
        if iface:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
                self.log.info("dhcp socket bound to device", extra={"fields": {
                    "event": "socket_bindtodevice", "iface": iface}})
            except OSError as exc:
                self.log.error("SO_BINDTODEVICE(%s) failed: %s — broadcast may not "
                               "reach the appliance on a multi-homed box", iface, exc,
                               extra={"fields": {"event": "bindtodevice_failed", "iface": iface}})
        # Relay topology: bind to the relay/server port (67) so Kea unicasts replies
        # to giaddr:67 back to us. Broadcast topology: bind to the client port (68).
        bind_port = 67 if self.relay else 68
        # 0.0.0.0 is required, not a misconfiguration: a simulated client has no
        # lease yet, so it must listen on the wildcard address to receive the
        # broadcast OFFER/ACK. SO_BINDTODEVICE above already pins the socket to the
        # appliance-facing NIC on multi-homed hosts, so this is not "all interfaces".
        try:
            s.bind(("0.0.0.0", bind_port))
        except PermissionError:
            self.log.error(
                "binding UDP/%d needs CAP_NET_BIND_SERVICE (run the load-gen "
                "as root or grant the cap)", bind_port,
                extra={"fields": {"event": "bind_denied", "port": bind_port}},
            )
            raise
        s.setblocking(False)
        self._sock = s
        self.log.info("dhcp socket bound", extra={"fields": {
            "event": "socket_open", "bind_port": bind_port, "topology": self.m.target.dhcp.topology}})

    # ---------------- timer wheel ----------------
    def _schedule(self, delay: float, index: int, action: str) -> None:
        import heapq
        heapq.heappush(self._timers, (time.monotonic() + delay, index, action))

    def _due(self, now: float) -> list[tuple[int, str]]:
        import heapq
        out = []
        while self._timers and self._timers[0][0] <= now:
            when, idx, action = heapq.heappop(self._timers)
            lag = now - when
            if lag > self._sched_lag_max:
                self._sched_lag_max = lag
            out.append((idx, action))
        return out

    # ---------------- DHCP send paths ----------------
    def _new_xid(self, dev: Device) -> int:
        # xid encodes the device index in the low 24 bits + a per-attempt nonce in the
        # high byte → cheap recv correlation while staying unique across re-sends.
        nonce = self.rng.randrange(256)
        return ((nonce & 0xFF) << 24) | (dev.index & 0xFFFFFF)

    def _send(self, pkt: bytes, dev: Device) -> None:
        if self._sock is None:
            return
        if self.relay:
            # Unicast to Kea; subnet selection is by giaddr (kea.py:237-241).
            dest = (self.node_ip, self.dhcp_port)
        else:
            # Broadcast on the local L2 segment (kea.py interfaces ["*"]).
            dest = ("255.255.255.255", self.dhcp_port)
        try:
            self._sock.sendto(pkt, dest)
        except OSError as exc:
            self.log.debug("send failed: %s", exc,
                           extra={"fields": {"event": "send_error", "index": dev.index}})

    def _send_discover(self, dev: Device) -> None:
        if dev.discover_tpl is None:
            dev.discover_tpl = dp.build_discover(
                mac=dev.mac, client_id=dev.client_id_bytes,
                hostname=dev.hostname, broadcast=not self.relay,
            )
        pkt = bytearray(dev.discover_tpl)
        dev.xid = self._new_xid(dev)
        giaddr = self.subnets[dev.subnet_idx].giaddr if self.relay else None
        dp.patch_send_fields(pkt, xid=dev.xid, giaddr=giaddr)
        dev.state = DState.DISCOVERING
        dev.tx_at = time.monotonic()
        self.pending[dev.xid] = dev.index
        self._send(bytes(pkt), dev)
        self.counters.dora_sent += 1
        self._schedule(DORA_TIMEOUT_S, dev.index, "dora_timeout")

    def _send_request_renew(self, dev: Device) -> None:
        assert dev.leased_ip
        pkt = bytearray(dp.build_request_renew(
            mac=dev.mac, client_id=dev.client_id_bytes,
            hostname=dev.hostname, leased_ip=dev.leased_ip))
        dev.xid = self._new_xid(dev)
        dp.patch_send_fields(pkt, xid=dev.xid)  # NO giaddr — renew is unicast direct
        dev.state = DState.RENEWING
        dev.tx_at = time.monotonic()
        self.pending[dev.xid] = dev.index
        self._send(bytes(pkt), dev)
        self.counters.renew_sent += 1
        self._schedule(RENEW_TIMEOUT_S, dev.index, "renew_timeout")

    def _send_request_rebind(self, dev: Device) -> None:
        assert dev.leased_ip
        pkt = bytearray(dp.build_request_rebind(
            mac=dev.mac, client_id=dev.client_id_bytes,
            hostname=dev.hostname, leased_ip=dev.leased_ip))
        dev.xid = self._new_xid(dev)
        giaddr = self.subnets[dev.subnet_idx].giaddr if self.relay else None
        dp.patch_send_fields(pkt, xid=dev.xid, giaddr=giaddr)
        dev.state = DState.REBINDING
        dev.tx_at = time.monotonic()
        self.pending[dev.xid] = dev.index
        self._send(bytes(pkt), dev)
        self.counters.rebind_sent += 1
        self._schedule(RENEW_TIMEOUT_S, dev.index, "rebind_timeout")

    def _send_request_selecting(self, dev: Device, offered_ip: str, server_id: str) -> None:
        pkt = bytearray(dp.build_request_selecting(
            mac=dev.mac, client_id=dev.client_id_bytes, hostname=dev.hostname,
            requested_ip=offered_ip, server_id=server_id, broadcast=not self.relay))
        dev.xid = self._new_xid(dev)
        giaddr = self.subnets[dev.subnet_idx].giaddr if self.relay else None
        dp.patch_send_fields(pkt, xid=dev.xid, giaddr=giaddr)
        dev.tx_at = time.monotonic()  # keep DORA clock running through SELECTING
        self.pending[dev.xid] = dev.index
        self._send(bytes(pkt), dev)
        self._schedule(DORA_TIMEOUT_S, dev.index, "dora_timeout")

    def _send_release(self, dev: Device) -> None:
        if not (dev.leased_ip and dev.server_id):
            return
        pkt = bytearray(dp.build_release(
            mac=dev.mac, client_id=dev.client_id_bytes,
            leased_ip=dev.leased_ip, server_id=dev.server_id))
        dev.xid = self._new_xid(dev)
        dp.patch_send_fields(pkt, xid=dev.xid)
        self._send(bytes(pkt), dev)
        self.counters.releases += 1

    # ---------------- FSM event handlers ----------------
    def _on_offer(self, dev: Device, reply: dict) -> None:
        # SELECTING: accept the OFFER with a REQUEST(opt-50/opt-54).
        offered = reply.get("yiaddr")
        sid = reply.get("server_id") or offered
        if not offered or offered == "0.0.0.0":
            return
        self._send_request_selecting(dev, offered, sid)

    def _ip_in_seeded_subnet(self, ip: str) -> bool:
        """True if ``ip`` falls within any seeded subnet (perf #454 foreign-responder guard)."""
        import ipaddress as _ipa  # noqa: PLC0415
        try:
            addr = _ipa.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self._seeded_nets)

    def _on_ack(self, dev: Device, reply: dict, now: float) -> None:
        prev_state = dev.state
        new_ip = reply.get("yiaddr")
        latency_ms = (now - dev.tx_at) * 1000.0
        if prev_state in (DState.DISCOVERING,):
            # perf #454 — foreign-responder guard. If the leased IP is outside
            # every seeded subnet, a DIFFERENT DHCP server answered (e.g. the
            # site router on a shared LAN) — we're not testing the appliance's
            # Kea. Count it and warn loudly (once) so the run isn't silently
            # measuring the wrong server. Use an isolated VLAN or relay topology.
            if new_ip and self._seeded_nets and not self._ip_in_seeded_subnet(new_ip):
                self.counters.foreign_ack += 1
                if not self._foreign_warned:
                    self._foreign_warned = True
                    self.log.error(
                        "DHCP ACK from a FOREIGN server — leased IP %s is outside "
                        "every seeded subnet. A non-appliance DHCP server is winning "
                        "the broadcast race; use an isolated test VLAN or relay "
                        "topology (perf #454).", new_ip,
                        extra={"fields": {"event": "foreign_dhcp_responder",
                                          "leased_ip": new_ip, "server_id": reply.get("server_id")}})
            # DORA ACK — first lease (or re-lease after re-arrival).
            dev.leased_ip = new_ip
            dev.server_id = reply.get("server_id") or dev.server_id
            dev.lease_time = int(reply.get("lease_time", self.m.scale.lease_time_s))
            dev.state = DState.ONLINE
            self.online_set.add(dev.index)
            self.counters.dora_ack += 1
            self.lat_dora.record_ms(latency_ms)
            dev.dora_retries = 0
            # DDNS coupling happens ONLY on first DORA (hostname-bearing devices).
            if dev.hostname:
                self.counters.ddns_first_publish += 1
            self.lifecycle.emit(mac=dev.mac, index=dev.index, event="dora_ack",
                                ip=new_ip, ack_ms=round(latency_ms, 2),
                                ddns=bool(dev.hostname))
            # Self-schedule the next renewal at T1=900s (renewals self-track online).
            self._schedule(T1_RENEW_S, dev.index, "t1_renew")
            # Propagation probe membership (1-in-1000 arrivals).
            if dev.probe:
                self._schedule(0.0, dev.index, "probe_start")
        elif prev_state in (DState.RENEWING, DState.REBINDING):
            # RENEW/REBIND ACK — HARD CONTRACT: same IP. A changed IP is a FAIL.
            if new_ip and dev.leased_ip and new_ip != dev.leased_ip:
                self.counters.renew_ip_changed += 1
                self.counters.ddns_renew_writes += 1  # would trigger the 6-write cascade
                self.log.error(
                    "RENEWAL LANDED ON A DIFFERENT IP — correctness FAIL (H3)",
                    extra={"fields": {"event": "renew_ip_changed", "index": dev.index,
                                      "old_ip": dev.leased_ip, "new_ip": new_ip}})
                self.lifecycle.emit(mac=dev.mac, index=dev.index, event="renew_ip_changed",
                                    old_ip=dev.leased_ip, new_ip=new_ip)
                dev.leased_ip = new_ip
            dev.state = DState.ONLINE
            if prev_state is DState.RENEWING:
                self.counters.renew_ack += 1
            else:
                self.counters.rebind_ack += 1
            self.lat_renew.record_ms(latency_ms)
            self.lifecycle.emit(mac=dev.mac, index=dev.index, event="renew_ack",
                                ip=dev.leased_ip, ack_ms=round(latency_ms, 2))
            self._schedule(T1_RENEW_S, dev.index, "t1_renew")
        self.pending.pop(dev.xid, None)

    def _on_nak(self, dev: Device) -> None:
        self.counters.nak += 1
        self.pending.pop(dev.xid, None)
        self.lifecycle.emit(mac=dev.mac, index=dev.index, event="nak")
        # NAK → fall back to a fresh DISCOVER (lease invalid).
        dev.leased_ip = None
        self.online_set.discard(dev.index)
        self._send_discover(dev)

    def _handle_timer(self, idx: int, action: str, now: float) -> None:
        dev = self.devices.get(idx)
        if dev is None:
            return
        if action == "arrival":
            if dev.state in (DState.OFFLINE, DState.LEFT):
                if dev.state is DState.LEFT:
                    self.counters.rearrivals += 1
                    self.lifecycle.emit(mac=dev.mac, index=dev.index, event="rearrival")
                else:
                    self.lifecycle.emit(mac=dev.mac, index=dev.index, event="arrival")
                # sample propagation-probe membership
                self._probe_counter += 1
                dev.probe = (self._probe_counter % PROPAGATION_SAMPLE) == 0
                self._send_discover(dev)
        elif action == "dora_timeout":
            if dev.state is DState.DISCOVERING:
                self.pending.pop(dev.xid, None)
                dev.dora_retries += 1
                if dev.dora_retries <= MAX_DORA_RETRIES:
                    self._send_discover(dev)
                else:
                    self.counters.timeout += 1
                    dev.state = DState.OFFLINE
                    dev.dora_retries = 0
                    self.lifecycle.emit(mac=dev.mac, index=dev.index, event="timeout")
        elif action == "t1_renew":
            if dev.state is DState.ONLINE and dev.leased_ip:
                self._send_request_renew(dev)
        elif action == "renew_timeout":
            if dev.state is DState.RENEWING:
                self.pending.pop(dev.xid, None)
                # T1 unanswered → escalate to REBINDING at T2.
                self._send_request_rebind(dev)
        elif action == "rebind_timeout":
            if dev.state is DState.REBINDING:
                self.pending.pop(dev.xid, None)
                self.counters.lapses += 1
                self.online_set.discard(dev.index)
                dev.state = DState.LEFT
                dev.leased_ip = None
                self.lifecycle.emit(mac=dev.mac, index=dev.index, event="lapse")
        elif action == "depart":
            if dev.state in (DState.ONLINE, DState.RENEWING, DState.REBINDING):
                self.counters.departures += 1
                if self.rng.random() < RELEASE_FRACTION:
                    self._send_release(dev)
                    self.lifecycle.emit(mac=dev.mac, index=dev.index, event="release",
                                        ip=dev.leased_ip)
                else:
                    self.lifecycle.emit(mac=dev.mac, index=dev.index, event="depart_silent",
                                        ip=dev.leased_ip)
                self.online_set.discard(dev.index)
                dev.state = DState.LEFT
                dev.leased_ip = None
        elif action == "probe_start":
            asyncio.ensure_future(self._run_propagation_probe(dev))

    # ---------------- receive loop ----------------
    async def _recv_loop(self) -> None:
        loop = asyncio.get_running_loop()
        assert self._sock
        while not self._stop.is_set():
            try:
                data = await loop.sock_recv(self._sock, 2048)
            except (BlockingIOError, InterruptedError):
                await asyncio.sleep(0.001)
                continue
            except OSError:
                if self._stop.is_set():
                    break
                await asyncio.sleep(0.01)
                continue
            reply = dp.parse_reply(data)
            if not reply:
                continue
            xid = reply.get("xid")
            idx = self.pending.get(xid)
            if idx is None:
                idx = xid & 0xFFFFFF if xid is not None else None  # recover from low bits
                if idx not in self.devices:
                    continue
            dev = self.devices[idx]
            mt = reply.get("msg_type")
            now = time.monotonic()
            if mt == dp.DHCPOFFER:
                if dev.state is DState.DISCOVERING:
                    self._on_offer(dev, reply)
            elif mt == dp.DHCPACK:
                self._on_ack(dev, reply, now)
            elif mt == dp.DHCPNAK:
                self._on_nak(dev)

    # ---------------- scheduler loop ----------------
    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            for idx, action in self._due(now):
                self._handle_timer(idx, action, now)
            await asyncio.sleep(SCHED_TICK_S)

    # ---------------- arrival / departure / dns drivers (setpoint-driven) ----------------
    async def _control_loop(self) -> None:
        """Reads the setpoint each tick; trues-up arrivals + departures + DNS rate."""
        last_pace = time.monotonic()
        while not self._stop.is_set():
            if self.rp.stop_file.exists():
                self.log.warning("kill-switch present — stopping",
                                 extra={"fields": {"event": "kill_switch"}})
                self._stop.set()
                break
            sp = setpoints_mod.read_current(self.rp)
            sp = self._check_stale(sp)
            now = time.monotonic()
            dt = now - last_pace
            last_pace = now
            if sp is not None and not self._failsafe:
                self._drive_arrivals(sp, dt)
                self._drive_departures(sp)
                self._drive_dns(sp, dt)
            await asyncio.sleep(0.25)

    _failsafe = False

    def _check_stale(self, sp: setpoints_mod.Setpoint | None):
        now = time.monotonic()
        if sp is None:
            return None
        if sp.tick != self._last_seen_tick:
            self._last_seen_tick = sp.tick
            self._tick_seen_at = now
            self._failsafe = False
        elif now - self._tick_seen_at > STALE_TICKS * SETPOINT_TICK_S:
            if not self._failsafe:
                self.log.error(
                    "setpoint stale for >%ds — failing safe to OFF (no new load)",
                    int(STALE_TICKS * SETPOINT_TICK_S),
                    extra={"fields": {"event": "setpoint_stale", "tick": sp.tick}})
            self._failsafe = True
        return sp

    def _shard_share(self, total: float) -> float:
        """This shard's slice of a fleet-wide rate (even split across shards)."""
        return total / max(1, self.shards)

    def _drive_arrivals(self, sp: setpoints_mod.Setpoint, dt: float) -> None:
        # Bring devices toward sp.active_devices (this shard's share) AND honor the
        # explicit new_dora_per_s arrival rate. Arrivals = max(deficit-fill, dora rate).
        target_online = int(round(self._shard_share(sp.active_devices)))
        cur_online = len(self.online_set)
        dora_rate = self._shard_share(sp.new_dora_per_s)
        self._arrival_accum += dora_rate * dt
        # also fill a concurrency deficit faster during ramp (bounded per pass)
        deficit = max(0, target_online - cur_online)
        to_arrive = int(self._arrival_accum) + min(deficit, 200)
        self._arrival_accum -= int(self._arrival_accum)
        if to_arrive <= 0:
            return
        offline = [d for d in self.devices.values()
                   if d.state in (DState.OFFLINE, DState.LEFT)]
        self.rng.shuffle(offline)
        for dev in offline[:to_arrive]:
            self._schedule(self.rng.random() * 0.5, dev.index, "arrival")

    def _drive_departures(self, sp: setpoints_mod.Setpoint) -> None:
        # If online exceeds target, depart the surplus (commuters leave).
        target_online = int(round(self._shard_share(sp.active_devices)))
        surplus = len(self.online_set) - target_online
        if surplus <= 0:
            return
        leaving = list(self.online_set)[: min(surplus, 200)]
        for idx in leaving:
            self._schedule(self.rng.random() * 1.0, idx, "depart")

    def _drive_dns(self, sp: setpoints_mod.Setpoint, dt: float) -> None:
        # Aggregate Poisson DNS stream across online devices: target qps = min(setpoint
        # share, per-device model). We dispatch floor(rate*dt) queries this pass.
        if not _HAVE_DNSPYTHON:
            return  # no DNS client available — DNS stream is owned by dnsperf instead
        online = len(self.online_set)
        if online == 0:
            return
        model_qps = online * DNS_QPS_ACTIVE
        setpoint_qps = self._shard_share(sp.dns_qps)
        # The orchestrator emits the realistic per-device stream; cap at the setpoint
        # share so dnsperf owns the raw-ceiling headroom above it (§4.7).
        qps = min(model_qps, setpoint_qps) if setpoint_qps > 0 else model_qps
        self._dns_accum += qps * dt
        n = int(self._dns_accum)
        self._dns_accum -= n
        for _ in range(min(n, 500)):  # bound per pass; surplus rolls into accum
            idx = self.rng.choice(tuple(self.online_set))
            asyncio.ensure_future(self._dns_query(self.devices[idx]))

    # ---------------- DNS query (dnspython async) ----------------
    async def _dns_query(self, dev: Device) -> None:
        if not _HAVE_DNSPYTHON:
            return
        z = self._zipf[dev.subnet_idx]
        qname, qtype, _expect_nx = z.draw(self.rng)
        try:
            q = _dns_msg.make_query(qname, qtype)
        except Exception:
            return
        t0 = time.monotonic()
        self.counters.dns_sent += 1
        try:
            resp = await _dns_aq.udp(
                q, self.node_ip, port=self.m.target.dns.port, timeout=2.0)
            latency_ms = (time.monotonic() - t0) * 1000.0
            self.lat_dns.record_ms(latency_ms)
            rc = resp.rcode()
            if rc in (_dns_rcode.NOERROR, _dns_rcode.NXDOMAIN):
                self.counters.dns_ok += 1
        except Exception:
            self.counters.dns_timeout += 1

    # ---------------- propagation-lag probe (single-clock) ----------------
    async def _run_propagation_probe(self, dev: Device) -> None:
        """On a sampled arrival: t_lease -> poll IPAM mirror -> dig A/PTR (§3.4 #3).

        Both legs are timestamped by the orchestrator (single-clock) so they're robust
        to cross-box skew. lease->IPAM = the auto_from_lease row appears; IPAM->DNS =
        the A resolves (DDNS landed). Budget ~5-12s; we give up at 30s and record a
        miss so a stuck pipeline doesn't hang the probe forever.
        """
        if not dev.leased_ip:
            return
        t_lease = time.monotonic()
        ip = dev.leased_ip
        subnet = self.subnets[dev.subnet_idx]
        # leg 1: lease -> IPAM mirror row (API poll).
        ipam_seen = await self._poll_ipam_mirror(subnet, ip, deadline_s=30.0)
        if ipam_seen is not None:
            self.lat_prop_ipam.record_ms((ipam_seen - t_lease) * 1000.0)
        # leg 2: IPAM -> DNS resolves (only for DDNS hostname-bearing devices).
        if dev.hostname:
            zone = (self.m.seed.dns.forward_zones or ["campus.example.edu"])[0]
            fqdn = fleet.forward_fqdn(dev.hostname, zone)
            dns_seen = await self._poll_dns_resolves(fqdn, ip, deadline_s=30.0)
            if dns_seen is not None:
                self.lat_prop_dns.record_ms((dns_seen - t_lease) * 1000.0)
        self.lifecycle.emit(mac=dev.mac, index=dev.index, event="propagation_probe",
                            ip=ip, ipam_ok=ipam_seen is not None)

    async def _poll_ipam_mirror(self, subnet: SubnetInfo, ip: str, deadline_s: float):
        """Poll GET /ipam/subnets/{id}/addresses for the auto_from_lease row.

        Grounded: list_addresses at backend/app/api/v1/ipam/router.py:5957 returns
        IPAddressResponse{address, auto_from_lease, ...} (router.py:2269). No
        lookup-by-IP-string endpoint exists, so we scan the subnet's address list — an
        open_item at 150k rows (see module docstring); the probe is 1-in-1000 so the
        cost is bounded. We need the subnet's UUID; the seed-manifest carries it.
        """
        subnet_uuid = self._subnet_uuid(subnet.idx)
        if subnet_uuid is None:
            return None
        token = os.environ.get(self.m.observability.superadmin_token_env)
        if not token:
            return None
        import httpx
        url = f"{self.m.target.api_base}/ipam/subnets/{subnet_uuid}/addresses"
        verify = os.environ.get("SPDDI_PERF_CA_BUNDLE", False)
        deadline = time.monotonic() + deadline_s
        async with httpx.AsyncClient(verify=verify, timeout=5.0,
                                     headers={"Authorization": f"Bearer {token}"}) as c:
            while time.monotonic() < deadline and not self._stop.is_set():
                try:
                    r = await c.get(url, params={"status_filter": "dhcp"})
                    if r.status_code == 200:
                        for row in r.json():
                            if row.get("address") == ip and row.get("auto_from_lease"):
                                return time.monotonic()
                    # status_filter may not match the mirror status; fall back to full list
                    r2 = await c.get(url)
                    if r2.status_code == 200:
                        for row in r2.json():
                            if row.get("address") == ip:
                                return time.monotonic()
                except httpx.HTTPError:
                    # Transient API/network errors are expected while the IPAM mirror
                    # propagates; fall through to the sleep below and retry until deadline.
                    pass
                await asyncio.sleep(0.5)
        return None

    async def _poll_dns_resolves(self, fqdn: str, ip: str, deadline_s: float):
        """dig the A record every 250ms until it answers ``ip`` (§4.6.3)."""
        if not _HAVE_DNSPYTHON:
            return None
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                q = _dns_msg.make_query(fqdn, "A")
                resp = await _dns_aq.udp(
                    q, self.node_ip, port=self.m.target.dns.port, timeout=1.0)
                for rrset in resp.answer:
                    for item in rrset:
                        if str(item) == ip:
                            return time.monotonic()
            except Exception:
                # Transient DNS errors (NXDOMAIN/timeout) are expected during
                # propagation; fall through to the sleep below and retry until deadline.
                pass
            await asyncio.sleep(0.25)
        return None

    def _subnet_uuid(self, idx: int) -> str | None:
        # seed_scaffold writes each subnet row as {"id","network","index"}
        # (seed_scaffold.py:356) — match those keys, not "idx"/"cidr".
        seed = read_json(self.rp.seed_manifest) or {}
        target_cidr = self.subnets[idx].cidr
        for s in seed.get("subnets", []):
            if s.get("index") == idx or (s.get("network") or s.get("cidr")) == target_cidr:
                return s.get("id") or s.get("subnet_id")
        return None

    # ---------------- per-shard stats emitter ----------------
    async def _stats_loop(self) -> None:
        last = dict(self._counter_snapshot())
        last_t = time.monotonic()
        while not self._stop.is_set():
            await asyncio.sleep(STATS_INTERVAL_S)
            now = time.monotonic()
            cur = self._counter_snapshot()
            dt = max(0.001, now - last_t)
            dora_s = (cur["dora_ack"] - last["dora_ack"]) / dt
            renew_s = (cur["renew_ack"] - last["renew_ack"]) / dt
            dns_s = (cur["dns_sent"] - last["dns_sent"]) / dt
            dora_p = self.lat_dora.window_percentiles()
            renew_p = self.lat_renew.window_percentiles()
            dns_p = self.lat_dns.window_percentiles()
            prop_ipam = self.lat_prop_ipam.window_percentiles()
            prop_dns = self.lat_prop_dns.window_percentiles()
            # DDNS short-circuit ratio on renewals: renew-driven DNS writes / renews.
            renew_total = max(1, cur["renew_ack"] + cur["rebind_ack"])
            ddns_short_ratio = round(cur["ddns_renew_writes"] / renew_total, 6)
            unique_macs = sum(1 for d in self.devices.values() if d.leased_ip is not None) \
                + cur["departures"] + cur["lapses"]  # approx distinct seen
            rec = {
                "ts": utc_now_iso(),
                "shard": self.shard,
                "online": len(self.online_set),
                "dora_s": round(dora_s, 3),
                "renew_s": round(renew_s, 3),
                "dns_s": round(dns_s, 3),
                "ack_dora_p50": dora_p["p50"], "ack_dora_p95": dora_p["p95"], "ack_dora_p99": dora_p["p99"],
                "ack_renew_p50": renew_p["p50"], "ack_renew_p95": renew_p["p95"], "ack_renew_p99": renew_p["p99"],
                "dns_p50": dns_p["p50"], "dns_p95": dns_p["p95"], "dns_p99": dns_p["p99"],
                "propagation_ipam_p50": prop_ipam["p50"], "propagation_ipam_p95": prop_ipam["p95"],
                "propagation_ipam_p99": prop_ipam["p99"],
                "propagation_dns_p50": prop_dns["p50"], "propagation_dns_p95": prop_dns["p95"],
                "propagation_dns_p99": prop_dns["p99"],
                "scheduler_lag": round(self._sched_lag_max, 4),
                "ddns_short_circuit_ratio": ddns_short_ratio,
                "renew_ip_changed": cur["renew_ip_changed"],
                "ddns_first_publish": cur["ddns_first_publish"],
                "unique_macs": unique_macs,
                "nak": cur["nak"], "timeout": cur["timeout"], "decline": cur["decline"],
                "departures": cur["departures"], "releases": cur["releases"],
                "lapses": cur["lapses"], "rearrivals": cur["rearrivals"],
                "dns_timeout": cur["dns_timeout"],
            }
            append_ndjson(self.stats_path, rec)
            self._sched_lag_max = 0.0  # reset window max
            last, last_t = cur, now

    def _counter_snapshot(self) -> dict[str, int]:
        c = self.counters
        return {k: getattr(c, k) for k in c.__dataclass_fields__}

    # ---------------- lifecycle ----------------
    async def run(self) -> None:
        if not self.node_ip:
            self.log.error("target.node_ip empty — nothing to send to")
            return
        self._open_socket()
        self.log.info(
            "orchestrator shard online",
            extra={"fields": {"event": "start", "shard": self.shard, "shards": self.shards,
                              "devices": len(self.indices), "subnets": self.n_subnets,
                              "topology": self.m.target.dhcp.topology}})
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                # add_signal_handler isn't available on every platform / loop;
                # the _stop event still drives shutdown via other paths.
                self.log.debug("signal handler unavailable for %s", sig)
        tasks = [
            asyncio.ensure_future(self._recv_loop()),
            asyncio.ensure_future(self._scheduler_loop()),
            asyncio.ensure_future(self._control_loop()),
            asyncio.ensure_future(self._stats_loop()),
        ]
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._finalize()

    def _finalize(self) -> None:
        # Dump cumulative HdrHistograms + a final summary.
        for acc in (self.lat_dora, self.lat_renew, self.lat_dns,
                    self.lat_prop_ipam, self.lat_prop_dns):
            acc.dump_hdr(str(self.rp.generator(f"orchestrator.shard{self.shard}.{acc.name}.hdr")))
        summary = {
            "ts": utc_now_iso(),
            "shard": self.shard,
            "counters": self._counter_snapshot(),
            "dora_ack": self.lat_dora.cumulative_summary(),
            "renew_ack": self.lat_renew.cumulative_summary(),
            "dns_resolve": self.lat_dns.cumulative_summary(),
            "propagation_lease_to_ipam": self.lat_prop_ipam.cumulative_summary(),
            "propagation_ipam_to_dns": self.lat_prop_dns.cumulative_summary(),
            "unique_macs_with_lease_or_seen": sum(
                1 for d in self.devices.values()
                if d.state is not DState.OFFLINE),
        }
        append_ndjson(self.rp.generator(f"orchestrator.shard{self.shard}.summary.ndjson"), summary)
        if self._sock:
            try:
                self._sock.close()
            except OSError as exc:
                # Best-effort close during teardown; a socket error here is non-fatal.
                self.log.debug("socket close failed during finalize: %s", exc)
        self.log.info("orchestrator shard stopped",
                      extra={"fields": {"event": "stop", "shard": self.shard,
                                        **summary["counters"]}})


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SpatiumDDI perf — diurnal device-fleet orchestrator (DHCP FSM + "
                    "DNS streams + propagation probe). Runs OFF-BOX.")
    p.add_argument("--run-id", required=True)
    p.add_argument("--run-root", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--shard", type=int, default=0, help="this shard index (0-based)")
    p.add_argument("--shards", type=int, default=1, help="total shard count (~vCPU)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    orch = Orchestrator(args)
    try:
        asyncio.run(orch.run())
    except KeyboardInterrupt:
        # Intentional: swallow Ctrl-C for a clean shutdown without a traceback.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
