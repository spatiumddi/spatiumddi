# Branch progress: `feat/dhcp-pxe`

Single-issue branch for #51 — first-class PXE / iPXE
provisioning fields with per-arch matching (BIOS / UEFI / ARM).

## Resume protocol

1. `git checkout feat/dhcp-pxe`
2. Read this file from top to bottom.
3. The first unchecked box under "Checkpoints" is the next thing
   to do.
4. Self-contained DHCP work — split into commits at the
   model+driver / API / UI seams.
5. When all "Done when" criteria check, run `make ci`, commit
   chain, merge to `main`.

## Issue #51 — PXE / iPXE provisioning

### Spec (from issue)

> First-class fields for `next-server`, `boot-filename`, with
> per-arch matching (BIOS vs UEFI vs ARM). Today it's manual
> option-stuffing. Renders to Kea client classes + scope-level
> overrides; surfaces in the scope edit modal as a "PXE / iPXE"
> tab.

### Background — how PXE/iPXE provisioning works

A PXE-booting client sets DHCP option 93 (Client Architecture
Type) so the DHCP server can match by arch and return the right
boot file:

| Option 93 | Architecture |
|---|---|
| `0` | BIOS / Legacy x86 |
| `6` | UEFI x86 (32-bit) |
| `7` | UEFI x86-64 |
| `9` | UEFI x86-64 |
| `10` | ARM 32-bit UEFI |
| `11` | ARM 64-bit UEFI |
| `15` | HTTP boot UEFI |
| `16` | HTTP boot UEFI x86-64 |

Plus option 60 (vendor class identifier) — `iPXE` for chained
iPXE boot vs first-stage `PXEClient:Arch:00007` etc. The
typical pattern: first DHCP exchange returns a `.kpxe` /
`.efi` binary (option 67 boot-filename + option 66/`siaddr`
next-server); after the client chains to iPXE, a second DHCP
exchange (this time with vendor class `iPXE`) returns an
HTTP URL to a config script.

### Implementation plan

#### Phase 1 — data model

- **New `dhcp_pxe_profile` table.** Group-scoped (lives on
  `DHCPServerGroup`, not per-server, mirroring scopes). Fields:
  - `id: uuid PK`
  - `group_id: uuid FK CASCADE`
  - `name: str` — operator label
  - `description: str | null`
  - `next_server: str` — IPv4 of TFTP/HTTP boot server
  - `enabled: bool = true`
  - `tags: dict`
  - `created_at` / `updated_at`
- **New `dhcp_pxe_arch_match` table.** A profile carries N arch
  matches; each maps a (vendor_class_pattern, arch_code_pattern)
  pair to a boot-filename (or chained iPXE URL). Fields:
  - `id: uuid PK`
  - `profile_id: uuid FK CASCADE`
  - `priority: int` — order matters; lowest first
  - `match_kind: enum("first_stage", "ipxe_chain")` — most-
    specific first
  - `vendor_class_match: str | null` — substring match on
    option 60 (e.g. `PXEClient`, `iPXE`, `HTTPClient`); null
    = match anything.
  - `arch_codes: list[int] | null` — list of option 93
    values to match; null = match anything.
  - `boot_filename: str` — option 67 value. For
    `match_kind = ipxe_chain` this is typically a full HTTP
    URL.
  - `boot_file_url_v6: str | null` — option 59 / Bootfile-URL
    for v6 scopes; optional.
- **Scope-level binding.** Add
  `dhcp_scope.pxe_profile_id: uuid | null` (FK SET NULL).
  Operator picks one profile per scope; null = no PXE.
- **Migration.** Three changes: new `dhcp_pxe_profile` table,
  new `dhcp_pxe_arch_match` table, new
  `dhcp_scope.pxe_profile_id` column.

#### Phase 2 — Kea driver renderer

- **`drivers/dhcp/kea.py`** gains `_render_pxe_classes(group)`
  that walks every profile in the group and emits Kea client
  classes:
  ```json
  {
    "name": "pxe-profile-X-arch-uefi-x64",
    "test": "substring(option[60].hex,0,9)=='PXEClient' and option[93].hex=='0007'",
    "next-server": "10.0.0.5",
    "boot-file-name": "ipxe.efi",
    "server-hostname": ""
  }
  ```
  Class name is deterministic for diffability; `test`
  expression composes vendor-class substring + arch-code
  match.
- **Per-scope wiring.** Scopes with `pxe_profile_id` set get
  their classes appended to the Dhcp4 `client-classes` list.
  Scope-level `next-server` + `boot-file-name` left empty —
  the per-class values win (Kea evaluates classes before
  scope defaults).
