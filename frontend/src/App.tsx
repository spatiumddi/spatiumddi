import { Routes, Route, Navigate } from "react-router-dom";

import { AppLayout } from "@/components/layout/AppLayout";
import { LoginPage } from "@/pages/LoginPage";
import { LoginCallbackPage } from "@/pages/LoginCallbackPage";
import { ChangePasswordPage } from "@/pages/ChangePasswordPage";
import { DashboardPage } from "@/pages/DashboardPage";
import { IPAMPage } from "@/pages/ipam/IPAMPage";
import { NATPage } from "@/pages/ipam/NATPage";
import { DNSPage } from "@/pages/dns/DNSPage";
import { DNSPoolsPage } from "@/pages/dns/DNSPoolsPage";
import { VLANsPage } from "@/pages/vlans/VLANsPage";
import { DHCPPage } from "@/pages/dhcp/DHCPPage";
import { KubernetesPage } from "@/pages/kubernetes/KubernetesPage";
import { DockerPage } from "@/pages/docker/DockerPage";
import { ProxmoxPage } from "@/pages/proxmox/ProxmoxPage";
import { TailscalePage } from "@/pages/tailscale/TailscalePage";
import { NetworkPage } from "@/pages/network/NetworkPage";
import { DeviceDetailView } from "@/pages/network/DeviceDetailView";
import { NmapToolsPage } from "@/pages/nmap/NmapToolsPage";
import { CidrCalculatorPage } from "@/pages/tools/CidrCalculatorPage";
import { SubnetPlannerListPage } from "@/pages/ipam/SubnetPlannerListPage";
import { SubnetPlannerEditorPage } from "@/pages/ipam/SubnetPlannerEditorPage";
import { LogsPage } from "@/pages/LogsPage";
import { UsersPage } from "@/pages/admin/UsersPage";
import { AuditPage } from "@/pages/admin/AuditPage";
import { CustomFieldsPage } from "@/pages/admin/CustomFieldsPage";
import { ApiTokensPage } from "@/pages/admin/ApiTokensPage";
import { AlertsPage } from "@/pages/admin/AlertsPage";
import { AuthProvidersPage } from "@/pages/admin/AuthProvidersPage";
import { GroupsPage } from "@/pages/admin/GroupsPage";
import { RolesPage } from "@/pages/admin/RolesPage";
import { TrashPage } from "@/pages/admin/TrashPage";
import { WebhooksPage } from "@/pages/admin/WebhooksPage";
import { PlatformInsightsPage } from "@/pages/admin/PlatformInsightsPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { useAuth } from "@/hooks/useAuth";

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useAuth();
  return isAuthenticated ? <>{children}</> : <Navigate to="/login" replace />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/login/callback" element={<LoginCallbackPage />} />
      <Route path="/change-password" element={<ChangePasswordPage />} />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <AppLayout />
          </ProtectedRoute>
        }
      >
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="ipam" element={<IPAMPage />} />
        <Route path="ipam/nat" element={<NATPage />} />
        <Route path="ipam/plans" element={<SubnetPlannerListPage />} />
        <Route path="ipam/plans/:id" element={<SubnetPlannerEditorPage />} />
        <Route path="dns" element={<DNSPage />} />
        <Route path="dns/pools" element={<DNSPoolsPage />} />
        <Route path="vlans" element={<VLANsPage />} />
        <Route path="network" element={<NetworkPage />} />
        <Route path="network/:id" element={<DeviceDetailView />} />
        <Route path="tools/nmap" element={<NmapToolsPage />} />
        <Route path="tools/cidr" element={<CidrCalculatorPage />} />
        <Route path="dhcp" element={<DHCPPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="kubernetes" element={<KubernetesPage />} />
        <Route path="docker" element={<DockerPage />} />
        <Route path="proxmox" element={<ProxmoxPage />} />
        <Route path="tailscale" element={<TailscalePage />} />
        <Route path="admin/users" element={<UsersPage />} />
        <Route path="admin/groups" element={<GroupsPage />} />
        <Route path="admin/roles" element={<RolesPage />} />
        <Route path="admin/audit" element={<AuditPage />} />
        <Route path="admin/custom-fields" element={<CustomFieldsPage />} />
        <Route path="admin/auth-providers" element={<AuthProvidersPage />} />
        <Route path="admin/api-tokens" element={<ApiTokensPage />} />
        <Route path="admin/alerts" element={<AlertsPage />} />
        <Route path="admin/webhooks" element={<WebhooksPage />} />
        <Route path="admin/trash" element={<TrashPage />} />
        <Route
          path="admin/platform-insights"
          element={<PlatformInsightsPage />}
        />
        <Route
          path="admin/failover-channels"
          element={<Navigate to="/dhcp" replace />}
        />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
    </Routes>
  );
}
