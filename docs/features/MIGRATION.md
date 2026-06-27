# Migration — importing existing DNS + DHCP + IPAM estates

> **One-shot import, not ongoing sync.** These importers exist so an
> operator can load a real DNS / DHCP / IPAM estate into a sandbox
> SpatiumDDI without retyping every zone, record, scope, pool,
> reservation, prefix, and address. Once imported, **SpatiumDDI is the
> source of truth** for the imported objects. There is no
> conflict-resolution loop and no scheduled re-pull. Operators who want
> a running read-only mirror are served by the Windows DNS / Windows
> DHCP Path A drivers and the integration shelf instead.

Three sibling importers share one design — a per-source parser feeding
a shared canonical intermediate representation (IR), then a
source-agnostic two-phase **preview → commit** pipeline. Each lives
behind a togglable feature module so operators who don't need the
surface can hide it (Settings → Features).

| | DNS importer (#128) | DHCP importer (#129) | NetBox / IPAM importer (#36) |
|---|---|---|---|
| Feature module | `dns.import` | `dhcp.import` | `ipam.import.netbox` |
| Admin surface | DNS Import (sidebar) | DHCP Import (sidebar) | Import → NetBox |
| Sources | BIND9 archive · Windows DNS live-pull · PowerDNS REST · Cloud DNS live-pull (Cloudflare / Route 53 / Azure DNS / Google Cloud DNS) | Kea JSON file · Windows DHCP live-pull · ISC `dhcpd.conf` | NetBox REST live-pull (v3.x–4.6+) |
| Target | DNS server group (+ optional view) | DHCP server group (+ IPAM linkage) | native IPAM rows (space / block / subnet / address + VRF / VLAN / Customer / Site) |
| Provenance columns | `dns_zone` / `dns_record` `import_source` + `imported_at` | `dhcp_scope` / `dhcp_pool` / `dhcp_static_assignment` / `dhcp_client_class` `import_source` + `imported_at` | IPAM / network rows `import_source` + `imported_at` + `netbox_id` (in `custom_fields` / `tags`) |
| Conflict actions | skip / overwrite / rename (per zone) | skip / overwrite (per scope) | skip / overwrite (per entity) |
| API prefix | `/api/v1/dns/import/{source}/…` | `/api/v1/dhcp/import/{source}/…` | `/api/v1/ipam/import/netbox/…` |
| RBAC | superadmin | superadmin | superadmin |

The provenance columns are nullable + non-default: pre-existing,
hand-created rows look "not imported" to the matcher, so a re-import
never claims ownership of a row it didn't create.

---

## Shared shape

1. **Configure source.** File upload (BIND9 archive, Kea JSON, ISC
   `dhcpd.conf`) or a connection (a pre-registered Windows server, a
   PowerDNS REST endpoint). Plus the **target** — which server group
   the import lands in.
2. **Preview.** `POST …/{source}/preview` parses the source into the
   canonical IR and returns the would-create plan plus per-object
   conflict status. Side-effect-free — no DB writes, no audit rows.
   The operator can re-upload / re-pull while iterating.
3. **Commit.** `POST …/{source}/commit` replays the previewed plan
   (the UI hands the plan back in the request body, so the server stays
   stateless between the two calls) and writes the IR. Each object
   commits in **its own savepoint** — a failure on object N never rolls
   back objects 1..N-1, and the result ledger carries one row per
   attempted object so partial success is visible.

Anything the source carries that SpatiumDDI can't model surfaces in the
preview: per-object `parse_warnings`, a top-level `warnings` list, and
a **"didn't import"** panel (`unsupported`) for whole subsystems we
deliberately don't translate (DNSSEC keys, Kea hook libraries, ISC
failover / TSIG keys, classifier DSL).

---

## DNS importer (#128)

Three sources, all reducing to canonical `ImportedZone` + `ImportedRecord`:

- **BIND9 archive** — upload a `.zip` / `.tar(.gz|.bz2|.xz)` containing
  `named.conf` + the referenced master files. The parser walks every
  `zone "…" { … }` declaration (including those nested in `view {}`
  blocks), resolves the `file "…"` directive inside the archive, and
  parses each master file. ACL / controls / logging / key declarations
  stay out of scope; DNSSEC records (DNSKEY / RRSIG / NSEC* / DS) are
  stripped with a warning — re-sign post-import via the zone DNSSEC tab.
- **Windows DNS live-pull** — pick a registered `windows_dns` server
  with WinRM credentials; the importer walks `Get-DnsServerZone` +
  `Get-DnsServerResourceRecord`.
- **PowerDNS REST** — paste an API URL + key (read once, never
  persisted); the importer walks `/api/v1/servers/{server}/zones`.
