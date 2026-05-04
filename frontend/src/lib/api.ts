import axios, { AxiosError, type AxiosInstance } from "axios";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

function createClient(): AxiosInstance {
  const client = axios.create({
    baseURL: API_BASE,
    headers: { "Content-Type": "application/json" },
  });

  // Attach Bearer token from localStorage on every request
  client.interceptors.request.use((config) => {
    const token = localStorage.getItem("access_token");
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  });

  // On 401, attempt token refresh once then redirect to login.
  //
  // Two deadlock traps to avoid:
  //
  //   1. The ``/auth/refresh`` call itself goes through this same
  //      interceptor. A 401 from refresh must NOT be treated as
  //      "try to refresh again" — otherwise it queues on itself and
  //      the outer ``await`` never resolves, the ``catch`` block
  //      never runs, and the user is never redirected.
  //
  //   2. If refresh fails, we must reject every queued request too
  //      — not just the current one. Otherwise any concurrent
  //      requests that were already queued hang forever.
  let isRefreshing = false;
  let refreshQueue: Array<{
    resolve: (token: string) => void;
    reject: (err: unknown) => void;
  }> = [];

  function isRefreshCall(url: string | undefined): boolean {
    return !!url && url.replace(/^[^/]*\/\//, "").includes("/auth/refresh");
  }

  function forceLogin(): void {
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    // Skip redirect if we're already on /login so the user doesn't
    // see a white flash when an expired session fires first.
    if (!window.location.pathname.startsWith("/login")) {
      window.location.href = "/login";
    }
  }

  client.interceptors.response.use(
    (res) => res,
    async (err: AxiosError) => {
      const originalRequest = err.config as typeof err.config & {
        _retry?: boolean;
      };

      // 401 on the refresh call itself = the refresh token is dead.
      // Surface the original error so the caller's ``catch`` branch
      // runs its cleanup + redirect. Don't try to refresh the refresh.
      if (err.response?.status === 401 && isRefreshCall(originalRequest?.url)) {
        return Promise.reject(err);
      }

      if (err.response?.status === 401 && !originalRequest?._retry) {
        const refreshToken = localStorage.getItem("refresh_token");
        if (!refreshToken) {
          forceLogin();
          return Promise.reject(err);
        }

        if (isRefreshing) {
          return new Promise((resolve, reject) => {
            refreshQueue.push({
              resolve: (token: string) => {
                if (originalRequest?.headers)
                  originalRequest.headers.Authorization = `Bearer ${token}`;
                resolve(client(originalRequest!));
              },
              reject,
            });
          });
        }

        originalRequest._retry = true;
        isRefreshing = true;
        try {
          const res = await client.post("/auth/refresh", {
            refresh_token: refreshToken,
          });
          const { access_token, refresh_token: newRefresh } = res.data;
          localStorage.setItem("access_token", access_token);
          localStorage.setItem("refresh_token", newRefresh);
          refreshQueue.forEach(({ resolve }) => resolve(access_token));
          refreshQueue = [];
          if (originalRequest?.headers)
            originalRequest.headers.Authorization = `Bearer ${access_token}`;
          return client(originalRequest!);
        } catch (refreshErr) {
          // Refresh failed — reject every queued request so their
          // awaits resolve, then clear storage + redirect.
          refreshQueue.forEach(({ reject }) => reject(refreshErr));
          refreshQueue = [];
          forceLogin();
          return Promise.reject(err);
        } finally {
          isRefreshing = false;
        }
      }
      return Promise.reject(err);
    },
  );

  return client;
}

export const api = createClient();

/**
 * Normalise whatever shape an API error arrives in into a single string
 * suitable for React children.
 *
 * FastAPI returns three common shapes:
 *   - 4xx HTTPException:   `{"detail": "some message"}`
 *   - 422 validation:      `{"detail": [{"type", "loc", "msg", "input", "ctx"}, ...]}`
 *   - Unhandled 500:       `{"detail": "Internal Server Error"}` or no body
 *
 * Passing the raw `detail` into `setError(...)` was crashing React with
 * error #31 ("Objects are not valid as a React child") when a 422 hit a
 * code path that assumed it was always a string. Use this helper instead
 * of a naive `err.response?.data?.detail ?? "Error"`.
 */
export function formatApiError(err: unknown, fallback = "Error"): string {
  const anyErr = err as {
    response?: { data?: { detail?: unknown } };
    message?: string;
  };
  const detail = anyErr?.response?.data?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    // Pydantic v2 validation errors.
    const parts = detail
      .map((e) => {
        if (!e || typeof e !== "object") return String(e);
        const rec = e as { msg?: unknown; loc?: unknown };
        const loc = Array.isArray(rec.loc)
          ? rec.loc.filter((s) => s !== "body").join(".")
          : "";
        const msg = typeof rec.msg === "string" ? rec.msg : JSON.stringify(e);
        return loc ? `${loc}: ${msg}` : msg;
      })
      .filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  if (detail && typeof detail === "object") {
    // Unexpected shape — stringify defensively so we never render an object.
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  if (typeof anyErr?.message === "string" && anyErr.message)
    return anyErr.message;
  return fallback;
}

// Typed API helpers

export type DdnsHostnamePolicy =
  | "client_provided"
  | "client_or_generated"
  | "always_generate"
  | "disabled";

export interface IPSpace {
  id: string;
  name: string;
  description: string;
  is_default: boolean;
  tags: Record<string, unknown>;
  color: string | null;
  dns_group_ids: string[];
  dns_zone_id: string | null;
  dns_additional_zone_ids: string[];
  dhcp_server_group_id?: string | null;
  // DDNS defaults — root of the block → subnet inheritance chain. Descendants
  // use these when their own `ddns_inherit_settings` is True.
  ddns_enabled?: boolean;
  ddns_hostname_policy?: DdnsHostnamePolicy;
  ddns_domain_override?: string | null;
  ddns_ttl?: number | null;
  // VRF / routing annotation — pure metadata; address allocation
  // ignores these. ``route_targets`` is an array of strings so the
  // operator can encode the inline import:A:B; export:C:D convention
  // until first-class import / export columns land.
  vrf_id?: string | null;
  vrf_name?: string | null;
  route_distinguisher?: string | null;
  route_targets?: string[] | null;
  asn_id?: string | null;
  created_at?: string;
  modified_at?: string;
}

export interface IPBlock {
  id: string;
  space_id: string;
  parent_block_id: string | null;
  network: string;
  name: string;
  description: string;
  utilization_percent: number;
  tags: Record<string, unknown>;
  custom_fields: Record<string, unknown>;
  dns_group_ids: string[] | null;
  dns_zone_id: string | null;
  dns_additional_zone_ids: string[] | null;
  dns_inherit_settings: boolean;
  // Issue #25 — block-level split-horizon flag, inheritable to
  // descendant subnets via the existing dns_inherit_settings walk.
  dns_split_horizon?: boolean;
  dhcp_server_group_id?: string | null;
  dhcp_inherit_settings?: boolean;
  // DDNS inheritance. When `ddns_inherit_settings` is True, the four
  // fields above it are ignored and the effective config comes from the
  // parent block chain → space.
  ddns_enabled?: boolean;
  ddns_hostname_policy?: DdnsHostnamePolicy;
  ddns_domain_override?: string | null;
  ddns_ttl?: number | null;
  ddns_inherit_settings?: boolean;
  vrf_id?: string | null;
  asn_id?: string | null;
  applied_template_id?: string | null;
  created_at?: string;
  modified_at?: string;
}

export interface FreeCidrRange {
  network: string;
  first: string;
  last: string;
  size: number;
  prefix_len: number;
}

export interface PlanRequestItem {
  count: number;
  prefix_len: number;
}

export interface PlannedSubnet {
  prefix_len: number;
  network: string;
  first: string;
  last: string;
  size: number;
}

export interface UnfulfilledItem {
  prefix_len: number;
  requested: number;
  allocated: number;
}

export interface AggregationSuggestion {
  supernet: string;
  prefix_len: number;
  total_size: number;
  subnet_ids: string[];
  subnet_networks: string[];
}

export interface PlanAllocationResponse {
  block_network: string;
  block_prefix_len: number;
  allocations: PlannedSubnet[];
  unfulfilled: UnfulfilledItem[];
  remaining_free: FreeCidrRange[];
}

// ── Subnet plans (multi-level CIDR designs) ─────────────────────────────

export interface PlanNode {
  id: string;
  network: string;
  name: string;
  description: string;
  existing_block_id?: string | null;
  kind: "block" | "subnet";
  // Optional resource bindings — null/undefined = inherit from parent.
  dns_group_id?: string | null;
  dns_zone_id?: string | null;
  dhcp_server_group_id?: string | null;
  vlan_ref_id?: string | null;
  gateway?: string | null;
  children: PlanNode[];
}

export interface SubnetPlanRead {
  id: string;
  name: string;
  description: string;
  space_id: string;
  tree: PlanNode | null;
  applied_at: string | null;
  applied_resource_ids: { block_ids: string[]; subnet_ids: string[] } | null;
  created_by_user_id: string | null;
  created_at: string;
  modified_at: string;
}

export interface SubnetPlanCreate {
  name: string;
  description?: string;
  space_id: string;
  tree: PlanNode;
}

export interface SubnetPlanUpdate {
  name?: string;
  description?: string;
  tree?: PlanNode;
}

export interface PlanValidationConflict {
  node_id: string;
  network: string;
  kind:
    | "overlap_existing"
    | "out_of_parent"
    | "sibling_overlap"
    | "duplicate_id"
    | "missing_block";
  message: string;
}

export interface PlanValidationResult {
  ok: boolean;
  conflicts: PlanValidationConflict[];
  summary: { block_count: number; subnet_count: number };
}

export interface PlanApplyResult {
  block_ids: string[];
  subnet_ids: string[];
  applied_at: string;
}

export interface EffectiveDns {
  dns_group_ids: string[];
  dns_zone_id: string | null;
  dns_additional_zone_ids: string[];
  inherited_from_block_id: string | null;
}

export interface EffectiveDhcp {
  dhcp_server_group_id: string | null;
  inherited_from_block_id: string | null;
  inherited_from_space: boolean;
}

export interface DnsSyncMissing {
  ip_id: string;
  ip_address: string;
  hostname: string;
  record_type: "A" | "PTR";
  expected_name: string;
  expected_value: string;
  zone_id: string;
  zone_name: string;
}

export interface DnsSyncMismatch {
  record_id: string;
  ip_id: string;
  ip_address: string;
  record_type: "A" | "PTR";
  zone_id: string;
  zone_name: string;
  current_name: string;
  current_value: string;
  expected_name: string;
  expected_value: string;
}

export interface DnsSyncStale {
  record_id: string;
  record_type: string;
  zone_id: string;
  zone_name: string;
  name: string;
  value: string;
  reason: string;
}

export interface DnsSyncPreview {
  subnet_id: string;
  forward_zone_id: string | null;
  forward_zone_name: string | null;
  reverse_zone_id: string | null;
  reverse_zone_name: string | null;
  missing: DnsSyncMissing[];
  mismatched: DnsSyncMismatch[];
  stale: DnsSyncStale[];
}

export interface DnsSyncCommitResult {
  created: number;
  updated: number;
  deleted: number;
  errors: string[];
}

export interface DnsSyncSummary {
  subnet_id: string;
  missing: number;
  mismatched: number;
  stale: number;
  total: number;
  has_drift: boolean;
}

export interface SubnetVLANRef {
  id: string;
  router_id: string;
  router_name: string | null;
  vlan_id: number;
  name: string;
}

export interface Subnet {
  id: string;
  space_id: string;
  block_id: string;
  network: string;
  name: string;
  description: string;
  vlan_id: number | null;
  vxlan_id: number | null;
  vlan_ref_id?: string | null;
  vlan?: SubnetVLANRef | null;
  gateway: string | null;
  status: string;
  skip_auto_addresses?: boolean;
  utilization_percent: number;
  total_ips: number;
  allocated_ips: number;
  tags: Record<string, unknown>;
  custom_fields: Record<string, unknown>;
  dns_group_ids: string[] | null;
  dns_zone_id: string | null;
  dns_additional_zone_ids: string[] | null;
  dns_inherit_settings: boolean;
  // Issue #25 — when true the IP picker becomes a multi-select
  // grouped by DNS server group; ``IPAddress.extra_zone_ids``
  // carries the extras. Off = single-zone publishing (current).
  dns_split_horizon?: boolean;
  dhcp_server_group_id?: string | null;
  dhcp_inherit_settings?: boolean;
  // DDNS — dynamic DNS reconciliation from DHCP leases. Subnet-level
  // opt-in; when enabled, lease-mirrored IPAM rows get an A/AAAA + PTR
  // per ``ddns_hostname_policy``. See docs/features/DNS.md §7.
  ddns_enabled?: boolean;
  ddns_hostname_policy?:
    | "client_provided"
    | "client_or_generated"
    | "always_generate"
    | "disabled";
  ddns_domain_override?: string | null;
  ddns_ttl?: number | null;
  // When True, the four fields above are ignored and the effective DDNS
  // config is resolved from the containing block / space.
  ddns_inherit_settings?: boolean;
  // Device profiling — opt-in auto-nmap on new DHCP leases. See
  // CLAUDE.md "Device profiling" entry. Default off because nmap is
  // loud (corporate IDS will flag the source IP); operators must
  // authorise + enable per subnet. ``auto_profile_refresh_days`` is
  // the dedupe window so churning Wi-Fi clients don't re-trigger.
  auto_profile_on_dhcp_lease?: boolean;
  auto_profile_preset?:
    | "quick"
    | "service_version"
    | "os_fingerprint"
    | "service_and_os"
    | "default_scripts"
    | "udp_top100"
    | "aggressive";
  auto_profile_refresh_days?: number;
  // Compliance / classification flags. First-class booleans (rather
  // than freeform tags) so auditor queries — "show me every PCI
  // subnet" — are clean indexed predicates. Default false on every
  // subnet; flip via the Edit modal. Surfaces on the Compliance
  // dashboard at /admin/compliance.
  pci_scope?: boolean;
  hipaa_scope?: boolean;
  internet_facing?: boolean;
  dns_servers?: string[] | null;
  domain_name?: string | null;
  applied_template_id?: string | null;
  created_at?: string;
  modified_at?: string;
}

/** Optional role tag, orthogonal to ``status``. Roles in
 *  ``IP_ROLES_SHARED`` (anycast / vip / vrrp) are intentionally
 *  shared across multiple devices — the API skips MAC-collision
 *  warnings for them. */
export type IPRole =
  | "host"
  | "loopback"
  | "anycast"
  | "vip"
  | "vrrp"
  | "secondary"
  | "gateway";

export const IP_ROLE_OPTIONS: IPRole[] = [
  "host",
  "loopback",
  "anycast",
  "vip",
  "vrrp",
  "secondary",
  "gateway",
];

export const IP_ROLES_SHARED: ReadonlySet<IPRole> = new Set([
  "anycast",
  "vip",
  "vrrp",
]);

export interface IPAddress {
  id: string;
  subnet_id: string;
  address: string;
  status: string;
  /** Curated role tag — null = no specific role. See ``IP_ROLE_OPTIONS``. */
  role?: IPRole | null;
  /** TTL on a ``status='reserved'`` row. The Celery sweep task flips
   *  the row back to ``available`` after this passes. */
  reserved_until?: string | null;
  hostname: string | null;
  fqdn: string | null;
  description: string;
  mac_address: string | null;
  owner_user_id?: string | null;
  last_seen_at?: string | null;
  last_seen_method?: string | null;
  tags: Record<string, unknown>;
  custom_fields: Record<string, unknown>;
  // Linkage (§3) — populated by Wave 3 DDNS/DHCP integration.
  forward_zone_id?: string | null;
  // Issue #25 — additional zones to publish A/AAAA records into
  // beyond the singular primary. Each entry is a zone UUID. Empty
  // list = current behaviour (one record). Operator-edited via the
  // multi-zone picker on the IP create / edit modal when the
  // subnet's ``dns_split_horizon`` is on.
  extra_zone_ids?: string[];
  reverse_zone_id?: string | null;
  dns_record_id?: string | null;
  dhcp_lease_id?: string | null;
  static_assignment_id?: string | null;
  // True when this row is a dynamic-lease mirror created by the DHCP
  // lease-pull task. Such rows are read-only in the UI — the DHCP server
  // owns their state and any edit would get overwritten on the next pull.
  auto_from_lease?: boolean;
  // User-added CNAME/A aliases on this IP (excludes the primary A).
  alias_count?: number;
  // Count of NAT mappings referencing this IP (as either internal or
  // external endpoint). Populated by /ipam/subnets/{id}/addresses;
  // defaults to 0 elsewhere. Surfaced as a small "NAT" badge in the
  // IP-row of IPAMPage with a tooltip listing the matching mapping
  // names.
  nat_mapping_count?: number;
  // IEEE OUI vendor for this MAC (populated when OUI lookup is enabled).
  vendor?: string | null;
  // Device profile (active-layer Phase 1). ``last_profiled_at`` is the
  // finished_at of the most recent successful nmap profile scan;
  // ``last_profile_scan_id`` deep-links to the NmapScan row. Surfaced
  // in the IP detail modal's "Device profile" section.
  last_profiled_at?: string | null;
  last_profile_scan_id?: string | null;
  // Device profile (passive-layer Phase 2). Populated by the
  // fingerbank lookup task off DHCP option-55/option-60 captures
  // pushed by the agent's scapy sniffer. Null while the sniffer is
  // disabled or no fingerprint has been observed for the row's MAC.
  device_type?: string | null;
  device_class?: string | null;
  device_manufacturer?: string | null;
  created_at?: string;
  modified_at?: string;
}

/** Joined view of the ``dhcp_fingerprint`` row (passive-layer Phase 2)
 *  that matches an IP's MAC. Surfaced in the IP detail modal's "Device
 *  profile" section under a "Raw signature" disclosure. */
export interface DHCPFingerprintResponse {
  mac_address: string;
  option_55: string | null;
  option_60: string | null;
  option_77: string | null;
  client_id: string | null;
  fingerbank_device_id: number | null;
  fingerbank_device_name: string | null;
  fingerbank_device_class: string | null;
  fingerbank_manufacturer: string | null;
  fingerbank_score: number | null;
  fingerbank_last_lookup_at: string | null;
  fingerbank_last_error: string | null;
  first_seen_at: string;
  last_seen_at: string;
}

/** One observation in an IP's MAC history. ``vendor`` is best-effort
 *  via the OUI lookup feature. */
export interface MacHistoryEntry {
  id: string;
  mac_address: string;
  first_seen: string;
  last_seen: string;
  vendor?: string | null;
}

export interface SubnetAlias {
  id: string;
  zone_id: string;
  name: string;
  record_type: string;
  value: string;
  fqdn: string;
  ip_address_id: string;
  ip_address: string;
  ip_hostname: string | null;
}

export interface SubnetDomain {
  id: string;
  subnet_id: string;
  dns_zone_id: string;
  is_primary: boolean;
  zone_name: string | null;
}

export interface EffectiveFields {
  subnet_id: string;
  tags: Record<string, unknown>;
  custom_fields: Record<string, unknown>;
  tag_sources: Record<string, string>;
  custom_field_sources: Record<string, string>;
}

export interface BlockEffectiveFields {
  block_id: string;
  tags: Record<string, unknown>;
  custom_fields: Record<string, unknown>;
  tag_sources: Record<string, string>;
  custom_field_sources: Record<string, string>;
}

export interface SubnetBulkEditChanges {
  name?: string;
  description?: string;
  status?: string;
  vlan_id?: number;
  tags?: Record<string, unknown>;
  custom_fields?: Record<string, unknown>;
}

export interface SubnetBulkEditResponse {
  batch_id: string;
  updated_count: number;
  not_found: string[];
}

// ── Resize (grow-only) ─────────────────────────────────────────────────────
//
// Two-phase contract (preview → commit). Preview is a pure read; commit
// takes a pg advisory lock and re-validates before mutating. Shrinking is
// explicitly out of scope — the backend returns 422 if the new prefix
// length is >= the current one.

export interface ResizeConflict {
  type: string;
  detail: string;
}

export interface SubnetResizePlaceholder {
  ip: string;
  hostname: string;
}

export interface SubnetResizePreviewRequest {
  new_cidr: string;
  move_gateway_to_first_usable?: boolean;
}

export interface SubnetResizePreviewResponse {
  old_cidr: string;
  new_cidr: string;
  network_address_shifts: boolean;
  old_network_ip: string;
  new_network_ip: string;
  old_broadcast_ip: string | null;
  new_broadcast_ip: string | null;
  total_ips_before: number;
  total_ips_after: number;
  gateway_current: string | null;
  gateway_suggested_new_first_usable: string | null;
  placeholders_default_named: SubnetResizePlaceholder[];
  placeholders_renamed: SubnetResizePlaceholder[];
  affected_ip_addresses_total: number;
  affected_dhcp_scopes: number;
  affected_dhcp_pools: number;
  affected_dhcp_static_assignments: number;
  affected_dns_records_auto: number;
  affected_active_leases: number;
  reverse_zones_existing: string[];
  reverse_zones_will_be_created: string[];
  conflicts: ResizeConflict[];
  warnings: string[];
}

export interface SubnetResizeCommitRequest {
  new_cidr: string;
  move_gateway_to_first_usable?: boolean;
  replace_default_placeholders?: boolean;
}

export interface SubnetResizeCommitResponse {
  subnet: Subnet;
  old_cidr: string;
  new_cidr: string;
  placeholders_deleted: number;
  placeholders_created: number;
  dhcp_servers_notified: number;
  summary: string[];
}

export interface BlockResizeChildRow {
  id: string;
  network: string;
  name: string;
}

export interface BlockResizePreviewRequest {
  new_cidr: string;
}

export interface BlockResizePreviewResponse {
  old_cidr: string;
  new_cidr: string;
  network_address_shifts: boolean;
  old_network_ip: string;
  new_network_ip: string;
  total_ips_before: number;
  total_ips_after: number;
  child_blocks_count: number;
  child_blocks: BlockResizeChildRow[];
  child_subnets_count: number;
  child_subnets: BlockResizeChildRow[];
  descendant_ip_addresses_total: number;
  conflicts: ResizeConflict[];
  warnings: string[];
}

export interface BlockResizeCommitRequest {
  new_cidr: string;
}

export interface BlockResizeCommitResponse {
  block: IPBlock;
  old_cidr: string;
  new_cidr: string;
  summary: string[];
}

// ── Block move (issue #27) ───────────────────────────────────────────────
//
// Operator-driven relocation of an IPBlock + everything under it
// (descendant blocks, subnets, addresses) into a different IPSpace.
// Preview is a pure read of the blast radius; commit takes a per-block
// advisory lock and refuses if any descendant is owned by an integration
// reconciler (kubernetes / docker / proxmox / tailscale FK set).

export interface BlockMovePreviewRequest {
  target_space_id: string;
  target_parent_id?: string | null;
}

export interface BlockMoveIntegrationBlocker {
  kind: "block" | "subnet" | "ip_address";
  resource_id: string;
  network: string;
  integration: "kubernetes" | "docker" | "proxmox" | "tailscale";
}

export interface BlockMovePreviewResponse {
  block_id: string;
  block_network: string;
  source_space_id: string;
  target_space_id: string;
  target_parent_id: string | null;
  descendant_blocks_count: number;
  descendant_subnets_count: number;
  descendant_ip_addresses_total: number;
  reparent_chain_block_ids: string[];
  integration_blockers: BlockMoveIntegrationBlocker[];
  warnings: string[];
}

export interface BlockMoveCommitRequest {
  target_space_id: string;
  target_parent_id?: string | null;
  confirmation_cidr: string;
}

export interface BlockMoveCommitResponse {
  block: IPBlock;
  source_space_id: string;
  target_space_id: string;
  target_parent_id: string | null;
  blocks_moved: number;
  subnets_moved: number;
  addresses_in_moved_subtree: number;
  reparented_block_ids: string[];
}

// ── Free-space finder ──────────────────────────────────────────────────────
//
// Sweep an IPSpace (or one block subtree) for unused CIDRs of the
// requested prefix length. Empty space yields HTTP 200 with
// `summary.warning="space has no blocks"` so the UI can render a
// "create a block first" nudge instead of an error.

export interface FindFreeRequest {
  prefix_length: number;
  address_family?: 4 | 6;
  count?: number;
  min_free_addresses?: number | null;
  parent_block_id?: string | null;
}

export interface FindFreeCandidate {
  cidr: string;
  parent_block_id: string;
  parent_block_cidr: string;
  free_addresses?: number | null;
}

export interface FindFreeResponse {
  candidates: FindFreeCandidate[];
  summary: Record<string, string | number>;
}

// ── Subnet split ───────────────────────────────────────────────────────────

export interface SplitChildPreview {
  cidr: string;
  allocations_count: number;
  placeholders_default_named: number;
  placeholders_renamed: number;
  dhcp_scope_id: string | null;
  dhcp_pool_count: number;
  dhcp_static_count: number;
  dns_record_count: number;
}

export interface SplitSubnetPreviewRequest {
  new_prefix_length: number;
}

export interface SplitSubnetPreviewResponse {
  parent_cidr: string;
  new_prefix_length: number;
  children: SplitChildPreview[];
  conflicts: ResizeConflict[];
  warnings: string[];
}

export interface SplitSubnetCommitRequest {
  new_prefix_length: number;
  confirm_cidr: string;
}

export interface SplitSubnetCommitResponse {
  parent_cidr: string;
  children: Subnet[];
  summary: string[];
}

// ── Subnet merge ───────────────────────────────────────────────────────────

export interface MergeSourceRow {
  id: string;
  cidr: string;
}

export interface MergeSubnetPreviewRequest {
  sibling_subnet_ids: string[];
}

export interface MergeSubnetPreviewResponse {
  merged_cidr: string | null;
  source_subnets: MergeSourceRow[];
  surviving_dhcp_scope_id: string | null;
  conflicts: ResizeConflict[];
  warnings: string[];
}

export interface MergeSubnetCommitRequest {
  sibling_subnet_ids: string[];
  confirm_cidr: string;
}

export interface MergeSubnetCommitResponse {
  merged_subnet: Subnet;
  deleted_subnet_ids: string[];
  summary: string[];
}

// ── Bulk allocate (subnet-level) ──────────────────────────────────────────
//
// Stamp a contiguous IP range with name templating in one shot. Token
// language is mirrored client-side in BulkAllocateModal for live preview;
// keep the regex in sync with the backend `_BULK_TEMPLATE_RE`.

export interface BulkAllocateItem {
  address: string;
  hostname: string;
  fqdn: string | null;
  in_use: boolean;
  in_dynamic_pool: boolean;
  fqdn_collision: boolean;
}

export interface BulkAllocateRequest {
  range_start: string;
  range_end: string;
  hostname_template: string;
  template_start?: number;
  status?: string;
  description?: string | null;
  dns_zone_id?: string | null;
  create_dns_records?: boolean;
  on_collision?: "skip" | "abort";
  tags?: Record<string, unknown>;
  custom_fields?: Record<string, unknown>;
}

export interface BulkAllocatePreviewResponse {
  total: number;
  will_create: number;
  conflicts_in_use: number;
  conflicts_in_pool: number;
  conflicts_fqdn: number;
  sample: BulkAllocateItem[];
  warnings: string[];
}

export interface BulkAllocateCommitResponse {
  created: number;
  skipped_in_use: number;
  skipped_in_pool: number;
  skipped_fqdn_collision: number;
  sample_created: string[];
  summary: string[];
}

// ── IPAM Templates (issue #26) ────────────────────────────────────────

export type IPAMTemplateAppliesTo = "block" | "subnet";

export interface IPAMTemplateChildLayoutEntry {
  prefix: number;
  name_template: string;
  description?: string;
  tags?: Record<string, unknown>;
  custom_fields?: Record<string, unknown>;
}

export interface IPAMTemplateChildLayout {
  children: IPAMTemplateChildLayoutEntry[];
}

export interface IPAMTemplate {
  id: string;
  name: string;
  description: string;
  applies_to: IPAMTemplateAppliesTo;
  tags: Record<string, unknown>;
  custom_fields: Record<string, unknown>;
  dns_group_id: string | null;
  dns_zone_id: string | null;
  dns_additional_zone_ids: string[] | null;
  dhcp_group_id: string | null;
  ddns_enabled: boolean;
  ddns_hostname_policy: DdnsHostnamePolicy;
  ddns_domain_override: string | null;
  ddns_ttl: number | null;
  child_layout: IPAMTemplateChildLayout | null;
  applied_count: number;
  created_at: string;
  modified_at: string;
}

export interface IPAMTemplateCreate {
  name: string;
  description?: string;
  applies_to: IPAMTemplateAppliesTo;
  tags?: Record<string, unknown>;
  custom_fields?: Record<string, unknown>;
  dns_group_id?: string | null;
  dns_zone_id?: string | null;
  dns_additional_zone_ids?: string[];
  dhcp_group_id?: string | null;
  ddns_enabled?: boolean;
  ddns_hostname_policy?: DdnsHostnamePolicy;
  ddns_domain_override?: string | null;
  ddns_ttl?: number | null;
  child_layout?: IPAMTemplateChildLayout | null;
}

export interface IPAMTemplateUpdate {
  name?: string;
  description?: string;
  tags?: Record<string, unknown>;
  custom_fields?: Record<string, unknown>;
  dns_group_id?: string | null;
  dns_zone_id?: string | null;
  dns_additional_zone_ids?: string[] | null;
  dhcp_group_id?: string | null;
  ddns_enabled?: boolean;
  ddns_hostname_policy?: DdnsHostnamePolicy;
  ddns_domain_override?: string | null;
  ddns_ttl?: number | null;
  child_layout?: IPAMTemplateChildLayout | null;
  clear_dns_group_id?: boolean;
  clear_dhcp_group_id?: boolean;
  clear_dns_zone_id?: boolean;
  clear_dns_additional_zone_ids?: boolean;
  clear_child_layout?: boolean;
  clear_ddns_domain_override?: boolean;
  clear_ddns_ttl?: boolean;
}

export interface TemplateApplyRequest {
  block_id?: string;
  subnet_id?: string;
  force?: boolean;
  carve_children?: boolean;
}

export interface TemplateApplyResponse {
  template_id: string;
  target_kind: "block" | "subnet";
  target_id: string;
  fields_written: string[];
  children_carved: { cidr: string; name: string; skipped: boolean }[];
}

export interface TemplateReapplyAllResponse {
  template_id: string;
  target_kind: "block" | "subnet";
  instances_total: number;
  instances_processed: number;
  instances_skipped: number;
  cap_reached: boolean;
}

export const ipamApi = {
  listSpaces: () => api.get<IPSpace[]>("/ipam/spaces").then((r) => r.data),
  getSpace: (id: string) =>
    api.get<IPSpace>(`/ipam/spaces/${id}`).then((r) => r.data),
  createSpace: (data: Partial<IPSpace>) =>
    api.post<IPSpace>("/ipam/spaces", data).then((r) => r.data),
  updateSpace: (id: string, data: Partial<IPSpace>) =>
    api.put<IPSpace>(`/ipam/spaces/${id}`, data).then((r) => r.data),
  deleteSpace: (id: string) => api.delete(`/ipam/spaces/${id}`),

  listBlocks: (spaceId?: string) =>
    api
      .get<
        IPBlock[]
      >("/ipam/blocks", { params: spaceId ? { space_id: spaceId } : undefined })
      .then((r) => r.data),
  createBlock: (data: Partial<IPBlock> & { template_id?: string | null }) =>
    api.post<IPBlock>("/ipam/blocks", data).then((r) => r.data),
  updateBlock: (
    id: string,
    data: Partial<
      Pick<
        IPBlock,
        | "name"
        | "description"
        | "parent_block_id"
        | "tags"
        | "custom_fields"
        | "dns_group_ids"
        | "dns_zone_id"
        | "dns_additional_zone_ids"
        | "dns_inherit_settings"
        | "dhcp_server_group_id"
        | "dhcp_inherit_settings"
        | "asn_id"
        | "vrf_id"
      >
    >,
  ) => api.put<IPBlock>(`/ipam/blocks/${id}`, data).then((r) => r.data),
  deleteBlock: (id: string) => api.delete(`/ipam/blocks/${id}`),
  availableSubnets: (blockId: string, prefixLen: number) =>
    api
      .get<
        string[]
      >(`/ipam/blocks/${blockId}/available-subnets`, { params: { prefix_len: prefixLen } })
      .then((r) => r.data),
  blockFreeSpace: (blockId: string) =>
    api
      .get<FreeCidrRange[]>(`/ipam/blocks/${blockId}/free-space`)
      .then((r) => r.data),
  blockAggregationSuggestions: (blockId: string) =>
    api
      .get<
        AggregationSuggestion[]
      >(`/ipam/blocks/${blockId}/aggregation-suggestions`)
      .then((r) => r.data),
  planBlockAllocation: (blockId: string, items: PlanRequestItem[]) =>
    api
      .post<PlanAllocationResponse>(`/ipam/blocks/${blockId}/plan-allocation`, {
        items,
      })
      .then((r) => r.data),

  // Subnet plans
  listSubnetPlans: (spaceId?: string) =>
    api
      .get<SubnetPlanRead[]>(`/ipam/plans`, {
        params: spaceId ? { space_id: spaceId } : undefined,
      })
      .then((r) => r.data),
  getSubnetPlan: (id: string) =>
    api.get<SubnetPlanRead>(`/ipam/plans/${id}`).then((r) => r.data),
  createSubnetPlan: (body: SubnetPlanCreate) =>
    api.post<SubnetPlanRead>(`/ipam/plans`, body).then((r) => r.data),
  updateSubnetPlan: (id: string, body: SubnetPlanUpdate) =>
    api.patch<SubnetPlanRead>(`/ipam/plans/${id}`, body).then((r) => r.data),
  deleteSubnetPlan: (id: string) =>
    api.delete<void>(`/ipam/plans/${id}`).then((r) => r.data),
  validateSubnetPlan: (id: string) =>
    api
      .post<PlanValidationResult>(`/ipam/plans/${id}/validate`)
      .then((r) => r.data),
  validateSubnetPlanTree: (body: SubnetPlanCreate) =>
    api
      .post<PlanValidationResult>(`/ipam/plans/validate-tree`, body)
      .then((r) => r.data),
  applySubnetPlan: (id: string) =>
    api.post<PlanApplyResult>(`/ipam/plans/${id}/apply`).then((r) => r.data),
  reopenSubnetPlan: (id: string) =>
    api.post<SubnetPlanRead>(`/ipam/plans/${id}/reopen`).then((r) => r.data),
  getEffectiveBlockDns: (blockId: string) =>
    api
      .get<EffectiveDns>(`/ipam/blocks/${blockId}/effective-dns`)
      .then((r) => r.data),
  getEffectiveSubnetDns: (subnetId: string) =>
    api
      .get<EffectiveDns>(`/ipam/subnets/${subnetId}/effective-dns`)
      .then((r) => r.data),
  getEffectiveSpaceDns: (spaceId: string) =>
    api
      .get<EffectiveDns>(`/ipam/spaces/${spaceId}/effective-dns`)
      .then((r) => r.data),
  getEffectiveBlockDhcp: (blockId: string) =>
    api
      .get<EffectiveDhcp>(`/ipam/blocks/${blockId}/effective-dhcp`)
      .then((r) => r.data),
  getEffectiveSubnetDhcp: (subnetId: string) =>
    api
      .get<EffectiveDhcp>(`/ipam/subnets/${subnetId}/effective-dhcp`)
      .then((r) => r.data),
  getEffectiveSpaceDhcp: (spaceId: string) =>
    api
      .get<EffectiveDhcp>(`/ipam/spaces/${spaceId}/effective-dhcp`)
      .then((r) => r.data),

  listSubnets: (params?: {
    space_id?: string;
    block_id?: string;
    vlan_ref_id?: string;
    pci_scope?: boolean;
    hipaa_scope?: boolean;
    internet_facing?: boolean;
  }) => api.get<Subnet[]>("/ipam/subnets", { params }).then((r) => r.data),
  getSubnet: (id: string) =>
    api.get<Subnet>(`/ipam/subnets/${id}`).then((r) => r.data),
  createSubnet: (data: Partial<Subnet> & { template_id?: string | null }) =>
    api.post<Subnet>("/ipam/subnets", data).then((r) => r.data),
  updateSubnet: (
    id: string,
    data: Partial<Subnet> & { manage_auto_addresses?: boolean },
  ) => api.put<Subnet>(`/ipam/subnets/${id}`, data).then((r) => r.data),
  // ``force`` cascades the delete: the backend refuses by default if
  // the subnet still has user-owned IP rows or attached DHCP scopes.
  // The UI's two confirmation modals already make the cascade
  // explicit ("…and all IP address records will be permanently
  // deleted") so they pass force=true; the bare-id callable shape is
  // kept for any future "soft" call site.
  deleteSubnet: (id: string, force: boolean = false) =>
    api.delete(`/ipam/subnets/${id}${force ? "?force=true" : ""}`),

  // Resize (grow-only) — preview is a pure read; commit takes a pg
  // advisory lock and returns 423 Locked if another resize is in flight
  // for the same subnet / block.
  resizeSubnetPreview: (subnetId: string, body: SubnetResizePreviewRequest) =>
    api
      .post<SubnetResizePreviewResponse>(
        `/ipam/subnets/${subnetId}/resize/preview`,
        body,
      )
      .then((r) => r.data),
  resizeSubnetCommit: (subnetId: string, body: SubnetResizeCommitRequest) =>
    api
      .post<SubnetResizeCommitResponse>(
        `/ipam/subnets/${subnetId}/resize`,
        body,
      )
      .then((r) => r.data),
  resizeBlockPreview: (blockId: string, body: BlockResizePreviewRequest) =>
    api
      .post<BlockResizePreviewResponse>(
        `/ipam/blocks/${blockId}/resize/preview`,
        body,
      )
      .then((r) => r.data),
  resizeBlockCommit: (blockId: string, body: BlockResizeCommitRequest) =>
    api
      .post<BlockResizeCommitResponse>(`/ipam/blocks/${blockId}/resize`, body)
      .then((r) => r.data),

  // ── Block move (issue #27) ───────────────────────────────────────────
  moveBlockPreview: (blockId: string, body: BlockMovePreviewRequest) =>
    api
      .post<BlockMovePreviewResponse>(
        `/ipam/blocks/${blockId}/move/preview`,
        body,
      )
      .then((r) => r.data),
  moveBlockCommit: (blockId: string, body: BlockMoveCommitRequest) =>
    api
      .post<BlockMoveCommitResponse>(
        `/ipam/blocks/${blockId}/move/commit`,
        body,
      )
      .then((r) => r.data),

  // ── Free-space finder ───────────────────────────────────────────────
  findFreeSpace: (spaceId: string, body: FindFreeRequest) =>
    api
      .post<FindFreeResponse>(`/ipam/spaces/${spaceId}/find-free`, body)
      .then((r) => r.data),

  // ── Subnet split / merge ────────────────────────────────────────────
  splitSubnetPreview: (subnetId: string, body: SplitSubnetPreviewRequest) =>
    api
      .post<SplitSubnetPreviewResponse>(
        `/ipam/subnets/${subnetId}/split/preview`,
        body,
      )
      .then((r) => r.data),
  splitSubnetCommit: (subnetId: string, body: SplitSubnetCommitRequest) =>
    api
      .post<SplitSubnetCommitResponse>(
        `/ipam/subnets/${subnetId}/split/commit`,
        body,
      )
      .then((r) => r.data),
  mergeSubnetPreview: (subnetId: string, body: MergeSubnetPreviewRequest) =>
    api
      .post<MergeSubnetPreviewResponse>(
        `/ipam/subnets/${subnetId}/merge/preview`,
        body,
      )
      .then((r) => r.data),
  mergeSubnetCommit: (subnetId: string, body: MergeSubnetCommitRequest) =>
    api
      .post<MergeSubnetCommitResponse>(
        `/ipam/subnets/${subnetId}/merge/commit`,
        body,
      )
      .then((r) => r.data),

  bulkAllocatePreview: (subnetId: string, body: BulkAllocateRequest) =>
    api
      .post<BulkAllocatePreviewResponse>(
        `/ipam/subnets/${subnetId}/bulk-allocate/preview`,
        body,
      )
      .then((r) => r.data),
  bulkAllocateCommit: (subnetId: string, body: BulkAllocateRequest) =>
    api
      .post<BulkAllocateCommitResponse>(
        `/ipam/subnets/${subnetId}/bulk-allocate`,
        body,
      )
      .then((r) => r.data),

  dnsSyncPreview: (subnetId: string) =>
    api
      .get<DnsSyncPreview>(`/ipam/subnets/${subnetId}/dns-sync/preview`)
      .then((r) => r.data),
  dnsSyncSummary: (subnetId: string) =>
    api
      .get<DnsSyncSummary>(`/ipam/subnets/${subnetId}/dns-sync/summary`)
      .then((r) => r.data),
  dnsSyncCommit: (
    subnetId: string,
    body: {
      create_for_ip_ids?: string[];
      update_record_ids?: string[];
      delete_stale_record_ids?: string[];
    },
  ) =>
    api
      .post<DnsSyncCommitResult>(
        `/ipam/subnets/${subnetId}/dns-sync/commit`,
        body,
      )
      .then((r) => r.data),

  dnsSyncPreviewBlock: (blockId: string) =>
    api
      .get<DnsSyncPreview>(`/ipam/blocks/${blockId}/dns-sync/preview`)
      .then((r) => r.data),
  dnsSyncCommitBlock: (
    blockId: string,
    body: {
      create_for_ip_ids?: string[];
      update_record_ids?: string[];
      delete_stale_record_ids?: string[];
    },
  ) =>
    api
      .post<DnsSyncCommitResult>(
        `/ipam/blocks/${blockId}/dns-sync/commit`,
        body,
      )
      .then((r) => r.data),

  dnsSyncPreviewSpace: (spaceId: string) =>
    api
      .get<DnsSyncPreview>(`/ipam/spaces/${spaceId}/dns-sync/preview`)
      .then((r) => r.data),
  dnsSyncCommitSpace: (
    spaceId: string,
    body: {
      create_for_ip_ids?: string[];
      update_record_ids?: string[];
      delete_stale_record_ids?: string[];
    },
  ) =>
    api
      .post<DnsSyncCommitResult>(
        `/ipam/spaces/${spaceId}/dns-sync/commit`,
        body,
      )
      .then((r) => r.data),

  listAddresses: (subnetId: string) =>
    api
      .get<IPAddress[]>(`/ipam/subnets/${subnetId}/addresses`)
      .then((r) => r.data),
  createAddress: (
    data: Partial<IPAddress> & {
      hostname: string;
      dns_zone_id?: string | null;
      aliases?: { name: string; record_type: "CNAME" | "A" }[];
      /** Re-submit flag after the user confirms a 409 collision warning. */
      force?: boolean;
    },
  ) =>
    api
      .post<IPAddress>(`/ipam/subnets/${data.subnet_id}/addresses`, data)
      .then((r) => r.data),
  updateAddress: (
    id: string,
    data: Partial<IPAddress> & {
      dns_zone_id?: string | null;
      /** Re-submit flag after the user confirms a 409 collision warning. */
      force?: boolean;
    },
  ) => api.put<IPAddress>(`/ipam/addresses/${id}`, data).then((r) => r.data),
  deleteAddress: (id: string, permanent = false) =>
    api.delete(`/ipam/addresses/${id}`, {
      params: permanent ? { permanent: true } : undefined,
    }),
  /** Operator-triggered "Re-profile now" — bypasses the subnet's
   *  refresh-window dedupe but still respects the per-subnet
   *  concurrency cap (returns 429 when full). */
  profileAddress: (id: string, preset?: string) =>
    api
      .post<{
        scan_id: string;
        preset: string;
        status: string;
      }>(`/ipam/addresses/${id}/profile`, { preset: preset ?? null })
      .then((r) => r.data),
  /** Fetch the passive DHCP fingerprint joined to this IP's MAC.
   *  404 when the IP has no MAC or no fingerprint has been captured
   *  yet — callers should swallow that and treat it as "no data". */
  getDhcpFingerprint: (id: string) =>
    api
      .get<DHCPFingerprintResponse>(`/ipam/addresses/${id}/dhcp-fingerprint`)
      .then((r) => r.data),
  listAliases: (addressId: string) =>
    api
      .get<
        {
          id: string;
          name: string;
          record_type: string;
          value: string;
          zone_id: string;
          fqdn: string;
        }[]
      >(`/ipam/addresses/${addressId}/aliases`)
      .then((r) => r.data),
  /** Pull every distinct MAC ever observed on this IP, newest-first. */
  listMacHistory: (addressId: string) =>
    api
      .get<MacHistoryEntry[]>(`/ipam/addresses/${addressId}/mac-history`)
      .then((r) => r.data),
  addAlias: (
    addressId: string,
    data: { name: string; record_type: "CNAME" | "A" },
  ) =>
    api
      .post<{
        id: string;
        name: string;
        record_type: string;
        value: string;
        zone_id: string;
        fqdn: string;
      }>(`/ipam/addresses/${addressId}/aliases`, data)
      .then((r) => r.data),
  deleteAlias: (addressId: string, recordId: string) =>
    api.delete(`/ipam/addresses/${addressId}/aliases/${recordId}`),
  listSubnetAliases: (subnetId: string) =>
    api
      .get<SubnetAlias[]>(`/ipam/subnets/${subnetId}/aliases`)
      .then((r) => r.data),
  bulkDeleteAddresses: (data: { ip_ids: string[]; permanent?: boolean }) =>
    api
      .post<{
        deleted_count: number;
        not_found: string[];
        skipped: string[];
      }>(`/ipam/addresses/bulk-delete`, data)
      .then((r) => r.data),
  bulkEditAddresses: (data: {
    ip_ids: string[];
    changes: {
      status?: string;
      description?: string;
      tags?: Record<string, unknown>;
      custom_fields?: Record<string, unknown>;
      /** New forward-zone for every selected IP. Empty string clears. */
      dns_zone_id?: string;
      /** Curated role tag. Empty string clears (= no specific role). */
      role?: IPRole | "" | null;
      /** TTL on reservations. ``null`` clears, ISO 8601 string sets. */
      reserved_until?: string | null;
    };
  }) =>
    api
      .post<{
        batch_id: string;
        updated_count: number;
        not_found: string[];
        skipped: string[];
      }>(`/ipam/addresses/bulk-edit`, data)
      .then((r) => r.data),
  backfillReverseZonesSpace: (spaceId: string) =>
    api
      .post<{
        created: { subnet: string; zone: string }[];
        skipped: number;
      }>(`/ipam/spaces/${spaceId}/reverse-zones/backfill`)
      .then((r) => r.data),
  backfillReverseZonesBlock: (blockId: string) =>
    api
      .post<{
        created: { subnet: string; zone: string }[];
        skipped: number;
      }>(`/ipam/blocks/${blockId}/reverse-zones/backfill`)
      .then((r) => r.data),
  backfillReverseZonesSubnet: (subnetId: string) =>
    api
      .post<{
        created: { subnet: string; zone: string }[];
        skipped: number;
      }>(`/ipam/subnets/${subnetId}/reverse-zones/backfill`)
      .then((r) => r.data),
  purgeOrphans: (subnetId: string, ipIds: string[]) =>
    api
      .post<{ purged: number }>(`/ipam/subnets/${subnetId}/orphans/purge`, {
        ip_ids: ipIds,
      })
      .then((r) => r.data),
  nextAddress: (
    subnetId: string,
    data: {
      hostname: string;
      status?: string;
      mac_address?: string;
      description?: string;
      custom_fields?: Record<string, unknown>;
      dns_zone_id?: string | null;
      /** Issue #25 — additional zones to publish A/AAAA records into. */
      extra_zone_ids?: string[];
      aliases?: { name: string; record_type: "CNAME" | "A" }[];
      role?: IPRole;
      reserved_until?: string;
      /** Re-submit flag after the user confirms a 409 collision warning. */
      force?: boolean;
    },
  ) =>
    api
      .post<IPAddress>(`/ipam/subnets/${subnetId}/next`, data)
      .then((r) => r.data),
  previewNextIp: (
    subnetId: string,
    strategy: "sequential" | "random" = "sequential",
  ) =>
    api
      .get<{
        address: string | null;
        strategy: string;
      }>(`/ipam/subnets/${subnetId}/next-ip-preview`, { params: { strategy } })
      .then((r) => r.data),

  // Subnet ↔ DNS domain associations (§11)
  listSubnetDomains: (subnetId: string) =>
    api
      .get<SubnetDomain[]>(`/ipam/subnets/${subnetId}/domains`)
      .then((r) => r.data),
  addSubnetDomain: (
    subnetId: string,
    data: { dns_zone_id: string; is_primary?: boolean },
  ) =>
    api
      .post<SubnetDomain>(`/ipam/subnets/${subnetId}/domains`, data)
      .then((r) => r.data),
  removeSubnetDomain: (subnetId: string, domainId: string) =>
    api.delete(`/ipam/subnets/${subnetId}/domains/${domainId}`),

  // Effective (inherited) tags + custom_fields (§11)
  effectiveFields: (subnetId: string) =>
    api
      .get<EffectiveFields>(`/ipam/subnets/${subnetId}/effective-fields`)
      .then((r) => r.data),
  effectiveBlockFields: (blockId: string) =>
    api
      .get<BlockEffectiveFields>(`/ipam/blocks/${blockId}/effective-fields`)
      .then((r) => r.data),

  // Bulk edit multiple subnets in one transaction (§11)
  bulkEditSubnets: (subnet_ids: string[], changes: SubnetBulkEditChanges) =>
    api
      .post<SubnetBulkEditResponse>("/ipam/subnets/bulk-edit", {
        subnet_ids,
        changes,
      })
      .then((r) => r.data),

  // ── IPAM templates (issue #26) ────────────────────────────────────
  listTemplates: (params?: {
    applies_to?: IPAMTemplateAppliesTo;
    search?: string;
  }) =>
    api.get<IPAMTemplate[]>("/ipam/templates", { params }).then((r) => r.data),
  getTemplate: (id: string) =>
    api.get<IPAMTemplate>(`/ipam/templates/${id}`).then((r) => r.data),
  createTemplate: (body: IPAMTemplateCreate) =>
    api.post<IPAMTemplate>("/ipam/templates", body).then((r) => r.data),
  updateTemplate: (id: string, body: IPAMTemplateUpdate) =>
    api.put<IPAMTemplate>(`/ipam/templates/${id}`, body).then((r) => r.data),
  deleteTemplate: (id: string) =>
    api.delete<void>(`/ipam/templates/${id}`).then((r) => r.data),
  applyTemplate: (id: string, body: TemplateApplyRequest) =>
    api
      .post<TemplateApplyResponse>(`/ipam/templates/${id}/apply`, body)
      .then((r) => r.data),
  reapplyAllTemplate: (id: string) =>
    api
      .post<TemplateReapplyAllResponse>(`/ipam/templates/${id}/reapply-all`, {})
      .then((r) => r.data),
};

// ── IPAM Import / Export ───────────────────────────────────────────────────────

export interface ImportDiffRow {
  kind: "subnet" | "block" | "address";
  action: "create" | "update" | "conflict" | "skip" | "error";
  network: string;
  name?: string;
  reason?: string | null;
  details?: Record<string, unknown>;
}

export interface ImportPreviewResponse {
  space_id: string;
  space_name: string;
  summary: {
    creates: number;
    updates: number;
    conflicts: number;
    errors: number;
  };
  creates: ImportDiffRow[];
  updates: ImportDiffRow[];
  conflicts: ImportDiffRow[];
  errors: ImportDiffRow[];
}

export interface ImportCommitResponse {
  space_id: string;
  created_subnets: number;
  updated_subnets: number;
  skipped: number;
  auto_created_blocks: number;
  errors: string[];
}

export interface AddressImportCommitResponse {
  subnet_id: string;
  created: number;
  updated: number;
  skipped: number;
  dns_synced: number;
  errors: string[];
}

export type ImportStrategy = "skip" | "overwrite" | "fail";

function _buildImportForm(
  file: File,
  opts: { space_id?: string; space_name?: string; strategy: ImportStrategy },
): FormData {
  const form = new FormData();
  form.append("file", file);
  if (opts.space_id) form.append("space_id", opts.space_id);
  if (opts.space_name) form.append("space_name", opts.space_name);
  form.append("strategy", opts.strategy);
  return form;
}

export const ipamIoApi = {
  preview: (
    file: File,
    opts: { space_id?: string; space_name?: string; strategy: ImportStrategy },
  ) =>
    api
      .post<ImportPreviewResponse>(
        "/ipam/import/preview",
        _buildImportForm(file, opts),
        {
          headers: { "Content-Type": "multipart/form-data" },
        },
      )
      .then((r) => r.data),

  commit: (
    file: File,
    opts: { space_id?: string; space_name?: string; strategy: ImportStrategy },
  ) =>
    api
      .post<ImportCommitResponse>(
        "/ipam/import/commit",
        _buildImportForm(file, opts),
        {
          headers: { "Content-Type": "multipart/form-data" },
        },
      )
      .then((r) => r.data),

  previewAddresses: (
    file: File,
    opts: { subnet_id: string; strategy: ImportStrategy },
  ) => {
    const form = new FormData();
    form.append("file", file);
    form.append("subnet_id", opts.subnet_id);
    form.append("strategy", opts.strategy);
    return api
      .post<ImportPreviewResponse>("/ipam/import/addresses/preview", form, {
        headers: { "Content-Type": "multipart/form-data" },
      })
      .then((r) => r.data);
  },

  commitAddresses: (
    file: File,
    opts: { subnet_id: string; strategy: ImportStrategy },
  ) => {
    const form = new FormData();
    form.append("file", file);
    form.append("subnet_id", opts.subnet_id);
    form.append("strategy", opts.strategy);
    return api
      .post<AddressImportCommitResponse>(
        "/ipam/import/addresses/commit",
        form,
        {
          headers: { "Content-Type": "multipart/form-data" },
        },
      )
      .then((r) => r.data);
  },

  exportUrl: (params: {
    space_id?: string;
    block_id?: string;
    subnet_id?: string;
    format: "csv" | "json" | "xlsx";
    include_addresses?: boolean;
  }) => {
    const qs = new URLSearchParams();
    if (params.space_id) qs.set("space_id", params.space_id);
    if (params.block_id) qs.set("block_id", params.block_id);
    if (params.subnet_id) qs.set("subnet_id", params.subnet_id);
    qs.set("format", params.format);
    if (params.include_addresses) qs.set("include_addresses", "true");
    return `/ipam/export?${qs.toString()}`;
  },

  /** Download an export using the caller's auth token. */
  download: async (params: {
    space_id?: string;
    block_id?: string;
    subnet_id?: string;
    format: "csv" | "json" | "xlsx";
    include_addresses?: boolean;
  }) => {
    const res = await api.get(ipamIoApi.exportUrl(params), {
      responseType: "blob",
    });
    const disp = (res.headers["content-disposition"] as string) || "";
    const match = disp.match(/filename="?([^";]+)"?/i);
    // UTC YYYYMMDD-HHMMSS matches the backend's export filename convention
    // so the fallback (no Content-Disposition) sorts alongside real exports.
    const ts = new Date()
      .toISOString()
      .slice(0, 19)
      .replace(/[-:]/g, "")
      .replace("T", "-");
    const filename = match ? match[1] : `ipam-export-${ts}.${params.format}`;
    const blob = new Blob([res.data as BlobPart]);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  },
};

export interface LoginResponse {
  // Set when MFA is NOT required — normal token issuance.
  access_token: string | null;
  refresh_token: string | null;
  token_type: string;
  force_password_change: boolean;
  // Set when the user has TOTP enabled (issue #69). The frontend
  // routes the operator into a TOTP prompt + posts to /auth/login/mfa
  // with this challenge token + the second factor.
  mfa_required?: boolean;
  mfa_token?: string | null;
}

export interface MfaStatusResponse {
  enabled: boolean;
  enrolment_pending: boolean;
  recovery_codes_remaining: number;
}

export interface MfaEnrolBeginResponse {
  secret: string;
  otpauth_uri: string;
  recovery_codes: string[];
}

export interface AppUser {
  id: string;
  username: string;
  email: string;
  display_name: string;
  is_active: boolean;
  is_superadmin: boolean;
  force_password_change: boolean;
  auth_source: string;
  last_login_at: string | null;
}

export const usersApi = {
  list: () => api.get<AppUser[]>("/users").then((r) => r.data),
  get: (id: string) => api.get<AppUser>(`/users/${id}`).then((r) => r.data),
  create: (data: {
    username: string;
    email: string;
    display_name: string;
    password: string;
    is_superadmin: boolean;
    force_password_change: boolean;
  }) => api.post<AppUser>("/users", data).then((r) => r.data),
  update: (
    id: string,
    data: Partial<
      Pick<
        AppUser,
        | "display_name"
        | "email"
        | "is_active"
        | "is_superadmin"
        | "force_password_change"
      >
    >,
  ) => api.put<AppUser>(`/users/${id}`, data).then((r) => r.data),
  resetPassword: (id: string, newPassword: string) =>
    api.post(`/users/${id}/reset-password`, { new_password: newPassword }),
  delete: (id: string) => api.delete(`/users/${id}`),
};

export interface AuditLogEntry {
  id: string;
  timestamp: string;
  user_display_name: string;
  auth_source: string;
  action: string;
  resource_type: string;
  resource_id: string;
  resource_display: string;
  result: string;
  source_ip: string | null;
}

export interface AuditLogPage {
  total: number;
  items: AuditLogEntry[];
}

export const auditApi = {
  list: (params?: {
    limit?: number;
    offset?: number;
    action?: string;
    resource_type?: string;
    user_display_name?: string;
    resource_display?: string;
    result?: string;
    source_ip?: string;
  }) => api.get<AuditLogPage>("/audit", { params }).then((r) => r.data),
};

// ── Search ─────────────────────────────────────────────────────────────────────

export interface SearchResult {
  type:
    | "ip_address"
    | "subnet"
    | "block"
    | "space"
    | "dns_group"
    | "dns_zone"
    | "dns_record";
  id: string;
  display: string;
  name: string | null;
  status: string | null;
  description: string | null;
  hostname: string | null;
  mac_address: string | null;
  // IPAM breadcrumb
  subnet_id: string | null;
  subnet_network: string | null;
  block_id: string | null;
  space_id: string | null;
  space_name: string | null;
  // DNS context
  dns_group_id: string | null;
  dns_group_name: string | null;
  dns_zone_id: string | null;
  dns_zone_name: string | null;
  dns_record_type: string | null;
  dns_record_value: string | null;
  matched_field?: string | null;
}

export interface SearchResponse {
  query: string;
  total: number;
  results: SearchResult[];
}

export const searchApi = {
  search: (q: string, types?: string, limit = 25) =>
    api
      .get<SearchResponse>("/search", { params: { q, types, limit } })
      .then((r) => r.data),
};

// ── Settings ───────────────────────────────────────────────────────────────────

export interface PlatformSettings {
  app_title: string;
  app_base_url: string;
  dns_auto_sync_enabled: boolean;
  dns_auto_sync_interval_minutes: number;
  dns_auto_sync_delete_stale: boolean;
  dns_auto_sync_last_run_at: string | null;
  dns_pull_from_server_enabled: boolean;
  dns_pull_from_server_interval_minutes: number;
  dns_pull_from_server_last_run_at: string | null;
  dhcp_pull_leases_enabled: boolean;
  dhcp_pull_leases_interval_seconds: number;
  dhcp_pull_leases_last_run_at: string | null;
  audit_forward_syslog_enabled: boolean;
  audit_forward_syslog_host: string;
  audit_forward_syslog_port: number;
  audit_forward_syslog_protocol: string;
  audit_forward_syslog_facility: number;
  audit_forward_webhook_enabled: boolean;
  audit_forward_webhook_url: string;
  audit_forward_webhook_auth_header: string;
  ip_allocation_strategy: string;
  session_timeout_minutes: number;
  auto_logout_minutes: number;
  utilization_warn_threshold: number;
  utilization_critical_threshold: number;
  utilization_max_prefix_ipv4: number;
  utilization_max_prefix_ipv6: number;
  subnet_tree_default_expanded_depth: number;
  github_release_check_enabled: boolean;
  dns_default_ttl: number;
  dns_default_zone_type: string;
  dns_default_dnssec_validation: string;
  dns_recursive_by_default: boolean;
  dhcp_default_dns_servers: string[];
  dhcp_default_domain_name: string;
  dhcp_default_domain_search: string[];
  dhcp_default_ntp_servers: string[];
  dhcp_default_lease_time: number;
  oui_lookup_enabled: boolean;
  oui_update_interval_hours: number;
  oui_last_updated_at: string | null;
  integration_kubernetes_enabled: boolean;
  integration_docker_enabled: boolean;
  integration_proxmox_enabled: boolean;
  integration_tailscale_enabled: boolean;
  /** Domain WHOIS refresh cadence (hours). Beat ticks hourly; the
   *  task itself reads this on every fire so cadence changes take
   *  effect on the next tick without restarting beat. 1–168 h range
   *  enforced server-side. */
  domain_whois_interval_hours: number;
  asn_whois_interval_hours: number;
  rpki_roa_source: string;
  rpki_roa_refresh_interval_hours: number;
  vrf_strict_rd_validation: boolean;
  /** Read-only — true when an encrypted fingerbank API key is on file.
   *  The plaintext is never returned. Submit a value via
   *  ``fingerbank_api_key`` on the update payload to set or clear it
   *  (empty string clears). */
  fingerbank_api_key_set: boolean;
  /** Write-only — Fernet-encrypted server-side. Set to a non-empty
   *  string to store; set to "" to clear; omit to leave unchanged. */
  fingerbank_api_key?: string;
}

export interface OUIStatus {
  enabled: boolean;
  interval_hours: number;
  last_updated_at: string | null;
  vendor_count: number;
}

export interface OUITaskStatus {
  task_id: string;
  state: string; // "PENDING" | "STARTED" | "SUCCESS" | "FAILURE" | "RETRY"
  ready: boolean;
  result:
    | ({
        status?: string; // "ran" | "disabled" | "skipped" | "error"
        total?: number;
        added?: number;
        updated?: number;
        removed?: number;
        unchanged?: number;
        forced?: boolean;
        reason?: string;
        detail?: string;
      } & Record<string, unknown>)
    | null;
  error: string | null;
}

export type AuditForwardKind = "syslog" | "webhook" | "smtp";
export type AuditForwardWebhookFlavor =
  | "generic"
  | "slack"
  | "teams"
  | "discord";
export type AuditForwardSmtpSecurity = "none" | "starttls" | "ssl";
export type AuditForwardFormat =
  | "rfc5424_json"
  | "rfc5424_cef"
  | "rfc5424_leef"
  | "rfc3164"
  | "json_lines";
export type AuditForwardProtocol = "udp" | "tcp" | "tls";
export type AuditForwardSeverity = "info" | "warn" | "error" | "denied";

export interface AuditForwardTarget {
  id: string;
  name: string;
  enabled: boolean;
  kind: AuditForwardKind;
  format: AuditForwardFormat;
  host: string;
  port: number;
  protocol: AuditForwardProtocol;
  facility: number;
  ca_cert_pem: string | null;
  url: string;
  auth_header_set: boolean;
  webhook_flavor: AuditForwardWebhookFlavor;
  smtp_host: string;
  smtp_port: number;
  smtp_security: AuditForwardSmtpSecurity;
  smtp_username: string;
  // Server returns only a boolean (the password is Fernet-encrypted at rest).
  smtp_password_set: boolean;
  smtp_from_address: string;
  smtp_to_addresses: string[] | null;
  smtp_reply_to: string;
  min_severity: AuditForwardSeverity | null;
  resource_types: string[] | null;
  created_at: string;
  modified_at: string;
}

export interface AuditForwardTargetWrite {
  name: string;
  enabled: boolean;
  kind: AuditForwardKind;
  format: AuditForwardFormat;
  host?: string;
  port?: number;
  protocol?: AuditForwardProtocol;
  facility?: number;
  ca_cert_pem?: string | null;
  url?: string;
  auth_header?: string;
  webhook_flavor?: AuditForwardWebhookFlavor;
  smtp_host?: string;
  smtp_port?: number;
  smtp_security?: AuditForwardSmtpSecurity;
  smtp_username?: string;
  // ``null`` keeps the existing encrypted password, ``""`` clears it,
  // any other string is sent in plaintext and encrypted server-side.
  smtp_password?: string | null;
  smtp_from_address?: string;
  smtp_to_addresses?: string[] | null;
  smtp_reply_to?: string;
  min_severity?: AuditForwardSeverity | null;
  resource_types?: string[] | null;
}

export const settingsApi = {
  get: () => api.get<PlatformSettings>("/settings").then((r) => r.data),
  update: (data: Partial<PlatformSettings>) =>
    api.put<PlatformSettings>("/settings", data).then((r) => r.data),
  getDefaults: () =>
    api
      .get<Partial<PlatformSettings>>("/settings/defaults")
      .then((r) => r.data),
  getOUIStatus: () =>
    api.get<OUIStatus>("/settings/oui/status").then((r) => r.data),
  refreshOUI: () =>
    api
      .post<{ status: string; task_id: string | null }>("/settings/oui/refresh")
      .then((r) => r.data),
  getOUIRefreshStatus: (taskId: string) =>
    api
      .get<OUITaskStatus>(`/settings/oui/refresh/${taskId}`)
      .then((r) => r.data),
  listAuditTargets: () =>
    api
      .get<AuditForwardTarget[]>("/settings/audit-forward-targets")
      .then((r) => r.data),
  createAuditTarget: (body: AuditForwardTargetWrite) =>
    api
      .post<AuditForwardTarget>("/settings/audit-forward-targets", body)
      .then((r) => r.data),
  updateAuditTarget: (id: string, body: AuditForwardTargetWrite) =>
    api
      .put<AuditForwardTarget>(`/settings/audit-forward-targets/${id}`, body)
      .then((r) => r.data),
  deleteAuditTarget: (id: string) =>
    api.delete(`/settings/audit-forward-targets/${id}`),
  testAuditTarget: (id: string) =>
    api
      .post<{
        status: string;
        target: string;
      }>(`/settings/audit-forward-targets/${id}/test`)
      .then((r) => r.data),
};

// ── Auth Providers ─────────────────────────────────────────────────────────────

export type AuthProviderType = "ldap" | "oidc" | "saml" | "radius" | "tacacs";

export interface AuthProvider {
  id: string;
  name: string;
  type: AuthProviderType;
  is_enabled: boolean;
  priority: number;
  config: Record<string, unknown>;
  has_secrets: boolean;
  auto_create_users: boolean;
  auto_update_users: boolean;
  mapping_count: number;
  created_at: string;
  modified_at: string;
}

export interface AuthProviderCreate {
  name: string;
  type: AuthProviderType;
  is_enabled?: boolean;
  priority?: number;
  config?: Record<string, unknown>;
  secrets?: Record<string, unknown> | null;
  auto_create_users?: boolean;
  auto_update_users?: boolean;
}

export interface AuthProviderUpdate {
  name?: string;
  is_enabled?: boolean;
  priority?: number;
  config?: Record<string, unknown>;
  /** undefined = leave stored secrets untouched. {} = clear. */
  secrets?: Record<string, unknown> | null;
  auto_create_users?: boolean;
  auto_update_users?: boolean;
}

export interface AuthGroupMapping {
  id: string;
  provider_id: string;
  external_group: string;
  internal_group_id: string;
  internal_group_name: string;
  priority: number;
  created_at: string;
  modified_at: string;
}

export interface AuthGroupMappingCreate {
  external_group: string;
  internal_group_id: string;
  priority?: number;
}

export interface AuthGroupMappingUpdate {
  external_group?: string;
  internal_group_id?: string;
  priority?: number;
}

export interface AuthProviderTestResult {
  ok: boolean;
  message: string;
  details: Record<string, unknown>;
}

export interface InternalGroup {
  id: string;
  name: string;
  description: string;
  auth_source: string;
  external_dn?: string | null;
  role_ids?: string[];
  user_ids?: string[];
}

export interface InternalGroupCreate {
  name: string;
  description?: string;
  auth_source?: string;
  external_dn?: string | null;
  role_ids?: string[];
  user_ids?: string[];
}

export interface InternalGroupUpdate {
  name?: string;
  description?: string;
  external_dn?: string | null;
  role_ids?: string[];
  user_ids?: string[];
}

export const groupsApi = {
  list: () => api.get<InternalGroup[]>("/groups").then((r) => r.data),
  get: (id: string) =>
    api.get<InternalGroup>(`/groups/${id}`).then((r) => r.data),
  create: (body: InternalGroupCreate) =>
    api.post<InternalGroup>("/groups", body).then((r) => r.data),
  update: (id: string, body: InternalGroupUpdate) =>
    api.put<InternalGroup>(`/groups/${id}`, body).then((r) => r.data),
  delete: (id: string) => api.delete(`/groups/${id}`),
};

// ── Roles ─────────────────────────────────────────────────────────────────────

export interface PermissionEntry {
  action: string;
  resource_type: string;
  resource_id?: string | null;
}

export interface AppRole {
  id: string;
  name: string;
  description: string;
  is_builtin: boolean;
  permissions: PermissionEntry[];
}

export interface RoleCreate {
  name: string;
  description?: string;
  permissions?: PermissionEntry[];
}

export interface RoleUpdate {
  name?: string;
  description?: string;
  permissions?: PermissionEntry[];
}

export const rolesApi = {
  list: () => api.get<AppRole[]>("/roles").then((r) => r.data),
  get: (id: string) => api.get<AppRole>(`/roles/${id}`).then((r) => r.data),
  create: (body: RoleCreate) =>
    api.post<AppRole>("/roles", body).then((r) => r.data),
  update: (id: string, body: RoleUpdate) =>
    api.put<AppRole>(`/roles/${id}`, body).then((r) => r.data),
  delete: (id: string) => api.delete(`/roles/${id}`),
  clone: (id: string, name: string) =>
    api.post<AppRole>(`/roles/${id}/clone`, { name }).then((r) => r.data),
};

export const authProvidersApi = {
  list: () => api.get<AuthProvider[]>("/auth-providers").then((r) => r.data),
  test: (id: string, body: { username?: string; password?: string }) =>
    api
      .post<AuthProviderTestResult>(`/auth-providers/${id}/test`, body)
      .then((r) => r.data),
  // Dry-run test against an unsaved provider config. Nothing is persisted —
  // lets admins iterate on config + secrets before committing a row.
  testUnsaved: (body: {
    type: AuthProviderType;
    config: Record<string, unknown>;
    secrets: Record<string, unknown>;
    username?: string;
    password?: string;
  }) =>
    api
      .post<AuthProviderTestResult>("/auth-providers/test", body)
      .then((r) => r.data),
  get: (id: string) =>
    api.get<AuthProvider>(`/auth-providers/${id}`).then((r) => r.data),
  create: (body: AuthProviderCreate) =>
    api.post<AuthProvider>("/auth-providers", body).then((r) => r.data),
  update: (id: string, body: AuthProviderUpdate) =>
    api.put<AuthProvider>(`/auth-providers/${id}`, body).then((r) => r.data),
  delete: (id: string) => api.delete(`/auth-providers/${id}`),
  revealSecrets: (id: string) =>
    api
      .get<Record<string, unknown>>(`/auth-providers/${id}/secrets`)
      .then((r) => r.data),
  listMappings: (id: string) =>
    api
      .get<AuthGroupMapping[]>(`/auth-providers/${id}/mappings`)
      .then((r) => r.data),
  createMapping: (id: string, body: AuthGroupMappingCreate) =>
    api
      .post<AuthGroupMapping>(`/auth-providers/${id}/mappings`, body)
      .then((r) => r.data),
  updateMapping: (
    id: string,
    mappingId: string,
    body: AuthGroupMappingUpdate,
  ) =>
    api
      .put<AuthGroupMapping>(
        `/auth-providers/${id}/mappings/${mappingId}`,
        body,
      )
      .then((r) => r.data),
  deleteMapping: (id: string, mappingId: string) =>
    api.delete(`/auth-providers/${id}/mappings/${mappingId}`),
};

// ── AI Providers (issue #90 — Operator Copilot) ────────────────────────────────

export type AIProviderKind =
  | "openai_compat"
  | "anthropic"
  | "google"
  | "azure_openai";

export const AI_PROVIDER_KIND_LABELS: Record<AIProviderKind, string> = {
  openai_compat: "OpenAI-compatible (OpenAI / Ollama / vLLM / OpenWebUI / …)",
  anthropic: "Anthropic Claude",
  google: "Google Gemini",
  azure_openai: "Azure OpenAI",
};

// Short labels for table cells (the verbose ``AI_PROVIDER_KIND_LABELS``
// fits in a form picker but pushes the table too wide on narrow viewports).
export const AI_PROVIDER_KIND_SHORT: Record<AIProviderKind, string> = {
  openai_compat: "OpenAI-compat",
  anthropic: "Claude",
  google: "Gemini",
  azure_openai: "Azure OpenAI",
};

// Wave 1 ships only openai_compat. Other kinds appear in the dropdown
// but the backend rejects them until the matching driver lands.
export const AI_PROVIDER_KIND_AVAILABLE: AIProviderKind[] = ["openai_compat"];

export interface AIProvider {
  id: string;
  name: string;
  kind: AIProviderKind;
  base_url: string;
  has_api_key: boolean;
  default_model: string;
  is_enabled: boolean;
  priority: number;
  options: Record<string, unknown>;
  created_at: string;
  modified_at: string;
}

export interface AIProviderCreate {
  name: string;
  kind: AIProviderKind;
  base_url?: string;
  api_key?: string | null;
  default_model?: string;
  is_enabled?: boolean;
  priority?: number;
  options?: Record<string, unknown>;
}

export interface AIProviderUpdate {
  name?: string;
  base_url?: string;
  api_key?: string | null;
  default_model?: string;
  is_enabled?: boolean;
  priority?: number;
  options?: Record<string, unknown>;
}

export interface AITestConnectionResult {
  ok: boolean;
  detail: string;
  latency_ms: number | null;
  sample_models: string[];
}

export interface AIModelInfo {
  id: string;
  owned_by: string;
  context_window: number | null;
}

export const aiApi = {
  listProviders: () =>
    api.get<AIProvider[]>("/ai/providers").then((r) => r.data),
  getProvider: (id: string) =>
    api.get<AIProvider>(`/ai/providers/${id}`).then((r) => r.data),
  createProvider: (body: AIProviderCreate) =>
    api.post<AIProvider>("/ai/providers", body).then((r) => r.data),
  updateProvider: (id: string, body: AIProviderUpdate) =>
    api.put<AIProvider>(`/ai/providers/${id}`, body).then((r) => r.data),
  deleteProvider: (id: string) =>
    api.delete<void>(`/ai/providers/${id}`).then((r) => r.data),
  testProvider: (id: string) =>
    api
      .post<AITestConnectionResult>(`/ai/providers/${id}/test`, {})
      .then((r) => r.data),
  testUnsaved: (body: {
    kind: AIProviderKind;
    base_url?: string;
    api_key?: string | null;
    default_model?: string;
    options?: Record<string, unknown>;
  }) =>
    api
      .post<AITestConnectionResult>("/ai/providers/test", body)
      .then((r) => r.data),
  listModels: (id: string) =>
    api
      .get<{ models: AIModelInfo[] }>(`/ai/providers/${id}/models`)
      .then((r) => r.data.models),
};

// ── Custom Fields ──────────────────────────────────────────────────────────────

export interface CustomField {
  id: string;
  resource_type: string;
  name: string;
  label: string;
  field_type: string;
  options: string[] | null;
  is_required: boolean;
  is_searchable: boolean;
  default_value: string | null;
  display_order: number;
  description: string;
}

export const customFieldsApi = {
  list: (resource_type?: string) =>
    api
      .get<
        CustomField[]
      >("/custom-fields", { params: resource_type ? { resource_type } : undefined })
      .then((r) => r.data),
  create: (data: Omit<CustomField, "id">) =>
    api.post<CustomField>("/custom-fields", data).then((r) => r.data),
  update: (
    id: string,
    data: Partial<
      Omit<CustomField, "id" | "resource_type" | "name" | "field_type">
    >,
  ) => api.put<CustomField>(`/custom-fields/${id}`, data).then((r) => r.data),
  delete: (id: string) => api.delete(`/custom-fields/${id}`),
};

// ── DNS ────────────────────────────────────────────────────────────────────

export interface DNSServerGroup {
  id: string;
  name: string;
  description: string;
  group_type: string;
  default_view: string | null;
  is_recursive: boolean;
  // RFC 9432 BIND9 catalog zones — distribute zones via one catalog
  // instead of per-server config push. Producer is the group's primary
  // bind9 server; consumers auto-pull members.
  catalog_zones_enabled: boolean;
  catalog_zone_name: string;
  // Issue #25 — flag this group as exposed to the public internet.
  // The IPAM safety guard returns ``requires_confirmation`` when an
  // operator binds a private IP into a zone in this group, forcing a
  // typed-CIDR confirm.
  is_public_facing?: boolean;
  created_at: string;
  modified_at: string;
}

export interface WindowsDNSCredentials {
  username: string;
  password: string;
  winrm_port?: number;
  transport?: "ntlm" | "kerberos" | "basic" | "credssp";
  use_tls?: boolean;
  verify_tls?: boolean;
}

export interface DNSZoneSyncItem {
  zone: string;
  imported: number;
  pushed: number;
  server_records: number;
  push_errors: string[];
  error: string | null;
}

export interface DNSServerSyncResult {
  zones_attempted: number;
  zones_succeeded: number;
  zones_failed: number;
  total_imported: number;
  total_pushed: number;
  total_push_errors: number;
  /** Zones listed on the server via WinRM — windows_dns Path B only.
   * Empty for BIND9 and windows_dns without credentials. */
  zones_on_server: string[];
  /** Subset of zones_on_server that aren't tracked in SpatiumDDI yet. */
  new_zones_on_server: string[];
  /** Zones that were auto-imported into SpatiumDDI during this sync
   * (when the caller passed import_new_zones=true, the default). */
  zones_imported: string[];
  /** Zones on the server that were skipped because they look like a
   * Windows system zone (TrustAnchors, single-label names, …). */
  zones_skipped_system: string[];
  /** Zones that existed in SpatiumDDI but not on the Windows server
   * — pushed over WinRM during this sync. */
  zones_pushed_to_server: string[];
  /** Per-zone error strings when the DB→server zone push failed. */
  zones_push_to_server_errors: string[];
  items: DNSZoneSyncItem[];
}

export interface DNSPerServerSyncItem {
  server_id: string;
  server_name: string;
  driver: string;
  error: string | null;
  result: DNSServerSyncResult | null;
}

export interface DNSGroupSyncResult {
  servers_attempted: number;
  servers_succeeded: number;
  total_imported: number;
  total_pushed: number;
  total_push_errors: number;
  total_zones_imported: number;
  total_zones_pushed_to_server: number;
  items: DNSPerServerSyncItem[];
}

export interface DNSPerServerZoneStateEntry {
  zone_id: string;
  zone_name: string;
  zone_type: string;
  target_serial: number;
  current_serial: number | null;
  reported_at: string | null;
  in_sync: boolean;
}

export interface DNSPerServerZoneStateResponse {
  server_id: string;
  server_name: string;
  zones: DNSPerServerZoneStateEntry[];
  summary: {
    total: number;
    in_sync: number;
    drift: number;
    not_reported: number;
  };
}

export interface DNSPendingOpEntry {
  op_id: string;
  zone_name: string;
  op: string;
  state: string;
  record: Record<string, unknown>;
  target_serial: number | null;
  attempts: number;
  last_error: string | null;
  created_at: string;
  applied_at: string | null;
}

export interface DNSPendingOpsResponse {
  server_id: string;
  counts: Record<string, number>;
  items: DNSPendingOpEntry[];
}

export interface DNSServerEventEntry {
  id: string;
  timestamp: string;
  user_display_name: string;
  action: string;
  resource_type: string;
  resource_display: string;
  result: string;
}

export interface DNSServerEventsResponse {
  server_id: string;
  items: DNSServerEventEntry[];
}

// Latest agent-pushed snapshot of the on-disk rendered config tree.
// One entry per text file under the agent's rendered/ directory:
// "named.conf" + every "zones/<name>.db". `rendered_at` is null when
// the agent hasn't pushed yet (fresh server, never reloaded).
export interface DNSRenderedConfigFile {
  path: string;
  content: string;
}
export interface DNSRenderedConfigResponse {
  server_id: string;
  rendered_at: string | null;
  files: DNSRenderedConfigFile[];
}

// Latest agent-pushed `rndc status` output. Confirms the daemon is
// running + which zones are loaded without needing SSH access.
export interface DNSRndcStatusResponse {
  server_id: string;
  observed_at: string | null;
  text: string | null;
}

export interface DNSPoolMember {
  id: string;
  pool_id: string;
  address: string;
  weight: number;
  enabled: boolean;
  last_check_state: "unknown" | "healthy" | "unhealthy";
  last_check_at: string | null;
  last_check_error: string | null;
  consecutive_failures: number;
  consecutive_successes: number;
  created_at: string;
  modified_at: string;
}

export interface DNSPoolMemberWrite {
  address: string;
  weight?: number;
  enabled?: boolean;
}

export interface DNSPool {
  id: string;
  group_id: string;
  zone_id: string;
  name: string;
  description: string;
  record_name: string;
  record_type: "A" | "AAAA";
  ttl: number;
  enabled: boolean;
  hc_type: "none" | "tcp" | "http" | "https" | "icmp";
  hc_target_port: number | null;
  hc_path: string;
  hc_method: string;
  hc_verify_tls: boolean;
  hc_expected_status_codes: number[];
  hc_interval_seconds: number;
  hc_timeout_seconds: number;
  hc_unhealthy_threshold: number;
  hc_healthy_threshold: number;
  next_check_at: string | null;
  last_checked_at: string | null;
  members: DNSPoolMember[];
  created_at: string;
  modified_at: string;
}

export interface DNSPoolListEntry {
  id: string;
  group_id: string;
  group_name: string;
  zone_id: string;
  zone_name: string;
  name: string;
  description: string;
  record_name: string;
  record_type: "A" | "AAAA";
  ttl: number;
  enabled: boolean;
  hc_type: "none" | "tcp" | "http" | "https" | "icmp";
  hc_target_port: number | null;
  hc_interval_seconds: number;
  next_check_at: string | null;
  last_checked_at: string | null;
  member_count: number;
  healthy_count: number;
  enabled_count: number;
  live_count: number;
  created_at: string;
  modified_at: string;
}

export interface DNSPoolWrite {
  name: string;
  description?: string;
  record_name: string;
  record_type?: "A" | "AAAA";
  ttl?: number;
  enabled?: boolean;
  hc_type?: "none" | "tcp" | "http" | "https" | "icmp";
  hc_target_port?: number | null;
  hc_path?: string;
  hc_method?: string;
  hc_verify_tls?: boolean;
  hc_expected_status_codes?: number[];
  hc_interval_seconds?: number;
  hc_timeout_seconds?: number;
  hc_unhealthy_threshold?: number;
  hc_healthy_threshold?: number;
  members?: DNSPoolMemberWrite[];
}

export interface DNSServer {
  id: string;
  group_id: string;
  name: string;
  driver: string;
  host: string;
  port: number;
  api_port: number | null;
  roles: string[];
  status: string;
  /** User-controlled pause. When false, health sweeps, the bi-directional
   * sync job, and record-op writes all skip this server. Separate from
   * ``status`` which is automatically set by the health probe. */
  is_enabled: boolean;
  last_sync_at: string | null;
  last_health_check_at: string | null;
  notes: string;
  /** True when stored Fernet-encrypted WinRM credentials exist. Used by
   * the UI to show "Credentials set" / "Clear" and gate the Path B
   * affordances without exposing the password. */
  has_credentials: boolean;
  /** True when the driver runs from the control plane (no agent). Used
   * by the UI to hide approval / agent-registration affordances. */
  is_agentless: boolean;
  /** Agent-state fields surfaced for the Server Detail modal. */
  agent_id: string | null;
  last_seen_at: string | null;
  last_config_etag: string | null;
  pending_approval: boolean;
  is_primary: boolean;
  created_at: string;
  modified_at: string;
}

export interface DNSServerOptions {
  id: string;
  group_id: string;
  forwarders: string[];
  forward_policy: string;
  recursion_enabled: boolean;
  allow_recursion: string[];
  dnssec_validation: string;
  gss_tsig_enabled: boolean;
  gss_tsig_keytab_path: string | null;
  gss_tsig_realm: string | null;
  gss_tsig_principal: string | null;
  notify_enabled: string;
  also_notify: string[];
  allow_notify: string[];
  allow_query: string[];
  allow_query_cache: string[];
  allow_transfer: string[];
  blackhole: string[];
  query_log_enabled: boolean;
  query_log_channel: string;
  query_log_file: string;
  query_log_severity: string;
  query_log_print_category: boolean;
  query_log_print_severity: boolean;
  query_log_print_time: boolean;
  trust_anchors: DNSTrustAnchor[];
  modified_at: string;
}

export interface DNSTrustAnchor {
  id: string;
  zone_name: string;
  algorithm: number;
  key_tag: number;
  public_key: string;
  is_initial_key: boolean;
  added_at: string;
}

export interface DNSAclEntry {
  id: string;
  value: string;
  negate: boolean;
  order: number;
}

export interface DNSAcl {
  id: string;
  group_id: string | null;
  name: string;
  description: string;
  entries: DNSAclEntry[];
  created_at: string;
  modified_at: string;
}

export interface DNSView {
  id: string;
  group_id: string;
  name: string;
  description: string;
  match_clients: string[];
  match_destinations: string[];
  recursion: boolean;
  order: number;
  allow_query: string[] | null;
  allow_query_cache: string[] | null;
  created_at: string;
  modified_at: string;
}

export interface DNSZone {
  id: string;
  group_id: string;
  view_id: string | null;
  name: string;
  zone_type: string;
  kind: string;
  ttl: number;
  refresh: number;
  retry: number;
  expire: number;
  minimum: number;
  primary_ns: string;
  admin_email: string;
  is_auto_generated: boolean;
  linked_subnet_id: string | null;
  domain_id?: string | null;
  dnssec_enabled: boolean;
  color: string | null;
  last_serial: number;
  last_pushed_at: string | null;
  allow_query: string[] | null;
  allow_transfer: string[] | null;
  also_notify: string[] | null;
  notify_enabled: string | null;
  // Conditional-forwarder config. Meaningful only when zone_type==="forward".
  forwarders: string[];
  forward_only: boolean;
  // Non-null when the zone was synthesised by the Tailscale Phase 2
  // reconciler. The UI shows a read-only badge and disables edit /
  // delete controls on the zone + its records.
  tailscale_tenant_id: string | null;
  created_at: string;
  modified_at: string;
}

// ── Zone server-state (per-server serial reporting) ──────────────────────────

export interface ZoneServerStateEntry {
  server_id: string;
  server_name: string;
  server_status: string;
  // `null` means the agent hasn't reported back yet (freshly-registered
  // server, or the zone was only just created).
  current_serial: number | null;
  reported_at: string | null;
}

export interface ZoneServerState {
  zone_id: string;
  zone_name: string;
  target_serial: number;
  servers: ZoneServerStateEntry[];
  // `false` while any server hasn't reported or is on a different serial.
  in_sync: boolean;
}

// Pending records the delegation wizard would land in the parent zone.
// `existing_*` lists are already-present rows the wizard would skip on apply.
export interface DelegationRecord {
  name: string;
  record_type: string;
  value: string;
  ttl: number | null;
}

export interface DelegationPreview {
  has_parent: true;
  parent_zone_id: string;
  parent_zone_name: string;
  child_zone_id: string;
  child_zone_name: string;
  child_label: string;
  ns_records_to_create: DelegationRecord[];
  glue_records_to_create: DelegationRecord[];
  existing_ns_records: DelegationRecord[];
  existing_glue_records: DelegationRecord[];
  warnings: string[];
  child_apex_ns_count: number;
}

export type DelegationPreviewResponse =
  | { has_parent: false }
  | DelegationPreview;

// Zone-template wizard catalog. Templates carry a parameter manifest
// the UI renders into a form; submission returns a fully-built zone.
export interface ZoneTemplateParameter {
  key: string;
  label: string;
  type: string;
  required: boolean;
  default: string | null;
  placeholder: string | null;
  hint: string | null;
}

export interface ZoneTemplate {
  id: string;
  name: string;
  category: string;
  description: string;
  parameters: ZoneTemplateParameter[];
  record_count: number;
}

export interface ZoneTemplateCatalog {
  templates: ZoneTemplate[];
}

export interface FromTemplateRequest {
  template_id: string;
  zone_name: string;
  params: Record<string, string>;
  view_id?: string | null;
  zone_type?: string;
  kind?: string;
}

// Operator-managed named TSIG keys for RFC 2136 / AXFR auth. Distinct
// from the legacy single key auto-generated on DNSServerGroup which is
// reserved for the agent's own loopback updates.
export interface DNSTSIGKey {
  id: string;
  group_id: string;
  name: string;
  algorithm: string;
  purpose: string | null;
  notes: string;
  last_rotated_at: string | null;
  created_at: string;
  modified_at: string;
  // Plaintext secret. Populated on the create / rotate responses *only*.
  // Read endpoints (list / get) leave this null.
  secret: string | null;
}

export interface TSIGKeyCreate {
  name: string;
  algorithm?: string;
  secret?: string | null;
  purpose?: string | null;
  notes?: string;
}

export interface TSIGKeyUpdate {
  name?: string;
  algorithm?: string;
  purpose?: string | null;
  notes?: string;
}

export interface DNSRecord {
  id: string;
  zone_id: string;
  view_id: string | null;
  name: string;
  fqdn: string;
  record_type: string;
  value: string;
  ttl: number | null;
  priority: number | null;
  weight: number | null;
  port: number | null;
  auto_generated: boolean;
  // Non-null when the record was synthesised by Tailscale Phase 2.
  tailscale_tenant_id: string | null;
  // Non-null when the record is rendered by the DNS pool health-check
  // pipeline. Operator edits / deletes are blocked while non-null.
  pool_member_id: string | null;
  created_at: string;
  modified_at: string;
}

export interface DNSGroupRecord {
  id: string;
  zone_id: string;
  zone_name: string;
  view_id: string | null;
  view_name: string | null;
  name: string;
  fqdn: string;
  // Synthesised by Tailscale Phase 2 → write paths blocked.
  tailscale_tenant_id?: string | null;
  // Managed by a DNS pool → write paths blocked.
  pool_member_id?: string | null;
  record_type: string;
  value: string;
  ttl: number | null;
  priority: number | null;
  weight: number | null;
  port: number | null;
  auto_generated: boolean;
  created_at: string;
  modified_at: string;
}

export interface ResolverInfo {
  name: string;
  address: string;
}

export interface PropagationResolverResult {
  resolver: string;
  name: string | null;
  status: "ok" | "nxdomain" | "timeout" | "error";
  rtt_ms: number | null;
  answers: string[];
  error: string | null;
}

export interface PropagationCheckResult {
  name: string;
  record_type: string;
  queried_at_ms: number;
  results: PropagationResolverResult[];
}

export const dnsApi = {
  // Server groups
  listGroups: () =>
    api.get<DNSServerGroup[]>("/dns/groups").then((r) => r.data),
  // Multi-resolver propagation check
  checkPropagation: (body: {
    name: string;
    record_type?: string;
    resolvers?: string[];
    timeout_seconds?: number;
  }) =>
    api
      .post<PropagationCheckResult>(`/dns/tools/propagation-check`, body)
      .then((r) => r.data),
  defaultResolvers: () =>
    api.get<ResolverInfo[]>(`/dns/tools/default-resolvers`).then((r) => r.data),
  createGroup: (data: Partial<DNSServerGroup>) =>
    api.post<DNSServerGroup>("/dns/groups", data).then((r) => r.data),
  updateGroup: (id: string, data: Partial<DNSServerGroup>) =>
    api.put<DNSServerGroup>(`/dns/groups/${id}`, data).then((r) => r.data),
  deleteGroup: (id: string) => api.delete(`/dns/groups/${id}`),

  // Servers
  listServers: (groupId: string) =>
    api.get<DNSServer[]>(`/dns/groups/${groupId}/servers`).then((r) => r.data),
  createServer: (
    groupId: string,
    data: Partial<DNSServer> & {
      api_key?: string;
      windows_credentials?: WindowsDNSCredentials | Record<string, never>;
    },
  ) =>
    api
      .post<DNSServer>(`/dns/groups/${groupId}/servers`, data)
      .then((r) => r.data),
  updateServer: (
    groupId: string,
    serverId: string,
    data: Partial<DNSServer> & {
      api_key?: string;
      windows_credentials?:
        | Partial<WindowsDNSCredentials>
        | Record<string, never>;
    },
  ) =>
    api
      .put<DNSServer>(`/dns/groups/${groupId}/servers/${serverId}`, data)
      .then((r) => r.data),
  deleteServer: (groupId: string, serverId: string) =>
    api.delete(`/dns/groups/${groupId}/servers/${serverId}`),

  testWindowsCredentials: (body: {
    host: string;
    credentials?: Partial<WindowsDNSCredentials>;
    server_id?: string;
  }) =>
    api
      .post<{
        ok: boolean;
        message: string;
      }>("/dns/test-windows-credentials", body)
      .then((r) => r.data),

  pullZonesFromServer: (groupId: string, serverId: string) =>
    api
      .post<{
        zones: Array<Record<string, unknown>>;
      }>(`/dns/groups/${groupId}/servers/${serverId}/pull-zones-from-server`)
      .then((r) => r.data),

  syncFromServer: (groupId: string, serverId: string) =>
    api
      .post<DNSServerSyncResult>(
        `/dns/groups/${groupId}/servers/${serverId}/sync-from-server`,
      )
      .then((r) => r.data),

  // Per-server detail (powers the Server Detail modal)
  getServerZoneState: (serverId: string) =>
    api
      .get<DNSPerServerZoneStateResponse>(`/dns/servers/${serverId}/zone-state`)
      .then((r) => r.data),
  getServerPendingOps: (serverId: string, limit = 50) =>
    api
      .get<DNSPendingOpsResponse>(
        `/dns/servers/${serverId}/pending-ops?limit=${limit}`,
      )
      .then((r) => r.data),
  getServerRecentEvents: (serverId: string, limit = 50) =>
    api
      .get<DNSServerEventsResponse>(
        `/dns/servers/${serverId}/recent-events?limit=${limit}`,
      )
      .then((r) => r.data),
  getServerRenderedConfig: (serverId: string) =>
    api
      .get<DNSRenderedConfigResponse>(
        `/dns/servers/${serverId}/rendered-config`,
      )
      .then((r) => r.data),
  getServerRndcStatus: (serverId: string) =>
    api
      .get<DNSRndcStatusResponse>(`/dns/servers/${serverId}/rndc-status`)
      .then((r) => r.data),

  // DNS pools (GSLB-lite)
  listAllPools: (groupId?: string) =>
    api
      .get<DNSPoolListEntry[]>(`/dns/pools`, {
        params: groupId ? { group_id: groupId } : undefined,
      })
      .then((r) => r.data),
  listPools: (groupId: string, zoneId: string) =>
    api
      .get<DNSPool[]>(`/dns/groups/${groupId}/zones/${zoneId}/pools`)
      .then((r) => r.data),
  createPool: (groupId: string, zoneId: string, data: DNSPoolWrite) =>
    api
      .post<DNSPool>(`/dns/groups/${groupId}/zones/${zoneId}/pools`, data)
      .then((r) => r.data),
  getPool: (poolId: string) =>
    api.get<DNSPool>(`/dns/pools/${poolId}`).then((r) => r.data),
  updatePool: (poolId: string, data: Partial<DNSPoolWrite>) =>
    api.put<DNSPool>(`/dns/pools/${poolId}`, data).then((r) => r.data),
  deletePool: (poolId: string) => api.delete(`/dns/pools/${poolId}`),
  checkPoolNow: (poolId: string) =>
    api.post<DNSPool>(`/dns/pools/${poolId}/check-now`).then((r) => r.data),
  addPoolMember: (poolId: string, data: DNSPoolMemberWrite) =>
    api
      .post<DNSPoolMember>(`/dns/pools/${poolId}/members`, data)
      .then((r) => r.data),
  updatePoolMember: (
    memberId: string,
    data: { address?: string; weight?: number; enabled?: boolean },
  ) =>
    api
      .put<DNSPoolMember>(`/dns/pool-members/${memberId}`, data)
      .then((r) => r.data),
  deletePoolMember: (memberId: string) =>
    api.delete(`/dns/pool-members/${memberId}`),

  syncGroupWithServers: (groupId: string) =>
    api
      .post<DNSGroupSyncResult>(`/dns/groups/${groupId}/sync-with-servers`)
      .then((r) => r.data),

  // Server options
  getOptions: (groupId: string) =>
    api
      .get<DNSServerOptions>(`/dns/groups/${groupId}/options`)
      .then((r) => r.data),
  updateOptions: (groupId: string, data: Partial<DNSServerOptions>) =>
    api
      .put<DNSServerOptions>(`/dns/groups/${groupId}/options`, data)
      .then((r) => r.data),
  addTrustAnchor: (
    groupId: string,
    data: Omit<DNSTrustAnchor, "id" | "added_at">,
  ) =>
    api
      .post<DNSTrustAnchor>(
        `/dns/groups/${groupId}/options/trust-anchors`,
        data,
      )
      .then((r) => r.data),
  deleteTrustAnchor: (groupId: string, anchorId: string) =>
    api.delete(`/dns/groups/${groupId}/options/trust-anchors/${anchorId}`),

  // ACLs
  listAcls: (groupId: string) =>
    api.get<DNSAcl[]>(`/dns/groups/${groupId}/acls`).then((r) => r.data),
  createAcl: (groupId: string, data: Partial<DNSAcl>) =>
    api.post<DNSAcl>(`/dns/groups/${groupId}/acls`, data).then((r) => r.data),
  updateAcl: (groupId: string, aclId: string, data: Partial<DNSAcl>) =>
    api
      .put<DNSAcl>(`/dns/groups/${groupId}/acls/${aclId}`, data)
      .then((r) => r.data),
  deleteAcl: (groupId: string, aclId: string) =>
    api.delete(`/dns/groups/${groupId}/acls/${aclId}`),

  // Views
  listViews: (groupId: string) =>
    api.get<DNSView[]>(`/dns/groups/${groupId}/views`).then((r) => r.data),
  createView: (groupId: string, data: Partial<DNSView>) =>
    api.post<DNSView>(`/dns/groups/${groupId}/views`, data).then((r) => r.data),
  updateView: (groupId: string, viewId: string, data: Partial<DNSView>) =>
    api
      .put<DNSView>(`/dns/groups/${groupId}/views/${viewId}`, data)
      .then((r) => r.data),
  deleteView: (groupId: string, viewId: string) =>
    api.delete(`/dns/groups/${groupId}/views/${viewId}`),

  // Zones
  listZones: (groupId: string) =>
    api.get<DNSZone[]>(`/dns/groups/${groupId}/zones`).then((r) => r.data),
  createZone: (groupId: string, data: Partial<DNSZone>) =>
    api.post<DNSZone>(`/dns/groups/${groupId}/zones`, data).then((r) => r.data),
  updateZone: (groupId: string, zoneId: string, data: Partial<DNSZone>) =>
    api
      .put<DNSZone>(`/dns/groups/${groupId}/zones/${zoneId}`, data)
      .then((r) => r.data),
  deleteZone: (groupId: string, zoneId: string) =>
    api.delete(`/dns/groups/${groupId}/zones/${zoneId}`),
  getZoneServerState: (groupId: string, zoneId: string) =>
    api
      .get<ZoneServerState>(
        `/dns/groups/${groupId}/zones/${zoneId}/server-state`,
      )
      .then((r) => r.data),

  // Delegation wizard
  getDelegationPreview: (groupId: string, zoneId: string) =>
    api
      .get<DelegationPreviewResponse>(
        `/dns/groups/${groupId}/zones/${zoneId}/delegation-preview`,
      )
      .then((r) => r.data),
  applyDelegation: (groupId: string, zoneId: string) =>
    api
      .post<
        DNSRecord[]
      >(`/dns/groups/${groupId}/zones/${zoneId}/delegate-from-parent`)
      .then((r) => r.data),

  // Zone templates (starter zones with parameterised records)
  listZoneTemplates: () =>
    api.get<ZoneTemplateCatalog>(`/dns/zone-templates`).then((r) => r.data),
  createZoneFromTemplate: (groupId: string, body: FromTemplateRequest) =>
    api
      .post<DNSZone>(`/dns/groups/${groupId}/zones/from-template`, body)
      .then((r) => r.data),

  // TSIG keys (operator-managed named keys for RFC 2136 / AXFR auth)
  listTSIGKeys: (groupId: string) =>
    api
      .get<DNSTSIGKey[]>(`/dns/groups/${groupId}/tsig-keys`)
      .then((r) => r.data),
  createTSIGKey: (groupId: string, body: TSIGKeyCreate) =>
    api
      .post<DNSTSIGKey>(`/dns/groups/${groupId}/tsig-keys`, body)
      .then((r) => r.data),
  updateTSIGKey: (groupId: string, keyId: string, body: TSIGKeyUpdate) =>
    api
      .put<DNSTSIGKey>(`/dns/groups/${groupId}/tsig-keys/${keyId}`, body)
      .then((r) => r.data),
  rotateTSIGKey: (groupId: string, keyId: string) =>
    api
      .post<DNSTSIGKey>(`/dns/groups/${groupId}/tsig-keys/${keyId}/rotate`)
      .then((r) => r.data),
  deleteTSIGKey: (groupId: string, keyId: string) =>
    api.delete(`/dns/groups/${groupId}/tsig-keys/${keyId}`),
  generateTSIGSecret: (groupId: string, algorithm: string) =>
    api
      .get<{
        algorithm: string;
        secret: string;
      }>(`/dns/groups/${groupId}/tsig-keys/generate-secret`, {
        params: { algorithm },
      })
      .then((r) => r.data),

  // Records
  listGroupRecords: (groupId: string) =>
    api
      .get<DNSGroupRecord[]>(`/dns/groups/${groupId}/records`)
      .then((r) => r.data),
  listRecords: (groupId: string, zoneId: string) =>
    api
      .get<DNSRecord[]>(`/dns/groups/${groupId}/zones/${zoneId}/records`)
      .then((r) => r.data),
  createRecord: (groupId: string, zoneId: string, data: Partial<DNSRecord>) =>
    api
      .post<DNSRecord>(`/dns/groups/${groupId}/zones/${zoneId}/records`, data)
      .then((r) => r.data),
  updateRecord: (
    groupId: string,
    zoneId: string,
    recordId: string,
    data: Partial<DNSRecord>,
  ) =>
    api
      .put<DNSRecord>(
        `/dns/groups/${groupId}/zones/${zoneId}/records/${recordId}`,
        data,
      )
      .then((r) => r.data),
  deleteRecord: (groupId: string, zoneId: string, recordId: string) =>
    api.delete(`/dns/groups/${groupId}/zones/${zoneId}/records/${recordId}`),

  bulkDeleteRecords: (groupId: string, zoneId: string, recordIds: string[]) =>
    api
      .post<{
        deleted: number;
        skipped: { record_id: string; reason: string }[];
      }>(`/dns/groups/${groupId}/zones/${zoneId}/records/bulk-delete`, {
        record_ids: recordIds,
      })
      .then((r) => r.data),

  // Bulk zone file import / export
  importZonePreview: (
    groupId: string,
    zoneId: string,
    data: { zone_file: string; zone_name?: string; view_id?: string | null },
  ) =>
    api
      .post<DNSImportPreview>(
        `/dns/groups/${groupId}/zones/${zoneId}/import/preview`,
        data,
      )
      .then((r) => r.data),
  importZoneCommit: (
    groupId: string,
    zoneId: string,
    data: {
      zone_file: string;
      zone_name?: string;
      view_id?: string | null;
      conflict_strategy: "merge" | "replace" | "append";
    },
  ) =>
    api
      .post<DNSImportCommit>(
        `/dns/groups/${groupId}/zones/${zoneId}/import/commit`,
        data,
      )
      .then((r) => r.data),
  exportZone: (groupId: string, zoneId: string) =>
    api
      .get<string>(`/dns/groups/${groupId}/zones/${zoneId}/export`, {
        responseType: "text",
        transformResponse: (d) => d,
      })
      .then((r) => ({
        data: r.data,
        // Prefer the backend's Content-Disposition filename — it carries the
        // UTC timestamp suffix. Callers must not fabricate their own.
        filename: _parseContentDispositionFilename(
          r.headers["content-disposition"],
        ),
      })),
  syncZoneWithServer: (groupId: string, zoneId: string, apply = true) =>
    api
      .post<{
        server_records: number;
        existing_in_db: number;
        imported: number;
        skipped_unsupported: number;
        imported_records: {
          name: string;
          fqdn: string;
          record_type: string;
          value: string;
          ttl: number | null;
        }[];
        push_candidates: number;
        pushed: number;
        pushed_records: {
          name: string;
          fqdn: string;
          record_type: string;
          value: string;
          ttl: number | null;
        }[];
        push_errors: string[];
      }>(`/dns/groups/${groupId}/zones/${zoneId}/sync-with-server`, { apply })
      .then((r) => r.data),
  exportAllZones: (groupId: string, viewId?: string | null) =>
    api
      .get<Blob>(`/dns/groups/${groupId}/zones/export`, {
        params: viewId ? { view_id: viewId } : {},
        responseType: "blob",
      })
      .then((r) => ({
        data: r.data,
        filename: _parseContentDispositionFilename(
          r.headers["content-disposition"],
        ),
      })),
};

function _parseContentDispositionFilename(
  header: string | undefined,
): string | null {
  if (!header) return null;
  const match = header.match(/filename="?([^";]+)"?/i);
  return match ? match[1] : null;
}