- **iPXE chain tag.** When a class has
  `match_kind = ipxe_chain`, the rendered class also sets
  `option-data` with code 175 (iPXE Encap) `0` so dnsmasq-
  style chaining works. (Tweak based on Kea docs — confirm
  with a `client-classes` example before shipping.)
- **Renderer test.** Add a test ensuring two profiles in
  the same group with overlapping arch codes diff into two
  classes with deterministic ordering by priority.

#### Phase 3 — API

- **`/api/v1/dhcp/groups/{gid}/pxe-profiles`:**
  - `GET /` — list profiles in group.
  - `POST /` — create profile + nested `arch_matches`.
  - `GET /{id}` — full profile + matches.
  - `PUT /{id}` — update; replaces match list (mirrors
    `dhcp_pool` / `dhcp_static` patterns).
  - `DELETE /{id}` — cascade.
- **`DHCPScopeUpdate`** accepts `pxe_profile_id` (UUID or
  null).
- **Audit log.** `dhcp.pxe_profile.created` / `.updated` /
  `.deleted` + `dhcp.scope.updated` payload includes
  pxe_profile_id diff.

#### Phase 4 — Frontend

- **Scope edit modal — new "PXE / iPXE" tab.** Drop-in
  alongside the existing General / Pools / Statics tabs:
  - Top: profile picker (combo box) — list of profiles in
    the scope's group + a "Create new..." inline option that
    opens the profile editor as a nested modal.
  - Below: read-only summary of the selected profile's
    next-server + match list (so the operator can see what
    will fire without leaving the scope edit).
- **Profile editor modal.** Title: "PXE Profile — <name>".
  Form layout:
  - Top: name / description / next-server / enabled.
  - Middle: arch-match table — one row per match, draggable
    to reorder (priority). Columns: Match kind / Vendor
    class / Arch codes / Boot filename. Add Row button at
    the bottom.
  - Arch codes column uses a multi-select chip with the
    documented arch-code labels.
  - Match-kind toggle drives which preset boot-filename hints
    the operator sees ("ipxe.efi" vs "http://.../config.ipxe").
- **Group-level page.** Optional — `/dhcp/groups/{gid}/pxe`
  to manage profiles outside the scope flow. Defer if scope
  scope creep; the scope-tab path is the primary surface.

### Checkpoints

- [ ] Migration: `dhcp_pxe_profile` + `dhcp_pxe_arch_match` + `dhcp_scope.pxe_profile_id`
- [ ] Models: `DHCPPXEProfile` + `DHCPPXEArchMatch`; extend `DHCPScope`
- [ ] Schemas: profile create / update / response with nested matches
- [ ] Service: `app/services/dhcp/pxe.py` — CRUD with match-list replace semantics
- [ ] Driver: extend `drivers/dhcp/kea.py` with `_render_pxe_classes`; wire into ConfigBundle
- [ ] Driver tests: deterministic class ordering by priority; vendor + arch match expression renders correctly
- [ ] Router: `/dhcp/groups/{gid}/pxe-profiles` CRUD + `pxe_profile_id` on scope update
- [ ] Frontend: PXE / iPXE tab on scope edit modal
- [ ] Frontend: profile editor modal with draggable arch-match rows
- [ ] Frontend: arch-code multi-select with documented labels
- [ ] `make ci` clean
- [ ] Commit chain split at model+driver / API / frontend

### Done when

All checkpoints checked + commit chain landed + merged to main.

## Risks / unknowns

- **Kea class-test syntax.** The `test` expression syntax for
  matching `option[60]` (vendor class) and `option[93]` (arch)
  has Kea-version-specific quirks. Validate against Kea 2.6+
  docs before committing the renderer; if the substring match
  isn't quite right the agent will refuse the bundle.
- **HTTP-boot vs TFTP-boot.** UEFI HTTP boot uses arch code
  16 with vendor class `HTTPClient` AND requires Kea to set
  option-data `60` to `HTTPClient` in the response (standard
  signaling). The renderer must handle this asymmetry —
  HTTP-boot profiles need the response-side option 60 set,
  TFTP profiles don't.
- **iPXE chain detection.** Kea's option-93 from chained iPXE
  is the *original* arch (matters for the binary served);
  the vendor class is `iPXE`. So the chain class
  `vendor_class = "iPXE"` is what differentiates first-stage
  from chained — most-specific-first ordering is critical.
- **Scope-level next-server fallback.** If profile is null,
  do scope-level `next-server` / `boot-file-name` fields
  still render? Probably yes (manual-mode path for legacy
  configs) — don't strip them.
- **DHCPv6 PXE.** Out of scope for v1. v6 PXE uses option 16
  (vendor class) + option 59 (Bootfile-URL); plumb the
  `boot_file_url_v6` field but don't render to v6 scopes
  yet. Follow-up issue.
