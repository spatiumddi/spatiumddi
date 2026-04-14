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
      const originalRequest = err.config as typeof err.config & { _retry?: boolean };
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
              if (originalRequest?.headers) originalRequest.headers.Authorization = `Bearer ${token}`;
              resolve(client(originalRequest!));
            });
          });
        }

        originalRequest._retry = true;
        isRefreshing = true;
        try {
          const res = await client.post("/auth/refresh", { refresh_token: refreshToken });
          const { access_token, refresh_token: newRefresh } = res.data;
          localStorage.setItem("access_token", access_token);
          localStorage.setItem("refresh_token", newRefresh);
          refreshQueue.forEach((cb) => cb(access_token));
          refreshQueue = [];
          if (originalRequest?.headers) originalRequest.headers.Authorization = `Bearer ${access_token}`;
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
    }
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
}

export interface IPAddress {
  id: string;
  subnet_id: string;
  address: string;
  status: string;
  hostname: string | null;
  description: string | null;
  mac_address: string | null;
  tags: Record<string, unknown>;
  custom_fields: Record<string, unknown>;
}

export const ipamApi = {
  listSpaces: () => api.get<IPSpace[]>("/ipam/spaces").then((r) => r.data),
  getSpace: (id: string) => api.get<IPSpace>(`/ipam/spaces/${id}`).then((r) => r.data),
  createSpace: (data: Partial<IPSpace>) =>
    api.post<IPSpace>("/ipam/spaces", data).then((r) => r.data),
  updateSpace: (id: string, data: Partial<IPSpace>) =>
    api.put<IPSpace>(`/ipam/spaces/${id}`, data).then((r) => r.data),
  deleteSpace: (id: string) => api.delete(`/ipam/spaces/${id}`),

  listBlocks: (spaceId?: string) =>
    api
      .get<IPBlock[]>("/ipam/blocks", { params: spaceId ? { space_id: spaceId } : undefined })
      .then((r) => r.data),
  createBlock: (data: Partial<IPBlock>) =>
    api.post<IPBlock>("/ipam/blocks", data).then((r) => r.data),
  updateBlock: (id: string, data: Partial<Pick<IPBlock, "name" | "description" | "tags" | "custom_fields">>) =>
    api.put<IPBlock>(`/ipam/blocks/${id}`, data).then((r) => r.data),
  deleteBlock: (id: string) => api.delete(`/ipam/blocks/${id}`),

  listSubnets: (params?: { space_id?: string; block_id?: string }) =>
    api.get<Subnet[]>("/ipam/subnets", { params }).then((r) => r.data),
  getSubnet: (id: string) => api.get<Subnet>(`/ipam/subnets/${id}`).then((r) => r.data),
  createSubnet: (data: Partial<Subnet>) =>
    api.post<Subnet>("/ipam/subnets", data).then((r) => r.data),
  updateSubnet: (id: string, data: Partial<Subnet> & { manage_auto_addresses?: boolean }) =>
    api.put<Subnet>(`/ipam/subnets/${id}`, data).then((r) => r.data),
  deleteSubnet: (id: string) => api.delete(`/ipam/subnets/${id}`),

  listAddresses: (subnetId: string) =>
    api.get<IPAddress[]>(`/ipam/subnets/${subnetId}/addresses`).then((r) => r.data),
  createAddress: (data: Partial<IPAddress> & { hostname: string }) =>
    api.post<IPAddress>(`/ipam/subnets/${data.subnet_id}/addresses`, data).then((r) => r.data),
  updateAddress: (id: string, data: Partial<IPAddress>) =>
    api.put<IPAddress>(`/ipam/addresses/${id}`, data).then((r) => r.data),
  deleteAddress: (id: string, permanent = false) =>
    api.delete(`/ipam/addresses/${id}`, { params: permanent ? { permanent: true } : undefined }),
  nextAddress: (subnetId: string, data: { hostname: string; status?: string; mac_address?: string; description?: string; custom_fields?: Record<string, unknown> }) =>
    api.post<IPAddress>(`/ipam/subnets/${subnetId}/next`, data).then((r) => r.data),
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
  update: (id: string, data: Partial<Pick<AppUser, "display_name" | "email" | "is_active" | "is_superadmin" | "force_password_change">>) =>
    api.put<AppUser>(`/users/${id}`, data).then((r) => r.data),
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
  type: "ip_address" | "subnet" | "block" | "space" | "dns_group" | "dns_zone" | "dns_record";
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
      .get<CustomField[]>("/custom-fields", { params: resource_type ? { resource_type } : undefined })
      .then((r) => r.data),
  create: (data: Omit<CustomField, "id">) =>
    api.post<CustomField>("/custom-fields", data).then((r) => r.data),
  update: (id: string, data: Partial<Omit<CustomField, "id" | "resource_type" | "name" | "field_type">>) =>
    api.put<CustomField>(`/custom-fields/${id}`, data).then((r) => r.data),
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
  listGroups: () => api.get<DNSServerGroup[]>("/dns/groups").then((r) => r.data),
  createGroup: (data: Partial<DNSServerGroup>) =>
    api.post<DNSServerGroup>("/dns/groups", data).then((r) => r.data),
  updateGroup: (id: string, data: Partial<DNSServerGroup>) =>
    api.put<DNSServerGroup>(`/dns/groups/${id}`, data).then((r) => r.data),
  deleteGroup: (id: string) => api.delete(`/dns/groups/${id}`),

  // Servers
  listServers: (groupId: string) =>
    api.get<DNSServer[]>(`/dns/groups/${groupId}/servers`).then((r) => r.data),
  createServer: (groupId: string, data: Partial<DNSServer> & { api_key?: string }) =>
    api.post<DNSServer>(`/dns/groups/${groupId}/servers`, data).then((r) => r.data),
  updateServer: (groupId: string, serverId: string, data: Partial<DNSServer> & { api_key?: string }) =>
    api.put<DNSServer>(`/dns/groups/${groupId}/servers/${serverId}`, data).then((r) => r.data),
  deleteServer: (groupId: string, serverId: string) =>
    api.delete(`/dns/groups/${groupId}/servers/${serverId}`),

  // Server options
  getOptions: (groupId: string) =>
    api.get<DNSServerOptions>(`/dns/groups/${groupId}/options`).then((r) => r.data),
  updateOptions: (groupId: string, data: Partial<DNSServerOptions>) =>
    api.put<DNSServerOptions>(`/dns/groups/${groupId}/options`, data).then((r) => r.data),
  addTrustAnchor: (groupId: string, data: Omit<DNSTrustAnchor, "id" | "added_at">) =>
    api.post<DNSTrustAnchor>(`/dns/groups/${groupId}/options/trust-anchors`, data).then((r) => r.data),
  deleteTrustAnchor: (groupId: string, anchorId: string) =>
    api.delete(`/dns/groups/${groupId}/options/trust-anchors/${anchorId}`),

  // ACLs
  listAcls: (groupId: string) =>
    api.get<DNSAcl[]>(`/dns/groups/${groupId}/acls`).then((r) => r.data),
  createAcl: (groupId: string, data: Partial<DNSAcl>) =>
    api.post<DNSAcl>(`/dns/groups/${groupId}/acls`, data).then((r) => r.data),
  updateAcl: (groupId: string, aclId: string, data: Partial<DNSAcl>) =>
    api.put<DNSAcl>(`/dns/groups/${groupId}/acls/${aclId}`, data).then((r) => r.data),
  deleteAcl: (groupId: string, aclId: string) =>
    api.delete(`/dns/groups/${groupId}/acls/${aclId}`),

  // Views
  listViews: (groupId: string) =>
    api.get<DNSView[]>(`/dns/groups/${groupId}/views`).then((r) => r.data),
  createView: (groupId: string, data: Partial<DNSView>) =>
    api.post<DNSView>(`/dns/groups/${groupId}/views`, data).then((r) => r.data),
  updateView: (groupId: string, viewId: string, data: Partial<DNSView>) =>
    api.put<DNSView>(`/dns/groups/${groupId}/views/${viewId}`, data).then((r) => r.data),
  deleteView: (groupId: string, viewId: string) =>
    api.delete(`/dns/groups/${groupId}/views/${viewId}`),

  // Zones
  listZones: (groupId: string) =>
    api.get<DNSZone[]>(`/dns/groups/${groupId}/zones`).then((r) => r.data),
  createZone: (groupId: string, data: Partial<DNSZone>) =>
    api.post<DNSZone>(`/dns/groups/${groupId}/zones`, data).then((r) => r.data),
  updateZone: (groupId: string, zoneId: string, data: Partial<DNSZone>) =>
    api.put<DNSZone>(`/dns/groups/${groupId}/zones/${zoneId}`, data).then((r) => r.data),
  deleteZone: (groupId: string, zoneId: string) =>
    api.delete(`/dns/groups/${groupId}/zones/${zoneId}`),

  // Records
  listRecords: (groupId: string, zoneId: string) =>
    api.get<DNSRecord[]>(`/dns/groups/${groupId}/zones/${zoneId}/records`).then((r) => r.data),
  createRecord: (groupId: string, zoneId: string, data: Partial<DNSRecord>) =>
    api.post<DNSRecord>(`/dns/groups/${groupId}/zones/${zoneId}/records`, data).then((r) => r.data),
  updateRecord: (groupId: string, zoneId: string, recordId: string, data: Partial<DNSRecord>) =>
    api.put<DNSRecord>(`/dns/groups/${groupId}/zones/${zoneId}/records/${recordId}`, data).then((r) => r.data),
  deleteRecord: (groupId: string, zoneId: string, recordId: string) =>
    api.delete(`/dns/groups/${groupId}/zones/${zoneId}/records/${recordId}`),
};

export const authApi = {
  login: (username: string, password: string) =>
    api.post<LoginResponse>("/auth/login", { username, password }).then((r) => r.data),
  logout: () => api.post("/auth/logout"),
  refresh: (refreshToken: string) =>
    api.post<LoginResponse>("/auth/refresh", { refresh_token: refreshToken }).then((r) => r.data),
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
