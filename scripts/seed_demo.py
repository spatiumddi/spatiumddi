"""Seed a running SpatiumDDI instance with realistic demo data for screenshots.

Usage (with the compose stack running):
    python3 scripts/seed_demo.py http://localhost:8000 admin admin

Creates a representative slice of every first-class entity that's
shipped to date so screenshots look populated and the AI Copilot's
dynamic context block has real numbers to summarise. Sections:

* DNS — group + forward zone + reverse zone + records
* DHCP — server group, scope on one subnet, dynamic pool
* ASN — operator's local AS + a couple of upstream peers
* VRF — default + guest + iot
* IP space — Corporate (with VRF + ASN), Guest, IoT
* IP blocks + subnets, with VLAN / gateway
* IP addresses — random allocation across most subnets
* Network devices — SNMP-stubbed (is_active=False so beat doesn't poll)
* VLANs — legacy router/VLAN entries (the screenshot UI still uses these)
* Domains — corp.example.com (matches the DNS zone) + example.com
* Custom fields — one IP-level + one subnet-level
* IPAM templates — one block + one subnet template
* Alert rules — utilization, server-unreachable, domain-expiring
* AI prompts — three shared triage prompts (issue #90 Phase 2)

Every section is idempotent — the script swallows 409 from already-
existing rows, and PATCHes spaces / blocks where the FK targets need
to converge on a re-run after new entities exist.

Out of scope: AI providers (secrets), webhooks (per-deployment URLs),
audit-forward targets (per-deployment endpoints), API tokens
(operator-scoped). Those need real creds from the operator.
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
    """Thin idempotent JSON-over-HTTP client.

    409 (already exists) is swallowed silently — that's what makes the
    seed re-runnable. Every other 4xx / 5xx is logged but doesn't
    abort the run, since later sections often don't depend on earlier
    failures (e.g. domain creation doesn't need DHCP to succeed).
    """

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


# ── Section: DNS ──────────────────────────────────────────────────────────


def seed_dns(a: Api) -> tuple[str | None, str | None, str | None]:
    """Returns ``(group_id, forward_zone_id, reverse_zone_id)`` so the
    space + domain rows can pin them.
    """
    print("Creating DNS group + zones + records…")
    a.call(
        "POST",
        "/api/v1/dns/groups",
        json={
            "name": "default",
            "description": "Main DNS cluster",
            "group_type": "internal",
            "is_recursive": True,
        },
    )
    groups = a.call("GET", "/api/v1/dns/groups") or []
    g = find(groups, "name", "default")
    if not g:
        return None, None, None
    gid = g["id"]

    # Forward zone — the space defaults to this and the AI copilot
    # uses it as a worked example for "list my DNS records" queries.
    a.call(
        "POST",
        f"/api/v1/dns/groups/{gid}/zones",
        json={"name": "corp.example.com", "zone_type": "primary", "kind": "forward"},
    )
    # Reverse zone — DDNS / PTR auto-population queries traverse this.
    a.call(
        "POST",
        f"/api/v1/dns/groups/{gid}/zones",
        json={"name": "10.in-addr.arpa", "zone_type": "primary", "kind": "reverse"},
    )
    zones = a.call("GET", f"/api/v1/dns/groups/{gid}/zones") or []
    fz = find(zones, "name", "corp.example.com")
    rz = find(zones, "name", "10.in-addr.arpa")
    fz_id = fz["id"] if fz else None
    rz_id = rz["id"] if rz else None

    if fz_id:
        for rec in [
            {"name": "www", "record_type": "A", "value": "10.10.10.20", "ttl": 3600},
            {"name": "mail", "record_type": "A", "value": "10.10.10.25", "ttl": 3600},
            {
                "name": "@",
                "record_type": "MX",
                "value": "10 mail.corp.example.com.",
                "ttl": 3600,
                "priority": 10,
            },
            {
                "name": "_sip._tcp",
                "record_type": "SRV",
                "value": "10 10 5060 voip.corp.example.com.",
                "ttl": 3600,
            },
            {
                "name": "office",
                "record_type": "TXT",
                "value": '"v=spf1 ip4:10.1.0.0/16 -all"',
                "ttl": 3600,
            },
        ]:
            a.call("POST", f"/api/v1/dns/groups/{gid}/zones/{fz_id}/records", json=rec)

    return gid, fz_id, rz_id


# ── Section: DHCP ─────────────────────────────────────────────────────────


def seed_dhcp_group(a: Api) -> str | None:
    print("Creating DHCP server group…")
    a.call(
        "POST",
        "/api/v1/dhcp/server-groups",
        json={
            "name": "default",
            "description": "Default DHCP group",
            "mode": "hot-standby",
        },
    )
    groups = a.call("GET", "/api/v1/dhcp/server-groups") or []
    g = find(groups, "name", "default")
    return g["id"] if g else None


def seed_dhcp_scope(a: Api, group_id: str | None, subnets: list[dict]):
    """Wire a scope on the Servers subnet so the DHCP page isn't empty.

    The subnet has gateway + DDNS already inherited from the space; the
    scope just configures the lease window + a small dynamic pool.
    """
    if not group_id or not subnets:
        return
    target = find(subnets, "name", "Servers")
    if not target:
        return
    print("Creating DHCP scope + pool on Servers subnet…")
    scope = a.call(
        "POST",
        f"/api/v1/dhcp/subnets/{target['id']}/dhcp-scopes",
        json={
            "server_group_id": group_id,
            "name": "Servers DHCP",
            "lease_time": 86400,
            "enabled": True,
            "ddns_enabled": True,
        },
    )
    if scope and scope.get("id"):
        # Small dynamic pool so screenshots show "▼ start / ▲ end" rows
        # in the IPAM table.
        a.call(
            "POST",
            f"/api/v1/dhcp/scopes/{scope['id']}/pools",
            json={
                "name": "Default pool",
                "start_address": "10.10.10.100",
                "end_address": "10.10.10.149",
                "kind": "dynamic",
            },
        )


# ── Section: ASNs + VRFs ──────────────────────────────────────────────────


def seed_asns(a: Api) -> dict[str, str]:
    """Returns ``{name: id}`` for the seeded ASNs."""
    print("Creating ASNs…")
    rows = [
        {
            "number": 64512,
            "name": "Corporate",
            "description": "Operator's private AS (RFC 6996 16-bit private range)",
        },
        {
            "number": 13335,
            "name": "Cloudflare",
            "description": "Upstream peer — public",
            "holder_org": "Cloudflare, Inc.",
        },
        {
            "number": 174,
            "name": "Cogent",
            "description": "Transit provider — public",
            "holder_org": "Cogent Communications",
        },
    ]
    for body in rows:
        a.call("POST", "/api/v1/asns", json=body)
    listed = a.call("GET", "/api/v1/asns") or {}
    items = listed.get("items", listed) if isinstance(listed, dict) else listed
    return {row["name"]: row["id"] for row in items if row.get("name")}


def seed_peerings(a: Api, asns: dict[str, str]):
    if "Corporate" not in asns:
        return
    print("Creating BGP peerings…")
    local = asns["Corporate"]
    for peer_name, kind in (
        ("Cloudflare", "peer"),
        ("Cogent", "provider"),
    ):
        peer = asns.get(peer_name)
        if not peer:
            continue
        a.call(
            "POST",
            "/api/v1/asns/peerings",
            json={
                "local_asn_id": local,
                "peer_asn_id": peer,
                "relationship_type": kind,
                "description": f"{peer_name} {kind}",
            },
        )


def seed_vrfs(a: Api, asns: dict[str, str]) -> dict[str, str]:
    print("Creating VRFs…")
    corp = asns.get("Corporate")
    rows = [
        {
            "name": "default",
            "description": "Underlay VRF for the corporate network",
            "asn_id": corp,
            "route_distinguisher": "64512:1" if corp else None,
            "import_targets": ["64512:1"] if corp else [],
            "export_targets": ["64512:1"] if corp else [],
        },
        {
            "name": "guest",
            "description": "Guest WiFi — internet-only, no internal access",
            "asn_id": corp,
            "route_distinguisher": "64512:100" if corp else None,
        },
        {
            "name": "iot",
            "description": "IoT segment — restricted east-west",
            "asn_id": corp,
            "route_distinguisher": "64512:200" if corp else None,
        },
    ]
    for body in rows:
        # Strip None values so the API's strict-RD validator doesn't
        # 422 when ASN seeding failed and we passed RD=None.
        clean = {k: v for k, v in body.items() if v is not None}
        a.call("POST", "/api/v1/vrfs", json=clean)
    listed = a.call("GET", "/api/v1/vrfs") or []
    return {row["name"]: row["id"] for row in listed if row.get("name")}


# ── Section: IPAM ─────────────────────────────────────────────────────────


def seed_spaces_blocks_subnets(
    a: Api,
    *,
    dns_group_id: str | None,
    dns_zone_id: str | None,
    dhcp_group_id: str | None,
    vrfs: dict[str, str],
    asns: dict[str, str],
) -> tuple[str | None, list[dict]]:
    """Returns ``(corporate_space_id, list_of_seeded_subnets)``."""
    print("Creating IP space…")
    space_payload: dict = {
        "name": "Corporate",
        "description": "Primary office network",
    }
    if dns_group_id:
        space_payload["dns_group_ids"] = [dns_group_id]
    if dns_zone_id:
        space_payload["dns_zone_id"] = dns_zone_id
    if dhcp_group_id:
        space_payload["dhcp_server_group_id"] = dhcp_group_id
    if vrfs.get("default"):
        space_payload["vrf_id"] = vrfs["default"]
    if asns.get("Corporate"):
        space_payload["asn_id"] = asns["Corporate"]
    a.call("POST", "/api/v1/ipam/spaces", json=space_payload)

    spaces = a.call("GET", "/api/v1/ipam/spaces") or []
    corp = find(spaces, "name", "Corporate")
    if not corp:
        print("! could not create/find Corporate space")
        return None, []
    space_id = corp["id"]

    # On re-run, converge stale FK pointers if previous-run entities now
    # exist. Without this, a fresh seed against an already-seeded DB
    # leaves dangling values.
    patch: dict = {}
    if dns_group_id and corp.get("dns_group_ids") != [dns_group_id]:
        patch["dns_group_ids"] = [dns_group_id]
    if dns_zone_id and corp.get("dns_zone_id") != dns_zone_id:
        patch["dns_zone_id"] = dns_zone_id
    if dhcp_group_id and corp.get("dhcp_server_group_id") != dhcp_group_id:
        patch["dhcp_server_group_id"] = dhcp_group_id
    if vrfs.get("default") and corp.get("vrf_id") != vrfs["default"]:
        patch["vrf_id"] = vrfs["default"]
    if asns.get("Corporate") and corp.get("asn_id") != asns["Corporate"]:
        patch["asn_id"] = asns["Corporate"]
    if patch:
        a.call("PUT", f"/api/v1/ipam/spaces/{space_id}", json=patch)

    print("Creating blocks…")
    a.call(
        "POST",
        "/api/v1/ipam/blocks",
        json={
            "space_id": space_id,
            "name": "HQ 10.0.0.0/8",
            "network": "10.0.0.0/8",
            "description": "Headquarters aggregate",
        },
    )
    a.call(
        "POST",
        "/api/v1/ipam/blocks",
        json={
            "space_id": space_id,
            "name": "DMZ 192.168.0.0/16",
            "network": "192.168.0.0/16",
            "description": "DMZ and lab",
        },
    )
    blocks = a.call("GET", f"/api/v1/ipam/blocks?space_id={space_id}") or []
    hq = find(blocks, "name", "HQ 10.0.0.0/8")
    dmz = find(blocks, "name", "DMZ 192.168.0.0/16")
    if not hq or not dmz:
        print("! block creation failed")
        return space_id, []

    print("Creating subnets…")
    subnet_specs = [
        (hq["id"], "Office-LAN", "10.1.0.0/24", "Staff workstations", 10),
        (hq["id"], "Voice-VLAN", "10.1.1.0/24", "VoIP phones", 11),
        (hq["id"], "Servers", "10.10.10.0/24", "Production servers", 20),
        (hq["id"], "Wireless-Staff", "10.20.0.0/22", "Corporate WiFi", 30),
        (dmz["id"], "DMZ-Public", "192.168.1.0/24", "Public-facing hosts", 100),
        (dmz["id"], "Lab", "192.168.99.0/24", "Test/lab network", 200),
    ]
    for block_id, name, net, desc, vlan_id in subnet_specs:
        a.call(
            "POST",
            "/api/v1/ipam/subnets",
            json={
                "space_id": space_id,
                "block_id": block_id,
                "name": name,
                "network": net,
                "description": desc,
                "gateway": net.rsplit(".", 1)[0] + ".1",
                "vlan_id": vlan_id,
            },
        )

    all_subnets = a.call("GET", "/api/v1/ipam/subnets") or []
    seeded_names = {name for _, name, _, _, _ in subnet_specs}
    subnets = [s for s in all_subnets if s["name"] in seeded_names]
    return space_id, subnets


def seed_addresses(a: Api, subnets: list[dict]):
    print(f"Allocating IPs into {len(subnets)} subnet(s)…")
    hostname_pool = [
        "web01",
        "web02",
        "db-primary",
        "db-replica",
        "app-api",
        "app-worker",
        "mail",
        "git",
        "jenkins",
        "grafana",
        "prometheus",
        "vault",
        "consul",
        "nfs01",
        "backup",
        "printer-lobby",
        "printer-2f",
        "ap-floor1",
        "ap-floor2",
        "jdoe-laptop",
        "asmith-desktop",
        "mlee-laptop",
        "kvm-1",
        "kvm-2",
    ]
    random.seed(42)
    for subnet in subnets:
        # Wireless / Lab stay sparse on purpose so utilization charts
        # show variance in the dashboard heatmap.
        if "Wireless" in subnet["name"] or "Lab" in subnet["name"]:
            continue
        for _ in range(random.randint(3, 8)):
            hn = random.choice(hostname_pool) + f"-{random.randint(100, 999)}"
            mac = ":".join(f"{random.randint(0, 255):02x}" for _ in range(6))
            a.call(
                "POST",
                f"/api/v1/ipam/subnets/{subnet['id']}/next",
                json={
                    "hostname": hn,
                    "mac_address": mac,
                    "description": "",
                    "status": "allocated",
                },
            )


# ── Section: Network devices + VLANs ──────────────────────────────────────


def seed_network_devices(a: Api, space_id: str | None):
    """SNMP-stubbed devices.

    ``is_active=False`` so the SNMP-poll beat task doesn't actually
    probe these hostnames (which would 404 on a bare seed). Operators
    flip them active once they've pointed each row at a real switch.
    """
    if not space_id:
        return
    print("Creating SNMP-stubbed network devices…")
    rows = [
        {
            "name": "core-sw1",
            "hostname": "core-sw1.corp.example.com",
            "ip_address": "10.1.0.2",
            "device_type": "switch",
            "description": "HQ core switch — Cisco Catalyst 9300",
            "snmp_version": "v2c",
            "community": "public-readonly",
            "ip_space_id": space_id,
            "is_active": False,
        },
        {
            "name": "dmz-sw1",
            "hostname": "dmz-sw1.corp.example.com",
            "ip_address": "192.168.1.2",
            "device_type": "switch",
            "description": "DMZ switch — Arista 7050SX",
            "snmp_version": "v2c",
            "community": "public-readonly",
            "ip_space_id": space_id,
            "is_active": False,
        },
        {
            "name": "edge-fw1",
            "hostname": "edge-fw1.corp.example.com",
            "ip_address": "10.0.0.254",
            "device_type": "firewall",
            "description": "Internet edge firewall",
            "snmp_version": "v2c",
            "community": "public-readonly",
            "ip_space_id": space_id,
            "is_active": False,
        },
    ]
    for body in rows:
        a.call("POST", "/api/v1/network-devices", json=body)


def seed_legacy_vlans(a: Api):
    """The legacy VLANs page (under Network → VLANs) still uses the
    router-keyed model. Seed two routers + a handful of VLANs so the
    page isn't empty for demos.
    """
    print("Creating legacy router + VLAN entries…")
    a.call(
        "POST",
        "/api/v1/vlans/routers",
        json={
            "name": "core-sw1",
            "description": "HQ core switch",
            "location": "MDF rack A",
            "management_ip": "10.1.0.1",
            "vendor": "Cisco",
            "model": "Catalyst 9300",
        },
    )
    a.call(
        "POST",
        "/api/v1/vlans/routers",
        json={
            "name": "dmz-sw1",
            "description": "DMZ switch",
            "location": "Server room B",
            "management_ip": "192.168.1.1",
            "vendor": "Arista",
            "model": "7050SX",
        },
    )
    routers = a.call("GET", "/api/v1/vlans/routers") or []
    core = find(routers, "name", "core-sw1")
    dmz_sw = find(routers, "name", "dmz-sw1")
    if core:
        for vid, name in [
            (10, "Office"),
            (11, "Voice"),
            (20, "Servers"),
            (30, "WiFi-Staff"),
        ]:
            a.call(
                "POST",
                f"/api/v1/vlans/routers/{core['id']}/vlans",
                json={"vlan_id": vid, "name": name, "description": ""},
            )
    if dmz_sw:
        for vid, name in [(100, "DMZ-Public"), (200, "Lab")]:
            a.call(
                "POST",
                f"/api/v1/vlans/routers/{dmz_sw['id']}/vlans",
                json={"vlan_id": vid, "name": name, "description": ""},
            )


# ── Section: Domains ──────────────────────────────────────────────────────


def seed_domains(a: Api):
    """RDAP refresh + alert-rule expiry trigger off these.

    Keep the list small and realistic — we don't want the seed to
    flood the operator's Domain page with dozens of rows.
    """
    print("Creating domains…")
    rows = [
        {
            "name": "corp.example.com",
            "expected_nameservers": ["ns1.corp.example.com", "ns2.corp.example.com"],
        },
        {
            "name": "example.com",
            "expected_nameservers": ["a.iana-servers.net", "b.iana-servers.net"],
        },
    ]
    for body in rows:
        a.call("POST", "/api/v1/domains", json=body)


# ── Section: Custom fields + IPAM templates ───────────────────────────────


def seed_custom_fields(a: Api):
    """One field per resource type so the per-resource admin pages
    have a worked example.
    """
    print("Creating custom fields…")
    rows = [
        {
            "resource_type": "ip_address",
            "name": "owner",
            "label": "Owner",
            "field_type": "text",
            "is_required": False,
            "is_searchable": True,
            "default_value": "",
            "display_order": 0,
            "description": "Operator team or person responsible for this IP",
            "options": None,
        },
        {
            "resource_type": "subnet",
            "name": "environment",
            "label": "Environment",
            "field_type": "select",
            "is_required": False,
            "is_searchable": True,
            "default_value": "production",
            "display_order": 0,
            "description": "Subnet environment classification",
            "options": ["production", "staging", "development", "lab"],
        },
    ]
    for body in rows:
        a.call("POST", "/api/v1/custom-fields", json=body)


def seed_ipam_templates(a: Api, dns_group_id: str | None, dhcp_group_id: str | None):
    """Two templates — one block-shaped (with child layout) + one
    subnet-shaped — so the IPAM Templates admin page has something
    operators can click "apply".
    """
    print("Creating IPAM templates…")
    block_template: dict = {
        "name": "Standard /24 block",
        "description": "Three /27 subnets carved out — staff / phones / servers",
        "applies_to": "block",
        "tags": {},
        "custom_fields": {},
        "ddns_enabled": True,
        "ddns_hostname_policy": "leave_unchanged",
        "child_layout": {
            "subnets": [
                {"prefix": "27", "name_template": "Staff-{n}", "description": ""},
                {"prefix": "27", "name_template": "Phones-{n}", "description": ""},
                {"prefix": "27", "name_template": "Servers-{n}", "description": ""},
            ]
        },
    }
    subnet_template: dict = {
        "name": "Standard subnet",
        "description": "Default DNS / DHCP / DDNS for new subnets",
        "applies_to": "subnet",
        "tags": {},
        "custom_fields": {},
        "ddns_enabled": True,
        "ddns_hostname_policy": "leave_unchanged",
    }
    if dns_group_id:
        block_template["dns_group_id"] = dns_group_id
        subnet_template["dns_group_id"] = dns_group_id
    if dhcp_group_id:
        block_template["dhcp_group_id"] = dhcp_group_id
        subnet_template["dhcp_group_id"] = dhcp_group_id
    a.call("POST", "/api/v1/ipam/templates", json=block_template)
    a.call("POST", "/api/v1/ipam/templates", json=subnet_template)


# ── Section: Alerts ───────────────────────────────────────────────────────


def seed_alert_rules(a: Api):
    """Three rules covering the three families of subjects the
    evaluator currently understands. Disabled by default — operators
    flip them on once they've configured an audit-forward target.
    """
    print("Creating alert rules…")
    rows = [
        {
            "name": "Subnet 90% utilised",
            "description": "Fires when any tracked subnet hits ≥ 90% utilisation.",
            "rule_type": "subnet_utilization",
            "threshold_percent": 90,
            "severity": "warning",
            "enabled": False,
            "notify_syslog": False,
            "notify_webhook": True,
            "notify_smtp": False,
        },
        {
            "name": "DNS server unreachable",
            "description": "Fires when a DNS server's health check fails.",
            "rule_type": "server_unreachable",
            "server_type": "dns",
            "severity": "critical",
            "enabled": False,
            "notify_syslog": True,
            "notify_webhook": True,
            "notify_smtp": False,
        },
        {
            "name": "Domain expiring within 30 days",
            "description": "Fires when a tracked domain is < 30 d from expiry.",
            "rule_type": "domain_expiring",
            "threshold_days": 30,
            "severity": "warning",
            "enabled": False,
            "notify_syslog": False,
            "notify_webhook": True,
            "notify_smtp": True,
        },
    ]
    for body in rows:
        a.call("POST", "/api/v1/alerts/rules", json=body)


# ── Section: AI prompts (issue #90 Phase 2) ───────────────────────────────


def seed_ai_prompts(a: Api):
    """A few shared prompts the operator's Copilot drawer will surface
    in its 'Prompts ▾' picker.
    """
    print("Creating shared AI prompts…")
    rows = [
        {
            "name": "Daily IPAM triage",
            "description": "Walk every subnet ≥ 80% utilised and list resize candidates.",
            "prompt_text": (
                "Walk through every subnet whose utilisation is at or above 80%. "
                "For each, list the current size, current allocated count, and "
                "suggest whether resize, split, or new-block is the right move. "
                "Group by IPSpace."
            ),
            "is_shared": True,
        },
        {
            "name": "Find static / DHCP collisions",
            "description": "List statically-assigned IPs that fall inside an active DHCP scope.",
            "prompt_text": (
                "Find every IP address with status='allocated' or 'reserved' that "
                "falls inside an active DHCP scope's dynamic pool range. Group by "
                "subnet. For each collision, name the static row, the scope, and "
                "the pool range it conflicts with."
            ),
            "is_shared": True,
        },
        {
            "name": "Audit log triage",
            "description": "Summarise the last hour of audit events grouped by user.",
            "prompt_text": (
                "Summarise the last hour of audit log events. Group by user. "
                "Highlight anything that looks unusual (off-hours activity, "
                "cross-resource bursts, repeated failures). Keep it under 200 words."
            ),
            "is_shared": True,
        },
    ]
    for body in rows:
        a.call("POST", "/api/v1/ai/prompts", json=body)


# ── Driver ────────────────────────────────────────────────────────────────


def main(base: str, user: str, pw: str):
    token = login(base, user, pw)
    a = Api(base, token)

    dns_group_id, dns_fwd_zone_id, _dns_rev_zone_id = seed_dns(a)
    dhcp_group_id = seed_dhcp_group(a)

    # ASNs / VRFs come before the IP space so the space's vrf_id /
    # asn_id pointers can resolve on first creation rather than via
    # the re-run patch.
    asns = seed_asns(a)
    seed_peerings(a, asns)
    vrfs = seed_vrfs(a, asns)

    space_id, subnets = seed_spaces_blocks_subnets(
        a,
        dns_group_id=dns_group_id,
        dns_zone_id=dns_fwd_zone_id,
        dhcp_group_id=dhcp_group_id,
        vrfs=vrfs,
        asns=asns,
    )
    seed_addresses(a, subnets)
    seed_dhcp_scope(a, dhcp_group_id, subnets)

    seed_network_devices(a, space_id)
    seed_legacy_vlans(a)

    seed_domains(a)
    seed_custom_fields(a)
    seed_ipam_templates(a, dns_group_id, dhcp_group_id)
    seed_alert_rules(a)
    seed_ai_prompts(a)

    print("\n✓ Seed complete.")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python3 scripts/seed_demo.py <base_url> <username> <password>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