- **Cloud DNS live-pull** *(issue #37)* — pick a registered cloud DNS
  server (driver in `{cloudflare, route53, azure_dns, google_dns}`)
  that has its provider credentials configured. The control plane
  pulls every hosted zone + its records through the agentless driver's
  `pull_zones_from_server` / `pull_zone_records` reads — the same
  method names Windows DNS uses, so this source is a near-clone of the
  Windows live-pull. Cloud providers own the SOA + apex NS on their
  side, so the importer applies standards-compliant SOA defaults
  (rewritten from the zone's own `primary_ns` / `admin_email` at push
  time) and surfaces a per-zone warning; a DNSSEC-signed source zone
  warns that signing state isn't imported and must be re-established on
  the destination driver after commit.

Endpoints: `GET /dns/import/cloud/servers` (the credentialled
server picker), `POST /dns/import/cloud/preview`, and
`POST /dns/import/cloud/commit` — same preview → commit shape, same
per-zone conflict actions (below). This is also the engine behind a
cloud DNS server's **Sync from provider** button.

Per-zone conflict actions: **skip** (default — never trample an
existing zone), **overwrite** (delete + recreate), **rename** (create
under an operator-typed FQDN).

**Per-provider provenance.** Unlike the other DNS sources (which stamp
a single `bind9` / `windows_dns` / `powerdns` label), the cloud source
stamps the **provider name** — `cloudflare`, `route53`, `azure_dns`,
or `google_dns` — into every created row's `import_source` column, so
provenance stays queryable per provider. The plan's `source` field
must match the endpoint or the commit is rejected.

See `docs/drivers/DNS_DRIVERS.md` for parser + agentless-driver
internals.

---

## DHCP importer (#129)

Three sources, all reducing to canonical `ImportedScope` +
`ImportedPool` + `ImportedReservation` + `ImportedClientClass`:

- **Kea JSON** *(Phase 1)* — upload a `kea-dhcp4.conf` /
  `kea-dhcp6.conf` from a non-managed daemon. Cleanest source: it's
  exactly the shape SpatiumDDI's own Kea driver renders. The parser
  strips Kea's JSON-with-comments extensions (string-aware), accepts
  either a wrapped (`{"Dhcp4": {…}}`) or bare (`{"subnet4": […]}`)
  body, and maps `subnet4` / `subnet6` → scopes, `pools` → pools,
  `reservations` → reservations, `option-data` → canonical options,
  and top-level `client-classes` → client classes (Kea `test`
  expressions are SpatiumDDI's native class shape, so they import
  verbatim).
- **Windows DHCP live-pull** *(Phase 2)* — pick a registered
  `windows_dhcp` server with WinRM credentials; the importer reuses the
  Path A read driver (`Get-DhcpServerv4Scope` + option values +
  exclusion ranges + reservations). IPv4 only.
- **ISC `dhcpd.conf`** *(Phase 3)* — upload a `dhcpd.conf`. A hand-rolled
  tokeniser + recursive-descent walker maps `subnet` / `subnet6` →
  scopes, `range` / `range6` / `pool {}` → pools, `host` → reservations
  (a global `host` attaches to the subnet whose CIDR contains its
  `fixed-address`), and scope `option` statements → canonical options.
  ISC classifier expressions don't translate to SpatiumDDI's
  constrained class model, so `class` declarations are surfaced for
  **manual review** (`supported=false`) and never auto-created;
  `failover` / `key` / `zone` / `include` are listed in the "didn't
  import" panel.

### IPAM linkage (the DHCP-specific wrinkle)

A `DHCPScope` must bind to an IPAM `Subnet`. For each imported scope the
commit either:

- **links** to an existing IPAM subnet whose CIDR matches the scope, or
- **auto-creates** a subnet under the operator-chosen **IP space +
  block** (containment + non-overlap validated; the network /
  broadcast / gateway placeholder rows the manual create path adds are
  skipped — the imported pools + reservations are the real occupancy).

Leave the IP space + block blank for **link-only** mode: scopes with no
matching subnet report an actionable per-scope error rather than
silently creating something. The auto-created (or linked) subnet gets
its `dhcp_server_group_id` set so IPAM reflects DHCP ownership.

Per-scope conflict actions when the target group **already serves a
scope on the matched subnet**: **skip** (default) or **overwrite**
(delete the existing scope — cascading its pools + statics — and
recreate). Client classes are group-scoped and created once, skipping
any that already exist by name.

### Not imported

- **Live leases** — transient, unrelated to config, and would race with
  the running daemon. They repopulate from the running daemon once a
  real Kea server is attached to the target group.
- **Kea hook libraries** (HA / host-cache / lease-cmds), **ISC
  failover** — SpatiumDDI's HA is implicit at the server-group level;
  configure it server-side post-import.
- **ISC / Kea classifier DSL** beyond the directly-modellable subset,
  **TSIG / failover keys**, **`include` files** (inline them before
  upload).

See `docs/drivers/DHCP_DRIVERS.md` § "Importing existing daemon
configs" for parser internals.

---

## NetBox → IPAM importer (#36)

A one-shot **migration** importer — *not* a continuous reconciler.
There is no `netbox_target` row, no beat sweep, and no absence-delete:
the operator live-pulls a NetBox install once, reviews the plan, and
commits it into native IPAM. (Operators who want an ongoing read-only
mirror belong on the integration shelf, not here.) The service package
is `backend/app/services/netbox_import/`; the router lives at
`/api/v1/ipam/import/netbox/`.

### What it imports

| NetBox source | → SpatiumDDI |
|---|---|
| `/api/ipam/vrfs/` | `VRF` (name / rd / import + export targets / tenant→customer) |
| `/api/tenancy/tenants/` | `Customer` (ownership FK, **not** the space boundary) |
| `/api/dcim/sites/` (+ regions) | `Site` (region is the single parent axis; site-groups fold into `tags`) |
| `/api/ipam/aggregates/` | top-level `IPBlock` |
| `/api/ipam/prefixes/` | `IPBlock` (container) **or** `Subnet` (leaf), by NetBox `status` |
| `/api/ipam/ip-addresses/` | `IPAddress` (mask stripped, `dns_name` → fqdn) |
| `/api/ipam/vlans/` | `VLAN` under a synthesized router |

DCIM devices / racks / cables / interfaces, circuits, and write-back to
NetBox are **out of scope** — the `assigned_object` on an IP is read
for hostname enrichment only, never imported as its own row.

### Flow — test → preview → commit

1. **Test connection.** `POST …/test-connection` probes
   `GET /api/status/`, validates the token (NetBox 4.5+ exposes a cheap
   `authentication-check`), and returns the daemon version + object
   counts so the operator confirms scale before a large pull.
2. **Preview.** `POST …/preview` live-pulls every in-scope endpoint,
   maps onto the canonical IR, and flags every entity whose key already
   exists in the target. Side-effect-free — no DB writes, no audit
   rows. Optional `filters` (vrf / tenant / status / family /
   within-include) slice a large NetBox; re-run freely while iterating.
3. **Commit.** `POST …/commit` replays the **unmodified** previewed
   plan (the UI hands the same `PreviewOut` shape straight back as
   `CommitIn.plan`, so the server is stateless between the two calls).
   Conflicts are **re-detected fresh** against current state; each
   entity writes in its **own savepoint**, so a FK / overlap error on
   entity N never rolls back 1..N-1, and the result ledger carries one
   `created` / `overwrote` / `skipped` / `failed` row per attempt. Each
   committed entity gets one `audit_log` row tagged
   `import_source=netbox`. There is **no agent wake** — NetBox seeds
   IPAM rows only and touches no DNS / DHCP config bundle.

### Space strategy — `per_vrf` vs `single`

The crux decision is which IP space each prefix / address lands in:

- **`per_vrf`** (default) — synthesise one `IPSpace` per NetBox VRF
  (named after the VRF), plus a **Global** space for everything with no
  VRF. Each block / subnet / address resolves its space from its VRF.
- **`single`** — collapse every imported row into one operator-chosen
  `target_space_id`; no `ImportedSpace` rows are synthesised. The
  preview **warns** and the commit **422s** if `target_space_id` is
  missing under this strategy.

### Provenance + idempotent re-run

Every created (or overwritten) row is stamped `import_source="netbox"`
+ `imported_at` + the NetBox primary key (`netbox_id`, carried in
`custom_fields` for rows that have a custom-fields column, or `tags`
for those that don't — `Site` / `VLAN`). On a re-run the matcher keys
off `(import_source="netbox", netbox_id)`, so a second commit
re-detects conflicts against the live DB and defaults every conflicting
entity to **skip** — a double-"Commit" never duplicates or tramples.
To intentionally refresh, set the entity's per-conflict action to
**overwrite**. Pre-existing, hand-created rows look "not imported" to
the matcher, so the importer never claims ownership of a row it didn't
create.

### Connection + token

The NetBox `base_url` + API `token` (v1 `Token` or v2 `Bearer nbt_…`,
auto-detected) are supplied **in the request body** on every call and
**read once — never persisted**. The token should be read-only on the
NetBox side; an advisory SSRF guard logs the resolved target IP (a
co-located / LAN NetBox is a legitimate source, so it is not
hard-blocked). The surface is superadmin-only; the feature module
`ipam.import.netbox` is **default-on** (importers are default-on for
discovery per non-negotiable #13/#14 — only continuous integration
*mirrors* default off).

---

## Re-running an import

All three importers are safe to re-run. A second commit of the same
source re-detects conflicts against the live DB and defaults every
conflicting object to **skip**, so a fat-fingered "Commit" twice
doesn't duplicate or trample. To intentionally refresh an imported
object, set its conflict action to **overwrite** (DNS also offers
**rename**; NetBox keys re-runs off the stored `netbox_id`).
