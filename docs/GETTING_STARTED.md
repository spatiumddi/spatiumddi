---
layout: default
title: Getting Started
---

# Getting Started — Setup Order

SpatiumDDI has a few internal dependencies between modules (records need zones, scopes need subnets, etc.). This guide walks you through the **recommended order** to get from a fresh install to a useful working system — whether your DNS/DHCP servers are the built-in Kea + BIND9 containers, a Windows Server DC, or a mix.

> If you haven't installed SpatiumDDI yet, start with the [Docker Compose quick start](deployment/DOCKER.md) or [README Quick start](../README.md#quick-start-with-docker-compose), then come back here.

---

## TL;DR — the order

```
1. Platform settings        (app title, defaults, sync cadences)
2. Auth providers           (LDAP / OIDC / SAML / RADIUS / TACACS+)   ← optional, do later if you want
3. DNS server groups + servers
4. DNS zones                (forward first, reverse second)
5. DHCP server groups + servers
6. IPAM — IP Space
7. IPAM — IP Block(s)       (optional — aggregates that own inherited settings)
8. IPAM — Subnets           (pin the DNS + DHCP group here, OR let them inherit)
9. DHCP scopes              (per subnet, per DHCP server)
10. Addresses               (start allocating; A/AAAA/PTR follow automatically)
```

The cleanest mental model is: **servers → zones/scopes → subnets → addresses**. Addresses are the leaf; everything above them has to exist before SpatiumDDI can push a record or a reservation anywhere useful.

---

## 1. Platform settings (first login)

After logging in as `admin` / `admin` and changing your password:

1. Go to **Settings**.
2. Set **Branding & URL** — especially **External URL** if you're going to use OIDC or SAML (those redirect flows need it).
3. Tune **DNS Defaults** (default zone TTL, DNSSEC mode) and **DHCP Defaults** (default DNS servers, search domain, lease time) — these are the pre-filled values when you later create zones and scopes, so setting them up front saves repetition.
4. Leave the two sync jobs **off** for now:
   - *IPAM → DNS Reconciliation* — turn this on once you have zones + subnets.
   - *Zone ↔ Server Reconciliation* — turn this on once at least one Windows DNS server with credentials is registered.
5. **Utilization thresholds** are cosmetic — set them if you care about the colour of the bars.

Each section has a **Reset to defaults** button at the top. It populates the section with the built-in defaults but still requires **Save** — you can back out by navigating away.

---

## 2. (Optional) Auth providers

If you want SSO before anyone logs in, do it now. Otherwise, skip and come back later.

- **LDAP** — fastest to set up. Add a service account, point at your DC, test the connection, map groups.
- **OIDC** — needs your External URL from step 1. The redirect URL is `https://<External URL>/api/v1/auth/{provider_id}/callback`.
- **SAML** — needs External URL and an IdP that can consume the SP metadata at `/api/v1/auth/{provider_id}/metadata`.
- **RADIUS / TACACS+** — point at your network-device auth infra. Primary + optional backup hosts share the same shared secret.

See [AUTH.md](features/AUTH.md) for the full provider matrix.

---

## 3. DNS — server groups + servers

Zones live under server groups, so this has to come before zones.

1. **DNS → Server Groups → New Group**. Give it a name (`default`, `internal`, `corp`, whatever) and pick the recursion / DNSSEC defaults.
2. **Add a server** to the group. You have three options:

   | Backend | Setup | When to choose |
   |---|---|---|
   | **Built-in BIND9** (`bind9`) | Run `docker compose --profile dns-bind9 up -d` (legacy `--profile dns` also works). The container auto-registers using `DNS_AGENT_KEY` and shows up in the group automatically. | New deployments; you want SpatiumDDI to own the whole DNS plane and the BIND ecosystem (RPZ, full views support). |
   | **Built-in PowerDNS** (`powerdns`, issue #127) | Run `docker compose --profile dns-powerdns up -d`. Same `DNS_AGENT_KEY` bootstrap; auto-registers under group `default-powerdns`. | You want online DNSSEC with one-button sign-zone, ALIAS records (CNAME-at-apex), LUA computed records, or PowerDNS's REST-native operational model. |
   | **Windows DNS — Path A** (`windows_dns`, no credentials) | Point at an existing Windows DC. Enable "Secure and Nonsecure" dynamic updates on each zone in Windows DNS Manager and allow AXFR to SpatiumDDI's host. | You have an AD-integrated DNS already and just want record-level writes from SpatiumDDI. |
   | **Windows DNS — Path B** (`windows_dns` + credentials) | Same as above, but also provide WinRM credentials. Unlocks zone create/delete and lets SpatiumDDI list + pull zones without relying on AXFR. | You want the full experience without giving up Windows DNS Manager. Best for AD environments. |

   See [WINDOWS.md](deployment/WINDOWS.md) for the Windows-side prerequisites (WinRM, service accounts, firewall).

3. Click **Sync with Servers** on the group header. For Windows servers this AXFRs/WinRM-pulls every zone on the wire, auto-imports zones that aren't in SpatiumDDI yet, and pushes any DB-only zones back to the server.

---

## 4. DNS — zones

Zones come in two flavours, and the order matters a little:

1. **Forward zones first.** Create `corp.example.com`, `lab.example.com`, etc. These are what your A/AAAA records live in.
2. **Reverse zones second.** These back PTR records. You can either:
   - Create them manually now (`0.20.10.in-addr.arpa` for `10.20.0.0/16` — standard RFC 2317 layout), or
   - Let SpatiumDDI auto-create them when you create the subnet in step 8 (tick the "auto-create reverse zone" box on the subnet form).

Zones that SpatiumDDI didn't create itself can be imported by clicking **Sync with Servers** on the group — anything present on the wire but not in the DB is auto-imported as `is_auto_generated=False` (so it won't be touched by stale-record cleanup).

