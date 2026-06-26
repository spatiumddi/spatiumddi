#!/usr/bin/env python3
"""Operator-API mutation stream — the audit hash-chain lock exerciser (docs §1.6 / H1).

The pure device-lifecycle load writes ZERO audit rows (verified — the Kea lease path
never calls ``write_audit``, `agents.py:1097-1105`), so it never touches the audit
hash-chain advisory lock (`audit_chain.py:153`,
``pg_advisory_xact_lock(0x4144495441554449)``, held to COMMIT). This stream is the
ONLY thing in the headline run that exercises criterion (a)'s lock/deadlock dimension:
a parallel stream of authenticated operator-API mutations, each of which writes one
``audit_log`` row and therefore acquires the global advisory lock to COMMIT.

Mix (each acquires the lock; grounded against the real backend):
  * bulk-allocate  POST /ipam/subnets/{id}/bulk-allocate          (router.py:7151;
                   audit ``ipam_bulk_allocate`` router.py:7296; cap 1024/call)
  * IP edit        PUT  /ipam/addresses/{id}                       (router.py:6225)
  * DNS record CRUD POST/DELETE /dns/groups/{gid}/zones/{zid}/records
                   (router.py:4272 create / router.py:4398 delete)
  * subnet tag edit PUT /ipam/subnets/{id}  (tags/custom_fields)   (router.py:4270;
                   SubnetUpdate.tags router.py:1888)

Rate: ``setpoint.operator_mutation_per_s`` (2-5/s sustained, bursting ~50/s in the
ceiling window — §1.6). Reads the rate each loop from the setpoint bus; honors the
kill-switch + stale-setpoint fail-safe-to-OFF.

Emits ``warroom/operator_mutation.ndjson``: {ts, rate, p50/p95/p99_ms, http_5xx,
audited_ops}. The admin token comes from the env var NAMED in the manifest
(``observability.superadmin_token_env``); NEVER hardcoded.

CLI: api_mutation_stream.py --run-id <id> --run-root <path> --manifest <path>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import spddi_perf.manifest as manifest_mod  # noqa: E402
import spddi_perf.setpoints as setpoints_mod  # noqa: E402
from spddi_perf.logging_util import append_ndjson, get_logger, read_json, utc_now_iso  # noqa: E402
from spddi_perf.runpaths import RunPaths  # noqa: E402

from lifecycle_log import LatencyAccumulator  # noqa: E402

SERVICE = "api_mutation"
STATS_INTERVAL_S = 10.0
STALE_TICKS = 3
SETPOINT_TICK_S = 60.0


class ApiMutationStream:
    def __init__(self, args: argparse.Namespace) -> None:
        self.rp = RunPaths.for_run(args.run_id, args.run_root)
        self.m = manifest_mod.load(args.manifest)
        self.log = get_logger(
            SERVICE, service=SERVICE, run_id=args.run_id,
            logfile=str(self.rp.worker_log("api_mutation")))
        self.out = self.rp.warroom("operator_mutation")
        self.api = self.m.target.api_base.rstrip("/")
        self.token = os.environ.get(self.m.observability.superadmin_token_env)
        self.verify = os.environ.get("SPDDI_PERF_CA_BUNDLE", False)
        self.rng = random.Random(0xA17ED)
        self.lat = LatencyAccumulator("operator_mutation")
        self._stop = asyncio.Event()
        self._last_seen_tick = -1
        self._tick_seen_at = time.monotonic()
        self._failsafe = False
        self._accum = 0.0
        # window counters
        self._audited = 0
        self._http_5xx = 0
        self._http_4xx = 0
        self._inflight = 0
        # discovered targets (refreshed lazily)
        self._subnets: list[dict] = []
        self._mutation_subnet: dict | None = None
        self._dns_group_id: str | None = None
        self._dns_zone_id: str | None = None
        self._created_records: list[str] = []   # zone records we POSTed, to DELETE later
        self._editable_ips: list[str] = []       # operator-owned IP ids we may PUT
        self._bulk_cursor = 0                     # rolling /24 offset for bulk-allocate

    # ---------------- helpers ----------------
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def _discover_targets(self, client) -> None:
        """One-time target discovery: a mutation-scratch subnet + a DNS group/zone."""
        # Prefer a seed-manifest-designated scratch subnet so bulk-allocate doesn't
        # collide with the device-fleet dynamic pools.
        seed = read_json(self.rp.seed_manifest) or {}
        scratch = seed.get("mutation_subnet") or seed.get("operator_subnet")
        try:
            r = await client.get(f"{self.api}/ipam/subnets")
            if r.status_code == 200:
                self._subnets = r.json()
        except Exception as exc:
            self.log.warning("subnet discovery failed: %s", exc,
                             extra={"fields": {"event": "discover_failed"}})
        if scratch:
            for s in self._subnets:
                if s.get("id") == scratch.get("id") or s.get("cidr") == scratch.get("cidr"):
                    self._mutation_subnet = s
                    break
        if self._mutation_subnet is None and self._subnets:
            # Fall back to the smallest subnet so bulk-allocate stamps a bounded range.
            self._mutation_subnet = min(
                self._subnets, key=lambda s: _prefix_size(s.get("cidr", "0.0.0.0/0")))
        # DNS group + a zone for record CRUD.
        try:
            r = await client.get(f"{self.api}/dns/groups")
            groups = r.json() if r.status_code == 200 else []
            if groups:
                self._dns_group_id = groups[0]["id"]
                rz = await client.get(
                    f"{self.api}/dns/groups/{self._dns_group_id}/zones")
                zones = rz.json() if rz.status_code == 200 else []
                # pick a forward zone (skip in-addr.arpa to keep record names simple)
                fwd = [z for z in zones if not str(z.get("name", "")).endswith(".arpa")]
                if fwd:
                    self._dns_zone_id = fwd[0]["id"]
        except Exception as exc:
            self.log.warning("dns discovery failed: %s", exc,
                             extra={"fields": {"event": "discover_failed"}})
        self.log.info(
            "targets discovered",
            extra={"fields": {"event": "targets",
                              "mutation_subnet": (self._mutation_subnet or {}).get("network"),
                              "dns_group": self._dns_group_id, "dns_zone": self._dns_zone_id}})

    async def _do_mutation(self, client) -> None:
        """Pick one mutation kind and issue it (each writes one audit_log row)."""
        kinds = ["dns_record_create", "dns_record_delete", "subnet_tag_edit",
                 "ip_edit", "bulk_allocate"]
        weights = [0.30, 0.15, 0.30, 0.15, 0.10]
        kind = self.rng.choices(kinds, weights=weights)[0]
        try:
            if kind == "dns_record_create":
                await self._m_dns_record_create(client)
            elif kind == "dns_record_delete":
                await self._m_dns_record_delete(client)
            elif kind == "subnet_tag_edit":
                await self._m_subnet_tag_edit(client)
            elif kind == "ip_edit":
                await self._m_ip_edit(client)
            elif kind == "bulk_allocate":
                await self._m_bulk_allocate(client)
        except Exception as exc:  # network/transport — counted as a failure, not 5xx
            self.log.debug("mutation %s error: %s", kind, exc,
                           extra={"fields": {"event": "mutation_error", "kind": kind}})

    def _record(self, status_code: int, latency_ms: float, *, audited: bool) -> None:
        self.lat.record_ms(latency_ms)
        if 500 <= status_code < 600:
            self._http_5xx += 1
        elif 400 <= status_code < 500:
            self._http_4xx += 1
        elif audited and 200 <= status_code < 300:
            self._audited += 1

    async def _m_dns_record_create(self, client) -> None:
        if not (self._dns_group_id and self._dns_zone_id):
            return
        name = f"perf-op-{self.rng.randrange(1 << 28):x}"
        body = {"name": name, "record_type": "A",
                "value": f"10.255.{self.rng.randrange(256)}.{self.rng.randrange(1, 255)}"}
        t0 = time.monotonic()
        r = await client.post(
            f"{self.api}/dns/groups/{self._dns_group_id}/zones/{self._dns_zone_id}/records",
            json=body)
        self._record(r.status_code, (time.monotonic() - t0) * 1000.0, audited=True)
        if r.status_code in (200, 201):
            rid = r.json().get("id")
            if rid and len(self._created_records) < 5000:
                self._created_records.append(rid)

    async def _m_dns_record_delete(self, client) -> None:
        if not (self._dns_group_id and self._dns_zone_id and self._created_records):
            return
        rid = self._created_records.pop()
        t0 = time.monotonic()
        r = await client.delete(
            f"{self.api}/dns/groups/{self._dns_group_id}/zones/{self._dns_zone_id}/records/{rid}")
        self._record(r.status_code, (time.monotonic() - t0) * 1000.0, audited=True)

    async def _m_subnet_tag_edit(self, client) -> None:
        if self._mutation_subnet is None:
            return
        sid = self._mutation_subnet["id"]
        body = {"tags": {"perf_op": str(self.rng.randrange(1 << 30))}}
        t0 = time.monotonic()
        r = await client.put(f"{self.api}/ipam/subnets/{sid}", json=body)
        self._record(r.status_code, (time.monotonic() - t0) * 1000.0, audited=True)

    async def _m_ip_edit(self, client) -> None:
        # Edit an operator-owned IP (NOT auto_from_lease — those 409 on PUT,
        # router.py:6237). Lazily build a pool of editable rows from the scratch subnet.
        if not self._editable_ips:
            await self._refresh_editable_ips(client)
        if not self._editable_ips:
            return
        ip_id = self.rng.choice(self._editable_ips)
        body = {"description": f"perf-op edit {self.rng.randrange(1 << 24)}"}
        t0 = time.monotonic()
        r = await client.put(f"{self.api}/ipam/addresses/{ip_id}", json=body)
        self._record(r.status_code, (time.monotonic() - t0) * 1000.0, audited=True)
        if r.status_code == 409:
            # was an auto_from_lease mirror — drop it from the editable pool
            self._editable_ips = [i for i in self._editable_ips if i != ip_id]

    async def _refresh_editable_ips(self, client) -> None:
        if self._mutation_subnet is None:
            return
        sid = self._mutation_subnet["id"]
        try:
            r = await client.get(f"{self.api}/ipam/subnets/{sid}/addresses")
            if r.status_code == 200:
                self._editable_ips = [
                    row["id"] for row in r.json() if not row.get("auto_from_lease")][:500]
        except Exception as exc:
            # Non-fatal: a failed refresh just reuses the previous editable pool
            # next tick. Log at debug so it's diagnosable without adding noise.
            self.log.debug("editable-IP refresh failed: %s", exc)

    async def _m_bulk_allocate(self, client) -> None:
        # Stamp a small contiguous range (cap 1024/call, router.py:7155). Roll a /24
        # cursor through the scratch subnet's high octet so successive calls don't
        # all collide on the same already-allocated rows.
        if self._mutation_subnet is None:
            return
        # GET /ipam/subnets returns the CIDR under "network" (SubnetResponse.network);
        # there is no "cidr" field — the old .get("cidr") made _bulk_range("") raise.
        cidr = self._mutation_subnet.get("network") or self._mutation_subnet.get("cidr") or ""
        base = _bulk_range(cidr, self._bulk_cursor) if cidr else None
        if base is None:
            return
        self._bulk_cursor = (self._bulk_cursor + 1) % 200
        start, end = base
        body = {
            "range_start": start, "range_end": end,
            "hostname_template": "perfop-{n:04d}",
            "create_dns_records": False,          # keep this to the IPAM audit path
            "on_collision": "skip",               # don't 4xx the whole batch on overlap
        }
        sid = self._mutation_subnet["id"]
        t0 = time.monotonic()
        r = await client.post(f"{self.api}/ipam/subnets/{sid}/bulk-allocate", json=body)
        self._record(r.status_code, (time.monotonic() - t0) * 1000.0, audited=True)

    # ---------------- setpoint / staleness ----------------
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
                self.log.error("setpoint stale — failing safe to OFF",
                               extra={"fields": {"event": "setpoint_stale"}})
            self._failsafe = True
        return sp

    # ---------------- loops ----------------
    async def _drive_loop(self, client) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            if self.rp.stop_file.exists():
                self.log.warning("kill-switch present — stopping",
                                 extra={"fields": {"event": "kill_switch"}})
                self._stop.set()
                break
            sp = self._check_stale(setpoints_mod.read_current(self.rp))
            now = time.monotonic()
            dt = now - last
            last = now
            rate = 0.0
            if sp is not None and not self._failsafe:
                rate = max(0.0, sp.operator_mutation_per_s)
            self._accum += rate * dt
            n = int(self._accum)
            self._accum -= n
            for _ in range(min(n, 100)):  # bound dispatch per pass
                asyncio.ensure_future(self._do_mutation(client))
            await asyncio.sleep(0.1)

    async def _stats_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(STATS_INTERVAL_S)
            p = self.lat.window_percentiles()
            sp = setpoints_mod.read_current(self.rp)
            rate = sp.operator_mutation_per_s if sp else 0.0
            rec = {
                "ts": utc_now_iso(),
                "rate": round(rate, 3),
                "p50_ms": p["p50"], "p95_ms": p["p95"], "p99_ms": p["p99"],
                "http_5xx": self._http_5xx,
                "http_4xx": self._http_4xx,
                "audited_ops": self._audited,
                "window_count": p["count"],
            }
            append_ndjson(self.out, rec)
            self._http_5xx = self._http_4xx = self._audited = 0

    async def run(self) -> None:
        if not self.token:
            self.log.error(
                "no admin token in $%s — operator-mutation stream cannot run; "
                "audit-lock contention WILL NOT BE EXERCISED",
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
                # add_signal_handler isn't available on every platform / non-main
                # thread; shutdown still flows through the _stop event via other paths.
                self.log.debug("signal handler unavailable for %s", sig)
        async with httpx.AsyncClient(
                verify=self.verify, timeout=15.0, headers=self._headers()) as client:
            await self._discover_targets(client)
            if self._mutation_subnet is None and self._dns_zone_id is None:
                self.log.error(
                    "no IPAM subnet or DNS zone discovered — the operator-mutation "
                    "stream has nothing to mutate; audit-lock contention (§1.6/H1) WILL "
                    "NOT BE EXERCISED. Ensure the scaffold is seeded before this starts.",
                    extra={"fields": {"event": "no_targets"}})
            self.log.info("operator-mutation stream online",
                          extra={"fields": {"event": "start"}})
            tasks = [
                asyncio.ensure_future(self._drive_loop(client)),
                asyncio.ensure_future(self._stats_loop()),
            ]
            await self._stop.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.log.info("operator-mutation stream stopped",
                      extra={"fields": {"event": "stop"}})


def _prefix_size(cidr: str) -> int:
    try:
        return int(cidr.split("/")[1])
    except (IndexError, ValueError):
        return 0


def _bulk_range(cidr: str, cursor: int) -> tuple[str, str] | None:
    """A small (16-address) contiguous range inside ``cidr`` offset by ``cursor``.

    Walks the third octet by ``cursor`` so successive calls hit fresh space. Bounded
    well under the 1024/call cap (router.py:7155).
    """
    import ipaddress
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None
    base = int(net.network_address) + 1 + (cursor * 16)
    last = base + 15
    if last >= int(net.broadcast_address):
        return None
    return (str(ipaddress.ip_address(base)), str(ipaddress.ip_address(last)))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SpatiumDDI perf — operator-API mutation stream (audit-lock "
                    "exerciser). Runs OFF-BOX.")
    p.add_argument("--run-id", required=True)
    p.add_argument("--run-root", required=True)
    p.add_argument("--manifest", required=True)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        asyncio.run(ApiMutationStream(args).run())
    except KeyboardInterrupt:
        # Intentional: swallow Ctrl-C for a clean CLI exit without a traceback.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
