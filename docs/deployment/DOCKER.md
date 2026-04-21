# Docker Compose Deployment

## Prerequisites

- Docker Engine 25+ and Docker Compose v2.20+
- 2 GB RAM minimum (4 GB recommended)
- Ports 80 (frontend) and optionally 8000 (API direct access) free on the host

---

## 1. Port Reference

| Port | Service | Protocol | Notes |
|---|---|---|---|
| 80 | Frontend (nginx) | HTTP | Configurable via `HTTP_PORT` env var |
| 443 | Frontend (nginx) | HTTPS | When TLS is configured (see §5) |
| 8000 | API (uvicorn) | HTTP | Configurable via `API_PORT` env var; internal only in production |
| 5432 | PostgreSQL | TCP | Internal only — never expose externally |
| 6379 | Redis | TCP | Internal only — never expose externally |

All services communicate on the `spatiumddi` Docker bridge network. Only the frontend and API ports are published to the host.

---

## 2. First-Time Setup

```bash
# Clone the repository
git clone https://github.com/spatiumddi/spatiumddi.git
cd spatiumddi

# Create your environment file
cp .env.example .env

# Edit .env — at minimum change POSTGRES_PASSWORD and SECRET_KEY
# SECRET_KEY: openssl rand -hex 32
nano .env

# Build images
docker compose build

# Run database migrations
docker compose run --rm migrate

# Start all services
docker compose up -d
```

The API automatically creates a default admin user on first startup if no users exist:
- **Username:** `admin`
- **Password:** `admin`
- **Force password change:** Yes — you will be redirected to the change-password page on first login.

Access the UI at `http://your-host-or-ip/` (or `http://localhost/` if running locally).

---

## 3. Environment Variables

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_PASSWORD` | `changeme` | PostgreSQL password — **must change** |
| `SECRET_KEY` | (none) | JWT signing key — **must change** (use `openssl rand -hex 32`) |
| `HTTP_PORT` | `80` | Host port for the frontend |
| `API_PORT` | `8000` | Host port for the API (set to `127.0.0.1:8000:8000` to restrict to localhost) |
| `DATABASE_URL` | auto-constructed | Override only if using an external PostgreSQL |
| `REDIS_URL` | `redis://redis:6379/0` | Override to point at an external Redis |
| `DEBUG` | `false` | Enable FastAPI debug mode |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | JWT access token lifetime |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token lifetime |

---

## 4. Resetting the Admin Password (CLI)

If the admin password is lost, reset it directly against the database inside the API container:

```bash
docker compose exec api python - <<'EOF'
from app.core.security import hash_password
import asyncio
from sqlalchemy import update
from app.db import AsyncSessionLocal
from app.models.auth import User

async def reset():
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(User)
            .where(User.username == "admin")
            .values(
                hashed_password=hash_password("NewPassword123!"),
                force_password_change=True,
            )
        )
        await db.commit()
        print("Password reset OK")

asyncio.run(reset())
EOF
```

Or use the management script (once it is written — Phase 4):
```bash
docker compose exec api python -m spatiumddi.cli reset-password admin
```

---

## 5. TLS / HTTPS

### Option A: Terminate TLS at the host with nginx (recommended for VMs)

Place a reverse proxy in front of the `spatiumddi-frontend-1` container:

```nginx
server {
    listen 443 ssl;
    server_name spatiumddi.example.com;

    ssl_certificate     /etc/letsencrypt/live/spatiumddi.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/spatiumddi.example.com/privkey.pem;

    location / {
        proxy_pass         http://localhost:80;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
    }
}
```

### Option B: Caddy (automatic Let's Encrypt — simplest for appliance installs)

```caddyfile
spatiumddi.example.com {
    reverse_proxy localhost:80
}
```

Caddy handles ACME certificate issuance and renewal automatically.

### Option C: TLS inside the nginx container

Mount your certificate files and an updated `nginx.conf` into the frontend container:

```yaml
# In docker-compose.override.yml
services:
  frontend:
    ports:
      - "443:443"
    volumes:
      - /etc/letsencrypt:/etc/letsencrypt:ro
      - ./nginx-ssl.conf:/etc/nginx/conf.d/default.conf:ro
```

### ACME / DNS Challenge (for IPAM-integrated certificate management)

SpatiumDDI's DNS module exposes an API endpoint (`POST /api/v1/dns/acme-challenge`) for creating and removing ACME DNS-01 challenge TXT records. This allows ACME clients (certbot, acme.sh) to complete DNS challenges using SpatiumDDI as the DNS backend:

```bash
# Example with acme.sh using the SpatiumDDI DNS hook
export SPATIUMDDI_URL=https://spatiumddi.example.com
export SPATIUMDDI_API_TOKEN=your-api-token
acme.sh --issue --dns dns_spatiumddi -d your-domain.example.com
```

See `docs/features/DNS.md` for the full ACME API specification.

---

## 6. PostgreSQL High Availability (Docker Compose)

For single-server deployments, the default single PostgreSQL container is sufficient. For HA:

