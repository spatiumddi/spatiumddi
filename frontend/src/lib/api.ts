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

  // On 401, attempt token refresh once then redirect to login
  let isRefreshing = false;
  let refreshQueue: Array<(token: string) => void> = [];

  client.interceptors.response.use(
    (res) => res,
    async (err: AxiosError) => {
      const originalRequest = err.config as typeof err.config & {
        _retry?: boolean;
      };
      if (err.response?.status === 401 && !originalRequest?._retry) {
        const refreshToken = localStorage.getItem("refresh_token");
        if (!refreshToken) {
          localStorage.removeItem("access_token");
          window.location.href = "/login";
          return Promise.reject(err);
        }

        if (isRefreshing) {
          return new Promise((resolve) => {
            refreshQueue.push((token: string) => {
              if (originalRequest?.headers)
                originalRequest.headers.Authorization = `Bearer ${token}`;
              resolve(client(originalRequest!));
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
          refreshQueue.forEach((cb) => cb(access_token));
          refreshQueue = [];
          if (originalRequest?.headers)
            originalRequest.headers.Authorization = `Bearer ${access_token}`;
          return client(originalRequest!);
        } catch {
          localStorage.removeItem("access_token");
          localStorage.removeItem("refresh_token");
          window.location.href = "/login";
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

export interface IPSpace {
  id: string;
  name: string;
  description: string;
  is_default: boolean;
  tags: Record<string, unknown>;
  dns_group_ids: string[];
  dns_zone_id: string | null;
  dns_additional_zone_ids: string[];
  dhcp_server_group_id?: string | null;
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
  dhcp_server_group_id?: string | null;
  dhcp_inherit_settings?: boolean;
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
  dns_servers?: string[] | null;
  domain_name?: string | null;
  created_at?: string;
  modified_at?: string;
}

export interface IPAddress {
  id: string;
  subnet_id: string;
  address: string;
  status: string;
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
  created_at?: string;
  modified_at?: string;
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
  createBlock: (data: Partial<IPBlock>) =>
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
  }) => api.get<Subnet[]>("/ipam/subnets", { params }).then((r) => r.data),
  getSubnet: (id: string) =>
    api.get<Subnet>(`/ipam/subnets/${id}`).then((r) => r.data),
  createSubnet: (data: Partial<Subnet>) =>
    api.post<Subnet>("/ipam/subnets", data).then((r) => r.data),
  updateSubnet: (
    id: string,
    data: Partial<Subnet> & { manage_auto_addresses?: boolean },
  ) => api.put<Subnet>(`/ipam/subnets/${id}`, data).then((r) => r.data),
  deleteSubnet: (id: string) => api.delete(`/ipam/subnets/${id}`),

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
      aliases?: { name: string; record_type: "CNAME" | "A" }[];
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
  access_token: string;
  refresh_token: string;
  token_type: string;
  force_password_change: boolean;
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
  dhcp_pull_leases_interval_minutes: number;
  dhcp_pull_leases_last_run_at: string | null;
  ip_allocation_strategy: string;
  session_timeout_minutes: number;
  auto_logout_minutes: number;
  utilization_warn_threshold: number;
  utilization_critical_threshold: number;
  subnet_tree_default_expanded_depth: number;
  discovery_scan_enabled: boolean;
  discovery_scan_interval_minutes: number;
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
}

export const settingsApi = {
  get: () => api.get<PlatformSettings>("/settings").then((r) => r.data),
  update: (data: Partial<PlatformSettings>) =>
    api.put<PlatformSettings>("/settings", data).then((r) => r.data),
  getDefaults: () =>
    api
      .get<Partial<PlatformSettings>>("/settings/defaults")
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
  dnssec_enabled: boolean;
  last_serial: number;
  last_pushed_at: string | null;
  allow_query: string[] | null;
  allow_transfer: string[] | null;
  also_notify: string[] | null;
  notify_enabled: string | null;
  created_at: string;
  modified_at: string;
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

export const dnsApi = {
  // Server groups
  listGroups: () =>
    api.get<DNSServerGroup[]>("/dns/groups").then((r) => r.data),
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

export const dnsBlocklistApi = {
  list: () => api.get<DNSBlockList[]>("/dns/blocklists").then((r) => r.data),
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

export interface DHCPServerGroup {
  id: string;
  name: string;
  description: string;
  mode: string;
  created_at: string;
  modified_at: string;
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
  server_id: string | null;
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
  created_at: string;
  modified_at: string;
}

export const dhcpApi = {
  listGroups: () =>
    api.get<DHCPServerGroup[]>("/dhcp/server-groups").then((r) => r.data),
  getGroup: (id: string) =>
    api.get<DHCPServerGroup>(`/dhcp/server-groups/${id}`).then((r) => r.data),
  createGroup: (data: Partial<DHCPServerGroup>) =>
    api.post<DHCPServerGroup>("/dhcp/server-groups", data).then((r) => r.data),
  updateGroup: (id: string, data: Partial<DHCPServerGroup>) =>
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

  listClientClasses: (serverId: string) =>
    api
      .get<DHCPClientClass[]>(`/dhcp/servers/${serverId}/client-classes`)
      .then((r) => r.data),
  createClientClass: (serverId: string, data: Partial<DHCPClientClass>) =>
    api
      .post<DHCPClientClass>(`/dhcp/servers/${serverId}/client-classes`, data)
      .then((r) => r.data),
  updateClientClass: (
    _serverId: string,
    classId: string,
    data: Partial<DHCPClientClass>,
  ) =>
    api
      .put<DHCPClientClass>(`/dhcp/client-classes/${classId}`, data)
      .then((r) => r.data),
  deleteClientClass: (_serverId: string, classId: string) =>
    api.delete(`/dhcp/client-classes/${classId}`),
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
  server_id: string;
  name: string;
  description: string;
  match_expression: string;
  options: Record<string, unknown>;
  created_at: string;
  modified_at: string;
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

export const logsApi = {
  listSources: () => api.get<LogSource[]>("/logs/sources").then((r) => r.data),
  query: (body: LogQueryRequest) =>
    api.post<LogQueryResponse>("/logs/query", body).then((r) => r.data),
  dhcpAudit: (body: DhcpAuditRequest) =>
    api.post<DhcpAuditResponse>("/logs/dhcp-audit", body).then((r) => r.data),
};
