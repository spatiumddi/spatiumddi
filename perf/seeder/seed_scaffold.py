#!/usr/bin/env python3
"""seed_scaffold — build the IPAM/DHCP/DNS scaffold for one perf run (§7.5, §9 Phase 1).

Creates, via the REST API (no direct DB writes — non-negotiable #1/#3):

  * 1 IP space + 1 block (``seed.ip_block``);
  * N subnets under the block (``seed.subnets.count`` × /``prefix``);
  * 1 DHCP server group + one scope per subnet + a large dynamic pool per scope
    (``seed.subnets.pool_fraction`` of the host range);
    — if ``target.dhcp.topology == relay`` each scope gets ``relay_addresses=[giaddr_i]``
      (one giaddr per subnet, from ``target.dhcp.giaddr``);
  * 1 DNS server group + the forward zone(s) (``seed.dns.forward_zones``) + the reverse
    zone(s) per ``seed.dns.reverse_zone_shape``;
  * then BULK-LOADS ``seed.dns.authoritative_records`` A/AAAA + PTR records (chunked,
    concurrent, progress-logged) so the DNS test is closed-loop (§4.9 Layer 1).

Group reuse (pre-prepared appliances):
  If the appliance was set up manually via PERF_APPLIANCE_SETUP.md, the DNS/DHCP
  groups already exist and bind9/kea are already bound to them. Set:
    SPDDI_PERF_DNS_GROUP_ID=<uuid>   — skip group creation; use this group for zones
    SPDDI_PERF_DHCP_GROUP_ID=<uuid>  — skip group creation; use this group for scopes
  The seeder will then create zones (get-or-create, idempotent) and fresh scopes inside
  the existing groups. Bulk record load is skipped when a forward zone already exists
  (assumes it was seeded by a prior run).

It does NOT pre-seed ``ip_address`` rows — those auto-mirror from lease events (§3.3).

It writes ``rp.seed_manifest`` recording exactly what it created (ids, cidrs, zone
names, pool ranges, giaddrs). Downstream generators read it.

GROUNDING (real backend routes — cited inline):
  * POST /v1/ipam/spaces                       ipam/router.py:2403 (IPSpaceCreate:1486)
  * POST /v1/ipam/blocks                       ipam/router.py:2667 (IPBlockCreate:1599)
  * POST /v1/ipam/subnets                      ipam/router.py:3551 (SubnetCreate:1725)
  * POST /v1/dhcp/server-groups                dhcp/server_groups.py:166 (GroupCreate:42)
  * POST /v1/dhcp/subnets/{id}/dhcp-scopes     dhcp/scopes.py:321 (ScopeCreate:131, relay_addresses:152)
  * POST /v1/dhcp/scopes/{id}/pools            dhcp/pools.py:201 (PoolCreate:31)
  * POST /v1/dns/groups                        dns/router.py:1021 (ServerGroupCreate:157)
  * GET  /v1/dns/groups/{gid}/zones            dns/router.py:2783 (list zones)
  * POST /v1/dns/groups/{gid}/zones            dns/router.py:2786 (ZoneCreate:694, kind forward|reverse:608)
  * POST /v1/dns/groups/{gid}/zones/{zid}/records  dns/router.py:4271 (RecordCreate:882)

Usage:  python3 seed_scaffold.py --run-id <id> --run-root <path> --manifest <path>
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import ipaddress
import logging
import os
import sys
import threading
import time
from typing import Any

import spddi_perf.manifest as manifest_mod
from spddi_perf import canonical
from spddi_perf.logging_util import atomic_write_json, get_logger, log_event, utc_now_iso
from spddi_perf.runpaths import RunPaths

# _api lives beside this script (perf/seeder/) — add to path defensively so it
# imports whether launched as a module or a file.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from _api import ApiClient, ApiError  # noqa: E402

# Env vars for group reuse (pre-prepared appliances — see PERF_APPLIANCE_SETUP.md §2).
# When set, skip group creation and seed zones/scopes into the existing groups so the
# already-running bind9/kea agents serve the seeded data.
ENV_DNS_GROUP_ID = "SPDDI_PERF_DNS_GROUP_ID"
ENV_DHCP_GROUP_ID = "SPDDI_PERF_DHCP_GROUP_ID"

RECORD_CHUNK = 200          # records logged per progress line
RECORD_WORKERS = 16         # concurrent POSTs for the bulk record load


# ---------------------------------------------------------------------------
# Subnet planning
# ---------------------------------------------------------------------------
def _plan_subnets(block_cidr: str, count: int, prefix: int) -> list[str]:
    """First ``count`` subnets of length ``prefix`` carved from ``block_cidr``."""
    block = ipaddress.ip_network(block_cidr, strict=False)
    if prefix <= block.prefixlen:
        raise ValueError(f"subnet prefix /{prefix} must be longer than block /{block.prefixlen}")
    out: list[str] = []
    for net in block.subnets(new_prefix=prefix):
        out.append(str(net))
        if len(out) >= count:
            break
    if len(out) < count:
        raise ValueError(
            f"block {block_cidr} only yields {len(out)} /{prefix} subnets, need {count}"
        )
    return out


def _pool_range(subnet_cidr: str, fraction: float) -> tuple[str, str]:
    """A dynamic pool spanning ``fraction`` of the usable host range.

    Starts just above the gateway (network+2) and runs to network + fraction*size,
    so network / broadcast / gateway placeholders stay outside the pool (§3.3).
    """
    net = ipaddress.ip_network(subnet_cidr, strict=False)
    hosts = int(net.num_addresses)
    # first usable = .1 (gateway) ; pool starts at .2
    start_off = 2
    span = max(1, int(hosts * fraction))
    end_off = min(hosts - 2, start_off + span - 1)  # leave broadcast out
    if end_off < start_off:
        end_off = start_off
    base = int(net.network_address)
    return str(ipaddress.ip_address(base + start_off)), str(ipaddress.ip_address(base + end_off))


def _reverse_zone_names(subnet_cidrs: list[str], shape: str, explicit: list[str]) -> list[str]:
    """Reverse-zone names per ``reverse_zone_shape`` (§0.A).

    ``per-octet`` → one classful reverse zone per distinct /16-derived octet pair
    (e.g. 10.0.0.0/16 → ``0.10.in-addr.arpa``). ``single`` → one zone covering the
    block's leading octets. ``explicit`` (manifest ``seed.dns.reverse_zones``) wins.
    """
    if explicit:
        return list(dict.fromkeys(explicit))
    names: list[str] = []
    seen: set[str] = set()
    for cidr in subnet_cidrs:
        net = ipaddress.ip_network(cidr, strict=False)
        if not isinstance(net, ipaddress.IPv4Network):
            continue  # IPv6 reverse zones not auto-shaped here
        octets = str(net.network_address).split(".")
        if shape == "single":
            name = f"{octets[0]}.in-addr.arpa"
        else:  # per-octet — /16 reverse zone (covers each subnet's leased space)
            name = f"{octets[1]}.{octets[0]}.in-addr.arpa"
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# Record generation (deterministic, in-zone by construction — §4.5 / §4.9 Layer 2)
# ---------------------------------------------------------------------------
def _ip_for_index(subnet_cidrs: list[str], idx: int) -> tuple[str, str]:
    """Deterministic (subnet_cidr, host_ip) for record index ``idx``.

    Spread round-robin across subnets, walking the host range. Stays inside the
    subnet so the synthesised A record's PTR lands in a seeded reverse zone.
    """
    n = len(subnet_cidrs)
    cidr = subnet_cidrs[idx % n]
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = int(net.num_addresses)
    host_off = 2 + (idx // n) % max(1, hosts - 3)
    return cidr, str(ipaddress.ip_address(int(net.network_address) + host_off))


def _gen_records(
    total: int, forward_zone: str, subnet_cidrs: list[str]
) -> list[dict[str, Any]]:
    """Build the authoritative record plan: an A in the forward zone + matching PTR.

    Each entry: ``{kind: 'forward'|'reverse', zone, name, record_type, value}``.
    Names are device-keyed (``dev-NNNNNNN``) matching canonical.client_hostname so
    the DNS query-set generator can target them without scraping the live zone (§4.5).
    """
    plan: list[dict[str, Any]] = []
    for i in range(total):
        cidr, ip = _ip_for_index(subnet_cidrs, i)
        host = f"dev-{i:07d}"
        plan.append(
            {"kind": "forward", "zone": forward_zone, "name": host, "record_type": "A", "value": ip}
        )
        rev = ipaddress.ip_address(ip).reverse_pointer  # e.g. 5.20.0.10.in-addr.arpa
        plan.append(
            {
                "kind": "reverse",
                "ip": ip,
                "name": rev,
                "record_type": "PTR",
                "value": f"{host}.{forward_zone.rstrip('.')}.",
            }
        )
    return plan


def _reverse_zone_for_ip(ip: str, reverse_zones: list[str]) -> str | None:
    """Longest-suffix match of an IP's reverse name against the seeded reverse zones."""
    rev = ipaddress.ip_address(ip).reverse_pointer
    best: str | None = None
    for z in reverse_zones:
        if rev == z or rev.endswith("." + z):
            if best is None or len(z) > len(best):
                best = z
    return best


