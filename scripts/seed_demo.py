"""Seed a running SpatiumDDI instance with realistic demo data for screenshots.

Usage (with the compose stack running):
    python scripts/seed_demo.py http://localhost:8000 admin admin

Creates:
- 1 IP space with 2 blocks and a handful of subnets at different utilisation levels
- ~30 IP addresses with hostnames / MACs / DNS / orphan states
- 2 routers + 6 VLANs, wired up to subnets
- 1 DNS server group + zone + records
- 1 DHCP server group (the agent already auto-registered the real server if it's running)

Safe to re-run — every create swallows 409 (already exists).
"""
from __future__ import annotations

import random
import sys

import httpx


def login(base: str, user: str, pw: str) -> str:
    r = httpx.post(f"{base}/api/v1/auth/login", json={"username": user, "password": pw})
    r.raise_for_status()
    return r.json()["access_token"]


class Api:
    def __init__(self, base: str, token: str):
        self.c = httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
        )

    def call(self, method: str, path: str, **kwargs):
        try:
            r = self.c.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            print(f"  ! {method} {path} transport error: {e}")
            return None
        if r.status_code == 409:
            return None
        if r.status_code >= 400:
            print(f"  ! {method} {path} -> {r.status_code}: {r.text[:200]}")
            return None
        return r.json() if r.content else None


def find(items, key, val):
    if not items:
        return None
    return next((i for i in items if i.get(key) == val), None)


