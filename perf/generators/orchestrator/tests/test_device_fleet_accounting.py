"""The orchestrator's FSM wired to the exchange ledger (#1057), driven without a
socket: replies are injected as parsed dicts and timers fired by hand.

Needs PyYAML (the manifest) and dnspython (the DNS leg) — both installed by
the perf CI job; in a bare environment the file import-skips. The one
assertion that needs the HdrHistogram backend (the .hdr dump) is gated on
it: hdrhistogram ships a wheel for x86_64 only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
dns_exception = pytest.importorskip("dns.exception")
dns_rcode = pytest.importorskip("dns.rcode")

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                 # generators/orchestrator
sys.path.insert(0, str(HERE.parents[3] / "harness"))     # spddi_perf

import device_fleet as df  # noqa: E402
import lifecycle_log  # noqa: E402
from accounting import DORA_KINDS, KIND_RENEW, KIND_SELECT  # noqa: E402
from spddi_perf.runpaths import RunPaths  # noqa: E402

SMOKE = HERE.parents[3] / "manifests" / "smoke.yaml"


@pytest.fixture
def orch(tmp_path, monkeypatch):
    """A relay-topology orchestrator over 8 devices / 2 subnets, no socket."""
    monkeypatch.delenv("SPDDI_PERF_NODE_IP", raising=False)
    monkeypatch.delenv("SPDDI_PERF_API_BASE", raising=False)
    monkeypatch.delenv("SPDDI_PERF_DHCP_IFACE", raising=False)
    m = yaml.safe_load(SMOKE.read_text())
    m["target"]["node_ip"] = "192.0.2.10"
    m["target"]["api_base"] = "https://192.0.2.10/api"
    m["target"]["dhcp"] = {"port": 67, "topology": "relay",
                           "giaddr": ["10.9.0.1", "10.9.1.1"], "iface": ""}
    m["scale"]["unique_devices"] = 8
    m["scale"]["peak_active_devices"] = 8
    m["scale"]["students"] = 8
    m["seed"]["ip_block"] = "10.8.0.0/16"
    m["seed"]["subnets"] = {"count": 2, "prefix": 24, "pool_fraction": 0.9}
    m["seed"]["relay_addresses_per_scope"] = True
    mpath = tmp_path / "m.yaml"
    mpath.write_text(yaml.safe_dump(m))
    run_root = tmp_path / "run"
    RunPaths.for_run("t-run", run_root).ensure_dirs()
    o = df.Orchestrator(argparse.Namespace(
        run_id="t-run", run_root=str(run_root), manifest=str(mpath), shard=0, shards=1))
    o.sent = []
    o._send = lambda pkt, dev: o.sent.append((dev.index, bytes(pkt)))  # type: ignore[method-assign]
    # The send paths stamp tx_at with time.monotonic(); the tests hand every
    # handler an explicit `now`, so the clock the stamps read is the same one.
    o.clock = {"t": 0.0}
    monkeypatch.setattr(df.time, "monotonic", lambda: o.clock["t"])
    return o


def at(o, t: float) -> float:
    """Advance the orchestrator's clock to t and return it (for a handler's `now`)."""
    o.clock["t"] = t
    return t


def timers(o, prefix: str, idx: int) -> list[str]:
    return sorted(a for _w, i, a in o._timers if i == idx and a.startswith(prefix))


def ack(ip: str, xid: int) -> dict:
    return {"xid": xid, "yiaddr": ip, "server_id": "192.0.2.10", "lease_time": 1800,
            "msg_type": df.dp.DHCPACK}


def offer(ip: str, xid: int) -> dict:
    return {"xid": xid, "yiaddr": ip, "server_id": "192.0.2.10", "msg_type": df.dp.DHCPOFFER}


def dora_to_select(o, idx: int, ip: str, t: float) -> tuple[int, int]:
    """arrival → DISCOVER → OFFER → REQUEST(selecting); returns (discover xid, select xid)."""
    dev = o.devices[idx]
    o._handle_timer(idx, "arrival", at(o, t))
    xd = dev.xid
    assert dev.state is df.DState.DISCOVERING
    assert timers(o, "dora_timeout:", idx) == [f"dora_timeout:{xd}"]
    o._on_offer(dev, offer(ip, xd), o.ledger.get(xd), at(o, at(o, t + 0.02)))
    xs = dev.xid
    assert xs != xd and o.ledger.get(xs).kind == KIND_SELECT
    assert not o.ledger.get(xd).open and o.ledger.get(xd).closed_by == "offer"
    return xd, xs


def test_discover_deadline_does_not_retransmit_over_a_live_request(orch):
    """Pre-#1057 the DISCOVER's per-packet timer fired on whatever exchange was
    current and re-DISCOVERed under an in-flight REQUEST."""
    o = orch
    xd, xs = dora_to_select(o, 0, "10.8.0.5", 0.0)
    sent_before = len(o.sent)
    o._handle_timer(0, f"dora_timeout:{xd}", at(o, 4.0))      # the DISCOVER's own deadline
    assert len(o.sent) == sent_before and o.devices[0].xid == xs
    assert o.devices[0].dora_retries == 0 and o.counters.dora_sent == 1
    # the REQUEST's own deadline is what retries
    o._handle_timer(0, f"dora_timeout:{xs}", at(o, 4.1))
    assert o.counters.dora_sent == 2 and o.devices[0].dora_retries == 1
    assert o.devices[0].state is df.DState.DISCOVERING


def test_strict_ack_and_over_budget_ack_keep_dora_ack_meaning(orch):
    o = orch
    # strict: ACK within the REQUEST's budget
    _xd, xs = dora_to_select(o, 1, "10.8.1.5", 10.0)
    o._on_ack(o.devices[1], ack("10.8.1.5", xs), at(o, 10.05), o.ledger.get(xs))
    assert o.counters.dora_ack == 1 and o.counters.dora_ack_over_budget == 0
    assert o.devices[1].state is df.DState.ONLINE and o.devices[1].leased_ip == "10.8.1.5"
    assert 1 in o.online_set and timers(o, "t1_renew:", 1) == ["t1_renew:1"]
    assert o.lat_dora.cumulative_summary()["count"] == 1
    # over budget: the REQUEST timed out (retry sent), then its ACK lands
    _xd, xs2 = dora_to_select(o, 2, "10.8.0.6", 20.0)
    o._handle_timer(2, f"dora_timeout:{xs2}", at(o, 24.0))
    retry_xid = o.devices[2].xid
    assert retry_xid != xs2 and o.ledger.get(retry_xid).open
    o._on_ack(o.devices[2], ack("10.8.0.6", xs2), at(o, 24.5), o.ledger.get(xs2))
    assert o.counters.dora_ack == 2 and o.counters.dora_ack_over_budget == 1
    assert o.counters.dora_ack_late == 0 and o.counters.timeout == 0
    assert o.devices[2].state is df.DState.ONLINE
    # the retried DISCOVER was settled with the round; its OFFER is now ignored
    assert not o.ledger.get(retry_xid).open
    o._on_offer(o.devices[2], offer("10.8.0.6", retry_xid), o.ledger.get(retry_xid), at(o, 24.6))
    assert o.counters.offer_ignored == 1 and o.counters.dora_offer == 3
    assert o.devices[2].state is df.DState.ONLINE


def test_late_ack_after_give_up_is_counted_and_takes_the_lease(orch):
    """The #1057 hole: after MAX_DORA_RETRIES the device is OFFLINE and its
    `timeout` counted; the ACK kea sends a moment later used to match no branch."""
    o = orch
    dev = o.devices[3]
    _xd, xs = dora_to_select(o, 3, "10.8.1.9", 30.0)
    t = 34.0
    last_select = xs
    for _ in range(df.MAX_DORA_RETRIES):                 # 3 retries, each a fresh DORA
        o._handle_timer(3, f"dora_timeout:{dev.xid}", at(o, t))
        assert dev.state is df.DState.DISCOVERING
        xd = dev.xid
        o._on_offer(dev, offer("10.8.1.9", xd), o.ledger.get(xd), at(o, t + 0.1))
        last_select = dev.xid
        t += 4.0
    o._handle_timer(3, f"dora_timeout:{dev.xid}", at(o, t))     # 4th deadline → give up
    assert o.counters.timeout == 1 and dev.state is df.DState.OFFLINE
    assert dev.leased_ip is None and 3 not in o.online_set
    assert all(e.gave_up_at is not None for e in [o.ledger.get(last_select)])
    # kea's ACK for the last REQUEST arrives 2 s after the device gave up
    o._on_ack(dev, ack("10.8.1.9", last_select), at(o, t + 2.0), o.ledger.get(last_select))
    assert o.counters.dora_ack_late == 1 and o.counters.dora_ack == 0
    assert dev.state is df.DState.ONLINE and dev.leased_ip == "10.8.1.9" and 3 in o.online_set
    assert timers(o, "t1_renew:", 3) == ["t1_renew:1"]
    assert o.lat_dora_late.cumulative_summary()["count"] == 1
    assert o.lat_dora.cumulative_summary()["count"] == 0
    # a retransmitted ACK for the same exchange is a duplicate, an unknown xid unmatched
    o._on_ack(dev, ack("10.8.1.9", last_select), at(o, t + 2.5), o.ledger.get(last_select))
    o._on_ack(dev, ack("10.8.1.9", 0xAB000003), at(o, t + 2.6), None)
    assert o.counters.ack_duplicate == 1 and o.counters.ack_unmatched == 1
    assert o.counters.dora_ack_late == 1
    snap = o._counter_snapshot()
    assert snap["dora_ack_late"] == 1 and snap["timeout"] == 1
    h = df.handshake_summary(snap)
    assert h["strict_pct"] == 0.0 and h["with_late_pct"] == 100.0


def test_renew_ack_after_escalation_then_rebind_ack_renews_once(orch):
    o = orch
    dev = o.devices[4]
    _xd, xs = dora_to_select(o, 4, "10.8.0.7", 40.0)
    o._on_ack(dev, ack("10.8.0.7", xs), at(o, 40.1), o.ledger.get(xs))
    assert timers(o, "t1_renew:", 4) == ["t1_renew:1"]
    o._handle_timer(4, "t1_renew:1", at(o, 940.1))
    xr = dev.xid
    assert dev.state is df.DState.RENEWING and o.ledger.get(xr).kind == KIND_RENEW
    assert o.counters.renew_sent == 1
    o._handle_timer(4, f"renew_timeout:{xr}", at(o, 944.1))   # escalate
    xb = dev.xid
    assert dev.state is df.DState.REBINDING and o.counters.rebind_sent == 1
    # the renew's ACK lands late (kea unicast it after the deadline)
    o._on_ack(dev, ack("10.8.0.7", xr), at(o, 944.3), o.ledger.get(xr))
    assert o.counters.renew_ack_late == 1 and o.counters.renew_ack == 0
    assert dev.state is df.DState.ONLINE and dev.leased_ip == "10.8.0.7"
    assert timers(o, "t1_renew:", 4)[-1] == "t1_renew:2"
    # then the rebind's ACK: its exchange is live → strict, and T1 re-armed once more
    o._on_ack(dev, ack("10.8.0.7", xb), at(o, 944.4), o.ledger.get(xb))
    assert o.counters.rebind_ack == 1 and o.counters.rebind_ack_late == 0
    # the device was already ONLINE: the T1 armed by the round's first ACK stands
    assert dev.state is df.DState.ONLINE and dev.t1_token == 2
    # the retired T1 token renews nothing; the live one does — one renewal, not two
    o._handle_timer(4, "t1_renew:1", at(o, 1844.4))
    assert o.counters.renew_sent == 1
    o._handle_timer(4, "t1_renew:2", at(o, 1844.5))
    assert o.counters.renew_sent == 2 and dev.state is df.DState.RENEWING


def test_lapse_then_late_rebind_ack_revives_but_departure_does_not(orch):
    o = orch
    # lapse → late rebind ACK revives the lease
    dev = o.devices[5]
    _xd, xs = dora_to_select(o, 5, "10.8.1.7", 50.0)
    o._on_ack(dev, ack("10.8.1.7", xs), at(o, 50.1), o.ledger.get(xs))
    o._handle_timer(5, "t1_renew:1", at(o, 950.1))
    o._handle_timer(5, f"renew_timeout:{dev.xid}", at(o, 954.1))
    xb = dev.xid
    o._handle_timer(5, f"rebind_timeout:{xb}", at(o, 958.1))
    assert o.counters.lapses == 1 and dev.state is df.DState.LEFT and dev.lapsed
    assert dev.leased_ip is None and 5 not in o.online_set
    o._on_ack(dev, ack("10.8.1.7", xb), at(o, 959.0), o.ledger.get(xb))
    assert o.counters.rebind_ack_late == 1
    assert dev.state is df.DState.ONLINE and dev.leased_ip == "10.8.1.7" and 5 in o.online_set
    # departure with a renew in flight → its ACK is late and moves nothing
    dev6 = o.devices[6]
    _xd, xs6 = dora_to_select(o, 6, "10.8.0.8", 60.0)
    o._on_ack(dev6, ack("10.8.0.8", xs6), at(o, 60.1), o.ledger.get(xs6))
    o._handle_timer(6, "t1_renew:1", at(o, 960.1))
    xr6 = dev6.xid
    o.rng.random = lambda: 0.99                          # silent departure, no RELEASE
    o._handle_timer(6, "depart", at(o, 961.0))
    assert o.counters.departures == 1 and dev6.state is df.DState.LEFT and not dev6.lapsed
    o._on_ack(dev6, ack("10.8.0.8", xr6), at(o, 961.5), o.ledger.get(xr6))
    assert o.counters.renew_ack_late == 1
    assert dev6.state is df.DState.LEFT and dev6.leased_ip is None and 6 not in o.online_set


def test_nak_restarts_the_round_and_settles_the_old_exchanges(orch):
    o = orch
    dev = o.devices[7]
    _xd, xs = dora_to_select(o, 7, "10.8.1.8", 70.0)
    ep = dev.episode
    o._on_nak(dev, o.ledger.get(xs), at(o, 70.1))
    assert o.counters.nak == 1 and dev.state is df.DState.DISCOVERING
    assert dev.episode == ep + 1 and o.counters.dora_sent == 2
    assert o.ledger.get(xs).closed_by == "nak"
    assert [e.xid for e in o.ledger.open_for(7, DORA_KINDS)] == [dev.xid]


def test_dns_query_counts_every_rcode_timeouts_and_errors_apart(orch, monkeypatch):
    o = orch
    dev = o.devices[0]
    outcomes = iter([
        types.SimpleNamespace(rcode=lambda: dns_rcode.REFUSED),
        types.SimpleNamespace(rcode=lambda: dns_rcode.NOERROR),
        types.SimpleNamespace(rcode=lambda: dns_rcode.NXDOMAIN),
        types.SimpleNamespace(rcode=lambda: dns_rcode.SERVFAIL),
        dns_exception.Timeout(),
        OSError("network unreachable"),
        OSError("network unreachable"),
    ])

    async def fake_udp(q, where, port=53, timeout=None):
        nxt = next(outcomes)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    monkeypatch.setattr(df, "_dns_aq", types.SimpleNamespace(udp=fake_udp))
    for _ in range(7):
        asyncio.run(o._dns_query(dev))
    c = o.counters
    assert (c.dns_sent, c.dns_answered, c.dns_ok, c.dns_timeout, c.dns_error) == (7, 4, 2, 1, 2)
    snap = o._counter_snapshot()
    assert snap["dns_rcode_REFUSED"] == 1 and snap["dns_rcode_NOERROR"] == 1
    assert snap["dns_rcode_NXDOMAIN"] == 1 and snap["dns_rcode_SERVFAIL"] == 1
    assert o.dns_tally.errors == {"OSError": 2}
    assert o.lat_dns.cumulative_summary()["count"] == 4
    d = df.dns_summary(snap)
    assert d["unaccounted"] == 0 and d["not_ok"] == 2 and d["ok_pct_of_sent"] == round(200 / 7, 3)


def test_finalize_writes_the_ledgers_into_the_shard_summary(orch):
    o = orch
    _xd, xs = dora_to_select(o, 1, "10.8.1.5", 10.0)
    o._on_ack(o.devices[1], ack("10.8.1.5", xs), at(o, 10.05), o.ledger.get(xs))
    o.dns_tally.answered("REFUSED")
    o.counters.dns_sent += 1
    o.counters.dns_answered += 1
    o._finalize()
    path = o.rp.generator("orchestrator.shard0.summary.ndjson")
    summary = json.loads(path.read_text().splitlines()[-1])
    assert summary["counters"]["dora_ack"] == 1 and summary["counters"]["dns_rcode_REFUSED"] == 1
    assert summary["handshake"]["strict_pct"] == 100.0 and summary["handshake"]["acked_late"] == 0
    assert summary["dns"]["rcodes"] == {"REFUSED": 1} and summary["dns"]["ok"] == 0
    assert summary["dora_ack_late"]["count"] == 0 and summary["dora_ack"]["count"] == 1
    # The encoded histogram exists only with the HdrHistogram backend; the
    # reservoir fallback dumps nothing (lifecycle_log.dump_hdr).
    hdr = o.rp.generator("orchestrator.shard0.dhcp_dora_ack_late.hdr")
    assert hdr.exists() == lifecycle_log._HAVE_HDR
