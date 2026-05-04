# Branch progress: `feat/dns-multi-zone-publishing`

Single-issue branch for #25 — split-horizon DNS publishing at
the IPAM layer. One IPAM row publishes N records, one per
`(group, zone)` pin.

## Resume protocol

1. `git checkout feat/dns-multi-zone-publishing`
2. Read this file from top to bottom.
3. The first unchecked box under "Checkpoints" is the next thing
   to do.
4. This touches the IPAM → DNS sync pipeline — split into commits
   at the model+service / safety-gates / UI seams.
5. When all "Done when" criteria check, run `make ci`, commit
   chain, merge to `main`.

## Issue #25 — Multi-group DNS publishing (split-horizon at the IPAM layer)

### Spec (from issue, full body)

> Distinct from DNS Views above. Scenario: operator has one
> public-IP subnet hosting an internet-facing service + two DNS
> server groups (one internal, one external) and wants the same
> A record published into a zone in EACH group so internal
> resolvers and external resolvers both answer for that
> hostname.

> Today the IPAM → DNS pipeline is 1:1 — `IPAddress` carries a
> single `forward_zone_id`, `_resolve_effective_zone` walks
> subnet → block → space and returns one zone, and
> `_sync_dns_record` publishes one A/AAAA. Need 1:N: one IPAM
> row publishes N records, one per `(group, zone)` pin.

> **Smallest correct shape** (additive, two-day landing): keep
> `forward_zone_id` as the singular primary for backward compat,
> add `IPAddress.extra_zone_ids: list[uuid]` for the multi-zone
> case. Each zone naturally belongs to exactly one group, so
> multi-group fanout is implicit — no `dns_zone_ids_by_group`
> map needed.

> **Safety gates:**
> 1. Per-subnet opt-in `Subnet.dns_split_horizon: bool` (default
>    false), inheritable from Block. When off, picker stays
>    single-group like today. When on, picker becomes a
>    multi-select grouped by DNS group.
> 2. `DNSServerGroup.is_public_facing: bool` + a server-side
>    guard. When operator pins a private subnet's IP into a
>    public-facing group, return 422 with
>    `requires_confirmation` (mirrors the existing
>    `_check_ip_collisions` shape) so the modal can render
>    "This is RFC 1918 — publishing to `{group}` exposes
>    internal IPs to a publicly-facing resolver. Type the
>    CIDR to confirm."

### Implementation plan

#### Phase 1 — additive data model

- **`IPAddress.extra_zone_ids: list[uuid]`** — JSONB list,
  default `[]`. Each entry is a zone ID; the zone implicitly
  carries its group via the existing `DNSZone.group_id` FK.
- **`Subnet.dns_split_horizon: bool`** — default false; when
  off the picker stays single-group (current behaviour). Add
  to `Block` too with `inherit_from_block` semantics matching
  the existing DDNS inheritance pattern. **Not added to Space**
  — keep the inheritance to two levels matching the existing
  DDNS shape.
- **`DNSServerGroup.is_public_facing: bool`** — default false;
  flagged when the operator declares the group is internet-
  facing.
- **Subnet inheritance helpers:** new
  `resolve_effective_split_horizon(subnet) -> bool` walks
  Subnet → Block, returning the first non-null. Mirror
  `resolve_effective_ddns`.

#### Phase 2 — sync pipeline fanout

- **`_sync_dns_record`** in `app/services/dns/sync.py`:
  - Today: resolves a single `forward_zone_id` (with
    inheritance fallback) + emits one A/AAAA.
  - New: resolves `forward_zone_id` (primary, unchanged) +
    walks `extra_zone_ids` + dedupes; emits one A/AAAA per
    distinct zone ID.
  - Reverse (PTR) stays singular — PTR lives in the reverse
    zone, which is bound to the IP's CIDR, not the
    forward-publishing group set. One PTR per IP. (If the
    operator runs split-horizon reverse zones, that's a
    follow-up — out of scope for v1.)
  - Cleanup pass: when an IP is updated and its
    `extra_zone_ids` shrinks, the records under the
    no-longer-pinned zones must be DELETEd. Same code path as
    delete-on-IP-delete but scoped to specific zones.
- **`_resolve_effective_zone`** stays singular for backward
  compat (still needed for the primary). New
  `_resolve_effective_extra_zones(ipaddress) -> list[uuid]`
  walks the same chain.
- **Audit log.** `ipam.address.updated` payload extended to
  include the before/after `extra_zone_ids` diff so operators
  can see "added zone X, removed zone Y" at a glance.

#### Phase 3 — safety gates

