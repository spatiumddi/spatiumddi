# DNS Feature Specification

> **Implementation status (snapshot):** Full CRUD for groups / servers / zones / records / views / ACLs / trust anchors; BIND9 driver with TSIG + RFC 2136 dynamic updates; agent auto-registration and long-poll config sync with ETag; RPZ blocklists actively rendered by the agent (nxdomain / sinkhole / redirect / passthru; wildcard + exceptions); **curated 14-source RPZ blocklist catalog** with one-click subscribe; per-entry `reason` and `is_wildcard` toggles; zone import/export (RFC 1035); **conditional forwarders as a first-class zone type**; **zone delegation wizard** (auto-stamps NS + glue records in the parent zone); **four starter zone-template wizards** (Email / Active Directory / Web / k8s external-dns target); **operator-managed TSIG keys** with Fernet-encrypted secrets and one-shot reveal modal; query logging + **clickable analytics strip** (top qnames + top clients + qtype distribution); **multi-resolver propagation check** (Cloudflare / Google / Quad9 / OpenDNS in parallel); **BIND9 catalog zones (RFC 9432)** with producer / consumer roles auto-derived from the group's primary; per-server zone serial reporting + drift pill; health checks; IPAM ↔ DNS drift detection & reconciliation (`Check DNS Sync` on subnet/block/space); reverse-zone auto-create + backfill; **Windows DNS driver shipped** — Path A (agentless, RFC 2136) and Path B (agentless, WinRM + PowerShell for zone CRUD and zone-record pull that sidesteps AXFR); group-level "Sync with Servers" button performs bi-directional zone reconciliation; **BIND9 Response Rate Limiting (RRL) + amplification toggles** (responses-per-second / window / slip / qps-scale / exempt-clients / log-only dry-run + minimal-responses / tcp-clients / clients-per-query; group-level, default-off — issue #146 Phase 1); **BIND9 + PowerDNS DNSSEC** — inline-signing policies, DS export, manual rollover (issue #49 — see §3.3a). **Deferred:** DoT / DoH listener, secondary-zone (AXFR/IXFR) full support, GSS-TSIG (Kerberos-signed RFC 2136), Windows DNS Path B record-level writes.

## Overview

SpatiumDDI manages DNS servers as first-class resources. It acts as the **authoritative source of truth** for all DNS configuration, pushing changes to backend DNS servers via their respective drivers. The DNS subsystem supports:

- Forward and reverse zones organized in a **tree hierarchy**
- **Multiple DNS server groups** (e.g., internal, external, DMZ)
- **DNS views** (split-horizon) per server group
- **Dynamic DNS (DDNS)** — automatic record creation from DHCP leases
- **Incremental record updates** — no server restarts for record changes
- **Blocking lists** — integrated ad/malware blocking similar to Pi-hole
- **Per-zone assignment** to IP ranges/subnets
- Role-based access control on zones

---

## 0. Driver choice — BIND9, PowerDNS, or Windows DNS

SpatiumDDI ships three authoritative DNS drivers. Pick **per server group** — every server inside a group runs the same driver, but mixed installs (one group on BIND, another on PowerDNS, a third on Windows) are first-class. The driver registry is in [`drivers/dns/__init__.py`](../../backend/app/drivers/dns/__init__.py); the per-driver internals are in [`docs/drivers/DNS_DRIVERS.md`](../drivers/DNS_DRIVERS.md).

