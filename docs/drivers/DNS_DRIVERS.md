# DNS Driver Specification

## Overview

DNS drivers implement the `DNSDriverBase` abstract class. They are responsible for translating SpatiumDDI's internal DNS model into operations on real DNS servers. The critical constraint: **no DNS driver may restart the DNS daemon** as part of normal record or zone operations.

---

## 1. Abstract Base Class

```python
from abc import ABC, abstractmethod
from enum import Enum
from dataclasses import dataclass
from typing import Literal

@dataclass
class DNSRecordData:
    name: str             # Relative to zone (e.g., "host1")
    record_type: str      # "A", "AAAA", "PTR", "CNAME", etc.
    value: str
    ttl: int
    priority: int | None = None   # MX, SRV
    weight: int | None = None     # SRV
    port: int | None = None       # SRV

@dataclass
class DNSZoneData:
    name: str             # FQDN with trailing dot (e.g., "example.com.")
    zone_type: str        # "primary", "secondary"
    ttl: int
    refresh: int
    retry: int
    expire: int
    minimum: int
    primary_ns: str
    admin_email: str

@dataclass
class DriverHealth:
    status: Literal["online", "offline", "degraded"]
    message: str
    checked_at: datetime
    version: str | None = None

class DNSDriverBase(ABC):
    @abstractmethod
    async def health_check(self) -> DriverHealth: ...

    @abstractmethod
    async def get_zones(self) -> list[DNSZoneData]: ...

    @abstractmethod
    async def create_zone(self, zone: DNSZoneData) -> None: ...

    @abstractmethod
    async def delete_zone(self, zone_name: str) -> None: ...

    @abstractmethod
    async def get_records(self, zone_name: str) -> list[DNSRecordData]: ...

    @abstractmethod
    async def create_record(
        self, zone_name: str, record: DNSRecordData
    ) -> None: ...

    @abstractmethod
    async def update_record(
        self, zone_name: str, record: DNSRecordData
    ) -> None: ...

    @abstractmethod
    async def delete_record(
        self, zone_name: str, name: str, record_type: str
    ) -> None: ...

    @abstractmethod
    async def apply_blocklist(
        self, rpz_zone: str, domains: list[str], mode: str
    ) -> None: ...

    # MUST NOT restart the daemon. Use incremental update mechanisms.
    # Raise NotImplementedError if the driver cannot avoid a restart.
```

---

## 2. BIND9 Driver

### Update Strategy: RFC 2136 + rndc (never named restart)

| Operation | Mechanism | Notes |
|---|---|---|
| Add/update/delete record | RFC 2136 `nsupdate` (via `dnspython`) | Incremental, instant, TSIG-signed |
| Create zone | `rndc addzone` | No restart; zone active immediately |
| Delete zone | `rndc delzone` | No restart |
| Update zone options (SOA, TTL) | `nsupdate` SOA record replacement | Incremental |
| Add/update view | Regenerate `named.conf` → SCP → `rndc reconfig` | No restart; new config loaded in-place |
| Update forwarders, options | Regenerate `named.conf` → SCP → `rndc reconfig` | No restart |
| Update RPZ (blocking) | `rndc reload <rpz-zone>` after writing RPZ zone file | Zone-level reload only |
| **Full daemon restart** | ❌ NEVER for normal operations | Only for: initial install, major version upgrade |

### TSIG Authentication

All RFC 2136 updates are TSIG-signed:
```python
keyring = dns.tsigkeyring.from_text({
    self.tsig_keyname: self.tsig_secret   # stored encrypted in DB
})
update = dns.update.Update(zone, keyring=keyring, keyalgorithm=dns.tsig.HMAC_SHA256)
```

### Zone Creation via rndc addzone

```bash
rndc addzone example.com '{ type primary; file "/var/named/example.com.db"; };'
```

The driver:
1. Generates the zone file from SpatiumDDI zone data
2. SCPs zone file to BIND9 host
3. Runs `rndc addzone`
4. Verifies zone is active with `rndc zonestatus example.com`

### Named.conf Management

`named.conf` is **never edited by hand** when SpatiumDDI manages BIND9. The driver maintains:
- `named.conf.spatiumddi` — generated includes (views, zones, options)
- `named.conf` includes `named.conf.spatiumddi`
- Changes: regenerate `named.conf.spatiumddi` → SCP → `rndc reconfig`

### Serial Number Management

Zone serial follows `YYYYMMDDnn` format:
- On each record change via `nsupdate`, serial auto-increments (BIND handles this)
- Driver tracks and displays current serial from `rndc zonestatus`

### BIND9 Driver Configuration

