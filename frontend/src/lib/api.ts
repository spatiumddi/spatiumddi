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

export interface Subnet {
  id: string;
  space_id: string;
  block_id: string;
  network: string;
  name: string;
  description: string;
  vlan_id: number | null;
  vxlan_id: number | null;
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
}

export interface IPAddress {
  id: string;
  subnet_id: string;
  address: string;
  status: string;
  hostname: string | null;
  fqdn: string | null;
  description: string | null;
  mac_address: string | null;
  tags: Record<string, unknown>;
  custom_fields: Record<string, unknown>;
  // Linkage (§3) — populated by Wave 3 DDNS/DHCP integration.
  forward_zone_id?: string | null;
  reverse_zone_id?: string | null;
  dns_record_id?: string | null;
  dhcp_lease_id?: string | null;
  static_assignment_id?: string | null;
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

  listSubnets: (params?: { space_id?: string; block_id?: string }) =>
    api.get<Subnet[]>("/ipam/subnets", { params }).then((r) => r.data),
  getSubnet: (id: string) =>
    api.get<Subnet>(`/ipam/subnets/${id}`).then((r) => r.data),
  createSubnet: (data: Partial<Subnet>) =>
    api.post<Subnet>("/ipam/subnets", data).then((r) => r.data),
  updateSubnet: (
    id: string,
    data: Partial<Subnet> & { manage_auto_addresses?: boolean },
  ) => api.put<Subnet>(`/ipam/subnets/${id}`, data).then((r) => r.data),
  deleteSubnet: (id: string) => api.delete(`/ipam/subnets/${id}`),

  dnsSyncPreview: (subnetId: string) =>
    api
      .get<DnsSyncPreview>(`/ipam/subnets/${subnetId}/dns-sync/preview`)
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
    },
  ) =>
    api
      .post<IPAddress>(`/ipam/subnets/${data.subnet_id}/addresses`, data)
      .then((r) => r.data),
  updateAddress: (
    id: string,
    data: Partial<IPAddress> & { dns_zone_id?: string | null },
  ) => api.put<IPAddress>(`/ipam/addresses/${id}`, data).then((r) => r.data),
  deleteAddress: (id: string, permanent = false) =>
    api.delete(`/ipam/addresses/${id}`, {
      params: permanent ? { permanent: true } : undefined,
    }),
  nextAddress: (
    subnetId: string,
    data: {
      hostname: string;
      status?: string;
      mac_address?: string;
      description?: string;
      custom_fields?: Record<string, unknown>;
      dns_zone_id?: string | null;
    },
  ) =>
    api
      .post<IPAddress>(`/ipam/subnets/${subnetId}/next`, data)
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
    const date = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
    const filename = match ? match[1] : `ipam-export-${date}.${params.format}`;
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
}

export const settingsApi = {
  get: () => api.get<PlatformSettings>("/settings").then((r) => r.data),
  update: (data: Partial<PlatformSettings>) =>
    api.put<PlatformSettings>("/settings", data).then((r) => r.data),
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
  last_sync_at: string | null;
  last_health_check_at: string | null;
  notes: string;
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
    data: Partial<DNSServer> & { api_key?: string },
  ) =>
    api
      .post<DNSServer>(`/dns/groups/${groupId}/servers`, data)
      .then((r) => r.data),
  updateServer: (
    groupId: string,
    serverId: string,
    data: Partial<DNSServer> & { api_key?: string },
  ) =>
    api
      .put<DNSServer>(`/dns/groups/${groupId}/servers/${serverId}`, data)
      .then((r) => r.data),
  deleteServer: (groupId: string, serverId: string) =>
    api.delete(`/dns/groups/${groupId}/servers/${serverId}`),

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
      .then((r) => r.data),
  exportAllZones: (groupId: string, viewId?: string | null) =>
    api
      .get<Blob>(`/dns/groups/${groupId}/zones/export`, {
        params: viewId ? { view_id: viewId } : {},
        responseType: "blob",
      })
      .then((r) => r.data),
};

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
  deleteException: (id: string, exceptionId: string) =>
    api.delete(`/dns/blocklists/${id}/exceptions/${exceptionId}`),
};

// ── DHCP ───────────────────────────────────────────────────────────────────

export interface DHCPServerGroup {
  id: string;
  name: string;
  description: string;
  mode: string; // "load-balancing" | "hot-standby" | "standalone"
  created_at: string;
  modified_at: string;
}

export interface DHCPServer {
  id: string;
  group_id: string | null;
  name: string;
  driver: string; // "kea" | "isc" | "windows"
  host: string;
  port: number;
  api_port: number | null;
  status: string; // "active" | "syncing" | "unreachable" | "error" | "pending"
  last_sync_at: string | null;
  last_health_check_at: string | null;
  notes: string;
  approved: boolean;
  created_at: string;
  modified_at: string;
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
  hostname_sync_mode: string; // "none" | "ipam" | "learned"
  options: DHCPOption[];
  created_at: string;
  modified_at: string;
}

export interface DHCPPool {
  id: string;
  scope_id: string;
  name: string;
  start_ip: string;
  end_ip: string;
  pool_type: string; // "dynamic" | "excluded" | "reserved"
  client_class_id: string | null;
  lease_time_override: number | null;
  options: DHCPOption[];
  created_at: string;
  modified_at: string;
}

export interface DHCPStaticAssignment {
  id: string;
  scope_id: string;
  mac: string;
  ip: string;
  hostname: string;
  description: string;
  client_class_id: string | null;
  options: DHCPOption[];
  created_at: string;
  modified_at: string;
}