# ---------------------------------------------------------------------------
# Zone get-or-create (idempotent — handles reuse of pre-existing groups)
# ---------------------------------------------------------------------------
def _get_or_create_zone(
    api: "ApiClient", group_id: str, zname: str, kind: str, log: logging.Logger
) -> tuple[str, bool]:
    """Return ``(zone_id, was_created)``.

    Tries POST first. On 409 (zone already exists in the group) fetches the zone
    list and returns the existing id with ``was_created=False``. Used when reusing
    a pre-existing DNS group so the seeder is idempotent across smoke-run retries.
    """
    try:
        z = api.json("POST", f"/v1/dns/groups/{group_id}/zones", json={
            "name": zname, "kind": kind, "zone_type": "primary",
        }, ok=(201,))
        return z["id"], True
    except ApiError as exc:
        if exc.status_code != 409:
            raise
        # Zone already exists — find it in the group's zone list
        zones = api.json("GET", f"/v1/dns/groups/{group_id}/zones")
        for z in zones:
            if z.get("name") == zname:
                log_event(log, logging.INFO, "zone_reused", zone=zname, zone_id=z["id"])
                return z["id"], False
        raise RuntimeError(
            f"zone {zname!r} reported conflict (409) but was not found in group {group_id}"
        ) from exc