export interface DNSRecordChange {
  op: "create" | "update" | "delete" | "unchanged";
  name: string;
  record_type: string;
  value: string;
  ttl: number | null;
  priority: number | null;
  weight: number | null;
  port: number | null;
  existing_id: string | null;
}

export interface DNSImportPreview {
  zone_id: string | null;
  zone_name: string;
  to_create: DNSRecordChange[];
  to_update: DNSRecordChange[];
  to_delete: DNSRecordChange[];
  unchanged: DNSRecordChange[];
  soa_detected: boolean;
  record_count: number;
}

export interface DNSImportCommit {
  zone_id: string;
  batch_id: string;
  created: number;
  updated: number;
  deleted: number;
  unchanged: number;
  conflict_strategy: string;
}

// ── DNS Blocking Lists ─────────────────────────────────────────────────────

export interface DNSBlockList {
  id: string;
  name: string;
  description: string;
  category: string;
  source_type: string;
  feed_url: string | null;
  feed_format: string;
  update_interval_hours: number;
  block_mode: string;
  sinkhole_ip: string | null;
  enabled: boolean;
  last_synced_at: string | null;
  last_sync_status: string | null;
  last_sync_error: string | null;
  entry_count: number;
  created_at: string;
  modified_at: string;
  applied_group_ids: string[];
  applied_view_ids: string[];
}

