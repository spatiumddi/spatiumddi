# Changelog

All notable changes to SpatiumDDI are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/); versioning uses [CalVer](https://calver.org/) (`YYYY.MM.DD-N`).

This file is hard-wrapped at ~70 chars for terminal reading. The
release workflow runs each section through `scripts/format_release_
notes.py` before pasting it into the GitHub release body, which:

* unwraps consecutive prose lines into single-line paragraphs (since
  GitHub's release renderer turns every `\n` into a forced `<br>`),
* wraps the top summary paragraph in `### 🚀 Highlights`,
* emoji-prefixes the standard section headings (`### ✨ Added`,
  `### 🔧 Changed`, `### 🐛 Fixed`, `### 🔒 Security`,
  `### 🗃️ Migrations`, `### ⚠️ Deprecated`, `### 💥 Breaking`).

So author each section here with the plain Keep-a-Changelog headings
(`### Added`, `### Changed`, …) and a hard-wrapped summary paragraph;
the formatter handles the rest.

---

## Unreleased

The **operator-toggleable platform** wave so far. Three batches
landed: feature-module toggles + Settings → Features page that
let admins hide whole sidebar / REST / MCP surfaces (Network
ownership entities, AI Copilot, Conformity, Tools, plus the four
integrations); a Tool Catalog rewrite that mirrors the Features
page layout (3-col adaptive grid + pill toggle + auto-save on
flip); and the Operator Copilot's Tier 2 tool wave from issue
\#101 — six new read tools surfacing customers / sites /
providers / users / groups / roles, plus a `get_customer_summary`
roll-up that counts every owned resource type for one customer.
Plus release-note formatting got a complete rework: the GitHub
release body now reads as flowing prose with emoji section
headings instead of a wall of forced `<br>`s, applied to the
last three releases retroactively.

### Added

- **Aggregation candidates — passive badge with per-candidate
  snooze (issue #114).** The inline "Aggregation suggestions"
  banner that crowded the IPAM page on every load is replaced
  with a small badge button in the block header — click expands
  a popover showing the same candidate set with two new
  per-row actions: **Snooze 30 days** (re-appears after the
  timer) and **Don't suggest again** (permanent, operator-flagged
  "I know, leave me alone"). Snooze entries persist in
  `platform_settings.aggregation_snooze` JSONB keyed on a stable
  hash of parent block + sorted child CIDRs, so a snooze still
  matches the same candidate even if `collapse_addresses` returns
  the children in a different order on a later pass. Filtered
  server-side by default; the popover surfaces a "Show snoozed"
  toggle so operators can revisit and Re-enable. Per-session
  expand/collapse keyed on block id, so an operator working
  through suggestions doesn't re-click on every nav. Migration
  `d9e4c12a7f85`.
- **Dashboard IPAM-tab IP-space filter (issue #115).** Multi-select
  pill on the Dashboard's IPAM tab that scopes every space-aware
  card to the selected spaces — IPv4/v6 split, capacity headroom,
  utilization KPIs, subnet heatmap, top-subnets list, and the
  shared KPI grid all narrow to the selection. Default "All
  spaces"; persisted per session in `sessionStorage` keyed on
  `spatium.dashboard.ipam.space_filter` so a refresh / drawer
  toggle / nav-away-and-back keeps it. Pill is IPAM-tab-only —
  switching to Overview / DNS / DHCP shows global numbers
  unfiltered, which sidesteps the "the filter looks broken on
  other tabs" trap. Non-IPAM-shaped surfaces on the IPAM tab
  (the Integrations panel) carry a small "(not space-scoped)"
  annotation when the filter is active so operators understand
  the scope. Backend: `/ipam/subnets` `space_id` query param now
  accepts repeated values (`?space_id=A&space_id=B`) — single-id
  callers still work since FastAPI's repeated-key parsing returns
  a 1-element list. axios's `paramsSerializer: { indexes: null }`
  serialises arrays as repeated keys with no brackets.
- **UniFi Network integration — Phase 1.** Issue #30. Read-only
  mirror of UniFi networks + active clients into IPAM. Per
  controller `unifi_controller` row, dual-transport (local +
  cloud connector via api.ui.com), dual-API (public Integration
  v1 surface for site enumeration + version probe; legacy
  controller API for the actual rich data — `rest/networkconf`,
  `stat/sta`, `rest/user` — which is the only place UniFi
  exposes MAC, hostname, network_id, oui, fixed_ip, and CIDR
  fields). Single `UnifiClient` parameterised by mode; cloud
  mode prepends the `connector/consoles/<host_id>` segment.
  Auth flavours: `api_key` (modern UniFi OS, required for
  cloud) or `user_password` (legacy local-only via cookie +
  CSRF login). Networks land as Subnets with gateway from
  `ip_subnet`, VLAN tag preserved; clients land as IPAddress
  rows keyed on MAC with hostname / OUI / `is_wired` carried
  through. DHCP fixed-IP reservations from `rest/user` mirror
  as `status="reserved"` so the UI surfaces them as static.
  `mirror_networks` / `mirror_clients` / `mirror_fixed_ips`
  per-row toggles, `site_allowlist` (empty = all sites),
  `network_allowlist` per-site VLAN filter, `include_wired` /
  `include_wireless` / `include_vpn` (default false) for the
  client mirror. Same Celery beat shape as the other
  integrations — 30 s tick, per-row interval gate, 60 s floor
  in cloud mode (api.ui.com rate-limits). Sidebar entry,
  admin CRUD page with mode-aware form (host vs. cloud_host_id),
  per-row test-connection probe, MCP `list_unifi_targets` tool,
  feature module `integrations.unifi` (default off — operator
  must opt in via Settings → Features → Integrations).
  Migration `b2c84f7a91d3` adds `unifi_controller` table +
  `unifi_controller_id` cascade FK on `subnet` / `ip_block` /
  `ip_address` and seeds the feature_module row. Phase 2 (DHCP
  reservation surfacing in the controller detail view + WiFi
  broadcast roster) and Phase 3 (write surface — propose-only
  subnet/VLAN renames pushed back via the integration API) are
  deferred per the issue's phasing.
- **Operator feature toggles + Settings → Features page.** New
  `feature_module` table seeded from a 17-entry catalog covering
  Network (customer / provider / site / service / asn / circuit /
  device / overlay / vlan / vrf), AI (copilot), Compliance
  (conformity), Tools (nmap), and Integrations (kubernetes /
  docker / proxmox / tailscale). Network / AI / Compliance / Tools
  default-on for discovery; Integrations default-off (each one
  needs operator-supplied credentials anyway). New
  `require_module(...)` FastAPI dependency 404s when a module is
  disabled, applied to every togglable router. The AI tool
  registry gains a `module` attribute and `effective_tool_names`
  strips disabled-module tools regardless of catalog overrides —
  disabling `network.customer` removes the customer find / count
  tools end-to-end. Toggle endpoint mirrors integration toggles
  into the existing `PlatformSettings.integration_*_enabled`
  columns in the same transaction so reconciler tasks
  (`kubernetes_sync` / `docker_sync` / `proxmox_sync` /
  `tailscale_sync`) keep gating on the settings column without
  migration churn. Migration `d8b5e4a91f27` backfills the
  feature_module rows from existing settings so on-toggles stay on
  across the upgrade. Settings → Features page lays out modules
  in a 3-column adaptive grid (wide groups full-width, narrow
  groups cluster three-up so AI / Compliance / Tools sit
  side-by-side), tab split between "Features" and "Integrations",
  pill toggle that auto-saves on flip. Integration toggles moved
  out of Settings → Integrations in the same wave — single home
  for the on/off switch is now Features → Integrations. CLAUDE.md
  Non-Negotiable \#14 documents the five-step checklist for
  adding new togglable feature modules in future PRs.
- **AI Tool Catalog page rewrite — auto-save + matching layout.**
  Same 3-column adaptive grid as Features, same shared
  `Toggle` pill, every flip fires `PUT /ai/tools/catalog` with
  the recomputed enabled list — no more batch Save button. React
  Query optimistic update via `setQueryData` so the toggle moves
  instantly; reverts on error, refetches on settle. Per-category
  "Enable all" / "Disable all" link in the section header still
  fires a single batch PUT for bulk operations. "Reset to
  defaults" sends NULL to revert to registry per-tool defaults.
  Search filter + "registry defaults" badge preserved.
- **Operator Copilot — Tier 2 tool wave (issue \#101).** Six new
  read-only tools register on import via two new modules. New
  `tools/ownership.py`: `list_customers` (filterable by status /
  contact substring), `get_customer_summary` (deep roll-up
  counting subnets / blocks / spaces / circuits / services / ASNs
  / DNS zones / domains / overlays for one customer; accepts UUID
  or exact name), `list_sites` (kind + region filters, surfaces
  parent-site nesting), `list_providers` (kind + contact filters,
  surfaces default-ASN linkage). New `tools/admin.py`: `list_users`
  (auth source + active flag + superadmin filter, returns groups
  / MFA / lockout state), `list_groups` (role assignments +
  member counts + has-role filter), `list_roles` (built-in vs
  custom filter, returns the full permission grants JSON +
  groups holding the role). All three admin tools are
  superadmin-gated inline — non-superadmin callers get a clear
  "ask your platform admin" error rather than silent empty
  results. Ownership tools tagged with the matching feature_module
  (`network.customer` / `network.site` / `network.provider`) so
  disabling a module removes the corresponding tools from the AI
  surface in lock-step with the sidebar. Operators who pinned an
  explicit `platform_override` won't auto-get the new tools — by
  design; they'll appear as disabled rows in the Tool Catalog
  page so the operator can opt in.
- **Operator Copilot — Tier 4 tool wave (issue \#101).** Ten new
  read-only tools across two modules. New `tools/integrations.py`
  with `list_kubernetes_targets` / `list_docker_targets` /
  `list_proxmox_targets` / `list_tailscale_targets` — each gated
  by the matching `integrations.*` feature_module so disabling
  the integration in Settings → Features removes the tool from
  the AI surface. Output is intentionally narrow — credentials,
  CA bundles, encrypted keys never appear; just name / endpoint /
  enabled flag / last-sync timestamp + error / IPAM space binding.
  New `tools/observability.py` with `query_dns_query_log` (BIND9
  query log; qname / qtype / client_ip / view / since-window
  filters), `query_dhcp_activity_log` (Kea activity log; severity
  / log code / MAC / IP filters), `query_logs` (inventory of which
  log sources are populated in the last N hours — operators run
  this once per chat to learn what's available), `get_dns_query_rate`
  + `get_dhcp_lease_rate` (timeseries roll-ups from the
  `dns_metric_sample` / `dhcp_metric_sample` tables, capped at 24
  buckets), and `global_search` (cross-resource lookup that
  reuses the same internal helpers as the Cmd-K palette via lazy
  import to avoid pulling FastAPI router glue at boot).
- **Operator Copilot — Tier 3 tool wave (issue \#101).** Ten new
  read-only sub-resource tools that drill into the rows inside a
  zone / scope. DNS side: `list_dns_records` (cross-zone with
  name / fqdn / type / value substring filters — distinct from
  the existing per-zone `query_dns_records`), `list_dns_blocklists`
  (RPZ rows with category / source / sync state), `list_dns_pools`
  (GSLB pools with eager-loaded members + per-member health
  state), `list_dns_views` (split-horizon views). DHCP side:
  `list_dhcp_pools` (dynamic / excluded / reserved ranges within
  a scope), `list_dhcp_statics` (MAC → IP reservations with
  hostname filter), `list_dhcp_client_classes` (group-scoped
  conditional option-delivery), `list_dhcp_option_templates`
  (named option bundles), `list_pxe_profiles` (PXE / iPXE
  provisioning profiles with per-arch boot-file matches),
  `list_dhcp_mac_blocks` (group-global blocked MACs with reason +
  expiry). All default-enabled per the discovery argument; all
  appended to the existing `tools/dns.py` and `tools/dhcp.py`
  modules. Total tool count: 67 → 77.
- **Operator Copilot — Tier 5 write proposals (issue \#101).**
  Four new `propose_*` tools that stage write actions for the
  operator to Approve / Reject in the chat drawer:
  `propose_create_dns_record` (zone_id + name + record_type +
  value + optional ttl/priority; preview probes for an identical
  existing record and surfaces it as a hint without rejecting),
  `propose_create_dhcp_static` (scope_id + ip_address + mac_address
  + optional hostname/description; preview rejects on out-of-scope
  IPs and warns on conflicting IP/MAC reservations),
  `propose_create_alert_rule` (subnet-utilization rule_type only —
  the simplest case; other rule_types keep their UI authoring
  path), `propose_archive_session` (sets
  `AIChatSession.archived_at = now()`; preview rejects cross-user
  attempts so operators can only archive their own sessions). All
  four ship default-disabled — operators opt in per-tool via
  Settings → AI → Tool Catalog. Each underlying mutation lives
  as a registered `Operation` in `services/ai/operations.py` with
  a preview / apply pair and writes an audit row tagged
  `via=ai_proposal` at apply time. **Double validation to enable**:
  the Tool Catalog page now detects the `propose_` name prefix
  and shows a confirm modal before turning on any such tool —
  even bulk "Enable all" on a category routes write proposals
  through the modal so the AI can't be silently armed with write
  capability. Bonus visual cue: each propose_ tool now renders a
  yellow "proposal" badge in the catalog so they read as distinct
  from read-only tools at a glance. Total tool count: 47 → 67
  (10 of those default-disabled, including the 4 propose_ tools
  and the 4 integration list_targets gated behind their default-
  off feature_module). Deferred from Tier 5 per the issue's
  "needs UX thought" note: `propose_create_subnet` (too many edge
  cases — auto-allocate network/broadcast rows, parent-block
  overlap checks, allocation policy) and the `propose_delete_*`
  family (cascade-impact preview is a separate design pass).
- **Release-note formatting — flowing prose + emoji section
  headings.** New `scripts/format_release_notes.py` runs between
  the changelog awk-extract and the GitHub release body in
  `.github/workflows/release.yml`. The transformer joins
  consecutive prose lines into single-line paragraphs (so GFM's
  `breaks: true` mode renders them as one paragraph instead of
  forced `<br>`s), preserves blank-line paragraph breaks + fenced
  code blocks + bullet lists, and emoji-prefixes the
  Keep-a-Changelog headings — `🚀 Highlights` (the top summary),
  `✨ Added`, `🔧 Changed`, `🐛 Fixed`, `🔒 Security`,
  `🗃️ Migrations`, `⚠️ Deprecated`, `💥 Breaking`. Soft-hyphen
  edge case handled (`per-\nframework` joins to `per-framework`,
  not `per- framework`). Idempotent — safe to re-run on
  already-transformed input. Backfilled the last three releases
  on GitHub (`2026.05.05-2`, `2026.05.05-1`, `2026.05.03-1`) via
  `gh release edit` so they pick up the new format immediately.
  CHANGELOG.md keeps its terminal-friendly hard-wrap; the
  formatter handles release-time cleanup.
- **Shared `Toggle` pill component.** Extracted from
  `SettingsPage.tsx` into `components/ui/toggle.tsx` so the
  Settings page, Features page, and Tool Catalog page all use
  the same on/off pill with identical look + hit area.
- **CLAUDE.md — Non-Negotiable \#13 (MCP coverage) + \#14 (feature
  module gating).** Two new project-wide rules: when adding a new
  resource / feature with REST endpoints, also expose matching
  MCP tools (with an explicit per-tool default-enabled decision);
  when adding a new top-level resource family, evaluate whether
  it should be a togglable feature module and follow the
  five-step checklist (catalog entry, seed migration, route gate,
  MCP module attribute, sidebar nav module tag).

### Changed

- **Sidebar integration visibility moved to feature_module.** The
  sidebar now reads `useFeatureModules().enabled("integrations.*")`
  instead of `platformSettings.integration_*_enabled`. Behaviour
  is identical (the toggle endpoint mirrors both columns) — but
  the source of truth is the new feature_module catalog so future
  toggles stay consistent.

### Migrations

- `c4f7a1d3e589_feature_module_table.py` — new `feature_module`
  table seeded with 13 togglable ids (network.\* / ai.copilot /
  compliance.conformity / tools.nmap).
- `d8b5e4a91f27_integration_feature_modules.py` — adds the four
  `integrations.*` rows + backfills `enabled` from existing
  `PlatformSettings.integration_*_enabled` so existing on-toggles
  stay on.
- `b2c84f7a91d3_unifi_integration.py` — new `unifi_controller`
  table; `unifi_controller_id` cascade FK on `subnet` /
  `ip_block` / `ip_address`; `integration_unifi_enabled` column
  on `platform_settings`; seeds the `integrations.unifi`
  feature_module row at `enabled=False`.

### Fixed

- **CodeQL alert \#25 — explicit TLS minimum version on the
  copilot's `tls_cert_check` tool.** `_fetch_cert_sync` in
  `app/services/ai/tools/ops.py` now sets
  `ctx.minimum_version = ssl.TLSVersion.TLSv1_2` explicitly so
  TLSv1.0 / 1.1 servers fail handshake with a clear error and
  the contract is obvious to readers (modern OpenSSL already
  disables them by default — explicit beats implicit, and CodeQL
  no longer flags the call site).

---

## 2026.05.05-2 — 2026-05-05

The **Operator Copilot polish + compliance loop** release. Three
threads land together. **Operator Copilot polish** turns the chat
into something operators can actually rely on — especially against
self-hosted Ollama: the OpenAI-compat driver now forwards
`options.num_ctx` / `num_predict` / `extra_body` so Ollama respects
the configured context window (the silent 2048-token default was
truncating the 8K-token prompt + tool schemas and caused every
small model to hallucinate tool names from a half-cut list); a
reasoning-channel fallback captures `delta.reasoning` from
qwen3.5 / DeepSeek-R1 / o1-style models so post-tool answers don't
disappear; a trailing-usage-chunk handler picks up Ollama's
separate empty-choices `usage` chunk so per-message token counts
are accurate. The tool registry expands from 22 to 35 read-only
tools with new modules covering ASNs / domains / VRFs / circuits /
services / overlays / applications, plus `find_switchport` (joins
IP→MAC→FDB→interface), `ping_host`, nmap inspection +
`propose_run_nmap_scan` write proposal, OUI vendor enrichment
inline on `find_ip` / `find_dhcp_leases` / `find_switchport`, and
name-or-UUID resolution on `space_id` / `block_id`. Per-provider
**system prompt override** + per-provider **tool allowlist** ship
as new tabs on the AI Provider modal — narrow Ollama down to the 8
tools you actually use, restrict a kiosk provider to read-only,
fork the prompt without losing the baked-in default. The chat
drawer renders markdown via react-markdown / remark-gfm, persists
the active session + composer draft to sessionStorage, surfaces
per-message tokens / copy / info popover under every assistant
reply, and grows multi-select bulk delete in the History panel.
**Compliance change alerts (#105)** add a new `compliance_change`
rule type plus three disabled seed rules (PCI / HIPAA /
internet-facing): the alert evaluator scans the audit log on the
existing 60 s tick, opens one event per mutation against a
classification-flagged subnet (or descendant IP / DHCP scope), and
auto-resolves after 24 h. **Conformity evaluations (#106)** are
the proactive companion: declarative `ConformityPolicy` rows pin a
`check_kind` against a target set; a beat-driven engine runs every
enabled policy on its `eval_interval_hours` cadence, writes
append-only `ConformityResult` rows, and emits AlertEvent on
pass→fail transitions. Six starter check kinds cover the common
shapes (`has_field`, `in_separate_vrf`, `no_open_ports`,
`alert_rule_covers`, `last_seen_within`, `audit_log_immutable`),
eight seed policies span PCI-DSS / HIPAA / SOC2, and a synchronous
**reportlab PDF export** is the auditor-facing artifact (per-
framework section, failing-row enumeration with diagnostic JSON,
SHA-256 integrity hash over (id, status) tuples in the trailer for
tamper detection). Two new built-in roles — **Auditor**
(read-only) and **Compliance Editor** (admin) — drop into the
RBAC seeder. #105 + #106 form the complete compliance loop:
alerts catch the change in real time, evaluations prove steady
state and produce the document auditors actually file. Plus
ancillaries: nmap `quick` preset bumped from top-100 to top-1000
(`udp_top100` preset renamed to `udp_top1000` with migration), a
`PXE Profiles` button added to the DHCP server-group view, README
gains a top-level table of contents, and a mypy fix on
`network_modeling.py` unblocks the dependabot axios PR (#102).

### Added

- **Operator Copilot — Ollama context-window forwarding.**
  `openai_compat` driver now forwards `provider.options.num_ctx` /
  `num_predict` / `extra_body` via the OpenAI SDK's `extra_body`
  parameter so Ollama respects the configured context. Without
  this Ollama silently truncated to its 2048-token default and
  cut the system prompt + tool schemas mid-stream, which caused
  every small model (gemma4 / qwen2.5 / qwen3.5 / gpt-oss) to
  hallucinate tool names. README documents the
  `OLLAMA_CONTEXT_LENGTH` env-var route as the recommended
  server-side default.
- **Operator Copilot — reasoning-channel fallback in the streaming
  driver.** qwen3.5 / DeepSeek-R1 / o1-style models route their
  post-tool answer to `delta.reasoning` instead of
  `delta.content`. The driver now captures both, flushes
  `reasoning_buf` as content when the turn emitted no content and
  no tool calls, and falls back to `model_extra["reasoning"]`
  when the field isn't a first-class delta attribute.
- **Operator Copilot — trailing usage-chunk handler.** Ollama
  emits a separate empty-choices chunk carrying `usage` after the
  `finish_reason` chunk. Driver now branches on `not choices and
  chunk.usage`, the orchestrator captures token counts independent
  of `finish_reason`, and the per-message footer renders accurate
  prompt / completion totals.
- **Operator Copilot — per-provider system prompt override.**
  Migration `d6a39e84c512` adds `ai_provider.system_prompt_override`
  TEXT column. New "System prompt" tab in the AI Provider
  create/edit modal carries a textarea, "Reset to default", "Start
  from default" copy-and-edit, and a collapsible inline view of
  the baked-in default. Snapshotted into the chat session at
  creation time so a mid-conversation provider edit doesn't break
  in-flight chats. Baked-in default expanded ~10× — persona, DDI
  domain primer, full tool taxonomy, response-style rules,
  write-action gating, "no LaTeX" + "not a general-purpose coding
  assistant" scope rules, three worked examples for the canonical
  question shapes.
- **Operator Copilot — per-provider tool allowlist.** Migration
  `c4e8b71f0d23` adds `ai_provider.enabled_tools` JSONB column
  (NULL = "all enabled" default; empty list = no tools at all;
  non-empty list = exactly those names). New "Tools" tab in the
  AI Provider modal renders a category-grouped checkbox list with
  tool descriptions and "write" badges on `propose_*` rows.
  Saving with every box checked writes back NULL so the provider
  stays on "use whatever the registry has" rather than pinning a
  stale snapshot. Orchestrator filters
  `REGISTRY.read_only()` to `provider.enabled_tools` when set;
  unknown names silently skipped at request build time so a tool
  rename doesn't break a saved allowlist. The system prompt's
  "Tools available: N" line mirrors the filtered count. New
  `GET /api/v1/ai/providers/tools` returns the catalog
  (name / description / category / writes flag), registered ahead
  of `/{provider_id}` so Starlette matches the literal first.
  Three use cases this addresses: small local Ollama models that
  struggle with 35 tools, read-only kiosk providers, compliance
  posture restricting `propose_*` writes per provider.
- **Operator Copilot — tool registry expansion (22 → 35).** New
  module `tools/network_modeling.py` covering recently-shipped
  entities: `list_asns`, `get_asn`, `list_domains`, `list_vrfs`,
  `list_circuits`, `list_network_services`,
  `get_network_service_summary`, `list_overlay_networks`,
  `get_overlay_topology`, `list_application_categories`. New
  module `tools/nmap.py` with `list_nmap_scans` +
  `get_nmap_scan_results` read tools, plus `propose_run_nmap_scan`
  in `tools/proposals.py` wired through the existing
  preview/apply operation pattern with audit hooks. New
  `find_switchport` joins IP→MAC (IPAM or ARP) → FDB → interface
  to answer "what port is X plugged into" — flags trunk uplinks
  via `interpretation_hint` when the MAC matches multiple
  interfaces. New `ping_host` runs argv-validated ICMP from the
  SpatiumDDI host (read tool, no proposal — liveness checks
  shouldn't need a confirm prompt). Name-or-UUID resolution on
  `space_id` / `block_id` lets the model pass `"home"` instead of
  a UUID. OUI vendor enrichment surfaced inline on `find_ip` /
  `find_dhcp_leases` / `find_switchport` so no extra tool call
  is needed. Tool-not-found errors echo the full registry list
  + a `hint` so smaller models that hallucinate names can
  self-correct.
- **Operator Copilot — chat drawer markdown rendering.**
  Assistant messages now render through `react-markdown` +
  `remark-gfm` (tables, code fences, headings, links). Curated
  override map keeps inline code distinct from block code via the
  `language-` className regex. Streaming-compatible — partial
  render on every chunk, blinking caret preserved.
- **Operator Copilot — sessionStorage persistence.** Active
  session ID + composer draft persist via `useSessionState` so
  closing and reopening the drawer lands on the same conversation
  with the half-typed message intact. Stale-id guard
  (`detailQ.isError → setActiveSessionId(null)`) drops the
  reference cleanly when the underlying session is deleted in
  another tab.
- **Operator Copilot — per-message footer + bulk delete.**
  OpenWebUI-style footer under every assistant reply renders
  prompt / completion tokens, a copy button, and an info popover
  with provider / model / latency. History panel grows a checkbox
  column + "Select all" + "Delete N" / "Delete all" toolbar
  driven by a single `bulkDeleteMut` Promise.all fan-out. Both
  delete paths invalidate `["ai-usage-me"]` so the daily token
  chip drops live as messages cascade.
- **Operator Copilot — anti-loop dedup guard.** Per-turn
  `seen_calls: set[tuple[str, str]]` in the orchestrator catches
  small models that loop on a successful tool call (qwen2.5:7b
  reproducibly did this); the loop emits a synthetic warning
  telling the model the result is already in context, breaking
  the cycle without crashing the conversation.
- **Compliance change alerts (#105) — `compliance_change` rule
  type.** Migration `e3f1c92a4d68` adds three columns to
  `alert_rule`: `classification` (one of `pci_scope` /
  `hipaa_scope` / `internet_facing`), `change_scope` (one of
  `any_change` / `create` / `delete`), and `last_scanned_audit_at`
  watermark. The evaluator scans `audit_log` on the existing 60 s
  alert tick, opens one event per mutation against a
  classification-flagged subnet (or descendant IP / DHCP scope
  via the subnet FK), and auto-resolves after 24 h. Watermark
  baselines to `now()` on first run so historical audit rows
  don't retro-page operators. Resource resolution falls back to
  `audit_log.old_value.subnet_id` for delete actions where the
  live row no longer exists. Per-pass scan capped at 1000 audit
  rows so a long-disabled rule flipping on doesn't pause the
  evaluator. Three disabled seed rules (PCI / HIPAA /
  internet-facing) ship via the existing main.py role/seed
  pipeline. Frontend `AlertsPage` rule-type picker + form gain a
  Compliance optgroup with classification + change-scope fields.
- **Conformity evaluations (#106) — declarative policies +
  scheduled evaluator + auditor PDF.** Migration `b5d8a3f12c91`
  adds `conformity_policy` (declarative check definitions —
  framework / reference / severity / target_kind / target_filter
  / check_kind / check_args / enabled / eval_interval_hours /
  fail_alert_rule_id) and `conformity_result` (append-only
  history, indexed twice on `(policy_id, evaluated_at)` and
  `(resource_kind, resource_id, evaluated_at)` so both natural
  drilldowns hit an index). Beat task `app.tasks.conformity`
  ticks every 60 s; per-policy `eval_interval_hours` gating
  (default 24 h) keeps the work cheap. On-demand re-evaluation
  via `POST /conformity/policies/{id}/evaluate`. Six starter
  `check_kind` evaluators in `services/conformity/checks.py`:
  `has_field` (non-empty named field), `in_separate_vrf`
  (subnet's effective VRF holds only classification-matched
  siblings), `no_open_ports` (latest nmap scan didn't expose
  forbidden ports — warn when no recent scan, never silent-pass),
  `alert_rule_covers` (≥1 enabled alert rule of named rule_type
  exists), `last_seen_within` (IP / subnet recency check), and
  `audit_log_immutable` (platform-level positive-presence
  signal). Eight seed policies covering PCI-DSS / HIPAA /
  internet-facing / SOC2, all `is_builtin=True` and
  `enabled=False` so the operator opts in. Built-in rows accept
  narrow updates only (enabled / interval / severity /
  fail_alert_rule_id / description) — clone first to author a
  variant. pass→fail transitions emit `AlertEvent` rows against
  the policy's wired alert rule when set, surfacing conformity
  drift in the existing alerts dashboard. Permission resource
  type `conformity` plus two new built-in roles seeded:
  **Auditor** (read-only on conformity + audit + the underlying
  resources, suitable for an external auditor account that can
  pull the PDF and verify evidence without changes) and
  **Compliance Editor** (admin on conformity + read on the
  underlying resources, for the team that authors and tunes
  policies). Frontend `/admin/conformity` page renders a
  per-framework summary card row, policies table with inline
  toggle / re-evaluate / edit / delete, and a filterable results
  panel where each row expands to show the diagnostic JSON
  inline. Platform Insights gains a Conformity card with deep-
  link. Sidebar entry under the Auditing divider.
- **Conformity — auditor-facing PDF export.** New `reportlab>=4.2`
  dependency. `services/conformity/pdf.py` renders the latest
  result per (policy, resource) tuple as a single PDF organised
  by framework: per-framework summary table, per-policy section
  with pass / warn / fail / not_applicable tally and enumerated
  failing rows with the diagnostic JSON pretty-printed beneath,
  trailer with a SHA-256 hash over (result_id, status) tuples
  so the auditor can verify the underlying rows haven't been
  edited post-generation. `GET /conformity/export.pdf` endpoint
  with an optional `?framework=` filter. Per-framework "download
  PDF" deep-link icon in each summary card.
- **DHCP — PXE Profiles button on the server-group view.**
  `GroupDetailView` header now carries a PXE Profiles
  `HeaderButton` that navigates to `/dhcp/groups/:gid/pxe`.
  Previously the page was only reachable from inside the scope
  edit modal.
- **README — top-level table of contents.** New `## Contents`
  section between the alpha warning and the elevator pitch with
  one anchor per top-level heading so the 700-line README is
  scannable from the top.

### Changed

- **Operator Copilot — system prompt + safety rules.** Baked-in
  default expanded ~10× to include the persona, DDI domain
  primer, full tool taxonomy, write-action gating, formatting
  conventions (no LaTeX), and three worked examples. Explicit
  "you are not a general-purpose coding assistant" scope rule
  (decline code generation outside narrow platform-config
  snippets). Reads the per-provider override when set, falls
  back to the baked-in default otherwise.
- **nmap — `quick` preset bumped to top 1000.** Was `-T4 -F`
  (top 100); now `-T4 --top-ports 1000`. The `udp_top100` preset
  renamed to `udp_top1000` (`--top-ports 1000`) with migration
  `a8d6e10f3b59` backfilling existing scan rows so historical
  history doesn't silently mislabel its preset. Frontend
  `NmapScanForm` + IPAM auto-profile preset enum updated.
- **Compliance change alerts (#105) — fields surface on
  AlertRuleResponse.** `classification` / `change_scope` /
  `last_scanned_audit_at` flow through the REST surface; the
  rule-list cell renders `pci_scope · any_change` instead of
  the generic dash for compliance_change rows.
- **README — Operator Copilot section rewritten.** ~13 lines →
  ~60 lines covering accurate tool names, the Ollama 5-minute
  self-host recipe, and the `OLLAMA_CONTEXT_LENGTH` gotcha
  called out so operators don't repeat the truncation
  diagnosis.

### Fixed

- **mypy — `network_modeling.py` `dict(rows.all())` typing.**
  SQLAlchemy returns `Sequence[Row[tuple[UUID, int]]]` which
  mypy can't narrow to `Iterable[tuple]` for `dict()`'s
  constructor. Three count-rollup queries rewritten as
  `{row[0]: row[1] for row in rows.all()}` dict comprehensions.
  Same fix unblocks the dependabot axios 1.15.0 → 1.15.2 PR
  (#102) — its CI was failing on the same three errors. After
  this lands the PR can be rebased (`@dependabot rebase`) and
  merged.

### Migrations

- `a8d6e10f3b59_nmap_udp_top100_to_top1000` — backfills
  `nmap_scan.preset` from `udp_top100` → `udp_top1000`.
- `c4e8b71f0d23_ai_provider_enabled_tools` — adds
  `ai_provider.enabled_tools` JSONB column (NULL-default).
- `d6a39e84c512_ai_provider_system_prompt_override` — adds
  `ai_provider.system_prompt_override` TEXT column (NULL-default).
- `e3f1c92a4d68_compliance_change_alerts` — adds three columns
  to `alert_rule` (`classification` / `change_scope` /
  `last_scanned_audit_at`) for the new rule type.
- `b5d8a3f12c91_conformity_evaluations` — creates
  `conformity_policy` + `conformity_result` tables with their
  partial indexes.

---

## 2026.05.05-1 — 2026-05-05

The **Operator Copilot + network modeling** release. Two big themes
land together: **Operator Copilot (#90)** ships in two phases —
Phase 1 lays the LLM provider foundation (config, MCP HTTP endpoint,
tool registry with 18 read-only tools, chat orchestrator + SSE chat
endpoint, floating chat drawer, token / cost observability with
per-user daily caps); Phase 2 widens it to a full multi-vendor
copilot with Anthropic (Claude) + Azure OpenAI + Google Gemini
drivers alongside the existing OpenAI-compat driver, automatic
failover chain across enabled providers, "Ask AI about this"
affordances on subnets / IPs / DNS zones / records / alerts / audit
rows / DHCP / network devices, custom prompts library, Cmd-K palette
"Ask AI" entry, daily Operator Copilot digest, write tools with
preview / apply flow, and richer dynamic context in the system
prompt. **Network modeling (#91 / #93 / #94 / #95)** lands a
four-issue umbrella: **Customer / Site / Provider** logical
ownership entities cross-cutting IPAM / DNS / DHCP, **WAN circuits**
with transport classes + endpoints + term + cost (foundation for
the SD-WAN routing layer), **service catalog** (`network_service`
+ polymorphic `network_service_resource` join row that bundles VRF /
Subnet / IPBlock / DNSZone / DHCPScope / Circuit / Site /
OverlayNetwork into a customer-deliverable, with kind-aware
`/summary` endpoint — L3VPN view returns canonical VRF + edge
sites + edge circuits + edge subnets shape with warnings), and
**SD-WAN overlay topology** (`overlay_network` + `overlay_site` m2m
with role + edge device + ordered preferred-circuit chain +
`routing_policy` + curated `application_category` catalog seeded
with 33 well-known SaaS apps; `/topology` endpoint returns
nodes/edges by shared preferred circuits; `/simulate` endpoint runs
pure read-only what-if when circuits go down; SVG circular-layout
topology visualization). Also lands the **security wave** (#69 TOTP
MFA for local users, #74 API-token scopes, #75 subnet classification
tags), **#26 IPAM template classes** (reusable stamp templates with
child layouts), **#27 block move across IP spaces** (typed-name
confirm + dependent-row validation), **#25 split-horizon DNS
publishing** at the IPAM layer (block-level `dns_split_horizon` flag
inheritable to descendant subnets), **#51 PXE / iPXE provisioning
profiles** for DHCP, **#96 API docs link** in sidebar + header, plus
a wave of UX polish: tabbed IP Space modals (shared `ModalTabs`
helper), pinned identity header above tabs on every IPAM modal, the
Network sidebar section sub-grouped (Logical: Customers / Providers
/ Services / Sites; Infrastructure: ASNs / Circuits / Devices /
Overlays / VLANs / VRFs), Administration sidebar split, and an
extended `seed_demo.py` that now covers every shipped entity for a
realistic demo dataset.

### Added

- **Operator Copilot — provider config + LLM driver foundation
  (#90 Wave 1).** Migration `a4b8c2d619e7` adds `ai_provider`
  table (Fernet-encrypted api_key, kind discriminator with CHECK
  constraint covering `openai_compat` / `anthropic` / `google` /
  `azure_openai`, ordered priority for failover, JSONB options
  bag, indexed on `(is_enabled, priority)`). LLM driver ABC at
  `app/drivers/llm/base.py` defines neutral request / chunk / tool
  dataclasses modeled on the OpenAI Chat Completions schema;
  concrete drivers translate at the SDK boundary so the
  orchestrator only speaks the neutral interface. OpenAI-compat
  driver covers OpenAI + Ollama + OpenWebUI + vLLM + LM Studio +
  llama.cpp server + LocalAI + Together + Groq + Fireworks with
  streaming + tool-call delta reassembly per `index`.
  `/api/v1/ai/providers` CRUD with unsaved test-connection probe.
  Admin → AI Providers page wires it all up.
- **Operator Copilot — tool registry + 18 read-only tools + MCP
  HTTP endpoint (#90 Wave 2).** Tool registry shape mirrors the
  driver registry. 18 read-only tools cover the common operator
  asks: `list_subnets`, `get_subnet`, `list_ips`, `get_ip`,
  `list_zones`, `list_records`, `list_dhcp_scopes`, `list_leases`,
  `list_alerts`, `list_audit`, `list_devices`, `list_circuits`,
  `list_customers`, `list_sites`, `list_providers`, `list_asns`,
  `list_vrfs`, `list_overlays` (added later in Phase 2). MCP-shaped
  HTTP endpoint at `/api/v1/ai/mcp` exposes the same tool set so
  external MCP clients (Claude Desktop, Cursor, Cline) can connect
  directly without going through the chat drawer.
- **Operator Copilot — chat orchestrator + sessions API + SSE
  chat endpoint (#90 Wave 3a).** Migration `b5d9c41e2f80` adds
  `ai_chat_session` + `ai_chat_message` tables. Orchestrator at
  `services/ai/chat.py` runs the iterative tool-calling loop:
  selects highest-priority enabled provider, sends the message
  history + available tools, streams chunks back, dispatches tool
  calls, appends results to the history, repeats until the model
  emits a non-tool-call response. SSE endpoint at `POST
  /api/v1/ai/chat/{session_id}/messages` streams chunks to the
  frontend with `text-delta` / `tool-call-delta` / `tool-result`
  events.
- **Operator Copilot — floating chat drawer (#90 Wave 3b).**
  Right-side slide-in drawer (`ChatDrawer.tsx`) keyed on a global
  toggle. Streams responses live via EventSource, renders Markdown
  + code blocks + tool-call collapsed-by-default cards. Empty
  state shows clickable example prompts that auto-start the chat.
  SSE endpoint sets `X-Accel-Buffering: no` so nginx doesn't
  buffer the stream.
- **Operator Copilot — token / cost observability + per-user daily
  caps (#90 Wave 4a).** Migration `c8e3a7f10b54` adds
  `ai_usage_event` table tracking input + output token counts,
  computed cost per pricing table, model + provider, request kind
  (chat / mcp), and user. `services/ai/pricing.py` ships a curated
  pricing table for the major hosted models (gpt-4o / gpt-4o-mini
  / claude-3-5-sonnet / claude-3-5-haiku / gemini-1.5-pro /
  gemini-1.5-flash / common Ollama models) with cost-per-Mtoken
  rates. Per-user daily cap (`AIChatSetting.daily_token_cap_per_user`)
  enforced in the orchestrator.
- **Operator Copilot — chat drawer usage chip + platform-insights
  AI usage card (#90 Wave 4b).** Live token chip in the drawer
  header shows today's usage / daily cap. New "AI usage" card on
  Platform Insights aggregates the last 7 days by provider + model
  with a stacked bar chart (Recharts).
- **Operator Copilot — Anthropic (Claude) driver (#90 Phase 2).**
  Driver translates the neutral request shape into Anthropic's
  Messages API format — system prompt as a top-level field
  (vs. system role in OpenAI), tool-use blocks vs. tool-calls.
  Streaming via `messages.stream` event types.
- **Operator Copilot — Azure OpenAI + Google Gemini drivers
  (#90 Phase 2).** Azure OpenAI driver adapts the existing
  OpenAI-compat shape to Azure's per-deployment URL pattern
  (`https://{resource}.openai.azure.com/openai/deployments/{deploy}`
  + `?api-version=` query param). Google Gemini driver translates
  to Gemini's `generateContent` API and reassembles streamed
  function calls.
- **Operator Copilot — failover chain across enabled providers
  (#90 Phase 2).** Orchestrator now walks providers in priority
  order on transient failures (5xx / timeout / rate-limit) — first
  successful chunk wins. Permanent errors (4xx / auth) surface
  immediately. Failover events recorded in `ai_usage_event` with
  `request_kind="failover"`.
- **Operator Copilot — "Ask AI about this" affordances
  (#90 Phase 2).** Compact icon button on resource detail
  contexts that pre-fills the chat drawer with a templated prompt
  and the resource UUID for tool calls. Wired across subnets / IP
  rows / DNS zones / DNS records / DHCP scopes / leases / alerts
  / audit rows / network devices.
- **Operator Copilot — custom prompts library (#90 Phase 2).**
  New `ai_custom_prompt` table — operator-curated prompt templates
  stored per-org. Surfaces in the chat drawer as a "Prompts ▾"
  dropdown above the input. Built-in starter pack (Find unused
  IPs, Audit recent changes, Summarize subnet utilization, Triage
  open alerts).
- **Operator Copilot — Cmd-K palette "Ask AI" entry
  (#90 Phase 2).** Cmd-K (Ctrl-K on Linux/Windows) opens a global
  palette; "Ask AI" is the top entry and pre-fills the prompt
  with the current page's context. Fixed a shortcut conflict
  with the existing search hotkey.
- **Operator Copilot — daily digest (#90 Phase 2).** Optional
  daily 0900 local digest — Celery beat fires
  `tasks.ai_daily_digest`, the orchestrator calls a fixed prompt
  ("summarise interesting changes since yesterday: alerts /
  pending IPs / DNS drift / circuits expiring soon"), result lands
  in operator inbox via the existing audit-forward / SMTP /
  webhook channels. Off by default; `AIChatSetting.daily_digest_enabled`
  toggles it on per platform.
- **Operator Copilot — write tools with preview / apply flow
  (#90 Phase 2).** Two-phase write contract: model proposes via
  `propose_*` tool variants (returns the planned diff in a
  `proposed_change` envelope), operator reviews + clicks "Apply"
  in the chat drawer, frontend sends an `apply_change` follow-up
  that hits the real CRUD endpoint. Three pilot tools:
  `propose_create_ip`, `propose_update_ip_status`,
  `propose_create_dns_record`. Audit log captures both the
  proposal and the apply.
- **Operator Copilot — richer dynamic context in system prompt
  (#90 Phase 2).** System prompt now interpolates platform stats
  (subnet count, alert count, recent audit summary), the operator's
  role + scoped permissions, and "today's interesting things"
  (services with terms expiring < 30 d, alerts opened in the last
  hour, deviced last seen > 7 d ago).
- **TOTP MFA for local users (#69).** New `user_mfa_secret` table
  with Fernet-encrypted TOTP shared secret + backup-codes JSONB
  list. Enrolment flow: Settings → Security → "Enable MFA" →
  scan QR (`pyotp` + `qrcode` libraries) → enter 6-digit code to
  confirm → backup codes shown once. Login flow gains a second
  step when MFA is enabled — JWT pre-token issued on
  username+password, exchanged for full token after TOTP code or
  backup code accepted. Backup codes are single-use and persisted
  hashed. Admin can force-disable MFA per user (audit-logged).
  Migration `f8d4e29b1c75_user_modified_at` (rename) +
  `c4e7d28f1059_totp_mfa`.
- **API-token scopes (#74).** `api_token` rows gain a `scopes`
  JSONB column listing the resource_types the token is allowed to
  touch (vs. inheriting all of the user's permissions). Scope set
  is permission-name granularity (`subnet:read`, `subnet:admin`,
  `*` for full inheritance). Token create modal lets the operator
  pick scopes via a chip selector grouped by resource family.
  Authorization enforces scope intersection: token can do at most
  what the scope set allows AND what the user has permission for.
- **Subnet classification tags (#75).** `subnet.pci_scope`,
  `subnet.hipaa_scope`, `subnet.internet_facing`,
  `subnet.contains_pii` boolean columns. List filters across the
  IPAM page + the API. Compliance card on Platform Insights shows
  rolled-up counts. Tags inherit through the IP block tree (set on
  a parent block → all descendant subnets get the tag) with an
  explicit override toggle.
- **#26 IPAM template classes — reusable stamp templates with
  child layouts.** Migration `f9c1a7e25b83`. `ipam_template`
  captures default tags / custom-fields / DNS / DHCP / DDNS
  settings (plus optional sub-subnet `child_layout`) and stamps
  them onto blocks or subnets at apply time. `applies_to` locks
  each template to one of the two carriers. `force=False` fills
  only empty / null target columns;`force=True` overwrites and is
  the path `/reapply-all` uses to refresh drift across every
  recorded instance (cap 200). `IPBlockCreate.template_id` /
  `SubnetCreate.template_id` add optional pre-fill on the create
  paths; carrier rows now carry an `applied_template_id` SET-NULL
  FK so a "reapply across instances" sweep can find every row
  touched. `/admin/ipam/templates` page (list + tabbed editor:
  General / Tags + CFs + DNS-DHCP / DDNS / Child layout). New
  `manage_ipam_templates` permission + IPAM Editor seed.
- **#27 block move across IP spaces.** New `POST
  /ipam/blocks/{id}/move` accepts a target `space_id` + a
  typed-name confirmation. Pre-flight validates: target space
  exists, no CIDR overlap in the target tree, every dependent row
  (DNS records, DHCP scopes, addresses with custom-field
  inheritance) survives the move. `MoveBlockModal` walks the
  operator through the consequences with a chevron-revealed list
  of affected resources before the typed-name confirm unlocks
  Move.
- **#25 split-horizon DNS publishing at the IPAM layer.**
  `IPBlock.dns_split_horizon` boolean + the existing
  `dns_inherit_settings` walk. When set, descendant subnets
  publish records to `dns_zone_id` (internal) AND every entry in
  `dns_additional_zone_ids` (DMZ / external). Per-record routing
  is decided by the new `IPAddress.dns_zone_overrides` JSONB list
  (`[{zone_id, record_type}]`) so an operator can pin one address
  to publish only into the internal zone. Auto-sync task respects
  the split.
- **#51 DHCP — PXE / iPXE provisioning profiles.** New
  `pxe_profile` table — operator-curated profiles per
  architecture (`bios_x86` / `efi_x86_64` / `efi_arm64` /
  `efi_x86`). Each profile binds to a TFTP `next-server` + a
  `boot-filename` per arch, plus an optional iPXE script body.
  `DHCPScope.pxe_profile_id` SET-NULL FK; on render, the Kea
  driver emits one `client-class` per arch-match guarded by
  `option dhcp.user-class` matching the iPXE signature so legacy
  PXE clients see the BIOS bootfile and iPXE clients see the iPXE
  script. New `/dhcp/groups/{id}/pxe` admin page with profile CRUD
  and a per-scope "PXE profile" picker on the scope editor.
- **#91 Customer / Site / Provider logical ownership entities.**
  Three first-class rows that cross-cut IPAM / DNS / DHCP /
  Network so operators can answer "who owns this?", "what's at
  NYC?", and "which circuits does Cogent supply us?" without
  resorting to free-form tags. `Customer` is soft-deletable;
  `Site` is hierarchical (`parent_site_id`) with a unique-per-
  parent `code` (NULLS NOT DISTINCT for top-level deduping);
  `Provider` carries an optional `default_asn_id` FK. Cross-
  reference columns added on `subnet` / `ip_block` / `ip_space` /
  `vrf` / `dns_zone` / `asn` / `network_device` / `domain` /
  `circuit` / `network_service` / `overlay_network` with
  `ON DELETE SET NULL` so a customer/site/provider deletion never
  cascades into core IPAM / DNS / DHCP rows. Three new admin
  pages (Customers / Sites / Providers) with bulk-action tables +
  draggable modals. Shared `CustomerPicker` / `SitePicker` /
  `ProviderPicker` + matching Chip components plug into every
  IPAM / DNS / circuit / overlay create / edit modal. RBAC seeded
  into Network Editor + IPAM Editor. Migration
  `c2a7e4f81b69_logical_ownership_entities`.
- **#93 WAN circuits + transport classes.** New `circuit` table —
  carrier-supplied logical pipe (the contract + transport class +
  bandwidth + endpoints + term + cost), distinct from the
  equipment lighting it up. `provider_id` is `ON DELETE RESTRICT`
  (carrier relationship too load-bearing to silently null);
  `customer_id` and the four endpoint refs (a/z-end site +
  subnet) are `ON DELETE SET NULL`. Nine transport classes (mpls
  / internet_broadband / fiber_direct / wavelength / lte /
  satellite + three cloud cross-connects: direct_connect_aws /
  express_route_azure / interconnect_gcp). Soft-deletable so
  `status='decom'` is the operator-visible end-of-life flag while
  the row stays restorable. CRUD under `/api/v1/circuits` with
  filters (provider_id / customer_id / site_id matching either
  end / subnet_id / transport_class / status / expiring_within_days
  / search) and `/by-site/{site_id}` convenience endpoint. New
  `/network/circuits` page with bulk-action table + tabbed editor
  modal (General / Endpoints / Term + cost / Notes) + colour-
  coded term-end badge + asymmetric bandwidth display. Migration
  `d9f3b21e8c54_wan_circuits`.
- **#93 alert rules — `circuit_term_expiring` +
  `circuit_status_changed`.** First mirrors `domain_expiring`
  (severity escalation per `threshold/4` / `threshold/12`).
  Second is transition-style: router stamps `previous_status` +
  `last_status_change_at` on every status update; evaluator keys
  events on `last_status_change_at` and latches `(from, to,
  changed_at)` into `last_observed_value` so a single transition
  fires exactly one event, auto-resolved after 7 d. Routine
  `active` ↔ `pending` flips during commissioning are
  intentionally excluded — only `suspended` / `decom`
  transitions surface.
- **#94 service catalog — `network_service` + polymorphic
  resources + L3VPN summary.** First-class customer-deliverable
  bundle. `NetworkService` is one row per thing the operator
  delivers (`mpls_l3vpn` is the v1 concrete kind, `custom` is
  catch-all; `sdwan` lit up alongside #95; future: DIA, hosted
  DNS / DHCP, MPLS L2VPN, VPLS, EVPN). `NetworkServiceResource`
  is the polymorphic m2m that binds to VRF / Subnet / IPBlock /
  DNSZone / DHCPScope / Circuit / Site / OverlayNetwork. Hard
  rule: `mpls_l3vpn` services may have at most one VRF attached
  (422 on second VRF, 422 on kind-flip-to-L3VPN if >1 VRF
  already linked). Soft rules surfaced as warnings on
  `GET /summary`: missing VRF, fewer than 2 edge sites, edge
  subnet's enclosing block in a different VRF than the service.
  Endpoints: standard CRUD + bulk-delete, `POST/DELETE
  /{id}/resources` for attach / detach, `GET /{id}/summary`
  with kind-aware shape (L3VPN view returns canonical
  VRF + edge sites + edge circuits + edge subnets + warnings),
  `GET /by-resource/{kind}/{id}` reverse lookup powering the
  upcoming "show services using this resource" entry points
  (#99). New `/network/services` page (bulk-action table) +
  tabbed editor modal (General / Resources / Term + cost / Notes
  / Summary) with per-kind resource pickers (cross-group
  fan-out for DNS zones + DHCP scopes). Migration
  `e1d8c92a4f73_network_service_catalog`. RBAC into Network
  Editor + IPAM Editor.
- **#94 alert rules — `service_term_expiring` +
  `service_resource_orphaned`.** First mirrors
  `circuit_term_expiring`. Second is sweep-style: walks every
  active service's join rows and surfaces any link whose target
  row no longer exists or is soft-deleted. Subject is the join
  row's PK so detaching the orphan resolves the alert via the
  standard "subject no longer matches" branch in `evaluate_all`.
  Migration `f2c8d49a1e76` widens `alert_event.subject_type`
  from VARCHAR(20) → VARCHAR(40) to fit `network_service_resource`.
- **#95 SD-WAN overlay — overlays + routing policies + apps +
  topology + simulate.** Vendor-neutral source of truth for
  overlay topology + routing-policy intent. Four new tables
  landing together: `overlay_network` (soft-deletable; six kinds
  — sdwan / ipsec_mesh / wireguard_mesh / dmvpn / vxlan_evpn /
  gre_mesh; free-form vendor + encryption_profile so non-curated
  vendors plug in without enum migration), `overlay_site` (m2m
  binding sites with role hub / spoke / transit / gateway, edge
  device, loopback subnet, ordered `preferred_circuits` jsonb —
  first wins, fall through on outage), `routing_policy`
  (declarative per-overlay policy with priority + match-kind +
  match-value + action + action-target + enabled), and
  `application_category` (curated SaaS catalog used by
  `match_kind=application`, seeded at startup with 33 apps —
  Office365 / Teams / Zoom / Slack / Salesforce / GitHub / AWS /
  Azure / GCP / SIP voice / OpenAI / Anthropic / …). CRUD under
  `/api/v1/overlays` (with sites + policies sub-resources) and
  `/api/v1/applications`. `GET /overlays/{id}/topology` returns
  nodes (sites + roles + device + loopback + preferred-circuits)
  + edges (site pairs whose `preferred_circuits` lists overlap —
  `shared_circuits` is the intersection so the UI can colour by
  transport class) + policies. `POST /overlays/{id}/simulate` —
  pure read-only what-if; body specifies `down_circuits`,
  response shows per-site fallback resolution + per-policy
  effective-target with `impacted` flag and human-readable note.
  Three new RBAC resource types (`overlay_network` /
  `routing_policy` / `application_category`) into Network Editor.
  Service-catalog (#94) integration unlocked: `sdwan` added to
  `SERVICE_KINDS_V1`, `overlay_network` lit up as a real attach
  target, `service_resource_orphaned` alert sweep covers deleted
  overlays. New `/network/overlays` list page + detail page at
  `/network/overlays/{id}` with five tabs — Overview / Topology
  (SVG circular layout, role-coloured nodes, transport-coloured
  edges with solid for single-class and dashed for mixed) /
  Sites (table + editor with up/down circuit-reorder) / Policies
  (priority-ordered with up/down reorder + per-kind editors) /
  Simulate (toggle circuits down + see per-site fallback +
  per-policy impact with amber-tinted impacted rows). Migration
  `c4f7e92d3a18_sdwan_overlay`.
- **#96 API docs link — sidebar + header.** Surface the existing
  Swagger UI / ReDoc at `/docs` and `/redoc` from the navigation
  itself instead of expecting operators to know the URL. New
  "API Docs" entry under Help in the sidebar; new external-link
  icon in the header next to the user menu.
- **Tabbed IP Space modals + shared `ModalTabs` helper.** The
  Create / Edit IPSpace modal grew enough fields that a single
  long form was hard to scan. Split into General / DNS Defaults
  / DHCP Defaults / DDNS Defaults / Custom Fields tabs via a new
  shared `ModalTabs` helper at `frontend/src/components/ui/
  modal.tsx` that any modal can opt into.
- **Pinned identity header above tabs on every IPAM modal.** The
  identity row (Name + CIDR / colour swatch / breadcrumbs) now
  pins above the tab bar in Create / Edit modals across IPSpace
  / IPBlock / Subnet so tab switches don't lose context.
- **Network sidebar sub-grouping.** The Network section grew to
  8 entries (Customers / Providers / Services / Sites under
  "Logical"; ASNs / Circuits / Devices / Overlays / VLANs / VRFs
  under "Infrastructure") and got hard to scan flat. Two
  `SubNavLabel` rows split the contents the same way
  Administration handles its 18 items. Same collapse behaviour
  preserved.
- **Administration sidebar split.** Identity / Platform /
  Auditing / Tools dividers added so the 18 admin items aren't
  one flat scroll.
- **Demo seeder coverage.** `scripts/seed_demo.py` extended to
  cover every shipped entity (customers / sites / providers /
  circuits / services / overlays + routing policies +
  application catalog / VRFs / ASNs + RPKI / domains) so
  `make seed-demo` produces a realistic dataset for a fresh
  install. README updated to mention it.

### Changed

- **#90 Phase 1 — multiple polish landings.** SSE chat streaming
  disabled nginx buffering on `/api/v1/ai/chat` so chunks flush
  to the browser without 5–10 s batched delays. Chat drawer
  optimistically renders the user's just-sent message
  (previously the message popped in only when the assistant
  reply started). Empty-state example prompts in the chat
  drawer made clickable so first-time users have a single-click
  path to a working chat.
- **AI Providers + IPAM Templates pages — admin-page overflow
  fixes.** Both pages now wrap in `h-full overflow-auto p-6`
  matching the rest of the admin surface; AI Providers + IPAM
  Templates pages also picked up the narrow-viewport overflow
  rules from the admin-page memory (flex-wrap header,
  `min-w-0/flex-1/shrink-0`, `break-all` on URL / UUID cells,
  wide modals for tabs + 2-column grids).
- **Domains list — chevron expander column dropped.** The per-row
  expander never carried information that wasn't in the row
  itself; removing it widens the actually-useful columns
  (registrar, expiry, NS state).

### Fixed

- **`#90` Phase 2 follow-ups — CI failures + 4 CodeQL alerts.**
  Mostly minor: missing imports surfaced when the AI tool
  registry was lifted out of Phase 1 wave 2; CodeQL flagged
  three uncontrolled-format-string false positives in the system-
  prompt builder that became real risks once we started
  interpolating tenant-supplied strings into the prompt — fixed
  with explicit `str.format(safe_field=…)` calls. Fourth alert
  was an unused parameter in `_load_proposed_change` flagged by
  the tighter Phase 2 ruff config.
- **"Ask AI" button visual weight.** The first iteration used
  muted-gray styling and looked greyed-out next to the active
  HeaderButton family. Bumped to match HeaderButton's normal
  weight + added a visible "Ask AI" label next to the icon.
- **Black reformat on `alerts.py`.** Drift carried through from
  the #93 alert-types commit; clean-up.

---

## 2026.05.03-1 — 2026-05-03

Network-layer release. Closes the four-issue umbrella roadmap
(#84–#87) plus two follow-ups (#88, #89): the standalone "VLANs"
+ "Network" sidebar entries get rolled into a new **Network**
section that groups Devices / VLANs / VRFs / ASNs, and three
brand-new first-class entities land underneath. **ASNs** become a
real table with RDAP holder refresh (per-RIR routing through the
IANA bootstrap), RPKI ROA pull (Cloudflare or RIPE source) with
expiry tracking, holder-drift detection with a side-by-side diff
viewer, four ASN/RPKI alert rule types, BGP peering relationships
(`peer | customer | provider | sibling`) with directional listing,
and a BGP communities catalog (RFC 1997 / 7611 / 7999 well-knowns
seeded as platform rows + per-AS extensions, large communities per
RFC 8092). **VRFs** replace the freeform `vrf_name` /
`route_distinguisher` / `route_targets` text fields on IPSpace
with a proper relational entity carrying optional `asn_id`, with a
cross-cutting validator that warns (or 422s under
`vrf_strict_rd_validation`) when the ASN portion of an `ASN:N` RD
or RT doesn't match the VRF's linked ASN — the migration backfills
existing freeform values into VRF rows so nothing is lost.
**Domains** track the registry side of a name (registrar, expiry,
nameservers, DNSSEC status) distinct from DNSZone, with RDAP
refresh through a TLD → RDAP-base lookup driven by the IANA
bootstrap registry, four `domain_*` alert rule types
(expiring / NS drift / registrar changed / DNSSEC status changed),
and explicit `dns_zone.domain_id` linkage that follows the
sub-zone tree (so `test.example.com` shows up under
`example.com`'s linked-zones tab). Plus a wave of UX easy-wins:
shared `RdapPanel` that flattens RDAP wire shape into operator-
friendly UI on both ASN and Domain WHOIS tabs, ASN + VRF pickers
on IPSpace / IPBlock modals, the dashboard Platform Health card
moved up next to the KPI ribbon, alphabetised API tag ordering,
and IPAM gap rows (`.11 – .13 · 3 free`) are now clickable to
launch AddAddressModal pre-filled with First / Last / Random
quick-pick buttons over the gap.

### Added

- **Network sidebar section.** New non-clickable "Network" header
  (mirrors the Administration shape) groups Devices / VLANs /
  VRFs / ASNs. Devices replaces the old top-level Network entry;
  VLANs lifts up from its own slot. Routes move to
  `/network/devices`, `/network/vlans`, `/network/vrfs`,
  `/network/asns`; the old `/network` and `/vlans` paths redirect
  so existing bookmarks keep working, and the legacy
  `/network/:id` device-detail URL is preserved alongside the new
  `/network/devices/:id` canonical form. Closes #84.
- **ASN management — first-class entity.** New `asn` table:
  BigInteger `number` to fit the full 32-bit range; `kind`
  (public / private) auto-derived from RFC 6996 + RFC 7300;
  `registry` (RIR — arin / ripe / apnic / lacnic / afrinic) auto-
  derived from a hand-curated IANA delegation snapshot at
  `app/data/asn_registry_delegations.json`; WHOIS columns for the
  RDAP refresh task. Sibling `asn_rpki_roa` table tracks prefix +
  max_length + validity window + trust_anchor + state, ON DELETE
  CASCADE from `asn`. CRUD at `/api/v1/asns` with kind / registry
  / whois_state / search filters, bulk-delete capped at 500,
  audit-logged. New `manage_asns` permission seeded into the
  Network Editor builtin role. List page at `/network/asns` with
  sticky thead, multi-select bulk delete, kind / registry / WHOIS
  filter chips, draggable create / edit modal. Detail page at
  `/network/asns/:id` with WHOIS / RPKI ROAs / Linked IPAM /
  BGP Peering / Communities / Alerts tabs; per-row Refresh WHOIS +
  Refresh RPKI buttons in the header. Migrations
  `f59a5371bdfb_asn_management` + `4a7c8e3d51b9_asn_phase2`. Refs
  #85.
- **ASN — RDAP holder refresh.** `app/services/rdap_asn.py`
  derives the RIR via the existing `derive_registry()` classifier
  and queries the RIR's RDAP base directly (`rdap.arin.net`,
  `rdap.db.ripe.net`, `rdap.apnic.net`, `rdap.lacnic.net`,
  `rdap.afrinic.net`) — `rdap.iana.org/autnum/<n>` is a bootstrap
  registry, not a query proxy, and returns HTTP 501 for every
  real query, so the routing-by-RIR layer is mandatory.
  `app/tasks/asn_whois_refresh.refresh_due_asns` ticks hourly,
  walks every `asn` row whose `next_check_at` has elapsed, parses
  holder + last-modified out of the response, derives
  `whois_state` (`ok` / `drift` / `unreachable` / `n/a`), and
  audit-logs every state transition.
  `POST /api/v1/asns/{id}/refresh-whois` drives the same code
  path synchronously for the operator. Operator-tunable cadence
  via new `PlatformSettings.asn_whois_interval_hours` (default
  24, range 1–168). Settings → Network → ASN Refresh surfaces
  the knob.
- **ASN — RPKI ROA pull.** `app/services/rpki_roa.py` fetches the
  global ROA dump from Cloudflare (`rpki.cloudflare.com/rpki.json`
  ~80 MB JSON, ~850k ROAs) or RIPE NCC's validator JSON, filters
  by AS number, and caches the multi-MB payload in-memory for 5
  min via a `_get_cached_roas` so a beat sweep refreshing 50 ASNs
  makes a single HTTP call instead of 50.
  `app/tasks/rpki_roa_refresh.refresh_due_roas` ticks hourly,
  reconciles `asn_rpki_roa` rows additively + with deletes,
  derives state (`valid` / `expiring_soon` / `expired` /
  `not_found`) off `valid_to`, and audit-logs adds / removes /
  state transitions. `valid_from` and `valid_to` parsing accept
  Cloudflare's `expires` (Unix epoch) and RIPE's `notBefore` /
  `notAfter` (ISO 8601) on the same row.
  `POST /api/v1/asns/{id}/refresh-rpki` reuses `_refresh_one_asn`
  for the synchronous per-AS button — same reconcile shape (added
  / updated / removed / transitions) as the hourly beat tick.
  Two new `PlatformSettings` knobs: `rpki_roa_source`
  (cloudflare | ripe) and `rpki_roa_refresh_interval_hours`
  (default 4, range 1–168), surfaced through Settings → Network →
  ASN Refresh.
- **ASN — alert rules.** Four new rule types wired into
  `services.alerts`: `asn_holder_drift` (single-event latch via
  `alert_event.last_observed_value` JSONB so a single flip fires
  exactly one event, auto-resolves after 7 d),
  `asn_whois_unreachable`, `rpki_roa_expiring` (severity
  escalation at threshold/4 + threshold/12 around the operator-
  set `threshold_days`, default 30 d), and `rpki_roa_expired`.
  Frontend AlertsPage type-picker and AlertRuleType union
  extended.
- **ASN — holder-drift diff viewer.** `asn_whois_refresh` now
  persists `previous_holder` into `whois_data` on every successful
  RDAP refresh — drift or not — so the detail page can render a
  side-by-side without consulting the audit log. WHOIS tab on ASN
  detail renders a rose-tinted diff card when
  `whois_state === "drift"`: previous holder vs current holder
  plus the timestamp drift was detected.
- **ASN — BGP peering relationships (#89).** New `bgp_peering`
  table — operator-curated graph of BGP relationships between
  tracked ASNs (`peer | customer | provider | sibling`). Both
  endpoints are FK ON DELETE CASCADE because a peering row is
  meaningless once one endpoint is gone. Unique on
  `(local, peer, relationship_type)`. Column named
  `relationship_type` (not `relationship`) so it doesn't shadow
  the imported `sqlalchemy.relationship` function in the model
  body. New `router.local_asn_id` FK ON DELETE SET NULL stamps
  which AS a router originates routes from. CRUD endpoints under
  `/api/v1/asns/peerings` (router-level `manage_asns` gate
  inherited). New `PeeringsTab` on the ASN detail page with a
  directional listing (`→ outbound` / `← inbound` from this AS's
  POV) and clickable counter-AS that links to the peer's detail
  page. `PeeringFormModal` lets operators pick the counterparty
  (filtered to exclude self), pick whether "this AS is the local
  side" or "the counterparty is the local side" (modal normalises
  to canonical `(local, peer, relationship)` shape on submit),
  pick the relationship with inline copy explaining each, plus a
  free-form description. Edit limits the editable fields to
  relationship + description (the (local, peer) pair is the row's
  natural key). Migration `d3f2a51c8e76_bgp_peering`.
- **ASN — BGP communities catalog (#88).** New `bgp_community`
  table; `asn_id` is nullable so platform-level rows (RFC 1997 /
  7611 / 7999 well-knowns) can be shared across all ASes. `kind`
  denormalises which on-the-wire shape `value` carries:
  `standard` / `regular` (`ASN:N` per RFC 1997) / `large`
  (`ASN:N:M` per RFC 8092). `inbound_action` /
  `outbound_action` capture free-form policy hints.
  `app.services.bgp_communities` owns the well-known catalog
  (no-export, no-advertise, no-export-subconfed, local-as,
  graceful-shutdown, blackhole, accept-own) and seeds it on first
  boot via a hook in `main.py`'s lifespan; subsequent boots
  refresh the description text so upgrades that reword a row land
  without an admin edit. CRUD: `GET /asns/communities/standard`
  (read-only catalog), `GET|POST /asns/{asn_id:uuid}/communities`,
  `PATCH|DELETE /asns/communities/{community_id:uuid}`. Format
  validators per kind: standard must be one of the seven seeded
  names; regular matches `\d+:\d+`; large matches
  `\d+:\d+:\d+`. Standard catalog rows refuse PATCH / DELETE
  with a 400 explaining they're platform-owned. New
  `CommunitiesTab` on the ASN detail page with a collapsible
  standard-catalog table at the top with "Use on this AS" buttons
  per row that pre-fill the form, plus the per-AS list grouped by
  kind. Migration `f4a6c8b2e571_bgp_communities`.
- **VRFs as first-class entities (#86).** New `vrf` table carries
  name, description, `asn_id` FK, `route_distinguisher` with
  RD-format validation, split import / export RT lists, tags,
  custom_fields. `ip_space` and `ip_block` both gain a nullable
  `vrf_id` FK ON DELETE SET NULL. Migration backfills new VRF
  rows from every distinct (vrf_name, rd, rt-list) triple on
  existing IPSpace rows and stamps each space's `vrf_id` at the
  matching new row; the freeform columns stay in place for one
  release cycle so operators can verify the mapping landed
  correctly before they get dropped. CRUD at `/api/v1/vrfs`
  (list with asn_id + search filters and pagination, create / get
  / update / delete, bulk-delete with force-detach semantics),
  audit-logged on every mutation. `manage_vrfs` permission seeded
  into the Network Editor builtin role. List page at
  `/network/vrfs`; detail page with linked IP spaces / IP blocks
  tabs and an Edit button (Pencil HeaderButton wired to the
  shared `VRFEditorModal`). Phase 2 lights up the cross-cutting
  RD / RT validation: each `ASN:N` entry whose ASN portion does
  not match `vrf.asn.number` produces a non-blocking warning on
  the response; flipping `PlatformSettings.vrf_strict_rd_validation`
  to true escalates the same mismatch to 422. Second warning
  fires when `vrf.asn_id` is null but the RD is in `ASN:N` form,
  reminding the operator to either link an ASN row or move to
  `IP:N` flavour. IPBlock responses also carry a `vrf_warning`
  field that flags when a block's pinned VRF differs from its
  parent space's VRF — intentional in hub-and-spoke designs but
  worth a heads-up. Migrations `2c4e9d1a7f63_vrf_first_class` +
  `b7e2a4f91d35_vrf_phase2`.
- **VRF picker on IPSpace + IPBlock modals.** The four routing-
  context modals (New / Edit IPSpace + Create / Edit IPBlock)
  use a new `VrfPicker` component bound to the `vrf_id` FK. The
  freeform `vrf_name` / `route_distinguisher` / `route_targets`
  text inputs are gone from the IPSpace form — RD + import /
  export RTs live on the VRF row now and are surfaced read-only
  via the picker label. Backend `vrf_id` added to `IPSpaceCreate`,
  `IPSpaceUpdate`, `IPBlockCreate`, `IPBlockUpdate`. Space-detail
  header shows the linked VRF's name + RD + import / export RTs
  (resolved against the cached VRF list) instead of the
  deprecated freeform fields. Legacy rows that still have
  `space.vrf_name` set without `vrf_id` get a "(legacy)" suffix
  and an in-line nudge to migrate.
- **Domain registration tracking (#87).** Distinct from DNSZone —
  tracks the registry side of a name (registrar, registrant,
  expiry, the nameservers the registry advertises) versus the
  records SpatiumDDI serves. New `domain` table with the spec'd
  fields, an httpx-based RDAP client at `app.services.rdap` (10 s
  per-call / 15 s total budget). The TLD → RDAP-base lookup is
  driven by the IANA bootstrap registry at
  `data.iana.org/rdap/dns.json`, cached in-process for 6 h with
  an asyncio lock against thundering-herd refetch + a stale-cache
  fallback if the bootstrap fetch fails — `rdap.iana.org/domain/<n>`
  returns 404 for any non-test domain (only `example.net` etc.
  happen to work), so per-TLD routing is mandatory. CRUD +
  synchronous `POST /domains/{id}/refresh-whois` endpoints under
  the new `manage_domains` permission gate. Refresh writes the
  parsed fields back, recomputes `nameserver_drift` against the
  operator-pinned expected list, and stamps `whois_state` via the
  pure `derive_whois_state` decision tree (unreachable → expired
  → expiring < 30 d → drift → ok). Beat-fired
  `app.tasks.domain_whois_refresh.refresh_due_domains` ticks
  hourly, gates per-row on `Domain.next_check_at`, and self-paces
  via the new `PlatformSettings.domain_whois_interval_hours` knob
  (default 24 h, 1–168 h range). Detail page at
  `/admin/domains/:id` with registration card, expected-vs-actual
  NS diff panel with drift badge, raw WHOIS / Linked DNS Zones /
  Alert History tabs. List page at `/admin/domains` with a sticky
  table, expiry countdown badges (green > 90 d / amber 30–90 d /
  red < 30 d / dark-red expired), per-row Refresh + Edit + Delete,
  multi-select bulk refresh / bulk delete. Domains nav lives in
  the core sidebar (between DNS Pools and Logs) — registration
  tracking is core operational data, not platform admin.
  Migrations `3124d540d74f_domain_registration` +
  `4a9e7c2d18b3_domain_phase2`.
- **Domain — alert rules.** Four new rule types: `domain_expiring`
  (severity escalation at threshold/4 + threshold/12 around the
  operator-set `threshold_days`, default 30 d),
  `domain_nameserver_drift`, `domain_registrar_changed`,
  `domain_dnssec_status_changed`. The two transition-once rules
  latch the observed value into `alert_event.last_observed_value`
  JSONB so a single flip fires exactly one event, auto-resolves
  after 7 d. `alert_rule.threshold_days` is the new params column.
  Frontend AlertsPage exposes the new types in a grouped picker
  with rule-type-specific form fields and help text.
- **DNSZone ↔ Domain explicit linkage.** New
  `dns_zone.domain_id` nullable FK ON DELETE SET NULL. Picker on
  the DNS zone create / edit modal — "Auto-match by zone name"
  remains the default for backward-compat. Domain detail page's
  "Linked DNS Zones" tab prefers the explicit FK and falls back
  to a left-anchored suffix match (`zone === domain || zone.
  endsWith("." + domain)`) when `domain_id` is unset, so child
  zones inherit (`test.example.com` shows up under `example.com`)
  but `example.com.au` correctly does NOT match `example.com`.
  Sub-zones get a small "sub-zone" badge so the operator can tell
  parent vs descendant at a glance. Migration
  `e7b8c4f96a12_dns_zone_domain_fk`.
- **BGP FK on IPSpace / IPBlock.** New optional `asn_id` UUID
  column on both, FK to `asn.id` ON DELETE SET NULL, indexed.
  Schema surfaces it on Create / Update / Response so the API is
  ready, with a new shared `AsnPicker` component at
  `components/ipam/asn-picker.tsx` wired into the New IPSpace,
  Edit IPSpace, Create IPBlock, and Edit IPBlock modals as an
  optional "Origin ASN (BGP)" field. Migration
  `c9f1e47d2a83_bgp_asn_fk`.
- **Dashboard — three new network summary cards under the KPI
  row.** ASNs (public / private count + WHOIS health + ROA expiry
  warnings), VRFs (count + missing-RD / unlinked-ASN warnings),
  Domains (count + expiry buckets + NS-drift indicator). Platform
  Health card moved up to immediately below the KPI ribbon,
  where the colour-coded ribbon belongs.
- **Settings → Network section.** Three new sections: ASN Refresh
  (asn_whois_interval_hours, rpki_roa_source,
  rpki_roa_refresh_interval_hours), Domain Refresh
  (domain_whois_interval_hours), and VRF Validation
  (vrf_strict_rd_validation toggle). Backs the platform_settings
  columns added by the phase 2 migrations.
- **Shared `RdapPanel` for WHOIS rendering.** New component that
  flattens the wire shape into operator-friendly UI: handle / name
  / DNSSEC / port43 headlines, status flags, nameserver chip list
  (domain side), event timeline, and entities flattened from
  nested vCard arrays into per-role org / email / phone / address
  blocks. Raw JSON is still available behind a "Show raw RDAP
  JSON" toggle for ops debugging. Wired into both the ASN detail
  WHOIS tab and the Domain detail WHOIS tab.
- **DNS sub-zone shortcut.** New "Sub-zone" header button on the
  zone detail page pre-fills the New Zone modal with `.<parent>`
  so the operator just types the leading label. Saves a
  back-trip to the group level.
- **IPAM gap-row click → AddAddressModal.** The
  `192.168.0.112 – 192.168.0.120 · 9 free` rows that interleave
  the IP table are now clickable. Click → AddAddressModal opens
  locked to manual mode with the range banner shown above the IP
  input plus First / Last / Random quick-pick buttons.
- **API tag ordering.** `/api/v1` router includes are now
  alphabetised by tag name so the ReDoc / Swagger UI lists
  sections A → Z. Comment at the top reminds future contributors
  to insert in sort order.

### Changed

- **Sticky table headers.** RPKI ROAs / Communities / Peerings
  tables had `sticky top-0` thead rows with `bg-muted/30` (30%
  opaque) so scrolled rows showed through and the headers looked
  visually merged with the data. Switched to `bg-card` (fully
  opaque) plus a `shadow-[inset_0_-1px_0]` trick for the bottom
  divider so the rule stays attached to the sticky header
  instead of getting clipped against the scroll edge.
- **CLAUDE.md roadmap trimmed to GitHub issue links.** Each
  pending roadmap entry (Major roadmap items, Integration
  roadmap, Future ideas — categorised) is now a single-line
  markdown link to the GitHub issue that holds the full design
  body. Section headings, intro paragraphs, and h4 subsection
  headings are preserved so the categorical browse view still
  works — only the multi-paragraph item bodies move out. CLAUDE.md
  drops from 1029 to 427 lines (~58% smaller in the roadmap
  region); the canonical design context lives on GitHub where it
  can be assigned, commented on, milestoned, and linked from PRs.

### Fixed

- **RDAP lookups silently broken end-to-end.** ASN side:
  `rdap.iana.org/autnum/<n>` returns HTTP 501 Not Implemented for
  every real query — IANA's RDAP service is a bootstrap registry,
  not a query proxy. Switched `app.services.rdap_asn` to derive
  the RIR via `derive_registry()` and query the RIR's RDAP base
  directly. Domain side: same story — `rdap.iana.org/domain/<n>`
  returns 404 for any non-test domain. Added a TLD → RDAP-base
  lookup driven by the IANA bootstrap registry at
  `data.iana.org/rdap/dns.json`. Routes `.net` →
  `rdap.verisign.com/net/v1/`, `.com` → same, etc.
- **RPKI ROA pull was permanently locked out for fresh ASNs.**
  `ASN.next_check_at` is owned by the WHOIS refresh task — it
  bumps the column ~24 h forward on every successful RDAP pull.
  The RPKI ROA refresh task was *also* gating its first-time-pull
  SELECT on that same column, which meant the first WHOIS refresh
  after an ASN was created (typically within minutes) would push
  `next_check_at` past `now()`, and the RPKI sweep would never
  see the ASN as eligible. Net effect: zero ROAs ever landed for
  any ASN that had at least one WHOIS refresh — which is every
  public ASN. Dropped the gate on the first-time SELECT entirely;
  the source-side service caches the global ROA dump in-memory
  for 5 min so back-to-back sweeps don't fan out to N network
  calls.
- **RPKI ROA validity windows were always empty.** The service
  docstring said the public mirrors don't surface `valid_from` /
  `valid_to`, but Cloudflare's `rpki.json` actually ships
  `expires` (Unix epoch) on every ROA and RIPE's validator emits
  `notBefore` / `notAfter` (ISO 8601). Added `_parse_validity`
  that accepts either shape; `valid_to` now lands on every row,
  `valid_from` lands on RIPE rows.
- **`GET /asns/{id}/rpki-roas` route never existed.** Frontend
  client was calling the endpoint since the wave 2 detail-page
  landed but the backend never implemented it. Every call 404'd
  silently and the React Query result stayed empty. Added the
  route alongside `refresh-rpki` (same router, same gate). Also
  aligned `ASNRpkiRoaState` frontend union to what the task
  actually emits (`valid | expiring_soon | expired | not_found`).
- **`GET /asns/peerings` was 422'ing.** Earlier route registration
  order put `GET /{asn_id}` before `GET /peerings`, so `peerings`
  was being fed to the UUID coercer and rejected. Constrained
  every `{asn_id}` path to Starlette's `:uuid` converter so
  non-UUID strings fall through to the literal `/peerings` and
  `/bulk-delete` matches further down.
- **`prefix` came back as `IPv4Network` not `str`.** asyncpg
  returns CIDR columns as `ipaddress.IPv4Network` / `IPv6Network`
  instances; Pydantic's `str` field type refused to coerce them
  and 500'd every list call. Added a `mode="before"`
  field_validator that round-trips through `str(...)`.
- **Raw RDAP payload rendered as "No raw WHOIS data".** Frontend
  was checking `typeof asn.whois_data?.raw === "string"` but the
  RIRs all serve JSON, so the refresh task stores `raw` as a
  nested object. Switched to a defensive
  `JSON.stringify(raw, null, 2)` pretty-print that also still
  accepts string `raw` values from older snapshots (no migration
  needed). Heading retitled "Raw RDAP response".
- **VRF migration's source-row scan crash.** `route_targets`
  filter was `jsonb_array_length(route_targets) > 0`, which throws
  `cannot get array length of a scalar` on existing rows whose
  `route_targets` JSONB happens to be a string rather than an
  array. Added a `jsonb_typeof(route_targets) = 'array'` guard so
  non-array values just don't match the filter (no VRF row is
  created for them).
- **VRF detail page Edit button.** Hoisted `VRFEditorModal` to
  `export` from `VRFsPage.tsx` and wired a Pencil HeaderButton in
  the `VRFDetailPage` header.

---

## 2026.04.30-1 — 2026-04-30

Notifications-and-automation release. The headline work closes
out the Notifications & external integrations bucket on the
roadmap: SMTP email delivery for the alerts framework + audit
forward, Slack / Teams / Discord chat-channel webhook flavors
that render mrkdwn / MessageCard / embed bodies natively, and a
new typed-event webhook surface — 96 curated events
(`subnet.created`, `dns.zone.updated`, `ip.allocated`, …)
delivered with HMAC-SHA256 signatures via an outbox-backed retry
queue (exponential backoff 2 / 4 / 8 … 600 s, dead-letter on
permanent failure, manual retry from the UI). Bundled with two
DNS deliverables (GSLB pools — priority + weight + health-checked
A/AAAA record sets that auto-render rendered-record sets the
BIND9 driver applies, and a server-detail modal with
logs / stats / config tabs) plus three IPAM deliverables (device
profiling — passive DHCP fingerprinting + active auto-nmap on
fresh leases, IPAM bulk allocate — contiguous IP range stamping
with name templates, and the post-bulk-allocate IPAM table polish
wave: sticky thead, shift-click range select, dashed gap-marker
rows for missing IPs between contiguous allocations, plus a
revamped subnet Tools dropdown). Plus a DNS pool reconciliation
fix (member IP edits + removed-member cleanup + zone-detail
refresh).

### Added

- **Typed-event webhooks** (Phase 2 of notifications-and-
  external-integrations). New `/admin/webhooks` admin surface
  + `POST/GET/PUT/DELETE /api/v1/webhooks` CRUD. 96 typed event
  types derived from a `resource_namespace × verb` cross-product
  (e.g. `space.created`, `subnet.bulk_allocate`,
  `dns.zone.updated`, `dhcp.scope.deleted`, `auth.user.created`,
  `integration.kubernetes.created`); subscribers with empty
  `event_types` match everything. SQLAlchemy
  `after_flush` + `after_commit` listeners snapshot committed
  `AuditLog` rows and write one `EventOutbox` row per matching
  `EventSubscription`. Celery beat (`event-outbox-drain`, 10 s)
  drains the outbox via `SELECT … FOR UPDATE SKIP LOCKED`, signs
  each POST with `hmac(secret, ts + "." + body, sha256)`, and
  retries with exponential backoff (2 / 4 / 8 … 600 s capped) up
  to `max_attempts` (default 8 ≈ 8.5 min cumulative). Permanent
  failures flip to `state="dead"` for operator review. Reserved
  `X-SpatiumDDI-*` headers (Event / Delivery / Timestamp /
  Signature) protected from operator override. Migration
  `0f83a227b16d_event_subscription_outbox_tables`.
- **Webhook admin UI.** Subscriptions page with one-time secret
  reveal modal on create (auto-generated 32-byte hex unless an
  operator supplies their own), event-type multi-select with
  filter box, custom-headers editor, timeout + max-attempts
  inputs. Per-row test button synthesizes a `test.ping` event
  through the live pipeline with an inline success / failure
  flash. Per-row expandable deliveries panel auto-refreshes
  every 8 s and shows state / attempts / last-status / next-
  retry; **Retry now** on failed/dead rows resets attempts and
  re-queues. Edit form supports secret rotation toggle.
- **SMTP delivery for alerts + audit forward** (Phase 1 of
  notifications-and-external-integrations). New SMTP target
  type with host / port / username / encrypted password / TLS
  mode (`starttls` / `ssl` / `none`) / from-address / to-list
  fields. stdlib `smtplib` driven through `asyncio.to_thread`
  (no extra dep). Subject + body rendered from the audit row
  (or the alert event for the alerts framework). Audit-forward
  targets gain `kind="smtp"`; alert rules gain a `notify_smtp`
  toggle alongside the existing syslog + webhook channels.
  Migration `30cda233dce9_add_smtp_chat_flavor_to_audit_forward`.
- **Chat-flavored webhooks** — Slack / Teams / Discord. New
  `webhook_flavor` column on `audit_forward_target` selects
  between generic JSON (default), Slack `mrkdwn` block, Teams
  `MessageCard`, and Discord `embed`. Single payload renderer
  per flavor; no extra dep. Configured by pasting the
  platform's incoming-webhook URL into a webhook target.
- **DNS GSLB pools.** New `DNSPool` model — priority + weight +
  health-checked record sets that render to a rotating set of
  A / AAAA records. The pool has members (each with an
  address, weight, health-check policy) and an enabled/disabled
  flag; on each beat tick (`dns_pool_healthcheck.dispatch_due_pools`
  → per-pool `run_pool_check`) the worker runs every member's
  TCP / HTTP(S) / ICMP probe with configurable
  unhealthy / healthy thresholds, transitions states based on
  consecutive successes / failures, and `apply_pool_state`
  reconciles the rendered records — DELETE the rows for
  members that are now unhealthy or removed; CREATE / UPDATE
  for members whose IP changed or who newly went healthy. All
  changes flow through the existing `enqueue_record_op`
  pipeline so the zone serial bumps once per reconciliation
  pass and the agent applies in driver-native order. UI: new
  Pools tab on the zone detail with members CRUD, weight + IP
  edit, and per-member last-check state badge.
- **DNS server detail modal.** Click a server row in the
  group's Servers tab to open a draggable detail modal with
  three tabs — **Logs** (filtered live tail of the agent's
  query log + structured filters: substring / qtype / client
  IP / since), **Stats** (push-driven 5 m / 15 m / 1 h
  rolling agent metrics: queries-per-second, NXDOMAIN rate,
  cache hit ratio when reported), and **Config** (read-only
  rendered server-options snapshot from the latest applied
  ConfigBundle).
- **IPAM device profiling.** Two new sub-systems converging on
  a unified "Device profile" panel inside the read-only IP
  detail modal.
  - **Active auto-nmap on fresh DHCP leases** (Phase 1).
    Subnet-level opt-in (`Subnet.auto_profile_on_lease: bool`,
    default false). On a fresh lease event the agent posts to
    `/api/v1/dhcp/agents/lease-events`; the API enqueues a
    `service_and_os` nmap run against the new IP, capped at 4
    in-flight scans per subnet with a refresh-window dedupe so
    a flapping lease can't fire-hose the scanner. Per-IP
    re-profile-now button (`POST /ipam/addresses/{id}/profile`)
    on the IP detail modal lets operators kick a fresh scan
    on demand.
  - **Passive DHCP fingerprinting** (Phase 2, default off,
    needs `cap_add: NET_RAW`). DHCP agent gains a scapy
    `AsyncSniffer` thread that captures DHCP DISCOVER /
    REQUEST option lists and posts them to a fingerbank lookup
    task. Results land in the IP row's profile panel as
    Type / Class / Manufacturer (e.g.
    `Phone / VoIP / Polycom`).
  - Same-day follow-ups: `setcap cap_net_raw+eip` on
    `/usr/bin/nmap` plus `NMAP_PRIVILEGED=1` so non-root
    operator OS scans actually work (Debian's nmap does an
    early `getuid()==0` check that ignores file caps),
    `securityContext.capabilities.add: [NET_RAW]` on the K8s
    worker + `worker.netRawCapability` Helm gate for
    restricted PSA / OpenShift-SCC / GKE Autopilot, and a
    Settings → IPAM → Device Profiling form for the
    fingerbank API key (Fernet-encrypted at rest; response
    only exposes a boolean `fingerbank_api_key_set`).
- **IPAM bulk allocate.** New `POST /ipam/subnets/{id}/bulk-
  allocate/{preview,commit}` stamps a contiguous IP range plus
  a name template (`{n}` / `{n:03d}` / `{n:x}` /
  `{oct1}`–`{oct4}` octet fragments) in one shot. Per-row
  conflict detection (already-allocated, dynamic-pool overlap,
  FQDN collision) with `on_collision: skip|abort` policy,
  capped at 1024 IPs per call. New `BulkAllocateModal` lives
  under the subnet Tools menu with a three-phase form →
  preview → committed flow and live client-side template
  rendering as the operator types.
- **Nmap subnet sweep + bulk operations.** Two new presets —
  `subnet_sweep` (`-sn` ping-sweep, capped at /16 worth of
  hosts) and `service_and_os` (`-sV -O --version-light`, the
  device-profiling default). CIDR-aware target validation +
  multi-host XML parsing — the runner walks every `<host>`
  element and emits a `hosts[]` summary when more than one
  responds. New `POST /nmap/scans/bulk-delete` (cap 500,
  mixes cancel + delete based on per-row state) and
  `POST /nmap/scans/{id}/stamp-discovered` (claim alive hosts
  as `discovered` IPAM rows + stamp `last_seen_at`;
  integration-owned rows just bump the timestamp). The
  `NmapToolsPage` is rewritten as a 3-tab right panel
  (Live / History / Last result) with a checkbox column +
  bulk-delete toolbar on history.
- **Seen recency column on IPAM IP table.** New "Seen" column
  backed by a 4-state `SeenDot` (alive < 24 h green / stale
  24 h–7 d amber / cold > 7 d red / never grey, source method
  in the tooltip — orthogonal to lifecycle status).
- **Tools dropdown on subnet header.** IPAM subnet header
  collapsed from 9 buttons to 6 via a Tools dropdown
  (alphabetised: Bulk allocate…, Clean Orphans, Merge…,
  Resize…, Scan with nmap, Split…). New `discovered` status
  added to `IP_STATUSES_INTEGRATION_OWNED` so nmap-stamped
  rows show up correctly across the integration colour-coding.

### Changed

- **IPAM table polish — sticky `<thead>` finally holds in
  Chrome.** The inner `<div className="overflow-x-auto">`
  wrapper was establishing a Y-scroll context per CSS spec —
  `overflow-x: auto` with `overflow-y: visible` computes to
  `overflow-y: auto` automatically, defeating sticky
  positioning by anchoring the head to a non-scrolling
  intermediate parent. Removed the wrapper so sticky resolves
  to the outer `flex-1 overflow-auto`.
- **Shift-click range select on IPAM IP checkboxes.** Capture
  `e.shiftKey` in `onClick` (which fires before `onChange`),
  walk the IP-only `tableRows` order between the previous
  click and the new one, and toggle every selectable row to
  the new state.
- **Subtle dashed-emerald gap-marker rows in the IP table.**
  Between non-adjacent IPAM entries (e.g. `.11 · 1 free` or
  `.11 – .13 · 3 free`) a heads-up row makes deleted /
  missing IPs visible — humans tend to miss single-row gaps
  scrolling a long table. Suppressed inside dynamic DHCP
  pools where slots are owned by the DHCP server.

### Fixed

- **DNS pool member IP edits silently dropped.** The
  `PoolMemberUpdate` Pydantic schema only declared `weight` +
  `enabled`, so a frontend PUT carrying a new `address` was
  filtered out before the handler saw it; the diff loop in
  `PoolsView` was also only checking `enabled` / `weight`.
  Added `address` to both the schema (with an IP validator +
  uniqueness guard returning 409 on collision) and the
  frontend diff. Address change resets the member's health
  stats (`last_check_state="unknown"`, counters → 0) so the
  new IP re-proves health.
- **DNS pool member removal didn't clean up rendered records.**
  `apply_pool_state` only iterated `pool.members` (the in-
  memory list), so records whose member had just been deleted
  were missed before the FK CASCADE stripped the row at SQL
  commit. Added an orphan sweep using a JOIN through
  `DNSPoolMember.pool_id` that catches records whose member
  is no longer attached to the pool, deleting them as part of
  the same reconciliation pass.
- **Pools tab Refresh button on zone detail.** The header
  Refresh on the zone view only invalidated `["dns-records"]`,
  so the Pools tab + per-zone server-state pill stayed
  stale. Now invalidates `["dns-records"]`, `["dns-pools"]`,
  and `["dns-zone-server-state"]`.
- **Reconciliation gate widened on pool member edit.** Was
  `enabled_changed` only — now `member_changed` covers any of
  address / enabled / weight, so a pure address change
  triggers reconciliation (was previously a no-op).
- **CodeQL alerts #19 + #20 — polynomial-redos in the bulk-
  allocate hostname-template parser.** The hand-rolled regex
  driving the `{n}` / `{oct1-4}` substitution
  (`\{(n|oct[1-4])(?::([^}]+))?\}`) was flagged as polynomial
  under `re.sub`: for adversarial inputs starting with `{{n:`
  and many repetitions, every `{` starting position triggered
  an O(n) backtrack scan of the inner `[^}]+`, taking the
  whole substitution to O(n²). Same shape as the BIND9 query
  log parser fixes #16 / #18. Replaced with the stdlib
  `string.Formatter` parser — a C-implemented linear-time
  tokenizer that already understands the exact grammar we
  want and handles escaped `{{` / `}}` correctly. 250 kB
  adversarial input (`{{n:|` × 50000) now renders in ~5 ms.
  Companion `_bulk_template_has_token` warning detector
  uses the same parser, so escaped tokens like `a{{n}}b` no
  longer trip a false-positive warning.
- **`event_outbox` Celery task corrupted asyncpg in prefork
  workers.** First implementation imported `AsyncSessionLocal`
  from `app.db`, which binds asyncpg connections to the loop
  that first checked them out. Celery's prefork pool reuses
  processes across tasks, so the second `asyncio.run`
  re-entered with a different loop and surfaced as `Future
  attached to a different loop` followed by cascading
  `cannot perform operation: another operation is in
  progress` errors. Replaced with a per-tick `NullPool`
  ephemeral engine — same pattern `audit_forward.
  _ephemeral_session` and `event_publisher._ephemeral_session`
  use.

---

## 2026.04.28-2 — 2026-04-28

DNS finish-line + IPAM subnet planning + DHCP option authoring
release. The headline work closes most of the remaining
DNS-specific roadmap items in CLAUDE.md: a multi-resolver
propagation check that fans out to Cloudflare / Google / Quad9 /
OpenDNS in parallel, conditional forwarders as a first-class
zone type, a curated catalog of 14 well-known public RPZ
blocklist sources, a zone-delegation wizard that finds the
parent zone in the same group and auto-stamps the NS + glue
records, four starter zone-template wizards (Email with
MX/SPF/DMARC, Active Directory with the standard SRV records,
Web with apex A + www CNAME, k8s external-dns target), full
TSIG key management with Fernet-encrypted secrets and a
"copy this secret now" reveal modal, a clickable DNS query
analytics strip on the Logs page (top qnames + top clients +
qtype distribution), and BIND9 catalog zones (RFC 9432) with
producer / consumer roles auto-derived from the group's
primary. On the IPAM side the subnet planner lands as a
draggable multi-level CIDR design surface with transactional
apply, the block detail tooling gains a CIDR calculator,
address planner, aggregation suggestion, and free-space
treemap, and bulk-select on block detail reaches parity with
the space view. Plus DHCP scope authoring gets an option-code
library lookup (95-entry RFC 2132 + IANA catalog with
autocomplete on the custom-options row) and named option
templates that can be applied to a scope in one click.
Also: vendor-neutral LLDP neighbour collection on network
devices, hostname targets for the nmap scanner, and a
follow-up linear-time fix for CodeQL alert #18 in the BIND9
query log parser (same shape as the #16 fix that landed in
2026.04.28-1).

### Added

- **DNS multi-resolver propagation check.** New `/dns/tools/
  propagation-check` POST endpoint fires the same query against
  Cloudflare / Google / Quad9 / OpenDNS in parallel via
  `dnspython`'s `AsyncResolver` and returns per-resolver
  `{resolver, status, rtt_ms, answers, error}`. Each query
  carries its own timeout so a slow resolver can't poison the
  others. UI surfaces as a Radar button on every record row in
  the records table; modal lets the operator switch record
  type and re-check. Driver-agnostic — queries are made from
  the API process, doesn't touch the BIND9 / Windows drivers.
- **Conditional forwarders.** `DNSZone` carries `forwarders`
  (JSONB list of IPs) + `forward_only` (true → `forward only;`,
  false → `forward first;`). When `zone_type = "forward"` the
  BIND9 driver renders `zone "X" { type forward; forward only;
  forwarders { ... }; }` in `zone.stanza.j2` and the agent's
  wire-format renderer (no zone file written, no allow-update);
  the form gates the forwarders/policy fields on the type
  selector and refuses submit when no upstreams are listed.
  `ZoneDetailView` swaps the records table for a forwarders +
  policy panel for forward zones — record management never
  applied there. Migration `a07f6c12e5d3_dns_zone_forwarders`.
- **Curated RPZ blocklist source catalog.** Static JSON shipped
  at `backend/app/data/dns_blocklist_catalog.json` with 14
  well-known public blocklists drawn from AdGuard's
  HostlistsRegistry + Pi-hole defaults + Hagezi / OISD
  (AdGuard DNS Filter, StevenBlack Unified, OISD Small/Big,
  Hagezi Pro / Pro+, 1Hosts Lite, Phishing Army Extended,
  URLhaus, DigitalSide Threat-Intel, EasyPrivacy, plus
  StevenBlack fakenews / gambling / adult). New
  `GET /dns/blocklists/catalog` returns the snapshot
  (in-process cached); `POST /dns/blocklists/from-catalog`
  creates a normal `DNSBlockList` row with `source_type="url"`
  prefilled and immediately enqueues `refresh_blocklist_feed`
  so the list populates without a manual click. Frontend
  "Browse Catalog" button on the Blocklists tab opens a
  filterable picker (category + free-text), with already-
  subscribed entries flagged.
- **Zone delegation wizard.** `services/dns/delegation.py`
  finds the longest-suffix-matching parent zone in the same
  group (forward zones excluded), reads the child's apex NS
  records, and computes the NS records the parent needs to
  delegate the child plus glue (A / AAAA) for any
  in-bailiwick NS hostnames. Diffs against existing parent
  records so a second run is a no-op, surfaces warnings
  ("ns1 is in-bailiwick but has no A/AAAA in child"), and
  applies through the normal `enqueue_record_op` pipeline so
  the parent zone's serial bumps once. Endpoints
  `GET /dns/groups/{gid}/zones/{zid}/delegation-preview` +
  `POST /dns/groups/{gid}/zones/{zid}/delegate-from-parent`.
  Frontend: contextual "Delegate" button appears in the zone
  header only when an eligible parent has missing records;
  `DelegationModal` shows the exact records that would land
  in the parent before commit.
- **DNS template wizards.** Static catalog at
  `backend/app/data/dns_zone_templates.json` with four starter
  shapes (Email zone with MX + SPF + DMARC + optional DKIM
  selector, Active Directory zone with the standard LDAP /
  Kerberos / GC SRV records + optional `_sites` entries, Web
  zone with apex A + optional AAAA + `www CNAME`, Kubernetes
  external-dns target — empty zone). `services/dns/
  zone_templates.py` validates required parameters and
  substitutes `{{key}}` placeholders (plus a built-in
  `{{__zone__}}`) at materialise time; records can declare
  `skip_if_empty: ["param"]` so optional fields drop out
  cleanly. Endpoints `GET /dns/zone-templates` +
  `POST /dns/groups/{gid}/zones/from-template`. Frontend
  `ZoneTemplateModal` mounted as a "From Template" button on
  the ZonesTab header alongside "Add Zone".
- **TSIG key management UI.** New `DNSTSIGKey` model with
  Fernet-encrypted `secret_encrypted`, `algorithm` enum
  (hmac-sha1 / 224 / 256 / 384 / 512), `name`, `purpose`,
  `notes`, `last_rotated_at`. CRUD at `/api/v1/dns/groups/
  {gid}/tsig-keys` with a side `/generate-secret` helper that
  returns a fresh random base64 secret of the right size for
  the chosen algorithm, and a `/{kid}/rotate` endpoint that
  re-randomises the secret. Plaintext is returned **once** on
  the create / rotate response — list / get never expose it.
  Operator-managed rows distribute to every BIND9 agent in
  the group via the existing `tsig_keys` block in the
  `ConfigBundle` (alongside the legacy auto-generated agent
  loopback key); named.conf renders one
  `key { algorithm …; secret …; };` stanza per row. UI: new
  "TSIG Keys" tab on the DNS server group view, with create /
  edit / rotate / delete plus a one-shot "Copy this secret
  now" modal after each create / rotate. Migration
  `7c299e8a5490_dns_tsig_keys`.
- **DNS query analytics.** `POST /api/v1/logs/dns-queries/
  analytics` returns top-10 qnames + top-10 clients + complete
  qtype distribution in a single round trip. Computed
  on-demand via `GROUP BY` against the existing
  `dns_query_log_entry` rows (24 h retention) — no new schema,
  no new beat task. The Logs → DNS Queries tab renders an
  Analytics strip above the raw event grid: three cards each
  showing key + count + percentage of total, with every row
  clickable to seed the corresponding filter (qname /
  client_ip / qtype). The strip refetches only when
  `(server_id, since)` changes, so per-keystroke filter edits
  on the events grid don't pay for a re-aggregation.
- **BIND9 catalog zones (RFC 9432).** Opt-in per group via
  `DNSServerGroup.catalog_zones_enabled` +
  `catalog_zone_name` (defaults to `catalog.spatium.invalid.`).
  The producer is the group's `is_primary=True` bind9 server;
  every other bind9 member joins as a consumer. Bundle
  assembly emits a `catalog` block per server: `mode=producer`
  ships the member zone list, `mode=consumer` ships the
  producer's IP. The agent renders the catalog zone file per
  RFC 9432 §4.1 — SOA + NS at apex,
  `version IN TXT "2"`, and one `<sha1-of-wire-name>.zones IN
  PTR <member>` per primary zone — and on consumers injects a
  single `catalog-zones { zone "<catalog>." default-masters {
  <producer-ip>; } in-memory yes; };` directive into the
  options block. The catalog block is part of the structural
  ETag so membership changes trigger a daemon reload, and
  SHA-1 hashing uses the proper wire format (length-prefixed
  labels + null terminator). Frontend toggle lives in the
  server-group create / edit modal alongside the recursion
  checkbox. Migration `d8e4a73f12c5_dns_catalog_zones`.
- **IPAM subnet planner.** New `/ipam/plans` page where the
  operator designs a multi-level CIDR hierarchy as a
  draggable tree (one root + nested children, arbitrary
  depth), saves it as a `SubnetPlan` row, validates against
  current state, then one-click applies — every block +
  subnet created in a single transaction. `kind` is
  explicit per node (`block` or `subnet`); root must be a
  block (subnets need a block parent), and a subnet may not
  have children. Resource bindings (DNS group, DHCP group,
  gateway) are optional per-node — `null` = inherit, explicit
  value sets the field on the materialised row and flips the
  corresponding `*_inherit_settings=False`. Two root modes:
  new top-level CIDR (creates a fresh block at the space
  root) OR anchor to an existing `IPBlock` (descendants land
  as children of the existing block). Validation
  (`/plans/{id}/validate` + `/plans/validate-tree` for
  in-flight trees) checks duplicate node ids, kind rules,
  parent-containment, sibling-overlap, and overlap against
  current IPAM state. Live validation runs every 300 ms; the
  apply confirmation modal surfaces block + subnet counts;
  any conflict mid-apply → 409 with the full conflict list
  and nothing is written. `/plans/{id}/reopen` flips an
  applied plan back to draft state when the materialised
  resources have all been deleted, so operators can iterate
  without starting fresh. Frontend uses `@dnd-kit/core` for
  drag-to-reparent; drops onto descendants OR onto subnet
  targets are refused. Sidebar entry "Subnet Planner"
  alongside NAT Mappings. Migration
  `c8e1f04a932d_subnet_plan`.
- **IPAM subnet planning + calculation tools.** Four
  related additions on the block detail surface:
  - **CIDR calculator** at `/tools/cidr` — pure client-side
    breakdown of any IPv4 or IPv6 prefix
    (network / netmask / wildcard / broadcast / range / total
    addresses / decimal + hex / binary breakdown for v4 and
    compressed + expanded forms for v6). Quick-paste preset
    buttons for the common RFC 1918 / CGNAT / ULA blocks.
    BigInt math throughout so v6 prefixes work cleanly.
    Sidebar entry under Tools.
  - **Address planner** —
    `POST /api/v1/ipam/blocks/{id}/plan-allocation` accepts
    a list of `{count, prefix_len}` requests (e.g. `4 × /24,
    2 × /26, 1 × /22`) and packs them into the block's free
    space using largest-prefix-first ordering with first-
    fit-by-address placement (so sequential same-size
    requests pack contiguously from low addresses). Returns
    the planned allocations + any unfulfilled rows + the
    remaining free space after the plan. Reuses the same
    `address_exclude` walk that powers `/free-space`. UI:
    "Plan allocation…" button next to the Allocation map.
  - **Aggregation suggestion** —
    `GET /api/v1/ipam/blocks/{id}/aggregation-suggestions`
    runs `ipaddress.collapse_addresses` on the block's
    direct-child subnets; any output that subsumes more than
    one input is a clean merge opportunity. Read-only banner
    on the block detail surfaces them when present
    (e.g. `10.0.0.0/24 + 10.0.1.0/24 → /23`).
  - **Free-space treemap** — Recharts squarified Treemap on
    the block detail, toggled via a Band / Treemap selector
    next to the Allocation map header (selection persisted
    in sessionStorage per block). Cells coloured by kind
    (violet child blocks, blue subnets, hashed-zinc free)
    and sized by raw address count. Pixel-thin slices on the
    1-D band become visible squares here.
- **Block-detail bulk-select parity with space view.** The
  block-detail table can now select child blocks (not just
  subnets), so the bulk-action toolbar that's been there at
  the top level is accessible inside any block. Selection
  state moved to the same `subnet:<id>` / `block:<id>` keyed
  Set the space view uses; a single bulk delete cascades a
  mixed set (subnets first, then leaf blocks, allSettled on
  both phases). Subnet-only actions (Bulk Edit, Split, Merge)
  gate on the absence of selected blocks.
- **Vendor-neutral LLDP neighbour collection.** Adds an
  LLDP-MIB (IEEE 802.1AB) walk as a 5th poll step on every
  network device, gated by per-device `poll_lldp` toggle
  (default on). Captures remote chassis ID + port ID
  (subtype-aware decoding — MAC addresses formatted, interface
  names left raw), system name + description, port
  description, and decoded capabilities bitmask (Bridge /
  Router / WLAN AP / Phone / Repeater / Other / Station /
  DocsisCableDevice). Stored in
  `network_neighbour(device_id, interface_id,
  remote_chassis_id, remote_port_id)` with absence-delete
  every poll so stale neighbours fall off cleanly. New API
  `GET /network-devices/{id}/neighbours` with `sys_name` /
  `chassis_id` / `interface_id` filters; new "Neighbours" tab
  on the network device detail page with vendor-aware enable
  hints (Cisco IOS / NX-OS, Junos, Arista EOS, ProCurve /
  Aruba, MikroTik RouterOS, OPNsense / pfSense) when no rows
  are present. Migration `b9e4d2a17c83_network_neighbour`.
- **Nmap accepts hostname targets.** Operators routinely
  scan `router1.lan` without first looking up its IP, and
  nmap does its own DNS resolution at scan time. New
  `_HOSTNAME_RE` validates RFC 1123 labels (rejecting shell
  metachars, spaces, slashes, anything that isn't a valid
  DNS character). The `target_ip` column is widened from
  `INET` to `VARCHAR(255)` (DNS hard upper bound) via
  migration `f4a83cb15920_nmap_target_text`; the column name
  stays for audit / API continuity. Form input relabelled
  "Target" with hostname examples + helper text.
- **DHCP option-code library lookup.** Static catalog of 95
  RFC 2132 + IANA `bootp-dhcp-parameters` v4 entries shipped
  at `backend/app/data/dhcp_option_codes.json` (each entry:
  `code`, `name`, `kind`, `description`, `rfc`). Loaded once
  per process via `services/dhcp/option_codes.py`
  (lru_cache); `search()` helper does case-insensitive
  name/description matching with numeric-prefix code lookup.
  `GET /api/v1/dhcp/option-codes` returns the catalog (with
  optional `q=` substring filter + `limit`). Frontend wires
  it into `DHCPOptionsEditor`'s custom-options row — the
  bare numeric code input is now a combobox that searches by
  code or name, surfaces the description as a hint under the
  row, and auto-fills `name` on pick. Catalog is fetched once
  per session (`staleTime: Infinity`) and filtered
  client-side, so per-keystroke search has no server round-
  trip. v6 catalog deferred until v6-specific UI lands.
- **DHCP option templates.** New `DHCPOptionTemplate` model,
  group-scoped, holds named bundles of option-code → value
  pairs (e.g. "VoIP phones", "PXE BIOS clients"). CRUD at
  `/api/v1/dhcp/server-groups/{gid}/option-templates` +
  `/api/v1/dhcp/option-templates/{id}` plus a server-side
  `POST /scopes/{id}/apply-option-template` for programmatic
  apply (mode `merge` = template wins, mode `replace` = drop
  existing). UI ships a new "Option Templates" tab on the
  DHCP server-group view (mirrors Client Classes / MAC
  Blocks) with the shared `DHCPOptionsEditor` for authoring,
  plus an "Apply template…" picker above the options editor
  on the scope create / edit modal that does a client-side
  merge into the editor's local state — operator still hits
  Save to persist; conflict-key list surfaces inline so the
  operator knows what was overwritten. Apply is a stamp,
  not a binding — later template edits do not propagate back
  to scopes that already used it. Permission gate
  `dhcp_option_template`, seeded into the existing
  "DHCP Editor" role. Migration
  `e7f218ac4d9b_dhcp_option_templates`.
- **DNS Blocklists multi-select.** Conditional bulk-action
  toolbar (Apply / Detach / Refresh / Delete) on the
  Blocklists tab — each button counts only the rows where
  the action makes sense (Refresh skips manual lists,
  Detach skips not-applied, etc.).
- **IP space VRF / RD / RT fields on Create.** The IPAM
  Create IP Space modal gains the same VRF /
  Route-distinguisher / Route-targets fields the Edit modal
  already had — the backend schema accepted them; only the
  create form was the gap. Collapsed by default since most
  homelab deployments don't run multi-VRF.

### Changed

- **Subnet / block utilization is now visible in the
  Allocation map.** Each subnet/block cell carries a
  mid-saturation tint (the slice exists) plus a saturated
  fill sized by `utilization_percent`, so you can see at a
  glance which subnets are nearly full vs nearly empty
  without scrolling to read the table below. Applies to
  both the Band view and the new Treemap view.
- **Per-row Refresh button on the Blocklists tab now shows
  a spinner + auto-polls** until `last_synced_at` advances
  (was silently no-op-feeling).
- **Blocklist per-row action buttons no longer hide on
  hover** — operator complaint after the bulk-actions push
  obscured the per-row affordances.
- **Sidebar nav alphabetised within each section** and the
  audit-log group merged into platform admin (one less
  divider to scan past).
- **Adding a new IPAM block now reparents existing subnets
  too**, not just sibling blocks. Operator intent on adding
  e.g. a /16 inside a /12 that already holds /24 subnets is
  for the /24s to land under the new /16 — matching the
  existing block-reparenting story we already had on
  `create_block`. Audit row carries `reparented_subnets`
  listing what moved.
- **DNS group selection / expand-collapse UX** — clicking
  the name of an already-expanded group no longer collapses
  it (operator complaint after returning from a zone
  drilldown caused unwanted collapse). Click is now
  expand-only; explicit chevron click still toggles.

### Fixed

- **CodeQL alert #18 — polynomial-redos in BIND9 query log
  view-name regex.** The previous shape was
  `\(\s*view\s+(?P<view_paren>[^)]+?)\s*\)` — both `\s+` and
  the lazy `[^)]+?` could match whitespace, so on
  operator-supplied inputs like `(view ` followed by many
  spaces with no closing paren the engine enumerated every
  split between the two and backtracked quadratically
  (~3.4 s on 50k chars in the lab). Same shape as alert #16
  that landed at the start of release 2026.04.28-1. Fix is
  to give every segment a disjoint character class:
  `\(view\s+(?P<view_paren>[^)\s]+)\s*\)`. Adversarial
  200k-char input now parses in ~4 ms; existing parser tests
  still pass. Taint source is the agent-posted query log
  `raw` field, so this is a real DoS surface.
- **Live nmap SSE viewer now clears the output buffer when
  the scan ID changes.** The parent reuses the same
  component instance across scans (just swaps the prop), so
  React's `useState` initial value never reset and old
  lines lingered until the first new `data:` frame painted
  over them. Reset moved into the `useEffect` that opens the
  EventSource so it fires on every scan switch.

---

## 2026.04.28-1 — 2026-04-28

Network discovery + nmap release. The headline work is the SNMP
polling surface that walks standard MIBs (IF-MIB, IP-MIB,
Q-BRIDGE-MIB, RFC1213/BRIDGE-MIB fallbacks) on routers + switches
to populate ARP / FDB / interface tables and cross-references the
results back into IPAM (last-seen timestamps, optional auto-create
of discovered IPs, switch-port + VLAN visibility on every IP that's
been observed). Bundled with an on-demand nmap scanner — preset
or custom scans launched from a per-IP "Scan with Nmap" button or
the standalone `/tools/nmap` page, with live SSE output streaming
to the browser and structured XML parsed into a results panel. The
IPAM IP table now opens a read-only detail modal on row click
(replacing the previous edit-form-on-click behaviour); the sidebar
is regrouped (core flattened, Tools section added, Administration
items separated by dividers); the Settings → Discovery section that
toggled a never-implemented stub task is removed; and the BIND9
query log parser is reworked to be linear-time (CodeQL alert #16
closed).

### Added

- **SNMP-based network discovery.** New `/network` top-level page
  for managing routers + switches with read-only SNMP polling.
  Vendor-neutral — works on Cisco / Juniper / Arista / Aruba /
  MikroTik / OPNsense / pfSense / FortiNet / Cumulus / SONiC /
  FS.com / Ubiquiti out of the box because everything walks
  standard MIBs.
  - **Data model** (migration `c4e7a2f813b9_network_devices`):
    `network_device` row carries SNMP credentials Fernet-encrypted
    at rest (v1 / v2c community OR v3 USM with auth + priv
    protocol enums + context name); `network_interface`,
    `network_arp_entry` keyed `(device, ip, vrf)`, and
    `network_fdb_entry` keyed `(device, mac, vlan)` with the
    Postgres 15+ `NULLS NOT DISTINCT` unique index — so a single
    port can carry the same MAC across multiple VLANs (hypervisor
    with VMs in different access VLANs, IP phone with PC
    passthrough on voice + data VLANs).
  - **MIBs walked** — SNMPv2-MIB system group (sysDescr /
    sysObjectID / sysName / sysUpTime), IF-MIB `ifTable` +
    `ifXTable`, IP-MIB `ipNetToPhysicalTable` with legacy
    RFC1213 `ipNetToMediaTable` fallback, Q-BRIDGE-MIB
    `dot1qTpFdbTable` with BRIDGE-MIB `dot1dTpFdbTable` fallback.
  - **Polling pipeline.** `pysnmp` 6.x async with `bulkWalkCmd`
    (one OID column per walk to avoid GETBULK PDU bloat that
    timed out on UniFi switches). `app.tasks.snmp_poll.poll_device`
    runs sysinfo → interfaces → ARP → FDB sequentially under a
    per-device `SELECT FOR UPDATE SKIP LOCKED` so concurrent
    dispatches can't double-poll the same row.
    `dispatch_due_devices` beat-fires every 60 s and queues every
    active device whose `next_poll_at <= now`. Per-device interval
    default 300 s, minimum 60 s. Status: `success | partial |
    failed | timeout`, with `last_poll_error` populated for ops
    triage. Stale ARP entries are kept with `state='stale'` (no
    delete); `purge_stale_arp_entries` daily beat task removes
    rows older than 30 days.
  - **IPAM cross-reference.** After every successful ARP poll,
    `cross_reference_arp` finds matching `IPAddress` rows in the
    device's bound `IPSpace` and updates `last_seen_at` (max-merge),
    `last_seen_method='snmp'`, and fills `mac_address` only when
    currently NULL — operator-set MACs are never overwritten. When
    the per-device `auto_create_discovered=True` toggle is on
    (off by default), inserts new `status='discovered'` rows for
    ARP IPs that fall inside a known `Subnet`. Returns counts
    (`updated`, `created`, `skipped_no_subnet`).
  - **Switch-port column in IPAM.** The IPAM IP table carries a
    "Network" column showing `<device> · <port> [VLAN N]` for the
    most-recent FDB hit on each IP's MAC, with a `+N more` badge
    + hover tooltip listing every (device, port, VLAN) tuple when
    the MAC is learned in multiple places. Backed by a batched
    `GET /api/v1/ipam/subnets/{id}/network-context` endpoint that
    returns `{ip_address_id: NetworkContextEntry[]}` in one round
    trip — no N+1 fan-out per page-of-IPs. Per-IP detail modal
    keeps the deeper "Network" tab for the full per-MAC drilldown.
  - **API.** Full CRUD at `/api/v1/network-devices` plus
    `POST /test` (synchronous SNMP probe, ≤10 s, returns
    `TestConnectionResult` with sysDescr + classified
    `error_kind`: `timeout | auth_failure | no_response |
    transport_error | internal`), `POST /poll-now` (queues
    immediate Celery task, returns 202 + task_id), and per-device
    list endpoints `/interfaces`, `/arp` (filter by ip / mac /
    vrf / state), `/fdb` (filter by mac / vlan / interface_id).
    All paginated `{items, total, page, page_size}`.
  - **Frontend.** Top-level `/network` page in the core sidebar.
    Per-device detail at `/network/:id` with Overview / Interfaces
    / ARP / FDB tabs, each filterable + paginated. Add/edit modal
    with SNMP-version-conditional credential fields plus inline
    Test Connection (saves first on create, then probes against
    the saved row). New "Network" tab on the IP detail modal
    showing per-IP switch/port table sorted by `last_seen DESC`.
  - **Bulk operations + import/export.** Network page supports
    multi-select with bulk Test / Poll Now / Activate / Deactivate
    / Delete actions, plus per-row Edit pencil. CSV export
    (deliberately no credentials) and CSV import with default-
    community fallback for v1/v2c rows missing the column. Live
    preview validates each row (resolves `ip_space_name` → id,
    checks enums + port range), shows ready/error per row, then
    commits via `Promise.allSettled` of per-row creates with
    per-row outcome reporting.
  - **Permissions.** Single `manage_network_devices` permission
    gates all endpoints (read + write); new "Network Editor"
    builtin role gets it. Superadmin always bypasses.
  - **Tests.** 35 backend tests covering pysnmp wrapper paths
    (mocked: v1 / v2c / v3 auth construction, OID resolution,
    `ipNetToPhysical → ipNetToMedia` fallback,
    `Q-BRIDGE → BRIDGE` fallback, error classification), API CRUD
    + `/test` + `/poll-now` + the four list endpoints +
    `/network-context`, and three cross-reference paths.

- **Nmap scan integration.** On-demand nmap scans against any
  IPv4/IPv6 host from the SpatiumDDI host perspective. Two entry
  points: a per-IP "Scan with Nmap" button on the IPAM detail
  modal, and a standalone `/tools/nmap` page for ad-hoc targets
  (including IPs that aren't in IPAM yet).
  - **Data model** (migration `d2f7a91e4c8b_nmap_scans`):
    `nmap_scan` table carries the target IP + optional FK to the
    matching `IPAddress` row, the operator's preset choice, the
    sanitised port-spec + extra-args, full status / exit-code /
    duration metadata, and the parsed summary JSON. The actual
    XML artefact lands in `raw_xml`; the line-buffered human
    output lands in `raw_stdout` (so the SSE stream has something
    to replay if an operator opens a viewer mid-scan).
  - **Presets.** `quick` (`-T4 -F`), `service_version` (`-T4 -sV
    --version-light`), `os_fingerprint` (`-T4 -O`),
    `default_scripts` (`-T4 -sC`), `udp_top100` (`-T4 -sU
    --top-ports 100`), `aggressive` (`-T4 -A`), and `custom`
    (everything from `extra_args`).
  - **Argv hardening.** `build_argv` validates target IPs via
    `ipaddress.ip_address`, port-specs against `^[0-9,\-,UTSI:]+$`,
    and shlex-tokenises operator-supplied extra args, rejecting
    any token containing shell metacharacters
    (`;|&$\`<>()`) or path traversal in `--script` values. The
    subprocess is spawned via `create_subprocess_exec` — never a
    shell. nmap runs as the API container's non-root user, so
    privileged scan modes (raw SYN, OS detection without
    privilege) silently degrade to TCP-connect.
  - **Dual output for live UX.** `nmap -oN -` streams human-
    readable output to stdout (what the operator sees scrolling
    in the live viewer); `-oX <tmpfile>` writes structured XML
    to a per-scan tempfile in parallel. After process exit, the
    runner reads the XML, parses it into `summary_json`, and
    unlinks the file. No XML wall-of-text in the live view.
  - **API.** `POST /api/v1/nmap/scans` (queues a celery task,
    returns 202 + the row), `GET /scans` (paginated list, filter
    by ip_address_id / target_ip / status), `GET /scans/{id}`
    (full record), `GET /scans/{id}/stream` (SSE — emits one
    `data:` frame per nmap stdout line, then a final `event:done`
    on terminal status), and `DELETE /scans/{id}` (cancels
    queued/running scans, hard-deletes terminal ones — both
    paths share the trash button in the UI).
  - **SSE auth.** `EventSource` can't set Authorization headers,
    so the stream endpoint accepts `?token=<jwt-or-api-token>`.
    A dedicated `_resolve_user_from_query_token` helper validates
    the token against the same JWT / API-token paths that the
    Bearer dep uses; the router has no global
    `Depends(get_current_user)` because that would 401 the SSE
    request before the query-token resolver could run (each
    non-SSE endpoint declares its own permission dep instead).
  - **Frontend.** `NmapScanModal` flips between the form view and
    the live output viewer (Cmd+K-style). `NmapScanForm` carries
    the preset radio group + port-spec + extra-args + lockable
    target. `NmapScanLiveViewer` opens an `EventSource`,
    appends each line to a `<pre>` with auto-scroll, and renders
    the parsed summary panel (open ports table + OS guess) on
    `done`. `NmapToolsPage` reuses the same form + viewer
    components, plus a Recent Scans table with row-click to
    open and per-row delete (both with a custom
    `ConfirmDeleteScanModal` instead of `window.confirm`).
  - **Permissions.** Single `manage_nmap_scans` permission gates
    all endpoints, seeded into the existing "Network Editor"
    builtin role.
  - **Image.** `nmap` added to the api Dockerfile's apt-get
    install list.

- **IP detail modal.** Clicking an IP row in the IPAM table now
  opens a read-only detail surface (`IPDetailModal`) with status /
  role / DHCP-mirror badges, hostname + FQDN + MAC + OUI vendor,
  forward / reverse DNS zone references, DNS / DHCP linkage
  flags, tags, custom-fields table, and the per-IP SNMP network-
  context inline. Action buttons in the modal header: **Scan with
  Nmap**, **Edit** (hops into the existing form), **Delete**
  (routes through the existing orphan-vs-purge confirm).
  Read-only rows (network / broadcast / DHCP-mirror / orphan /
  read-only statuses) stay inspectable but hide the Edit /
  Delete actions. The pencil + trash icons in the row's right-
  edge cell still behave as before — the detail modal is purely
  additive.

- **Network device CSV import / export.** Import accepts CSV via
  file picker or paste, validates each row pre-commit (resolves
  `ip_space_name` → id, checks enums + port range), and shows
  per-row status. Export downloads
  `network-devices-<utc>.csv` with name / hostname / ip_address /
  device_type / description / vendor / snmp_version / snmp_port /
  ip_space_name / is_active / last_poll_status — deliberately no
  community / v3 keys since exports must not leak credentials.
  An import-time "default community" field fills v1/v2c rows
  missing the column so round-trip exports + edits + re-imports
  work without re-typing communities per device.

### Changed

- **Sidebar regroup.** Core nav reordered for data-flow logic
  (Dashboard → IPAM → VLANs → NAT → DNS → DHCP → Network →
  Logs). New **Tools** section between core and Integrations
  (always visible, default-open) holds the Nmap entry. The
  Administration section's 11 items are now grouped into
  Identity & Access (Users, Groups, Roles, Auth Providers, API
  Tokens) → Platform (Settings, Custom Fields, Alerts, Platform
  Insights, Trash) → Audit (Audit Log), separated by horizontal
  dividers within the same collapsible parent — no nested
  collapsibles. Collapsed-rail mode flattens cleanly.

- **README "What's in the box".** The 17 dense paragraph-bullets
  are replaced with five category-grouped tables (Core DDI /
  Discovery & visibility / Integrations / Identity & ops /
  Deployment) plus a one-line tagline above. Each row carries
  an emoji + bold feature name + 6-10 word detail — eyes scan
  in seconds. The original long-form prose is preserved verbatim
  under a `<details>` disclosure for evaluators who want the
  full spec.

- **BIND9 query log parser.** Reworked to drop the polynomial-
  ReDoS regex shape that CodeQL alert #16 flagged. The previous
  iteration's three independent `\s+`-anchored optional groups
  (parenthesised view, bare view, qname/qclass/qtype/flags
  chain) gave the engine room to try multiple alignments of
  whitespace runs on adversarial input. Replaced with a hard
  split on the unambiguous `: query: ` literal: a tiny linear
  `_HEAD_RE` matches client + port and emits the remainder,
  `_VIEW_RE` extracts an optional view name from that remainder,
  and `_BODY_RE` matches qname/qclass/qtype/flags anchored at
  the start of the post-separator slice. Each regex is now
  clearly linear; the existing 13 parser tests pass unchanged.

- **CI workflow on docs-only pushes.** `ci.yml` now uses
  `paths-ignore` to skip the lint / typecheck / test pipeline
  when a push only touches `**/*.md`, `docs/**`, `LICENSE`,
  `NOTICE`, `.gitignore`, or issue/PR templates.

### Removed

- **Settings → Discovery section.** The two toggles
  (`discovery_scan_enabled`, `discovery_scan_interval_minutes`)
  shipped in 2026.04.16-1 with a Celery task stub that never
  did anything — no beat schedule, no production code reading
  the flags. Real discovery is the SNMP polling surface above
  (with its own per-device `auto_create_discovered` toggle).
  Migration `a4d92f61c08b_drop_discovery_scan_settings` drops
  both columns; the Settings page section is gone; the stub
  Celery task is deleted. Destructive but safe — the columns
  held no operational data.

### Fixed

- **Nmap task dispatch.** `app.tasks.nmap.*` had no entry in
  `task_routes` so dispatched scans landed in celery's default
  `celery` queue, which the worker doesn't subscribe to (worker
  consumes only `ipam` / `dns` / `dhcp` / `default`). Added
  the route. Without this, scan rows stayed `queued` forever
  and the SSE stream just polled an empty `raw_stdout`.

- **Nmap SSE 401.** The router-level `Depends(get_current_user)`
  fired the Bearer extractor before the per-endpoint query-token
  resolver could run, so EventSource (which can't set Authorization
  headers) always 401'd on `/scans/{id}/stream`. Removed the
  router-level dep; every other endpoint already enforces auth
  via its own permission dep.

- **Confirm-delete dialogs in nmap.** The first cut used the
  browser's `window.confirm()` which doesn't match the rest of
  the app's modal patterns. Replaced with a shared
  `ConfirmDeleteScanModal` showing the target IP, preset, and
  status; verb flips between "Cancel" (running) and "Delete"
  (terminal).

### Notes

- The SSE stream is implemented as a 500 ms-poll over the DB-
  persisted `raw_stdout` column (one `db.get(NmapScan, …)` +
  `expire_all()` per tick per active stream). For nmap that's
  fine — the tool emits lines at human cadence — but it's a hot
  loop per concurrent viewer. If many operators end up watching
  live scans simultaneously (more than ~20-30) it'll show up as
  measurable Postgres load; the natural follow-up is a Redis
  pub/sub fanout or `LISTEN/NOTIFY` behind the same HTTP shape.
  Tracked under deferred follow-ups in `CLAUDE.md`.

- SNMP polling lives in the existing Celery worker pool. That's
  fine to ~100 devices on a 5-min interval. Splitting into a
  dedicated `snmp-poller` container becomes interesting once
  SNMP traffic competes with the worker's other tasks or when
  the operator wants different network reachability for the
  poller (different VLAN, jumphost, etc) — also tracked as a
  deferred follow-up.

- Nmap runs as a non-root user inside the api container. That's
  the right default for a containerised service, but it means
  raw SYN scans (`-sS`) and unprivileged OS detection silently
  fall back to TCP-connect. Operators running on bare metal can
  give the API process `CAP_NET_RAW` to unlock those modes;
  containerised deployments can't and shouldn't.

---

## 2026.04.26-1 — 2026-04-26

IPAM operations + observability release. The headline work is the
soft-delete + Trash recovery surface for accidental deletions, three
preview-then-commit subnet operations (find-free / split / merge),
NAT mapping cross-reference into IPAM, the new
`/admin/platform-insights` page surfacing Postgres + container stats
without a Prometheus pipeline, and dashboard sub-tabs that split
the home page into Overview / IPAM / DNS / DHCP. Also bundles per-IP
role + reservation TTL + MAC observation history, DHCP lease history
forensics, IPSpace VRF metadata, VXLAN UI surface, the `task_session`
helper that fixes a long-standing Celery loop-leak across seven
tasks, the DNS-agent 404 → re-bootstrap recovery path, k8s + Helm
worker / beat liveness probes, ReDoS hardening on the BIND9 + Kea
log-line parsers, and the f963137 fix (status-validator +
`user_modified_at` lock + Proxmox bridge gateway) that was pushed
in the previous cycle but never made the changelog.

### Added

- **Dashboard sub-tabs.** Home page now sits under four tabs:
  Overview / IPAM / DNS / DHCP. Selection persists to
  ``localStorage`` so reload lands on the last-viewed tab; the KPI
  strip stays visible across all tabs (subnet count + utilisation +
  zones + servers as the always-present inventory). Per tab:
  - **Overview** — heatmap, Top Subnets (compact, top 6), Live
    Activity feed, Platform Health card, empty-state for fresh
    installs. The "everything-at-a-glance" page.
  - **IPAM** — heatmap (also shown), three new summary cards (IPv4
    vs IPv6 subnet-count split, total NAT mappings, IPv4 capacity
    headroom), Top Subnets extended list (top 20), and the
    Integrations panel (Kubernetes / Docker / Proxmox / Tailscale)
    moved here since they all populate IPAM rows.
  - **DNS** — DNS query rate chart full-width (previously cramped
    into a half-width slot on the home page) + DNS server list
    with status / driver / group / last-seen columns. Empty-state
    explains how to register a server.
  - **DHCP** — DHCP traffic chart full-width + DHCP server list
    + HA Pairs section listing groups with ≥ 2 Kea members.
    Same empty-state pattern.
  Refresh button now also invalidates the new
  ``["nat-mappings", "count"]`` and ``["platform-health"]`` keys
  alongside the existing dashboard query keys.

- **Platform Insights admin page.** New `/admin/platform-insights`
  surface with two tabs covering the bits operators usually need a
  separate Prometheus / pgwatch / Grafana pipeline for:
  - **Postgres** — version + DB size, cache hit ratio, current WAL
    position, active vs max connections, longest-running
    transaction (PID / age / state / query / app / client). Tables
    by total size (heap + indexes + TOAST, live + dead rows, last
    autovacuum) for catching unbounded growth in audit / metrics
    / log tables. Connections grouped by state with "idle in
    transaction" tinted amber — the canonical signal for a stuck
    pool. Slow queries from `pg_stat_statements` if the extension
    is enabled, with a friendly hint when it isn't (we don't
    install it ourselves; needs `shared_preload_libraries` +
    restart).
  - **Containers** — per-container CPU% (computed the same way
    `docker stats` does), memory used / limit / %, network rx /
    tx, block-IO read / write. Default-filtered to the
    `spatiumddi-*` prefix; pass empty `prefix=` to see every
    container on the host. Tone-coded (red >80% CPU, amber >50%;
    red >90% memory, amber >75%) and auto-refreshes every 5 s.
    Endpoint reports `available=false` with a one-line hint when
    `/var/run/docker.sock` isn't mounted into the api container —
    operator opt-in via the same compose toggle the Docker
    integration uses. K8s side covered the same way via a
    hostPath mount.
  Backend at `app/api/v1/admin/postgres.py` (4 endpoints) +
  `app/api/v1/admin/containers.py` (1 endpoint). Sidebar entry
  under Admin → Platform Insights with a Cpu icon.

- **NAT mapping ↔ IPAM tighter integration.** NAT records used
  to be loose strings; an `IPAddress` row showed only a count
  badge with no way to drill in. Now:
  - **FK columns on `nat_mapping`** — `internal_ip_address_id`
    and `external_ip_address_id` (nullable, ``ON DELETE SET
    NULL``) auto-resolved on create / update by looking up the
    typed string in `ip_address`. Strings stay authoritative for
    addresses outside IPAM (a public WAN IP, a peer's NAT
    endpoint), so existing operator workflows keep working.
    Migration `f5b9c1e8d472` adds + backfills the columns.
  - **Conflict detection** — create / update reject 409 when the
    requested external IP+ports is already claimed by another
    `1to1` / `pat` rule on the same protocol. Port-overlap
    aware; protocol-aware (an `any`-protocol pat collides with
    everything on its IP).
  - **Per-IP and per-subnet listing endpoints** —
    `GET /ipam/nat-mappings/by-ip/{id}` returns every mapping
    touching an IPAM row (FK match + INET-string match on either
    side); `GET /ipam/nat-mappings/by-subnet/{id}` uses Postgres
    `inet <<= cidr` containment to find every mapping whose
    internal IP falls inside a subnet's CIDR.
  - **UI** — clicking the NAT badge on an IP row opens a modal
    listing every mapping for that IP (formatted as
    `internal:port → external:port` with kind / protocol /
    device pills). A new "NAT" tab on the subnet detail page
    shows every mapping touching that subnet.

- **VXLAN ID surface in the IPAM UI.** `subnet.vxlan_id` already
  existed in the schema (Integer, nullable, range 1–16 777 214)
  and the frontend type, but no UI ever read or wrote it.
  Numeric input added to Create + Edit subnet modals next to the
  VLAN picker; chip on the subnet detail header next to the
  existing VLAN chip when set.

- **Per-IP role + reservation TTL + MAC observation history.**
  `IPAddress` gains `role` (host / loopback / anycast / vip /
  vrrp / secondary / gateway — orthogonal to status) and
  `reserved_until` (datetime, nullable). New beat task
  `app.tasks.ipam_reservation_sweep.sweep_expired_reservations`
  flips reserved rows past their TTL back to `available`, on a
  5-minute cadence. Roles in `IP_ROLES_SHARED` (anycast / vip /
  vrrp) bypass MAC-collision warnings — the same MAC legitimately
  appears on multiple IPs in a load-balancer or HSRP/VRRP pair.
  New `ip_mac_history` table tracks every distinct MAC ever
  observed against an IP, keyed `(ip_address_id, mac_address)`
  with `first_seen` + `last_seen` timestamps; written on every IP
  create / update where a MAC is present, surfaced via
  `GET /ipam/addresses/{id}/mac-history` (newest-first, OUI
  vendor lookup attached). Migration
  `f1c9a4d2b8e6_ip_role_reserved_mac_history`. Test coverage in
  `tests/test_ip_role.py`, `tests/test_mac_history.py`,
  `tests/test_reservation_sweep.py`.

- **IPAM subnet operations — find-free + split + merge.** Three
  preview-then-commit endpoints under `/ipam/spaces/{id}/find-
  free`, `/ipam/subnets/{id}/split/preview` + `/commit`,
  `/ipam/subnets/{id}/merge/preview` + `/commit`. Find-free
  walks the IPBlock tree for unallocated CIDRs of a requested
  prefix length (with optional `parent_block_id` scope and a
  minimum-free-addresses filter). Split breaks a subnet into
  2^k aligned children at a longer prefix; merge collapses
  contiguous siblings back into one supernet via
  `ipaddress.collapse_addresses`. Both gate non-trivial
  operations on a typed-CIDR confirmation, hold a pg advisory
  lock through commit, and re-validate every constraint pre-
  mutation. Surfaced in the UI via three header buttons on
  the subnet detail (Find Free… / Split… / Merge…) **and** via
  the bulk-action toolbar on the block- and space-level subnet
  tables: select 1 subnet to split, select 2+ to merge.
  Free-space finder also lives on the block detail header
  pre-scoped to that block.

- **`IPSpace` VRF / route-domain annotation.** Three new
  optional columns on `ip_space`: `vrf_name` (≤ 64 chars),
  `route_distinguisher` (ASN:idx or IPv4:idx, no validation —
  vendors disagree), `route_targets` (JSONB list of RT strings).
  Pure metadata — address allocation already supports
  overlapping ranges via separate IPSpace rows; these columns
  give operators somewhere to put the routing identity for
  reporting / export / future BGP-EVPN integration. Migration
  `f1c8b2a945d3_subnet_ops_ipspace_vrf`. Surfaced as badges on
  the IPSpace detail header when set, plus a "VRF / Routing"
  section in the Edit Space modal (open by default since
  operators kept missing it under a collapsed toggle).

- **Soft-delete + 30-day recovery + Trash admin page.**
  `IPSpace`, `IPBlock`, `Subnet`, `DNSZone`, `DNSRecord`, and
  `DHCPScope` rows now inherit a `SoftDeleteMixin` (`deleted_at`,
  `deleted_by_user_id`, `deletion_batch_id`). A global
  `do_orm_execute` event listener injects
  `Model.deleted_at IS NULL` into every SELECT touching one of
  these models — callers that need to see soft-deleted rows opt
  in via `execution_options(include_deleted=True)`. Cascade-
  stamping under one `deletion_batch_id` means restoring a
  subnet brings its DHCP scopes back atomically; restoring a
  zone brings its records back. New endpoints under `/admin/`:
  - `GET /admin/trash` — paginated list across every in-scope
    model, with type / since / `q` substring filters and
    deleted-by user resolution.
  - `POST /admin/trash/{type}/{id}/restore` — atomic batch
    restore with `default_conflict_check` (rejects 409 when a
    live row would clash on the same uniqueness key).
  - `DELETE /admin/trash/{type}/{id}` — hard-delete a row
    that's already soft-deleted.

  Frontend page at `/admin/trash` lists soft-deleted rows
  newest-first, per-row Restore (with confirmation modal +
  conflict-detail rendering) and Delete-permanently buttons.
  Sidebar entry under Admin. Nightly `trash_purge` Celery beat
  task (`app.tasks.trash_purge.purge_expired_soft_deletes`)
  hard-deletes rows past `PlatformSettings.soft_delete_purge_days`
  (default 30; set to 0 to disable purging). Subnet / block /
  space delete confirmation text updated from "permanently
  delete" → "move to Trash. You can restore from Admin → Trash
  within 30 days." since the actual behaviour is soft-delete,
  not hard-delete. IP addresses are intentionally NOT
  soft-deletable — they cascade-delete with their parent
  subnet, and the parent subnet is the recoverable unit.
  Migration `c1f4a8b27d09_soft_delete`.

- **DHCP lease history + NAT mapping table.** New
  `dhcp_lease_history` table records every lease that ever
  expired, was reassigned to a different MAC, or disappeared
  from an absence-delete sweep — gives operators a forensic
  trail when "who had this IP last week" comes up. Written from
  three sites: the `dhcp_lease_cleanup` expiry sweep, the agent
  lease-event ingest path on MAC change, and `pull_leases` on
  absence-delete. Surfaced on the DHCP server detail as a new
  "Lease History" tab with filtering by MAC / IP / time window.
  Daily prune task (`app.tasks.dhcp_lease_history_prune`)
  honours `PlatformSettings.dhcp_lease_history_retention_days`
  (default 90; set to 0 to keep forever).

  New `nat_mapping` table is operator-curated metadata
  describing 1:1 NAT, PAT, or hide-NAT bindings between
  internal and external IPs. SpatiumDDI doesn't render or push
  these rules anywhere — purely IPAM cross-reference: an IP row
  gets a `nat_mapping_count` badge and the dedicated
  `/ipam/nat` page lists / creates / edits / deletes mappings.
  Migration `f4e1d2a09b75_lease_history_and_nat`.

- **Tailscale integration — Phase 2: synthetic tailnet DNS surface.**
  When a `TailscaleTenant` has `dns_group_id` bound, the reconciler
  now also materialises a `<tailnet>.ts.net` `DNSZone` in that
  group and one A / AAAA record per device address. Tailnet domain
  is auto-derived from the first device FQDN (same as Phase 1's
  `tailnet_domain`). Records carry `auto_generated=True` plus a
  new `tailscale_tenant_id` FK on both `dns_zone` and `dns_record`
  (CASCADE on tenant delete) — so deleting the tenant sweeps the
  whole synthetic zone in one shot.
  - **Read-only enforcement.** API blocks `PUT /zones/{id}`,
    `DELETE /zones/{id}`, record CRUD on synthesised zones with a
    422 + explanatory message ("delete the Tailscale tenant or
    unbind its DNS group to release the zone"). UI shows a cyan
    "Tailscale (read-only)" badge near the zone title and disables
    the Edit / Delete / Add Record header buttons. The per-record
    lock badge in the records table now branches on
    `tailscale_tenant_id` to read "Tailscale" instead of "IPAM"
    when the record was synthesised by Tailscale (rather than
    DDNS / IPAM auto-sync).
  - **Diff semantics.** Reconciler compares desired vs. current
    on every pass keyed by `(name, record_type, value)`: new
    records are inserted, removed devices have their records
    deleted. Idempotent — a second sync with the same device list
    creates / deletes nothing.
  - **Conflict safety.** If an operator-managed zone with the
    same name already exists in the bound DNS group, the
    reconciler refuses to claim it (would silently overwrite
    operator records every sync); the collision lands as a summary
    warning, the operator-managed zone is left untouched, and the
    sync still succeeds for the IPAM mirror.
  - **Filtering.** Devices with expired keys (and
    `keyExpiryDisabled=false`) are skipped, matching Phase 1's
    IPAM mirror semantics. Devices with no FQDN, or whose FQDN
    doesn't end in the derived tailnet domain (different tailnet,
    truncated name during onboarding), are skipped without error.
  - **Bonus.** Because we land actual `DNSRecord` rows, the
    existing BIND9 render path picks them up automatically — non-
    Tailscale LAN clients can resolve `<host>.<tailnet>.ts.net`
    through SpatiumDDI's BIND9 with no extra forwarder plumbing.
  - **TTL.** Synthesised records are stamped at 300 s — short
    enough that a stale entry (device reauthed with a different
    IP) falls out of resolver caches within five minutes of the
    next sync.
  - Migration `e6f12b9a3c84_tailscale_phase2_dns`. 5 new reconciler
    tests cover the synthesis happy path + idempotency, diff on
    device disappearance, no-DNS-group skip, operator-zone
    collision refusal, and foreign-FQDN filtering.

- **Query / activity log surface for BIND9 + Kea agents.** Two new
  tabs on the Logs page (`/logs`):
  - **DNS Queries** — BIND9 `query-log` channel content, parsed into
    timestamp / client IP+port / qname / qclass / qtype / flags /
    view columns with the original raw line preserved. Filters: `q`
    (substring match on qname or raw), `qtype`, `client_ip`, time
    `since`, max events. Requires `query_log_enabled` on the
    `DNSServerOptions` row (already a UI toggle); the BIND9 template
    has rendered the channel since the `f8a3c1e7d925` migration —
    this wave just plugs in the read side.
  - **DHCP Activity** — Kea `kea-dhcp4` log content, parsed into
    timestamp / severity / log code / MAC / IP / transaction id
    columns. Filters: severity, log code (`DHCP4_LEASE_ALLOC` etc),
    MAC, IP, time `since`, raw substring search.
  Each tab has its own server picker drawing from the new
  `GET /logs/agent-sources` endpoint (lists `bind9` DNS + `kea`
  DHCP servers).
- **Agent push pipeline.** The DNS agent gains a `QueryLogShipper`
  thread that tails `/var/log/named/queries.log` (override via
  `DNS_QUERY_LOG_PATH`), batches up to 200 lines or 5 s of activity
  (whichever first), and POSTs to `POST /api/v1/dns/agents/query-
  log-entries`. The DHCP agent gains a `LogShipper` thread doing the
  same against `/var/log/kea/kea-dhcp4.log` (override via
  `DHCP_LOG_PATH`) → `POST /api/v1/dhcp/agents/log-entries`. Kea's
  rendered config now writes to *both* stdout (existing
  `docker logs` workflow) and the new file with in-process rotation
  (`maxsize=50MB`, `maxver=5`). Both shippers handle file-not-yet-
  present (sleep + retry), inode-change rotation (re-open), and
  transient control-plane errors (drop the batch, never block the
  daemon). Memory cap at 5000 buffered lines per shipper trims the
  oldest half if the control plane is unreachable.
- **Storage + retention.** Two narrow tables `dns_query_log_entry`
  and `dhcp_log_entry` hold the parsed lines (composite indexes on
  `(server_id, ts)`); FK cascade drops a server's entries when the
  server row is removed. Nightly `prune_log_entries` Celery task
  drops rows older than 24 h — query logs are *operator triage*, not
  analytics; longer retention belongs in Loki / a SIEM. Migration
  `d8c5f12a47b9_query_log_entries`. Parser unit tests in
  `tests/test_log_parsers.py` cover IPv4 / IPv6 / view-tagged BIND9
  lines plus stock Kea lease-alloc / decline / packet-trace shapes,
  including the "unparseable line still preserves raw text" path.
  Retention task tests in `tests/test_prune_logs.py`.

- **Tailscale integration (read-only tenant mirror) — Phase 1.**
  Settings → Integrations → Tailscale toggle
  (`integration_tailscale_enabled`) lights up a Tailscale nav item
  in the sidebar. `TailscaleTenant` rows bind per-tenant to one
  IPAM space + optional DNS server group, with a Fernet-encrypted
  PAT (`tskey-api-…`) and the tailnet slug (or `-` for the API
  key's default tailnet). Same 30 s beat sweep + per-tenant
  `sync_interval_seconds` gating (60 s default, 30 s floor) + Sync
  Now button as Proxmox / Docker / Kubernetes; FK cascade on
  tenant delete. The reconciler hits `GET
  /api/v2/tailnet/{tn}/devices?fields=all` and:
  - Auto-creates the CGNAT IPv4 block (`100.64.0.0/10` by default,
    operator can override per tenant for non-default slices) and
    the IPv6 ULA block (`fd7a:115c:a1e0::/48`) under the bound
    space on first sync, plus one subnet per block. Idempotent —
    subsequent reconciles don't duplicate.
  - Mirrors every device's `addresses[]` (both IPv4 + IPv6) as
    `IPAddress` rows with `status="tailscale-node"`, hostname =
    device FQDN (`<host>.<tailnet>.ts.net`), description carrying
    OS + client version + user, and `custom_fields` for tags,
    authorized flag, last seen, expires, advertised + enabled
    routes, key-expiry-disabled, update-available, plus the stable
    Tailscale device + node IDs.
  - Skips devices whose Tailscale node-key has expired by default
    (`skip_expired=True`); devices with `keyExpiryDisabled=true`
    (long-lived servers / appliances where the operator has turned
    expiry off) are kept regardless of what the `expires`
    timestamp says, since Tailscale leaves a frozen / ignored
    value on the field. Tailscale's `0001-01-01T00:00:00Z`
    sentinel for "never expires" is correctly interpreted as
    not-expired.
  - Auto-derives the tailnet domain (e.g. `rooster-trout.ts.net`)
    from the first device FQDN — no separate config field.
  - Claim-on-existing + `user_modified_at` lock semantics match
    the Proxmox path: pre-existing operator rows in the CGNAT
    block get adopted (FK stamped) with the lock set, so operator
    edits to hostname / description / status / mac survive every
    subsequent reconcile. Custom fields stay reconciler-owned
    because the tailnet metadata (last_seen, version, route list)
    is most useful when fresh.
  - Un-claim-on-disappear preserves operator-edited rows when the
    upstream device goes away — releases the FK rather than
    deleting the row, mirroring Proxmox/Docker/K8s behaviour.
  - Setup guide in the admin page walks the operator through PAT
    generation in the Tailscale admin console and explains the
    `-` shorthand for default tailnet. Test Connection probe hits
    `/devices?fields=default` for a cheap reachability + auth
    check before save.
  - Migration `c4e1a87b3920_tailscale_integration` adds the
    `tailscale_tenant` table + `tailscale_tenant_id` provenance
    FK on `ip_address` / `ip_block` / `subnet` (CASCADE on tenant
    delete) + `integration_tailscale_enabled` on
    `platform_settings`.
  - 14 unit tests covering tailnet-domain derivation edge cases
    plus the full reconciler diff: block/subnet idempotency,
    multi-address mirroring, expired-device skip + the
    `0001-01-01` sentinel guard, claim-on-existing with the
    `user_modified_at` lock, and the lock-vs-unlock branches in
    the un-claim-on-disappear path.

### Changed

- **Dashboard headline KPIs restricted to IPv4.** "Allocated IPs"
  and "Utilization %" now compute over IPv4 subnets only. A single
  IPv6 /64 carries 2^64 hosts and was swamping the totals across
  every IPv4 subnet combined, making the headline numbers
  meaningless. Per-subnet utilisation, the heatmap, and the IPv6
  subnet count remain — IPv6 stays first-class everywhere it's
  meaningful, just not in capacity-planning rollups. KPI labels
  updated to "Allocated IPs (IPv4)" / "Utilization (IPv4)" so the
  scope is explicit.

- **VRF / Routing section open by default in Edit Space modal.**
  Previously collapsed under a toggle that operators kept missing;
  now expanded by default so the fields are visible the first time
  you open the modal. Operators who don't run multiple VRFs can
  collapse it with the toggle.

- **VLAN page "New VLAN" button promoted to page header.**
  Previously a small inline button above the VLANs sub-table that
  was easy to miss; now lives in the router page header alongside
  Edit / Delete as a `HeaderButton variant="primary" icon={Plus}`,
  consistent with the create-button placement on every other page.

- **Trash page wrapped in standard admin container** (`h-full
  overflow-auto p-6` + `mx-auto max-w-5xl`) so the table no longer
  spans the entire viewport and truncates the rightmost columns
  on widescreen layouts. Restore now opens a confirmation modal
  with conflict-detail rendering instead of firing the mutation
  directly from the table row.

- **Soft-delete confirmation copy** — every "Delete" dialog whose
  underlying API path soft-deletes (Subnet bulk, Block, Space,
  Subnet single) now reads "move to Trash. You can restore from
  Admin → Trash within 30 days" instead of the misleading
  "permanently delete" wording carried over from the pre-trash
  era. Hard-delete (operator picks Permanent in the Trash modal)
  still says "cannot be restored".

- **K8s + Helm worker / beat liveness probes.** Both manifests
  shipped with no probes for the Celery worker + beat
  Deployments — k8s couldn't detect a hung worker. Worker now
  runs `celery -A app.celery_app inspect ping -d
  celery@${HOSTNAME}` (scoped by hostname so the probe matches
  this specific pod, not any random worker on the broker); beat
  runs `grep -q celery /proc/1/cmdline` (matches the
  docker-compose pattern). API readinessProbe in both k8s/base
  and the Helm chart switched from `/health/live` to
  `/health/ready` (which actually checks DB + Redis
  connectivity) — a pod that can't reach its dependencies is now
  removed from LB rotation instead of returning 5xx. Liveness
  stays on `/health/live` so a transient Postgres blip doesn't
  trigger a pod restart.

- **Dev compose worker / beat healthchecks.** Same overrides the
  prod `docker-compose.yml` had — without them, both services
  inherited the Dockerfile's `/health/live` HTTP probe and
  reported `unhealthy` because they don't run an HTTP listener.

### Security

- **ReDoS hardening on agent log parsers.** CodeQL flagged the
  BIND9 query-line regex (`_QUERY_RE` in
  `app/services/logs/bind9_parser.py`) as polynomial — the
  whitespace + optional view-group repetitions could be coerced
  into quadratic-time matching by a malicious agent shipping a
  crafted line through `POST /api/v1/dns/agents/query-log-entries`.
  Added a 4 KiB length cap (`_MAX_LINE_LEN`) at the top of
  `parse_query_line` before any regex execution; same cap added
  to `parse_kea_line` for parity. A real BIND9 query line is
  bounded by qname (≤ 255 chars per RFC 1035) plus timestamp /
  client / view metadata, so 4 KiB is well above any legitimate
  line. Verified — a 10 KiB pathological input now caps at 4 KiB
  and parses in under 50 ms instead of degrading.

### Fixed

- **Multiple Celery tasks broke under "Future attached to a
  different loop".** `asyncio.run(...)` creates a fresh event
  loop per task invocation; the shared `engine` /
  `AsyncSessionLocal` from `app/db.py` were binding asyncpg
  connections to whichever loop first checked them out, so a
  later task using a pooled connection would crash with a stale
  loop reference. Manifested most visibly in the alerts
  evaluator (fires every 60 s) but lurked across
  `dhcp_lease_cleanup`, `dhcp_lease_history_prune`,
  `ipam_reservation_sweep`, `trash_purge`, `prune_metrics`, and
  `prune_logs` — every newer task that imported
  `AsyncSessionLocal` directly. Added a `task_session()`
  context-manager helper in `app/db.py` that builds a throwaway
  `create_async_engine` + `async_sessionmaker` per call and
  disposes the engine on exit (connection lifecycle now matches
  loop lifecycle). Migrated all seven affected tasks. Existing
  `dhcp_health` and `dhcp_pull_leases` already followed this
  pattern with their own per-task engine; we now have one
  canonical helper.

- **DNS agent stuck in 404 loop after stale server row.** Sync
  loop handled 401 by dropping cached token + signalling stop,
  but treated 404 ("server row deleted on the control plane")
  as a generic "unexpected status" and just logged + retried
  forever. CLAUDE.md non-negotiable was clear that both 401 and
  404 should re-bootstrap from PSK; the DHCP agent already had
  it right. Mirrored the DHCP pattern. Even after sync stopped
  itself, the DNS supervisor only watched `daemon_running()` and
  signal events — heartbeat / metrics / query-log threads kept
  hammering the API with the stale token. Supervisor now adds
  the DHCP-agent-style "die if any thread dies" check; container
  exits with code 2, orchestrator restarts, `ensure_token` sees
  the empty cache, and the agent re-bootstraps from PSK.

- **NAT mappings sidebar nav also lit up the IPAM nav item.**
  React Router's `NavLink` does prefix matching by default;
  `/ipam/nat` matched both the `/ipam` IPAM entry and the
  `/ipam/nat` NAT Mappings entry. Added an `end` prop to
  `NavItem` and set `end: true` on the IPAM nav config so it
  only matches `/ipam` exactly.

- **Free space finder now scoped per block.** `FindFreeModal`
  takes an optional `defaultBlockId` prop that flows into the
  request body's `parent_block_id`; the block-detail toolbar
  passes the current block so search results are pre-restricted
  to candidates inside it. Without this, opening Find Free from
  inside a block searched the whole space and surfaced
  candidates the operator probably didn't want.

- **Integration mirror reconcilers preserve operator edits +
  accept integration-owned statuses on update + don't fake
  bridge gateways.** Three closely-related fixes around the
  Proxmox / Kubernetes / Docker reconcilers, all hitting the
  same scenario (operator has IPAM rows, enables an integration,
  syncs run, things either error or get clobbered):
  - **Status validator now accepts integration values on
    update.** `IPAddressUpdate` hardcoded
    `{available, allocated, reserved, static_dhcp,
    deprecated}` and 422'd anything else, making every
    Proxmox-mirrored row (`status="proxmox-vm"`) un-editable
    from the API/UI. Lifted the sets to module-level constants
    in `app.models.ipam` (`IP_STATUSES_OPERATOR_SETTABLE`,
    `IP_STATUSES_INTEGRATION_OWNED`, `IP_STATUSES`); update
    path now accepts ALL statuses, create + next-IP paths are
    unchanged in spirit (operators shouldn't be hand-creating
    `proxmox-vm` rows).
  - **Operator edits sticky across reconciles.** Added
    `ip_address.user_modified_at` (timestamp, nullable) —
    stamped by the API write path when an operator changes
    hostname / description / status / mac_address. All three
    integration reconcilers consult the column: claim-on-
    existing adopts an operator-owned row at a desired
    (subnet, address) tuple by stamping the FK + `user_modified
    _at = now()`; subsequent edit-skip protects the operator's
    fields; preserve-on-disappear releases the FK rather than
    deleting the row when a guest goes away. Migration
    `f8d4e29b1c75`.
  - **Proxmox bridge stops faking a gateway.** The reconciler
    was treating the PVE host's bridge IP (e.g. `192.168.0.94`
    on `vmbr0.20`) as the network gateway — wrong: in plain-
    bridge deployments PVE is a peer on the LAN, not the
    router. Bridge subnets now land with `gateway=None`; the
    bridge IP becomes a per-PVE-host placeholder row labelled
    with the node name. SDN subnets keep their declared
    gateway (PVE owns L3 there, so the value is real). Subnet
    gateway updates are now no-clobber: integrations only set
    the field when they know a real value, so an operator who
    fixes the upstream gateway on a Proxmox-mirrored subnet
    doesn't see it cleared on every sync.

### Notes

- Phase 2 of the Tailscale integration (synthetic
  `<tailnet>.ts.net` DNS surface) shipped in this cycle (entry
  above). The optional BIND9 forwarder zone for
  `100.100.100.100` remains a roadmap item — see `CLAUDE.md`
  "Future Phases" for the deferred follow-ups.

---

## 2026.04.24-1 — 2026-04-24

Proxmox VE integration release. The headline work is a read-only
PVE endpoint mirror with first-class SDN + VNet-inference support,
plus a per-guest discovery modal so "why isn't this VM showing up
in IPAM?" is a two-click answer instead of a log-trawl. Also bundles
four UX polish fixes (real source IP behind the reverse proxy,
alphabetised Integrations nav, wider Custom Fields page, search-row
amber highlight) and a shared IP-space quick-create component so a
fresh install doesn't dead-end on the integration modals.

### Added

- **Proxmox VE integration (read-only endpoint mirror).** Settings →
  Integrations → Proxmox toggle (`integration_proxmox_enabled`)
  lights up a Proxmox nav item in the sidebar. `ProxmoxNode` rows
  bind per-endpoint to one IPAM space + optional DNS group; a single
  row represents a standalone host *or* a whole cluster — the PVE
  REST API is homogeneous across cluster members, so one endpoint
  surfaces the full cluster state via `/cluster/status` and
  `/nodes`. Auth is API-token only (no password, no cookie+CSRF):
  operators issue a read-only token with `PVEAuditor`, paste the
  `user@realm!tokenid` + UUID secret into the admin page's setup
  guide, and hit Test Connection. Same 30 s beat sweep + per-node
  `sync_interval_seconds` gating + on-demand Sync Now as Kubernetes
  / Docker, plus FK cascade on endpoint delete. Mirrors:
  - **SDN VNets** (`/cluster/sdn/vnets` + per-vnet `/subnets`) →
    `Subnet` named `vnet:<vnet>`, with the declared gateway. This
    is the authoritative source when the operator runs PVE SDN:
    the backing bridge often doesn't carry a host IP (split-
    responsibility setup where a router upstream owns L3), so the
    bridge pass alone would miss every overlay VLAN. SDN wins over
    a bridge advertising the same CIDR because the VNet label
    carries the operator's intent. PVE without SDN installed
    returns 404 on the endpoint — the reconciler treats that as
    "no SDN configured" and moves on.
  - **VNet subnet inference** (opt-in via the endpoint's new
    `infer_vnet_subnets` toggle, default off) — when a VNet exists
    in `/cluster/sdn/vnets` but has no declared subnets, the
    reconciler derives a CIDR from the guests attached to that
    VNet. Priority order: exact `static_cidr` from a VM's
    `ipconfigN` (gateway from the accompanying `gw=`) or an LXC's
    inline `ip=`/`gw=`; falling back to a /24 guess around
    guest-agent runtime IPs. The /24 fallback is speculative and
    logs a warning with a `pvesh create` hint; operators running
    /23 or /25 should declare SDN subnets properly instead. Solves
    the common "PVE is L2 passthrough, gateway lives on an
    upstream router" layout where operators have 14 VNets but
    zero declared subnets and have to chase each CIDR by hand
    today. Migration `e5a72f14c890`.
  - **Bridges + VLAN interfaces with a CIDR** → `Subnet` (nested
    under enclosing operator blocks when present, otherwise under
    an auto-created RFC 1918 / CGNAT supernet). Bridges without a
    CIDR are skipped — they're the common L2-span case and would
    pollute IPAM with empty subnets.
  - **VM NICs** → `IPAddress` with `status="proxmox-vm"`, hostname
    = VM name, MAC from `netN` config. Runtime IP comes from the
    QEMU guest-agent (`/nodes/{n}/qemu/{vmid}/agent/network-get-interfaces`)
    when the agent is enabled + running; falls back to the
    `ipconfigN` static IP; otherwise the NIC contributes no row.
    Link-local and loopback IPs are stripped from the agent
    response so `fe80::…` / `127.0.0.1` don't land in IPAM.
  - **LXC NICs** → `IPAddress` with `status="proxmox-lxc"`,
    hostname = container hostname (or name fallback), MAC from
    config. Runtime IP comes from `/nodes/{n}/lxc/{vmid}/interfaces`
    when the container is running; falls back to the inline
    `ip=` value on the netN config.
  - **Bridge gateway IPs** → `reserved`-status `IPAddress` per
    subnet, matching the LAN placeholder shape used by
    operator-created subnets.
  - **Mirror toggles default ON** (`mirror_vms` + `mirror_lxc`) —
    unlike Docker containers (CI-ephemeral, noisy), PVE guests are
    typically long-lived operator inventory, so the integration is
    useful without flipping anything extra after setup.
  Minimal `httpx`-based client (no `proxmoxer` / `pveapi-py` dep).
  Admin page at `/proxmox` with a copy-paste `pveum` token-setup
  guide, Test Connection probe that distinguishes 401 / 403 / TLS
  / connect errors with human-readable messages, and Sync Now
  button per endpoint. Dashboard Integrations panel grows a
  Proxmox column when the toggle is on, with the same
  green/amber/red staleness dot + click-through to the admin page.
  Migration `d1a8f3c704e9`. 38 tests covering parse helpers, SDN
  subnet pipeline, VNet inference (both static-CIDR + runtime-IP
  paths), discovery payload shape, and cascade delete.

- **Proxmox discovery modal** — the reconciler now persists a
  `last_discovery` JSONB snapshot on every successful sync
  containing (a) category counters (VM agent reporting / not
  responding / off, LXC reporting / no IP, SDN VNets resolved /
  unresolved, addresses skipped because no subnet encloses them)
  and (b) a per-guest list with a single top-level `issue` code
  + operator-facing `hint`. New magnifier button on each endpoint
  row opens a "Discovery — {endpoint}" modal: counter pills along
  the top, filter bar (`Issues (N)` / `All (N)` / per-issue tabs),
  search box, and a filterable table with agent-state pills +
  IPs-mirrored split (`N from agent / M from static`) + inline
  hints like "install qemu-guest-agent inside the VM:
  `apt install qemu-guest-agent && systemctl enable --now
  qemu-guest-agent`". Default filter is `Issues` so operators land
  directly on what needs attention. Migration `e7b3f29a1d6c`.

- **Shared `IPSpacePicker` component with inline quick-create.**
  Proxmox / Docker / Kubernetes endpoint modals all require an IPAM
  space; operators on a fresh install had to cancel out of the
  endpoint form, create a space on the IPAM page, and come back.
  The picker wraps the select with a `+ New` button that opens a
  minimal quick-create modal (name + description + colour only —
  DNS/DHCP defaults still live on the full IPAM page). On success
  the new space auto-selects in the outer form. Lives at
  `frontend/src/components/ipam/space-picker.tsx`; wired into all
  three integration pages.

### Fixed

- **Source IP in audit log behind the reverse proxy.** The backend
  already captured `request.client.host` into `AuditLog.source_ip`
  and the audit UI already surfaces the field, but every deployment
  behind the frontend nginx container was logging the nginx IP
  instead of the real user IP. Uvicorn now runs with
  `--proxy-headers --forwarded-allow-ips=*` in `backend/Dockerfile`
  so the ASGI scope's client host is populated from the
  `X-Forwarded-For` header that nginx already sends. Wildcard is
  safe — only nginx can reach the api container on the compose /
  k8s network.

- **Settings → Integrations: stable alphabetical order.** Within
  the Integrations sidebar group, entries now sort by title so
  Docker appears before Kubernetes regardless of source-file
  order. Other groups (IPAM, DNS, DHCP) keep their declared
  ordering — alphabetisation is intentionally scoped to the
  integrations cluster where "which comes first" isn't meaningful.

- **Custom Fields settings page width.** The CF page was capped at
  the same `max-w` as the narrow single-column settings panes,
  which truncated the rightmost columns on the CF table. Bumped to
  the wide-table cap used by the roles + audit pages.

- **Search-result row highlight actually fires + stays amber +
  one-shot.** The amber highlight on subnet-detail navigation from
  global search had three bugs stacked on top of each other:
  `useStickyLocation` was calling `navigate(…, {replace: true})`
  which dropped `location.state`; `selectSubnet` was then calling
  `setSearchParams(…, {replace: true})` on the detail view which
  dropped what remained; and the CSS animation used
  `animation-fill-mode: none` so the amber faded back out instead
  of staying visible. All three patched — the highlight now fires
  the first time only, paints the row amber for ~2 s with a hold
  until the user clicks elsewhere, and clears when navigating
  between subnets.

- **Proxmox settings toggle actually persists.** The settings
  router's Pydantic response + update schemas were missing
  `integration_proxmox_enabled`, so toggling it on in the UI
  silently round-tripped to `false` on save. Added to both schemas
  — the Kubernetes and Docker toggles were already correct.

### Changed

- **Proxmox, Kubernetes, Docker endpoint modals now embed the new
  IPSpacePicker**, replacing the plain `<select>` that previously
  listed only existing spaces. Operators can create a new IPAM
  space without leaving the endpoint form.

---

## 2026.04.22-1 — 2026-04-22

Integrations-heavy release. The headline work is **Docker** and
**Kubernetes** read-only mirror integrations that pull host/cluster
network state into IPAM automatically, plus three dashboard additions
(platform-health card, integrations panel, collapsible sidebar) that
make the control plane easier to eyeball at a glance. Also bundles
the multi-target audit-forwarding rewrite, the ACME DNS-01 provider
for external certbot / lego / acme.sh clients, the DHCP MAC
blocklist, self-contained DNS+DHCP traffic charts on the dashboard,
and runtime-version + GitHub-release-check wiring.

### Added

- **Dashboard: Platform Health card.** Five-up grid showing the live
  status of every control-plane component SpatiumDDI ships — API,
  PostgreSQL, Redis, Celery workers, Celery beat. Per-component dot
  (green / amber / red) + one-line detail ("SELECT 1 in 1 ms",
  "1 alive", "last tick 5s ago"). Worker list surfaces on hover.
  Backed by a new `/health/platform` endpoint that probes each
  piece — worker liveness via `celery_app.control.inspect().ping()`
  in a threadpool with a 3 s outer timeout (so a dead broker can't
  hang the call), beat liveness via a new
  `app.tasks.heartbeat.beat_tick` task that writes
  `spatium:beat:heartbeat` to Redis every 30 s with a 5-minute TTL
  and folds the key's age into ok (≤90 s) / warn (>90 s) / error
  (missing). Endpoint always returns 200 with per-component status
  so partial failures surface without the UI losing the whole card.
  Runtime-agnostic — same output on Docker Compose, Kubernetes, or
  bare metal.

- **Dashboard: Integrations panel.** Appears when Kubernetes or
  Docker integrations are enabled. Two columns (one per enabled
  integration type) with one row per registered cluster / host:
  status dot that folds `last_sync_error` + staleness into a single
  green (synced recently) / amber (stalled > 3× interval) / red
  (sync error) / gray (disabled or never synced) signal, name,
  endpoint, node / container count, humanized last-synced age.
  Section header click-throughs to `/kubernetes` / `/docker` full
  pages. Panel auto-hides when both integration toggles are off —
  default deployments stay clean.

- **Sidebar: collapsible sections + Core header.** All three
  sections now carry an uppercase chevron-header — **Core** (was
  unlabeled), **Integrations**, **Admin** — and each toggles
  open/closed independently. Per-section expanded state persists to
  `sessionStorage` via the existing `useSessionState` helper so it
  survives in-session navigation but not tab close. Collapsed-
  sidebar mode still uses separator lines (no labels to hide at
  that width). Groundwork for adding more sections without the
  sidebar becoming a wall of links.

- **Docker integration (read-only host mirror).** Settings →
  Integrations → Docker toggle (`integration_docker_enabled`)
  lights up a Docker nav item in the sidebar. `DockerHost` rows
  bind per-host to one IPAM space + optional DNS group; bearer-
  equivalent secret is the Fernet-encrypted TLS client key. Two
  transports: **Unix socket** (requires mounting
  `/var/run/docker.sock` into the api + worker containers) and
  **TCP+TLS** (CA bundle + client cert + client key). Same
  reconciler pattern as Kubernetes Phase 1b: 30 s beat sweep,
  per-host `sync_interval_seconds` gating, on-demand
  `sync_host_now` for the UI's Sync Now button, FK cascade on
  host delete. Mirrors **every Docker network** into IPAM
  (smart parent-block detection when an enclosing operator block
  exists), with network gateway stamped as a `reserved` IP row so
  the subnet looks like a normal LAN. `bridge` / `host` / `none`
  / `docker_gwbridge` / `ingress` skipped by default; Swarm
  overlay networks always skipped. **Container mirroring is
  opt-in** per host (`mirror_containers`, default off) — matches
  the `mirror_pods` shape on the k8s side. When on, each
  container's IP lands as `status="docker-container"` with
  hostname = either `<compose_project>.<compose_service>` (via
  the `com.docker.compose.*` labels) or the container name.
  Stopped containers skipped unless
  `include_stopped_containers=true`. Minimal `httpx`-based
  client (no `docker` SDK dep). Migration `c9e2b0d3a5f7`.
  Frontend admin page at `/docker` with setup guide (copy-paste
  TCP+TLS `daemon.json` or unix-socket compose mount snippet),
  Test Connection probe, and Sync Now button. SSH transport
  (`ssh://user@host`) deferred.

- **Kubernetes integration — Phase 1a scaffolding + 1b read-only
  reconciler.** Settings → Integrations is a new settings section;
  per-integration toggle `integration_kubernetes_enabled` drives
  whether the Kubernetes nav item appears in the sidebar. Per-
  cluster config lives on `KubernetesCluster` rows bound to exactly
  one IPAM Space (required) + optionally one DNS server group.
  Bearer token Fernet-encrypted at rest; CA bundle optional
  (system CA store used when empty, for cloud-managed clusters).
  Admin page at `/kubernetes` with setup guide (YAML +
  ServiceAccount / ClusterRole / ClusterRoleBinding / Secret +
  `kubectl` extract commands) shown in the Add modal, Test
  Connection probe that calls `/version` + `/api/v1/nodes` on the
  apiserver and distinguishes 401 / 403 / TLS / network errors
  with human-readable messages. Migrations `f8c3d104e27a`
  (`kubernetes_cluster` table + toggle) and `a917b4c9e251`
  (`kubernetes_cluster_id` FK on `ip_address`, `ip_block`,
  `dns_record` with `ON DELETE CASCADE`). Every 30 s Celery beat
  tick sweeps every enabled cluster whose
  `sync_interval_seconds` (min 30) has elapsed and runs the
  reconciler: pod CIDR + service CIDR become `IPBlock`s under the
  bound space; node `InternalIP`s become `IPAddress` rows with
  `status="kubernetes-node"`; `Service`s of type `LoadBalancer`
  with a populated VIP become `IPAddress` with
  `status="kubernetes-lb"` and hostname `<svc>.<ns>`; `Ingress`
  hostnames become A records (or CNAME if the LB surfaces a
  hostname rather than an IP) in the longest-suffix-matching zone
  in the bound DNS group. Create / update / **delete** semantics
  — removed cluster objects immediately drop their mirror rows
  (not orphaned). Deleting the cluster itself cascades every
  mirror via the FK. "Sync Now" button per cluster fires the same
  reconciler on demand, bypassing the interval. Covered end-to-end
  by `backend/tests/test_kubernetes_reconcile.py` with the k8s
  client stubbed — 9 tests across block diff, node mapping, LB
  VIP mapping, Ingress → A, Ingress → CNAME, zone-miss skipping,
  subnet-miss skipping, and cascade delete.
  **Deferred follow-ups**:
  - Pod IP mirroring (deliberately out of scope — pods churn;
    CIDR-as-IPBlock is the value).
  - external-dns webhook provider protocol (Phase 2 — separate
    feature).
  - Service annotation-driven DNS (`external-dns.alpha.kubernetes.io/hostname`
    on non-Ingress Services) — trivially additive on top of the
    existing Ingress path once a user asks for it.
  - ClusterRoleBinding check in Test Connection (detects the common
    "applied the SA but forgot the binding" case by probing
    `/apis/networking.k8s.io/v1/ingresses` explicitly).

- **Runtime version reporting + GitHub release check.** The sidebar
  footer now shows the actual running version instead of the
  hardcoded `0.1.0` that came from `package.json`. Mechanism: the
  release workflow tags Docker images with the git tag, and
  operators pick a tag via `SPATIUMDDI_VERSION` in their `.env`.
  That value flows through `docker-compose.yml` to a `VERSION` env
  var on the api/worker/beat containers, and through a
  `VITE_APP_VERSION` build arg on the frontend Dockerfile (now
  honored) as a build-time fallback. New public endpoint `GET
  /api/v1/version` returns `{version, latest_version,
  update_available, latest_release_url, latest_checked_at,
  release_check_enabled, latest_check_error}`. A daily Celery beat
  task `app.tasks.update_check.check_github_release` queries
  `api.github.com/repos/{github_repo}/releases/latest`
  unauthenticated (60/hour rate limit is plenty for a daily tick),
  compares the tag against the running version (CalVer
  lexicographic compare; `dev` is treated as outdated vs any real
  tag), and stores `latest_version`, `update_available`,
  `latest_release_url`, `latest_checked_at`, and `latest_check_error`
  on `PlatformSettings`. Gated by the existing
  `github_release_check_enabled` flag — air-gapped deployments
  flip it off and the task no-ops. When an update is available,
  the sidebar shows a small "update" pill linking directly to the
  release notes page. Migration `e5b21a8f0d94`.

- **DHCP MAC blocklist at the server-group level.** Block a MAC
  address from getting a lease anywhere in the group. Covers both
  Kea (rendered into Kea's reserved `DROP` client class via the
  ConfigBundle → agent path, packets are silently dropped before
  allocation) and Windows DHCP (`Add-DhcpServerv4Filter -List Deny`
  pushed over WinRM by a 60 s Celery beat task that diffs desired
  against the server's current deny-list). Group-global: one entry
  blocks the MAC on every scope served by every member of the
  group — no per-subnet pinning. Per-entry fields: `mac_address`,
  `reason` (`rogue` / `lost_stolen` / `quarantine` / `policy` /
  `other`), `description`, `enabled` (soft-disable without losing
  history), `expires_at` (nullable; expired rows stay in the DB
  but are stripped from the rendered config), `created_at` /
  `created_by_user_id` / `updated_by_user_id` / `last_match_at` /
  `match_count`. List reads enrich each row with the OUI vendor
  name (via the existing `oui_vendor` table) and an IPAM cross-
  reference (any `IPAddress` rows currently tied to the blocked
  MAC, with IP + subnet + hostname). Admin UI under the DHCP server
  → "MAC Blocks" tab with filter-as-you-type over MAC / vendor /
  IP / hostname, reason pills, status pill, IPAM links, expiry
  formatting, and a modal for add / edit. MACs accept the common
  operator formats (colon, dash, dotted, bare hex) and canonicalize
  to colon-lowercase server-side. CRUD endpoints: `GET/POST
  /api/v1/dhcp/server-groups/{gid}/mac-blocks`, `PUT/DELETE
  /api/v1/dhcp/mac-blocks/{id}`. Permission gate on
  `dhcp_mac_block` (builtin "DHCP Editor" role gets it
  automatically). Migration `d4a18b20e3c7_dhcp_mac_blocks`.
  Covered by `backend/tests/test_dhcp_mac_blocks.py` (model,
  bundle-filter on enabled + expiry, API round-trip, validation
  rejections) and four new Kea-renderer cases in
  `agent/dhcp/tests/test_render_kea.py` (DROP emission, empty
  list skip, invalid-entry resilience, user-defined `DROP` not
  clobbered). **Deferred follow-ups:** bulk import from CSV,
  per-scope restriction (Kea supports class/pool pinning; Windows
  doesn't), `last_match_at` wiring from Kea lease-event hooks +
  Windows DHCP FilterNotifications event log.

- **Multi-target audit forwarding + pluggable wire formats.** The
  single-syslog + single-webhook slot on `PlatformSettings` is
  replaced by a dedicated `audit_forward_target` table: one row per
  destination with independent transport, format, and filter. Five
  syslog output formats — `rfc5424_json` (current default),
  `rfc5424_cef` (ArcSight), `rfc5424_leef` (QRadar), `rfc3164`
  (legacy BSD), and `json_lines` (bare NDJSON for Logstash / Vector).
  Three transports — UDP, TCP, **new TLS** (with optional per-target
  PEM CA bundle). Per-target filters `min_severity` and
  `resource_types` cut noisy events before they leave the box.
  Admin UI under Settings → Audit Event Forwarding gains an
  add / edit / delete table plus a **Test** button that sends a
  synthetic event to the target so operators get instant feedback on
  a new collector. Migration seeds one row per previously configured
  flat target so existing deployments keep forwarding through the
  upgrade; the flat columns remain as a fallback for one release.
  Admin-only endpoints: `GET /POST /PUT /DELETE
  /api/v1/settings/audit-forward-targets{/id}` and `POST
  /api/v1/settings/audit-forward-targets/{id}/test`. Migration
  `c7e2f5a91d48_audit_forward_targets`. Covered by 17 new tests in
  `backend/tests/test_audit_forward.py`.

- **Built-in DNS query rate + DHCP traffic charts on the dashboard.**
  Two new time-series cards under the activity row. BIND9 agents
  emit per-60s-bucket deltas of `QUERY` / `QryAuthAns` /
  `QryNoauthAns` / `QryNXDOMAIN` / `QrySERVFAIL` / `QryRecursion`
  pulled from `statistics-channels` XMLv3 on `127.0.0.1:8053`
  (injected into the rendered `named.conf`). Kea agents emit
  deltas of `pkt4-{discover,offer,request,ack,nak,decline,release,
  inform}-{received,sent}` pulled from `statistic-get-all` over
  the existing control socket. Counter resets from a daemon
  restart are detected agent-side and drop one bucket rather than
  emitting a spurious spike. Control plane stores samples in two
  small tables — `dns_metric_sample` + `dhcp_metric_sample` —
  keyed on `(server_id, bucket_at)`. Dashboard reads via
  `GET /api/v1/metrics/{dns,dhcp}/timeseries?window={1h|6h|24h|7d}`
  with server-side `date_bin` downsampling (60 s buckets for ≤24 h,
  5 min for 7 d). Nightly `prune_metric_samples` Celery task
  enforces retention (default 7 days). New `recharts` dep on the
  frontend. Windows DNS/DHCP drivers don't report yet — the card
  shows an empty state explaining where data comes from.
  Migration `bd4f2a91c7e3_metric_samples`.

- **ACME DNS-01 provider — external-client flow.** New
  `/api/v1/acme/` surface implementing the
  [acme-dns](https://github.com/joohoi/acme-dns) protocol so
  certbot / lego / acme.sh can prove control of a FQDN hosted in
  (or CNAME-delegated to) a SpatiumDDI-managed zone and issue
  public certs (wildcards included). Five endpoints: `POST
  /register` (admin, returns plaintext creds once), `POST /update`
  (acme-dns auth via `X-Api-User` / `X-Api-Key`, writes TXT with
  60 s TTL and blocks up to 30 s until the primary DNS server
  acks), `DELETE /update` (idempotent cleanup), `GET /accounts` +
  `DELETE /accounts/{id}` (admin list / revoke). Keeps the two
  most-recent TXT values per subdomain so wildcard + base cert
  issuance works. bcrypt-hashed passwords at rest; optional
  `allowed_source_cidrs` per-account allowlist. Delegation pattern
  documented in [`docs/features/ACME.md`](docs/features/ACME.md)
  with worked certbot / lego / acme.sh examples. New permission
  resource type `acme_account`. Migration
  `ac3e1f0d8b42_acme_account`. Covered by 24 new tests in
  `backend/tests/test_acme.py`.

- **RFC 1918 + CGNAT supernet auto-creation on integration
  reconcile.** When the Kubernetes or Docker reconciler detects
  that a mirrored network is contained in `10.0.0.0/8`,
  `172.16.0.0/12`, `192.168.0.0/16`, or `100.64.0.0/10`, and no
  enclosing parent block exists in the target IPAM space, the
  reconciler now auto-creates the canonical private supernet as
  an unowned top-level block (`Private 10.0.0.0/8`, etc). The
  mirrored subnet then nests under it via the existing smart
  parent-block detection. Unowned = integration-FK null, so the
  supernet survives removal of the integration that caused it
  and can be shared across Docker + Kubernetes + hand-made
  allocations. Applies to IPv4 only; matches `ipaddress.IPv4Network.subnet_of`
  semantics.

- **Block creation accepts strict supernets of existing siblings
  and auto-reparents.** `_assert_no_block_overlap` previously
  rejected any new top-level block that enclosed a sibling with
  409. The rule now admits one specific exception: if the new
  block is a strict supernet of one or more siblings (e.g.
  operator creates `172.16.0.0/12` when `172.20.0.0/16` and
  `172.21.0.0/16` already exist at top level), the new block is
  inserted and the existing siblings are reparented under it in
  the same transaction. Duplicates (same CIDR), strict subsets
  (new block contained in a sibling), and partial overlaps are
  still rejected. Matching behaviour lives in `create_block` +
  `update_block` (the reparent path). 4 new tests in
  `backend/tests/test_ipam_block_overlap.py`.

### Fixed

- **Frontend Docker image fails to build for `linux/arm64`.** The
  frontend Dockerfile's builder stage ran emulated under QEMU for
  each `--platform` target, which on `linux/arm64-musl` triggered
  the npm optional-dependency bug
  ([npm/cli#4828](https://github.com/npm/cli/issues/4828)): the
  committed `package-lock.json` only resolves concrete `node_modules/`
  entries for `@rollup/rollup-linux-x64-{gnu,musl}`, so `npm install`
  on emulated arm64 couldn't find `@rollup/rollup-linux-arm64-musl`
  and rollup bailed with "Cannot find module". Fixed by pinning the
  builder stage to `--platform=$BUILDPLATFORM` (usually amd64) — the
  output `dist/` is static JS/CSS/HTML and platform-independent, so
  the nginx final stage still ships both amd64 and arm64. Surfaced
  during the initial `2026.04.22-1` release build; caught + fixed
  before tag retargeted.

- **Copy-to-clipboard fails on insecure origins (HTTP LAN deploys).**
  `navigator.clipboard.writeText` is only exposed on secure contexts
  (HTTPS or `localhost`), so the API-token reveal modal's Copy
  button silently no-op'd on plain-HTTP LAN deployments served by
  IP / mDNS / Tailscale hostnames. Shared helper now falls back to
  a detached `<textarea>` + `document.execCommand("copy")` when the
  clipboard API is unavailable, so Copy works in every deployment
  topology. Tests cover both the secure-context happy path and the
  fallback path.

- **CodeQL alert #15 (`py/incomplete-url-substring-sanitization`).**
  The ACME account-registration test asserted
  `body["fulldomain"].endswith("acme.example.com")`, which
  CodeQL's URL-host rule flags as an unsafe substring check even
  in test code. Reworked to an exact-match comparison against
  `f"{subdomain}.acme.example.com"` — silences the static analyzer
  and tightens the test (now fails on any unexpected affix between
  the subdomain and zone name, not just on a wrong zone).

- **`audit_forward` crash in Celery workers.** The `after_commit`
  listener fired `loop.create_task(_dispatch(...))` which called
  `_load_targets()` / `_load_forward_config()` against the global
  `AsyncSessionLocal` from `app.db`. Celery wraps each async task
  in its own `asyncio.run()` loop, so the global engine's pool
  held asyncpg connections bound to a previous, now-defunct
  loop — reusing one raised `asyncpg.exceptions.InterfaceError:
  cannot perform operation: another operation is in progress`.
  Fixed by routing those two loaders through a new
  `_ephemeral_session()` helper that creates a short-lived engine
  with `NullPool` (no loop-bound pool state to leak), yields a
  session, and disposes on exit. Surfaced as a loud stack trace
  every time the Docker or Kubernetes reconciler wrote an audit
  row — now silent. (Separate issue not addressed here: forwards
  fired from Celery-committed audit rows may still be cancelled
  mid-HTTP when `asyncio.run` closes the loop; crash-free, but
  delivery is best-effort from reconcile-task context.)

---

## 2026.04.21-2 — 2026-04-21

Large consolidation release covering three waves of Kea HA work:

1. **End-to-end HA shake-out** — four distinct agent bugs surfaced the
   first time we actually brought two Kea peers up against `2026.04.21-1`
   (peer URL hostname resolution, port collision, `ha-status-get`
   removal, bootstrap reload), plus UI polish on the failover UX.
2. **DHCP data model refactor to group-centric** — scopes, pools,
   statics, and client classes now belong to `DHCPServerGroup` not
   `DHCPServer`. HA is implicit when a group has ≥ 2 Kea members. The
   standalone `DHCPFailoverChannel` is dropped. **Breaking** — see the
   Migration section below.
3. **Agent rendering + resiliency** — the Kea config renderer's
   long-standing wire-shape mismatch is fixed (no Kea install before
   this release was actually serving the control-plane-defined scope
   — every reload rendered `subnet4: []`), HA peer-IP drift
   self-heals via a new `PeerResolveWatcher` thread, and the Kea
   daemons run under a supervisor that handles stale PID files, bind
   races, and signal forwarding.

Also ships standalone agent-only compose files for distributed
deployments, and a refresh button on the DHCP server group detail
view.

### Breaking

- **DHCP data model is group-centric.** API surface everything scoped
  at `/dhcp/servers/{id}/...` for config objects moves to
  `/dhcp/server-groups/{id}/...`:
  - `GET/POST /dhcp/servers/{id}/client-classes` → `/dhcp/server-groups/{id}/client-classes`
  - The `/dhcp/failover-channels` CRUD router is **deleted**; HA
    fields live on `PATCH /dhcp/server-groups/{id}`.
  - `/dhcp/subnets/{id}/dhcp-scopes` still works as the IPAM-side
    pivot (same URL), but its request body takes `group_id` instead
    of `server_id`. A new alias `/dhcp/server-groups/{id}/scopes`
    exists for group-first lookups.
  - `DHCPScope.server_id` field dropped from the response; use
    `group_id` instead.
  - `DHCPClientClass.server_id` field dropped; use `group_id`.

- **UI navigation.** The "DHCP Failover" sidebar entry under Admin is
  removed. Old `/admin/failover-channels` URLs redirect to `/dhcp`.
  Configure HA on the DHCP server group from its edit modal.

Why: under the old model, scopes were pinned to one server. Pairing
two servers in a failover channel configured the HA hook but did
**not** mirror scope config — operators had to create every scope
twice and keep them in sync manually. Under the new model, you
configure scopes once on the group and every member renders the same
Kea `subnet4`. This matches how every mature DDI product (Infoblox,
BlueCat, Microsoft DHCP server groups) treats the server-group
abstraction.

### Migration

Alembic migration `e4b9f07d25a1_dhcp_group_centric_refactor` performs
the backfill automatically:

1. Every `DHCPServer` without a `server_group_id` gets a per-server
   singleton group named after the server (existing groupless servers
   keep working — they just become single-member groups).
2. `dhcp_scope.group_id` and `dhcp_client_class.group_id` are
   populated from the owning server's group, de-duplicated when
   multiple servers in the same group had overlapping rows (oldest
   wins by `created_at`).
3. Each existing `DHCPFailoverChannel` collapses into the primary
   server's group: mode + HA tuning copy onto the group, each peer's
   URL copies onto the matching `DHCPServer.ha_peer_url`. If the two
   peers were in different groups, the secondary moves into the
   primary's group (it had to be there anyway for HA to work).
4. The `dhcp_failover_channel` table is dropped.

**Downgrade is shape-only**, not semantic. Scopes / classes on multi-
server groups collapse onto whichever server is first by `created_at`.
Not round-trip safe for production rollback — exists for local dev
reset.

### Added

**DHCP — group-centric**

- **Group-centric DHCP page.** The DHCP tab remains the single
  navigation point; no sidebar item moves. HA tuning (heartbeat / max-
  response / max-ack / max-unacked / auto-failover) lives in the
  server group edit modal, shown only when the group's mode is
  `hot-standby` or `load-balancing`. Server edit modal grows a
  "HA Peer URL" field for Kea servers.
- **`DHCPServerGroup.kea_member_count`** — computed on the
  `/dhcp/server-groups` response so the UI can decide whether to
  render the HA panel without a second round-trip. ≥ 2 means the
  group renders `libdhcp_ha.so` on every peer.
- **`DHCPServerGroup.servers[]`** — member servers are rolled up into
  the group response (id, name, driver, host, status, `ha_state`,
  `ha_peer_url`, `agent_approved`) so the dashboard's HA panel can
  paint one network request instead of N.
- **`GET /dhcp/server-groups/{id}/scopes`** — group-first scope list
  endpoint.
- **Refresh button on the DHCP server group detail view.** Invalidates
  `dhcp-servers` + `dhcp-groups` queries so HA state + mode pill
  repaint on demand after switching HA mode (hot-standby ↔
  load-balancing) instead of waiting on the 30 s auto-refetch.
  Toolbar order aligned with IPAM / DNS:
  `[Refresh] [Edit] [Delete] [+ Add Server]`.
- **HA state pill in group detail server list** — inline per-server
  `ha_state` badge on the group detail view, so you can see live HA
  state for each peer without drilling into the server detail.

**DHCP — HA UX (shake-out)**

- **Refresh button on the failover UI** — invalidates both the
  channels list and the DHCP servers query so per-peer HA state
  updates on demand. Uses the shared `HeaderButton` primitive.
- **HA panel on the dashboard DHCP column.** When any group has ≥ 2
  Kea members, the DHCP column adds a `FAILOVER (N)` section under
  the server list. Each row shows group name + mode + two colored
  state dots (per peer) with the live `ha_state` strings. Green for
  `normal` / `hot-standby` / `load-balancing` / `ready`; amber for
  `waiting` / `syncing` / `communications-interrupted`; red for
  `partner-down` / `terminated`; muted for unknown.
- **Peer URL help text overhaul.** Renamed fields to "Primary server
  URL" / "Secondary server URL", added a highlighted info box
  explaining each URL is that peer's own HA-hook endpoint reachable
  from the other peer, placeholder values now show the compose
  hostnames (`http://dhcp-kea:8000/` etc.).

**Kea agent resiliency**

- **`PeerResolveWatcher`** (new 30 s background thread) re-resolves
  HA peer hostnames and triggers a render + reload if any peer's IP
  has drifted. Closes the long-standing "peer URL goes stale after a
  container restart / pod reschedule" failure mode for good.
- **Daemon supervisor** — both `kea-dhcp4` and `kea-ctrl-agent` now
  run under a retry loop with a 5-in-30 s crash-loop guard. SIGTERM
  traps forward to the live daemon AND flip a stopping flag so the
  loop doesn't retry during container shutdown.
- **Stable sha256-derived `subnet-id`** — Kea keys leases off
  `subnet-id`; using a deterministic hash of the CIDR (rather than an
  enumeration counter) guarantees the same CIDR always gets the same
  id across renders, so Kea's lease database doesn't orphan leases
  on config reload.

**Deployment**

- **`docker-compose.agent-dhcp.yml`** — standalone compose file for a
  Kea agent (or HA pair via `--profile dhcp-ha`) on a host *without*
  the control plane. Requires `SPATIUM_API_URL` + `SPATIUM_AGENT_KEY`
  — enforced at compose-config time so misconfiguration fails before
  pull.
- **`docker-compose.agent-dns.yml`** — companion file for a standalone
  BIND9 agent.
- Both files use bridge networking with lab-friendly host-side port
  remaps (5353 for DNS, 6767 for DHCP) so they don't collide with
  systemd-resolved or a host dhclient. Documented host-networking
  swap for real serving.

### Changed

- **Dashboard HA panel** now queries `/dhcp/server-groups` instead of
  the removed `/dhcp/failover-channels`. Each HA pair renders one
  row with a state dot + name per Kea member. The panel only appears
  when at least one group has ≥ 2 Kea members.
- **`CreateScopeModal`** picks the target group (no longer a server).
  The "inherited DHCP group from IPAM block / space" UX still works
  — we default the group picker rather than filtering a server
  picker.
- **Windows DHCP write-through** fans out across every Windows member
  of the scope's group. Editing a scope in a Windows-only group with
  two servers now pushes the cmdlet to both.
- **`pull_leases`** keys scope lookups by `server.server_group_id`.
  Two Windows DHCP servers in the same group pulling the same subnet
  converge on one scope row (replace-all on pool/static state is
  unchanged).
- **Subnet resize** walks every group hosting a scope for the subnet
  and refreshes every Kea member of each group, not just the
  originating server.
- **Kea agent bootstrap reload is now retried for 15 s** on agent
  restart, covering the Kea-startup race (agent and Kea launch
  together from the entrypoint; the control socket may not exist
  for a second or two). Without this retry, cached bundles never
  got applied on restart and Kea stayed on the baked image config.
- **Group detail "never synced" now driver-aware.** Kea members
  report liveness via `agent_last_seen` (heartbeat-driven); Windows
  DHCP reports via `last_sync_at` (lease-pull-driven). The label
  branches on driver so Kea members stop showing a perpetual
  "never synced" regardless of actually-alive heartbeat state.
- **Compose: DHCP HA is a single-flag opt-in.** `dhcp-ha` profile on
  both `docker-compose.yml` and `docker-compose.dev.yml` adds
  `dhcp-kea-2` as a second Kea agent. Enable with
  `docker compose --profile dhcp --profile dhcp-ha up -d`.
- **`.env.example`** carries an inline `openssl rand -hex 32` hint
  for `SECRET_KEY`; Fernet key generation command normalized from
  `python` → `python3` to match the host binary.
- **Compose dev overlay service naming** — `dhcp-1` / `dhcp-2` →
  `dhcp-kea` / `dhcp-kea-2`, per-service volumes split out so the
  second peer isn't contending for the primary's memfile lease CSV.

### Fixed

**Kea HA core (first shake-out)**

- **Peer URL hostname resolution.** Kea's HA hook parses peer URLs
  with Boost asio and only accepts IP literals. `_resolve_peer_url`
  in the agent's renderer now resolves hostnames (via Docker DNS /
  k8s DNS) before the config is emitted. IPv4/v6 literals pass
  through unchanged.
- **Kea port collision between HA hook and `kea-ctrl-agent`.** Kea
  2.6's HA hook spins up its own `CmdHttpListener` bound to the
  `this-server` peer URL. Collocating on `:8000` raced with
  `kea-ctrl-agent`; second binder died with `Address in use`.
  `kea-ctrl-agent` moved to `:8544`, HA hook owns `:8000`.
- **`ha-status-get` command removed in Kea 2.6.** Agent's
  `HAStatusPoller` was calling the standalone command; Kea 2.6 folded
  HA status into `status-get` under
  `arguments.high-availability[0].ha-servers.local.state`.
  `_extract_state` now accepts both new and pre-2.6 shapes.
- **Bootstrap-from-cache never reloaded Kea.** Cached bundle was
  re-rendered to `/etc/kea/kea-dhcp4.conf` but Kea was not told to
  reload, so it stayed on the Dockerfile-baked config; next long-poll
  returned `304 Not Modified` and no reload followed. Bootstrap now
  issues `config-reload` with a 15 s retry window.
- **PATCH `/dhcp/failover-channels/{id}` 500 on UUID fields.**
  Audit-log payload switched to `model_dump(mode="json", ...)` so
  raw `uuid.UUID` values don't crash JSONB serialization.
- **Missing `kea-hook-ha` package in the Kea image.** Dockerfile
  installed `kea-hook-lease-cmds` but not `kea-hook-ha`; config
  reference to `libdhcp_ha.so` fataled on every reload. Added
  `kea-hook-ha` to the apk install line.

**Kea agent rendering + runtime (biggest latent bug)**

- **Kea agent: scopes + pools now actually render.** The agent's
  `render_kea.py` has been reading `bundle["subnets"]` since the
  first Kea commit (Apr 15), but the control plane has always
  shipped `bundle["scopes"]` with `ScopeDef` fields (`subnet_cidr`,
  `pools` with `{start_ip, end_ip, pool_type}`, `statics` with
  `{ip_address, mac_address}`). The shape mismatch meant every Kea
  config reload emitted `subnet4: []` — no scopes, no pools, no
  leases served. Only surfaced now because HA + a real scope in the
  same bundle hit the empty-subnet path on a fresh install.
  Renderer now consumes the canonical wire shape natively;
  excluded/reserved pools filtered out (those are IPAM-only
  bookkeeping, not Kea pools).
- **Kea HA: peer IP drift now self-heals** (see `PeerResolveWatcher`
  under Added).
- **Kea agent: stale PID files no longer block restart.** Kea only
  removes its own PID file on graceful shutdown — SIGKILL and hard
  crashes leave it behind, and `createPIDFile` refuses to start with
  `DHCP4_ALREADY_RUNNING` / `DCTL_ALREADY_RUNNING`. Entrypoint now
  scrubs `/run/kea/*.pid` both at container start and before each
  supervise-loop retry, so `docker compose restart` (and any signal
  storm) brings Kea back cleanly.
- **Kea agent: daemons now supervised with crash-retry + signal
  forwarding** (see Daemon supervisor under Added).

**DHCP polish follow-ups (group-centric refactor close-out)**

- **`MissingGreenlet` on `GET /dhcp/server-groups`** — `servers`
  relationship on `DHCPServerGroup` eager-loaded (`lazy="selectin"`)
  so serialization of the rollup doesn't lazy-load after the
  session's greenlet context ends.
- **Cache invalidation on scope create from IPAM.** The IPAM → DHCP
  scope creation path invalidates the group-level scope query
  (`dhcp-scopes-group`) + the pool list, so the newly-created
  scope shows up in the DHCP tab without a hard page reload.
- **Group detail "never synced" label fixed for Kea members.** See
  Changed → driver-aware label.

### Docs

- `docs/features/DHCP.md` — rewritten data-model section + HA
  paragraph: scopes live on groups; HA is a property of a group
  with two Kea members.
- `docs/drivers/DHCP_DRIVERS.md` — HA coordination subsection
  covers the group-centric model, port split (8000 HA /
  8544 ctrl-agent), peer URL resolution, `status-get` shape,
  bootstrap reload retry, PeerResolveWatcher, supervised daemons.
- `docs/deployment/DOCKER.md` — new §10 "Distributed Agent
  Deployments" covering the two standalone agent compose files,
  two-VM HA pair, host vs bridge networking.
- `CLAUDE.md` — Kea HA roadmap entry trimmed (scope mirroring,
  peer IP re-resolve, daemon supervisor are all shipped now);
  remaining deferred items (Kea version skew guard, DDNS
  double-write under HA, state-transition UI actions, peer
  compatibility validation, HA e2e test) kept for future work.

### Tests

- `agent/dhcp/tests/test_render_kea.py` — 4 new tests pin the wire
  shape (dynamic-only pools, reservation mapping from `statics`,
  `match_expression` → `test` renaming, stable subnet-id invariance).
- `agent/dhcp/tests/test_peer_resolve.py` — 7 new tests cover the
  watcher: initial seed doesn't fire reload, IP change fires one
  reload, unchanged IP is a no-op, transient DNS failure doesn't
  thrash, IP-literal peers are skipped, empty failover is a no-op,
  `apply_fn` exceptions don't kill the watcher.

---

## 2026.04.21-1 — 2026-04-21

Big feature push — Kea HA failover, OUI vendor lookup, IPv6
auto-allocation, a first-cut alerts framework, the umbrella Helm
chart with OCI publishing, agent-side Kea DDNS + block/space
inheritance, per-server DNS zone serial reporting, API tokens,
audit-event forwarding, near-real-time Windows DHCP lease polling,
plus a healthy batch of infrastructure hardening (dev-stack
healthchecks, Trivy gate, kind-based e2e workflow).

### Added

**DHCP**

- **Kea HA failover channels** — new `DHCPFailoverChannel` model
  pairs two Kea DHCP servers in an HA relationship. Mode
  (`hot-standby` / `load-balancing`), per-peer `kea-ctrl-agent` URL,
  heartbeat / max-response / max-ack / max-unacked tuning, and
  auto-failover toggle live on the channel; each server may belong
  to at most one channel (unique FK constraints). The agent's
  `render_kea.py` injects `libdhcp_ha.so` + `high-availability`
  alongside the existing `libdhcp_lease_cmds.so` hook. Fourth agent
  thread (`HAStatusPoller`) polls `ha-status-get` every ~15 s and
  POSTs state to `/api/v1/dhcp/agents/ha-status` — control plane
  stores it on `DHCPServer.ha_state` + `ha_last_heartbeat_at`.
  Admin UI at **`/admin/failover-channels`** does CRUD; DHCP server
  detail header shows a live colored HA pill. Deferred: state-
  transition actions (`ha-maintenance-start` / `ha-continue` /
  force-sync), peer compatibility validation, per-pool HA scope
  tuning. See [`docs/features/DHCP.md` §14](docs/features/DHCP.md).
- **Near-real-time Windows DHCP lease polling** — beat ticks every
  10 s; interval now stored in seconds (default 15) via
  `PlatformSettings.dhcp_pull_leases_interval_seconds`, so
  operators can tune live cadence in the UI without restarting
  celery-beat. Closes the last agentless-DHCP visibility gap.
- **Agent-side Kea DDNS** — `/api/v1/dhcp/agents/lease-events` now
  calls `apply_ddns_for_lease` after mirroring a lease into IPAM
  and `revoke_ddns_for_lease` before deleting the mirror on
  expire / release. Errors are logged but never block lease
  ingestion.

**DNS**

- **Per-server zone serial reporting** — new `DNSServerZoneState`
  table (unique on `(server_id, zone_id)`); agents POST
  `{zones: [{zone_name, serial}, ...]}` to
  `/api/v1/dns/agents/zone-state` after each successful structural
  apply. Read endpoint
  `GET /dns/groups/{gid}/zones/{zid}/server-state` joins the
  servers for a group with their latest report. Frontend: new
  `ZoneSyncPill` on the zone detail header with 30 s refetch —
  emerald "N/N synced · serial X", amber "1/N drift · target X"
  with per-server tooltip, muted "not reported" for fresh agents.

**IPAM**

- **Full IPv6 `/next-address`** — three strategies at the API
  boundary (`sequential` / `random` / `eui64`) + a subnet-level
  default via the new `Subnet.ipv6_allocation_policy` column. EUI-64
  derives per RFC 4291 §2.5.1 (u/l bit flip + FF:FE insert); random
  uses CSPRNG with collision retry and skips the all-zero suffix
  (RFC 4291 §2.6.1 subnet-router anycast); sequential is a first-
  free linear scan capped at 65k hosts. Pydantic + UI exposed;
  `/next-ip-preview` accepts `?mac_address=` so the UI can show the
  EUI-64 candidate pre-commit. Unit coverage in
  `tests/test_ipv6_allocation.py` pins the RFC 4291 Appendix A
  example.
- **OUI vendor lookup** — opt-in IEEE OUI database fetched by the
  new `app.tasks.oui_update` Celery task on an hourly beat tick;
  task self-gates on `PlatformSettings.oui_lookup_enabled` +
  `oui_update_interval_hours` (default 24 h). Incremental diff-
  based upsert keeps each prefix's `updated_at` meaningful. New
  **Settings → IPAM → OUI Vendor Lookup** section shows source
  URL, toggle, interval, last-updated timestamp, vendor count, and
  a "Refresh Now" modal that polls task state via
  `/settings/oui/refresh/{task_id}` and renders added / updated /
  removed / unchanged counters on completion. IPAM address table +
  DHCP leases show `aa:bb:cc:dd:ee:ff (Vendor)` when enabled; the
  IPAM MAC column filter also matches vendor names so `apple` /
  `cisco` work without knowing the prefix. See
  [`docs/features/IPAM.md` §12](docs/features/IPAM.md).

**Alerts**

- **Rule-based alerts framework (v1)** — new `alert_rule` +
  `alert_event` tables. Two rule types at launch: `subnet_
  utilization` (honours `PlatformSettings.utilization_max_prefix_*`
  so PTP / loopback subnets can't trip the alarm) and
  `server_unreachable` (DNS / DHCP / any). Evaluator opens events
  for fresh matches and resolves on clear; partial index on
  `(rule_id, subject_type, subject_id) WHERE resolved_at IS NULL`
  keeps dedup O(1). Delivery reuses the audit-forward syslog +
  webhook targets. Celery beat fires every 60 s; a
  `POST /alerts/evaluate` endpoint lets the UI force a run.
  Admin page at `/admin/alerts` — rules CRUD + live events viewer
  (15 s refetch) + per-event "Resolve".

**DDNS inheritance**

- **Block + space DDNS inheritance** — `IPSpace` / `IPBlock` now
  carry `ddns_enabled` / `ddns_hostname_policy` /
  `ddns_domain_override` / `ddns_ttl`. `Subnet` / `IPBlock` carry
  `ddns_inherit_settings`. `services/dns/ddns.resolve_effective_
  ddns` walks `subnet → block chain → space` and returns an
  `EffectiveDDNS` with a `source` field for UI / debug. Both the
  hostname resolver and the apply path now consult the effective
  config instead of reading subnet fields directly — fixes the
  "space-level DDNS toggle doesn't cascade" behaviour.

**Auth / API**

- **API tokens with auto-expiry** — CRUD at `/api/v1/api-tokens`;
  `sddi_` prefix branch in `get_current_user`; tokens hashed at
  rest (sha256), shown plaintext exactly once on creation. Admin
  page at `/admin/api-tokens`.
- **Audit-event forwarding** — RFC 5424 syslog (UDP / TCP) and / or
  HTTP webhook. SQLAlchemy `after_commit` listener in
  `services/audit_forward.py`; delivery is fire-and-forget on a
  dedicated asyncio task so audit writes never block on network
  I/O. Configured under **Settings → Audit Event Forwarding** on
  platform-level `PlatformSettings` columns.

**Deployment**

- **Umbrella Helm chart (`charts/spatiumddi/`)** — replaces the
  narrow `charts/spatium-dns/` with a full application chart
  covering API, frontend, Celery worker, Celery beat, the migrate
  Job, Postgres + Redis via Bitnami subcharts, and optional DNS +
  DHCP agent StatefulSets (one per values entry). Chart-owned
  secret preserves `SECRET_KEY` across upgrades via `lookup`.
  Migrate Job runs as a pre-install + pre-upgrade Helm hook.
  Release workflow publishes to
  `oci://ghcr.io/<owner>/charts/spatiumddi` on every CalVer tag
  (CalVer → SemVer normalised: `2026.04.21-1` → `2026.4.21-1`).
  Chart README + NOTES.txt cover install, upgrade, external DB /
  Redis, and agent enablement.

**Infrastructure / CI**

- **Kind-based agent e2e workflow** — new `.github/workflows/
  agent-e2e.yml` spins up a kind cluster, installs the umbrella
  chart with one ns1 DNS agent, port-forwards the API for
  `/health/live`, execs `dig +short version.bind CH TXT` in the
  DNS agent pod, and checks restart count. Fires on
  `agent/**` / `charts/spatiumddi/**` / `backend/**` / `frontend/**`
  PRs + `workflow_dispatch`.
- **Trivy gate enforced** — `exit-code: "0"` → `"1"` with
  `ignore-unfixed: true` on both `build-dns-images.yml` and
  `build-dhcp-images.yml`, so HIGH/CRITICAL CVEs with an available
  fix block image builds. Un-fixed CVEs don't block the pipeline.

**UI polish**

- **IP Space tree interleaves blocks + subnets by network** —
  previously the tree rendered all child blocks first and all
  subnets second, so a block like `10.255.0.0/24` would bubble
  above sibling `/24` subnets regardless of address. Now
  `buildBlockTree()` merges children and sorts by network per
  level. New `lib/cidr.ts:compareNetwork()` + `addressToBigInt()`
  helpers work for both IPv4 and IPv6 at any prefix length.
- **Small-subnet suppression** —
  `PlatformSettings.utilization_max_prefix_ipv4` (default 29) and
  `_ipv6` (default 126). Subnets whose prefix exceeds the max are
  excluded from dashboard utilization counts, the heatmap, Top
  Subnets list, and the `subnet_utilization` alert rule — so
  `/30` / `/31` / `/32` (PTP, loopback) and `/127` / `/128` (RFC
  6164 PTP) no longer skew reporting. Shared
  `lib/utilization.ts:includeInUtilization` predicate.

### Changed

- **ISC DHCP support is now explicitly not supported.** Upstream
  entered maintenance-only mode in 2022 and the ISC team
  recommends Kea as the successor. Removed from the roadmap and
  every doc section, replaced with an explicit "not supported"
  note where the question would otherwise come up
  (`docs/features/DHCP.md`, `docs/drivers/DHCP_DRIVERS.md`). The
  `VALID_DRIVERS` check in the CRUD router rejects `driver:
  "isc_dhcp"` with a clean `422`.
- **Agent sync loop now unwraps the long-poll envelope.** The
  DHCP agent's `_apply_bundle` was passing the full envelope
  (`{server_id, etag, bundle, pending_ops}`) to `render_kea`,
  which expects the inner bundle dict. The agent would render a
  Kea config with no subnets or client classes. Fix: unwrap once
  in `_apply_bundle`, which also makes the new `failover` block
  actually reach the Kea renderer.

### Fixed

- **Frontend nginx cached the api upstream IP.** Recreating the
  `api` container changed its Docker-assigned IP; nginx held the
  stale one from config-load time and every `/api/v1/*` call
  started returning 502. `frontend/nginx.conf` now declares
  `resolver 127.0.0.11 valid=10s ipv6=off` + uses variable-based
  `proxy_pass` so each request re-resolves via Docker's embedded
  DNS. Adds a new `location = /nginx-health` that answers `200 ok`
  directly — no more upstream hop in the healthcheck.
- **Worker + beat healthchecks were wrong.** Both inherited the
  api's `http://localhost:8000/health/live` probe from
  `backend/Dockerfile` but neither process listens on HTTP; both
  kept flipping to `unhealthy`. Overrode in `docker-compose.yml`:
  worker uses `celery -A app.celery_app inspect ping -d celery@
  $HOSTNAME` (broker round-trip); beat uses
  `grep -q 'celery' /proc/1/cmdline`.
- **Frontend healthcheck resolved to IPv6.** Busybox wget prefers
  `::1` for `localhost`; nginx binds `0.0.0.0:80`, so the probe
  returned "Connection refused". Switched to
  `http://127.0.0.1/nginx-health`.
- **CodeQL `actions/missing-workflow-permissions` on agent-e2e**
  — new workflow missed its top-level `permissions:` block.
  Added `contents: read` (least-privilege).

### Docs

- `docs/features/DHCP.md` — §14 rewritten as a real "Kea HA
  failover channels" spec (data model, modes, agent-side rendered
  hook payload, state-reporting cadence, managing channels), and
  Rules & constraints gets a new "Failover channels" subsection.
  §15 "Parent / child setting inheritance" explicitly calls out
  the DDNS inheritance chain.
- `docs/features/IPAM.md` §12 — OUI section rewritten to match
  the shipped behaviour (source URL, gating fields, diff-based
  atomic replace, manual refresh endpoint, inline display).
- `CLAUDE.md` — multiple roadmap status updates: Phase 1 IPv6,
  DDNS agent path, DDNS block/space inheritance, per-server zone
  serial, Trivy-clean + kind e2e, alerts framework (v1), and
  Kea HA (core) flipped to ✅. ACME DNS-01 provider + embedded
  client entries added to Future Phases with full shape.
- `README.md` + `CLAUDE.md` + `docs/drivers/DHCP_DRIVERS.md` +
  `docs/PERMISSIONS.md` — ISC DHCP scrubbed.

---

## 2026.04.20-2 — 2026-04-20

Follow-on polish release. Dark sidebar so the nav is distinct from
content in light mode, per-zone / per-space color tagging, zebra
striping across every long-list table, and a batch of delete-flow
bug fixes turning silent failures into actionable 409s. New
troubleshooting doc + "Rules & constraints" sections in the feature
specs so operators hitting a 409 can jump straight to the
enforcement site.

### Added

**Theme + color**
- Dark sidebar in both themes (`--sidebar-*` CSS tokens wired through
  tailwind.config). In light mode the sidebar is dark slate with a
  white-pill active item so it no longer blends into the page; in
  dark mode it sits slightly darker than content for separation.
- `DNSZone.color` (migration `f4a9c1b2d6e7`) and `IPSpace.color`
  (migration `a5b8e9c31f42`). Curated 8-swatch set
  (slate/red/amber/emerald/cyan/blue/violet/pink) — free-form hex
  is deliberately rejected so every choice stays legible in both
  themes. Zones render a colored dot on tree rows, list rows, and
  the zone detail header. Spaces paint the tint as the *row
  background* (since spaces sit at the top of the tree); selection
  uses a `ring-1` so the color stays visible when the space is
  selected. Closes [#20].
- Shared `<SwatchPicker>` (`components/ui/swatch-picker.tsx`) + the
  `SWATCH_COLORS` / `swatchCls` / `swatchTintCls` helpers in
  `lib/utils.ts` so DNS and IPAM stay coherent.

**Zebra striping across long-list tables**
- `zebraBodyCls` utility applied to every substantial `<tbody>`:
  IPAM addresses / blocks / subnets / aliases, DNS zones / records,
  DHCP scopes / pools, VLANs, Users / Groups / Roles / Audit, and
  the Logs grid.
- Uses `bg-foreground/[0.05]` + hover `bg-foreground/[0.09]` instead
  of `bg-muted/40`. The old `muted` tint in light mode was only ~4%
  lightness darker than white and effectively invisible; the
  foreground-based tint gives consistent contrast in both themes.

**Docs**
- New `docs/TROUBLESHOOTING.md` covering the recovery recipes that
  aren't obvious from the feature specs: accidentally deleting a
  DNS / DHCP server from the UI (agent auto-rebootstraps via PSK on
  404; manual escape is wiping `agent_token.jwt` + `agent-id` and
  restarting), admin-password reset, and the new subnet-delete 409
  behaviour.
- "Rules & constraints" sections added to `IPAM.md` / `DHCP.md` /
  `DNS.md` / `AUTH.md`. Each rule: one-line intent, short
  why-it-exists where non-obvious, and `file:line` + HTTP status so
  operators can jump from a response `detail` to the enforcement
  site. ~100 rules across the four domains (delete guards, overlap
  checks, pool / collision rules, enum validators, Windows
  push-before-commit).
- `CLAUDE.md` doc map gains the TROUBLESHOOTING.md entry.

### Changed

- **Subnet delete is now refused when non-empty.** The endpoint used
  to cascade silently (wiping IPs + scopes with the subnet); it now
  returns `409` with a breakdown (*"Subnet is not empty: N allocated
  IP addresses, M DHCP scopes"*) matching the existing block-delete
  behaviour. Opt into the cascade with `?force=true`; the pre-delete
  WinRM remove-scope + Kea bundle rebuild still run either way so
  nothing is orphaned on a running server.
- Dashboard live-activity column widths. Long audit action names
  like `DHCP.SERVER.SYNC-LEASES` were breaking on the hyphen and
  bleeding into the adjacent resource column. Action column widened
  from `w-14` (56px) to `w-36` (144px); resource-type bumped to
  `w-20`.

### Fixed

- **Silent failures across every subnet / block delete path.**
  Single-subnet delete from the tree, single-subnet delete from the
  Edit Subnet modal, block-level bulk subnet delete, space-level
  bulk delete (mixed subnets + blocks), and block delete from the
  tree context menu all now capture 409 responses and render the
  detail inline in `ConfirmDestroyModal`. Bulk paths use
  `Promise.allSettled` + per-item messages so one blocker doesn't
  hide the rest; successes still commit.
- Space color stayed invisible when the space was selected because
  `bg-primary/5` overrode the tint. Selection now uses `ring-1
  ring-primary/60` so the color stays visible alongside the
  selection indicator.
- Multi-line errors in the confirmation modal — `whitespace-pre-line`
  + `max-h-48 overflow-auto` on the error box so long failure lists
  from bulk deletes scroll instead of pushing the buttons off-screen.

[#20]: https://github.com/spatiumddi/spatiumddi/issues/20

---

## 2026.04.20-1 — 2026-04-20

CI-only release to fix the multi-arch build in the release workflow
and publish the previously-missing agent images.

### Fixed

- Release workflow matrix was pushing each platform to the same tag
  separately, so the second push overwrote the first — the resulting
  images had no `linux/amd64` manifest and `docker compose pull`
  failed on amd64 hosts. Switched to a single job with
  `platforms: linux/amd64,linux/arm64` via QEMU so the push produces
  a proper multi-arch manifest list.
- Added `build-dns` and `build-dhcp` jobs so `dns-bind9` and
  `dhcp-kea` images are actually built and published alongside
  `spatiumddi-api` and `spatiumddi-frontend`. These images were
  referenced by `docker-compose.yml` but never produced by the
  release pipeline.

---

## 2026.04.19-1 — 2026-04-19

The **performance, polish, and visibility** release. Batched WinRM
dispatch turns multi-minute Windows DNS / DHCP syncs into a handful of
round trips. A new **Logs** surface exposes Windows Event Log + per-day
DHCP audit files over WinRM with filter / auto-fetch / date-picker UX.
The IPAM tree gains subnet + block **resize** with blast-radius preview,
subnet-scoped IP **import**, **DHCP pool awareness** (pool boundary
rows + dynamic-pool gates + next-IP preview), and **collision warnings**
on hostname+zone / MAC. Sync menu + DHCP sync modal + combined
Sync-All modal replace the silent lease-sync button. Dashboard rebuilt
around a **subnet-utilization heatmap** + live activity feed. Every
modal is now **draggable**; every detail-page header uses the same
**`HeaderButton`** primitive. DDNS (DHCP lease → DNS A/PTR) ships for
the agentless lease-pull path.

### Added

**DDNS — DHCP lease → DNS A/PTR reconciliation**
- Migration `e1f2a3b4c5d6` adds four subnet-level DDNS control fields:
  `ddns_enabled` (default False — opt-in),
  `ddns_hostname_policy` (`client_provided` | `client_or_generated` |
  `always_generate` | `disabled`),
  `ddns_domain_override` (publish into a different zone), and
  `ddns_ttl` (override the zone's TTL for auto-generated records).
  Independent of `DHCPScope.ddns_enabled` — that pair still drives
  Kea's native DDNS hook.
- New service `app/services/dns/ddns.py` with `resolve_ddns_hostname`,
  `apply_ddns_for_lease`, and `revoke_ddns_for_lease`. Static-assignment
  hostnames always win over policy; client hostnames are sanitised to
  RFC 1035 labels and truncated at 63 chars; generated hostnames use
  the last two IPv4 octets (`dhcp-20-5` for `10.1.20.5`) or the low
  32 bits hex for IPv6.
- Integration points: `services/dhcp/pull_leases.py` fires DDNS after
  each mirrored IPAM row (agentless lease-pull path);
  `tasks/dhcp_lease_cleanup.py` calls `revoke_ddns_for_lease` before
  deleting the mirrored row.
- Idempotent — repeat polls over the same lease state are a no-op.
- `_sync_dns_record` is lazy-imported from the IPAM router at call
  time to dodge a module-load cycle.
- `SubnetCreate` / `SubnetUpdate` / `SubnetResponse` + `Subnet`
  frontend type gain the four DDNS fields.
- New `DdnsSettingsSection` React component — enable toggle, policy
  dropdown, domain-override input, TTL input, live preview of what
  `always_generate` would produce for the subnet's first IP. Wired
  into `CreateSubnetModal` and `EditSubnetModal`.
- Docs: `features/DNS.md §7` rewritten to describe the shipped
  implementation (architecture diagram, subnet fields, policy
  semantics, static override, idempotency, enable walkthrough).

**Logs surface — Windows Event Log + DHCP audit (WinRM)**
- New top-level `/logs` page + sidebar entry. Pulls events on demand
  over WinRM from any agentless server that has credentials set — no
  new env vars, no migrations.
- `app/drivers/windows_events.py` — shared helper `fetch_events()`
  builds a `Get-WinEvent -FilterHashtable` script from neutral filters
  (log_name, level 1-5, max_events 1-500, since datetime, event_id).
  Filters run server-side so the full log never crosses the wire.
- Drivers expose log inventory through
  `available_log_names()` + `get_events()`:
  `WindowsDNSDriver` → `DNS Server` (classic) +
  `Microsoft-Windows-DNSServer/Audit`;
  `WindowsDHCPReadOnlyDriver` → `Operational` +
  `FilterNotifications`. Analytical log omitted (noisy, per-query —
  better viewed in MMC).
- `GET /logs/sources` — lists every server with WinRM creds + its
  available log names for the picker.
- `POST /logs/query` — runs the filtered `Get-WinEvent`; 400 on
  missing creds, 502 on upstream PowerShell failure with the Windows
  error surfaced to the UI.
- Dispatch goes through the abstract driver interface — logs router
  never imports `windows_events` directly (non-negotiable #10).
- **DHCP audit tab** — separate endpoint `POST /logs/dhcp-audit`
  reads `C:\Windows\System32\dhcp\DhcpSrvLog-<Day>.log` (the
  CSV-style per-lease trail Windows DHCP writes by default) over
  WinRM. Handles UTF-16 + ASCII encodings; event-code → human label
  map covers documented codes; unknown codes come through as
  `Code <n>` so new Windows releases don't drop silently. Access
  denied / missing file / locked-by-rotation all return `[]` instead
  of 500.
- Frontend: **Event Log | DHCP Audit** tab switcher reusing shared
  `ServerPicker` / `MaxEventsPicker` / `FilterSearch` / `RefreshButton`
  helpers. Audit tab columns: Time / Event (code + label) / IP /
  Hostname / MAC / Description with event-dot colours mirroring
  Windows severity families; event-code distribution picker (e.g.
  `10 — Lease granted (238)`) for targeted filtering; day picker
  defaults to Today with Mon-Sun backfill.
- Auto-fetch via `useQuery` keyed on every filter so page entry +
  filter changes trigger refetch; `staleTime: Infinity` means
  tab-switch doesn't spam the DC. Explicit **Refresh** button calls
  `refetch()` to bypass cache.
- Date picker uses native `<input type="datetime-local">` + `since`
  on `LogQueryRequest`; `×` clear button inline.

**IPAM — subnet + block resize (grow-only, preview + commit)**
- `POST /ipam/subnets/{id}/resize/preview` + `POST /ipam/subnets/{id}/resize`;
  parallel endpoints at `/ipam/blocks/{id}/resize/...`. Shrinking is
  explicitly out of scope — it silently orphans addresses.
- Preview returns a blast-radius summary (affected IPs, DHCP scopes,
  pools, static assignments, DNS records, reverse zones to create)
  + a `conflicts[]` list that disables the commit button when
  non-empty.
- Rules enforced server-side: grow only; same address family; old
  CIDR ⊂ new CIDR; new CIDR fits inside the parent block; no overlap
  with any subnet or block anywhere in the space (cross-subtree
  scan); block children must still fit; per-resource pg advisory
  lock during commit; commit re-runs every validation (TOCTOU guard)
  before mutating; optional gateway-to-first-usable move; rejects at
  preview when the new CIDR has no usable range
  (`/31 /32 /127 /128`).
- Renamed or DNS-bearing placeholder rows are preserved across
  resize; only default-named network/broadcast rows get recreated at
  the new boundaries.
- Reverse-zone backfill runs on commit so `/24 → /23` creates the
  second reverse zone automatically.
- Single audit entry per resize with old → new CIDR + counts.
- UI `ResizeSubnetModal` / `ResizeBlockModal` with typed-CIDR
  confirmation gate. Commit button hidden (not just disabled) on
  conflict so the user has no false sense they can force-commit.

**IPAM — subnet-scoped IP address import**
- Space-scoped importer already existed; the new subnet-scoped flow
  handles the common "export IPs from vendor X, load into
  SpatiumDDI" migration. `POST /ipam/import/addresses/preview` +
  `/commit`.
- Parser auto-routes CSV / JSON / XLSX rows by header:
  `address` / `ip` → addresses, `network` → subnets. Unrecognised
  columns drop into `custom_fields` so other-vendor exports work
  without rename passes.
- Validates each IP falls inside the subnet CIDR; respects
  fail / skip / overwrite strategies; writes audit rows; calls
  `_sync_dns_record` so rows with hostnames publish A + PTR through
  the same RFC 2136 path the UI uses.
- Frontend `AddressImportModal` + a combined `Import / Export`
  dropdown on the subnet header.

**IPAM — IP assignment collision warnings**
- Two non-fatal guardrails on IP create / update:
  **FQDN collision** on same `(lower(hostname), forward_zone_id)`
  across any subnet, and **MAC collision** on same MAC anywhere in
  IPAM.
- Server: new `_normalize_mac` + `_check_ip_collisions` helpers in
  `backend/app/api/v1/ipam/router.py`; `force: bool = False` added
  to `IPAddressCreate` / `IPAddressUpdate` / `NextIPRequest`. When
  `force=false` and the pending assignment collides, the endpoint
  returns 409 with
  `detail = {warnings: [...], requires_confirmation: true}`. Clients
  re-submit with `force=true` to proceed.
- Update path only checks fields the client explicitly set
  (`model_dump(exclude_unset=True)`), so unchanged rows never
  surface a pre-existing collision on unrelated edits;
  `exclude_ip_id` keeps the row from colliding with its own current
  state.
- UI: shared `CollisionWarning` type + amber
  `CollisionWarningBanner` in `IPAMPage.tsx`. Allocate and edit
  modals parse the 409, render one line per collision, and flip the
  submit button to "Allocate anyway" / "Save anyway". Editing any
  collision-relevant field clears the pending warning so the next
  submit re-checks fresh.

**IPAM — DHCP pool awareness**
- IP listing interleaves ▼ start / ▲ end pool boundary rows with
  existing IP rows. Dynamic pools tint cyan, reserved violet,
  excluded zinc. Each marker shows pool name + full range so the
  user sees pool extents even when no IP is assigned inside.
- `create_address` rejects with 422 when `body.address` lands inside
  a **dynamic** pool — the DHCP server owns that range. Excluded /
  reserved pools still allow manual allocation.
- `allocate_next_ip` uses a hoisted `_pick_next_available_ip` helper
  that skips dynamic ranges during its linear search.
- New `GET /ipam/subnets/{id}/next-ip-preview?strategy=...` returns
  `{address, strategy}` without committing.
- `AddAddressModal` "next" mode loads the preview on open and shows
  `Next available: 10.0.1.42 (skips dynamic DHCP pools)` in emerald
  — or a destructive "no free IPs" line with submit disabled when
  exhausted. Manual mode renders an inline red warning + disables
  submit when the typed IP falls in a dynamic range.

**IPAM — Sync menu + DHCP sync modals**
- `[Sync ▾]` dropdown in the subnet detail header with **DNS**,
  **DHCP** (gated on scope presence), and **All** entries. DHCP fans
  out `POST /dhcp/servers/{id}/sync-leases` across every unique
  server backing a scope in this subnet (deduped,
  `Promise.allSettled` so one bad server doesn't mask the others).
- `DhcpSyncModal` — per-server result cards (pending spinner → Done
  / Failed) with a counter grid: active leases, refreshed, new,
  **removed** (deleted on server), IPAM created, **IPAM revoked**.
  `removed` + `ipam_revoked` highlight amber when non-zero —
  they're the rows the stale-lease fix cleaned up. Close disabled
  until every server reports.
- `SyncAllModal` — one modal, two sections. DHCP panel uses the
  same `useDhcpSync` hook + body component. DNS panel fetches the
  existing drift summary (`missing / mismatched / stale / total`)
  and either shows "In sync" or an amber block with a "Review DNS
  changes…" button that chains into the existing `DnsSyncModal`.

**IPAM — refresh buttons**
- `[↻ Refresh]` buttons added to DNS zone records page, IPAM subnet
  detail, and the VLANs sidebar. Each invalidates every React Query
  key the surface consumes.

**Dashboard rewrite**
- Six compact KPI cards (IP Spaces / Subnets / Allocated IPs /
  Utilization / DNS Zones / Servers), tone-coloured left accent
  stripe, hover state, most click through to their module page.
- **Subnet Utilization Heatmap** (hero) — every managed subnet is
  one grid cell coloured by utilization. Auto-fill flow, hover
  tooltip (network + name + %/allocated/total), click opens the
  subnet in IPAM. Header has a colour legend; footer shows avg /
  p95 / hot counts.
- Two-column split: Top Subnets by Utilization + Live Activity
  feed (auto-refreshes every 15 s, action-family colour coding,
  relative timestamps).
- Services panel: two-column DNS + DHCP server list with status
  dots (pulsing for active + enabled) + driver / group /
  last-checked columns.

### Changed

**Batched WinRM dispatch** — the major perf win.
- New `apply_record_changes` on DNSDriver and
  `apply_reservations` / `remove_reservations` / `apply_exclusions`
  on DHCPDriver. Default ABC impls loop the singular method, so
  BIND9 / Kea inherit the batch interface without changes.
- Windows drivers override with real batching. DNS driver ships
  one PowerShell script per zone chunked at `_WINRM_BATCH_SIZE = 6`
  ops — empirically the ceiling given `pywinrm.run_ps` encodes
  UTF-16-LE + base64 through `powershell -EncodedCommand` as a
  single CMD.EXE command line (8191-char cap). Each chunk ships a
  compact data-only JSON payload (short keys
  `i/op/z/n/t/v/ttl/pr/w/p`) + one shared wrapper that dispatches
  per record type with per-op try / catch + JSON result array.
  One bad record doesn't abort the batch.
- DHCP driver batches at `_WINRM_BATCH_SIZE = 30` ops — DHCP
  payloads are leaner so the cmdline limit is further away, but
  capped to stay safe.
- RFC 2136 record ops run in parallel via `asyncio.gather` — cheap
  enough per-op that batching isn't needed but serial was still
  slow.
- `enqueue_record_ops_batch` in `record_ops.py` groups pending ops
  by zone and calls the plural driver method once per group.
- IPAM Sync-DNS stale-delete path switched to batch; DNS tab
  bulk-delete got a real server-side endpoint
  (`POST /dns/groups/{g}/zones/{z}/records/bulk-delete`) so the
  frontend no longer fans out N HTTP requests + the zone serial
  bumps once per batch instead of N times.
- DHCP `push_statics_bulk_delete` groups by (server, scope); the
  IPAM purge-orphans path went from N×M WinRM calls to one per
  server.
- 40-record Sync DNS: 2-3 min → ~5 s.

**Sync menu + combined Sync All** — replaces the per-page "Sync DNS"
button on the subnet detail (see Added). Blocks and spaces keep the
single "Sync DNS" button since they have no DHCP scopes.

**UI consolidation — draggable modals.**
- 7 near-identical `function Modal({...})` definitions (one per
  page) collapsed into a single `<Modal>` primitive at
  `frontend/src/components/ui/modal.tsx`.
- Title bar is a drag handle (`cursor-grab` /
  `active:cursor-grabbing`). Drags starting on buttons / inputs /
  selects / textareas / anchors are ignored — controls in the header
  stay clickable. Backdrop dimmed to `bg-black/20` so the page
  behind the dialog stays readable. Esc closes.
- Custom modal shapes (header with border-b + footer slot) use
  `useDraggableModal(onClose)` + `MODAL_BACKDROP_CLS` from
  `components/ui/use-draggable-modal.ts` (split out so Vite's
  fast-refresh doesn't warn about mixed component / utility
  exports).
- Migrated every standard modal across admin, DNS, DHCP, VLANs,
  IPAM plus `ResizeModals`, `ImportExportModals`, and the inline
  `DnsSyncModal`. Net ~168 lines removed.

**UI consolidation — standardised header buttons.**
- New `<HeaderButton>` primitive
  (`frontend/src/components/ui/header-button.tsx`) with three
  variants (`secondary` / `primary` / `destructive`) on a shared
  `inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm`
  base. Forwards refs + spreads `ButtonHTMLAttributes` for
  disabled / title / onClick without ceremony.
- Logical left → right ordering applied everywhere:
  `[Refresh] [Sync …] [Import] [Export] [misc reads] [Edit] [Resize]
  [Delete] [+ Primary]`. DNS was `text-xs`; DHCP was a mixed
  `text-xs px-3 py-1.5`; VLANs was `text-xs px-3 py-1`. All bumped
  to match IPAM's dominant `text-sm px-3 py-1.5 gap-1.5`.
- Migrated surfaces: IPAM `SubnetDetail` / `BlockDetail` /
  `SpaceTableView`; DNS `ZoneDetailView`; DHCP `ServerDetailView`;
  VLANs `RouterDetail` / `VLANDetail`.

**Kea driver — Dhcp6 option-name translation.** New
`_KEA_OPTION_NAMES_V6` map + `_DHCP4_ONLY_OPTION_NAMES` set;
`_render_option_data` takes `address_family` and routes accordingly.
v4-only options (`routers`, `broadcast-address`, `mtu`,
`time-offset`, `domain-name`, tftp-*) are dropped from v6 scopes with
a warning log instead of being emitted under the wrong space (which
Kea would reject on reload). Scope / pool / reservation /
client-class renderers all thread `address_family` through. Closes
the Phase 1 Dhcp6 TODO.

**Architecture SVG** — added explicit `width="1200" height="820"` +
`preserveAspectRatio="xMidYMid meet"` to the root `<svg>` element.
GitHub's blob view was falling back to ~300px when the image was
clicked through from the README because `viewBox` alone isn't
enough.

**README rewrite — hero + Why section.** Old two-paragraph "What is
SpatiumDDI?" prose split into a punchy tagline ("Self-hosted DNS,
DHCP, and IPAM — one control plane, real servers underneath"), a
new `## Why SpatiumDDI` with 5 scannable bold claims, and a
`## What's in the box` section holding the feature bullets.
Architecture and Getting Started sections untouched.

### Fixed

- K8s worker queue mismatch — `k8s/base/worker.yaml` listed
  `ipam,default`; compose widened to `ipam,dns,dhcp,default` in the
  Windows release. DNS + DHCP health sweeps + scheduled sync tasks
  were silently hanging on K8s.
- Windows DNS TLSA dispatch — `_SUPPORTED_RECORD_TYPES` listed TLSA
  (RFC 2136 handles it fine via dnspython) but `_ps_apply_record`
  raised ValueError for TLSA, so creating a TLSA record on a
  Windows server with credentials failed unpredictably. Added
  `_WINRM_UNSUPPORTED_RECORD_TYPES`; `apply_record_change` now falls
  back to RFC 2136 for those types even when credentials are set.
- `DHCPPage.tsx` lease-sync handler was invalidating
  `["ipam-addresses"]` which matches nothing; changed to
  `["addresses"]` (broad match) so the subnet-level address list
  refreshes after lease sync mirrors new rows.
- Frontend `DHCPPool` type now declares optional
  `existing_ips_in_range` so `CreatePoolModal` no longer needs an
  `as any` cast.
- **DHCP stale-lease absence-delete.** `pull_leases` was upsert-only
  but the Windows DHCP driver only returns *active* leases, so when
  an admin deleted a lease on the server it silently persisted in
  our DB + in IPAM (as `auto_from_lease=True` rows). After the
  upsert loop, `pull_leases` now finds every active `DHCPLease` row
  for this server whose IP wasn't in the wire response and deletes
  both the lease row and its IPAM mirror. `PullLeasesResult` gains
  `removed` + `ipam_revoked` counters; `SyncLeasesResponse` + the
  scheduled-task audit / log lines follow suit. The time-based
  `dhcp_lease_cleanup` sweep continues to handle leases that drift
  past `expires_at` between polls — the two mechanisms overlap
  harmlessly.
- **Sync DNS classifier — PTR overwrite on unassigned forward zone.**
  If a subnet had a reverse zone but the forward zone had been
  unassigned, the classifier built an "expected" PTR value of
  `hostname.` (bare label, no FQDN); existing PTRs got classified
  `mismatched` and the commit rewrote them to the broken value
  instead of deleting them. Now: when only reverse is effective,
  existing PTRs are classified `stale` with reason
  `no-forward-zone` so the sync deletes them.
- **Sync DNS classifier — A-record orphans invisible.** Matching
  bug on the A-record side. Unassigning the forward zone left A
  records orphaned in the old zone, but the classifier's
  `if forward_zone and not is_default_gateway` branch skipped them
  entirely — Sync DNS reported 0 drift. Now:
  `elif not forward_zone and ip_a_records` classifies them `stale`
  for the same reason.
- **Sync DNS cache invalidation.** When Sync-DNS deleted a stale
  record linked to an IPAddress, the cached `ip.fqdn` /
  `ip.forward_zone_id` / `ip.dns_record_id` (A/AAAA/CNAME) and
  `ip.reverse_zone_id` (PTR) stuck around; the UI kept showing the
  old FQDN after the records were gone. The stale-delete path now
  clears the matching cached fields.
- **Agentless bulk-delete silent failure.** `_apply_agentless_batch`
  caught wire failures, marked the op rows failed, and returned
  normally — so `_apply_dns_sync` deleted the DB rows anyway and
  told the user "deleted" while the records were still published on
  Windows. Same hole in `bulk_delete_records`. Both paths now zip
  through the returned op rows and only delete when
  `state == 'applied'`; the rest surface as per-record errors /
  skipped entries.
- **Logs — `EventLogException` handling.** `Get-WinEvent` raises
  `System.Diagnostics.Eventing.Reader.EventLogException` when the
  log name doesn't exist on the target host, when zero events match,
  or when a FilterHashtable key doesn't apply to the log — bypasses
  `-ErrorAction SilentlyContinue`. The shared helper now wraps the
  cmdlet in try/catch, explicitly catches `EventLogException`, and
  falls through to a generic catch matching common "no data / bad
  log" patterns — any of those return `[]` cleanly instead of
  surfacing "The parameter is incorrect" / "not an event log" to the
  UI as a 502. Dropped the bogus
  `Microsoft-Windows-Dhcp-Server/AdminEvents` log name (it isn't a
  real log); remaining `Operational` + `FilterNotifications` pair is
  reliable across Server 2016+.

---

## 2026.04.18-1 — 2026-04-18

The **Windows Server integration** release. Adds agentless drivers for
Windows DNS (Path A — RFC 2136, always available; Path B — WinRM +
PowerShell for zone CRUD and AXFR-free record pulls) and Windows DHCP
(Path A — WinRM lease mirroring + per-object scope / pool / reservation
write-through). IPAM gains full DHCP server-group inheritance parallel to
the existing DNS model, a two-action delete (Mark as Orphan / Delete
Permanently), and a right-click context menu across every top-level
module. Settings gets a per-section "Reset to defaults" button, the two
DNS sync sections were renamed with a layer diagram showing which
boundary each one reconciles, and three new doc sets (Getting Started,
Windows Server setup, DHCP driver spec) land alongside a redrawn
architecture SVG.

### Added

**DNS — Windows Server driver (agentless, Path A + B)**
- `WindowsDNSDriver` (`backend/app/drivers/dns/windows.py`) implementing
  record CRUD for `A / AAAA / CNAME / MX / TXT / PTR / SRV / NS / TLSA`
  over RFC 2136 via `dnspython`. Optional TSIG; GSS-TSIG and SIG(0) are
  Path B follow-ups.
- `AGENTLESS_DRIVERS` frozenset + `is_agentless()` in the DNS driver
  registry. `record_ops.enqueue_record_op` short-circuits straight to the
  driver for agentless servers instead of queueing for a non-existent
  agent; logs a warning when a record op is dropped for lack of a
  primary.
- **Path B (credentials required)** — `DNSServer.credentials_encrypted`
  (Fernet-encrypted WinRM dict, same shape as Windows DHCP) unlocks
  `Add-DnsServerPrimaryZone` / `Remove-DnsServerZone` for zone CRUD and
  `Get-DnsServerResourceRecord`-based record pulls that sidestep the
  AD-integrated zone AXFR ACL which otherwise returns REFUSED. All
  PowerShell paths are idempotent — guard on `Get-DnsServerZone
  -ErrorAction SilentlyContinue` before acting. Record writes still ride
  RFC 2136 to avoid paying the PowerShell-per-record cost.
- **Write-through for zones** — `_push_zone_to_agentless_servers` pushes
  zone create / delete to Windows *before* the DB commit; a WinRM
  failure surfaces as HTTP 502 and rolls back, so DB and server never
  drift. Mirrors the Windows DHCP write-through pattern.
- **Shared AXFR helper** — `app/drivers/dns/_axfr.py` now used by both
  BIND9 and the Windows RFC path. Filters SOA + apex NS and absolutises
  `CNAME / NS / PTR / MX / SRV` targets.
- `POST /dns/test-windows-credentials` — runs
  `(Get-DnsServerSetting -All).BuildNumber` as a cheap probe; wired into
  the server create modal's "Test Connection" button.
- Migration `d3f1ab7c8e02_windows_dns_credentials.py`.

**DHCP — Windows Server driver (agentless, Path A)**
- `WindowsDHCPReadOnlyDriver` (`backend/app/drivers/dhcp/windows.py`)
  speaks WinRM / PowerShell against the `DhcpServer` module. Reads:
  `Get-DhcpServerv4Lease` for lease monitoring,
  `Get-DhcpServerv4Scope` + options + exclusions + reservations for
  scope topology pulls. Writes (per-object, idempotent): `apply_scope`
  / `remove_scope` / `apply_reservation` / `remove_reservation` /
  `apply_exclusion` / `remove_exclusion`.
- `services/dhcp/windows_writethrough.py` pushes scope / pool / static
  edits to Windows before DB commit — same rollback guarantee as the
  Windows DNS path.
- `AGENTLESS_DRIVERS` + `READ_ONLY_DRIVERS` sets on the DHCP driver
  registry. The `/sync` bundle-push endpoint rejects read-only drivers;
  the UI hides "Sync / Push config" and substitutes "Sync Leases" +
  per-object CRUD instead.
- Scheduled Celery beat task `app.tasks.dhcp_pull_leases` (60 s cadence;
  gates on `PlatformSettings.dhcp_pull_leases_enabled` /
  `_interval_minutes`). Upserts leases by `(server_id, ip_address)` and
  mirrors each lease into IPAM as `status="dhcp"` + `auto_from_lease=
  True` when the IP falls inside a known subnet. The existing lease-
  cleanup sweep handles expiry uniformly.
- Admin UX: transport picker (`ntlm` / `kerberos` / `basic` / `credssp`),
  "Test WinRM" button, Windows setup checklist (security group + WinRM
  enablement), partial credential updates that preserve the stored blob
  across transport changes. Agentless servers auto-approve on create.
- Migration `b71d9ae34c50_windows_dhcp_support.py`.

**IPAM → DHCP server-group inheritance**
- New `dhcp_server_group_id` on `IPSpace` / `IPBlock` / `Subnet`, plus
  `dhcp_inherit_settings` on Block / Subnet — mirrors the existing DNS
  pattern.
- Three `/effective-dhcp` endpoints walk Space → Block → Subnet; subnet
  resolution falls through to the space when no block overrides.
- `CreateScopeModal` prefills the server from the effective group,
  restricts the dropdown to that group, and exposes an override
  checkbox. Space / Block / Subnet modals gain a DHCP section parallel
  to `DnsSettingsSection`.
- Migration `a92f317b5d08_ipam_dhcp_server_group_inheritance.py`.

**DNS — bi-directional zone reconciliation**
- Group-level "Sync with Servers" button iterates every enabled server,
  auto-imports zones found on the wire but missing from SpatiumDDI
  (skipping system zones TrustAnchors / RootHints / Cache), pushes
  DB-only zones back via `apply_zone_change`, then pulls records
  (AXFR for BIND9 / Windows Path A, `Get-DnsServerResourceRecord` for
  Path B) and reconciles against DB state. Additive-only — never
  deletes on either side.
- Dedup keys fold `CNAME / NS / PTR / MX / SRV` to canonical absolute
  FQDNs so IPAM-written (FQDN-with-dot) and AXFR-read (bare label)
  values no longer duplicate. Out-of-zone glue records are filtered.

**DNS server enable/disable**
- `DNSServer.is_enabled` — user-controlled pause flag separate from
  health-derived `status`. Disabled servers are skipped by the health
  sweep, bi-directional sync, and the record-op dispatcher.
- Migration `c4e8f1a25d93_dns_server_is_enabled.py`.
- `dhcp_health` + `dns` health tasks refactored to per-task async
  engines (fixes "Future attached to a different loop" when the worker
  queue is widened) and now call `driver.health_check()` for agentless
  drivers so the dashboard stops showing "never checked" for Windows
  DHCP / DNS. Compose worker queues re-widened to
  `ipam,dns,dhcp,default`.

**IPAM — two-action delete + cache propagation**
- Allocated-IP delete now offers two distinct actions: **Mark as
  Orphan** (amber — keeps the row, clears ownership metadata) and
  **Delete Permanently** (destructive). No double-confirm — the two
  coloured buttons are the confirmation.
- Every IPAM mutation that invalidates `["addresses", …]` now also
  invalidates `["dns-records"]`, `["dns-group-records"]`, and
  `["dns-zones"]`. A newly-created PTR shows up in the reverse-zone
  record list without a full page reload.

**Settings — per-section reset + DNS sync renames**
- `GET /settings/defaults` introspects column defaults from the
  `PlatformSettings` model (single source of truth — no frontend drift).
- Per-section **Reset to defaults** button populates only that section's
  fields; Save is still required so the user can back out.
- Renamed *DNS Auto-Sync* → **IPAM → DNS Reconciliation** and
  *DNS Server Sync* → **Zone ↔ Server Reconciliation**. Each gets a
  three-pill layer diagram (IPAM → SpatiumDDI DNS ↔ Windows / BIND9)
  with the relevant arrow highlighted.

**Delete guards + bulk actions + right-click menus**
- IP space, IP block, DNS server group, DHCP server group: **409** on
  delete if populated (plain text error with count).
- Subnet delete now cascades DHCP scope cleanup to Windows
  (`push_scope_delete`) and Kea (`config_etag` bump + `DHCPConfigOp`).
- DNS ZonesTab: compact table replaces card-per-zone; checkbox column
  + bulk delete toolbar. IPAM space table: bulk-select leaf blocks.
- Right-click context menus across IPAM IP rows, IPAM space headers,
  DNS zone tree + record rows, DHCP scope / static / lease rows, VLAN
  rows.
- DNS group picker: single-select dropdown; Additional Zones hidden
  behind a themed `<details>` expander.
- VLAN router delete: two-step confirmation with checkbox.
- `ConfirmDestroyModal` / `DeleteConfirmModal` surface 409 errors
  inline.
- Space-table refresh button: `forceRefetch` instead of invalidate.
- Migration `e5b831f02db9` enforces `subnet.block_id NOT NULL` and
  fixes FK drift from SET NULL to RESTRICT.

**Auth — provider form UX**
- Auth-provider form defaults to `is_enabled=True` and
  `tls_insecure=False`. Pre-save "Test Connection" probe validates
  before creation (instead of after a save that might fail at login
  time). Applies to all five provider types.

**UI — selection persistence**
- IPAM / DNS / DHCP selection (subnet / zone / server) now survives tab
  switches. IPAM + DNS had a race in `useStickyLocation`'s restore
  effect; DHCP had no URL backing at all and was pure in-memory state.
  Both fixed; DHCP gets `spatium.lastUrl.dhcp` + a `setSelection()`
  wrapper that mirrors into `?group=…&server=…`.

**Documentation**
- [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) — recommended
  setup order from fresh install to allocating the first IP, with three
  topology recipes (all-SpatiumDDI / hybrid Windows DNS / hybrid
  Windows DNS + DHCP).
- [`docs/deployment/WINDOWS.md`](docs/deployment/WINDOWS.md) — shared
  Windows-side checklist: WinRM enablement, transport / port matrix,
  firewall rules, service accounts (`DnsAdmins` / `DHCP Users`), zone
  dynamic-update settings, diagnosis recipe with a pywinrm snippet,
  hardening checklist.
- [`docs/drivers/DHCP_DRIVERS.md`](docs/drivers/DHCP_DRIVERS.md) —
  filled in the driver spec CLAUDE.md was already pointing at. Kea
  agented + Windows DHCP agentless, with `AGENTLESS_DRIVERS` /
  `READ_ONLY_DRIVERS` classification.
- README — Windows Server DNS/DHCP feature bullet; Architecture
  section reframed around agented vs agentless split; doc-index
  refreshed.
- [`docs/assets/architecture.svg`](docs/assets/architecture.svg) —
  redrawn. Two-lane data-plane split: agented (`dns-bind9` + `dhcp-kea`
  with sidecar-agent pills) vs agentless (Windows DNS Path A/B,
  Windows DHCP Path A read-only); scheduled-sync arrow from Beat →
  agentless lane.
- [`features/DNS.md`](docs/features/DNS.md) — new §12 "Sync with
  Servers" reconciliation, §13 Windows DNS Path A + B, §14 scheduled
  reconciliation jobs.
- [`features/DHCP.md`](docs/features/DHCP.md) — new §15 Windows DHCP
  Path A.
- [`drivers/DNS_DRIVERS.md`](docs/drivers/DNS_DRIVERS.md) — removed
  orphaned PowerDNS stub. New §3 Windows DNS driver with both paths,
  write-through pattern, shared AXFR helper. Section numbering cleaned
  up (1–6).
- `docs/index.md`, `CLAUDE.md` — document maps point at the new files.

### Fixed

- `ipam.create_space` — return 409 on duplicate `ip_space` name instead
  of letting `UniqueViolationError` surface as a bare 500. Matches the
  pre-check pattern already in DHCP server-group CRUD; demo-seed
  retries are idempotent again.
- `frontend/src/lib/api.ts` — `ipamApi.updateBlock`'s Pick was missing
  `dhcp_server_group_id` and `dhcp_inherit_settings`, so the prod
  `tsc -b && vite build` failed even though dev `tsc --noEmit` passed.
- **Subnet inheritance editing bug** — editing a subnet back to
  "inherit from parent" used to still push records to the previously-
  pinned server. The inheritance walk now goes subnet → block
  ancestors → space and respects `dns_inherit_settings` at every level.
  Same walk applied in `services/dns/sync_check.py`.
- **Login crash on external user group assignment** — LDAP / OIDC /
  SAML logins were throwing `MissingGreenlet` during the group-
  membership replace step of `sync_external_user`. Fixed by:
  (1) adding `AsyncAttrs` mixin to `Base` so models expose
  `awaitable_attrs`; (2) awaiting `user.awaitable_attrs.groups` before
  assigning `user.groups = groups` in
  `backend/app/core/auth/user_sync.py` — SQLAlchemy's collection
  setter computes a diff against the currently-loaded collection, and
  that lazy-load under AsyncSession would otherwise raise.
- **`is_superadmin` vs RBAC wildcard mismatch** —
  externally-provisioned users with the built-in Superadmin role got
  403 from every `require_superadmin`-gated endpoint because the
  legacy `User.is_superadmin` flag defaults False and
  `sync_external_user` never flipped it. `require_superadmin` now
  admits either the legacy flag *or* any user whose groups → roles
  include an `action=*, resource_type=*` permission. Function-local
  import of `user_has_permission` dodges the circular import against
  `app.api.deps`.
- **Dynamic-lease mirrors are read-only** — `auto_from_lease=true` IPs
  now return 409 from update / delete endpoints and are skipped by
  bulk-edit. Prevents manual edits from being overwritten on the next
  lease pull.
- **IP delete cascades to DHCP static reservation** — on Windows the
  FK was set to NULL, orphaning the reservation. Now cascades
  correctly.
- **Tree chevrons** — DHCP / DNS server-group sidebars + VLANs tree
  swapped to the IPAM `[+] / [−]` boxed toggle for consistency. DNS
  zone-tree folder icons left alone.
- **DNS group expand-stuck** — selecting a zone no longer latches its
  group's expanded state.
- **IPAM address list** gains a `tags` column rendering clickable chips
  that populate the tag filter.
- **`seed_demo.py`** creates DNS group + zone and DHCP server group
  *first*, then wires both into the IP Space so blocks/subnets inherit
  by default.

### Security

- **CodeQL alert #13 (CWE-601, URL redirection from remote source)**
  closed. Previous attempts added `_safe_error_suffix()` and then a
  `urlparse`-based sanitiser defence, neither of which CodeQL's taint
  tracker recognises as sanitisers. Replaced with a closed-set
  allowlist: all redirect reasons are now selected from
  `_LOGIN_ERROR_REASONS` (a frozenset of literals); anything else
  becomes `"unknown"`. The three interpolation sites that previously
  threaded IdP error codes / exception reason fields into f-strings
  now pass fixed literals. Actual IdP error strings still land in the
  server log + audit row — only the URL-visible part is generic.

### Changed

- Backend + frontend: `make lint` now mandatory before push —
  CI-mirroring `make ci` target (added in 2026.04.16-3) catches
  formatter drift before it hits GitHub Actions.

---

## 2026.04.16-3 — 2026-04-16

Third same-day iteration. First wave of substantive post-alpha work:
external auth providers (LDAP / OIDC / SAML + RADIUS / TACACS+ with
backup-server failover), group-based RBAC enforced across every API
router, partial IPv6 (storage + UI + Kea Dhcp6), inherited-field
placeholders on edit modals, mobile-responsive layout, IPAM block/subnet
overlap validation, scheduled IPAM↔DNS auto-sync, bulk-edit DNS zone
assignment, shared zone-picker dropdown with primary / additional
grouping, and a `make ci` target that mirrors GitHub Actions locally.

### Added

**Auth — Wave A (external identity providers)**
- `AuthProvider` + `AuthGroupMapping` models with Fernet-encrypted secrets
  (`backend/app/core/crypto.py`). Admin CRUD at `/api/v1/auth-providers`
  with per-type structured forms on `AuthProvidersPage`.
- **LDAP** — `ldap3`-based auth (`backend/app/core/auth/ldap.py`).
  Password-grant fallthrough from `/auth/login`. TLS / LDAPS / StartTLS
  support with optional CA cert path.
- **OIDC** — authorize / callback redirect flow with signed-JWT state+nonce
  cookie, discovery + JWKS caching, `authlib.jose` ID-token validation.
  Login page renders enabled providers as "Sign in with …" buttons.
- **SAML** — `python3-saml` SP flow: HTTP-Redirect AuthnRequest, ACS POST
  binding, `GET /auth/{provider_id}/metadata` for IdP-side SP metadata.
- Unified user sync (`backend/app/core/auth/user_sync.py`) creates / updates
  `User` rows, replaces group membership from group mappings, and
  **rejects logins with no mapping match** (configurable per provider).

**Auth — Wave B (network-device protocols)**
- **RADIUS** — `pyrad` driver (`backend/app/core/auth/radius.py`).
  Built-in minimal dictionary; extra vendor dicts via `dictionary_path`.
  Group info from `Filter-Id` / `Class` by default.
- **TACACS+** — `tacacs_plus` driver (`backend/app/core/auth/tacacs.py`).
  Separate `authorize()` round-trip pulls AV pairs; numeric `priv-lvl`
  values are surfaced as `priv-lvl:N` for group mapping.
- Both share the same password-grant fallthrough as LDAP via
  `PASSWORD_PROVIDER_TYPES = ("ldap", "radius", "tacacs")`.
- Per-provider "Test connection" probe in the admin UI returns
  `{ok, message, details}` for all five provider types.

**Auth — backup-server failover (LDAP / RADIUS / TACACS+)**
- Each password provider's config now accepts an optional list of backup
  hosts (`config.backup_hosts` for LDAP, `config.backup_servers` for
  RADIUS/TACACS+). Entries can be `"host"` or `"host:port"`; bracketed
  IPv6 literals (`[::1]:389`) are supported. The UI adds a "Backup hosts /
  servers" textarea (one per line).
- LDAP uses `ldap3.ServerPool(pool_strategy=FIRST, active=True,
  exhaust=True)` — dead hosts are skipped for the pool's lifetime.
- RADIUS and TACACS+ iterate primary → backups manually. A definitive
  auth answer (Accept / Reject, `valid=True/False`) stops iteration;
  network / timeout / protocol errors fail over to the next server.
- All backups share the primary's shared secret and timeout settings.

**Auth — Wave C (group-based RBAC enforcement)**
- Permission grammar `{action, resource_type, resource_id?}` with wildcard
  support; helpers in `backend/app/core/permissions.py`
  (`user_has_permission`, `require_permission`, `require_any_permission`,
  `require_resource_permission`).
- Five builtin roles seeded at startup: Superadmin, Viewer, IPAM Editor,
  DNS Editor, DHCP Editor.
- `/api/v1/roles` CRUD + expanded `/api/v1/groups` CRUD with role/user
  assignment. Router-level gates applied across IPAM / DNS / DHCP / VLANs
  / custom-fields / settings / audit. Superadmin always bypasses.
- `RolesPage` + `GroupsPage` admin UI. See `docs/PERMISSIONS.md`.

**Auth — Wave D UX polish**
- Per-field opt-in toggles on bulk-edit IPs (status / description / tags /
  custom-fields / DNS zone individually) plus a "replace all tags" mode.
- `EditSubnetModal` + `EditBlockModal` now surface inherited custom-field
  values as HTML `placeholder` with "inherited from block/space `<name>`"
  badges. New `/api/v1/ipam/blocks/{id}/effective-fields` endpoint for
  parity with the existing subnet endpoint.

**IPv6 (partial)**
- `DHCPScope.address_family` column (migration `d7a2b6e9f134`) + Kea
  driver `Dhcp6` branch renders a v6 config bundle from the same scope
  rows. Dhcp6 option-name translation TODO is flagged in
  `backend/app/drivers/dhcp/kea.py`.
- `Subnet.total_ips` widened to `BigInteger` (migration `e3c7b91f2a45`)
  so a `/64` (`2^64` addresses) fits. `_total_ips()` clamps at `2^63 − 1`.
- Subnet create skips the v6 broadcast row; `_sync_dns_record` emits AAAA
  + PTR in `ip6.arpa`.
- `/blocks/{id}/available-subnets` accepts `/8–/128` (was `le=32`) with
  an explicit address-family guard. Frontend "Find by size" splits the
  prefix pool into v4 (`/8–/32`) and v6 (`/32, /40, /44, /48, /52, /56,
  /60, /64, /72, /80, /96, /112, /120, /124, /127, /128`) and dynamically
  filters to prefixes strictly longer than the selected block's prefix.
- `/ipam/addresses/next-address` returns 409 on v6 subnets (EUI-64 / hash
  allocation is a future enhancement).
- IPAM create-block / create-subnet placeholders now include an IPv6
  example next to the IPv4 one (`e.g. 10.0.0.0/8 or 2001:db8::/32`).

**IPAM — block / subnet overlap validation**
- `_assert_no_block_overlap()` rejects same-level duplicates and CIDR
  overlaps in `create_block` and in the reparent path of `update_block`.
  Uses PostgreSQL's `cidr &&` operator for a single-query overlap check.

**IPAM — scheduled IPAM ↔ DNS auto-sync**
- Opt-in Celery beat task `app.tasks.ipam_dns_sync.auto_sync_ipam_dns`
  (`backend/app/tasks/ipam_dns_sync.py`). Beat fires every 60 s; the task
  gates on `PlatformSettings.dns_auto_sync_enabled` +
  `dns_auto_sync_interval_minutes`, so cadence changes in the UI take
  effect without restarting beat. Optional deletion of stale auto-
  generated records (`dns_auto_sync_delete_stale`).
- Settings UI: new **DNS Auto-Sync** section on `/admin/settings`
  (enable / interval / delete-stale toggle).

**IPAM — shared zone picker + bulk-edit DNS zone**
- New `ZoneOptions` component (`frontend/src/pages/ipam/IPAMPage.tsx`)
  renders the primary zone first, then an `<optgroup label="Additional
  zones">` separator. Used in Create / Edit / Bulk-edit IP modals.
- Zone picker is restricted to the subnet's explicit primary + additional
  zones when any are pinned; falls back to every zone in the group only
  when the admin picked a group without pinning specific zones.
- `IPAddressBulkChanges.dns_zone_id` — bulk-editing a set of IPs routes
  every selected address through `_sync_dns_record` for move / create /
  delete.

**IPAM — mobile responsive**
- Sidebar becomes a drawer on `<md` with backdrop + `Header` hamburger
  toggle.
- 10+ data tables wrapped in `overflow-x-auto` with `min-w` so wide
  columns scroll horizontally instead of overflowing the viewport.
- All modals sized `max-w-[95vw]` on `<sm`.

**IPAM — IP aliases polish**
- Adding or deleting an alias now also invalidates
  `["subnet-aliases", subnet_id]`, so switching to the Aliases tab after
  an add/delete no longer shows a stale list.
- Delete alias from the subnet Aliases tab now pops a single-step
  `ConfirmDeleteModal` ("Delete alias `<fqdn>`? The DNS record will be
  removed.") matching the standard IPAM delete flow.

**Developer tooling**
- `make ci` — new Makefile target that runs the exact three lint jobs
  CI runs (`backend-lint`: ruff + black + mypy; `frontend-lint`: eslint +
  prettier + tsc; `frontend-build`: `npm run build`). Backend checks run
  inside the running `api` container; ruff/black/mypy are installed on
  first run if missing.
- `.github/ISSUE_TEMPLATE/{bug_report,feature_request,config}.yml` and
  `.github/pull_request_template.md` — structured issue + PR templates
  with dropdown areas (IPAM / DNS / DHCP / Auth / RBAC / Audit / UI / API
  / Deployment / Docs), repro steps, a private Security Advisory link,
  and a test-plan checklist.

### Changed

- IPAM modal input focus ring switched to `focus:ring-inset` so the 2px
  ring draws inside the border. Prevents horizontal clipping by the
  modal's `overflow-y-auto` container (browsers clamp `overflow-x` when
  `overflow-y` is set), which previously cut the left edge of any focused
  box in the Create / Edit Block / Subnet forms.
- `CLAUDE.md` phase roadmap updated to reflect Waves A–D. Tech-stack Auth
  row now lists actual deps (`python-jose + bcrypt`, `ldap3`, `authlib`,
  `python3-saml`, `pyrad`, `tacacs_plus`, `Fernet`).

### Fixed

- `user_sync._matched_internal_groups` used one `res` variable name for
  two `db.execute()` calls with different result types, tripping mypy
  after the dev extras finally ran in `make ci`. Renamed to `map_res` /
  `group_res`.
- CI lint was still failing on `main` after `f38d533` — residual ruff
  warnings (20) and prettier issues (12 files). Now clean; `make ci`
  passes end-to-end.
- SAML ACS handler: `SAMLResponse` / `RelayState` form fields kept their
  spec-mandated casing; added `# noqa: N803` so ruff stops complaining.

### Security

- CodeQL alert #13 (CWE-601, URL redirection from remote source): the
  OIDC callback interpolated the IdP-provided `error` query parameter
  directly into the `/login?error=…` redirect. The redirect target was
  already a relative path (so no open-redirect in practice) but the
  tainted value still flowed into the URL. Added `_safe_error_suffix()`
  to strip any provider-supplied error code down to `[a-z0-9_]` (max 40
  chars) and applied it at every `f"…_{error}"` / `f"…_{exc.reason}"`
  site in the OIDC and SAML callback handlers.

---

## 2026.04.16-2 — 2026-04-16

First post-alpha iteration — same-day follow-up to the alpha. Adds IP
aliases across the stack, multi-select/bulk ops on the IP address table,
an always-visible per-column filter row on the audit log, a DNS zone
tree that can create sub-zones with a click, and switches the base
Compose file to pull release images from GHCR.

### Added

**IPAM**
- IP aliases — Allocate/Edit IP modal supports extra CNAME/A records tied to the IP. Auto-deleted on IP purge.
- `+N aliases` pill next to the hostname in the subnet IP table when an IP has user-added aliases (new `alias_count` on `IPAddressResponse`).
- New "Aliases" subnet tab listing every CNAME/A alias in the subnet (name · type · target · IP · host · delete). `GET /ipam/subnets/{id}/aliases`.
- Multi-select on the subnet IP table with a bulk-action bar inline on the tab row (no banner push-down). `POST /ipam/addresses/bulk-delete` (soft → orphan or permanent) and `POST /ipam/addresses/bulk-edit` (status, description, tags *merge*, custom_fields *merge*). System rows auto-excluded.
- Reverse-zone backfill — dedicated button on Space / Block / Subnet headers (`POST /ipam/{scope}/{id}/reverse-zones/backfill`). Also backfills opportunistically on every IP allocation.
- DHCP Pool membership column on subnet IP table — cyan/violet/zinc badge per IP shows which pool (dynamic/reserved/excluded) it falls in.
- Bulk orphan cleanup modal on subnet header.
- `IPAddress.auto_from_lease` column distinguishes DHCP-lease-mirrored rows from manual allocations (migration `e2a6f3b8c1d4`).

**DNS**
- Real RPZ blocklist rendering in the BIND9 agent — `response-policy { } break-dnssec yes`, CNAME trigger zone files (nxdomain/sinkhole/redirect/passthru). Wildcards block both apex and subdomains.
- Blocklist entries get a `reason` column (migration `b4d1c9e2f3a7`) and per-entry `is_wildcard` toggle (defaults true).
- Inline edit for blocklist entries + exceptions (`PUT .../entries/{id}`, `PUT .../exceptions/{id}`).
- Blocklist page reorganized into red **Blocked Domains** and green **Allow-list** sections.
- DNS records table: always-visible edit/delete, clickable record name, single-step delete confirm, multi-select bulk delete (IPAM records excluded).
- Zone tree folder click → Create-Zone modal pre-filled with the parent suffix (e.g. clicking `example.com` opens "New zone `*.example.com`"). TLD folders (org/com/net/…) just toggle, don't prompt. Zone names in the tree render without the trailing dot.
- DNS agent re-bootstraps on 404 (not just 401) — recovers from stale server rows.

**DHCP**
- Pool overlap validation + existing-IP warning on pool create.
- Static DHCP ↔ IPAM sync (creates `status=static_dhcp` rows, fires DNS sync on create/update/delete).
- Lease → IPAM mirror: active leases create `dhcp` rows; expired leases remove them (`auto_from_lease` flag only).
- Celery `sweep_expired_leases` task (every 5min) catches missed lease events.
- Force-sync coalesces repeated clicks into one pending op.
- Kea agent: UDP socket mode for relay-only deployments; `/run/kea` perms; lease op acks via heartbeat.
- DHCP scope options default-prefill from Settings (DNS/NTP/domain/lease-time).
- Static assignments moved from DHCP Pools tab into IPAM Allocate IP flow.

**Audit log**
- Per-column filter row on `/admin/audit` — User/Summary/IP text inputs, Action/Resource/Result dropdowns, always visible, Clear-all X in the actions column. Backend adds `resource_display` / `result` / `source_ip` query params.

**Platform**
- Base `docker-compose.yml` now pulls release images from GHCR (`ghcr.io/spatiumddi/spatiumddi-{api,frontend}`, `ghcr.io/spatiumddi/dns-bind9`, `ghcr.io/spatiumddi/dhcp-kea`); pin with `SPATIUMDDI_VERSION=<tag>` in `.env`.
- `docker-compose.dev.yml` is a standalone self-contained file that keeps `build:` stanzas for local dev builds — use `docker compose -f docker-compose.dev.yml …` or `export COMPOSE_FILE=docker-compose.dev.yml`.
- Jekyll docs site config (`docs/_config.yml`, `docs/index.md`).
- CHANGELOG; alpha banner; clickable screenshot thumbnails in README.
- Seed script (`scripts/seed_demo.py`).
- Alembic migrations now tracked in git (were `.gitignore`d — CI was broken).
- `COMPOSE_PROFILES` documented.

### Changed

- `CLAUDE.md` slimmed to a navigational entry point — Phase 1 / Waves 1–5 / DHCP Wave 1 implemented-lists moved to this CHANGELOG; added a Repo Layout section and a Cross-cutting Patterns section (driver abstraction, ConfigBundle+ETag long-poll, agent bootstrap/reconnection).

### Fixed

- Full audit of IPAM/VLANs/DNS/DHCP frontend ↔ backend API contracts; 10+ mismatches fixed.
- `allocate_next_ip` — `FOR UPDATE` on outer join, now `of=Subnet` + `.unique()`.
- Workflow permissions hardened (CodeQL alerts resolved).
- Ruff (import sort, unused `datetime.UTC`), Black (4 files), Prettier (3 files) — unblocked CI.

---

## 2026.04.16-1 — Alpha

First public release. **Alpha quality** — expect rough edges and breaking changes between releases.

### Added

**IPAM**
- Hierarchical IP management: spaces, blocks (nested), subnets, addresses
- Subnet CIDR validation with "Did you mean?" hints
- Next-available IP allocation (sequential / random)
- Soft-delete IP addresses (orphan → restore / purge)
- Bulk orphan cleanup modal on subnet view
- Subnet-by-size search ("Find by size" in create modal)
- Per-column filters on address & block tables
- Drag-and-drop reparenting of blocks and subnets
- Free-space band on block detail with click-to-create
- Import/export (CSV, JSON, XLSX with preview)
- Bulk-edit subnets (tags, custom fields)
- Custom field definitions per resource type
- DNS assignment at space / block / subnet level with inheritance
- IPAM ↔ DNS drift detection and reconciliation (subnet / block / space scope)
- DNS sync indicator column on IP address table
- DHCP pool membership column on IP address table

**DNS**
- Server groups, servers, zones, records — full CRUD
- BIND9 driver with Jinja templates, TSIG-signed RFC 2136 dynamic updates
- Agent runtime: bootstrap (PSK → JWT), long-poll config sync with ETag, on-disk cache
- Container image: `ghcr.io/spatiumddi/dns-bind9` (Alpine 3.22, multi-arch)
- Zone tree with nested sub-zone display
- Zone import/export (RFC 1035 parser, color-coded diff preview)
- Server health checks (heartbeat staleness → SOA fallback)
- ACLs, views, trust anchors
- Blocking lists (RPZ) with feed refresh, bulk-add, exceptions
- Query logging configuration (file / syslog / stderr)
- DNS defaults in Settings (TTL, zone type, DNSSEC, recursion)

**DHCP**
- Kea driver + agent runtime (bootstrap, long-poll, lease tail, local cache)
- Container image: `ghcr.io/spatiumddi/dhcp-kea` (Alpine 3.22, multi-arch)
- Server groups, servers, scopes, pools, static assignments, client classes
- DHCP options editor with NTP (option 42) as first-class field
- Pool overlap validation on create and resize
- Existing-IP-in-range warning on pool creation
- Scope auto-binds to sole server; gateway + settings defaults pre-filled
- Static DHCP ↔ IPAM sync (status=static_dhcp, DNS forward/reverse)
- DHCP defaults in Settings (DNS servers, domain, NTP, lease time)
- UDP socket mode for relay-only deployments (no broadcast / no NET_RAW)

**VLANs**
- Routers and VLANs with full CRUD
- Subnet ↔ VLAN association (router + VLAN columns in IPAM views)
- Delete protection when subnets still reference a VLAN/router

**Auth & Users**
- Local auth with JWT + refresh token rotation
- Forced password change on first login
- User management (create, edit, reset password, delete)

**Platform**
- Dashboard with utilisation stats, top subnets, VLAN/DNS/DHCP status sections
- Global search (Cmd+K / Ctrl+K) across IPs, hostnames, MACs, subnets
- Settings page (branding, allocation, session, DNS/DHCP defaults, utilisation thresholds)
- Audit log viewer with action/result badges and filters
- Docker Compose with `dns` and `dhcp` profiles (`COMPOSE_PROFILES=dns,dhcp`)
- Kubernetes manifests (StatefulSets, services, PVCs)
- GitHub Actions CI (lint, type-check, test) + release workflow (multi-arch images, GitHub Release)

### Security
- Workflow permissions hardened (CodeQL alerts resolved)
- All mutations audited before commit
- Agent re-bootstraps on 401/404 (no stale-token loops)

---

_For the full commit history, see the [GitHub compare view](https://github.com/spatiumddi/spatiumddi/commits/main)._