| Capability | BIND9 | PowerDNS | Windows DNS |
|---|:---:|:---:|:---:|
| Authoritative zone serving | ✅ | ✅ | ✅ |
| Recursive resolver | ✅ | — (recursor is a separate daemon) | ✅ |
| Record CRUD wire protocol | RFC 2136 + rndc | REST API (PATCH rrsets) | RFC 2136 (Path A) / WinRM (Path B) |
| Zone CRUD wire protocol | rndc addzone / delzone | REST API | WinRM (Path B only) |
| ALIAS records (CNAME at apex) | — | ✅ | — |
| LUA records (computed responses) | — | ✅ | — |
| Online DNSSEC signing | ✅ inline-signing (#49) | ✅ one-toggle | manual |
| Catalog zones (RFC 9432) — producer | ✅ | ✅ | — |
| Catalog zones (RFC 9432) — consumer | ✅ | — (waits for pdns 4.10+) | — |
| First-class views / split-horizon | ✅ | tag-based, not surfaced as views in UI | — (replication scope) |
| RPZ blocklists | ✅ | — (recursor feature only) | — |
| AD-integrated zones | — | — | ✅ |
| Agent shape | sidecar agent + named | sidecar agent + pdns_server | agentless (control plane → WinRM) |

**Default driver: BIND9.** It is the reference implementation, ubiquitous in operator muscle memory, and runs the catalog-zone consumer + RPZ paths SpatiumDDI ships.

**Pick PowerDNS when** you need ALIAS records (CNAME-at-apex without the BIND-side workaround), LUA records (geo-routing / weighted answers / `pickrandom` / `ifportup`), or the simpler one-toggle online DNSSEC story. The shipped image (`ghcr.io/spatiumddi/dns-powerdns`) bundles `pdns 4.9 + pdns-backend-lmdb` for an agent-isolated zone store with no external Postgres dependency. See [issue #127](https://github.com/spatiumddi/spatiumddi/issues/127) for the full driver rationale.

**Pick Windows DNS when** the zone is AD-integrated and operators expect to keep using DNS Manager / `Add-DnsServerResourceRecord` directly. Path A (RFC 2136 + AXFR) works without admin credentials; Path B (WinRM + PowerShell) unlocks zone CRUD and a JSON record-pull that sidesteps AXFR ACL configuration.

PowerDNS-only features (ALIAS / LUA / online DNSSEC sign+unsign) are server-side gated by the API's `_DRIVER_GATED_RECORD_TYPES` and `_DRIVER_GATED_OPERATIONS` maps. Calling them against a BIND9 / Windows / mixed group returns 422 with a remediation message — move the zone to a PowerDNS-only group, or add a PowerDNS server to the group, before retrying.

The Operator Copilot's `propose_create_dns_zone` tool accepts an explicit `driver_hint` argument (`bind9` / `powerdns` / `windows_dns`) so the LLM can route DNSSEC-required zones to PowerDNS groups without operators having to specify the group UUID by hand. See `app.services.ai.operations.CreateDNSZoneArgs`.

### 0a. Cloud DNS providers — Cloudflare / Route 53 / Azure DNS / Google Cloud DNS (issue #37)

**Add DNS server** also offers four cloud-hosted authoritative-DNS providers as driver choices: **Cloudflare**, **Amazon Route 53**, **Azure DNS**, and **Google Cloud DNS**. Once added, their zones and records are managed exactly like a local BIND9 / PowerDNS zone — same Zones / Records / group surfaces, same CRUD — but the control plane drives the provider's REST/SDK API directly instead of an agent (an *agentless* driver, the same shape as Windows DNS Path B). A cloud DNS server lives in a normal `DNSServerGroup`; credentials are a provider-specific dict (Cloudflare API token, Route 53 access keys, an Azure service-principal triple + subscription / resource group, or a GCP service-account JSON + project id) entered in the Add DNS server modal and Fernet-encrypted in `DNSServer.credentials_encrypted`.

The modal renders a per-driver **in-modal setup guide** for the required credential fields and a **Test** button that does a cheap auth + list-zones probe before save. No cloud driver advertises *online* DNSSEC sign/unsign — cloud DNSSEC is a provider-level zone toggle, not the per-record online signing SpatiumDDI's `dnssec_sign`/`unsign` ops model, so those operations stay gated to BIND9 / PowerDNS (#29 follow-up). Full per-driver internals (credential shapes, capability matrix, per-provider wrinkles) are in [DNS_DRIVERS.md §4A](../drivers/DNS_DRIVERS.md).

**Bringing existing zones in.** A cloud account that already hosts zones imports through the DNS importer's cloud source (preview → commit; see [MIGRATION.md](MIGRATION.md)). After that, ongoing drift is reconciled the same way as any other server — the **sync-from-server** path pulls the provider's live zone/record state via the driver's `pull_zones_from_server` / `pull_zone_records` reads.

A **token-only tier** (DigitalOcean / Hetzner / Linode / Vultr, issue #327) ships alongside the four headline providers as agentless first-class drivers with the same import-existing-zones flow.

These cloud-DNS drivers are distinct from the *Cloud (AWS / Azure / GCP)* read-only **infrastructure** mirror (issue #37 Part A — VPCs / subnets / instance IPs into IPAM; see [INTEGRATIONS.md](INTEGRATIONS.md)). One is authoritative-DNS management; the other is an IPAM reconciler. They share a provider vocabulary, not a code path.

---

## 1. DNS Server Groups

DNS servers are organized into named **groups** representing logical server clusters (not just individual servers). This reflects real-world deployments where you may have multiple resolvers per role.

### Group Model

```
DNSServerGroup
  id, name, description
  type: enum(internal, external, dmz, custom)
  default_view: str           -- which view clients in this group see by default
  is_recursive: bool          -- whether servers in group act as resolvers
  servers: [DNSServer]        -- one or more physical/virtual servers

DNSServer
  id, group_id, name
  driver: enum(bind9, powerdns, windows_dns, cloudflare, route53, azuredns, googledns, digitalocean, hetzner, linode, vultr)
  host, port
  credentials (encrypted)
  roles: [enum(authoritative, recursive, forwarder)]  -- server can have multiple roles
  status, last_sync_at, last_health_check_at
```

### Example Topology

```
Groups:
  "internal-resolvers"  → 2x BIND9 servers, recursive, serves internal view
  "dmz-resolvers"       → 1x BIND9, forwarder for DMZ hosts
```

A server may belong to only one group but may have **multiple roles** (e.g., both authoritative and recursive).

---

## 2. DNS Views (Split-Horizon)

Views allow the same zone name to return **different data** depending on the source IP of the DNS query. This is a native BIND9 feature.

### View Model

```
DNSView
  id, server_group_id, name
  description
  match_clients: [CIDR list]     -- source IPs that see this view
  match_destinations: [CIDR list]
  recursion: bool
  zones: [DNSZone]               -- zones present in this view (may differ per view)
  order: int                     -- views are evaluated in order (first match wins)
```

### Common View Pattern

| View Name | match_clients | What It Returns |
|---|---|---|
| `internal` | 10.0.0.0/8, 192.168.0.0/16 | Full internal zone data, internal IP for split names |
| `external` | any (0.0.0.0/0) | Public IP only, limited record set |
| `dmz` | 172.16.0.0/12 | DMZ-specific overrides |

### BIND9 Implementation
- Views map directly to BIND9 `view {}` blocks in `named.conf`
- Each view has its own set of `zone {}` directives
- Config is generated by the driver and pushed via `rndc reconfig` (no restart)

---

## 3. DNS Server Options & ACLs

Server-level options control how each DNS server (or server group) behaves globally — independent of any individual zone. These map to BIND9 `options {}` / `view {}` blocks. All settings are stored in the `DNSServerOptions` model and pushed to the server by the driver on change.

### 3.1 Forwarders

Forwarders are upstream resolvers used when the server cannot answer from its own zones.

```
DNSServerOptions.forwarders: list[str]          -- e.g. ["1.1.1.1", "8.8.8.8"]
DNSServerOptions.forward_policy: enum(
  first,     -- try forwarders first, fall back to recursion (BIND9 "forward first")
  only,      -- send all queries to forwarders, never recurse (BIND9 "forward only")
)
```

- **BIND9:** `forwarders { 1.1.1.1; 8.8.8.8; }; forward first|only;` in `options {}` or per-view
- Per-zone forward overrides (stub/forward zone type) take precedence over global forwarders

### 3.2 Recursion

Controls whether the server will follow referrals to resolve names it is not authoritative for.

```
DNSServerOptions.recursion_enabled: bool        -- default true for internal resolvers
DNSServerOptions.allow_recursion: list[str]     -- CIDR list; who may use this server as a resolver
                                                --   e.g. ["10.0.0.0/8", "192.168.0.0/16"]
                                                --   "any" or "none" are also valid literals
```

- **BIND9:** `recursion yes|no;` + `allow-recursion { <acl>; };` in `options {}` or per-view
- Authoritative-only servers must have `recursion_enabled: false` + `allow_recursion: ["none"]`

### 3.3 DNSSEC Resolution

Controls whether the server validates DNSSEC signatures when resolving.

```
DNSServerOptions.dnssec_validation: enum(
  auto,      -- validate using built-in / managed-keys (recommended)
  yes,       -- validate; trust anchors must be manually configured
  no,        -- do not validate DNSSEC
)

DNSServerOptions.trust_anchors: list[DNSTrustAnchor]

DNSTrustAnchor
  id, server_options_id
  zone_name: str          -- e.g. "." for root, "example.com." for island trust
  algorithm: int          -- DNSKEY algorithm number (e.g. 13 = ECDSAP256SHA256)
  key_tag: int
  public_key: str         -- base64-encoded DNSKEY public key
  is_initial_key: bool    -- true = initial-key (RFC 5011 managed), false = static-key
  added_at, added_by
```

- **BIND9:** `dnssec-validation auto|yes|no;` in `options {}` or per-view; trust anchors go in `managed-keys {}` or `trust-anchors {}`
- The root DNSSEC trust anchor (ICANN KSK) is pre-loaded automatically when `dnssec_validation: auto`
- UI shows DNSSEC chain validation status for each zone

> **Validation vs. signing.** The setting above controls whether the
> server *validates* answers as a resolver. *Signing* your own zones is a
> separate feature — see §3.3a (BIND9) / §0 (PowerDNS).

### 3.3a Zone signing — BIND9 inline-signing (issue #49)

BIND9 9.16+ `dnssec-policy` inline-signing. A **DNSSECPolicy** maps 1:1
to a BIND `dnssec-policy "<name>" { ... };` block — algorithm, NSEC3
params, KSK/ZSK lifetimes — and a zone references one. **BIND owns and
auto-rotates the private keys** (the modern, recommended model); SpatiumDDI
stores only the *public* state it reports back (DS rrset + per-key status),
so there is no private-key custody.

- **Policies** are managed at **DNS → DNSSEC Policies** (`/dns/dnssec-policies`).
  A built-in `default` policy (ECDSAP256SHA256, NSEC, unlimited KSK +
  90-day auto-rolled ZSK) is seeded and read-only.
- **Sign a zone** from its DNSSEC card (`POST .../dnssec/sign` with an
  optional `policy_id` — null ⇒ `default`). Signing is **config-driven**:
  flipping `dnssec_enabled` (+ policy) reshapes the agent ConfigBundle, the
  agent renders `dnssec-policy "<name>"; inline-signing yes;` into the zone
  stanza (and a top-level `dnssec-policy { }` block for custom policies),
  BIND auto-generates keys in `key-directory` and signs.
- **DS export.** After signing the agent runs `rndc dnssec -status` +
  `dnssec-dsfromkey` and reports the DS rrset + per-key state back via
  `POST /dns/agents/dnssec-state`; the card surfaces the DS records to
  paste at the parent registrar, plus a per-key table (tag / type / state).
- **Manual rollover.** The card's per-key **Roll** button
  (`POST .../dnssec/rollover`) enqueues a `dnssec_rollover` op; the agent
  runs `rndc dnssec -rollover -key <tag>`. Routine rollover is automatic
  per the policy — this is the "roll now" escape hatch.
- **Driver gating.** Sign/unsign are allowed on BIND9 + PowerDNS groups,
  rollover on BIND9 only; Windows DNS is refused (422). NSEC3 follows
  RFC 9276 (iterations 0 + salt-length 0 recommended) — the policy editor
  warns when iterations > 0.

### 3.4 GSS-TSIG

GSS-TSIG enables Kerberos-based authentication for secure DNS updates, used primarily with Active Directory / Windows DNS integration.

```
DNSServerOptions.gss_tsig_enabled: bool        -- default false
DNSServerOptions.gss_tsig_keytab_path: str     -- path to keytab on DNS server host
DNSServerOptions.gss_tsig_realm: str           -- e.g. "CORP.EXAMPLE.COM"
DNSServerOptions.gss_tsig_principal: str       -- e.g. "DNS/ns1.corp.example.com@CORP.EXAMPLE.COM"
```

- **BIND9:** `tkey-gssapi-keytab "/etc/bind/dns.keytab";` + `tkey-domain "CORP.EXAMPLE.COM";`
- When enabled, DDNS updates from Windows clients use Kerberos tickets rather than HMAC-TSIG keys
- The keytab file is deployed to the DNS server host by the SpatiumDDI agent; the path is stored (not the keytab content itself)
- Required for seamless AD/DNS integration and Windows Secure Dynamic Update

### 3.5 Notify

Controls whether the primary server notifies secondaries when a zone changes.

```
DNSServerOptions.notify_enabled: bool | enum(explicit, master-only, yes, no)
                                                -- "explicit" = only servers in also-notify list
DNSServerOptions.also_notify: list[str]         -- extra IPs to notify beyond NS records
                                                --   e.g. ["10.0.0.53", "10.0.1.53"]
DNSServerOptions.allow_notify: list[str]        -- who may send NOTIFY to this server
                                                --   (for secondary servers receiving notifies)
                                                --   e.g. ["10.0.0.1", "10.0.0.2"]
```

- **BIND9:** `notify yes|explicit|master-only|no;` + `also-notify { ... };` + `allow-notify { ... };`
- Notify settings can be overridden per-zone (the zone model inherits from server defaults)
- `also_notify` is useful when secondaries are stealth (not listed in zone NS records)
- `allow_notify` on secondaries controls which primaries are trusted to trigger a zone transfer

### 3.6 Query & Transfer Access Controls

Fine-grained controls over who can query, use the cache, transfer zones, and what gets blackholed.

```
DNSServerOptions.allow_query: list[str]         -- who may submit DNS queries
                                                --   default: ["any"]
DNSServerOptions.allow_query_cache: list[str]   -- who may use the recursive cache
                                                --   default: ["localhost", "localnets"]
DNSServerOptions.allow_transfer: list[str]      -- who may receive full zone transfers (AXFR/IXFR)
                                                --   default: ["none"]
DNSServerOptions.blackhole: list[str]           -- queries from these addresses are dropped silently
                                                --   e.g. ["192.0.2.0/24", "198.51.100.0/24"]
```

- **BIND9:** `allow-query { <acl>; };`, `allow-query-cache { <acl>; };`, `allow-transfer { <acl>; };`, `blackhole { <acl>; };` in `options {}` or per-view
- All values accept BIND9 ACL names (see §3.7), CIDRs, or literals (`any`, `none`, `localhost`, `localnets`)
- Can be overridden at the **view** level and further overridden at the **zone** level
- `blackhole` is applied before any ACL processing — matching queries receive no response (useful for DoS mitigation)
- `allow_query_cache` should be restricted to internal clients on authoritative-only servers (`none`)

### 3.7 DNS ACLs (Named Access Control Lists)

Named ACLs are reusable address match lists that can be referenced in any option above. They avoid repeating long CIDR lists across multiple settings.

```
DNSAcl
  id, server_group_id (nullable — global ACLs apply to all groups in the server group)
  name: str             -- e.g. "internal-clients", "trusted-secondaries"
  description: str
  entries: list[DNSAclEntry]

DNSAclEntry
  id, acl_id
  value: str            -- CIDR, IP, key name (e.g. "!10.0.0.5", "key my-tsig-key")
  negate: bool          -- if true, prefix with ! in generated config
  order: int            -- entries evaluated in order; first match wins
```

**Predefined ACL literals (no definition needed):**

| Literal | Meaning |
|---|---|
| `any` | All addresses |
| `none` | No addresses |
| `localhost` | All loopback addresses on the server |
| `localnets` | All directly attached networks |

**Example usage:**

```
ACL "internal-clients": 10.0.0.0/8, 192.168.0.0/16, 172.16.0.0/12
ACL "trusted-secondaries": 10.0.0.53, 10.0.1.53

allow_query:       ["internal-clients", "any"]   -- queries from anywhere allowed (auth server)
allow_query_cache: ["internal-clients"]           -- only internal clients use cache
allow_transfer:    ["trusted-secondaries"]        -- only known secondaries get AXFR
blackhole:         ["198.51.100.0/24"]            -- silently drop known bad actor range
```

- **BIND9:** ACLs are emitted as `acl "<name>" { ... };` blocks at the top of `named.conf`, before `options {}` and `view {}` blocks
- ACLs are managed at the **server group** level; views and zones within that group reference them by name
- UI: ACL editor under DNS Server Group settings — list of named ACLs, each with an ordered entry list; drag-to-reorder entries; inline negation toggle

### 3.8 Rate limiting (RRL) + amplification defenses (issue #146)

BIND9 Response Rate Limiting (RRL) and the related amplification-reduction knobs are exposed on `DNSServerOptions` (group-level; they apply to every view on the group) and render into the `options {}` block of `named.conf`. RRL is the single most effective in-process defense against DNS amplification — it drops or truncates duplicate responses to the same client `/24` + qname within a sliding window.

```
DNSServerOptions.rrl_enabled: bool                  -- default false (feature off — no rate-limit{} block rendered)
DNSServerOptions.rrl_responses_per_second: int      -- 1–1000; per-client-/24 response budget
DNSServerOptions.rrl_window: int                    -- 1–3600 seconds; the accounting window
DNSServerOptions.rrl_slip: int                      -- 0–10; every Nth dropped response is truncated (TC=1)
                                                    --   instead of dropped, so legit clients can retry over TCP
DNSServerOptions.rrl_qps_scale: int | null          -- optional; tighten the limit as overall QPS rises
DNSServerOptions.rrl_exempt_clients: list[str]      -- CIDRs / ACL names never rate-limited (e.g. your secondaries)
DNSServerOptions.rrl_log_only: bool                 -- default false; DRY RUN — count + log would-be drops without
                                                    --   actually dropping. Use to size the limit before enforcing.

DNSServerOptions.minimal_responses: bool            -- default false; emit "minimal-responses yes;" to shrink the
                                                    --   amplification payload (omit the extra section unless required)
DNSServerOptions.tcp_clients: int | null            -- optional; max simultaneous TCP clients
DNSServerOptions.clients_per_query: int | null      -- optional; starting per-query duplicate-client cap
DNSServerOptions.max_clients_per_query: int | null  -- optional; ceiling for clients_per_query
```

- **BIND9:** renders into `options {}`:
  ```
  rate-limit {
      responses-per-second 15;
      window 15;
      slip 2;
      qps-scale 250;                       // only when set
      exempt-clients { 10.0.0.0/8; };      // only when non-empty
      log-only yes;                        // only when rrl_log_only
  };
  minimal-responses yes;                   // only when minimal_responses
  tcp-clients 150;                         // only when set
  clients-per-query 10;                    // only when set
  max-clients-per-query 100;               // only when set
  ```
- **Defaults are a no-op.** With `rrl_enabled=false`, `minimal_responses=false`, and the optional knobs unset, the rendered `named.conf` is byte-identical to before the feature existed — adding it never changes an existing group's behavior until an operator opts in.
- **PowerDNS:** authoritative `pdns_server` has no RRL equivalent; the project's answer there is a **dnsdist front** (Phase 2 — see below). These RRL/amplification knobs are BIND9-only.
- **Recommended starting point** for an internet-facing authoritative server: `rrl_enabled=true`, `responses-per-second≈15`, `window=15`, `slip=2`, exempt your own secondaries. Run with `log-only=true` first and watch the drop counters before enforcing.
- UI: **DNS → Server Group → Server Options → "Rate limiting (RRL) & amplification"** card.
- MCP: `find_dns_rate_limit_settings` (read-only) reports the posture per group.
- **Observability (Phase 3 — shipped):** the BIND9 agent ships `RateDropped` + `RateSlipped` from the statistics-channels XML as `rate_dropped` / `rate_slipped` on the per-minute `dns_metric_sample`; the server detail modal's Stats tab draws an **"RRL drops/s"** line (shown once a server has dropped anything), and the default-off **`dns_rate_limit_dropping`** alert rule fires when drops over a 15-minute window clear a floor (`min_free_addresses`, default 100) — i.e. the server is actively shedding a flood. Auto-resolves when it subsides.

#### dnsdist front for PowerDNS (Phase 2)

PowerDNS Authoritative has no RRL, so rate limiting / DDoS defense in front of a PowerDNS group is provided by an opt-in **dnsdist sidecar** that binds `:53` and forwards to pdns. Configured group-level on `DNSServerOptions` (PowerDNS groups), default-off:

```
DNSServerOptions.dnsdist_enabled: bool                   -- default false
DNSServerOptions.dnsdist_max_qps_per_client: int | null  -- per-source-IP QPS cap (MaxQPSIPRule)
DNSServerOptions.dnsdist_action: enum(truncate, drop)    -- over-cap action; truncate sets TC=1 so a
                                                         --   legit client retries over TCP (default)
DNSServerOptions.dnsdist_dynblock_qps: int | null        -- sustained-rate dynamic block (exceedQRate over 10s)
DNSServerOptions.dnsdist_dynblock_seconds: int           -- dynamic block duration (default 60)
```

- The PowerDNS agent renders only the **rate-limit rules** (`MaxQPSIPRule` + `TCAction`/`DropAction`, `dynBlockRulesGroup:setQueryRate`) into a shared `dnsdist-rules.conf`; the dnsdist **front is a separate container** whose entrypoint composes those rules onto its base (`setLocal(:53)` + `newServer → dns-powerdns:53`) and reloads on change. **pdns never moves port** — the front forwards to pdns:53 over the network, fully decoupled from pdns's lifecycle. With dnsdist disabled the front is a plain pass-through (safe no-op).
- **Deploy the front:** compose `--profile dns-powerdns-with-dnsdist` (alongside `--profile dns-powerdns`); point DNS clients at the front (host `:5455` in the dev compose). **docker-compose only for now** — the k8s/appliance front (a dnsdist Deployment fronting the hostNetwork pdns DaemonSet) is a follow-up.
- UI: **DNS → Server Group → Server Options → "dnsdist front (PowerDNS)"** card. MCP `find_dns_rate_limit_settings` reports the dnsdist posture alongside RRL.

### 3.9 Options Precedence

Settings can be defined at three levels and are evaluated from most-specific to least-specific:

```
Zone override  (per-zone notify, allow-transfer, also-notify)
    ↓
View override  (per-view recursion, allow-query, allow-query-cache, forwarders)
    ↓
Server default (DNSServerOptions — applies to all views/zones on that server)
```

The driver is responsible for generating the correct BIND9 config that reflects this layered precedence. Service-layer code must not hard-code driver specifics.

---

## 4. DNS Zone Tree

Zones are displayed and managed in a **tree hierarchy** that mirrors the DNS namespace naturally.

```
. (root)
├── com.
│   └── example.
│       ├── internal.example.com.   [internal view only]
│       └── example.com.            [all views]
├── net.
└── in-addr.arpa.
    ├── 10.in-addr.arpa.
    │   └── 1.10.in-addr.arpa.
    └── 192.in-addr.arpa.
```

### Zone Model

```
DNSZone
  id, server_group_id, view_id (nullable)
  name (FQDN with trailing dot, e.g., "example.com.")
  type: enum(primary, secondary, stub, forward)
  kind: enum(forward, reverse)    -- forward or reverse lookup zone
  ttl (default SOA TTL)
  refresh, retry, expire, minimum (SOA fields)
  primary_ns, admin_email         (SOA fields)
  is_auto_generated: bool         -- created automatically for a subnet
  linked_subnet_id (nullable FK)  -- if reverse zone, tied to a subnet
  dnssec_enabled: bool
  last_serial: int
  last_pushed_at: timestamp
```

### Zone ↔ Subnet Binding

Every subnet can be assigned:
- A **forward DNS zone** (for auto-creating A/AAAA records)
- A **reverse DNS zone** (for auto-creating PTR records)

When a subnet is created or edited, the UI prompts: *"Auto-create reverse zone for 10.1.2.0/24?"* — which generates `2.1.10.in-addr.arpa.` on the designated server group.

---

## 5. DNS Records

### Supported Record Types

`A`, `AAAA`, `CNAME`, `MX`, `TXT`, `NS`, `PTR`, `SRV`, `CAA`, `TLSA`, `SSHFP`, `NAPTR`, `LOC`, `SVCB`, `HTTPS`, `DNAME`

Plus the PowerDNS-only `ALIAS` (CNAME-at-apex) and `LUA` (computed responses) types, which are driver-gated — see §0 and `_DRIVER_GATED_RECORD_TYPES` in `backend/app/api/v1/dns/router.py`.

### Record Model

```
DNSRecord
  id, zone_id, view_id (nullable)
  name (relative to zone, e.g., "host1" for host1.example.com.)
  fqdn (computed, stored for search)
  type: enum(A, AAAA, CNAME, ...)
  value
  ttl (overrides zone default if set)
  priority (for MX, SRV)
  weight, port (for SRV)
  auto_generated: bool   -- set true when created by DDNS or IPAM allocation
  ip_address_id (nullable FK) -- links back to IPAddress if auto-generated
  created_by_user_id, created_at
  last_modified_at
```

---

## 6. Incremental DNS Updates (No Restarts)

> See also: [`docs/deployment/DNS_AGENT.md`](../deployment/DNS_AGENT.md) for
> how the SpatiumDDI-shipped agent applies record ops over loopback and
> reports zone-serial telemetry back to the control plane.

**BIND9:**
- Record add/modify/delete → RFC 2136 `nsupdate` via `dnspython`
- Zone creation/deletion → `rndc addzone` / `rndc delzone`
- Config changes (views, options) → regenerate `named.conf`, push via SSH/SCP, then `rndc reconfig`
- **A full `named` restart is never required** for normal operations
- Serial is auto-incremented on every change (YYYYMMDDNN format)

**PowerDNS:**
- Record add/modify/delete → `PATCH /api/v1/servers/localhost/zones/<zone>` rrset patch via the REST API
- Record changes are atomic and immediate — no restart, no reload
- Zone creation/deletion via the REST API; PowerDNS handles the serial bump internally

### Driver Method: `apply_record_change()`

```python
async def apply_record_change(
    self,
    zone: str,
    record: DNSRecordData,
    operation: Literal["create", "update", "delete"]
) -> None:
    # Must NOT restart the service
    # Must increment zone serial
    # Must be atomic (or roll back on failure)
```

---

## 7. Dynamic DNS (DDNS) — DHCP Lease → DNS Record

> **Implementation status:** Subnet-level opt-in DDNS has shipped. When a lease lands via the agentless pull path (Windows DHCP) *or* an agent lease event (Kea), SpatiumDDI resolves a hostname per the subnet's policy and publishes A/AAAA + PTR via the same RFC 2136 / WinRM path static allocations use. The Kea path runs through `apply_ddns_for_lease` in the `POST /api/v1/dhcp/agents/lease-events` handler.

### Architecture

DDNS is a thin layer on top of the IPAM → DNS sync pipeline. The DNS side is identical to what a static allocation produces; the only DDNS-specific logic is picking a hostname from the lease.

```
┌──────────────────────────┐
│  Lease source            │
│  ─ agentless pull        │  → upserts DHCPLease + mirrors into IPAM
│    (Windows DHCP)         │    as auto_from_lease=True
│  ─ agent lease event      │  → Kea (POST /agents/lease-events)
│    (Kea)                  │
└───────────┬──────────────┘
            │ ipam_row
            ▼
┌──────────────────────────┐
│  services/dns/ddns.py    │
│  apply_ddns_for_lease()  │  → resolves hostname per subnet policy,
│                          │    writes it onto ipam_row.hostname,
│                          │    calls _sync_dns_record(..., "create")
└───────────┬──────────────┘
            │
            ▼
┌──────────────────────────┐
│  _sync_dns_record        │  → unchanged — drives A/AAAA + PTR via
│  (IPAM router)           │    RFC 2136 for BIND9 / Windows Path A,
│                          │    or WinRM for Windows Path B
└──────────────────────────┘
```

On lease expiry, `dhcp_lease_cleanup` sweeps the DHCPLease row past its grace period; before deleting the mirrored `auto_from_lease` IPAM row it calls `revoke_ddns_for_lease`, which fires `_sync_dns_record(..., action="delete")` to tear down the A/AAAA + PTR.

### Subnet-level configuration

DDNS is opt-in per subnet. A subnet can also inherit its DDNS settings from its enclosing block / space: `IPSpace` and `IPBlock` carry the same four DDNS fields, and when `ddns_inherit_settings` is true `resolve_effective_ddns` (in `backend/app/services/dns/ddns.py`) walks subnet → block → space to find the effective values.

| Field | Default | Purpose |
|---|---|---|
| `ddns_enabled` | `False` | Master toggle. When off, leases on the subnet don't publish DNS. |
| `ddns_hostname_policy` | `client_or_generated` | See below. Only read when `ddns_enabled`. |
| `ddns_domain_override` | `NULL` | Publish into a different zone than the subnet's primary forward zone (e.g. `dhcp.corp.example.com` while manual allocations stay in `corp.example.com`). |
| `ddns_ttl` | `NULL` | Override the zone's default TTL for auto-generated records. |

### Hostname policies

| Policy | Behaviour |
|---|---|
| `client_provided` | Publish only if the lease has a client hostname. Skip if empty. |
| `client_or_generated` | Use client hostname if present, else generate `dhcp-<tail>`. **Default**. |
| `always_generate` | Ignore client hostname, always synthesise. |
| `disabled` | Never publish, even if `ddns_enabled`. (Useful for temporarily parking DDNS without losing your config.) |

**Generated hostnames:**
- IPv4 — `dhcp-<third-octet>-<fourth-octet>`. So `10.1.20.5` → `dhcp-20-5`.
- IPv6 — `dhcp-<low-32-bits-hex>`. Rare path; the format is ugly but unique.

**Static assignment override:** if the lease IP matches a `DHCPStaticAssignment` that has a hostname set, that hostname always wins — regardless of policy, including `always_generate`. Rationale: a static hostname is an explicit admin choice.

**Sanitisation:** all hostnames (client-provided or static) are folded to lower-case, non-`[a-z0-9-]` characters collapse to `-`, leading/trailing hyphens strip, and the result truncates at RFC 1035's 63-character label limit.

### Idempotency

DDNS is safe to call repeatedly. If the resolved hostname matches what's already on the `IPAddress` row and there's already a linked auto-generated DNS record, no ops are enqueued. The agentless lease-pull loop hits this path every poll; post-steady-state it's effectively a no-op.

### Security

- RFC 2136 updates to BIND9 use TSIG signing (key stored Fernet-encrypted).
- WinRM calls to Windows DNS go over HTTPS with cert validation by default.
- The DDNS service itself never touches manual IPAM allocations (it gates on `auto_from_lease=True` before doing anything).

### Enabling DDNS (quick walkthrough)

1. **Pick a subnet** with a DNS forward + reverse zone already assigned.
2. Open the subnet editor → **Dynamic DNS (from DHCP)** section → toggle **Enabled**, pick a policy, optionally set a domain override or TTL. Save.
3. Make sure the subnet's DNS group has at least one healthy server (BIND9 via agent, or Windows DNS — Path A or B).
4. Ensure **Settings → DHCP Lease Sync** is enabled (the agentless poll loop — currently the only lease source that fires DDNS).
5. Issue a lease on the Windows DHCP scope covering the subnet. Wait for the next poll (default 5 min) or hit **Sync Leases** on the server detail page.
6. The IPAM subnet page shows the IP with its hostname; the DNS zone shows the matching A + PTR.

### Not-yet (planned follow-ups)

- **Grace period** on revoke — today we delete A + PTR immediately when the lease-cleanup sweep removes the IPAM row. A short grace where we drop the TTL to 30 s first would help mid-transition clients.

---

## 8. DNS Blocking Lists

Inspired by Pi-hole, SpatiumDDI can configure DNS servers to **block domains** by responding with `NXDOMAIN` or a configurable sinkhole IP.

### Blocking List Model

```
DNSBlockList
  id, name, description
  source_type: enum(url, manual, file_upload)
  source_url: str (nullable)    -- e.g., https://someblocklistprovider.com/list.txt
  format: enum(hosts, domains, adblock)
  update_interval_hours: int    -- 0 = manual only
  last_updated_at: timestamp
  entry_count: int (computed)
  is_enabled: bool
  applied_to_groups: [DNSServerGroup]  -- which server groups enforce this list

DNSBlockListEntry
  id, list_id
  domain: str                   -- e.g., ads.example.com
  is_wildcard: bool             -- blocks *.example.com too
  source_line: str              -- original line from source for debugging

DNSBlockListException
  id, domain, reason
  created_by_user_id
  applied_to_groups: [DNSServerGroup]
```

### Supported List Formats
- **Hosts file** (`0.0.0.0 ads.example.com`)
- **Domain list** (one domain per line)
- **AdBlock format** (`||ads.example.com^`)

### Block Response Modes

| Mode | DNS Response | Use Case |
|---|---|---|
| `nxdomain` | NXDOMAIN | Cleanest; some clients retry on NXDOMAIN |
| `sinkhole` | Returns configured IP (e.g., 0.0.0.0) | Can serve a block page |
| `refused` | REFUSED | Strict policy environments |

### BIND9 Implementation
- Uses a `Response Policy Zone (RPZ)` — an industry-standard BIND9 feature
- Block list entries are written as RPZ zone records
- Updating the RPZ zone uses `rndc reload <rpz-zone>` (no full restart)
- Multiple RPZ zones can be chained (per-list)

### UI Features
- Dashboard showing blocked query stats (pulled from DNS server logs)
- Per-list enable/disable toggle
- Allow-list exceptions (whitelist specific domains)
- Manual domain addition to block list
- "Test a domain" — check if a domain would be blocked

---

## 9. DNS UI Features

### Zone Tree View
- Collapsible namespace tree (same as the file explorer metaphor)
- Drag-and-drop zone organization (reorder, move between groups)
- Click zone → record list with inline editing

### Record Management
- Bulk import records from zone file (RFC 1035 format)
- Export zone as standard zone file
- Record diff view before pushing changes to server

### Zone Health Indicators
- Last successful sync timestamp
- Serial number displayed
- SOA consistency check across primary and secondaries
- DNSSEC validation status

### Server Group Dashboard
- All servers in group + their status
- Query rate (pulled from metrics)
- Block list hit rate
- Zone count

---

## 10. Permissions on DNS Resources

DNS zones inherit the standard permission model:

| Role | Capability |
|---|---|
| **superadmin** | Full DNS server, group, view, zone, record management |
| **admin** (scoped to zone) | Create/edit/delete records in zone, manage zone settings |
| **operator** (scoped to zone) | Create/edit/delete records; cannot change zone settings |
| **viewer** (scoped to zone) | Read zone and records; no modifications |

Zone permissions are assignable to groups via the standard Permission model, same as IP ranges.

---

## 11. Environment Variables for DNS

```bash
# DDNS defaults
DDNS_DEFAULT_TTL=300
DDNS_GRACE_PERIOD_SECONDS=60   # how long to keep record after lease expiry

# Blocking lists
BLOCKLIST_UPDATE_INTERVAL_HOURS=24
BLOCKLIST_SINKHOLE_IP=0.0.0.0

# BIND9 TSIG (global default — per-server keys stored in DB encrypted)
BIND_TSIG_ALGORITHM=hmac-sha256
```

---

## 12. "Sync with Servers" — group-level bi-directional reconciliation

Every DNS server group has a **Sync with Servers** button on its detail header. It iterates every enabled server in the group and runs a four-step reconciliation per server:

1. **List zones on the wire** — for Windows servers with credentials, `Get-DnsServerZone | Where { -not $_.IsAutoCreated }` over WinRM; for BIND9, the server's zone list from the agent's last-known config.
2. **Auto-import server-only zones** — any zone present on the wire but missing from SpatiumDDI is created as `is_auto_generated=False`. System-only zones (TrustAnchors, RootHints, Cache, anything without a dot) are skipped.
3. **Push DB-only zones back to the server** — any SpatiumDDI zone not present on the wire is created via the driver's `apply_zone_change`. For Windows Path B, this is `Add-DnsServerPrimaryZone -ReplicationScope Domain -DynamicUpdate Secure`.
4. **Per-zone record sync** — for each zone, pull all records (AXFR for BIND9 or RFC 2136-capable servers, `Get-DnsServerResourceRecord` over WinRM for Windows Path B), reconcile against DB records, and apply the delta. Additive-only — never deletes.

The UI surfaces per-server results with zones-imported / zones-pushed / errors, plus a per-zone table.

Manual drift checks from the subnet / block / space side use **Check DNS Sync** which only covers IPAM-managed records (A/AAAA/PTR). The group-level reconciliation covers everything in a zone.

---

## 13. Windows DNS — Path A & B

SpatiumDDI supports Windows Server DNS as an **agentless** backend in two tiers that coexist on the same driver class. Which one applies at runtime depends on whether the server has WinRM credentials configured.

| Tier | Activation | Capabilities | Protocol |
|---|---|---|---|
| **Path A** | Always available | Record CRUD, AXFR pull | RFC 2136 + dnspython over UDP/TCP 53 |
| **Path B** | `DNSServer.credentials_encrypted` set | Zone create / delete, `Get-DnsServerResourceRecord`-based pull (AXFR-free), server-level probes | WinRM + `DnsServer` PowerShell module over 5985/5986 |

Record writes always ride RFC 2136 — Path B is not used for per-record writes (to avoid paying the PowerShell-per-record cost on hot writes). Zone topology writes use Path B when credentials are present, otherwise SpatiumDDI can't create zones on Windows (create them manually in DNS Manager, then click **Sync with Servers**).

### 13.1 When to choose which

| Situation | Recommendation |
|---|---|
| Existing AD environment, you want the full SpatiumDDI experience | **Path B** — register the DC with WinRM credentials. |
| Existing AD environment, you only need record writes and don't want to provision a WinRM service account | **Path A** — make sure zones are "Nonsecure and secure" dynamic updates. |
| "Secure only" AD-integrated zone that can't be changed | **Path B for zone management; record writes fail** until GSS-TSIG lands. Treat it as zone-only for now. |
| Greenfield, no AD | Use the built-in BIND9 container instead. |

### 13.2 Credentials shape

Windows DNS credentials match the Windows DHCP shape — same Fernet-encrypted dict, same transport options:

```json
{
  "username": "CORP\\spatium-dns",
  "password": "…",
  "winrm_port": 5986,
  "transport": "ntlm",
  "use_tls": true,
  "verify_tls": true
}
```

Stored on `DNSServer.credentials_encrypted`. The server create modal in the UI renders these fields when `driver=windows_dns`, with a **Test Connection** button that runs `(Get-DnsServerSetting -All).BuildNumber` as a cheap probe.

### 13.3 What Path B unlocks

**Zone create / delete:**

```powershell
Add-DnsServerPrimaryZone -Name "corp.example.com" -ReplicationScope Domain -DynamicUpdate Secure
Remove-DnsServerZone -Name "corp.example.com" -Force
```

Both are idempotent — the driver guards with `Get-DnsServerZone -ErrorAction SilentlyContinue` before acting, so a missing zone on delete is a no-op.

**Zone record pull (AXFR-free):**

When AD-integrated zones refuse AXFR (the default ACL), Path B pulls records via `Get-DnsServerResourceRecord -ZoneName "…"`. The driver normalises the output (`HostName`, `Type`, `TTL`, `Value`, optional `Priority` / `Weight` / `Port`) into the neutral `RecordData` shape. SOA and apex NS are filtered out.

**Zone list:**

`Get-DnsServerZone | Where { -not $_.IsAutoCreated }` returns every non-system zone. Feeds the group-level "Sync with Servers" step 1.

### 13.4 What Path B does *not* do (yet)

| Not yet | Reason |
|---|---|
| Per-record writes via WinRM | Paying a PowerShell round-trip per record is too slow for hot writes. RFC 2136 stays the write path. |
| GSS-TSIG (Kerberos-signed RFC 2136) | Lets Path A work against "Secure only" zones without changing them. On the roadmap. |
| SIG(0) authentication | Niche; not prioritised. |
| Server-level options (forwarders, recursion, allow-query) | Out of scope for agentless Windows — Windows manages these via Registry + DNS MMC. |

### 13.5 Zone transfer / AXFR for Path A

For Path A to pull records, AXFR from the SpatiumDDI host must be allowed. In Windows DNS Manager:

1. Right-click the zone → **Properties** → **Zone Transfers** tab.
2. **Allow zone transfers** → **Only to the following servers** → add the SpatiumDDI host IP.

If AXFR is refused, the per-zone sync in step 4 of "Sync with Servers" shows `Zone transfer error: REFUSED`. Switching that server to Path B (adding WinRM credentials) bypasses AXFR entirely.

See [WINDOWS.md](../deployment/WINDOWS.md) for full Windows-side prerequisites including WinRM enablement, service account creation, and firewall rules.

---

## 14. IPAM ↔ DNS synchronization jobs

Two scheduled reconciliation jobs complement the live sync path (`_sync_dns_record` runs on every IP mutation):

### 14.1 IPAM → DNS Reconciliation

Catches drift between IPAM's expected records (every IP with a hostname + DNS zone pinned) and SpatiumDDI's DNS DB. Creates missing A/AAAA/PTR records and (optionally) updates mismatched ones.

| Setting | Default | Description |
|---|---|---|
| `dns_auto_sync_enabled` | off | Master toggle. |
| `dns_auto_sync_interval_minutes` | 60 | How often the task runs. |
| `dns_auto_sync_delete_stale` | off | Also delete auto-generated records whose IP was deleted. Conservative default — leaves stale rows so you can review. |

Implementation: `app.tasks.ipam_dns_sync.auto_sync_ipam_dns` (Celery beat fires every 60s, task gates on the `enabled` flag + interval).

### 14.2 Zone ↔ Server Reconciliation

Catches drift between SpatiumDDI's DNS DB and the authoritative server's wire. Identical to pressing "Sync with Servers" on every group, on a timer. AXFR imports out-of-band edits, then any DB-only records are pushed back via RFC 2136. Additive only.

| Setting | Default | Description |
|---|---|---|
| `dns_pull_from_server_enabled` | off | Master toggle. |
| `dns_pull_from_server_interval_minutes` | 30 | AXFR + RFC 2136 is heavier than a DB diff; a low cadence is usually wrong. |

Implementation: `app.tasks.dns_pull.auto_pull_dns_from_server`.

Both jobs have **Last Run** indicators in Settings so you can confirm they're firing.

## 15. Rules & constraints

Server-side validations that reject requests with a human-readable
error. Clients should display the response `detail` to the operator —
most of these feed the IPAM / DNS / DHCP UI error banners directly.

### Zones

- **Duplicate zone name inside a group/view.** `(group_id, view_id,
  name)` is a unique constraint; create/update returns `409` with
  *"A zone with that name already exists in this group/view."* in
  `backend/app/api/v1/dns/router.py`.
- **`zone_type` enum.** One of `primary` / `secondary` / `stub` /
  `forward`. Pydantic validator in
  `backend/app/api/v1/dns/router.py`.
- **`color` enum.** Must be in `VALID_ZONE_COLORS` (`slate`, `red`,
  `amber`, `emerald`, `cyan`, `blue`, `violet`, `pink`). Free-form
  hex is deliberately not accepted so both themes stay legible.
  `backend/app/api/v1/dns/router.py`.
- **`notify_enabled` enum.** One of `yes` / `no` / `explicit` /
  `master-only`. `backend/app/api/v1/dns/router.py`.
- **Windows zone push-before-commit.** When a zone is created / deleted
  on a group that has an agentless `windows_dns` server with
  credentials, the WinRM push happens first — a WinRM failure rolls
  back the DB transaction and returns `502` so the operator never
  sees a SpatiumDDI zone that doesn't exist on the real DC.
  `backend/app/api/v1/dns/router.py`.

### Records

- **`record_type` enum.** Must be in `VALID_RECORD_TYPES` — A, AAAA,
  CNAME, MX, TXT, NS, PTR, SRV, CAA, TLSA, SSHFP, NAPTR, LOC, SVCB,
  HTTPS, DNAME, plus the PowerDNS-only ALIAS / LUA types.
  `backend/app/api/v1/dns/router.py`.
- **Record owner-name conformance (issue #597).** `DNSRecord.name` is
  validated against the **RFC 2181 §11** rule, *not* the RFC 1123 LDH host
  rule — deliberately looser, because the DNS protocol permits an
  underscore and RFC 1123 does not. That looseness is load-bearing: it is
  exactly what keeps `_acme-challenge` (SpatiumDDI's own ACME DNS-01
  client, #438), `_dmarc`, and `_443._tcp` SRV / TLSA owners legal. A naive
  RFC 1123 check here would have broken our own certificate issuance.
  Wildcards are accepted as a leftmost `*` label. What *is* rejected:
  whitespace, control characters (a raw newline can inject a second record
  into a zone-file line), the zone-file-dangerous punctuation
  (`;` `$` `(` `)` `"` `@` `\`), and a leading or trailing hyphen. Labels
  cap at 63 characters, the whole name at 253. `422` via
  `app.core.dns_names.validate_record_owner` at
  `backend/app/api/v1/dns/router.py:944` (create) / `:963` (update) — and
  the same validator on GSLB pool members at
  `backend/app/api/v1/dns/pool_router.py:140`. See §18.
- **Bare-name rdata targets.** The target of a CNAME / MX / NS / SRV / PTR /
  DNAME record is validated as an FQDN. `422` at
  `backend/app/api/v1/dns/router.py:1133`.
- **Zone-name FQDN validation (issue #597).** `DNSZone.name` goes through the
  FQDN rule — a dotted series of RFC 2181 labels, IDN-normalised to `xn--`
  A-labels, lower-cased, with the trailing root dot re-appended on the way
  into storage. Underscore labels are allowed (`_msdcs.example.com` is a real
  zone); wildcards are not. `422` via `app.core.dns_names.validate_fqdn` at
  `backend/app/api/v1/dns/router.py:785` (create) / `:832` (update). See §18.

### Servers & server groups

- **Duplicate server-group name.** `409` in
  `backend/app/api/v1/dns/router.py`.
- **Duplicate server name within a group.** `(group_id, name)` is
  unique — same name is fine across different groups. `409` in
  `backend/app/api/v1/dns/router.py`.
- **`group_type` enum.** `VALID_GROUP_TYPES` —
  `backend/app/api/v1/dns/router.py`.
- **`driver` enum.** Must be one of the registered drivers
  (`VALID_DRIVERS`, derived from the driver registry — `bind9` /
  `powerdns` / `windows_dns` / the cloud + token-only providers, plus
  `stub_resolver` for tests). `422` in
  `backend/app/api/v1/dns/router.py`.
- **Windows credentials must be complete on first set.** Creating a
  `windows_dns` server with Path B credentials requires both
  `username` and `password`; an incomplete pair returns `400`. Later
  updates may include just one field.
  `backend/app/api/v1/dns/router.py`.

### ACLs & views

- **Duplicate ACL name within a group.** `(group_id, name)` unique.
  `409` in `backend/app/api/v1/dns/router.py`.
- **Duplicate view name within a group.** Same pattern. `409` in
  `backend/app/api/v1/dns/router.py`.

### Server options

- **`forward_policy` enum.** `first` or `only`. Validator in
  `backend/app/api/v1/dns/router.py`.
- **`dnssec_validation` enum.** `auto`, `yes`, or `no`. Validator in
  `backend/app/api/v1/dns/router.py`.

## 16. Multi-group / split-horizon publishing at the IPAM layer (issue #25)

Distinct from §2's DNS Views (which split horizons at the
recursive-resolver layer): #25 splits at the **IPAM** layer so an
operator can publish the same address into both an internal zone
and a public zone simultaneously, with per-record routing overrides
when a single host should appear only inside.

**Block-level flag.** `IPBlock.dns_split_horizon` is a boolean. When
true, descendant subnets that inherit DNS settings publish records
to **both** `dns_zone_id` (the internal / primary zone) AND every
entry in `dns_additional_zone_ids` (DMZ / external zones). The
existing `dns_inherit_settings` walk picks this up, so flipping the
flag at the block level cascades down without per-subnet edits.

**Per-record override.** `IPAddress.dns_zone_overrides` is a JSONB
list of `[{zone_id, record_type}]` pairs. When set, the auto-sync
emits records only into the listed zones for the listed record
types — useful for "this one bastion should only have an A record
in the internal zone, no PTR, no external A".

**Auto-sync.** The scheduled IPAM ↔ DNS auto-sync respects the
split: each address that lands in a split-horizon subnet emits one
record per zone that survives the override filter. DDNS for DHCP
leases follows the same path.


## 17. GSLB pools (health-checked) + geo / topology-aware steering (issue #530)

DNS **pools** (GSLB-lite) map one DNS name (e.g. `www` →
`www.example.com`) to a set of A / AAAA target IPs and flip each
target in / out of the served record set based on a periodic health
check. Members render as regular `DNSRecord` rows (one per healthy +
enabled member, carrying `pool_member_id`) so BIND9 / PowerDNS /
Windows DNS render unchanged. Config lives on `DNSPool` +
`DNSPoolMember`; the reconciler is `app.services.dns.pool_apply`.

A pool's DNS name must live in a **forward** zone. Reverse
(`in-addr.arpa` / `ip6.arpa`) zones are filtered out of the zone
picker and rejected server-side by the pool-create endpoint
(`pool_router.py` returns `400` on a `kind == "reverse"` zone),
because a pool member renders A / AAAA records and a reverse zone
holds only PTRs.

> **This is not a load balancer.** DNS is cached client-side, so a
> member dropping out doesn't take effect until the pool `ttl`
> expires — clients may keep hitting a dead / distant box for up to
> `ttl` seconds. Keep the TTL short (default 30 s). See the
> **TTL-race caveat** below.

### 17.1 Serving scope — steering one name to the nearest datacenter

By default every client gets the same healthy rrset. **Geo steering**
(issue #530) adds client-location awareness so one name resolves to
the nearest datacenter. Each pool member carries an optional
**serving scope**:

* `serving_cidrs` — a JSONB list of client CIDRs (`203.0.113.0/24`,
  `10.1.0.0/16`, …).
* `site_id` — an optional FK to a Network → **Site**. The site's
  linked subnets (`subnet.site_id`) contribute their CIDRs to the
  member's scope. `ON DELETE SET NULL` — deleting the Site just drops
  the association.

The two sources are **UNIONed**. A member with an empty scope
(`serving_cidrs == []` and `site_id IS NULL`) is a **default** target
served to everyone (the historical behaviour). A member with a scope
is served **only** to clients whose resolver source IP falls inside
that scope.

Result: a client from CIDR X resolves to `{geo members scoped to X} ∪
{default members}`; a client matching no geo scope resolves to
`{default members}`. Health-check gating composes cleanly — an
unhealthy member is never advertised regardless of scope.

**No-blackhole guarantee.** A pool where *every* member is geo-scoped
(the "each site serves its own region, no global fallback" config) has
no default members, so a client matching no geo CIDR would otherwise get
NODATA for a name that has healthy targets. To prevent that, an all-geo
pool's members are also served as a **union fallback** into the non-geo
views (operator views + the `spatium-geo-default` catch-all) — so an
unmatched client resolves to the union of all healthy members instead of
an empty rrset. A pool that has at least one default member keeps the
strict behaviour (geo members only in their geo view).

### 17.2 Rendering — synthesized BIND9 geo views

The mechanism is a BIND9 `view { match-clients … }` block: a "geo
view" == a view with a client-subnet match list. At ConfigBundle-build
time (`app.services.dns.pool_geo`) the control plane:

1. resolves each member's scope, groups members by distinct scope, and
   synthesizes one geo view per scope (`spatium-geo-1 …
   spatium-geo-N`, `match-clients` = the scope's CIDRs), ordered
   **before** any operator-defined split-horizon views (§2). BIND
   evaluates `view` blocks top-to-bottom, first-match-wins, so a
   geo-CIDR client must reach its geo view *before* a broad operator
   view (an `internal` view matching `10.0.0.0/8`, or any `any`/empty
   match) swallows the query and strips the geo member. Geo scopes are
   the more-specific match, so geo-first is most-specific-first in the
   common case (caveat: a narrow operator view — e.g. a `/32` mgmt host
   — that a broader geo view would shadow; split the geo scope if that
   bites);
2. appends a catch-all `spatium-geo-default` view (`match-clients {
   any; }`) **last**, so a client matching no specific geo view *and*
   no operator view still resolves;
3. scopes each geo-member's record into its own geo view, while
   default members (and every non-pool record) render as **shared**
   records visible in every view — reusing the same per-view record
   routing as the split-horizon path (`DNSRecord.view_id IS NULL` =
   shared). An all-geo pool's members are additionally rendered into
   the non-geo views as the no-blackhole union fallback (§17.1).

No `DNSView` / `DNSAcl` rows are persisted — geo views are a pure
render-time concern, kept out of the operator-managed split-horizon
view catalog so the two features don't collide in the admin UI. Geo
steering forces views mode on even for a group with no operator views;
the incremental RFC 2136 path can't target a view, so with geo active
the whole group re-renders view-correctly on each change (same as
split-horizon).

The Pools tab member editor exposes both scope inputs (client CIDRs +
Site picker) per member; scoped members show a `geo` chip. The
`list_dns_pools` MCP tool surfaces `serving_cidrs` + `site_id` on each
member so the Operator Copilot can answer "which datacenter does the
EU client get for www?".

### 17.3 Source-IP semantics (v1) and the ECS stretch goal

v1 keys purely on the **resolver source IP** — the address BIND sees
the query arriving from. When a recursive resolver sits between the
end client and the authoritative server (the common public-internet
case), that source IP is the *resolver's*, not the end client's, so
steering follows the resolver's location.

**EDNS Client Subnet (ECS, RFC 7871) is the future accuracy
improvement** and is deliberately **not implemented** in v1: it
carries a prefix of the real client's address so the authoritative
server can steer on the client rather than the resolver. Wiring it
needs `match-clients` driven off the ECS option rather than the
TCP/UDP source address, and is tracked as a stretch goal.

### 17.4 TTL-race caveat

As with all DNS-based steering, geo steering is subject to the pool
TTL cache window: a client that already cached an answer keeps using
it until the TTL expires, even after it crosses into a different geo
scope (e.g. a roaming laptop that moves between sites). Keep the pool
TTL short. This is the same caveat as the base pool feature — DNS
steering is a coarse, cache-bounded mechanism, not a per-request load
balancer.

## 18. DNS-name conformance (issue #597)

**There is no single "valid DNS name" rule.** The correct rule depends on
what the field *is* — a host name, a DNS record owner, and an FQDN are
three different grammars, and applying the strictest one everywhere breaks
legitimate DNS. `backend/app/core/dns_names.py` is the single place that
decides; nothing else hand-rolls a name regex.

| Context | Rule | Where |
|---|---|---|
| **Host names** — `IPAddress.hostname`, a DHCP reservation hostname | RFC 952 + RFC 1123 §2.1 **LDH**: letters, digits, hyphens; no leading / trailing hyphen. Internationalized input is normalised to its IDNA **A-label** (`xn--`) form rather than rejected. | `validate_hostname` / `validate_host_label` |
| **DNS record owners** — `DNSRecord.name` | RFC 2181 §11: LDH **plus underscore**, plus a leftmost `*` wildcard. | `validate_record_owner` / `validate_dns_label` |
| **FQDNs** — `DNSZone.name`, the DHCP `domain-name` / `domain-search` options, bare-name rdata targets | A dotted series of RFC 2181 labels (underscore allowed, wildcards not). | `validate_fqdn` |

The record-owner rule is deliberately the **looser** one. RFC 1123 forbids
an underscore; the DNS protocol does not — and `_acme-challenge`,
`_dmarc`, and `_443._tcp` SRV / TLSA owners all need it. Applying the host
rule to record owners would have broken SpatiumDDI's **own** ACME DNS-01
client (#438), which writes `_acme-challenge` TXT records into managed
zones to prove domain control. Underscore support here is a correctness
requirement, not a leniency.

Every `validate_*` helper both **rejects and canonicalises**: it raises
`ValueError` with an operator-facing message on a bad value, and returns
the *normalised* (IDNA-encoded, lower-cased, root-dot-stripped) value on
success — so a Pydantic `field_validator` does both in one pass. Common
caps across all three rules: label ≤ **63** characters, whole name ≤ **253**.

### 18.1 Validate on write — never auto-mutate

**The validators run on write only. Existing rows are never rewritten.**
Silently mutating an operator's stored name would be a worse failure than
leaving it alone — so a non-conforming legacy row stays exactly as it is
until someone edits it deliberately. §18.3 is how you find them.

The one place a bad name must *not* raise is the **DHCP lease path**. A
client-supplied hostname arriving off the wire (option 12 on a DISCOVER, a
lease event, a Windows lease pull) goes through the **non-raising**
`sanitize_hostname` instead, which folds it into a safe multi-label LDH
form (or `""` if nothing usable is left). A malformed hostname must never
drop a lease. Call sites: `backend/app/api/v1/dhcp/agents.py:166` and
`backend/app/services/dhcp/pull_leases.py:178`.

### 18.2 Defense in depth at the render boundary

The BIND9 and PowerDNS drivers run **every** rendered name and rdata value
through `strip_control_chars` before it reaches a zone-file master line or
the pdns API (`backend/app/drivers/dns/bind9.py:71` /
`backend/app/drivers/dns/powerdns.py:117`). A raw newline is the one
character that can inject a *second* record into a zone-file line, and no
legitimate name or rdata ever contains a control byte. This catches values
that never passed a field validator — an importer row, a legacy row, a
future code path — so they still cannot break out of their own record.
Spaces and quotes are left intact, so structured rdata (CAA / LOC / NAPTR /
SVCB) renders unharmed.

### 18.3 Auditing existing rows

`GET /api/v1/diagnostics/name-conformance` (**superadmin**, read-only,
mutates nothing) scans the live database for names today's validators would
reject and reports them by category:

| Category | Rows scanned | Rule applied |
|---|---|---|
| `ipam_hostname` | `IPAddress.hostname` (integration-owned rows excluded — an external mirror owns those names and the operator can't fix them here) | host |
| `dns_record_name` | `DNSRecord.name` | record owner |
| `dns_zone_name` | `DNSZone.name` | FQDN |
| `dhcp_static_hostname` | `DHCPStaticAssignment.hostname` | host |

Each category returns an exact `total` plus up to 100 examples
(`id` + `value` + the validator's own rejection `reason`), and a
`scanned_capped` flag when the per-category 200 000-row scan ceiling bit.
Implementation: `backend/app/services/dns_names_report.py`.

The same report is exposed to the Operator Copilot as the read-only
`find_nonconforming_names` MCP tool (default-enabled, superadmin-gated) —
*"which hostnames aren't valid DNS names?"* / *"do we have any records that
would break a zone file?"*.