export interface DNSBlockListEntry {
  id: string;
  list_id: string;
  domain: string;
  entry_type: string;
  target: string | null;
  source: string;
  is_wildcard: boolean;
  reason: string;
  added_at: string;
}

export interface DNSBlockListEntryPage {
  total: number;
  items: DNSBlockListEntry[];
}

export interface DNSBlockListException {
  id: string;
  list_id: string;
  domain: string;
  reason: string;
  created_at: string;
}

export interface BlocklistCatalogSource {
  id: string;
  name: string;
  description: string;
  category: string;
  feed_url: string;
  feed_format: string;
  license: string;
  homepage: string | null;
  recommended: boolean;
}

export interface BlocklistCatalogResponse {
  version: string;
  comment: string;
  sources: BlocklistCatalogSource[];
}

export const dnsBlocklistApi = {
  list: () => api.get<DNSBlockList[]>("/dns/blocklists").then((r) => r.data),
  catalog: () =>
    api
      .get<BlocklistCatalogResponse>("/dns/blocklists/catalog")
      .then((r) => r.data),
  subscribeFromCatalog: (body: {
    source_id: string;
    name?: string;
    update_interval_hours?: number;
    block_mode?: string;
    enabled?: boolean;
  }) =>
    api
      .post<DNSBlockList>("/dns/blocklists/from-catalog", body)
      .then((r) => r.data),
  get: (id: string) =>
    api.get<DNSBlockList>(`/dns/blocklists/${id}`).then((r) => r.data),
  create: (data: Partial<DNSBlockList>) =>
    api.post<DNSBlockList>("/dns/blocklists", data).then((r) => r.data),
  update: (id: string, data: Partial<DNSBlockList>) =>
    api.put<DNSBlockList>(`/dns/blocklists/${id}`, data).then((r) => r.data),
  delete: (id: string) => api.delete(`/dns/blocklists/${id}`),

  updateAssignments: (
    id: string,
    data: { server_group_ids?: string[]; view_ids?: string[] },
  ) =>
    api
      .put<DNSBlockList>(`/dns/blocklists/${id}/assignments`, data)
      .then((r) => r.data),

  refresh: (id: string) =>
    api
      .post<{
        list_id: string;
        task_id: string | null;
        status: string;
      }>(`/dns/blocklists/${id}/refresh`)
      .then((r) => r.data),

  listEntries: (
    id: string,
    params?: { q?: string; limit?: number; offset?: number },
  ) =>
    api
      .get<DNSBlockListEntryPage>(`/dns/blocklists/${id}/entries`, { params })
      .then((r) => r.data),
  addEntry: (id: string, data: Partial<DNSBlockListEntry>) =>
    api
      .post<DNSBlockListEntry>(`/dns/blocklists/${id}/entries`, data)
      .then((r) => r.data),
  bulkAddEntries: (id: string, domains: string[]) =>
    api
      .post<{
        added: number;
        skipped: number;
        total: number;
      }>(`/dns/blocklists/${id}/entries/bulk`, { domains })
      .then((r) => r.data),
  updateEntry: (
    id: string,
    entryId: string,
    data: Partial<DNSBlockListEntry>,
  ) =>
    api
      .put<DNSBlockListEntry>(`/dns/blocklists/${id}/entries/${entryId}`, data)
      .then((r) => r.data),
  deleteEntry: (id: string, entryId: string) =>
    api.delete(`/dns/blocklists/${id}/entries/${entryId}`),

  listExceptions: (id: string) =>
    api
      .get<DNSBlockListException[]>(`/dns/blocklists/${id}/exceptions`)
      .then((r) => r.data),
  addException: (id: string, data: { domain: string; reason?: string }) =>
    api
      .post<DNSBlockListException>(`/dns/blocklists/${id}/exceptions`, data)
      .then((r) => r.data),
  updateException: (
    id: string,
    exceptionId: string,
    data: { domain?: string; reason?: string },
  ) =>
    api
      .put<DNSBlockListException>(
        `/dns/blocklists/${id}/exceptions/${exceptionId}`,
        data,
      )
      .then((r) => r.data),
  deleteException: (id: string, exceptionId: string) =>
    api.delete(`/dns/blocklists/${id}/exceptions/${exceptionId}`),
};