- **Patroni + etcd + HAProxy**: See `k8s/ha/postgres-docker-compose.yaml`
- Connect your `.env` `DATABASE_URL` to HAProxy port 5000 (primary) instead of the `postgres` container

For multi-server deployments, use Kubernetes with CloudNativePG (see `k8s/README.md`).

---

## 7. Redis High Availability (Docker Compose)

The default single Redis container uses `maxmemory-policy allkeys-lru` for Celery task queues. For HA:

- Use Redis Sentinel (3 nodes) — see `k8s/ha/redis-sentinel.yaml` for the manifest pattern
- Or use Valkey Cluster (3+ nodes) for horizontal scale
- Update `REDIS_URL`, `CELERY_BROKER_URL`, and `CELERY_RESULT_BACKEND` to point at the Sentinel/cluster endpoint

---

## 8. Upgrading

```bash
# Pull latest code
git pull

# Rebuild images
docker compose build

# Run new migrations (safe to run — Alembic is idempotent)
docker compose run --rm migrate

# Restart services with zero-downtime rolling update
docker compose up -d --force-recreate api worker beat frontend
```

---

## 9. Backup and Restore

### PostgreSQL backup

```bash
# Dump
docker compose exec -T postgres pg_dump -U spatiumddi spatiumddi | gzip > backup-$(date +%Y%m%d).sql.gz

# Restore
gunzip -c backup-YYYYMMDD.sql.gz | docker compose exec -T postgres psql -U spatiumddi spatiumddi
```

### Redis backup

Redis persistence (`appendonly yes`) is enabled. The RDB/AOF files are in the `redis_data` volume. For point-in-time backup, also copy that volume.

---

## 10. Distributed Agent Deployments

For real production use you generally don't want the Kea DHCP agent and the BIND9 DNS agent running on the same host as the control plane. SpatiumDDI ships two standalone compose files for that shape:

| File | Purpose |
|---|---|
| `docker-compose.agent-dhcp.yml` | Kea DHCP agent(s) only — no control plane |
| `docker-compose.agent-dns.yml`  | BIND9 DNS agent only — no control plane |

The agent containers long-poll the remote control plane's API and cache the last-known-good config locally (non-negotiable #5), so the DHCP / DNS services keep serving even if the control plane is briefly unreachable.

### Prerequisites

1. Control plane already running somewhere reachable (e.g. `https://spatium.example.com`).
2. Generate an agent key and register it on the control plane before starting the agent. Settings → Agent Keys (or `POST /api/v1/agent-keys`) — the control plane rejects bootstrap attempts with an unknown key.
3. If the control plane uses a self-signed cert, either mount a CA bundle at `/etc/ssl/certs/spatium-ca.crt` and set `TLS_CA_PATH`, or (lab-only) leave `SPATIUM_INSECURE_SKIP_TLS_VERIFY=1`.

### DHCP-only VM

```bash
# On the DHCP VM (separate host from the control plane):
export SPATIUM_API_URL=https://spatium.example.com
export SPATIUM_AGENT_KEY=$(openssl rand -hex 32)
export DHCP_HOSTNAME=dhcp-kea-east    # unique across the deployment

# Single Kea node:
docker compose -f docker-compose.agent-dhcp.yml up -d

# Local HA pair (rare in prod — usually each peer goes on its own VM):
docker compose -f docker-compose.agent-dhcp.yml --profile dhcp-ha up -d
```

For a **true HA pair across two VMs**, run the same compose file on each VM with a different `DHCP_HOSTNAME` (say `dhcp-kea-east` and `dhcp-kea-west`) and the same `AGENT_GROUP`. On the control plane, edit the DHCP Server Group's HA mode (hot-standby or load-balancing) and set each server's `ha_peer_url` to the other peer's reachable URL. The agent resolves peer hostnames at render time and the `PeerResolveWatcher` thread keeps them fresh if IPs change.

### DNS-only VM

```bash
export CONTROL_PLANE_URL=https://spatium.example.com
export DNS_AGENT_KEY=$(openssl rand -hex 32)
export DNS_HOSTNAME=dns-bind9-east

docker compose -f docker-compose.agent-dns.yml up -d
```

For authoritative + secondary pairs, run this on each additional DNS VM with a unique `DNS_HOSTNAME` and the same `AGENT_GROUP`. Zone assignments and view membership are configured on the control plane.

### Host vs bridge networking

Default ports in both files map to non-53 / non-67 host ports (5353/udp+tcp, 6767/udp) so the containers don't collide with systemd-resolved or a running dhcp client on the host.

For **real DNS / DHCP serving** you want `network_mode: host` so the daemon binds 53 / 67 directly and, for DHCP, receives L2 broadcasts on the host NIC. Add to the service definition:

```yaml
services:
  dhcp-kea:
    network_mode: host
    # ...remove the `networks:` + `ports:` keys when host-networked.
```

### Observability

Once registered, the agent shows up on the control plane's Dashboard and in DHCP → Servers or DNS → Servers. Heartbeat / online status updates every 30s.
