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

  // On 401, clear auth and redirect to login
  client.interceptors.response.use(
    (res) => res,
    (err: AxiosError) => {
      if (err.response?.status === 401) {
        localStorage.removeItem("access_token");
        localStorage.removeItem("refresh_token");
        window.location.href = "/login";
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
  tags: Record<string, unknown>;
}

export interface Subnet {
  id: string;
  space_id: string;
  block_id: string | null;
  network: string;
  name: string;
  description: string;
  vlan_id: number | null;
  vxlan_id: number | null;
  gateway: string | null;
  status: string;
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
  deleteBlock: (id: string) => api.delete(`/ipam/blocks/${id}`),

  listSubnets: (params?: { space_id?: string; block_id?: string }) =>
    api.get<Subnet[]>("/ipam/subnets", { params }).then((r) => r.data),
  getSubnet: (id: string) => api.get<Subnet>(`/ipam/subnets/${id}`).then((r) => r.data),
  createSubnet: (data: Partial<Subnet>) =>
    api.post<Subnet>("/ipam/subnets", data).then((r) => r.data),
  updateSubnet: (id: string, data: Partial<Subnet>) =>
    api.put<Subnet>(`/ipam/subnets/${id}`, data).then((r) => r.data),
  deleteSubnet: (id: string) => api.delete(`/ipam/subnets/${id}`),

  listAddresses: (subnetId: string) =>
    api.get<IPAddress[]>(`/ipam/subnets/${subnetId}/addresses`).then((r) => r.data),
  createAddress: (data: Partial<IPAddress>) =>
    api.post<IPAddress>(`/ipam/subnets/${data.subnet_id}/addresses`, data).then((r) => r.data),
  updateAddress: (id: string, data: Partial<IPAddress>) =>
    api.put<IPAddress>(`/ipam/addresses/${id}`, data).then((r) => r.data),
  deleteAddress: (id: string) => api.delete(`/ipam/addresses/${id}`),
  nextAddress: (subnetId: string, data?: { hostname?: string; description?: string }) =>
    api.post<IPAddress>(`/ipam/subnets/${subnetId}/next`, data ?? {}).then((r) => r.data),
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

export const authApi = {
  login: (username: string, password: string) =>
    api.post<LoginResponse>("/auth/login", { username, password }).then((r) => r.data),
  logout: () => api.post("/auth/logout"),
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