// ── VLANs ────────────────────────────────────────────────────────────────────

export interface Router {
  id: string;
  name: string;
  description: string;
  location: string;
  management_ip: string | null;
  vendor: string | null;
  model: string | null;
  notes: string;
  created_at: string;
  modified_at: string;
}

export interface VLAN {
  id: string;
  router_id: string;
  vlan_id: number;
  name: string;
  description: string;
  created_at: string;
  modified_at: string;
}

// ── VRFs ─────────────────────────────────────────────────────────────────────

export interface VRF {
  id: string;
  name: string;
  description: string;
  asn_id: string | null;
  route_distinguisher: string | null;
  import_targets: string[];
  export_targets: string[];
  tags: Record<string, unknown>;
  custom_fields: Record<string, unknown>;
  created_at: string;
  modified_at: string;
  space_count: number;
  block_count: number;
}

export interface VRFCreate {
  name: string;
  description?: string;
  asn_id?: string | null;
  route_distinguisher?: string | null;
  import_targets?: string[];
  export_targets?: string[];
  tags?: Record<string, unknown>;
  custom_fields?: Record<string, unknown>;
}

export type VRFUpdate = Partial<VRFCreate>;

export interface VRFBulkDeleteResponse {
  deleted: number;
  detached_spaces: number;
  detached_blocks: number;
  not_found: string[];
  refused: {
    id: string;
    name: string;
    linked_spaces: number;
    linked_blocks: number;
  }[];
}