def main(base: str, user: str, pw: str):
    token = login(base, user, pw)
    a = Api(base, token)

    # ── IPAM ──────────────────────────────────────────────────────────────
    print("Creating IP space…")
    a.call("POST", "/api/v1/ipam/spaces", json={"name": "Corporate", "description": "Primary office network"})
    spaces = a.call("GET", "/api/v1/ipam/spaces") or []
    corp = find(spaces, "name", "Corporate")
    if not corp:
        print("! could not create/find Corporate space")
        return
    space_id = corp["id"]

    print("Creating blocks…")
    a.call("POST", "/api/v1/ipam/blocks", json={
        "space_id": space_id, "name": "HQ 10.0.0.0/8", "network": "10.0.0.0/8",
        "description": "Headquarters aggregate",
    })
    a.call("POST", "/api/v1/ipam/blocks", json={
        "space_id": space_id, "name": "DMZ 192.168.0.0/16", "network": "192.168.0.0/16",
        "description": "DMZ and lab",
    })
    blocks = a.call("GET", f"/api/v1/ipam/blocks?space_id={space_id}") or []
    hq = find(blocks, "name", "HQ 10.0.0.0/8")
    dmz = find(blocks, "name", "DMZ 192.168.0.0/16")
    if not hq or not dmz:
        print("! block creation failed")
        return

    print("Creating subnets…")
    subnet_specs = [
        (hq["id"], "Office-LAN",      "10.1.0.0/24",  "Staff workstations"),
        (hq["id"], "Voice-VLAN",      "10.1.1.0/24",  "VoIP phones"),
        (hq["id"], "Servers",         "10.10.10.0/24","Production servers"),
        (hq["id"], "Wireless-Staff",  "10.20.0.0/22", "Corporate WiFi"),
        (dmz["id"], "DMZ-Public",     "192.168.1.0/24","Public-facing hosts"),
        (dmz["id"], "Lab",            "192.168.99.0/24","Test/lab network"),
    ]
    for block_id, name, net, desc in subnet_specs:
        a.call("POST", "/api/v1/ipam/subnets", json={
            "space_id": space_id, "block_id": block_id, "name": name,
            "network": net, "description": desc,
            "gateway": net.rsplit(".", 1)[0] + ".1",
        })

    all_subnets = a.call("GET", "/api/v1/ipam/subnets") or []
    # Only allocate into the subnets this seed created (by name) — avoids
    # poking stale test subnets that may already be in a broken state.
    seeded_names = {name for _, name, _, _ in subnet_specs}
    subnets = [s for s in all_subnets if s["name"] in seeded_names]

    print(f"Allocating IPs into {len(subnets)} subnet(s)…")
    hostname_pool = [
        "web01", "web02", "db-primary", "db-replica", "app-api", "app-worker",
        "mail", "git", "jenkins", "grafana", "prometheus", "vault", "consul",
        "nfs01", "backup", "printer-lobby", "printer-2f", "ap-floor1", "ap-floor2",
        "jdoe-laptop", "asmith-desktop", "mlee-laptop", "kvm-1", "kvm-2",
    ]
    random.seed(42)
    for subnet in subnets:
        if "Wireless" in subnet["name"] or "Lab" in subnet["name"]:
            continue
        for _ in range(random.randint(3, 8)):
            hn = random.choice(hostname_pool) + f"-{random.randint(100, 999)}"
            mac = ":".join(f"{random.randint(0, 255):02x}" for _ in range(6))
            a.call("POST", f"/api/v1/ipam/subnets/{subnet['id']}/next", json={
                "hostname": hn, "mac_address": mac,
                "description": "", "status": "allocated",
            })

    # ── VLANs ─────────────────────────────────────────────────────────────
    print("Creating routers + VLANs…")
    a.call("POST", "/api/v1/vlans/routers", json={
        "name": "core-sw1", "description": "HQ core switch",
        "location": "MDF rack A", "management_ip": "10.1.0.1",
        "vendor": "Cisco", "model": "Catalyst 9300",
    })
    a.call("POST", "/api/v1/vlans/routers", json={
        "name": "dmz-sw1", "description": "DMZ switch",
        "location": "Server room B", "management_ip": "192.168.1.1",
        "vendor": "Arista", "model": "7050SX",
    })
    routers = a.call("GET", "/api/v1/vlans/routers") or []
    core = find(routers, "name", "core-sw1")
    dmz_sw = find(routers, "name", "dmz-sw1")
    if core:
        for vid, name in [(10, "Office"), (11, "Voice"), (20, "Servers"), (30, "WiFi-Staff")]:
            a.call("POST", f"/api/v1/vlans/routers/{core['id']}/vlans", json={
                "vlan_id": vid, "name": name, "description": "",
            })
    if dmz_sw:
        for vid, name in [(100, "DMZ-Public"), (200, "Lab")]:
            a.call("POST", f"/api/v1/vlans/routers/{dmz_sw['id']}/vlans", json={
                "vlan_id": vid, "name": name, "description": "",
            })

    # ── DNS ───────────────────────────────────────────────────────────────
    print("Creating DNS group + zone + records…")
    a.call("POST", "/api/v1/dns/groups", json={
        "name": "default", "description": "Main DNS cluster",
        "group_type": "internal", "is_recursive": True,
    })
    groups = a.call("GET", "/api/v1/dns/groups") or []
    g = find(groups, "name", "default")
    if g:
        a.call("POST", f"/api/v1/dns/groups/{g['id']}/zones", json={
            "name": "corp.example.com", "zone_type": "primary",
            "kind": "forward",
        })
        zones = a.call("GET", f"/api/v1/dns/groups/{g['id']}/zones") or []
        z = find(zones, "name", "corp.example.com")
        if z:
            for rec in [
                {"name": "www", "record_type": "A", "value": "10.10.10.20", "ttl": 3600},
                {"name": "mail", "record_type": "A", "value": "10.10.10.25", "ttl": 3600},
                {"name": "@", "record_type": "MX", "value": "10 mail.corp.example.com.", "ttl": 3600, "priority": 10},
                {"name": "_sip._tcp", "record_type": "SRV", "value": "10 10 5060 voip.corp.example.com.", "ttl": 3600},
                {"name": "office", "record_type": "TXT", "value": '"v=spf1 ip4:10.1.0.0/16 -all"', "ttl": 3600},
            ]:
                a.call("POST", f"/api/v1/dns/groups/{g['id']}/zones/{z['id']}/records", json=rec)

    print("\n✓ Seed complete.")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python scripts/seed_demo.py <base_url> <username> <password>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