# ---------------------------------------------------------------------------
# Bulk record loader (chunked, concurrent, progress-logged)
# ---------------------------------------------------------------------------
class _BulkLoader:
    def __init__(self, api: ApiClient, log: logging.Logger, group_id: str) -> None:
        self.api = api
        self.log = log
        self.group_id = group_id
        self._lock = threading.Lock()
        self.created = 0
        self.failed = 0
        self._first_errors: list[str] = []
        self.aborted = False   # tripped on an auth failure so we fast-fail, not silently skip

    def _post_one(self, zone_id: str, rec: dict[str, Any]) -> None:
        if self.aborted:
            return  # auth already failed — quick-skip the remaining submitted futures
        # POST /v1/dns/groups/{gid}/zones/{zid}/records — dns/router.py:4271
        path = f"/v1/dns/groups/{self.group_id}/zones/{zone_id}/records"
        body = {"name": rec["name"], "record_type": rec["record_type"], "value": rec["value"]}
        try:
            self.api.post(path, json=body, ok=(200, 201))
            with self._lock:
                self.created += 1
        except Exception as exc:  # noqa: BLE001 — keep loading; record + cap error spam
            msg = str(exc)
            with self._lock:
                self.failed += 1
                if len(self._first_errors) < 5:
                    self._first_errors.append(msg[:200])
                # Auth failure mid-load is NOT a per-record skip — every subsequent POST
                # would fail the same way. Trip a hard abort so the run fails loudly.
                if not self.aborted and any(s in msg for s in
                        ("401", "403", "Unauthorized", "Forbidden", "token", "expired")):
                    self.aborted = True
                    log_event(self.log, 40, "bulk_load_auth_abort", error=msg[:200])

    def load(self, items: list[tuple[str, dict[str, Any]]]) -> None:
        """Load ``(zone_id, record)`` pairs concurrently with progress logging."""
        total = len(items)
        if not total:
            return
        done = 0
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=RECORD_WORKERS) as ex:
            futs = [ex.submit(self._post_one, zid, rec) for zid, rec in items]
            for _ in cf.as_completed(futs):
                done += 1
                if done % RECORD_CHUNK == 0 or done == total:
                    rate = done / max(1e-6, time.time() - t0)
                    log_event(
                        self.log,
                        logging.INFO,
                        "bulk_record_progress",
                        done=done,
                        total=total,
                        created=self.created,
                        failed=self.failed,
                        rate_per_s=round(rate, 1),
                    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(rp: RunPaths, m: manifest_mod.Manifest, log: logging.Logger) -> int:
    sub_cfg = m.seed.subnets
    count = int(sub_cfg.get("count", 8))
    prefix = int(sub_cfg.get("prefix", 16))
    pool_fraction = float(sub_cfg.get("pool_fraction", 0.90))
    relay = m.target.dhcp.topology == "relay"
    giaddrs = list(m.target.dhcp.giaddr)
    if relay and len(giaddrs) != count:
        log.error("relay topology: %d giaddrs but %d subnets", len(giaddrs), count)
        return 2

    subnet_cidrs = _plan_subnets(m.seed.ip_block, count, prefix)
    forward_zones = list(m.seed.dns.forward_zones) or ["campus.example.edu"]
    forward_zone = forward_zones[0]
    reverse_zones = _reverse_zone_names(
        subnet_cidrs, m.seed.dns.reverse_zone_shape, m.seed.dns.reverse_zones
    )

    created: dict[str, Any] = {
        "ts": utc_now_iso(),
        "run_id": rp.run_id,
        "ip_space": None,
        "ip_block": None,
        "subnets": [],
        "dhcp_group": None,
        "dhcp_scopes": [],
        "dns_group": None,
        "forward_zones": [],
        "reverse_zones": [],
        "authoritative_records": {"requested": m.seed.dns.authoritative_records},
        "relay_topology": relay,
        "giaddrs": giaddrs,
    }
    # Write the partial manifest as we go (durability — §7.4).
    def _flush() -> None:
        atomic_write_json(rp.seed_manifest, created)

    # Check for pre-existing group IDs (set when the appliance was prepared manually
    # via PERF_APPLIANCE_SETUP.md — bind9/kea are already bound to those groups).
    existing_dns_group_id = (os.environ.get(ENV_DNS_GROUP_ID) or "").strip() or None
    existing_dhcp_group_id = (os.environ.get(ENV_DHCP_GROUP_ID) or "").strip() or None
    if existing_dns_group_id:
        log_event(log, logging.INFO, "reusing_dns_group", group_id=existing_dns_group_id,
                  env=ENV_DNS_GROUP_ID)
    if existing_dhcp_group_id:
        log_event(log, logging.INFO, "reusing_dhcp_group", group_id=existing_dhcp_group_id,
                  env=ENV_DHCP_GROUP_ID)

    with ApiClient.from_manifest(m, run_id=rp.run_id) as api:
        # ---- 1) IP space ------------------------------------------------
        # POST /v1/ipam/spaces — ipam/router.py:2403
        space = api.json("POST", "/v1/ipam/spaces", json={
            "name": f"perf-{rp.run_id[:32]}",
            "description": f"perf scaffold for {rp.run_id}",
        }, ok=(201,))
        space_id = space["id"]
        created["ip_space"] = {"id": space_id, "name": space["name"]}
        log_event(log, logging.INFO, "created_space", space_id=space_id)
        _flush()

        # ---- 2) IP block ------------------------------------------------
        # POST /v1/ipam/blocks — ipam/router.py:2667
        block = api.json("POST", "/v1/ipam/blocks", json={
            "space_id": space_id,
            "network": m.seed.ip_block,
            "name": "perf-block",
            "description": "perf root block",
        }, ok=(201,))
        block_id = block["id"]
        created["ip_block"] = {"id": block_id, "network": str(block["network"])}
        log_event(log, logging.INFO, "created_block", block_id=block_id, network=m.seed.ip_block)
        _flush()

        # ---- 3) DHCP server group --------------------------------------
        # Reuse existing group (SPDDI_PERF_DHCP_GROUP_ID) if set — the kea agent is
        # already bound to it. Otherwise create a fresh group with a run-specific name.
        if existing_dhcp_group_id:
            # Verify the group exists (raises ApiError if not).
            dhcp_group = api.json("GET", f"/v1/dhcp/server-groups/{existing_dhcp_group_id}")
            dhcp_group_id = existing_dhcp_group_id
            created["dhcp_group"] = {"id": dhcp_group_id, "name": dhcp_group.get("name", ""),
                                     "reused": True}
        else:
            # POST /v1/dhcp/server-groups — dhcp/server_groups.py:166
            # socket mode "relay" for relay topology (Kea udp sockets), else "direct".
            dhcp_group = api.json("POST", "/v1/dhcp/server-groups", json={
                "name": f"perf-dhcp-{rp.run_id[:24]}",
                "description": "perf DHCP group",
                "mode": "standalone",
                "dhcp_socket_mode": "relay" if relay else "direct",
            }, ok=(201,))
            dhcp_group_id = dhcp_group["id"]
            created["dhcp_group"] = {"id": dhcp_group_id, "name": dhcp_group["name"]}
        log_event(log, logging.INFO, "dhcp_group_ready", group_id=dhcp_group_id,
                  reused=bool(existing_dhcp_group_id))
        _flush()

        # ---- 4) DNS server group ---------------------------------------
        # Reuse existing group (SPDDI_PERF_DNS_GROUP_ID) if set — the bind9 agent is
        # already bound to it. Otherwise create a fresh group.
        if existing_dns_group_id:
            # Verify the group exists (raises ApiError if not).
            dns_group = api.json("GET", f"/v1/dns/groups/{existing_dns_group_id}")
            dns_group_id = existing_dns_group_id
            created["dns_group"] = {"id": dns_group_id, "name": dns_group.get("name", ""),
                                    "reused": True}
        else:
            # POST /v1/dns/groups — dns/router.py:1021. is_recursive=False so the
            # authoritative-only constraint is set at the group level too (§4.9 L3).
            dns_group = api.json("POST", "/v1/dns/groups", json={
                "name": f"perf-dns-{rp.run_id[:26]}",
                "description": "perf DNS group",
                "group_type": "internal",
                "is_recursive": False,
            }, ok=(201,))
            dns_group_id = dns_group["id"]
            created["dns_group"] = {"id": dns_group_id, "name": dns_group["name"]}
        log_event(log, logging.INFO, "dns_group_ready", group_id=dns_group_id,
                  reused=bool(existing_dns_group_id))
        _flush()

        # ---- 5) Forward + reverse zones --------------------------------
        # get-or-create so re-running against a pre-seeded group is idempotent.
        # When the forward zone already existed we skip the bulk record load below
        # (the records from the prior run are still there).
        zone_id_by_name: dict[str, str] = {}
        forward_zone_was_created = True  # default: assume fresh; set False if any reused
        for zname in forward_zones:
            # GET-or-create /v1/dns/groups/{gid}/zones — dns/router.py:2786 (kind forward)
            zid, was_created = _get_or_create_zone(api, dns_group_id, zname, "forward", log)
            if not was_created:
                forward_zone_was_created = False
            zone_id_by_name[zname] = zid
            created["forward_zones"].append({"id": zid, "name": zname, "created": was_created})
        for rzname in reverse_zones:
            zid, was_created = _get_or_create_zone(api, dns_group_id, rzname, "reverse", log)
            zone_id_by_name[rzname] = zid
            created["reverse_zones"].append({"id": zid, "name": rzname, "created": was_created})
        log_event(log, logging.INFO, "zones_ready",
                  forward=len(forward_zones), reverse=len(reverse_zones),
                  forward_zone_was_created=forward_zone_was_created)
        _flush()

        # ---- 6) Subnets + scopes + pools -------------------------------
        for i, cidr in enumerate(subnet_cidrs):
            # POST /v1/ipam/subnets — ipam/router.py:3551. skip_reverse_zone=True
            # because we created the reverse zones ourselves with the chosen shape.
            sn = api.json("POST", "/v1/ipam/subnets", json={
                "space_id": space_id, "block_id": block_id, "network": cidr,
                "name": f"perf-subnet-{i}", "skip_reverse_zone": True,
            }, ok=(201,))
            subnet_id = sn["id"]
            sub_rec: dict[str, Any] = {"id": subnet_id, "network": cidr, "index": i}

            # POST /v1/dhcp/subnets/{id}/dhcp-scopes — dhcp/scopes.py:321
            scope_body: dict[str, Any] = {
                "group_id": dhcp_group_id,
                "name": f"perf-scope-{i}",
                # lease_time from manifest; T1 renew-timer is server-hardcoded to
                # 900s regardless (canonical.T1_RENEW_S; render_kea.py:641).
                "lease_time": m.scale.lease_time_s,
                "ddns_enabled": m.scale.ddns_enabled,
            }
            if relay:
                scope_body["relay_addresses"] = [giaddrs[i]]  # one giaddr per subnet (§3.1.2)
            scope = api.json("POST", f"/v1/dhcp/subnets/{subnet_id}/dhcp-scopes",
                             json=scope_body, ok=(201,))
            scope_id = scope["id"]

            # POST /v1/dhcp/scopes/{id}/pools — dhcp/pools.py:201 (large dynamic pool)
            pstart, pend = _pool_range(cidr, pool_fraction)
            pool = api.json("POST", f"/v1/dhcp/scopes/{scope_id}/pools", json={
                "name": f"perf-pool-{i}", "start_ip": pstart, "end_ip": pend,
                "pool_type": "dynamic",
            }, ok=(201,))

            sub_rec["scope_id"] = scope_id
            sub_rec["pool"] = {"id": pool["id"], "start_ip": pstart, "end_ip": pend}
            if relay:
                sub_rec["giaddr"] = giaddrs[i]
            created["subnets"].append(sub_rec)
            created["dhcp_scopes"].append({"id": scope_id, "subnet_id": subnet_id})
            log_event(log, logging.INFO, "created_subnet_scope_pool",
                      index=i, subnet=cidr, pool=f"{pstart}-{pend}")
            _flush()

        # ---- 7) Bulk-load the authoritative dataset (§4.9 Layer 1) -----
        # Skip when the forward zone already existed (i.e. we're re-using a pre-seeded
        # group from a prior run via SPDDI_PERF_DNS_GROUP_ID). Re-loading would cause
        # duplicate-record 409s that exceed the 1% failure cap and abort the seeder.
        if not forward_zone_was_created:
            log_event(log, logging.INFO, "bulk_load_skipped",
                      reason="forward zone already exists in reused group — records from prior seed run",
                      forward_zone=forward_zone)
            created["authoritative_records"].update({
                "planned": 0, "created": 0, "failed": 0, "skipped_no_zone": 0,
                "skipped_reason": "zone_reused",
            })
            loader = None
        else:
            total_records = int(m.seed.dns.authoritative_records)
            plan = _gen_records(total_records, forward_zone, subnet_cidrs)
            # Map each record to its target zone id (forward zone or reverse zone).
            items: list[tuple[str, dict[str, Any]]] = []
            skipped_no_zone = 0
            fwd_zone_id = zone_id_by_name.get(forward_zone)
            for rec in plan:
                if rec["kind"] == "forward":
                    if fwd_zone_id is None:
                        skipped_no_zone += 1
                        continue
                    # name relative to zone: strip the trailing ".<zone>" suffix.
                    items.append((fwd_zone_id, {**rec, "name": rec["name"]}))
                else:  # reverse PTR
                    rz = _reverse_zone_for_ip(rec["ip"], reverse_zones)
                    rz_id = zone_id_by_name.get(rz) if rz else None
                    if rz_id is None:
                        skipped_no_zone += 1
                        continue
                    # PTR name relative to the reverse zone (strip the zone suffix).
                    rel = rec["name"][: -(len(rz) + 1)] if rec["name"].endswith("." + rz) else rec["name"]
                    items.append((rz_id, {**rec, "name": rel}))

            log_event(log, logging.INFO, "bulk_load_start",
                      records_planned=len(items), skipped_no_zone=skipped_no_zone,
                      workers=RECORD_WORKERS,
                      note="per-record POST path; see open_item re: §4.9 bulk fast-path")
            loader = _BulkLoader(api, log, dns_group_id)
            loader.load(items)
            created["authoritative_records"].update({
                "planned": len(items),
                "created": loader.created,
                "failed": loader.failed,
                "skipped_no_zone": skipped_no_zone,
                "first_errors": loader._first_errors,
            })
        _flush()

    # Normalized block consumed by the DNS query-set generator (§4.5 correlation):
    # gen_dns_queryset reads sm["dns"].{forward_zones,reverse_zones} (names),
    # sm["subnet_cidrs"], sm["pool_ranges"] — emit them so it queries the REAL seeded
    # zones/pools instead of silently re-deriving from the run manifest.
    created["dns"] = {"forward_zones": list(forward_zones), "reverse_zones": list(reverse_zones)}
    created["subnet_cidrs"] = list(subnet_cidrs)
    created["pool_ranges"] = [[s["pool"]["start_ip"], s["pool"]["end_ip"]]
                             for s in created["subnets"] if s.get("pool")]
    created["completed_at"] = utc_now_iso()
    _flush()
    log_event(log, logging.INFO, "seed_complete",
              subnets=len(created["subnets"]),
              records_created=created["authoritative_records"].get("created", 0),
              records_failed=created["authoritative_records"].get("failed", 0),
              t1_renew_s=canonical.T1_RENEW_S)
    if loader is not None:
        # Auth failed mid-load → the dataset is incomplete by an unknowable amount; fail
        # hard rather than letting the run treat a partial seed as healthy progress.
        if loader.aborted:
            log.error("bulk record load aborted on an auth failure — token expired/invalid")
            return 5
        # A bulk-load with substantial failures is a real problem for the closed-loop
        # DNS test, but we still write the manifest so downstream tooling can see it.
        planned = created["authoritative_records"].get("planned", 0)
        if created["authoritative_records"].get("failed", 0) > max(10, planned * 0.01):
            log.error("bulk record load had > 1%% failures — DNS test may not be closed-loop")
            return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Seed the IPAM/DHCP/DNS scaffold for a perf run.")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args(argv)

    rp = RunPaths.for_run(args.run_id, args.run_root)
    rp.ensure_dirs()
    log = get_logger("spddi_perf.seeder.scaffold", run_id=args.run_id,
                     logfile=rp.worker_log("seed_scaffold"))
    m = manifest_mod.load(args.manifest)
    try:
        return run(rp, m, log)
    except Exception as exc:  # noqa: BLE001 — surface as a non-zero rc the controller logs
        log.exception("seed_scaffold failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