```python
@dataclass
class BIND9DriverConfig:
    host: str
    ssh_port: int = 22
    ssh_user: str = "bind-mgmt"
    ssh_key: str                  # Path to SSH private key (or key content)
    rndc_key_name: str
    rndc_key_secret: str          # Encrypted in DB
    tsig_key_name: str
    tsig_key_secret: str          # Encrypted in DB
    named_conf_dir: str = "/etc/bind"
    zone_file_dir: str = "/var/cache/bind"
    rndc_host: str = "127.0.0.1"
    rndc_port: int = 953
```

### 2.5 DNSSEC — inline-signing (issue #49)

BIND9 9.16+ `dnssec-policy` inline-signing. **BIND owns + auto-rotates the
private keys**; the control plane stores only public state. The flow is
config-driven, not op-driven (unlike PowerDNS's REST sign):

1. **Control plane.** A signed zone (`DNSZone.dnssec_enabled`) carries an
   optional `dnssec_policy_id` → a `DNSSECPolicy` row (algorithm / NSEC3 /
   KSK+ZSK lifetimes). The bundle assembler stamps `dnssec_enabled` +
   `dnssec_policy_name` onto each zone and ships referenced custom policies
   in `dnssec_policies` (the built-in `default` carries no block). Both flow
   into the structural ETag, so a sign/policy change triggers a re-render.
2. **Agent render.** `named.conf` gets `key-directory "/var/cache/bind/keys"`,
   a top-level `dnssec-policy "<name>" { keys { ksk … ; zsk … ; }; nsec3param … ; };`
   per custom policy, and each signed primary zone's stanza gets
   `dnssec-policy "<name>"; inline-signing yes;`. BIND auto-generates keys in
   the key-directory and signs on load.
3. **DS + key-state report.** After a reload the agent's `collect_dnssec_state`
   runs `rndc dnssec -status <zone>` (parsed by the version-tolerant
   `_parse_dnssec_status`) + `dnssec-dsfromkey` over the KSK key files, and
   POSTs the DS rrset + per-key state to `/dns/agents/dnssec-state`. The
   control plane mirrors it into `DNSZone.dnssec_ds_records` + `DNSKey` rows
   (replace-per-zone) for the operator's DS-export + key-status view.
4. **Manual rollover.** `POST .../dnssec/rollover` enqueues a
   `dnssec_rollover` op (key tag); the agent runs
   `rndc dnssec -rollover -key <tag> <zone>` and re-reports. Sign/unsign ops
   are no-ops on the BIND9 agent (the config render drives signing).

Gating: `_DRIVER_GATED_OPERATIONS` allows `dnssec_sign`/`dnssec_unsign` on
`{powerdns, bind9}` and `dnssec_rollover` on `{bind9}`.

### 2.6 Rate limiting (RRL) + amplification (issue #146)

Group-level `DNSServerOptions` fields flow through the standard
`ServerOptions` → `ConfigBundle` → ETag → long-poll path (so a UI change
shifts the etag and re-renders `named.conf` with no extra wake plumbing) and
land in the `options {}` block of both renderers — the agent's
`NAMED_CONF_SKELETON` (the live config; see `_render_rate_limit_block`) and
the control-plane preview template `named.conf.j2`.

- `rrl_enabled` gates a `rate-limit { responses-per-second; window; slip;
  [qps-scale]; [exempt-clients]; [log-only]; }` stanza. `log-only` is BIND's
  dry-run (count + log drops, drop nothing) for sizing the limit safely.
- `minimal_responses`, `tcp_clients`, `clients_per_query`,
  `max_clients_per_query` each render only when set.
- **Every field defaults to a no-op**, so adding the feature renders
  byte-identical config for groups that haven't opted in (the bundle etag
  shifts once on upgrade, causing a single graceful `rndc reconfig`).