export const vrfsApi = {
  list: (params?: { search?: string; asn_id?: string }) =>
    api.get<VRF[]>("/vrfs", { params }).then((r) => r.data),
  get: (id: string) => api.get<VRF>(`/vrfs/${id}`).then((r) => r.data),
  create: (body: VRFCreate) => api.post<VRF>("/vrfs", body).then((r) => r.data),
  update: (id: string, body: VRFUpdate) =>
    api.put<VRF>(`/vrfs/${id}`, body).then((r) => r.data),
  delete: (id: string, force = false) =>
    api.delete(`/vrfs/${id}`, { params: { force } }),
  bulkDelete: (ids: string[], force = false) =>
    api
      .post<VRFBulkDeleteResponse>("/vrfs/bulk-delete", { ids, force })
      .then((r) => r.data),
};

export const vlansApi = {
  listRouters: () => api.get<Router[]>("/vlans/routers").then((r) => r.data),
  getRouter: (id: string) =>
    api.get<Router>(`/vlans/routers/${id}`).then((r) => r.data),
  createRouter: (data: Partial<Router>) =>
    api.post<Router>("/vlans/routers", data).then((r) => r.data),
  updateRouter: (id: string, data: Partial<Router>) =>
    api.put<Router>(`/vlans/routers/${id}`, data).then((r) => r.data),
  deleteRouter: (id: string) => api.delete(`/vlans/routers/${id}`),

  listVlans: (routerId: string) =>
    api.get<VLAN[]>(`/vlans/routers/${routerId}/vlans`).then((r) => r.data),
  createVlan: (
    routerId: string,
    data: { vlan_id: number; name: string; description?: string },
  ) =>
    api
      .post<VLAN>(`/vlans/routers/${routerId}/vlans`, data)
      .then((r) => r.data),
  getVlan: (id: string) =>
    api.get<VLAN>(`/vlans/vlans/${id}`).then((r) => r.data),
  updateVlan: (
    id: string,
    data: { vlan_id?: number; name?: string; description?: string },
  ) => api.put<VLAN>(`/vlans/vlans/${id}`, data).then((r) => r.data),
  deleteVlan: (id: string) => api.delete(`/vlans/vlans/${id}`),
};

// ── DHCP ───────────────────────────────────────────────────────────────────

export interface DHCPServerGroupMember {
  id: string;
  name: string;
  driver: string;
  host: string;
  status: string;
  ha_state: string | null;
  ha_peer_url: string;
  agent_approved: boolean;
}

export interface DHCPServerGroup {
  id: string;
  name: string;
  description: string;
  // mode is the HA mode when the group has ≥ 2 Kea members:
  //   "hot-standby" | "load-balancing" | "standalone".
  mode: string;
  heartbeat_delay_ms: number;
  max_response_delay_ms: number;
  max_ack_delay_ms: number;
  max_unacked_clients: number;
  auto_failover: boolean;
  // Number of Kea servers currently in the group. ≥ 2 means HA is
  // rendered into every peer's Kea config via libdhcp_ha.so.
  kea_member_count: number;
  // Member servers (rolled up by the /server-groups response).
  servers: DHCPServerGroupMember[];
  created_at: string;
  modified_at: string;
}

export interface DHCPServerGroupCreate {
  name: string;
  description?: string;
  mode?: "standalone" | "hot-standby" | "load-balancing";
  heartbeat_delay_ms?: number;
  max_response_delay_ms?: number;
  max_ack_delay_ms?: number;
  max_unacked_clients?: number;
  auto_failover?: boolean;
}

export interface DHCPServer {
  id: string;
  server_group_id: string | null;
  name: string;
  description: string;
  driver: string;
  host: string;
  port: number;
  roles: string[];
  status: string;
  last_sync_at: string | null;
  last_health_check_at: string | null;
  agent_registered: boolean;
  agent_approved: boolean;
  agent_last_seen: string | null;
  agent_version: string | null;
  config_etag: string | null;
  config_pushed_at: string | null;
  // This server's OWN HA listener URL — empty for standalone servers.
  // The partner in the same group calls this URL for heartbeats +
  // lease updates. Rendered into the peer URL of Kea's HA hook.
  ha_peer_url?: string;
  // Populated by the agent's periodic status-get poll. Null for
  // standalone servers (group size < 2). Kea state names:
  // waiting / syncing / ready / normal / communications-interrupted /
  // partner-down / hot-standby / load-balancing / backup /
  // passive-backup / terminated.
  ha_state?: string | null;
  ha_last_heartbeat_at?: string | null;
  // True once Windows admin credentials have been stored on this server.
  // The password itself is never returned — set via `windows_credentials`
  // on the create/update body.
  has_credentials: boolean;
  // Driver runs from the control plane without a co-located agent
  // (windows_dhcp). Drives lease-pull visibility.
  is_agentless: boolean;
  // Driver only supports reads — UI hides config-push actions.
  is_read_only: boolean;
  created_at: string;
  modified_at: string;
}

export interface WindowsDHCPCredentials {
  username: string;
  password: string;
  winrm_port?: number;
  transport?: "ntlm" | "kerberos" | "basic" | "credssp";
  use_tls?: boolean;
  verify_tls?: boolean;
}

export interface DHCPLeaseSyncResult {
  server_leases: number;
  imported: number;
  refreshed: number;
  removed: number;
  ipam_created: number;
  ipam_refreshed: number;
  ipam_revoked: number;
  out_of_scope: number;
  scopes_imported: number;
  scopes_refreshed: number;
  scopes_skipped_no_subnet: number;
  pools_synced: number;
  statics_synced: number;
  mac_blocks_added?: number;
  mac_blocks_removed?: number;
  errors: string[];
}

export interface DHCPOption {
  code: number;
  name?: string;
  value: string | string[];
}

export interface DHCPScope {
  id: string;
  subnet_id: string;
  // Scopes belong to groups, not individual servers — every member of
  // the group renders the same Kea subnet4 config from this row.
  group_id: string;
  name: string;
  description: string;
  enabled: boolean;
  lease_time: number;
  min_lease_time: number | null;
  max_lease_time: number | null;
  ddns_enabled: boolean;
  ddns_hostname_policy: string | null;
  ddns_domain_override: string | null;
  hostname_sync_mode: string;
  // "ipv4" → Kea Dhcp4; "ipv6" → Kea Dhcp6. Inferred from subnet CIDR.
  address_family?: "ipv4" | "ipv6";
  options: DHCPOption[];
  // PXE / iPXE profile binding (issue #51). Null = no PXE on this
  // scope. Bound profile renders one Kea client-class per arch-match
  // on the next ConfigBundle push.
  pxe_profile_id?: string | null;
  created_at: string;
  modified_at: string;
}

// ── PXE / iPXE profiles (issue #51) ─────────────────────────────
//
// Group-scoped reusable provisioning profiles. Each profile carries
// ``next_server`` + N arch-matches. Operators bind a profile to a
// scope via ``DHCPScope.pxe_profile_id``; the Kea driver renders one
// client-class per arch-match.

/** DHCP option-93 (Client Architecture Type) lookup. Surfaced in
 * the UI's arch-codes multi-select. */
export const DHCP_PXE_ARCH_LABELS: Record<number, string> = {
  0: "BIOS / Legacy x86",
  6: "UEFI x86 (32-bit)",
  7: "UEFI x86-64",
  9: "UEFI x86-64 (alt)",
  10: "ARM 32-bit UEFI",
  11: "ARM 64-bit UEFI",
  15: "HTTP boot UEFI",
  16: "HTTP boot UEFI x86-64",
};

export type PXEMatchKind = "first_stage" | "ipxe_chain";

export interface PXEArchMatch {
  id: string;
  profile_id: string;
  priority: number;
  match_kind: PXEMatchKind;
  vendor_class_match: string | null;
  arch_codes: number[] | null;
  boot_filename: string;
  boot_file_url_v6: string | null;
  created_at: string;
  modified_at: string;
}

