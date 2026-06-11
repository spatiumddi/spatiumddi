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

## DHCP server (Kea) doesn't respond to DISCOVER

**Symptom** — A client on the same L2 segment as the Kea server gets no
lease. Wireshark on the client shows the `DHCPDISCOVER` going out as a
broadcast, but no `DHCPOFFER` comes back. Static-IP connectivity between
the client and the server works fine, and the Kea service shows a green
**`healthy`** chip in the Fleet → Service health panel.

**Key point first** — the `healthy` chip only means the Kea *process is
up*. It does **not** mean Kea will answer for your client's subnet. There
are exactly two ways a DISCOVER ends in silence on an otherwise-healthy
server:

- **(A) The DISCOVER never reaches the Kea socket** — the broadcast is
  dropped before Kea sees it (host firewall, or the container isn't on
  the host NIC).
- **(B) Kea hears it but drops it** — no configured `subnet4` matches the
  interface the packet arrived on, or the matched subnet has no usable
  pool. Kea logs this and moves on; nothing goes on the wire.

For a first-time setup, **(B) is by far the most common.**

### Split (A) from (B) in two minutes

Run these on the appliance host (SSH as `admin`, or F1-login at the
console):

```bash
# 1. Does the broadcast actually arrive on the host NIC?
sudo tcpdump -ni any -v 'port 67 or port 68'
#    …then release/renew DHCP on the client.

# 2. Watch Kea react in real time (k3s appliance):
sudo k3s kubectl get pods -A | grep kea          # find pod + namespace
sudo k3s kubectl logs -n <ns> <kea-pod> -f
#    …on docker-compose installs: docker compose logs -f dhcp-kea
```

Read the result:

| tcpdump shows DISCOVER | Kea log | Conclusion |
|---|---|---|
| yes | logs DISCOVER, no OFFER (often `DHCP4_SUBNET_SELECTION_FAILED` / "no subnet selected") | **(B)** — subnet/scope mismatch. Most common. |
| yes | logs nothing | **(A)** — the packet reaches the host but not the Kea socket: the group is in **Relay-only (udp)** socket mode, or the firewall is dropping it. |
| no  | — | The broadcast isn't reaching the appliance VM at all (vSwitch port-group / VLAN). |

### Fixing (B) — scope/subnet mismatch (most common)

Kea selects a subnet for a broadcast (non-relayed) client by matching the
**receiving interface's own IP** against the configured `subnet4` ranges.
So if the appliance's NIC on that segment is `192.168.0.x/24` but your
DHCP scope was created for a different network, Kea silently ignores the
DISCOVER. Verify, in order:

- Run `ip -4 addr` on the appliance and note its IP on the client-facing
  interface.
- In the UI, confirm a **DHCP scope exists whose subnet CIDR contains
  that address**.
- The scope is **active** — inactive scopes are dropped from the agent's
  config bundle entirely.
- The scope is **IPv4** and has at least one **dynamic pool range** — a
  scope with no pool has nothing to offer.
- The scope is attached to the **same DHCP server group** as this Kea
  server. SpatiumDDI's DHCP model is group-centric (scopes / pools /
  statics live on the server group); only **active IPv4 scopes attached
  to that group** render into Kea's `subnet4`. If none qualify, Kea
  renders `subnet4: []` and answers nobody.

Confirm what Kea actually received by reading the agent's last-rendered
config (this is the source of truth for what the running Kea is using):

```bash
# k3s appliance:
sudo k3s kubectl exec -n <ns> <kea-pod> -- \
    cat /var/lib/spatium-dhcp-agent/rendered/kea-dhcp4.json | grep -A4 subnet4
# docker-compose:
docker compose exec dhcp-kea \
    cat /var/lib/spatium-dhcp-agent/rendered/kea-dhcp4.json | grep -A4 subnet4
```

An empty `"subnet4": []` confirms (B): no active IPv4 scope is reaching
the agent. Activate the scope / attach it to the right group / add a
pool, then the bundle ETag shifts and the agent re-renders within a
heartbeat. (The DHCP Activity tab on the **Logs** page surfaces the same
Kea log lines if you'd rather stay in the UI.)

### Fixing (A) — packet reaches the host but not Kea

Two causes; check the socket mode first.

**Socket mode (#365).** A directly-attached client can only be heard when
Kea's Dhcp4 daemon uses **raw** (AF_PACKET) sockets — UDP sockets are
relay-only and silently miss the broadcast. The DHCP **server group**
carries a *Client reachability* setting that controls this:

- **Directly attached / mixed** → `dhcp-socket-type: raw` (the default
  since #365). Hears broadcast DISCOVERs *and* relayed traffic.
- **Relay-only** → `dhcp-socket-type: udp`. Cannot receive direct L2
  broadcasts.

If the server is on the same LAN as its clients, the group must be
**Directly attached** (DHCP → the server group → Edit → *Client
reachability*). Confirm what's actually rendered:

```bash
# k3s appliance:
sudo k3s kubectl exec -n <ns> <kea-pod> -- \
    cat /var/lib/spatium-dhcp-agent/rendered/kea-dhcp4.json | grep socket-type
# docker-compose:
docker compose exec dhcp-kea \
    cat /var/lib/spatium-dhcp-agent/rendered/kea-dhcp4.json | grep socket-type
```

`"dhcp-socket-type": "udp"` on a direct-attached LAN is the problem —
switch the group to *Directly attached*; the agent re-renders within a
heartbeat. (Raw sockets need the `NET_RAW` capability, which the
appliance DaemonSet and the shipped compose files grant.)

> Installs predating #365 hardcoded `udp` and had no knob — that was the
> original bug. Upgraded installs default to `direct` (raw), so this is
> only a live cause if the group was deliberately set to Relay-only.

**Firewall.** UDP sockets are also subject to the host's nftables INPUT
chain (raw sockets bypass it). The DHCP role opens UDP **67 + 68**;
confirm the rules are present (and haven't drifted):

```bash
sudo nft list chain inet filter input | grep -E 'dport (67|68)'
```

You should see `udp dport 67 accept` / `udp dport 68 accept`. If they're
missing, the per-role firewall didn't apply — re-saving the DHCP role
assignment in **Fleet** re-renders the drop-in.

### Networking sanity check

On the k3s appliance the Kea DaemonSet runs with `hostNetwork: true` so
it sees broadcasts on the host NIC directly. On docker-compose the DHCP
container must use **host networking** (`DHCP_NETWORK_MODE=host`, the
default for supervisor-managed appliances) — a bridged/NAT network will
not receive the L2 broadcast. If tcpdump on the host shows the DISCOVER
but `kubectl exec … -- ip addr` / `docker compose exec dhcp-kea ip addr`
shows the container is *not* on the host's interfaces, the container is
on the wrong network.

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
