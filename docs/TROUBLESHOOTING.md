---
layout: default
title: Troubleshooting
---

# Troubleshooting

Recovery recipes for common incidents. Pages that cover a specific
feature (e.g. `docs/features/DHCP.md`) generally describe the *expected*
behaviour; this page covers what to do when something goes wrong.

---

## Recovering from an accidentally deleted DNS or DHCP server

**Symptom** — You deleted a managed DNS (`bind9`) or DHCP (`kea`) server
from the SpatiumDDI UI (or via the REST API) and it turned out to be one
of the real, running service containers rather than a stale row.

**What happened**

Deleting a server from the GUI drops the `dns_server` or `dhcp_server`
row from the control plane database. It does **not** touch the running
agent container. The agent still holds its cached JWT and agent-id on
disk under `/var/lib/spatium-dns-agent/` or
`/var/lib/spatium-dhcp-agent/` (cache layout is documented in
`CLAUDE.md` cross-cutting pattern #3).

**What the agent does automatically**

The agent will self-heal on its next poll. When the control plane
receives a request authenticated with a JWT that references a server row
that no longer exists it responds with **404**; the agent treats 401 *or*
404 as "bootstrap is invalid" and re-registers via its pre-shared key
(`DNS_AGENT_KEY` / `DHCP_AGENT_KEY`). A fresh server row appears in the
GUI within a heartbeat cycle (~30 s by default).

**When the auto-recovery is enough** — just wait. No manual steps.

**When you need to intervene**

- *The pre-shared key was rotated on the server.* The agent's bootstrap
  request will be rejected. Update the `DNS_AGENT_KEY` or
  `DHCP_AGENT_KEY` env var on the agent container (matching the value in
  SpatiumDDI settings) and restart:

  ```bash
  docker compose restart dns-bind9
  # or
  docker compose restart dhcp-kea
  ```

- *Auto-recovery appears stuck.* Force a clean re-bootstrap by wiping
  the cached credentials and restarting the container. The config cache
  is kept (so service keeps serving from last-known-good while the
  bootstrap runs) — only the identity files are removed:

  ```bash
  docker compose exec dns-bind9 sh -c \
      'rm -f /var/lib/spatium-dns-agent/agent_token.jwt \
             /var/lib/spatium-dns-agent/agent-id'
  docker compose restart dns-bind9
  ```

  Substitute `dhcp-kea` and `/var/lib/spatium-dhcp-agent/` for the DHCP
  side. You do **not** need to recreate the container — removing the two
  identity files is enough.

- *You want to start over from scratch.* Destroy the container and its
  volumes. This also clears the local config cache, so the agent will
  only come back online once the control plane is reachable.

  ```bash
  docker compose rm -sf dns-bind9
  docker volume rm spatiumddi_dns_bind9_state spatiumddi_dns_bind9_cache
  docker compose --profile dns up -d dns-bind9
  ```

**What doesn't come back automatically**

The re-bootstrapped server row inherits the agent's environment
variables (`SERVER_NAME`, `AGENT_GROUP`, `AGENT_ROLES`), **not** any
settings that had been edited in the GUI. Notes, credentials for Path B
drivers, per-server overrides, or TSIG keys that were rotated via the
UI all need to be re-applied on the new row.

Zones and records attached to a DNS server group, and DHCP scopes /
pools attached via `subnet_id`, are *not* lost — they live on the group
and subnet rows, not on the individual server. Deleting + recreating the
server just re-attaches the running agent to the same group config.

---

## Resetting the admin password

Documented inline in `CLAUDE.md` under *Development Commands → Reset
admin password*. Reproduced here so operators don't need to open
`CLAUDE.md`:

```bash
docker compose exec api python - <<'EOF'
import asyncio
from sqlalchemy import update
from app.core.security import hash_password
from app.db import AsyncSessionLocal
from app.models.auth import User
async def reset():
    async with AsyncSessionLocal() as db:
        await db.execute(update(User).where(User.username == "admin")
            .values(hashed_password=hash_password("NewPass!"), force_password_change=True))
        await db.commit()
asyncio.run(reset())
EOF
```

---

## Subnet delete is refused

**Symptom** — `DELETE /api/v1/ipam/subnets/{id}` returns `409 Conflict`
with a body like *"Subnet is not empty: N allocated IP addresses,
M DHCP scopes. Delete the contents first, or retry with force=true to
cascade."*

This is deliberate. A non-empty subnet delete used to cascade silently
and wipe IPAM rows + DHCP scopes out from under running services. The
server now refuses the delete unless you either:

- Remove the contents first (unassign every non-system IP, detach any
  DHCP scopes), then retry; or
- Opt into the cascade by appending `?force=true` to the request. The
  same pre-delete cleanup still runs — agentless Windows DHCP gets a
  WinRM remove-scope, agent-based Kea gets its bundle ETag bumped — so
  no lease is orphaned on a running server.

System placeholder rows (the `.0` network and `.255` broadcast) and
DHCP-lease mirrored rows (`auto_from_lease=True`) do **not** count as
blockers — they're cleaned up automatically on delete.
