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

### Update Strategy: REST API (all operations, no restarts)

No SSH, no config file management, no restarts for any normal operation.

| Operation | API Call | Notes |
|---|---|---|
| Add record | `PATCH /api/v1/servers/localhost/zones/{zone}` | Atomic RRset update |
| Update record | `PATCH /api/v1/servers/localhost/zones/{zone}` | Same endpoint |
| Delete record | `PATCH /api/v1/servers/localhost/zones/{zone}` with `changetype: DELETE` | |
| Create zone | `POST /api/v1/servers/localhost/zones` | |
| Delete zone | `DELETE /api/v1/servers/localhost/zones/{zone}` | |
| Notify secondaries | `PUT /api/v1/servers/localhost/zones/{zone}/notify` | Triggers AXFR to secondaries |
| Get zone info | `GET /api/v1/servers/localhost/zones/{zone}` | |

```python
async def update_record(self, zone_name: str, record: DNSRecordData) -> None:
    payload = {
        "rrsets": [{
            "name": f"{record.name}.{zone_name}",
            "type": record.record_type,
            "ttl": record.ttl,
            "changetype": "REPLACE",
            "records": [{"content": record.value, "disabled": False}]
        }]
    }
    async with self.session.patch(
        f"{self.api_url}/zones/{zone_name}",
        json=payload
    ) as resp:
        resp.raise_for_status()
```

Options:

1. **Multiple instances** (recommended): Run separate g.,  on port 5353,  on port 53). Each has its own API endpoint. The driver connects to the appropriate instance based on the view.

2. **GeoIP backend**: For geolocation-based routing (more complex, Phase 3).

The driver's `view_id` parameter selects which 
```python
@dataclass
    api_key: str                  # Encrypted in DB (X-API-Key header)
    timeout_seconds: int = 10
    verify_ssl: bool = True
```

---

## 4. Driver Selection and Registration

Drivers are registered by name and instantiated by the service layer:

```python
# app/drivers/dns/__init__.py
DNS_DRIVERS: dict[str, type[DNSDriverBase]] = {
    "bind9": BIND9Driver,
}

def get_dns_driver(server: DNSServer) -> DNSDriverBase:
    driver_class = DNS_DRIVERS.get(server.driver)
    if not driver_class:
        raise ValueError(f"Unknown DNS driver: {server.driver}")
    config = decrypt_server_credentials(server)
    return driver_class(config)
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

