#!/usr/bin/env python3
"""Synthetic human-UI probe — "can an admin open the Dashboard at surge peak?" (§7.6.8).

A LOW-RATE authenticated "human" page-load probe, separate from the operator-*mutation*
stream, with its OWN latency / 5xx SLO (b13). It issues the representative heavy reads a
real admin's browser fires when opening a page — the N+1 list endpoints — and times
them, answering the brief's explicit question: *can an admin open the Dashboard at the
surge peak without a 30s spinner or a 502, on a 30-connection app pool already shared by
the war-room + orchestrator + operator-mutation stream?*

Pages probed (grounded against the real backend + the frontend's own queries):
  * subnets list   GET /ipam/subnets                              (router.py:3501;
                   DashboardPage useQuery(["subnets"]) — the heavy utilization list)
  * dashboard roll GET /dashboards/network/summary                (dashboards/network.py:134;
                   frontend api.ts:7725 networkDashboardApi.summary)
  * DNS zone recs  GET /dns/groups/{gid}/zones/{zid}/records      (dns/router.py:4251)

Rate: a fixed low cadence (default ~1 page-load every 15s — a human refreshing), NOT
setpoint-driven (a human's click rate doesn't scale with device load — that's the
point). Still honors the kill-switch + stale-setpoint fail-safe (if the controller is
gone the run is over, so stop). Emits one row PER endpoint per sample to
``warroom/ui_probe.ndjson``: {ts, endpoint, p50/p95/p99_ms, http_5xx} (percentiles over
a rolling window per endpoint).

Admin token from the env var NAMED in the manifest (never hardcoded).

CLI: synthetic_ui_probe.py --run-id <id> --run-root <path> --manifest <path>
     [--interval-s 15]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import spddi_perf.manifest as manifest_mod  # noqa: E402
import spddi_perf.setpoints as setpoints_mod  # noqa: E402
from spddi_perf.logging_util import append_ndjson, get_logger, utc_now_iso  # noqa: E402
from spddi_perf.runpaths import RunPaths  # noqa: E402

from lifecycle_log import LatencyAccumulator  # noqa: E402

SERVICE = "ui_probe"
DEFAULT_INTERVAL_S = 15.0     # a human refreshing a page roughly every ~15s
STATS_INTERVAL_S = 30.0       # flush per-endpoint percentiles on this cadence
STALE_TICKS = 3
SETPOINT_TICK_S = 60.0


class SyntheticUiProbe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.rp = RunPaths.for_run(args.run_id, args.run_root)
        self.m = manifest_mod.load(args.manifest)
        self.interval = float(args.interval_s)
        self.log = get_logger(
            SERVICE, service=SERVICE, run_id=args.run_id,
            logfile=str(self.rp.worker_log("ui_probe")))
        self.out = self.rp.warroom("ui_probe")
        self.api = self.m.target.api_base.rstrip("/")
        self.token = os.environ.get(self.m.observability.superadmin_token_env)
        self.verify = os.environ.get("SPDDI_PERF_CA_BUNDLE", False)
        self._stop = asyncio.Event()
        self._last_seen_tick = -1
        self._tick_seen_at = time.monotonic()
        self._failsafe = False
        # per-endpoint accumulator + window 5xx counters
        self._endpoints = ["ipam_subnets_list", "dashboard_rollup", "dns_zone_records"]
        self._lat = {e: LatencyAccumulator(f"ui_{e}") for e in self._endpoints}
        self._5xx = {e: 0 for e in self._endpoints}
        self._4xx = {e: 0 for e in self._endpoints}
        self._dns_group_id: str | None = None
        self._dns_zone_id: str | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _discover(self, client) -> None:
        try:
            r = await client.get(f"{self.api}/dns/groups")
            groups = r.json() if r.status_code == 200 else []
            if groups:
                self._dns_group_id = groups[0]["id"]
                rz = await client.get(
                    f"{self.api}/dns/groups/{self._dns_group_id}/zones")
                zones = rz.json() if rz.status_code == 200 else []
                fwd = [z for z in zones if not str(z.get("name", "")).endswith(".arpa")]
                pick = fwd or zones
                if pick:
                    self._dns_zone_id = pick[0]["id"]
        except Exception as exc:
            self.log.warning("dns target discovery failed: %s", exc,
                             extra={"fields": {"event": "discover_failed"}})
        self.log.info("ui-probe targets",
                      extra={"fields": {"event": "targets",
                                        "dns_group": self._dns_group_id,
                                        "dns_zone": self._dns_zone_id}})

    async def _probe_once(self, client) -> None:
        await self._timed(client, "ipam_subnets_list", "GET",
                          f"{self.api}/ipam/subnets")
        await self._timed(client, "dashboard_rollup", "GET",
                          f"{self.api}/dashboards/network/summary")
        if self._dns_group_id and self._dns_zone_id:
            await self._timed(
                client, "dns_zone_records", "GET",
                f"{self.api}/dns/groups/{self._dns_group_id}/zones/{self._dns_zone_id}/records")

    async def _timed(self, client, endpoint: str, method: str, url: str) -> None:
        t0 = time.monotonic()
        try:
            r = await client.request(method, url)
            latency_ms = (time.monotonic() - t0) * 1000.0
            self._lat[endpoint].record_ms(latency_ms)
            if 500 <= r.status_code < 600:
                self._5xx[endpoint] += 1
                self.log.warning("ui probe 5xx", extra={"fields": {
                    "event": "ui_5xx", "endpoint": endpoint, "status": r.status_code,
                    "latency_ms": round(latency_ms, 1)}})
            elif 400 <= r.status_code < 500:
                self._4xx[endpoint] += 1
        except Exception as exc:
            # transport failure / timeout — a human would see a hung spinner.
            latency_ms = (time.monotonic() - t0) * 1000.0
            self._lat[endpoint].record_ms(latency_ms)
            self._5xx[endpoint] += 1  # count as unavailable (spinner/502 from the human POV)
            self.log.warning("ui probe transport failure: %s", exc, extra={"fields": {
                "event": "ui_transport_fail", "endpoint": endpoint,
                "latency_ms": round(latency_ms, 1)}})

    def _check_stale(self, sp):
        now = time.monotonic()
        if sp is None:
            return None
        if sp.tick != self._last_seen_tick:
            self._last_seen_tick = sp.tick
            self._tick_seen_at = now
            self._failsafe = False
        elif now - self._tick_seen_at > STALE_TICKS * SETPOINT_TICK_S:
            if not self._failsafe:
                self.log.error("setpoint stale — controller gone, stopping probe",
                               extra={"fields": {"event": "setpoint_stale"}})
            self._failsafe = True
        return sp

    async def _probe_loop(self, client) -> None:
        while not self._stop.is_set():
            if self.rp.stop_file.exists():
                self.log.warning("kill-switch present — stopping",
                                 extra={"fields": {"event": "kill_switch"}})
                self._stop.set()
                break
            self._check_stale(setpoints_mod.read_current(self.rp))
            if self._failsafe:
                # controller gone: the run is over for a UI human too. Stop driving.
                self._stop.set()
                break
            await self._probe_once(client)
            await asyncio.sleep(self.interval)

    async def _stats_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(STATS_INTERVAL_S)
            for endpoint in self._endpoints:
                p = self._lat[endpoint].window_percentiles()
                if p["count"] == 0 and self._5xx[endpoint] == 0:
                    continue
                rec = {
                    "ts": utc_now_iso(),
                    "endpoint": endpoint,
                    "p50_ms": p["p50"], "p95_ms": p["p95"], "p99_ms": p["p99"],
                    "http_5xx": self._5xx[endpoint],
                    "http_4xx": self._4xx[endpoint],
                    "window_count": p["count"],
                }
                append_ndjson(self.out, rec)
                self._5xx[endpoint] = 0
                self._4xx[endpoint] = 0

    async def run(self) -> None:
        if not self.token:
            self.log.error(
                "no admin token in $%s — synthetic-UI probe cannot run "
                "(human-usability b13 NOT MEASURED)",
                self.m.observability.superadmin_token_env,
                extra={"fields": {"event": "no_token"}})
            return
        try:
            import httpx
        except Exception:
            self.log.error("httpx not importable (install perf/requirements.txt)")
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        async with httpx.AsyncClient(
                verify=self.verify, timeout=30.0, headers=self._headers()) as client:
            await self._discover(client)
            self.log.info("synthetic-UI probe online",
                          extra={"fields": {"event": "start", "interval_s": self.interval}})
            tasks = [
                asyncio.ensure_future(self._probe_loop(client)),
                asyncio.ensure_future(self._stats_loop()),
            ]
            await self._stop.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.log.info("synthetic-UI probe stopped",
                      extra={"fields": {"event": "stop"}})


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SpatiumDDI perf — synthetic human-UI page-load probe (§7.6.8). "
                    "Runs OFF-BOX.")
    p.add_argument("--run-id", required=True)
    p.add_argument("--run-root", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--interval-s", type=float, default=DEFAULT_INTERVAL_S,
                   help="seconds between page-load samples (default 15)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        asyncio.run(SyntheticUiProbe(args).run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