export interface PXEArchMatchInput {
  priority?: number;
  match_kind?: PXEMatchKind;
  vendor_class_match?: string | null;
  arch_codes?: number[] | null;
  boot_filename: string;
  boot_file_url_v6?: string | null;
}

export interface PXEProfile {
  id: string;
  group_id: string;
  name: string;
  description: string;
  next_server: string;
  enabled: boolean;
  tags: Record<string, unknown>;
  matches: PXEArchMatch[];
  created_at: string;
  modified_at: string;
}

export interface PXEProfileCreate {
  name: string;
  description?: string;
  next_server: string;
  enabled?: boolean;
  tags?: Record<string, unknown>;
  matches?: PXEArchMatchInput[];
}

export interface PXEProfileUpdate {
  name?: string;
  description?: string;
  next_server?: string;
  enabled?: boolean;
  tags?: Record<string, unknown>;
  matches?: PXEArchMatchInput[];
}

export const dhcpApi = {
  listGroups: () =>
    api.get<DHCPServerGroup[]>("/dhcp/server-groups").then((r) => r.data),
  getGroup: (id: string) =>
    api.get<DHCPServerGroup>(`/dhcp/server-groups/${id}`).then((r) => r.data),
  createGroup: (data: DHCPServerGroupCreate) =>
    api.post<DHCPServerGroup>("/dhcp/server-groups", data).then((r) => r.data),
  updateGroup: (id: string, data: Partial<DHCPServerGroupCreate>) =>
    api
      .put<DHCPServerGroup>(`/dhcp/server-groups/${id}`, data)
      .then((r) => r.data),
  deleteGroup: (id: string) => api.delete(`/dhcp/server-groups/${id}`),

  listServers: (groupId?: string) =>
    api
      .get<DHCPServer[]>("/dhcp/servers")
      .then((r) =>
        groupId ? r.data.filter((s) => s.server_group_id === groupId) : r.data,
      ),
  getServer: (id: string) =>
    api.get<DHCPServer>(`/dhcp/servers/${id}`).then((r) => r.data),
  createServer: (data: Partial<DHCPServer>) =>
    api.post<DHCPServer>("/dhcp/servers", data).then((r) => r.data),
  updateServer: (id: string, data: Partial<DHCPServer>) =>
    api.put<DHCPServer>(`/dhcp/servers/${id}`, data).then((r) => r.data),
  deleteServer: (id: string) => api.delete(`/dhcp/servers/${id}`),
  syncServer: (id: string) =>
    api
      .post<{
        status: string;
        op_id: string;
        etag: string;
      }>(`/dhcp/servers/${id}/sync`)
      .then((r) => r.data),
  syncLeasesNow: (id: string) =>
    api
      .post<DHCPLeaseSyncResult>(`/dhcp/servers/${id}/sync-leases`)
      .then((r) => r.data),
  testWindowsCredentials: (body: {
    host: string;
    credentials?: WindowsDHCPCredentials;
    server_id?: string;
  }) =>
    api
      .post<{
        ok: boolean;
        message: string;
      }>("/dhcp/servers/test-windows-credentials", body)
      .then((r) => r.data),
  approveServer: (id: string) =>
    api.post<DHCPServer>(`/dhcp/servers/${id}/approve`).then((r) => r.data),
  getLeases: (id: string, params?: { limit?: number }) =>
    api
      .get<DHCPLease[]>(`/dhcp/servers/${id}/leases`, { params })
      .then((r) => r.data),

  listScopesBySubnet: (subnetId: string) =>
    api
      .get<DHCPScope[]>(`/dhcp/subnets/${subnetId}/dhcp-scopes`)
      .then((r) => r.data),
  listScopesByGroup: (groupId: string) =>
    api
      .get<DHCPScope[]>(`/dhcp/server-groups/${groupId}/scopes`)
      .then((r) => r.data),
  getScope: (id: string) =>
    api.get<DHCPScope>(`/dhcp/scopes/${id}`).then((r) => r.data),
  createScope: (subnetId: string, data: Partial<DHCPScope>) =>
    api
      .post<DHCPScope>(`/dhcp/subnets/${subnetId}/dhcp-scopes`, data)
      .then((r) => r.data),
  updateScope: (id: string, data: Partial<DHCPScope>) =>
    api.put<DHCPScope>(`/dhcp/scopes/${id}`, data).then((r) => r.data),
  deleteScope: (id: string) => api.delete(`/dhcp/scopes/${id}`),

  listPools: (scopeId: string) =>
    api.get<DHCPPool[]>(`/dhcp/scopes/${scopeId}/pools`).then((r) => r.data),
  createPool: (scopeId: string, data: Partial<DHCPPool>) =>
    api
      .post<DHCPPool>(`/dhcp/scopes/${scopeId}/pools`, data)
      .then((r) => r.data),
  updatePool: (_scopeId: string, poolId: string, data: Partial<DHCPPool>) =>
    api.put<DHCPPool>(`/dhcp/pools/${poolId}`, data).then((r) => r.data),
  deletePool: (_scopeId: string, poolId: string) =>
    api.delete(`/dhcp/pools/${poolId}`),

  listStatics: (scopeId: string) =>
    api
      .get<DHCPStaticAssignment[]>(`/dhcp/scopes/${scopeId}/statics`)
      .then((r) => r.data),
  createStatic: (scopeId: string, data: Partial<DHCPStaticAssignment>) =>
    api
      .post<DHCPStaticAssignment>(`/dhcp/scopes/${scopeId}/statics`, data)
      .then((r) => r.data),
  updateStatic: (
    _scopeId: string,
    staticId: string,
    data: Partial<DHCPStaticAssignment>,
  ) =>
    api
      .put<DHCPStaticAssignment>(`/dhcp/statics/${staticId}`, data)
      .then((r) => r.data),
  deleteStatic: (_scopeId: string, staticId: string) =>
    api.delete(`/dhcp/statics/${staticId}`),

  listClientClasses: (groupId: string) =>
    api
      .get<DHCPClientClass[]>(`/dhcp/server-groups/${groupId}/client-classes`)
      .then((r) => r.data),
  createClientClass: (groupId: string, data: Partial<DHCPClientClass>) =>
    api
      .post<DHCPClientClass>(
        `/dhcp/server-groups/${groupId}/client-classes`,
        data,
      )
      .then((r) => r.data),
  updateClientClass: (
    _groupId: string,
    classId: string,
    data: Partial<DHCPClientClass>,
  ) =>
    api
      .put<DHCPClientClass>(`/dhcp/client-classes/${classId}`, data)
      .then((r) => r.data),
  deleteClientClass: (_groupId: string, classId: string) =>
    api.delete(`/dhcp/client-classes/${classId}`),

  listOptionCodes: (q?: string) =>
    api
      .get<DHCPOptionCodeDef[]>("/dhcp/option-codes", {
        params: q ? { q } : undefined,
      })
      .then((r) => r.data),

  listOptionTemplates: (groupId: string) =>
    api
      .get<
        DHCPOptionTemplate[]
      >(`/dhcp/server-groups/${groupId}/option-templates`)
      .then((r) => r.data),
  createOptionTemplate: (groupId: string, data: DHCPOptionTemplateWrite) =>
    api
      .post<DHCPOptionTemplate>(
        `/dhcp/server-groups/${groupId}/option-templates`,
        data,
      )
      .then((r) => r.data),
  updateOptionTemplate: (
    _groupId: string,
    templateId: string,
    data: Partial<DHCPOptionTemplateWrite>,
  ) =>
    api
      .put<DHCPOptionTemplate>(`/dhcp/option-templates/${templateId}`, data)
      .then((r) => r.data),
  deleteOptionTemplate: (_groupId: string, templateId: string) =>
    api.delete(`/dhcp/option-templates/${templateId}`),
  applyOptionTemplate: (
    scopeId: string,
    data: { template_id: string; mode?: "merge" | "replace" },
  ) =>
    api
      .post<DHCPApplyTemplateResponse>(
        `/dhcp/scopes/${scopeId}/apply-option-template`,
        data,
      )
      .then((r) => r.data),

  // ── PXE / iPXE profiles (issue #51) ──────────────────────────────
  listPxeProfiles: (groupId: string) =>
    api
      .get<PXEProfile[]>(`/dhcp/server-groups/${groupId}/pxe-profiles`)
      .then((r) => r.data),
  getPxeProfile: (profileId: string) =>
    api.get<PXEProfile>(`/dhcp/pxe-profiles/${profileId}`).then((r) => r.data),
  createPxeProfile: (groupId: string, body: PXEProfileCreate) =>
    api
      .post<PXEProfile>(`/dhcp/server-groups/${groupId}/pxe-profiles`, body)
      .then((r) => r.data),
  updatePxeProfile: (profileId: string, body: PXEProfileUpdate) =>
    api
      .put<PXEProfile>(`/dhcp/pxe-profiles/${profileId}`, body)
      .then((r) => r.data),
  deletePxeProfile: (profileId: string) =>
    api.delete(`/dhcp/pxe-profiles/${profileId}`),

  listMacBlocks: (groupId: string) =>
    api
      .get<DHCPMACBlock[]>(`/dhcp/server-groups/${groupId}/mac-blocks`)
      .then((r) => r.data),
  createMacBlock: (groupId: string, data: DHCPMACBlockWrite) =>
    api
      .post<DHCPMACBlock>(`/dhcp/server-groups/${groupId}/mac-blocks`, data)
      .then((r) => r.data),
  updateMacBlock: (
    _groupId: string,
    blockId: string,
    data: Partial<DHCPMACBlockWrite>,
  ) =>
    api
      .put<DHCPMACBlock>(`/dhcp/mac-blocks/${blockId}`, data)
      .then((r) => r.data),
  deleteMacBlock: (_groupId: string, blockId: string) =>
    api.delete(`/dhcp/mac-blocks/${blockId}`),
};

export interface DHCPPool {
  id: string;
  scope_id: string;
  name: string;
  start_ip: string;
  end_ip: string;
  pool_type: string; // "dynamic" | "excluded" | "reserved"
  class_restriction: string | null;
  lease_time_override: number | null;
  options_override: Record<string, unknown> | null;
  // Populated by create/update only: IPs already allocated inside this
  // range, so the modal can surface a confirmation before overwriting.
  existing_ips_in_range?:
    | {
        address: string;
        status: string;
        hostname: string;
      }[]
    | null;
  created_at: string;
  modified_at: string;
}

export interface DHCPStaticAssignment {
  id: string;
  scope_id: string;
  ip_address: string;
  mac_address: string;
  hostname: string;
  description: string;
  client_id: string | null;
  options_override: Record<string, unknown> | null;
  ip_address_id: string | null;
  created_at: string;
  modified_at: string;
}

export interface DHCPClientClass {
  id: string;
  // Under the group-centric model, classes belong to a server group —
  // every member renders the same classes into its Kea config.
  group_id: string;
  name: string;
  description: string;
  match_expression: string;
  options: Record<string, unknown>;
  created_at: string;
  modified_at: string;
}

export interface DHCPOptionCodeDef {
  code: number;
  name: string;
  kind: string;
  description: string;
  rfc?: string | null;
}

export interface DHCPOptionTemplate {
  id: string;
  group_id: string;
  name: string;
  description: string;
  address_family: "ipv4" | "ipv6";
  options: Record<string, string | string[]>;
  created_at: string;
  modified_at: string;
}

export interface DHCPOptionTemplateWrite {
  name: string;
  description?: string;
  address_family?: "ipv4" | "ipv6";
  options?: Record<string, string | string[]>;
}

export interface DHCPApplyTemplateResponse {
  scope_id: string;
  options: Record<string, string | string[]>;
  overwritten_keys: string[];
}

export type DHCPMACBlockReason =
  | "rogue"
  | "lost_stolen"
  | "quarantine"
  | "policy"
  | "other";

export interface DHCPMACBlockIPAMMatch {
  ip_address: string;
  subnet_cidr: string;
  hostname: string;
  description: string;
}

export interface DHCPMACBlock {
  id: string;
  group_id: string;
  mac_address: string;
  reason: DHCPMACBlockReason;
  description: string;
  enabled: boolean;
  expires_at: string | null;
  created_at: string;
  modified_at: string;
  created_by_user_id: string | null;
  updated_by_user_id: string | null;
  last_match_at: string | null;
  match_count: number;
  vendor: string | null;
  ipam_matches: DHCPMACBlockIPAMMatch[];
}

export interface DHCPMACBlockWrite {
  mac_address?: string;
  reason?: DHCPMACBlockReason;
  description?: string;
  enabled?: boolean;
  expires_at?: string | null;
}

export interface DHCPLease {
  id: string;
  server_id: string;
  scope_id: string | null;
  ip_address: string;
  mac_address: string;
  hostname: string | null;
  state: string; // "active" | "expired" | "released" | "abandoned"
  starts_at: string | null;
  ends_at: string | null;
  expires_at: string | null;
  last_seen_at: string;
  vendor?: string | null;
}

export interface PublicAuthProvider {
  id: string;
  name: string;
  type: AuthProviderType;
}

export const authApi = {
  login: (username: string, password: string) =>
    api
      .post<LoginResponse>("/auth/login", { username, password })
      .then((r) => r.data),
  /** Complete a TOTP-gated login. Submit either ``code`` (6-digit
   * authenticator) or ``recovery_code``; submitting both 422s. */
  loginMfa: (
    mfa_token: string,
    body: { code?: string; recovery_code?: string },
  ) =>
    api
      .post<LoginResponse>("/auth/login/mfa", { mfa_token, ...body })
      .then((r) => r.data),
  publicProviders: () =>
    api.get<PublicAuthProvider[]>("/auth/providers").then((r) => r.data),
  logout: () => api.post("/auth/logout"),
  refresh: (refreshToken: string) =>
    api
      .post<LoginResponse>("/auth/refresh", { refresh_token: refreshToken })
      .then((r) => r.data),
  changePassword: (currentPassword: string, newPassword: string) =>
    api.post("/auth/change-password", {
      current_password: currentPassword,
      new_password: newPassword,
    }),
  me: () =>
    api
      .get<{
        id: string;
        username: string;
        email: string;
        display_name: string;
        is_superadmin: boolean;
        force_password_change: boolean;
        auth_source: string;
      }>("/auth/me")
      .then((r) => r.data),

  // ── MFA (issue #69) ─────────────────────────────────────────────────
  mfaStatus: () =>
    api.get<MfaStatusResponse>("/auth/mfa/status").then((r) => r.data),
  mfaEnrollBegin: () =>
    api
      .post<MfaEnrolBeginResponse>("/auth/mfa/enroll/begin")
      .then((r) => r.data),
  mfaEnrollVerify: (code: string) =>
    api.post("/auth/mfa/enroll/verify", { code }),
  mfaDisable: (password: string, code: string) =>
    api.post("/auth/mfa/disable", { password, code }),
  mfaRegenerateRecoveryCodes: (password: string, code: string) =>
    api
      .post<MfaEnrolBeginResponse>("/auth/mfa/recovery-codes/regenerate", {
        password,
        code,
      })
      .then((r) => r.data),
};

// ── Logs ──────────────────────────────────────────────────────────────────

export interface LogNameOption {
  name: string; // e.g. "Microsoft-Windows-Dhcp-Server/Operational"
  display: string; // what the UI shows in the picker
}

export interface LogSource {
  server_id: string;
  server_name: string;
  server_kind: "dns" | "dhcp";
  driver: string;
  host: string;
  logs: LogNameOption[];
}

export interface LogEventRow {
  time: string; // ISO 8601
  id: number;
  level: string; // "Error" | "Warning" | "Information" | "Verbose" | "Critical"
  provider: string;
  machine: string;
  message: string;
}

export interface LogQueryRequest {
  server_id: string;
  server_kind: "dns" | "dhcp";
  log_name: string;
  max_events?: number; // 1..500, default 100
  level?: number | null; // 1=Critical, 2=Error, 3=Warning, 4=Info, 5=Verbose
  since?: string | null; // ISO 8601
  event_id?: number | null;
}

export interface LogQueryResponse {
  server_id: string;
  server_kind: "dns" | "dhcp";
  log_name: string;
  events: LogEventRow[];
  truncated: boolean;
}

export interface DhcpAuditRow {
  time: string;
  event_code: number;
  event_label: string;
  description: string;
  ip_address: string;
  hostname: string;
  mac_address: string;
  user_name: string;
  transaction_id: string;
  q_result: string;
}

export type DhcpAuditDay =
  | "Mon"
  | "Tue"
  | "Wed"
  | "Thu"
  | "Fri"
  | "Sat"
  | "Sun";

export interface DhcpAuditRequest {
  server_id: string;
  day?: DhcpAuditDay | null;
  max_events?: number;
}

export interface DhcpAuditResponse {
  server_id: string;
  day: DhcpAuditDay;
  events: DhcpAuditRow[];
  truncated: boolean;
}

// ── Agent-shipped logs (BIND9 + Kea) ─────────────────────────────

export interface AgentLogSource {
  server_id: string;
  server_name: string;
  server_kind: "dns" | "dhcp";
  driver: string;
  host: string;
}

export interface DNSQueryLogRow {
  id: number;
  ts: string;
  client_ip: string | null;
  client_port: number | null;
  qname: string | null;
  qclass: string | null;
  qtype: string | null;
  flags: string | null;
  view: string | null;
  raw: string;
}

export interface DNSQueryLogRequest {
  server_id: string;
  since?: string | null;
  until?: string | null;
  q?: string | null;
  qtype?: string | null;
  client_ip?: string | null;
  max_events?: number;
}

export interface DNSQueryLogResponse {
  server_id: string;
  events: DNSQueryLogRow[];
  truncated: boolean;
}

export interface DHCPActivityLogRow {
  id: number;
  ts: string;
  severity: string | null;
  code: string | null;
  mac_address: string | null;
  ip_address: string | null;
  transaction_id: string | null;
  raw: string;
}

export interface DHCPActivityLogRequest {
  server_id: string;
  since?: string | null;
  until?: string | null;
  q?: string | null;
  severity?: string | null;
  code?: string | null;
  mac_address?: string | null;
  ip_address?: string | null;
  max_events?: number;
}

export interface DHCPActivityLogResponse {
  server_id: string;
  events: DHCPActivityLogRow[];
  truncated: boolean;
}

// On-demand top-N rollups computed against `dns_query_log_entry`
// (24 h retention). One round trip returns three dimensions.
export interface DNSQueryAnalyticsRow {
  key: string;
  count: number;
}

export interface DNSQueryAnalyticsRequest {
  server_id: string;
  since?: string | null;
  until?: string | null;
  limit?: number;
}

export interface DNSQueryAnalyticsResponse {
  server_id: string;
  since: string | null;
  until: string | null;
  total_queries: number;
  top_qnames: DNSQueryAnalyticsRow[];
  top_clients: DNSQueryAnalyticsRow[];
  qtype_distribution: DNSQueryAnalyticsRow[];
}

export const logsApi = {
  listSources: () => api.get<LogSource[]>("/logs/sources").then((r) => r.data),
  listAgentSources: () =>
    api.get<AgentLogSource[]>("/logs/agent-sources").then((r) => r.data),
  query: (body: LogQueryRequest) =>
    api.post<LogQueryResponse>("/logs/query", body).then((r) => r.data),
  dhcpAudit: (body: DhcpAuditRequest) =>
    api.post<DhcpAuditResponse>("/logs/dhcp-audit", body).then((r) => r.data),
  dnsQueries: (body: DNSQueryLogRequest) =>
    api
      .post<DNSQueryLogResponse>("/logs/dns-queries", body)
      .then((r) => r.data),
  dnsQueryAnalytics: (body: DNSQueryAnalyticsRequest) =>
    api
      .post<DNSQueryAnalyticsResponse>("/logs/dns-queries/analytics", body)
      .then((r) => r.data),
  dhcpActivity: (body: DHCPActivityLogRequest) =>
    api
      .post<DHCPActivityLogResponse>("/logs/dhcp-activity", body)
      .then((r) => r.data),
};

// ── API Tokens ────────────────────────────────────────────────────────────────

/** Coarse-grained scope vocabulary — see issue #74 +
 * `app/services/api_token_scopes.py`. Empty list = no scope
 * restriction (token still inherits the owner's RBAC). Non-empty
 * = enforced at the auth layer BEFORE RBAC. Multiple scopes
 * union; ``read`` covers safe-method requests across the surface.
 */
export type ApiTokenScope =
  | "read"
  | "ipam:write"
  | "dns:write"
  | "dhcp:write"
  | "agent";

export const API_TOKEN_SCOPES: {
  value: ApiTokenScope;
  label: string;
  hint: string;
}[] = [
  {
    value: "read",
    label: "Read-only",
    hint: "GET / HEAD / OPTIONS only — no mutations anywhere.",
  },
  {
    value: "ipam:write",
    label: "IPAM write",
    hint: "Mutate /ipam/*, /vlans*, /vrfs*, /network-devices*.",
  },
  {
    value: "dns:write",
    label: "DNS write",
    hint: "Mutate /dns/* + /dns-pools*. Excludes the agent surface.",
  },
  {
    value: "dhcp:write",
    label: "DHCP write",
    hint: "Mutate /dhcp/*. Excludes the agent surface.",
  },
  {
    value: "agent",
    label: "Agent",
    hint: "Bootstrap + push for /dns/agents/* and /dhcp/agents/*.",
  },
];

export interface ApiToken {
  id: string;
  name: string;
  description: string;
  prefix: string;
  scope: string;
  scopes: ApiTokenScope[];
  user_id: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  is_active: boolean;
  created_at: string;
}

export interface ApiTokenCreate {
  name: string;
  description?: string;
  expires_in_days?: number | null;
  scopes?: ApiTokenScope[];
}

/** Response from POST — contains the raw token ONCE. */
export interface ApiTokenCreated extends ApiToken {
  token: string;
}

export interface ApiTokenUpdate {
  name?: string;
  description?: string;
  is_active?: boolean;
  scopes?: ApiTokenScope[];
}

export const apiTokensApi = {
  list: () => api.get<ApiToken[]>("/api-tokens").then((r) => r.data),
  create: (body: ApiTokenCreate) =>
    api.post<ApiTokenCreated>("/api-tokens", body).then((r) => r.data),
  update: (id: string, body: ApiTokenUpdate) =>
    api.patch<ApiToken>(`/api-tokens/${id}`, body).then((r) => r.data),
  delete: (id: string) => api.delete(`/api-tokens/${id}`),
};

// ── Alerts ─────────────────────────────────────────────────────────────────────

export type AlertRuleType =
  | "subnet_utilization"
  | "server_unreachable"
  | "asn_holder_drift"
  | "asn_whois_unreachable"
  | "rpki_roa_expiring"
  | "rpki_roa_expired"
  | "domain_expiring"
  | "domain_nameserver_drift"
  | "domain_registrar_changed"
  | "domain_dnssec_status_changed";
export type AlertSeverity = "info" | "warning" | "critical";
export type AlertServerType = "dns" | "dhcp" | "any";

export interface AlertRule {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  rule_type: AlertRuleType;
  threshold_percent: number | null;
  threshold_days: number | null;
  server_type: AlertServerType | null;
  severity: AlertSeverity;
  notify_syslog: boolean;
  notify_webhook: boolean;
  notify_smtp: boolean;
  created_at: string;
  modified_at: string;
}

export interface AlertRuleCreate {
  name: string;
  description?: string;
  enabled?: boolean;
  rule_type: AlertRuleType;
  threshold_percent?: number | null;
  threshold_days?: number | null;
  server_type?: AlertServerType | null;
  severity?: AlertSeverity;
  notify_syslog?: boolean;
  notify_webhook?: boolean;
  notify_smtp?: boolean;
}

export interface AlertRuleUpdate {
  name?: string;
  description?: string;
  enabled?: boolean;
  threshold_percent?: number | null;
  threshold_days?: number | null;
  server_type?: AlertServerType | null;
  severity?: AlertSeverity;
  notify_syslog?: boolean;
  notify_webhook?: boolean;
  notify_smtp?: boolean;
}

export interface AlertEvent {
  id: string;
  rule_id: string;
  subject_type: string;
  subject_id: string;
  subject_display: string;
  severity: AlertSeverity;
  message: string;
  fired_at: string;
  resolved_at: string | null;
  delivered_syslog: boolean;
  delivered_webhook: boolean;
  delivered_smtp: boolean;
  last_observed_value: Record<string, unknown> | null;
}

