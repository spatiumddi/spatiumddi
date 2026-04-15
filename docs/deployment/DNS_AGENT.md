# DNS Agent / Container Architecture

> Design spec for how SpatiumDDI ships, enrolls, configures, and operates the
> managed DNS service containers (BIND9, PowerDNS) that sit on the data plane.
>
> **Status:** Design — no implementation yet. This document is the handoff
> contract for Wave 2 implementation agents.
>
> **Related:** `CLAUDE.md` (#5 config caching, #8 incremental DNS, #10 driver
> abstraction, #11 multi-arch), `docs/features/DNS.md`,
> `docs/drivers/DNS_DRIVERS.md`, `docs/OBSERVABILITY.md`.

---

## 0. Terminology

| Term | Meaning |
|---|---|
| **Control plane** | The SpatiumDDI FastAPI + PostgreSQL + Celery stack. Source of truth. |
| **Data plane** | Running DNS daemons (BIND9 / PowerDNS) that actually answer queries. |
| **Agent** | The SpatiumDDI-shipped sidecar process that supervises a DNS daemon, renders configs, applies records, and talks to the control plane. |
| **DNS container** | A container image containing both the DNS daemon and the agent. |
| **Driver** | Server-side (control-plane) Python code implementing `DNSDriverBase` per daemon flavor (see `docs/drivers/DNS_DRIVERS.md`). |

---

## 1. Container Role & Topology

### Decision

**One image per DNS flavor, agent baked in as a second process, supervised by a lightweight init (`tini` + a small Python supervisor).**

Two images ship in Phase 2:

| Image | Processes | Purpose |
|---|---|---|
| `ghcr.io/spatiumddi/dns-bind9` | `named` + `spatium-dns-agent` | Authoritative and/or recursive BIND9 |
| `ghcr.io/spatiumddi/dns-powerdns` | `pdns_server` **or** `pdns_recursor` + `spatium-dns-agent` | PowerDNS auth or recursor (flavor chosen by env var `PDNS_FLAVOR=auth|recursor`) |

The **agent is the same Python codebase** (`spatium_dns_agent`) in both images; the DNS daemon differs. The agent abstracts daemon specifics internally (symmetric to the control-plane driver, but on the container side).

### Rationale

- **Single image per flavor** keeps operational surface small and lets the agent run `rndc`, write `named.conf`, manage the pdns SQLite/pgsql backend, and own the daemon lifecycle locally — none of which a detached sidecar can do without shared volumes and ambient capabilities.
- **Not a standalone sidecar** because BIND9 config-file edits + `rndc reconfig` require filesystem and UNIX socket co-location. A sidecar model adds complexity (shared PID namespace, shared volumes) with no benefit at our scale.
- **Not a single universal image** because BIND9 and PowerDNS have wildly different footprints (Alpine `bind` + `bind-tools` ≈ 30 MB; PowerDNS auth + backends ≈ 80 MB). Bundling both bloats images and attack surface.

### Alternatives considered

- *Thin sidecar + upstream image* (e.g. `internetsystemsconsortium/bind9`): rejected — we lose control of base OS, healthchecks, multi-arch, and CVE response cadence.
- *Agent-less pure API management* (control plane SSHes into each server): rejected — violates non-negotiable #5 (local config cache) and is brittle across network partitions.

---

## 2. Auto-Registration Protocol

### Decision

**Pre-shared bootstrap key (`DNS_AGENT_KEY`) for first-contact registration, then per-server JWT (`agent_token`) issued by the control plane, rotated on each heartbeat.** The existing `/api/v1/dns/agents/register` + `/agents/{id}/heartbeat` endpoints are extended — the current shared-key model is kept as the *bootstrap* step and a token is issued on success.

### Flow

```
┌────────────────┐                                   ┌──────────────────┐
│ DNS container  │                                   │  Control plane   │
│  (fresh boot)  │                                   │   (FastAPI)      │
└───────┬────────┘                                   └────────┬─────────┘
        │                                                     │
        │ 1. Read env: CONTROL_PLANE_URL, DNS_AGENT_KEY,      │
        │              AGENT_ID (persisted in /var/lib)       │
        │                                                     │
        │ 2. If no cached agent_token.jwt → bootstrap:        │
        │    POST /dns/agents/register                        │
        │      Headers: X-DNS-Agent-Key: <bootstrap>          │
        │      Body: {hostname, driver, roles, version,       │
        │             group_name, fingerprint}                │
        │───────────────────────────────────────────────────► │
        │                                                     │ 3. Validate PSK
        │                                                     │    Create/update DNSServer row
        │                                                     │    Mark pending_approval=true
        │                                                     │      if settings.require_agent_approval
        │                                                     │    Mint agent_token (JWT, 24h)
        │  ◄────────────────────────────────────────────────  │
        │    200 {server_id, agent_token, config_etag, ...}   │
        │                                                     │
        │ 4. Persist token to /var/lib/spatium-dns-agent/     │
        │    agent_token.jwt (0600, owned by agent user)      │
        │                                                     │
        │ 5. From here on: Authorization: Bearer <agent_token>│
        │    Heartbeat every 30s — token rotated on rotation  │
        │    window (every 12h) via heartbeat response.       │
```

### Identity

- **AgentID** — UUID generated once on first boot, persisted in `/var/lib/spatium-dns-agent/agent-id`. Survives restarts; stable across re-registrations.
- **Fingerprint** — SHA-256 of a locally-generated ed25519 public key, sent with registration; pinned on the `DNSServer` row. A changed fingerprint on re-registration triggers `pending_approval=true` (anti-hijack).
- **Bootstrap key** rotation: admin rotates `DNS_AGENT_KEY`; existing agents already hold a valid JWT and are unaffected until re-bootstrap.

### Approval flow

A new platform setting `require_agent_approval: bool` (default **false** for homelab/single-tenant, recommended **true** for production) gates whether a freshly-registered agent is immediately active or sits in a `pending_approval` state visible in the DNS Server Group UI with an **Approve / Reject** action. Until approved:
- Agent receives `200` and a token but `config_version = null`.
- No config is served.
- Heartbeats still accepted (for telemetry).

### Re-registration

On restart, the agent tries its cached token first. If the control plane returns `401`, it falls back to bootstrap with the PSK. If the PSK has rotated too, the agent logs and enters a retry loop with jittered backoff (cap 5 min).

### Alternatives considered

- **mTLS with internal CA** — more robust but requires a CA pipeline (cert-manager in K8s, something custom in Docker Compose). Deferred to Phase 4; the token model is a clean superset.
- **First-contact UI approval with no PSK** (Tailscale-style) — better UX but requires a pre-enrolled claim code in the container. Equivalent to our PSK with extra steps.

---

## 3. Config Sync Model

### Decision

**Hybrid: long-poll for config, push for urgent record changes, local disk cache as the source of truth for daemon operation.**

Three channels:

| Channel | Direction | Transport | Purpose |
|---|---|---|---|
| **Config long-poll** | Agent → CP | `GET /dns/agents/{id}/config?etag=<current>` (30 s hold) | Full config bundle (views, ACLs, options, zone list). Returns `304` if unchanged, `200` with new bundle + new etag on change. |
| **Heartbeat** | Agent → CP | `POST /dns/agents/{id}/heartbeat` (30 s interval) | Liveness, daemon status, version, queued-change ACK, token rotation. |
| **Record fast-path** | CP → Agent (logically, but still agent-pulls) | Config long-poll response carries `pending_record_ops[]`; agent ACKs each op-id on heartbeat. | Per-record RFC 2136 / pdns-API changes delivered near-real-time via the long-poll's early return. |

**Why not push / webhook from control plane to agent?**
- Requires agent to expose an HTTPS listener, open an inbound port, and obtain a valid TLS cert. Non-negotiable #6 and general operational cost.
- Breaks behind NAT (on-prem appliances reaching a central control plane).
- Long-poll gives ~1 s effective latency and keeps the agent **egress-only**.

**Why not pure polling (e.g. 30 s)?**
- Record changes in DDNS flow must feel instant. Long-poll early-return delivers in <1 s.

**Why not WebSocket / SSE?**
- We considered it. Long-poll is simpler, survives hostile proxies, does not need sticky-session affinity on a multi-replica API. We can upgrade to SSE in a later phase without changing the agent contract (long-poll remains a compatible fallback).

### RFC 2136 `nsupdate` responsibility

**Agent-local.** The control-plane BIND9 driver does **not** connect to `named` directly. Instead:

1. Control plane computes the record delta and writes `pending_record_ops` rows.
2. The agent pulls them via config long-poll.
3. The agent invokes `nsupdate` (or the PowerDNS local API on `127.0.0.1:8081`) **against its own daemon over loopback**.
4. The agent ACKs success/failure per-op on the next heartbeat.

Rationale: loopback `nsupdate` is simpler, never traverses the network as a TSIG-sensitive payload, and makes the agent the single enforcer of the local daemon state. The TSIG key lives only on the container.

The control-plane `DNSDriverBase` implementations become **thin**: they translate the DB model into a canonical `AgentConfigBundle` + `RecordOp` list. They do not speak `nsupdate` or pdns-API directly.

### Local disk cache (non-negotiable #5)

Layout on `/var/lib/spatium-dns-agent/`:

```
agent-id                         # UUID, 0600
agent_token.jwt                  # current JWT, 0600
bootstrap.last                   # last-used PSK hash (for rotation detect)
config/
  current.json                   # last-applied AgentConfigBundle (ETag in header field)
  current.etag
  previous.json                  # rollback copy
rendered/
  named.conf                     # BIND9 — or pdns.conf, recursor.conf
  zones/
    example.com.db
    10.in-addr.arpa.db
  rpz/
    spatium-blocklist.rpz
tsig/
  ddns.key                       # 0600, owned by agent user; read by named via include
ops/
  inflight/                      # one file per unacked RecordOp
  failed/                        # ops that exhausted retries (surfaced in heartbeat)
```

**Offline operation**: if the control plane is unreachable on boot, the agent loads `config/current.json`, renders configs if not already rendered, starts the daemon, and continues serving DNS. It enters a retry loop and resumes sync when the control plane returns. No query path ever depends on control-plane reachability.

**Atomic apply**: new configs are rendered to `rendered.new/`, validated (`named-checkconf`, `pdnsutil check-all-zones`), swapped by rename, then `rndc reconfig` / `pdns_control reload` is issued. On validation failure the daemon keeps serving the previous config and the agent reports `status=degraded, reason=config_validation_failed`.

---

## 4. Health & Telemetry

### What the agent reports (heartbeat body)

```json
{
  "agent_version": "2026.04.13-1",
  "daemon": {
    "flavor": "bind9",
    "version": "9.20.1",
    "running": true,
    "pid": 12,
    "started_at": "2026-04-14T12:00:00Z",
    "queries_per_sec_1m": 42.1,
    "cache_hit_ratio_5m": 0.87
  },
  "config": { "etag": "sha256:...", "applied_at": "..." },
  "ops_ack": [
    {"op_id": "...", "result": "ok"},
    {"op_id": "...", "result": "error", "message": "NXRRSET"}
  ],
  "failed_ops_count": 0,
  "disk_free_bytes": 8123456789,
  "zone_serials": {"example.com.": 2026041407}
}
```

### Endpoints

| Endpoint | Direction | Cadence |
|---|---|---|
| `POST /dns/agents/{id}/heartbeat` | agent → CP | every 30 s (jittered ±3 s) |
| `GET  /dns/agents/{id}/config` | agent → CP | continuous long-poll (30 s hold) |
| `POST /dns/agents/{id}/ops/{op_id}/ack` | agent → CP | piggybacked on heartbeat; separate endpoint reserved for out-of-band recovery |
| `GET  /dns/agents/{id}/diagnostics` | admin UI → CP → agent | pull-through for logs / `rndc status` (Phase 3) |

### Stale / unhealthy surfacing

- Control plane marks `DNSServer.status = unreachable` if no heartbeat for **3 × heartbeat_interval** (90 s default).
- UI: the existing server-group view shows colored status dots; a new "Last seen" column uses `last_health_check_at`.
- A Celery beat job `dns_agent_stale_sweep` runs every 60 s, flips statuses, emits an audit entry, and triggers notifications (Phase 4).
- Metrics: Prometheus gauges `spatium_dns_agent_up`, `spatium_dns_agent_config_lag_seconds`, `spatium_dns_zone_serial`, `spatium_dns_failed_ops_total` — scraped from the control plane (agent does not expose a scrape endpoint; keeps it egress-only).

---

## 5. Incremental Updates (Non-Negotiable #8)

### Record lifecycle end-to-end

```
UI / API mutation (e.g. POST /dns/zones/{id}/records)
        │
        ▼
Service layer: validate, write DNSRecord row, compute delta
        │
        ▼
Enqueue RecordOp rows:
  { id, server_id, zone_name, op: create|update|delete, record: {...},
    serial_strategy: bump, created_at, state: pending }
        │
        ▼
Response returned to caller (HTTP 2xx) — the write is durable in Postgres.
        │
        ▼
Agents holding a long-poll on /config are released with op list.
(Or: next long-poll picks it up within ≤30 s; idempotent by op_id.)
        │
        ▼
Agent executes via loopback:
  BIND9   → nsupdate ‹signed with local TSIG›
  PowerDNS→ PATCH /api/v1/servers/localhost/zones/<z>  (127.0.0.1:8081)
        │
        ▼
Agent ACKs on next heartbeat → RecordOp.state = applied
If failed after N retries (default 5, expo-backoff): state=failed, alert.
```

### Serial bump responsibility

- **Control plane bumps the logical serial** when constructing the op: `YYYYMMDDNN` format, monotonically increasing per zone, persisted on `DNSZone.last_serial`.
- The op carries the target serial. The agent's `nsupdate` script explicitly deletes + re-adds the SOA with the target serial in the same update transaction (atomic under RFC 2136).
- For PowerDNS, the API zone `PATCH` includes the `serial` field.
- **Secondary servers** (same group, different `DNSServer` rows) do **not** receive record ops — the primary notifies them natively (BIND9 `notify` or PowerDNS AXFR). The agent on a secondary only syncs config (ACLs, views, zone definitions), never individual records.

### Primary/secondary coordination

- A `DNSServer.roles` array already exists. Extend semantics: within a group, exactly one server is `is_primary=true` per zone (new column on a `DNSZoneAssignment` join, or on `DNSServer.is_primary` for the simple case).
- Record ops target the primary only. Secondaries receive NOTIFY + AXFR/IXFR from the primary (standard DNS). SpatiumDDI does not proxy records to secondaries.
- If the primary agent is unreachable, ops queue in `RecordOp(state=pending)` and drain when it returns; the UI shows a "N record updates pending" banner on the zone.

---

## 6. Security

| Concern | Decision |
|---|---|
| **Bootstrap PSK** | `DNS_AGENT_KEY` env var on both control plane and agent. 32-byte random (`openssl rand -hex 32`). Rotatable. Compared with `hmac.compare_digest`. |
| **Agent token** | JWT (HS256) signed by control-plane `SECRET_KEY`, 24 h lifetime, rotated silently via heartbeat response if within 12 h of expiry. Claims: `sub=server_id`, `agent_id`, `fingerprint`, `exp`. |
| **TSIG keys** | Generated by control plane on zone bind, stored encrypted at rest (Fernet, `SECRET_KEY`-derived). Transmitted to agent inside the config bundle over TLS. Agent writes to `tsig/ddns.key` at 0600, referenced by `named.conf` via `include`. |
| **PowerDNS API key** | Per-server, generated at registration, written to `pdns.conf` with `api-key=` and `webserver-address=127.0.0.1`. Never exposed externally. |
| **Network exposure** | Agent is **egress-only**. Daemon listens on 53/udp+tcp (+ 853/tcp for DoT in Phase 3). No agent management port. DNS daemon's control socket (`rndc`, `pdns_control`) is a UNIX socket inside the container. |
| **TLS** | Agent↔CP is HTTPS-only. CP cert verified against the system CA bundle (+ optional `CA_BUNDLE_PATH` env for private CAs). Self-signed dev certs only when `SPATIUM_INSECURE_SKIP_TLS_VERIFY=1` (dev only). |
| **RBAC between agents** | An agent's JWT is scoped to its `server_id`. Config endpoint rejects requests for any other server. Record ops are likewise `server_id`-scoped; an agent cannot fetch another server's TSIG keys. |
| **Secret storage in container** | All secrets on a tmpfs-backed writable volume (`/var/lib/spatium-dns-agent`). Agent drops privileges after startup; runs as UID `spatium` (non-root). DNS daemon runs as its own unprivileged user (`named`, `pdns`). |
| **Audit** | Every config apply, op-apply, token rotation, and failed auth is audit-logged on the control plane. Agent-local audit is kept on disk for 7 days (rotated) and surfaced via `/agents/{id}/diagnostics`. |

---

## 7. Image Layout

### Base

**Alpine 3.20** (per CLAUDE.md) for both images. Multi-arch: `linux/amd64`, `linux/arm64/v8` via `docker buildx`.

### `dns-bind9` image

```
FROM alpine:3.20 AS runtime
RUN apk add --no-cache bind bind-tools tini python3 py3-pip ca-certificates tzdata
# Agent
COPY --from=agent-build /install /usr/local
COPY entrypoint.py /usr/local/bin/spatium-dns-entrypoint
RUN addgroup -S spatium && adduser -S -G spatium spatium \
 && mkdir -p /var/lib/spatium-dns-agent && chown spatium:spatium /var/lib/spatium-dns-agent
VOLUME ["/var/lib/spatium-dns-agent", "/var/cache/bind"]
EXPOSE 53/udp 53/tcp
ENTRYPOINT ["/sbin/tini", "--", "spatium-dns-entrypoint"]
```

Entrypoint (`entrypoint.py`) responsibilities:

1. Load/generate `agent-id`.
2. Bootstrap / token refresh against control plane.
3. Pull initial config bundle, render `named.conf`, zone files, RPZ files, TSIG keys.
4. Validate with `named-checkconf`.
5. `exec` a supervisor that runs two children: `named -g -u named` and the agent's sync loop. If either exits, kill the other and exit non-zero (let the orchestrator restart the container).

### `dns-powerdns` image

```
FROM alpine:3.20
RUN apk add --no-cache pdns pdns-backend-sqlite3 pdns-backend-lmdb pdns-recursor \
                      tini python3 py3-pip ca-certificates tzdata
# ... agent install same as above ...
EXPOSE 53/udp 53/tcp 8081/tcp  # 8081 bound to 127.0.0.1 only
```

`PDNS_FLAVOR=auth|recursor` selects which binary the supervisor starts.

### Volumes

| Path | Purpose | Typical size |
|---|---|---|
| `/var/lib/spatium-dns-agent` | Agent state, config cache, TSIG, tokens | <10 MB |
| `/var/cache/bind` (bind9 image) | Zone files, journals | grows with zone count |
| `/var/lib/powerdns` (pdns auth image) | SQLite/LMDB backend | grows with zone count |

All three must survive restarts → named volumes in Compose / PVCs in K8s.

---

## 8. Kubernetes Shape

### Decision

**One `StatefulSet` per `DNSServer` row**, not per group. Headless `Service` per StatefulSet (ClusterIP=None) plus an externally-exposed `Service` of type `LoadBalancer` or `NodePort` for DNS traffic (UDP/TCP 53).

### Rationale

- DNS servers have **stable identity** (primary vs secondary, TSIG keys scoped per server, persistent zone files). That matches StatefulSet semantics.
- Multiple DNS servers in one group are not interchangeable replicas — primary vs secondary roles matter for AXFR/NOTIFY. **Not a Deployment**, because Deployment replicas are fungible.
- **Not a DaemonSet**, because we do not want a DNS server on every node; placement is explicit.
- Shape:

```
DNSServerGroup "internal-resolvers"
 ├── StatefulSet/dns-internal-ns1  (replicas=1, role=primary)
 │    └── Service/dns-internal-ns1 (LoadBalancer, 53/udp+tcp)
 └── StatefulSet/dns-internal-ns2  (replicas=1, role=secondary)
      └── Service/dns-internal-ns2 (LoadBalancer, 53/udp+tcp)
```

- Primary/secondary coordination uses **native DNS NOTIFY + AXFR/IXFR** over the cluster network. The agent on the secondary knows the primary's in-cluster DNS name (`dns-internal-ns1.spatiumddi.svc.cluster.local`) via its config bundle.
- Anti-affinity rules ensure ns1 and ns2 land on different nodes.

### Helm chart structure (Phase 2 deliverable)

```
charts/spatium-dns/
  Chart.yaml
  values.yaml                # defines servers[] with name, flavor, role, resources
  templates/
    statefulset.yaml         # one per item in .Values.servers
    service-dns.yaml         # LB per server
    service-headless.yaml
    pdb.yaml
    configmap-bootstrap.yaml # non-secret bootstrap config
    secret-agent-key.yaml    # references existing secret created by user
    servicemonitor.yaml      # optional, Prometheus
```

The SpatiumDDI control-plane operator (Phase 3 stretch) can render this from the `DNSServerGroup` DB state, but Phase 2 ships only the static Helm chart driven by `values.yaml`.

### Docker Compose shape

One service per DNS server. Example added to `docker-compose.yml`:

```yaml
dns-bind9-primary:
  image: ghcr.io/spatiumddi/dns-bind9:${SPATIUM_VERSION}
  environment:
    CONTROL_PLANE_URL: http://api:8000
    DNS_AGENT_KEY: ${DNS_AGENT_KEY}
    AGENT_HOSTNAME: dns-bind9-primary
    AGENT_ROLE: primary
    AGENT_GROUP: default
  volumes:
    - dns-bind9-primary-state:/var/lib/spatium-dns-agent
    - dns-bind9-primary-cache:/var/cache/bind
  ports:
    - "53:53/udp"
    - "53:53/tcp"
```

---

## 9. Deliverables for Wave 2 Implementation

### Backend (control plane)

| File | Purpose |
|---|---|
| `backend/app/api/v1/dns/agents.py` | Split agent endpoints out of `router.py`: `register`, `heartbeat`, `config` (long-poll), `ops/ack`. |
| `backend/app/services/dns/agent_config.py` | Builds the `AgentConfigBundle` from DB state (zones, views, ACLs, options, TSIG keys, forwarders, blocklists). |
| `backend/app/services/dns/record_ops.py` | Enqueues `RecordOp` rows on record mutations; resolves primary server per zone. |
| `backend/app/services/dns/agent_token.py` | JWT mint/verify/rotate. |
| `backend/app/models/dns.py` | Extend with: `DNSServer.agent_id`, `fingerprint`, `pending_approval`, `is_primary`; new `RecordOp` model. |
| `backend/alembic/versions/<new>_dns_agent_ops.py` | Migration for the above. |
| `backend/app/tasks/dns.py` | Celery beat `dns_agent_stale_sweep`. |
| `backend/app/core/config.py` | Settings: `DNS_AGENT_TOKEN_TTL`, `DNS_AGENT_LONGPOLL_TIMEOUT`, `require_agent_approval`. |

### Agent (new codebase)

| File | Purpose |
|---|---|
| `agent/dns/pyproject.toml` | Package `spatium-dns-agent`. |
| `agent/dns/spatium_dns_agent/__main__.py` | CLI entry; loads env, delegates to supervisor. |
| `agent/dns/spatium_dns_agent/supervisor.py` | tini-child; runs daemon + sync loop. |
| `agent/dns/spatium_dns_agent/bootstrap.py` | PSK registration + token persistence. |
| `agent/dns/spatium_dns_agent/sync.py` | Long-poll loop, config apply, op execution. |
| `agent/dns/spatium_dns_agent/cache.py` | Disk-cache read/write, atomic swap, rollback. |
| `agent/dns/spatium_dns_agent/drivers/bind9.py` | Render `named.conf`, zone files, RPZ; `nsupdate` loopback; `rndc reconfig`. |
| `agent/dns/spatium_dns_agent/drivers/powerdns.py` | Render `pdns.conf`; local REST API calls. |
| `agent/dns/spatium_dns_agent/heartbeat.py` | Heartbeat body, token rotation. |
| `agent/dns/tests/` | Unit + integration (testcontainers with real `named`/`pdns`). |

### Container images

| Path | Purpose |
|---|---|
| `agent/dns/images/bind9/Dockerfile` | Alpine + BIND9 + agent, multi-arch. |
| `agent/dns/images/bind9/entrypoint.py` | Process-1 entrypoint. |
| `agent/dns/images/powerdns/Dockerfile` | Alpine + PowerDNS auth + recursor + agent. |
| `agent/dns/images/powerdns/entrypoint.py` | Entrypoint; reads `PDNS_FLAVOR`. |
| `.github/workflows/build-dns-images.yml` | buildx, amd64+arm64, push to `ghcr.io/spatiumddi/*`. |

### Kubernetes

| Path | Purpose |
|---|---|
| `k8s/dns/bind9-statefulset.yaml` | Reference StatefulSet. |
| `k8s/dns/powerdns-statefulset.yaml` | Reference StatefulSet. |
| `k8s/dns/service-dns.yaml` | Example LoadBalancer service (UDP+TCP 53). |
| `charts/spatium-dns/` | Helm chart (Phase 2.5). |
| `k8s/README.md` | Add "DNS server deployment" section. |

### Docker Compose

| File | Change |
|---|---|
| `docker-compose.yml` | Add optional `dns-bind9-primary` + `dns-bind9-secondary` services under a `dns` Compose profile. |
| `.env.example` | `DNS_AGENT_KEY=` (with `openssl rand -hex 32` hint). |

### Docs

| File | Change |
|---|---|
| `CLAUDE.md` | Add `docs/deployment/DNS_AGENT.md` to Document Map. |
| `docs/deployment/DNS_AGENT.md` | **This document.** |
| `docs/drivers/DNS_DRIVERS.md` | Update: clarify that drivers emit `AgentConfigBundle`/`RecordOp` rather than speaking to daemons directly. |
| `docs/features/DNS.md` | Cross-link to this doc from §6 and §7. |
| `docs/OBSERVABILITY.md` | Add agent metrics (`spatium_dns_agent_*`). |

### Acceptance criteria for Wave 2

1. `docker compose --profile dns up` starts a BIND9 container that auto-registers and appears in the DNS UI within 10 s.
2. Creating an A record via the UI results in the record being resolvable via `dig @<container-ip> ...` within 2 s.
3. Killing the control plane (`docker compose stop api worker`) does not interrupt DNS resolution; restarting it within 1 h resumes sync with no record loss.
4. The container image passes `trivy` with no high/critical CVEs at build time.
5. Helm chart deploys a 2-server group (primary + secondary) in `kind`; AXFR between them is observed in logs.

---

## 10. Open Questions (Deferred)

- **mTLS vs JWT**: reconsider in Phase 4 once we have an internal CA story.
- **IPv6-only deployments**: agent must support AAAA-only control-plane URL; fine in theory, test in Phase 3.
- **Windows DNS integration**: explicitly out of scope for the agent model — Windows servers are managed via WinRM from the control plane (different driver branch, see roadmap).
- **DNSSEC signing (online vs bump-in-the-wire)**: BIND9 inline-signing is assumed; PowerDNS pdnsutil-driven. Key storage and rotation design is a separate doc (`docs/features/DNS_DNSSEC.md`, Phase 3).