---

## 5. DHCP — server groups + servers

Same pattern as DNS. Do this before you start pinning subnets to DHCP servers.

1. **DHCP → Server Groups → New Group**.
2. **Add a server**:

   | Backend | Setup | When to choose |
   |---|---|---|
   | **Built-in Kea** (`kea`) | `docker compose --profile dhcp up -d`. Auto-registers via `DHCP_AGENT_KEY`. | New deployments; you want SpatiumDDI to own DHCP. |
   | **Windows DHCP — Path A** (`windows_dhcp`, read-only) | Point at an existing Windows DHCP server with WinRM credentials. SpatiumDDI polls leases and mirrors them into IPAM as `dhcp` rows. All writes (`/sync`, scope push) are rejected. | You want lease visibility in IPAM without giving SpatiumDDI write control over the Windows DHCP server. |

   See [WINDOWS.md](deployment/WINDOWS.md) for Windows DHCP Server prerequisites.

3. Toggle **DHCP Lease Sync** on in Settings once you have at least one agentless server — the Celery beat task (default: every 5 min) pulls leases.

---

## 6–8. IPAM — space → block → subnet

IPAM is the root of the hierarchy everything else hangs off. The top-down order is:

### 6. IP Space

An **IP Space** is the top-level container — think "address universe". Most orgs have one or two:
- `Corporate` — all IPs your org announces.
- `Lab` — disposable / isolated ranges.
- `Cloud-AWS` / `Cloud-Azure` — per-cloud RFC1918 ranges.

Create a space with **IPAM → New Space**. You can pin a default DNS group + DHCP group here; anything below will inherit unless overridden.

### 7. IP Blocks (optional)

A **Block** is an aggregate — a `10.0.0.0/8` under Corporate, broken into `10.1.0.0/16` for HQ and `10.2.0.0/16` for datacentre, etc. Blocks exist to:
- Own inherited settings (DNS group, DHCP group, custom fields, tags).
- Give the tree shape when you have dozens of subnets.

You can skip blocks entirely if your network is small — subnets can live directly under a space.

### 8. Subnets

A **Subnet** is where DHCP scopes attach and where individual addresses are allocated.

On the subnet create form:

- **CIDR** (e.g. `10.20.0.0/24`) — required.
- **VLAN ID** (optional) — if you manage VLANs in SpatiumDDI, link it here.
- **Primary DNS zone** — forward zone for A/AAAA records auto-created from IP allocations.
- **Additional DNS zones** — other zones you want the IP allocation form to offer in its dropdown.
- **DNS server group** — leave blank to inherit from block/space. Pin it here to override.
- **DHCP server group** — same. Pin it here if this subnet needs a different DHCP server than its parents.
- **DNS inherit settings** / **DHCP inherit settings** — toggle these back on if you pinned something and want to return to inheritance.
- **Auto-create reverse zone?** — if checked, SpatiumDDI creates the right `in-addr.arpa` (or `ip6.arpa`) zone on the effective DNS group.

**Important:** subnets and blocks respect inheritance independently for DNS and DHCP. If you want a subnet to inherit DNS from its parent but use a different DHCP group, that works — toggle `dns_inherit_settings` on, `dhcp_inherit_settings` off, and pin the DHCP group.

---

## 9. DHCP scopes