- BIND9-only. PowerDNS authoritative has no RRL equivalent; the planned
  answer there is a dnsdist front (#146 Phase 2, not yet shipped). RRL drop
  counters + a `dns_rate_limit_dropping` alert are #146 Phase 3.

---

## 3. Windows DNS Driver

Located at [`app/drivers/dns/windows.py`](../../backend/app/drivers/dns/windows.py). Class: `WindowsDNSDriver`. Two capability tiers coexist on the same driver class; which one applies at runtime depends on whether `DNSServer.credentials_encrypted` is set.

### 3.1 Path A — RFC 2136 (always available)

| Operation | Mechanism | Notes |
|---|---|---|
| Add / update / delete record | RFC 2136 via `dnspython` (`dns.update.Update`) | TSIG-signed when configured, unsigned when allowed. |
| Zone record pull | AXFR via `dns.query.xfr` + `dns.zone.from_xfr` | Requires AXFR ACL on the Windows zone. |
| Create / delete zone | Not available in Path A | Zones must be created in Windows DNS Manager (or via Path B when credentials are set). |
| Rendering (`render_server_config`, etc.) | Returns empty string | SpatiumDDI does not write named.conf-style config for Windows DNS. |
| `reload_*` | No-op | AD replication handles zone propagation across DCs. |

Supported record types: `A`, `AAAA`, `CNAME`, `MX`, `TXT`, `PTR`, `SRV`, `NS`, `TLSA`.

### 3.2 Path B — WinRM + PowerShell (credentials required)

Activated when `DNSServer.credentials_encrypted` is set. Does **not** replace Path A for record writes; it complements it.

| Operation | Mechanism | Notes |
|---|---|---|
| List zones | `Get-DnsServerZone \| Where { -not $_.IsAutoCreated }` | Feeds the group-level "Sync with Servers" step 1. |
| Create zone | `Add-DnsServerPrimaryZone -Name <n> -ReplicationScope Domain -DynamicUpdate Secure` | Guarded with `Get-DnsServerZone -ErrorAction SilentlyContinue` — idempotent. |
| Delete zone | `Remove-DnsServerZone -Name <n> -Force` | Same idempotent guard — no-op when zone already absent. |
| Pull records | `Get-DnsServerResourceRecord -ZoneName <n>` | Sidesteps AXFR ACLs on AD-integrated zones. Returns JSON that the driver normalises into `RecordData`. |
| Test connection | `(Get-DnsServerSetting -All).BuildNumber` | Cheap probe used by the `POST /dns/test-windows-credentials` endpoint and the UI's Test button. |
| Record writes | **Still RFC 2136** | PowerShell-per-record would be too slow for hot writes. |

### 3.3 Credentials

Fernet-encrypted JSON dict, same shape as Windows DHCP:

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

Required security group for the service account: `DnsAdmins` on the domain (or a delegated group with equivalent rights). See [WINDOWS.md](../deployment/WINDOWS.md).

### 3.4 Driver registry classification

In [`app/drivers/dns/__init__.py`](../../backend/app/drivers/dns/__init__.py):

```python
AGENTLESS_DRIVERS: frozenset[str] = frozenset(
    {"windows_dns", "cloudflare", "route53", "azure_dns", "google_dns"}
)
```

Agentless drivers don't emit a `ConfigBundle`; the API's `record_ops.enqueue_record_op` short-circuits them and calls `apply_record_change` directly instead of queueing for a co-located agent. The four cloud-hosted DNS providers (§4A) joined this set in issue #37.

### 3.5 Write-through pattern

Zone CRUD is pushed through the Windows server **before** the SpatiumDDI DB commit:

```python
await _push_zone_to_agentless_servers(db, zone, op="create")
await db.commit()
```

If the push fails, the 502 response prevents the DB commit — the Windows DNS state and SpatiumDDI state stay consistent. Record ops follow the same pattern via the agent-side op queue for agented drivers and direct calls for agentless.

### 3.6 Shared AXFR helper

[`drivers/dns/_axfr.py`](../../backend/app/drivers/dns/_axfr.py) extracts the AXFR → `RecordData` logic used by both BIND9 and Windows Path A. Filters SOA + apex NS; absolutises CNAME / NS / PTR / MX / SRV targets.

### 3.7 Batched WinRM dispatch

Record CRUD against a Windows DNS server used to round-trip WinRM **per record** — a 40-record Sync DNS took 2-3 minutes of wall time. The driver now ships one PowerShell script per zone-chunk with every op inside: each chunk does every op server-side with a per-op `try / catch` and returns a JSON result array so one bad record doesn't abort the batch.

**Driver surface.** New plural method on the ABC:

```python
class DNSDriver(ABC):
    async def apply_record_changes(
        self, changes: Sequence[RecordChange]
    ) -> Sequence[RecordOpResult]:
        """Apply many record changes in as few round trips as possible.

        Default impl calls apply_record_change in a loop — BIND9
        inherits it. Windows overrides with a real batch."""
```

BIND9 + any future driver gets the plural interface for free via the default loop.

**Windows batch size — 6 ops per chunk.** The real constraint isn't WinRM's envelope cap (`MaxEnvelopeSize` defaults to 500 KB) but the way `pywinrm.run_ps` ships the script: UTF-16-LE → base64 → `powershell.exe -EncodedCommand <b64>` → **single CMD.EXE command line, hard-capped at 8191 chars by Windows**. Base64 costs ×1.33, UTF-16-LE costs ×2, so each raw script char eats ~2.67 chars of command-line budget.

The minified wrapper is ~2000 raw chars (all eight supported record types + op dispatch + per-op try/catch + JSON result emit), which costs ~5350 chars of cmdline budget before any ops. Each op adds ~160 raw chars (~430 cmdline chars) once JSON-escaped + encoded. **6 ops fits; 7 trips the limit.** `_WINRM_BATCH_SIZE = 6` is empirically measured in the dev container, documented in the source.

**Script layout.** One invocation carries data-only JSON with short keys (`i/op/z/n/t/v/ttl/pr/w/p`) and a single dispatch wrapper:

```
$E='Continue'
$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('<b64>'))
$r = @()
($p | ConvertFrom-Json) | % {
    $e = [ordered]@{change_index=[int]$_.i; ...; ok=$false}
    try {
        $ze = [bool](Get-DnsServerZone -Name $_.z -EA SilentlyContinue)
        if ($_.op -eq 'delete') { ... }
        elseif ($_.op -eq 'create' -or $_.op -eq 'update') {
            switch ($_.t) { 'A' { ... }; 'AAAA' { ... }; 'CNAME' { ... }; ... }
        } else { throw "Unsupported op" }
        $e.ok = $true
    } catch { $e.error = "$($_.Exception.Message)" }
    $r += (New-Object PSObject -Property $e)
}
$r | ConvertTo-Json -Compress -Depth 3
```

`$ErrorActionPreference = 'Continue'` ensures a per-op `throw` doesn't abort the enclosing script; the try/catch per op records the error into the result array. Chunk-wide script errors (syntax, base64 decode) still raise from `_run_ps` and propagate to the caller.

**Lifting the ceiling — pypsrp.** Future upgrade path: swap `pywinrm` for `pypsrp`. PSRP uses the WSMan Runspace protocol instead of CMD.EXE and removes the 8K limit entirely — would yield ~100 ops/batch on the same envelope settings. Tracked as a TODO comment in [`drivers/dns/windows.py`](../../backend/app/drivers/dns/windows.py).

**RFC 2136 path — `asyncio.gather`.** The 2136 write path is cheap per-op but was still serial. Record ops now run in parallel via `asyncio.gather`; no batching needed because the dnspython update framing is already compact.

**Dispatch.** `enqueue_record_ops_batch(db, zone, ops)` in [`services/dns/record_ops.py`](../../backend/app/services/dns/record_ops.py) groups pending ops by zone and calls `apply_record_changes` once per group. Zone serial bumps once per batch instead of N times. **State-aware commit**: the caller zips through the returned op rows and only deletes (or confirms success) when `state == "applied"`. A failed wire op never causes a DB delete — previous behaviour would report "deleted" to the UI while the record was still published on Windows.

**Results.**

| Path | Before | After |
|---|---|---|
| Sync DNS / 40 records | ~3 min | ~5 s |
| Bulk delete / 80 records on one zone | 80 HTTP calls | 1 HTTP call, 14 WinRM round trips (80 / 6) |

---

## 4. PowerDNS Driver

Located at [`app/drivers/dns/powerdns.py`](../../backend/app/drivers/dns/powerdns.py). Class: `PowerDNSDriver`. Shipped in issue #127.

PowerDNS is a second authoritative driver running side-by-side with BIND9. It is **agent-managed** the same way BIND9 is — there is one DNS agent per server, the agent owns the local PowerDNS daemon (`pdns_server`), and the control plane never opens a connection to PowerDNS directly. The agent talks to PowerDNS's REST API on `127.0.0.1:8081`; the control plane talks to the agent through the existing long-poll `/config` channel.

The shipped image (`ghcr.io/spatiumddi/dns-powerdns`) bundles `pdns 4.9.x` with the `pdns-backend-lmdb` backend. LMDB is a single-file embedded zone store — no external Postgres, no shared credentials, full operational symmetry with BIND9's "zone files on local disk" model. A `gpgsql`-backend image variant is on the Phase 5+ wishlist for operators who want PowerDNS-pod-replicas-against-shared-Postgres HA, but is not the default.

### 4.1 Update Strategy: REST API (never daemon restart)

| Operation | Mechanism | Notes |
|---|---|---|
| Add / update / delete record | `PATCH /api/v1/servers/localhost/zones/<zone>` rrset patch | Idempotent; one HTTP call per rrset; PowerDNS handles serial bump internally. |
| Create zone | `POST /api/v1/servers/localhost/zones` | LMDB row created; available to query immediately. |
| Delete zone | `DELETE /api/v1/servers/localhost/zones/<zone>` | LMDB row removed; idempotent. |
| Reconcile zone (full sync) | `PUT /api/v1/servers/localhost/zones/<zone>` with full rrset list | Used on first sync or on detected drift. |
| Online DNSSEC sign | `POST .../zones/<zone>/cryptokeys` (KSK + ZSK) + `PUT .../metadata/PRESIGNED` + `PUT .../zones/<zone>/rectify` | Idempotent — re-sign skips when keys exist. |
| Online DNSSEC unsign | `DELETE .../cryptokeys/<id>` per key + clear `PRESIGNED` | Same idempotent shape. |
| Catalog zone (RFC 9432) producer | Render apex SOA + NS + `version` TXT + per-member SHA-1-hashed PTR via the same rrset PATCH path | Producer-only; consumer waits for pdns 4.10+ (Phase 5 polish). |
| **Full daemon restart** | ❌ NEVER for normal operations | Only for: image bump, zone-storage backend swap (LMDB → gpgsql). |

The agent never reads `pdns.conf` to figure out what to do — it queries PowerDNS over REST and reconciles against the `ConfigBundle` shipped from the control plane. This is the same conceptual loop as the BIND9 driver, but the wire protocol is HTTP+JSON instead of RFC 2136+rndc.

### 4.2 PowerDNS API key

PowerDNS gates its REST API with a static API key (`api-key=...` in `pdns.conf`). The container's entrypoint generates a fresh key on first boot, writes it into the local `pdns.conf`, and exports it to the agent via a tmpfs-mounted env file. **Operators never touch this key.** The agent reads it on startup and rotates it on every container restart.

The control plane has no knowledge of the API key — the trust boundary is between the agent and its co-located PowerDNS daemon, not between the control plane and PowerDNS.

### 4.3 Capabilities

```python
class PowerDNSDriver(DNSDriver):
    @classmethod
    def capabilities(cls) -> dict[str, Any]:
        return {
            "alias_records": True,         # CNAME-at-apex via PATCH
            "lua_records": True,           # ENABLE-LUA-RECORDS=1 zone metadata auto-set
            "dnssec_inline_signing": True, # online sign / unsign / re-sign
            "catalog_zones": "producer-only",  # consumer needs pdns 4.10+
            "views": False,                # tag-based; not surfaced as views in UI yet
            "rpz": False,                  # authoritative-only — RPZ is a recursor feature
        }
```

`alias_records: True`, `lua_records: True`, and `dnssec_inline_signing: True` are the three operator-visible features that BIND9 doesn't ship today. Each is gated server-side by the API's `_DRIVER_GATED_RECORD_TYPES` / `_DRIVER_GATED_OPERATIONS` maps — calling them against a non-PowerDNS group returns 422 with a remediation message ("move to a PowerDNS-only group").

### 4.4 LUA records

LUA records are PowerDNS's mechanism for computed responses — geo-routing, weighted answers, conditional `pickrandom` / `ifportup` / `createReverse` snippets. The frontend exposes a `<textarea>` for the LUA value when the operator selects record type `LUA`; the agent auto-sets `ENABLE-LUA-RECORDS=1` zone metadata via the PowerDNS REST API the first time a zone gains a LUA record (idempotent — the metadata stays harmless when no LUA records remain).

**Security note.** LUA records execute server-side at query time. Treat them as code. The frontend's contextual banner explains this; restrict who can create LUA records via the existing RBAC `dns.record.create` permission scoped to PowerDNS groups only.

### 4.5 Online DNSSEC

PowerDNS does the full DNSSEC dance internally:

1. `POST /cryptokeys` with `keytype: ksk` (Algorithm 13 / ECDSAP256SHA256 by default). PowerDNS generates the key and starts publishing DNSKEY rrsets.
2. `POST /cryptokeys` with `keytype: zsk` (same algorithm). PowerDNS now signs all rrsets in the zone with the ZSK on every query.
3. `PUT /metadata/PRESIGNED` set to `0` — confirms the daemon manages signing online (vs. presigned zones loaded from disk).
4. `PUT /zones/<zone>/rectify` — recomputes NSEC / NSEC3 chain.

After signing, the agent enumerates DS records via `GET /cryptokeys` and POSTs them back to the control plane through the new `POST /api/v1/dns/agents/dnssec-state` endpoint. The control plane caches them in `dns_zone.dnssec_ds_records` (JSONB) so the operator-facing zone-edit page renders them without round-tripping the agent.

**Backup integration.** DNSSEC keys live in the agent's LMDB store, **not** in the control-plane backup. Restoring a DNSSEC-signed zone to a fresh agent regenerates keys and produces NEW DS records, which must be re-published to the parent registrar. The restore endpoint surfaces this as a `RestoreOutcomeResponse.warnings[]` advisory. See [issue #127 Phase 4d](https://github.com/spatiumddi/spatiumddi/issues/127).

### 4.6 Catalog zones (RFC 9432)

Producer-side only. When `DNSServerGroup.catalog_zones_enabled` is on and this server is the group primary, the agent renders the catalog zone alongside regular zones via the same PATCH rrset path used for normal zones:

- Apex SOA + NS
- `version` TXT pinned to `"2"` (RFC 9432 §4.1)
- One PTR per primary zone under `<sha1>.zones.<catalog-zone>.` using the canonical RFC 9432 wire-format hash. **Identical bytes to BIND9's catalog renderer** — a SpatiumDDI catalog can be served from either driver and consumed by either.

Consumer mode logs a structured warning at agent startup: pdns 4.9 (the shipped image) does not auto-consume catalog zones. Operators with PowerDNS secondaries pull via plain AXFR against the producer; full consumer-side support waits for an image bump to pdns 4.10+.

### 4.7 PowerDNSDriver Configuration

```python
@dataclass
class PowerDNSDriverConfig:
    api_url: str = "http://127.0.0.1:8081"   # local-only by design
    api_key: str                             # generated by entrypoint, agent reads from env
    api_timeout: float = 10.0
```

There is no `host` / `ssh_user` / `ssh_key` / RNDC counterpart — PowerDNS configuration is entirely REST-driven, and the REST endpoint is bound to loopback. This is significantly less surface area than the BIND9 driver maintains.

### 4.8 LMDB cache + recovery

LMDB is a single mmap'd file at `/var/lib/powerdns/pdns.lmdb`. The shipped Helm chart and standalone Compose file mount it on a persistent PVC / volume:

```yaml
# charts/spatiumddi/templates/dns-agent.yaml — flavor: powerdns branch
volumeMounts:
  - { name: dns-state, mountPath: /var/lib/powerdns }
```

```yaml
# docker-compose.agent-dns-powerdns.yml
volumes:
  dns_powerdns_lmdb: {}
services:
  dns-powerdns:
    volumes:
      - dns_powerdns_lmdb:/var/lib/powerdns
```

On agent startup, the supervisor checks the LMDB file. If it's empty (fresh install), the entrypoint runs `pdnsutil create-bind-db` to seed an empty store; the long-poll then picks up the first ConfigBundle and reconciles the zones. If the LMDB file is populated (restart on existing volume), `pdns_server` boots straight into serving and the agent reconciles any DB drift on the next ConfigBundle ETag flip.

LMDB cache survives control-plane outages — non-negotiable #5 in `CLAUDE.md`. The daemon keeps answering queries from the on-disk LMDB store regardless of whether the agent can reach the control plane.

---

## 4A. Cloud DNS drivers (agentless) — issue #37 Part B

Four cloud-hosted authoritative-DNS providers ship as a driver family: **Cloudflare**, **Amazon Route 53** (`route53`), **Azure DNS** (`azure_dns`), and **Google Cloud DNS** (`google_dns`). SpatiumDDI manages their zones and records exactly like a local BIND9 / PowerDNS / Windows zone — same Zones / Records / group surfaces — except the control plane calls the provider's REST/SDK API directly. There is **no agent**.

These are infrastructure-DNS drivers, distinct from the *Cloud (AWS / Azure / GCP)* read-only infrastructure mirror (issue #37 Part A, in [INTEGRATIONS.md](../features/INTEGRATIONS.md)). A cloud DNS server is added through the normal Add DNS server flow and lives in a `DNSServerGroup`; it has no `CloudEndpoint` row.

### 4A.1 Agentless shape (reuses Windows-DNS Path B)

The shared base [`drivers/dns/_cloud_base.py`](../../backend/app/drivers/dns/_cloud_base.py) (`CloudDNSDriverBase`) mirrors how `windows_dns` Path B already works (§3 above):

- **No ConfigBundle / long-poll.** The `render_*` methods return `""` and the `reload_*` methods are no-ops — agentless drivers never render daemon config. `validate_config` accepts anything.
- **Credentials in the existing column.** The per-provider credential dict is Fernet-encrypted in the existing `DNSServer.credentials_encrypted` column — no new credential store. `_load_credentials` decrypts it, raising a clean `CloudDNSError` when unset vs. when the API rejects the key.
- **Record writes run synchronously from the control plane.** Once the driver name is listed in `AGENTLESS_DRIVERS` (registry §5), the API's `record_ops._apply_agentless` calls `apply_record_change` directly and records each op as `applied` / `failed` on a `DNSRecordOp` row — exactly the path Windows DNS uses. The default batch loop in `DNSDriver` isolates a per-op failure into a `RecordChangeResult(ok=False)`.
- **Zone create / delete via `apply_zone_change(server, zone, op)`** (`op` ∈ `create` / `delete`) — cloud providers have no rename, so the caller sends delete+create. Routed through the same write-through pattern as Windows zone CRUD (§3.5): pushed to the provider before the SpatiumDDI DB commit so the two states stay consistent.
- **Reads via `pull_zones_from_server` / `pull_zone_records`** — the same method names Windows DNS exposes, returning the same neutral dict / `RecordData` shape (record names relative to the apex, `@` for apex). So the existing `sync-from-server` drift path *and* the cloud import service (§ MIGRATION.md) both work against any cloud driver with no per-provider glue.

A concrete provider subclasses `CloudDNSDriverBase` and implements only five cloud-specific hooks — `_list_zones`, `_list_zone_records`, `_apply_record`, `_apply_zone`, `capabilities` — plus the `name` / `credential_fields` class attrs. Everything DNSDriver-shaped lives in the base.

### 4A.2 Per-driver credential dict shapes

`credential_fields` is an ordered tuple the Add-DNS-server modal renders and the probe validates as required:

| Driver | `name` | Credential dict (decrypted from `DNSServer.credentials_encrypted`) | SDK / transport |
|---|---|---|---|
| Cloudflare | `cloudflare` | `{api_token}` — `account_id` optional, only consulted on zone create | plain `httpx` REST against `api.cloudflare.com/client/v4` (no vendor SDK) |
| Route 53 | `route53` | `{access_key_id, secret_access_key}` — global service, no region | `boto3` |
| Azure DNS | `azure_dns` | `{tenant_id, client_id, client_secret, subscription_id, resource_group}` | `azure-identity` + `azure-mgmt-dns` |
| Google Cloud DNS | `google_dns` | `{service_account_json, project_id}` | `google-cloud-dns` |

All SDK imports are lazy (inside the client factory) so the modules import cleanly without the optional wheel, and tests can patch the factory; blocking SDK calls run under `asyncio.to_thread`. Cloudflare is the exception — pure JSON over HTTPS, so it uses `httpx` directly.

### 4A.3 Capabilities + online-DNSSEC matrix

Each driver's `capabilities()` returns the same dict shape Windows / PowerDNS use (`agentless: True`, `manages_zones: True`, `views: False`, `rpz: False`, a `record_types` list, a `notes` blurb). The operator-visible differences:

| | Cloudflare | Route 53 | Azure DNS | Google Cloud DNS |
|---|:---:|:---:|:---:|:---:|
| `dnssec_online` | ❌ | ❌ | ❌ | ❌ |
| ALIAS records | apex CNAME auto-flattened | read-only (`AliasTarget`, null TTL; authoring deferred) | deferred | — |
| Apex handling | Cloudflare flattens CNAME-at-apex | apex SOA / NS provider-managed | apex SOA Azure-managed (skipped on read) | apex SOA provider-managed |

**No cloud driver advertises online DNSSEC sign/unsign (#29).** Cloud DNSSEC is a zone-level *provider* toggle — Route 53 needs a KMS asymmetric key, Cloudflare/Google a managed-zone enable — not the per-record online signing the `dnssec_sign`/`unsign` ops model, so every cloud driver's capability dict carries `dnssec_online: False` and the DNSSEC sign/unsign/rollover operations stay gated server-side by `_DRIVER_GATED_OPERATIONS` to PowerDNS / BIND9. Likewise **cloud ALIAS authoring is deferred** — Route 53 / Azure alias targets need a provider resource id the generic `ALIAS` record type can't express (so `alias_records: False` and `ALIAS` is gated to PowerDNS), though existing alias rrsets are still read back. Wiring real cloud DNSSEC (provider enable + DS retrieval) and cloud ALIAS authoring is a #29 follow-up.

### 4A.4 Provider-specific wrinkles the hooks paper over

- **Cloudflare** — every reply is wrapped in a `{success, errors, result, result_info}` envelope; `_unwrap` raises `CloudDNSError` on non-2xx *or* a `success: false` (the API returns 200 with `success: false` for some validation failures). The opaque zone id is resolved by name per call. "Automatic" TTL is the sentinel `1`, surfaced as `ttl=None`. `update` is create-on-miss.
- **Route 53** — MX / SRV priority is baked into the record value (`"10 mail.example.com."`), kept raw so it isn't double-encoded on write. ALIAS rrsets (`AliasTarget`) have no TTL → surfaced with `ttl=None`. Writes are `UPSERT`/`DELETE` change batches; a `DELETE` of a non-existent rrset (`InvalidChangeBatch`) is treated as an idempotent no-op. Hosted-zone id resolved from the FQDN via `list_hosted_zones_by_name` with an exact-name match.
- **Azure DNS** — records live in *record sets*, one per `(name, type)`, each with a typed list (`a_records`, `mx_records`, …); each set expands into one neutral `RecordData` per contained record. Create and update are both a `create_or_update` PUT of the full set. SOA is Azure-managed and dropped on read.
- **Google Cloud DNS** — calls scope by the managed-zone *id* (a slug like `example-com`), not the DNS name, so the hooks re-resolve the managed zone by matching `dns_name`. A single rrset carries one or more `rrdatas` (one `RecordData` each on read, collapsed to a single-value rrset on write). Writes are transactional change sets (`changes.create()`); the op polls `changes.status` until `done` (bounded ~60 s) so it only returns once Cloud DNS has applied it.

### 4A.5 Probe

`CloudDNSDriverBase.probe(server)` is the cheap credential check behind the Add-DNS-server **Test** button — by default it lists zones and reports the count, never raising for an expected failure (returns `ok=False` with the provider message). The Cloudflare driver's `_unwrap`, Route 53's `_is_invalid_change_batch`, Azure's `_wrap_errors`, and Google's `_wrap_call` all normalise raw SDK faults into operator-facing `CloudDNSError` messages first.

---

## 5. Driver Selection and Registration

Drivers are registered by name and instantiated by the service layer:

```python
# app/drivers/dns/__init__.py
_DRIVERS: dict[str, type[DNSDriver]] = {
    "bind9": BIND9Driver,
    "powerdns": PowerDNSDriver,
    "windows_dns": WindowsDNSDriver,
    # Agentless cloud-hosted DNS providers (issue #37, Part B).
    "cloudflare": CloudflareDNSDriver,
    "route53": Route53DNSDriver,
    "azure_dns": AzureDNSDriver,
    "google_dns": GoogleCloudDNSDriver,
}

# Drivers whose record ops run from the control plane directly, no agent.
AGENTLESS_DRIVERS: frozenset[str] = frozenset(
    {"windows_dns", "cloudflare", "route53", "azure_dns", "google_dns"}
)
# The cloud subset (used by the cloud import + sync-from-server widening).
CLOUD_DNS_DRIVERS: frozenset[str] = frozenset(
    {"cloudflare", "route53", "azure_dns", "google_dns"}
)

def get_driver(server_type: str) -> DNSDriver:
    cls = _DRIVERS.get(server_type)
    if cls is None:
        raise ValueError(f"Unknown DNS driver: {server_type!r}")
    return cls()
```

### 5.1 Per-group driver homogeneity

Each `DNSServerGroup` is **single-driver**. The control plane rejects mixed BIND + PowerDNS within one group because catalog-zone semantics, AXFR/IXFR shape, and the gate logic for PowerDNS-only features (ALIAS / LUA / online DNSSEC) all assume every member of the group runs the same driver.

Mixed installs work via multiple groups:

- "Internal-zones" group runs PowerDNS (LMDB-backed, fast apply, ALIAS records for apex)
- "External-zones" group runs BIND9 (battle-tested, RPZ for outbound blocklists, well-known operator surface)

### 5.2 Decision tree — when to pick which driver

| You want... | Pick |
|---|---|
| Reference impl, BIND muscle memory, RPZ blocking | **BIND9** |
| ALIAS records (CNAME at apex) | **PowerDNS** |
| LUA records (computed responses, geo-routing) | **PowerDNS** |
| One-toggle online DNSSEC with auto NSEC3 | **PowerDNS** |
| Manual NSEC3 + KSK / ZSK rollover control | **BIND9** |
| First-class views / split-horizon (issue #24) | **BIND9** |
| Catalog zones as **producer** | Either — same wire bytes |
| Catalog zones as **consumer** | **BIND9** today (PowerDNS waits for 4.10+) |
| Active Directory-integrated DNS | **Windows DNS** (separate path) |

Both BIND9 and PowerDNS drivers are supported indefinitely. PowerDNS landed in issue #127 as a second driver, not a replacement.

---

## 6. Error Handling

All driver methods must:
- Raise `DriverConnectionError` for network/auth failures
- Raise `DriverOperationError` for successful connection but failed operation
- Never swallow errors silently
- Log the full error details at `ERROR` level before raising
- Be safe to retry (idempotent where possible)

The service layer handles retry logic via Celery task retries — drivers are not responsible for retry.

---

## 7. Local Config Cache (DNS Agent)

Same agent caching model as DHCP (see DHCP spec). For DNS:

- Cached config includes: all zones + all records the server is authoritative for
- On control plane outage: DNS server continues serving from its own zone data (it always does — DNS servers are not stateless)
- The agent ensures the **last-known-good config** (zone files DB) is preserved
- On reconnect: agent fetches diff of changes made during outage and applies incrementally

### BIND9 Cache
- Zone files on local disk ARE the cache — BIND9 serves from them natively
- Agent tracks which zone file versions were last pushed by SpatiumDDI
- On reconnect: compare SpatiumDDI DB serial vs. zone file serial; apply missing changes

### PowerDNS Cache
- LMDB file at `/var/lib/powerdns/pdns.lmdb` IS the cache — `pdns_server` serves from it natively
- Agent tracks the last-applied `ConfigBundle` ETag in `/var/lib/spatium-dns-agent/state.json`
- On reconnect: long-poll picks up any new ETag, the agent reconciles by GET-ing PowerDNS's current zone list and PATCHing the rrset diff against the new ConfigBundle. Same convergence shape as BIND9.