export interface AlertEvaluateResult {
  opened: number;
  resolved: number;
  delivered_syslog: number;
  delivered_webhook: number;
  delivered_smtp: number;
}

export const alertsApi = {
  listRules: () => api.get<AlertRule[]>("/alerts/rules").then((r) => r.data),
  createRule: (body: AlertRuleCreate) =>
    api.post<AlertRule>("/alerts/rules", body).then((r) => r.data),
  updateRule: (id: string, body: AlertRuleUpdate) =>
    api.patch<AlertRule>(`/alerts/rules/${id}`, body).then((r) => r.data),
  deleteRule: (id: string) => api.delete(`/alerts/rules/${id}`),
  listEvents: (
    params: { open_only?: boolean; rule_id?: string; limit?: number } = {},
  ) => api.get<AlertEvent[]>("/alerts/events", { params }).then((r) => r.data),
  resolveEvent: (id: string) =>
    api.post<AlertEvent>(`/alerts/events/${id}/resolve`).then((r) => r.data),
  evaluateNow: () =>
    api.post<AlertEvaluateResult>("/alerts/evaluate").then((r) => r.data),
};

// ── Domain registration (RDAP / WHOIS tracking) ─────────────────────────────

export type DomainWhoisState =
  | "ok"
  | "drift"
  | "expiring"
  | "expired"
  | "unreachable"
  | "unknown";

export interface Domain {
  id: string;
  name: string;
  registrar: string | null;
  registrant_org: string | null;
  registered_at: string | null;
  expires_at: string | null;
  last_renewed_at: string | null;
  expected_nameservers: string[];
  actual_nameservers: string[];
  nameserver_drift: boolean;
  dnssec_signed: boolean;
  whois_last_checked_at: string | null;
  whois_state: DomainWhoisState;
  whois_data: Record<string, unknown> | null;
  next_check_at: string | null;
  tags: Record<string, unknown>;
  custom_fields: Record<string, unknown>;
  created_at: string;
  modified_at: string;
}

export interface DomainCreate {
  name: string;
  expected_nameservers?: string[];
  tags?: Record<string, unknown>;
  custom_fields?: Record<string, unknown>;
}

export interface DomainUpdate {
  name?: string;
  expected_nameservers?: string[];
  tags?: Record<string, unknown>;
  custom_fields?: Record<string, unknown>;
}

export interface DomainListResponse {
  items: Domain[];
  total: number;
  page: number;
  page_size: number;
}

export interface DomainListParams {
  whois_state?: DomainWhoisState;
  expiring_within_days?: number;
  search?: string;
  page?: number;
  page_size?: number;
}

export const domainsApi = {
  list: (params: DomainListParams = {}) =>
    api.get<DomainListResponse>("/domains", { params }).then((r) => r.data),
  get: (id: string) => api.get<Domain>(`/domains/${id}`).then((r) => r.data),
  create: (body: DomainCreate) =>
    api.post<Domain>("/domains", body).then((r) => r.data),
  update: (id: string, body: DomainUpdate) =>
    api.put<Domain>(`/domains/${id}`, body).then((r) => r.data),
  delete: (id: string) => api.delete(`/domains/${id}`),
  refreshWhois: (id: string) =>
    api.post<Domain>(`/domains/${id}/refresh-whois`).then((r) => r.data),
  bulkDelete: (ids: string[]) =>
    api
      .post<{ deleted: number }>("/domains/bulk-delete", { ids })
      .then((r) => r.data),
};

// ── Webhooks (typed-event subscriptions) ────────────────────────────────────

export interface WebhookSubscription {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  url: string;
  // Server-side bool — the secret itself is Fernet-encrypted at rest
  // and only ever returned in plaintext from the create / update
  // response when one was newly assigned (``secret_plaintext``).
  secret_set: boolean;
  event_types: string[] | null;
  headers: Record<string, string> | null;
  timeout_seconds: number;
  max_attempts: number;
  created_at: string;
  modified_at: string;
  // Populated only on the create response (and on update when the
  // operator supplied a new value). Surface to the operator once,
  // then drop.
  secret_plaintext?: string | null;
}

export interface WebhookSubscriptionWrite {
  name: string;
  description?: string;
  enabled: boolean;
  url: string;
  // ``null`` on edit = keep existing, ``""`` = clear, anything else =
  // sent in plaintext + encrypted server-side.
  secret?: string | null;
  event_types?: string[] | null;
  headers?: Record<string, string> | null;
  timeout_seconds?: number;
  max_attempts?: number;
}

export interface WebhookDelivery {
  id: string;
  subscription_id: string;
  event_type: string;
  state: "pending" | "in_flight" | "delivered" | "failed" | "dead";
  attempts: number;
  next_attempt_at: string;
  last_error: string | null;
  last_status_code: number | null;
  delivered_at: string | null;
  created_at: string;
}

export interface WebhookTestResult {
  status: "ok" | "error";
  status_code: number | null;
  error: string | null;
}

export const webhooksApi = {
  listEventTypes: () =>
    api
      .get<{ event_types: string[] }>("/webhooks/event-types")
      .then((r) => r.data.event_types),
  list: () => api.get<WebhookSubscription[]>("/webhooks").then((r) => r.data),
  get: (id: string) =>
    api.get<WebhookSubscription>(`/webhooks/${id}`).then((r) => r.data),
  create: (body: WebhookSubscriptionWrite) =>
    api.post<WebhookSubscription>("/webhooks", body).then((r) => r.data),
  update: (id: string, body: WebhookSubscriptionWrite) =>
    api.put<WebhookSubscription>(`/webhooks/${id}`, body).then((r) => r.data),
  delete: (id: string) => api.delete(`/webhooks/${id}`),
  test: (id: string) =>
    api.post<WebhookTestResult>(`/webhooks/${id}/test`).then((r) => r.data),
  listDeliveries: (id: string, limit = 100) =>
    api
      .get<WebhookDelivery[]>(`/webhooks/${id}/deliveries`, {
        params: { limit },
      })
      .then((r) => r.data),
  retryDelivery: (deliveryId: string) =>
    api
      .post<WebhookDelivery>(`/webhooks/deliveries/${deliveryId}/retry`)
      .then((r) => r.data),
};

export type MetricsWindow = "1h" | "6h" | "24h" | "7d";

export interface DNSMetricsPoint {
  t: string;
  queries_total: number;
  noerror: number;
  nxdomain: number;
  servfail: number;
  recursion: number;
}

export interface DNSMetricsSeries {
  window: MetricsWindow;
  bucket_seconds: number;
  points: DNSMetricsPoint[];
}

export interface DHCPMetricsPoint {
  t: string;
  discover: number;
  offer: number;
  request: number;
  ack: number;
  nak: number;
  decline: number;
  release: number;
  inform: number;
}

export interface DHCPMetricsSeries {
  window: MetricsWindow;
  bucket_seconds: number;
  points: DHCPMetricsPoint[];
}

export const metricsApi = {
  dnsTimeseries: (
    params: { window?: MetricsWindow; server_id?: string } = {},
  ) =>
    api
      .get<DNSMetricsSeries>("/metrics/dns/timeseries", { params })
      .then((r) => r.data),
  dhcpTimeseries: (
    params: { window?: MetricsWindow; server_id?: string } = {},
  ) =>
    api
      .get<DHCPMetricsSeries>("/metrics/dhcp/timeseries", { params })
      .then((r) => r.data),
};

export interface VersionInfo {
  version: string;
  latest_version: string | null;
  update_available: boolean;
  latest_release_url: string | null;
  latest_checked_at: string | null;
  release_check_enabled: boolean;
  latest_check_error: string | null;
}

export const versionApi = {
  get: () => api.get<VersionInfo>("/version").then((r) => r.data),
};

// ── Kubernetes integration ─────────────────────────────────────────

export interface KubernetesCluster {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  api_server_url: string;
  ca_bundle_present: boolean;
  token_present: boolean;
  ipam_space_id: string;
  dns_group_id: string | null;
  pod_cidr: string;
  service_cidr: string;
  sync_interval_seconds: number;
  mirror_pods: boolean;
  last_synced_at: string | null;
  last_sync_error: string | null;
  cluster_version: string | null;
  node_count: number | null;
  created_at: string;
  modified_at: string;
}

export interface KubernetesClusterCreate {
  name: string;
  description?: string;
  enabled?: boolean;
  api_server_url: string;
  ca_bundle_pem?: string;
  token: string;
  ipam_space_id: string;
  dns_group_id?: string | null;
  pod_cidr?: string;
  service_cidr?: string;
  sync_interval_seconds?: number;
  mirror_pods?: boolean;
}

export interface KubernetesClusterUpdate {
  name?: string;
  description?: string;
  enabled?: boolean;
  api_server_url?: string;
  ca_bundle_pem?: string;
  token?: string;
  ipam_space_id?: string;
  dns_group_id?: string | null;
  pod_cidr?: string;
  service_cidr?: string;
  sync_interval_seconds?: number;
  mirror_pods?: boolean;
}

export interface KubernetesTestResult {
  ok: boolean;
  message: string;
  version: string | null;
  node_count: number | null;
}

export interface KubernetesDetectCIDRsResult {
  pod_cidr: string | null;
  service_cidr: string | null;
  messages: string[];
}

export const kubernetesApi = {
  listClusters: () =>
    api.get<KubernetesCluster[]>("/kubernetes/clusters").then((r) => r.data),
  createCluster: (data: KubernetesClusterCreate) =>
    api
      .post<KubernetesCluster>("/kubernetes/clusters", data)
      .then((r) => r.data),
  updateCluster: (id: string, data: KubernetesClusterUpdate) =>
    api
      .put<KubernetesCluster>(`/kubernetes/clusters/${id}`, data)
      .then((r) => r.data),
  deleteCluster: (id: string) => api.delete(`/kubernetes/clusters/${id}`),
  testConnection: (body: {
    cluster_id?: string;
    api_server_url?: string;
    ca_bundle_pem?: string;
    token?: string;
  }) =>
    api
      .post<KubernetesTestResult>("/kubernetes/clusters/test", body)
      .then((r) => r.data),
  syncNow: (id: string) =>
    api
      .post<{
        status: string;
        task_id: string;
      }>(`/kubernetes/clusters/${id}/sync`)
      .then((r) => r.data),
  detectCidrs: (body: {
    cluster_id?: string;
    api_server_url?: string;
    ca_bundle_pem?: string;
    token?: string;
  }) =>
    api
      .post<KubernetesDetectCIDRsResult>(
        "/kubernetes/clusters/detect-cidrs",
        body,
      )
      .then((r) => r.data),
};

// ── Docker integration ──────────────────────────────────────────────

export interface DockerHost {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  connection_type: "unix" | "tcp";
  endpoint: string;
  ca_bundle_present: boolean;
  client_cert_present: boolean;
  client_key_present: boolean;
  ipam_space_id: string;
  dns_group_id: string | null;
  mirror_containers: boolean;
  include_default_networks: boolean;
  include_stopped_containers: boolean;
  sync_interval_seconds: number;
  last_synced_at: string | null;
  last_sync_error: string | null;
  engine_version: string | null;
  container_count: number | null;
  created_at: string;
  modified_at: string;
}

export interface DockerHostCreate {
  name: string;
  description?: string;
  enabled?: boolean;
  connection_type: "unix" | "tcp";
  endpoint: string;
  ca_bundle_pem?: string;
  client_cert_pem?: string;
  client_key_pem?: string;
  ipam_space_id: string;
  dns_group_id?: string | null;
  mirror_containers?: boolean;
  include_default_networks?: boolean;
  include_stopped_containers?: boolean;
  sync_interval_seconds?: number;
}

export interface DockerHostUpdate {
  name?: string;
  description?: string;
  enabled?: boolean;
  connection_type?: "unix" | "tcp";
  endpoint?: string;
  ca_bundle_pem?: string;
  client_cert_pem?: string;
  client_key_pem?: string;
  ipam_space_id?: string;
  dns_group_id?: string | null;
  mirror_containers?: boolean;
  include_default_networks?: boolean;
  include_stopped_containers?: boolean;
  sync_interval_seconds?: number;
}

export interface DockerTestResult {
  ok: boolean;
  message: string;
  engine_version: string | null;
  container_count: number | null;
}

// ── Platform health ─────────────────────────────────────────────────

export type PlatformHealthStatus = "ok" | "warn" | "error";

export interface PlatformHealthComponent {
  name: string;
  status: PlatformHealthStatus;
  detail: string;
  workers?: string[];
  last_tick?: string;
}

export interface PlatformHealthResponse {
  status: "ok" | "degraded";
  components: PlatformHealthComponent[];
}

export const platformHealthApi = {
  get: () =>
    // Endpoint lives at root (outside /api/v1) so strip the prefix.
    api
      .get<PlatformHealthResponse>("/health/platform", { baseURL: "/" })
      .then((r) => r.data),
};

export const dockerApi = {
  listHosts: () => api.get<DockerHost[]>("/docker/hosts").then((r) => r.data),
  createHost: (data: DockerHostCreate) =>
    api.post<DockerHost>("/docker/hosts", data).then((r) => r.data),
  updateHost: (id: string, data: DockerHostUpdate) =>
    api.put<DockerHost>(`/docker/hosts/${id}`, data).then((r) => r.data),
  deleteHost: (id: string) => api.delete(`/docker/hosts/${id}`),
  testConnection: (body: {
    host_id?: string;
    connection_type?: "unix" | "tcp";
    endpoint?: string;
    ca_bundle_pem?: string;
    client_cert_pem?: string;
    client_key_pem?: string;
  }) =>
    api.post<DockerTestResult>("/docker/hosts/test", body).then((r) => r.data),
  syncNow: (id: string) =>
    api
      .post<{ status: string; task_id: string }>(`/docker/hosts/${id}/sync`)
      .then((r) => r.data),
};

// ── Proxmox VE integration ─────────────────────────────────────────

export interface ProxmoxDiscoveryGuest {
  kind: "qemu" | "lxc";
  vmid: number;
  name: string;
  node: string;
  status: string;
  nic_count: number;
  bridges: string[];
  ips_mirrored: number;
  ips_from_agent: number;
  ips_from_static: number;
  // "reporting" = agent on + returned IPs; "not_responding" = agent on
  // but no response; "off" = agent flag 0; "n/a" = LXC (no agent concept).
  agent_state: "reporting" | "not_responding" | "off" | "n/a";
  // Single top-level reason code for filtering in the UI. ``null`` ==
  // "everything's fine, guest is mirroring IPs into IPAM".
  issue:
    | null
    | "agent_not_responding"
    | "agent_off"
    | "no_ip"
    | "no_nic"
    | "static_only";
  // Operator-facing hint for fixing the issue. ``null`` when there's
  // nothing to fix.
  hint: string | null;
}

export interface ProxmoxDiscoverySummary {
  vm_total: number;
  vm_agent_reporting: number;
  vm_agent_not_responding: number;
  vm_agent_off: number;
  vm_no_nic: number;
  lxc_total: number;
  lxc_reporting: number;
  lxc_no_ip: number;
  sdn_vnets_total: number;
  sdn_vnets_with_subnet: number;
  sdn_vnets_unresolved: number;
  addresses_skipped_no_subnet: number;
  desired_subnets: number;
}

export interface ProxmoxDiscovery {
  summary: ProxmoxDiscoverySummary;
  guests: ProxmoxDiscoveryGuest[];
  generated_at: string;
}

export interface ProxmoxNode {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  host: string;
  port: number;
  verify_tls: boolean;
  ca_bundle_present: boolean;
  token_id: string;
  token_secret_present: boolean;
  ipam_space_id: string;
  dns_group_id: string | null;
  mirror_vms: boolean;
  mirror_lxc: boolean;
  include_stopped: boolean;
  infer_vnet_subnets: boolean;
  sync_interval_seconds: number;
  last_synced_at: string | null;
  last_sync_error: string | null;
  pve_version: string | null;
  cluster_name: string | null;
  node_count: number | null;
  last_discovery: ProxmoxDiscovery | null;
  created_at: string;
  modified_at: string;
}

export interface ProxmoxNodeCreate {
  name: string;
  description?: string;
  enabled?: boolean;
  host: string;
  port?: number;
  verify_tls?: boolean;
  ca_bundle_pem?: string;
  token_id: string;
  token_secret: string;
  ipam_space_id: string;
  dns_group_id?: string | null;
  mirror_vms?: boolean;
  mirror_lxc?: boolean;
  include_stopped?: boolean;
  infer_vnet_subnets?: boolean;
  sync_interval_seconds?: number;
}

export interface ProxmoxNodeUpdate {
  name?: string;
  description?: string;
  enabled?: boolean;
  host?: string;
  port?: number;
  verify_tls?: boolean;
  ca_bundle_pem?: string;
  token_id?: string;
  token_secret?: string;
  ipam_space_id?: string;
  dns_group_id?: string | null;
  mirror_vms?: boolean;
  mirror_lxc?: boolean;
  include_stopped?: boolean;
  infer_vnet_subnets?: boolean;
  sync_interval_seconds?: number;
}

export interface ProxmoxTestResult {
  ok: boolean;
  message: string;
  pve_version: string | null;
  cluster_name: string | null;
  node_count: number | null;
}

export const proxmoxApi = {
  listNodes: () => api.get<ProxmoxNode[]>("/proxmox/nodes").then((r) => r.data),
  createNode: (data: ProxmoxNodeCreate) =>
    api.post<ProxmoxNode>("/proxmox/nodes", data).then((r) => r.data),
  updateNode: (id: string, data: ProxmoxNodeUpdate) =>
    api.put<ProxmoxNode>(`/proxmox/nodes/${id}`, data).then((r) => r.data),
  deleteNode: (id: string) => api.delete(`/proxmox/nodes/${id}`),
  testConnection: (body: {
    node_id?: string;
    host?: string;
    port?: number;
    verify_tls?: boolean;
    ca_bundle_pem?: string;
    token_id?: string;
    token_secret?: string;
  }) =>
    api
      .post<ProxmoxTestResult>("/proxmox/nodes/test", body)
      .then((r) => r.data),
  syncNow: (id: string) =>
    api
      .post<{ status: string; task_id: string }>(`/proxmox/nodes/${id}/sync`)
      .then((r) => r.data),
};

// ── Tailscale integration ──────────────────────────────────────────

export interface TailscaleTenant {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  tailnet: string;
  api_key_present: boolean;
  ipam_space_id: string;
  dns_group_id: string | null;
  cgnat_cidr: string;
  ipv6_cidr: string;
  skip_expired: boolean;
  sync_interval_seconds: number;
  last_synced_at: string | null;
  last_sync_error: string | null;
  tailnet_domain: string | null;
  device_count: number | null;
  created_at: string;
  modified_at: string;
}

export interface TailscaleTenantCreate {
  name: string;
  description?: string;
  enabled?: boolean;
  tailnet?: string;
  api_key: string;
  ipam_space_id: string;
  dns_group_id?: string | null;
  cgnat_cidr?: string;
  ipv6_cidr?: string;
  skip_expired?: boolean;
  sync_interval_seconds?: number;
}

export interface TailscaleTenantUpdate {
  name?: string;
  description?: string;
  enabled?: boolean;
  tailnet?: string;
  api_key?: string;
  ipam_space_id?: string;
  dns_group_id?: string | null;
  cgnat_cidr?: string;
  ipv6_cidr?: string;
  skip_expired?: boolean;
  sync_interval_seconds?: number;
}

export interface TailscaleTestResult {
  ok: boolean;
  message: string;
  tailnet_domain: string | null;
  device_count: number | null;
}

export const tailscaleApi = {
  listTenants: () =>
    api.get<TailscaleTenant[]>("/tailscale/tenants").then((r) => r.data),
  createTenant: (data: TailscaleTenantCreate) =>
    api.post<TailscaleTenant>("/tailscale/tenants", data).then((r) => r.data),
  updateTenant: (id: string, data: TailscaleTenantUpdate) =>
    api
      .put<TailscaleTenant>(`/tailscale/tenants/${id}`, data)
      .then((r) => r.data),
  deleteTenant: (id: string) => api.delete(`/tailscale/tenants/${id}`),
  testConnection: (body: {
    tenant_id?: string;
    tailnet?: string;
    api_key?: string;
  }) =>
    api
      .post<TailscaleTestResult>("/tailscale/tenants/test", body)
      .then((r) => r.data),
  syncNow: (id: string) =>
    api
      .post<{
        status: string;
        task_id: string;
      }>(`/tailscale/tenants/${id}/sync`)
      .then((r) => r.data),
};

// ── Trash (soft-delete recovery) ────────────────────────────────────────────

export type TrashEntryType =
  | "ip_space"
  | "ip_block"
  | "subnet"
  | "dns_zone"
  | "dns_record"
  | "dhcp_scope";

export interface TrashEntry {
  id: string;
  type: TrashEntryType;
  name_or_cidr: string;
  deleted_at: string;
  deleted_by_user_id: string | null;
  deleted_by_username: string | null;
  deletion_batch_id: string | null;
  batch_size: number;
}

export interface TrashListResponse {
  items: TrashEntry[];
  total: number;
}

export interface TrashRestoreResponse {
  batch_id: string;
  restored: number;
}

export const trashApi = {
  list: (
    params: {
      type?: TrashEntryType;
      since?: string;
      until?: string;
      q?: string;
      limit?: number;
      offset?: number;
    } = {},
  ) =>
    api.get<TrashListResponse>("/admin/trash", { params }).then((r) => r.data),
  restore: (type: TrashEntryType, id: string) =>
    api
      .post<TrashRestoreResponse>(`/admin/trash/${type}/${id}/restore`)
      .then((r) => r.data),
  permanentDelete: (type: TrashEntryType, id: string) =>
    api.delete(`/admin/trash/${type}/${id}`),
};

// ── DHCP lease history ─────────────────────────────────────────────────────────

export interface DHCPLeaseHistoryRow {
  id: string;
  server_id: string;
  scope_id: string | null;
  ip_address: string;
  mac_address: string;
  hostname: string | null;
  client_id: string | null;
  started_at: string | null;
  expired_at: string;
  // "expired" | "released" | "removed" | "superseded"
  lease_state: string;
  created_at: string;
}

export interface DHCPLeaseHistoryPage {
  total: number;
  page: number;
  per_page: number;
  items: DHCPLeaseHistoryRow[];
}

export interface DHCPLeaseHistoryQuery {
  since?: string;
  until?: string;
  mac?: string;
  ip?: string;
  hostname?: string;
  lease_state?: string;
  page?: number;
  per_page?: number;
}

export const dhcpLeaseHistoryApi = {
  list: (serverId: string, params?: DHCPLeaseHistoryQuery) =>
    api
      .get<DHCPLeaseHistoryPage>(`/dhcp/servers/${serverId}/lease-history`, {
        params,
      })
      .then((r) => r.data),
};

// ── NAT mappings ───────────────────────────────────────────────────────────────

export type NATKind = "1to1" | "pat" | "hide";
export type NATProtocol = "tcp" | "udp" | "any";

export interface NATMapping {
  id: string;
  name: string;
  kind: NATKind;
  internal_ip: string | null;
  internal_ip_address_id: string | null;
  internal_subnet_id: string | null;
  // Display labels for the internal subnet (populated server-side on the
  // hide-NAT path); without these the UI can only show the bare UUID.
  internal_subnet_cidr: string | null;
  internal_subnet_name: string | null;
  internal_port_start: number | null;
  internal_port_end: number | null;
  external_ip: string | null;
  external_ip_address_id: string | null;
  external_port_start: number | null;
  external_port_end: number | null;
  protocol: NATProtocol;
  device_label: string | null;
  description: string | null;
  tags: unknown[];
  custom_fields: Record<string, unknown>;
  created_at: string;
  modified_at: string;
}

export interface NATMappingPage {
  total: number;
  page: number;
  per_page: number;
  items: NATMapping[];
}

export interface NATMappingWrite {
  name?: string;
  kind?: NATKind;
  internal_ip?: string | null;
  internal_subnet_id?: string | null;
  internal_port_start?: number | null;
  internal_port_end?: number | null;
  external_ip?: string | null;
  external_port_start?: number | null;
  external_port_end?: number | null;
  protocol?: NATProtocol;
  device_label?: string | null;
  description?: string | null;
  tags?: unknown[];
  custom_fields?: Record<string, unknown>;
}

