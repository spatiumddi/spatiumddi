"""The orchestrator's DNS leg wired to the per-rcode tally (#1057), driven without
a socket: the dnspython query is faked and its outcomes injected.

Needs the harness deps (PyYAML for the manifest, dnspython for the DNS leg);
skipped where they are absent (CI's perf job installs pytest only — the pure
ledger is covered by test_accounting.py there).
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


def test_finalize_writes_the_dns_ledger_into_the_shard_summary(orch):
    o = orch
    o.dns_tally.answered("REFUSED")
    o.counters.dns_sent += 1
    o.counters.dns_answered += 1
    o._finalize()
    path = o.rp.generator("orchestrator.shard0.summary.ndjson")
    summary = json.loads(path.read_text().splitlines()[-1])
    assert summary["counters"]["dns_rcode_REFUSED"] == 1
    assert summary["counters"]["dns_answered"] == 1
    assert summary["dns"]["rcodes"] == {"REFUSED": 1} and summary["dns"]["ok"] == 0
    assert summary["dns"]["unaccounted"] == 0 and summary["dns_error_types"] == {}