With a subnet and a DHCP server group in place, open the subnet → **DHCP** tab → **New Scope**. The form pre-fills defaults from Settings (DNS servers, domain, search list, NTP, lease time), so most scopes are a one-click save.

Pools go under scopes:
- **Dynamic** — handed out to clients.
- **Reserved** — held for static assignments only.
- **Excluded** — this range exists in the subnet but DHCP will never offer it (e.g. infrastructure).

For Windows DHCP servers in Path A (read-only) you can't create scopes from SpatiumDDI — you create them in the Windows DHCP MMC, and SpatiumDDI auto-imports them on the next lease sync.

---

## 10. Addresses

Now the fun part. In the subnet view:

- Click **Allocate** (or **Next free IP**) to auto-pick the next available address.
- Or click any row in the IP grid and fill in hostname, status, tags.

When you save an IP with a hostname + DNS zone:

1. SpatiumDDI creates an A/AAAA record in the forward zone.
2. SpatiumDDI creates a PTR record in the reverse zone (if one is linked).
3. For BIND9, the update goes over RFC 2136; for Windows, the update goes over RFC 2136 (Path A) or is idempotent-pushed via WinRM (Path B).

If anything ever drifts between IPAM and your DNS servers, there's a manual **Check DNS Sync** button on the subnet/block/space header, and two scheduled reconciliation jobs you can enable in Settings:

| Job | What it reconciles |
|---|---|
| **IPAM → DNS Reconciliation** | IPAM's expected records vs SpatiumDDI's DNS DB — fills in records the live sync missed. |
| **Zone ↔ Server Reconciliation** | SpatiumDDI's DNS DB vs the authoritative server's wire — imports out-of-band edits, pushes DB-only records back. |

Both are off by default and additive-only.

---

## Common setup shapes

### All-SpatiumDDI (fresh greenfield)

1. Compose profiles on: `COMPOSE_PROFILES=dns,dhcp`
2. Add the auto-registered `dns-bind9` and `dhcp-kea` containers to groups.
3. Create zones, then spaces/blocks/subnets, then scopes, then allocate.

### Hybrid — Windows DNS + SpatiumDDI DHCP

1. Run `dhcp-kea` (compose profile `dhcp`).
2. Register the Windows DC as a `windows_dns` server **with** WinRM credentials (Path B).
3. Click **Sync with Servers** — zones auto-import.
4. Build your subnets pinning the Windows DNS group + the Kea DHCP group.

### Hybrid — Windows DNS + Windows DHCP (read-only mirroring)

1. Don't enable the built-in compose profiles.
2. Register Windows DC(s) as `windows_dns` (Path A or B) + `windows_dhcp` (Path A, read-only).
3. Enable **DHCP Lease Sync** in Settings so leases mirror into IPAM as `status=dhcp`.
4. Manage scopes in Windows DHCP MMC; SpatiumDDI auto-imports them on each lease poll.

---

## Troubleshooting the first IP

If allocating your first IP doesn't produce a DNS record, check in this order:

1. **Subnet has a primary DNS zone?** — open the subnet, check the DNS zone field is set.
2. **Zone is on a server group?** — every zone has to belong to a group.
3. **Group has at least one enabled server?** — a group with zero `is_enabled=True` servers won't push anything.
4. **Server is healthy?** — hit **Sync with Servers** and watch the per-server status column.
5. **For Windows Path A**, is the zone set to "Nonsecure and secure" updates? — secure-only rejects our unsigned RFC 2136 updates.
6. **For Windows Path B**, are credentials stored?** — the server detail page shows "WinRM credentials: configured" or "not configured". If not configured, record writes fall back to RFC 2136.

If a record is expected but missing, the subnet's **Check DNS Sync** drift report will tell you exactly what's missing and let you apply it with one click.

---

## Next steps

- **Tag your subnets** — custom fields and tags propagate to IPs, and bulk-edit respects them.
- **Set up audit log filtering** — every mutation is already logged; the admin **Audit** page gives per-column filters.
- **Enable the health dashboard** — the **System** section surfaces server/agent status and recent errors.
- **Turn on the reconciliation jobs** once the system is stable.

For deeper dives:

- [IPAM features](features/IPAM.md) — custom fields, tags, bulk operations, import/export.
- [DNS features](features/DNS.md) — views, ACLs, blocklists, DDNS, zone import.
- [DHCP features](features/DHCP.md) — pools, client classes, options, static assignments.
- [Permissions](PERMISSIONS.md) — how to delegate subnets, zones, and scopes to different groups.
- [Windows setup](deployment/WINDOWS.md) — WinRM, service accounts, firewall rules.