export interface NATMappingQuery {
  kind?: NATKind;
  internal_ip?: string;
  external_ip?: string;
  q?: string;
  page?: number;
  per_page?: number;
}

// ── Postgres insights + container stats ──────────────────────────────────────

export interface PostgresOverview {
  version: string;
  db_size_bytes: number;
  cache_hit_ratio: number | null;
  wal_bytes: number | null;
  active_connections: number;
  max_connections: number;
  longest_transaction: {
    pid: number;
    state: string | null;
    age_seconds: number;
    query: string | null;
    application_name: string | null;
    client_addr: string | null;
  } | null;
}

export interface PostgresTableSize {
  schema_name: string;
  table_name: string;
  total_bytes: number;
  table_bytes: number;
  index_bytes: number;
  toast_bytes: number;
  live_rows: number;
  dead_rows: number;
  last_autovacuum: string | null;
  last_autoanalyze: string | null;
}

export interface PostgresConnection {
  state: string;
  count: number;
}

export interface PostgresSlowQuery {
  query: string;
  calls: number;
  total_time_ms: number;
  mean_time_ms: number;
  rows: number;
}

export interface PostgresSlowQueriesResponse {
  available: boolean;
  hint: string | null;
  rows: PostgresSlowQuery[];
}

export const postgresApi = {
  overview: () =>
    api.get<PostgresOverview>("/admin/postgres/overview").then((r) => r.data),
  tables: (limit = 50) =>
    api
      .get<{ rows: PostgresTableSize[] }>("/admin/postgres/tables", {
        params: { limit },
      })
      .then((r) => r.data.rows),
  connections: () =>
    api
      .get<{ rows: PostgresConnection[] }>("/admin/postgres/connections")
      .then((r) => r.data.rows),
  slowQueries: (limit = 20) =>
    api
      .get<PostgresSlowQueriesResponse>("/admin/postgres/slow-queries", {
        params: { limit },
      })
      .then((r) => r.data),
};

export interface ContainerStat {
  id: string;
  name: string;
  image: string;
  state: string;
  started_at: string | null;
  cpu_percent: number | null;
  memory_bytes: number | null;
  memory_limit_bytes: number | null;
  memory_percent: number | null;
  network_rx_bytes: number | null;
  network_tx_bytes: number | null;
  block_read_bytes: number | null;
  block_write_bytes: number | null;
}

export interface ContainerStatsResponse {
  available: boolean;
  hint: string | null;
  rows: ContainerStat[];
}

export const containersApi = {
  stats: (params: { prefix?: string; include_stopped?: boolean } = {}) =>
    api
      .get<ContainerStatsResponse>("/admin/containers/stats", { params })
      .then((r) => r.data),
};

export const natApi = {
  list: (params?: NATMappingQuery) =>
    api
      .get<NATMappingPage>("/ipam/nat-mappings", { params })
      .then((r) => r.data),
  get: (id: string) =>
    api.get<NATMapping>(`/ipam/nat-mappings/${id}`).then((r) => r.data),
  create: (data: NATMappingWrite) =>
    api.post<NATMapping>("/ipam/nat-mappings", data).then((r) => r.data),
  update: (id: string, data: NATMappingWrite) =>
    api.patch<NATMapping>(`/ipam/nat-mappings/${id}`, data).then((r) => r.data),
  delete: (id: string) => api.delete(`/ipam/nat-mappings/${id}`),
  byIp: (ipId: string) =>
    api
      .get<NATMapping[]>(`/ipam/nat-mappings/by-ip/${ipId}`)
      .then((r) => r.data),
  bySubnet: (subnetId: string) =>
    api
      .get<NATMapping[]>(`/ipam/nat-mappings/by-subnet/${subnetId}`)
      .then((r) => r.data),
};

// ── Network discovery (SNMP-based router/switch/AP polling) ────────────

export type NetworkDeviceType =
  | "router"
  | "switch"
  | "ap"
  | "firewall"
  | "l3_switch"
  | "other";

export type NetworkSnmpVersion = "v1" | "v2c" | "v3";

export type NetworkV3SecurityLevel = "noAuthNoPriv" | "authNoPriv" | "authPriv";

export type NetworkV3AuthProtocol =
  | "MD5"
  | "SHA"
  | "SHA224"
  | "SHA256"
  | "SHA384"
  | "SHA512";

export type NetworkV3PrivProtocol =
  | "DES"
  | "3DES"
  | "AES128"
  | "AES192"
  | "AES256";

export type NetworkPollStatus =
  | "pending"
  | "success"
  | "partial"
  | "failed"
  | "timeout";

export interface NetworkDeviceRead {
  id: string;
  name: string;
  hostname: string;
  ip_address: string;
  device_type: NetworkDeviceType;
  description: string | null;
  vendor: string | null;
  sys_descr: string | null;
  sys_object_id: string | null;
  sys_name: string | null;
  sys_uptime_seconds: number | null;
  snmp_version: NetworkSnmpVersion;
  snmp_port: number;
  snmp_timeout_seconds: number;
  snmp_retries: number;
  has_community: boolean;
  v3_security_name: string | null;
  v3_security_level: NetworkV3SecurityLevel | null;
  v3_auth_protocol: NetworkV3AuthProtocol | null;
  has_auth_key: boolean;
  v3_priv_protocol: NetworkV3PrivProtocol | null;
  has_priv_key: boolean;
  v3_context_name: string | null;
  poll_interval_seconds: number;
  poll_arp: boolean;
  poll_fdb: boolean;
  poll_interfaces: boolean;
  poll_lldp: boolean;
  auto_create_discovered: boolean;
  last_poll_at: string | null;
  next_poll_at: string | null;
  last_poll_status: NetworkPollStatus;
  last_poll_error: string | null;
  last_poll_arp_count: number | null;
  last_poll_fdb_count: number | null;
  last_poll_interface_count: number | null;
  last_poll_neighbour_count: number | null;
  ip_space_id: string;
  ip_space_name: string;
  is_active: boolean;
  tags: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface NetworkDeviceListResponse {
  items: NetworkDeviceRead[];
  total: number;
  page: number;
  page_size: number;
}

export interface NetworkDeviceListQuery {
  active?: boolean;
  device_type?: NetworkDeviceType;
  last_poll_status?: NetworkPollStatus;
  page?: number;
  page_size?: number;
}

export interface NetworkDeviceCreate {
  name: string;
  hostname: string;
  ip_address: string;
  device_type?: NetworkDeviceType;
  description?: string | null;
  snmp_version?: NetworkSnmpVersion;
  snmp_port?: number;
  snmp_timeout_seconds?: number;
  snmp_retries?: number;
  community?: string;
  v3_security_name?: string;
  v3_security_level?: NetworkV3SecurityLevel;
  v3_auth_protocol?: NetworkV3AuthProtocol;
  v3_auth_key?: string;
  v3_priv_protocol?: NetworkV3PrivProtocol;
  v3_priv_key?: string;
  v3_context_name?: string;
  poll_interval_seconds?: number;
  poll_arp?: boolean;
  poll_fdb?: boolean;
  poll_interfaces?: boolean;
  poll_lldp?: boolean;
  auto_create_discovered?: boolean;
  ip_space_id: string;
  is_active?: boolean;
  tags?: Record<string, unknown>;
}

export type NetworkDeviceUpdate = Partial<NetworkDeviceCreate>;

export interface NetworkTestConnectionResult {
  success: boolean;
  sys_descr: string | null;
  sys_object_id: string | null;
  sys_name: string | null;
  vendor: string | null;
  error_kind:
    | "timeout"
    | "auth_failure"
    | "no_response"
    | "transport_error"
    | "internal"
    | null;
  error_message: string | null;
  elapsed_ms: number;
}

export interface NetworkPollNowResponse {
  task_id: string;
  queued_at: string;
}

export interface NetworkInterfaceRead {
  id: string;
  device_id: string;
  if_index: number;
  name: string;
  alias: string | null;
  description: string | null;
  speed_bps: number | null;
  mac_address: string | null;
  admin_status: "up" | "down" | "testing" | null;
  oper_status:
    | "up"
    | "down"
    | "testing"
    | "unknown"
    | "dormant"
    | "notPresent"
    | "lowerLayerDown"
    | null;
  last_change_seconds: number | null;
  created_at: string;
  updated_at: string;
}

export interface NetworkInterfaceListResponse {
  items: NetworkInterfaceRead[];
  total: number;
  page: number;
  page_size: number;
}

export interface NetworkArpEntryRead {
  id: string;
  device_id: string;
  interface_id: string | null;
  interface_name: string | null;
  ip_address: string;
  mac_address: string;
  vrf_name: string | null;
  address_type: "ipv4" | "ipv6";
  state: "reachable" | "stale" | "delay" | "probe" | "invalid" | "unknown";
  first_seen: string;
  last_seen: string;
}

export interface NetworkArpListResponse {
  items: NetworkArpEntryRead[];
  total: number;
  page: number;
  page_size: number;
}

export interface NetworkArpQuery {
  ip?: string;
  mac?: string;
  vrf?: string;
  state?: NetworkArpEntryRead["state"];
  page?: number;
  page_size?: number;
}

export interface NetworkFdbEntryRead {
  id: string;
  device_id: string;
  interface_id: string;
  interface_name: string;
  mac_address: string;
  vlan_id: number | null;
  fdb_type: "learned" | "static" | "mgmt" | "other";
  first_seen: string;
  last_seen: string;
}

export interface NetworkFdbListResponse {
  items: NetworkFdbEntryRead[];
  total: number;
  page: number;
  page_size: number;
}

export interface NetworkFdbQuery {
  mac?: string;
  vlan_id?: number;
  interface_id?: string;
  page?: number;
  page_size?: number;
}

export interface NetworkContextEntry {
  device_id: string;
  device_name: string;
  interface_id: string;
  interface_name: string;
  interface_alias: string | null;
  vlan_id: number | null;
  mac_address: string;
  fdb_type: string;
  last_seen: string;
}

// LLDP-MIB chassis-id / port-id subtype enums kept in sync with the
// backend poller. Used by the Neighbours tab to render the right
// label next to opaque IDs (e.g. "MAC" vs "interfaceName").
export const LLDP_CHASSIS_ID_SUBTYPES: Record<number, string> = {
  1: "chassisComponent",
  2: "interfaceAlias",
  3: "portComponent",
  4: "macAddress",
  5: "networkAddress",
  6: "interfaceName",
  7: "local",
};
export const LLDP_PORT_ID_SUBTYPES: Record<number, string> = {
  1: "interfaceAlias",
  2: "portComponent",
  3: "macAddress",
  4: "networkAddress",
  5: "interfaceName",
  6: "agentCircuitId",
  7: "local",
};

export interface NetworkNeighbourRead {
  id: string;
  device_id: string;
  interface_id: string | null;
  interface_name: string | null;
  local_port_num: number;
  remote_chassis_id_subtype: number;
  remote_chassis_id: string;
  remote_port_id_subtype: number;
  remote_port_id: string;
  remote_port_desc: string | null;
  remote_sys_name: string | null;
  remote_sys_desc: string | null;
  remote_sys_cap_enabled: number | null;
  first_seen: string;
  last_seen: string;
}

export interface NetworkNeighbourListResponse {
  items: NetworkNeighbourRead[];
  total: number;
  page: number;
  page_size: number;
}

export interface NetworkNeighbourQuery {
  sys_name?: string;
  chassis_id?: string;
  interface_id?: string;
  page?: number;
  page_size?: number;
}

export const networkApi = {
  listDevices: (params?: NetworkDeviceListQuery) =>
    api
      .get<NetworkDeviceListResponse>("/network-devices", { params })
      .then((r) => r.data),
  getDevice: (id: string) =>
    api.get<NetworkDeviceRead>(`/network-devices/${id}`).then((r) => r.data),
  createDevice: (data: NetworkDeviceCreate) =>
    api.post<NetworkDeviceRead>("/network-devices", data).then((r) => r.data),
  updateDevice: (id: string, data: NetworkDeviceUpdate) =>
    api
      .patch<NetworkDeviceRead>(`/network-devices/${id}`, data)
      .then((r) => r.data),
  deleteDevice: (id: string) => api.delete(`/network-devices/${id}`),
  testConnection: (id: string) =>
    api
      .post<NetworkTestConnectionResult>(`/network-devices/${id}/test`)
      .then((r) => r.data),
  pollNow: (id: string) =>
    api
      .post<NetworkPollNowResponse>(`/network-devices/${id}/poll-now`)
      .then((r) => r.data),
  listInterfaces: (
    deviceId: string,
    params?: { page?: number; page_size?: number },
  ) =>
    api
      .get<NetworkInterfaceListResponse>(
        `/network-devices/${deviceId}/interfaces`,
        { params },
      )
      .then((r) => r.data),
  listArp: (deviceId: string, params?: NetworkArpQuery) =>
    api
      .get<NetworkArpListResponse>(`/network-devices/${deviceId}/arp`, {
        params,
      })
      .then((r) => r.data),
  listFdb: (deviceId: string, params?: NetworkFdbQuery) =>
    api
      .get<NetworkFdbListResponse>(`/network-devices/${deviceId}/fdb`, {
        params,
      })
      .then((r) => r.data),
  listNeighbours: (deviceId: string, params?: NetworkNeighbourQuery) =>
    api
      .get<NetworkNeighbourListResponse>(
        `/network-devices/${deviceId}/neighbours`,
        { params },
      )
      .then((r) => r.data),
  // Mounted under /ipam/addresses/{address_id}/network-context but exposed
  // here so all network-discovery client wrappers live in one place.
  getAddressNetworkContext: (addressId: string) =>
    api
      .get<
        NetworkContextEntry[]
      >(`/ipam/addresses/${addressId}/network-context`)
      .then((r) => r.data),
  // Batched: one round-trip per subnet, returns {ip_id: [entries...]}.
  // Drives the "Network" column on the IPAM IP listing without an
  // N+1 fan-out of per-IP requests.
  getSubnetNetworkContext: (subnetId: string) =>
    api
      .get<
        Record<string, NetworkContextEntry[]>
      >(`/ipam/subnets/${subnetId}/network-context`)
      .then((r) => r.data),
};

// ── Nmap on-demand scans ──────────────────────────────────────────────

export type NmapPreset =
  | "quick"
  | "service_version"
  | "os_fingerprint"
  | "service_and_os"
  | "subnet_sweep"
  | "default_scripts"
  | "udp_top100"
  | "aggressive"
  | "custom";

export type NmapScanStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface NmapPortResult {
  port: number;
  proto: string;
  state: string;
  reason: string | null;
  service: string | null;
  product: string | null;
  version: string | null;
  extrainfo: string | null;
}

export interface NmapOsResult {
  name: string | null;
  accuracy: number | null;
}

export interface NmapHostResult {
  address: string | null;
  hostname: string | null;
  host_state: string;
  ports: NmapPortResult[];
  os: NmapOsResult | null;
}

export interface NmapSummary {
  host_state: string;
  ports: NmapPortResult[];
  os: NmapOsResult | null;
  /** Populated when the scan target was a CIDR (or any target nmap
   *  expanded to multiple hosts). The single-host fields above mirror
   *  the first entry. */
  hosts: NmapHostResult[] | null;
}

export interface NmapScanRead {
  id: string;
  target_ip: string;
  ip_address_id: string | null;
  preset: NmapPreset;
  port_spec: string | null;
  extra_args: string | null;
  status: NmapScanStatus;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  exit_code: number | null;
  command_line: string | null;
  error_message: string | null;
  summary: NmapSummary | null;
  raw_xml: string | null;
  raw_stdout: string | null;
  created_by_user_id: string | null;
  created_at: string;
  modified_at: string;
}

export interface NmapScanCreate {
  target_ip: string;
  preset: NmapPreset;
  port_spec?: string | null;
  extra_args?: string | null;
  ip_address_id?: string | null;
}

export interface NmapScanListResponse {
  items: NmapScanRead[];
  total: number;
  page: number;
  page_size: number;
}

export interface NmapScanListQuery {
  ip_address_id?: string;
  target_ip?: string;
  status?: NmapScanStatus;
  page?: number;
  page_size?: number;
}

export const nmapApi = {
  listScans: (params?: NmapScanListQuery) =>
    api
      .get<NmapScanListResponse>("/nmap/scans", { params })
      .then((r) => r.data),
  getScan: (id: string) =>
    api.get<NmapScanRead>(`/nmap/scans/${id}`).then((r) => r.data),
  createScan: (body: NmapScanCreate) =>
    api.post<NmapScanRead>("/nmap/scans", body).then((r) => r.data),
  cancelScan: (id: string) => api.delete(`/nmap/scans/${id}`),
  /** Bulk-delete scans. Server returns ``{deleted, cancelled}`` —
   *  queued/running scans get cancelled, terminal scans are removed.
   *  Capped at 500 scan ids per call. */
  bulkDeleteScans: (scanIds: string[]) =>
    api
      .post<{ deleted: number; cancelled: number }>("/nmap/scans/bulk-delete", {
        scan_ids: scanIds,
      })
      .then((r) => r.data),
  /** Stamp every alive host from a multi-host (CIDR) scan into IPAM.
   *  Existing rows in ``available`` / ``discovered`` get bumped to
   *  ``discovered``; integration / operator-owned rows get a
   *  ``last_seen`` stamp only. New rows land as ``discovered``.
   *  Returns counters for the UI to render. */
  stampDiscovered: (scanId: string) =>
    api
      .post<{
        created: number;
        bumped: number;
        refreshed: number;
        skipped_no_subnet: number;
        skipped_addresses: string[];
      }>(`/nmap/scans/${scanId}/stamp-discovered`)
      .then((r) => r.data),
  // The SSE stream isn't fetched via axios — callers use `EventSource`
  // pointed at the URL returned here. Auth piggybacks on a query
  // token because EventSource can't set Authorization headers.
  streamUrl: (id: string) => {
    const token = localStorage.getItem("access_token") ?? "";
    const base = API_BASE.replace(/\/$/, "");
    return `${base}/nmap/scans/${id}/stream?token=${encodeURIComponent(token)}`;
  },
};

// ── ASN management ──────────────────────────────────────────────────

export type ASNKind = "public" | "private";
export type ASNRegistry =
  | "arin"
  | "ripe"
  | "apnic"
  | "lacnic"
  | "afrinic"
  | "unknown";
export type ASNWhoisState = "ok" | "drift" | "unreachable" | "n/a";

export interface ASNRead {
  id: string;
  number: number;
  name: string;
  description: string;
  kind: ASNKind;
  holder_org: string | null;
  registry: ASNRegistry;
  whois_last_checked_at: string | null;
  whois_data: Record<string, unknown> | null;
  whois_state: ASNWhoisState;
  tags: Record<string, unknown>;
  custom_fields: Record<string, unknown>;
  created_at: string;
  modified_at: string;
}

// Mirrors the canonical set written by ``app.tasks.rpki_roa_refresh``:
// ``valid`` (under 30 days from expiry — also the default when
// validity is unknown), ``expiring_soon`` (<30 days), ``expired``,
// ``not_found`` (the trust anchor stopped emitting the ROA).
export type ASNRpkiRoaState =
  | "valid"
  | "expiring_soon"
  | "expired"
  | "not_found";

export interface ASNRpkiRoa {
  id: string;
  asn_id: string;
  prefix: string;
  max_length: number;
  valid_from: string | null;
  valid_to: string | null;
  trust_anchor: string;
  state: ASNRpkiRoaState;
  last_checked_at: string | null;
}

export interface ASNListResponse {
  items: ASNRead[];
  total: number;
  limit: number;
  offset: number;
}

export interface ASNListQuery {
  limit?: number;
  offset?: number;
  kind?: ASNKind;
  registry?: ASNRegistry;
  whois_state?: ASNWhoisState;
  search?: string;
}

export interface ASNCreate {
  number: number;
  name?: string;
  description?: string;
  holder_org?: string | null;
  tags?: Record<string, unknown>;
  custom_fields?: Record<string, unknown>;
}

export interface ASNUpdate {
  name?: string;
  description?: string;
  holder_org?: string | null;
  tags?: Record<string, unknown>;
  custom_fields?: Record<string, unknown>;
}

export type BGPRelationshipType = "peer" | "customer" | "provider" | "sibling";

export interface BGPPeering {
  id: string;
  local_asn_id: string;
  peer_asn_id: string;
  relationship_type: BGPRelationshipType;
  description: string;
  local_asn_number: number;
  local_asn_name: string;
  peer_asn_number: number;
  peer_asn_name: string;
  created_at: string;
  modified_at: string;
}

export interface BGPPeeringCreate {
  local_asn_id: string;
  peer_asn_id: string;
  relationship_type: BGPRelationshipType;
  description?: string;
}

export interface BGPPeeringUpdate {
  relationship_type?: BGPRelationshipType;
  description?: string;
}

export const asnsApi = {
  list: (params?: ASNListQuery) =>
    api.get<ASNListResponse>("/asns", { params }).then((r) => r.data),
  get: (id: string) => api.get<ASNRead>(`/asns/${id}`).then((r) => r.data),
  create: (data: ASNCreate) =>
    api.post<ASNRead>("/asns", data).then((r) => r.data),
  update: (id: string, data: ASNUpdate) =>
    api.put<ASNRead>(`/asns/${id}`, data).then((r) => r.data),
  remove: (id: string) => api.delete(`/asns/${id}`),
  refreshWhois: (id: string) =>
    api.post<ASNRead>(`/asns/${id}/refresh-whois`).then((r) => r.data),
  refreshRpki: (id: string) =>
    api
      .post<{
        asn_id: string;
        asn_number: number;
        added: number;
        updated: number;
        removed: number;
        transitions: number;
      }>(`/asns/${id}/refresh-rpki`)
      .then((r) => r.data),
  getRpkiRoas: (id: string) =>
    api.get<ASNRpkiRoa[]>(`/asns/${id}/rpki-roas`).then((r) => r.data),
  bulkDelete: (ids: string[]) =>
    api
      .post<{ deleted: number; not_found: string[] }>("/asns/bulk-delete", {
        ids,
      })
      .then((r) => r.data),
  // BGP peering — operator-curated graph of peering relationships.
  listPeerings: (params?: {
    asn_id?: string;
    relationship_type?: BGPRelationshipType;
  }) => api.get<BGPPeering[]>("/asns/peerings", { params }).then((r) => r.data),
  createPeering: (data: BGPPeeringCreate) =>
    api.post<BGPPeering>("/asns/peerings", data).then((r) => r.data),
  updatePeering: (id: string, data: BGPPeeringUpdate) =>
    api.patch<BGPPeering>(`/asns/peerings/${id}`, data).then((r) => r.data),
  deletePeering: (id: string) => api.delete(`/asns/peerings/${id}`),

  // BGP communities catalog (issue #88).
  listStandardCommunities: () =>
    api.get<BGPCommunity[]>("/asns/communities/standard").then((r) => r.data),
  listCommunities: (asnId: string) =>
    api.get<BGPCommunity[]>(`/asns/${asnId}/communities`).then((r) => r.data),
  createCommunity: (asnId: string, data: BGPCommunityCreate) =>
    api
      .post<BGPCommunity>(`/asns/${asnId}/communities`, data)
      .then((r) => r.data),
  updateCommunity: (id: string, data: BGPCommunityUpdate) =>
    api
      .patch<BGPCommunity>(`/asns/communities/${id}`, data)
      .then((r) => r.data),
  deleteCommunity: (id: string) => api.delete(`/asns/communities/${id}`),
};

// ── BGP communities ────────────────────────────────────────────────

export type BGPCommunityKind = "standard" | "regular" | "large";

export interface BGPCommunity {
  id: string;
  asn_id: string | null;
  value: string;
  kind: BGPCommunityKind;
  name: string;
  description: string;
  inbound_action: string;
  outbound_action: string;
  tags: Record<string, unknown>;
  created_at: string;
  modified_at: string;
}

export interface BGPCommunityCreate {
  value: string;
  kind: BGPCommunityKind;
  name?: string;
  description?: string;
  inbound_action?: string;
  outbound_action?: string;
  tags?: Record<string, unknown>;
}

export interface BGPCommunityUpdate {
  name?: string;
  description?: string;
  inbound_action?: string;
  outbound_action?: string;
  tags?: Record<string, unknown>;
}
