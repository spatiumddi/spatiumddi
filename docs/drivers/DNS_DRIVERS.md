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

In [`app/drivers/dns/registry.py`](../../backend/app/drivers/dns/registry.py):

```python
AGENTLESS_DRIVERS: frozenset[str] = frozenset({"windows_dns"})
```

Agentless drivers don't emit a `ConfigBundle`; the API's `record_ops.enqueue_record_op` short-circuits them and calls `apply_record_change` directly instead of queueing for a co-located agent.

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

## 4. Driver Selection and Registration

Drivers are registered by name and instantiated by the service layer:

```python
# app/drivers/dns/registry.py
_DRIVERS: dict[str, type[DNSDriver]] = {
    "bind9": BIND9Driver,
    "windows_dns": WindowsDNSDriver,
}

AGENTLESS_DRIVERS: frozenset[str] = frozenset({"windows_dns"})

def get_driver(server_type: str) -> DNSDriver:
    cls = _DRIVERS.get(server_type)
    if cls is None:
        raise ValueError(f"Unknown DNS driver: {server_type!r}")
    return cls()
```

---

## 5. Error Handling

All driver methods must:
- Raise `DriverConnectionError` for network/auth failures
- Raise `DriverOperationError` for successful connection but failed operation
- Never swallow errors silently
- Log the full error details at `ERROR` level before raising
- Be safe to retry (idempotent where possible)

The service layer handles retry logic via Celery task retries — drivers are not responsible for retry.

---

## 6. Local Config Cache (DNS Agent)

Same agent caching model as DHCP (see DHCP spec). For DNS:

- Cached config includes: all zones + all records the server is authoritative for
- On control plane outage: DNS server continues serving from its own zone data (it always does — DNS servers are not stateless)
- The agent ensures the **last-known-good config** (zone files DB) is preserved
- On reconnect: agent fetches diff of changes made during outage and applies incrementally

### BIND9 Cache
- Zone files on local disk ARE the cache — BIND9 serves from them natively
- Agent tracks which zone file versions were last pushed by SpatiumDDI
- On reconnect: compare SpatiumDDI DB serial vs. zone file serial; apply missing changes