export interface DHCPClientClass {
  id: string;
  server_id: string;
  name: string;
  description: string;
  match_expression: string;
  options: DHCPOption[];
  created_at: string;
  modified_at: string;
}

export interface DHCPLease {
  ip: string;
  mac: string;
  hostname: string | null;
  state: string; // "active" | "expired" | "released" | "declined"
  expires_at: string | null;
  last_seen: string | null;
  subnet_id: string | null;
  scope_id: string | null;
  client_class: string | null;
}

export interface DHCPLeasePage {
  total: number;
  items: DHCPLease[];
}

export const dhcpApi = {
  // Server groups
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

  // Servers
  listServers: (groupId?: string) =>
    api
      .get<DHCPServer[]>("/dhcp/servers", {
        params: groupId ? { group_id: groupId } : undefined,
      })
      .then((r) => r.data),
  getServer: (id: string) =>
    api.get<DHCPServer>(`/dhcp/servers/${id}`).then((r) => r.data),
  createServer: (data: Partial<DHCPServer> & { api_key?: string }) =>
    api.post<DHCPServer>("/dhcp/servers", data).then((r) => r.data),
  updateServer: (
    id: string,
    data: Partial<DHCPServer> & { api_key?: string },
  ) => api.put<DHCPServer>(`/dhcp/servers/${id}`, data).then((r) => r.data),
  deleteServer: (id: string) => api.delete(`/dhcp/servers/${id}`),
  syncServer: (id: string) =>
    api.post<{ task_id: string | null; status: string }>(
      `/dhcp/servers/${id}/sync`,
    ).then((r) => r.data),
  approveServer: (id: string) =>
    api.post<DHCPServer>(`/dhcp/servers/${id}/approve`).then((r) => r.data),
  getLeases: (
    id: string,
    params?: {
      state?: string;
      subnet_id?: string;
      limit?: number;
      offset?: number;
    },
  ) =>
    api
      .get<DHCPLeasePage>(`/dhcp/servers/${id}/leases`, { params })
      .then((r) => r.data),

  // Scopes
  listScopesBySubnet: (subnetId: string) =>
    api
      .get<DHCPScope[]>(`/dhcp/subnets/${subnetId}/scopes`)
      .then((r) => r.data),
  getScope: (id: string) =>
    api.get<DHCPScope>(`/dhcp/scopes/${id}`).then((r) => r.data),
  createScope: (subnetId: string, data: Partial<DHCPScope>) =>
    api
      .post<DHCPScope>(`/dhcp/scopes/${subnetId}`, data)
      .then((r) => r.data),
  updateScope: (id: string, data: Partial<DHCPScope>) =>
    api.put<DHCPScope>(`/dhcp/scopes/${id}`, data).then((r) => r.data),
  deleteScope: (id: string) => api.delete(`/dhcp/scopes/${id}`),

  // Pools
  listPools: (scopeId: string) =>
    api.get<DHCPPool[]>(`/dhcp/scopes/${scopeId}/pools`).then((r) => r.data),
  createPool: (scopeId: string, data: Partial<DHCPPool>) =>
    api
      .post<DHCPPool>(`/dhcp/scopes/${scopeId}/pools`, data)
      .then((r) => r.data),
  updatePool: (scopeId: string, poolId: string, data: Partial<DHCPPool>) =>
    api
      .put<DHCPPool>(`/dhcp/scopes/${scopeId}/pools/${poolId}`, data)
      .then((r) => r.data),
  deletePool: (scopeId: string, poolId: string) =>
    api.delete(`/dhcp/scopes/${scopeId}/pools/${poolId}`),

  // Static assignments
  listStatics: (scopeId: string) =>
    api
      .get<DHCPStaticAssignment[]>(`/dhcp/scopes/${scopeId}/statics`)
      .then((r) => r.data),
  createStatic: (scopeId: string, data: Partial<DHCPStaticAssignment>) =>
    api
      .post<DHCPStaticAssignment>(`/dhcp/scopes/${scopeId}/statics`, data)
      .then((r) => r.data),
  updateStatic: (
    scopeId: string,
    staticId: string,
    data: Partial<DHCPStaticAssignment>,
  ) =>
    api
      .put<DHCPStaticAssignment>(
        `/dhcp/scopes/${scopeId}/statics/${staticId}`,
        data,
      )
      .then((r) => r.data),
  deleteStatic: (scopeId: string, staticId: string) =>
    api.delete(`/dhcp/scopes/${scopeId}/statics/${staticId}`),

  // Client classes
  listClientClasses: (serverId: string) =>
    api
      .get<DHCPClientClass[]>(`/dhcp/servers/${serverId}/client-classes`)
      .then((r) => r.data),
  createClientClass: (serverId: string, data: Partial<DHCPClientClass>) =>
    api
      .post<DHCPClientClass>(
        `/dhcp/servers/${serverId}/client-classes`,
        data,
      )
      .then((r) => r.data),
  updateClientClass: (
    serverId: string,
    classId: string,
    data: Partial<DHCPClientClass>,
  ) =>
    api
      .put<DHCPClientClass>(
        `/dhcp/servers/${serverId}/client-classes/${classId}`,
        data,
      )
      .then((r) => r.data),
  deleteClientClass: (serverId: string, classId: string) =>
    api.delete(`/dhcp/servers/${serverId}/client-classes/${classId}`),
};

export const authApi = {
  login: (username: string, password: string) =>
    api
      .post<LoginResponse>("/auth/login", { username, password })
      .then((r) => r.data),
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