- **Public-facing guard.**
  - In `_sync_dns_record` (and the validate path on
    `IPAddressCreate` / `Update`): for each pinned zone,
    check `zone.group.is_public_facing`. If true AND the IP's
    `address` is in an RFC 1918 / RFC 4193 / RFC 6598
    range, append a `CollisionWarning` shape. The existing
    `_check_ip_collisions` already returns warnings + a
    `requires_confirmation: True` flag — extend the same
    shape rather than inventing new error machinery.
  - `force` path on the create / update API already exists for
    other collision warnings — reuse it. Operator types the
    CIDR to confirm in the modal.
  - **Private-IP detection helper:** `app/services/ipam/
    classify.py::is_private_ip(ip_str) -> bool` — covers
    RFC 1918 + ULA (fc00::/7) + CGNAT (100.64.0.0/10) +
    link-local (169.254.0.0/16). Don't use Python's
    `ipaddress.is_private` — it doesn't include CGNAT and is
    too narrow for our intent.

#### Phase 4 — frontend

- **Subnet edit modal.** New "DNS split-horizon" toggle in the
  DNS section, with inheritance hint when block has it set.
- **DNS group editor.** New "This group is public-facing"
  toggle, with hint copy: "Used by the safety gate when
  publishing private-IP records — flips publish to require
  typed-CIDR confirmation."
- **IP address create/edit modal.** When subnet has
  `dns_split_horizon = true`, the zone picker becomes a
  multi-select grouped by DNS group:
  ```
  Group: Internal (private)
    [✓] internal.example.com
    [ ] dev.example.com
  Group: External (public-facing) ⚠
    [✓] example.com
  ```
  Public-facing groups carry the warning chip when the IP is
  RFC 1918 — modal shows the inline confirmation copy + asks
  for the CIDR before enabling Submit.
- **IP detail panel.** Show the published-to list as a chip
  list — "Published to: internal.example.com (Internal),
  example.com (External)" with the public-facing chip
  highlighted.

### Checkpoints

- [ ] Migration: add `IPAddress.extra_zone_ids`, `Subnet.dns_split_horizon`, `IPBlock.dns_split_horizon`, `DNSServerGroup.is_public_facing`
- [ ] Models: extend the four model classes
- [ ] Service: `resolve_effective_split_horizon` + `_resolve_effective_extra_zones`
- [ ] Service: extend `_sync_dns_record` to fan out + cleanup on shrink
- [ ] Service: `app/services/ipam/classify.py::is_private_ip`
- [ ] Service: extend `_check_ip_collisions` with public-facing guard
- [ ] Schemas: `extra_zone_ids` on IPAddress; `dns_split_horizon` on Subnet/Block; `is_public_facing` on DNSServerGroup
- [ ] Router: `/ipam/addresses` accepts `extra_zone_ids` + `force` flag for public-facing override
- [ ] Tests: fanout publishes N records; shrink cleans up; public-facing guard triggers; override with `force=true` succeeds
- [ ] Frontend: split-horizon toggle on Subnet + Block edit modals
- [ ] Frontend: public-facing toggle on DNS group editor
- [ ] Frontend: multi-select grouped picker on IP create/edit when split-horizon on
- [ ] Frontend: typed-CIDR confirm flow on public-facing-private-IP overlap
- [ ] Frontend: "Published to" chip list on IP detail
- [ ] `make ci` clean
- [ ] Commit chain split at model+service / safety-gates / frontend

### Done when

All checkpoints checked + commit chain landed + merged to main.

## Risks / unknowns

- **Cleanup-on-shrink edge case.** If `_sync_dns_record` runs
  on every save, and the previous save had `extra_zone_ids =
  [A, B]` and the new save has `[A]`, the helper must DELETE
  the record in zone B. Without an explicit before/after
  comparison the sync helper would just CREATE/UPDATE for [A]
  and leave the orphan in B. Solution: pass the previous
  `extra_zone_ids` into the sync helper or have the helper
  read it back before mutating.
- **Reverse zone PTR semantics.** PTR isn't fanned out —
  documented above. If an operator asks for split-horizon PTR
  in v1, it's a follow-up.
- **DNS Views overlap.** This issue is about *publishing the
  same name into two zones in two groups*. DNS Views (issue
  #24) is about *one zone with different match-clients
  filtering*. Both need to work. They don't conflict — one
  IP can publish into a Views-using zone in two groups too.
- **Inheritance at apex.** For records that aren't IPAM-
  driven (e.g. CNAMEs, MX, manual A records), this issue is
  silent — those are operator-authored at the zone level
  directly. Multi-group publishing for them is a different
  shape (probably a "publish in group X too" toggle on the
  record). Out of scope for v1.
