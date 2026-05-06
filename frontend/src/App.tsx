import { Routes, Route, Navigate } from "react-router-dom";

import { AppLayout } from "@/components/layout/AppLayout";
import { LoginPage } from "@/pages/LoginPage";
import { LoginCallbackPage } from "@/pages/LoginCallbackPage";
import { ChangePasswordPage } from "@/pages/ChangePasswordPage";
import { AccountPage } from "@/pages/AccountPage";
import { DashboardPage } from "@/pages/DashboardPage";
import { IPAMPage } from "@/pages/ipam/IPAMPage";
import { NATPage } from "@/pages/ipam/NATPage";
import { DNSPage } from "@/pages/dns/DNSPage";
import { DNSPoolsPage } from "@/pages/dns/DNSPoolsPage";
import { VLANsPage } from "@/pages/vlans/VLANsPage";
import { VRFsPage } from "@/pages/vrfs/VRFsPage";
import { VRFDetailPage } from "@/pages/vrfs/VRFDetailPage";
import { DHCPPage } from "@/pages/dhcp/DHCPPage";
import { PXEProfilesPage } from "@/pages/dhcp/PXEProfilesPage";
import { KubernetesPage } from "@/pages/kubernetes/KubernetesPage";
import { DockerPage } from "@/pages/docker/DockerPage";
import { ProxmoxPage } from "@/pages/proxmox/ProxmoxPage";
import { TailscalePage } from "@/pages/tailscale/TailscalePage";
import { NetworkPage } from "@/pages/network/NetworkPage";
import { DeviceDetailView } from "@/pages/network/DeviceDetailView";
import { AsnsPage } from "@/pages/network/AsnsPage";
import { AsnDetailPage } from "@/pages/network/AsnDetailPage";
import { CircuitsPage } from "@/pages/network/CircuitsPage";
import { CustomersPage } from "@/pages/network/CustomersPage";
import { OverlaysPage } from "@/pages/network/OverlaysPage";
import { OverlayDetailPage } from "@/pages/network/OverlayDetailPage";
import { ProvidersPage } from "@/pages/network/ProvidersPage";
import { ServicesPage } from "@/pages/network/ServicesPage";
import { SitesPage } from "@/pages/network/SitesPage";
import { NmapToolsPage } from "@/pages/nmap/NmapToolsPage";
import { CidrCalculatorPage } from "@/pages/tools/CidrCalculatorPage";
import { SubnetPlannerListPage } from "@/pages/ipam/SubnetPlannerListPage";
import { SubnetPlannerEditorPage } from "@/pages/ipam/SubnetPlannerEditorPage";
import { LogsPage } from "@/pages/LogsPage";
import { UsersPage } from "@/pages/admin/UsersPage";
import { AuditPage } from "@/pages/admin/AuditPage";
import { AIProvidersPage } from "@/pages/admin/AIProvidersPage";
import { AIPromptsPage } from "@/pages/admin/AIPromptsPage";
import { AIToolCatalogPage } from "@/pages/admin/AIToolCatalogPage";
import { FeaturesPage } from "@/pages/admin/FeaturesPage";
import { CustomFieldsPage } from "@/pages/admin/CustomFieldsPage";
import { IPAMTemplatesPage } from "@/pages/admin/IPAMTemplatesPage";
import { ApiTokensPage } from "@/pages/admin/ApiTokensPage";
import { SessionsPage } from "@/pages/admin/SessionsPage";
import { AlertsPage } from "@/pages/admin/AlertsPage";
import { AuthProvidersPage } from "@/pages/admin/AuthProvidersPage";
import { DomainsPage } from "@/pages/admin/DomainsPage";
import { DomainDetailPage } from "@/pages/admin/DomainDetailPage";
import { GroupsPage } from "@/pages/admin/GroupsPage";
import { RolesPage } from "@/pages/admin/RolesPage";
import { CompliancePage } from "@/pages/admin/CompliancePage";
import { ConformityPage } from "@/pages/admin/ConformityPage";
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
        <Route path="account" element={<AccountPage />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="ipam" element={<IPAMPage />} />
        <Route path="ipam/nat" element={<NATPage />} />
        <Route path="ipam/plans" element={<SubnetPlannerListPage />} />
        <Route path="ipam/plans/:id" element={<SubnetPlannerEditorPage />} />
        <Route path="dns" element={<DNSPage />} />
        <Route path="dns/pools" element={<DNSPoolsPage />} />
        {/* Network section — Devices / VLANs / VRFs / ASNs. The old top-
            level /network and /vlans paths redirect here so existing
            bookmarks keep working. See issue #84. */}
        <Route
          path="network"
          element={<Navigate to="/network/devices" replace />}
        />
        <Route path="network/devices" element={<NetworkPage />} />
        <Route path="network/devices/:id" element={<DeviceDetailView />} />
        <Route path="network/vlans" element={<VLANsPage />} />
        <Route path="network/vrfs" element={<VRFsPage />} />
        <Route path="network/vrfs/:id" element={<VRFDetailPage />} />
        <Route path="network/asns" element={<AsnsPage />} />
        <Route path="network/asns/:id" element={<AsnDetailPage />} />
        <Route path="network/circuits" element={<CircuitsPage />} />
        <Route path="network/customers" element={<CustomersPage />} />
        <Route path="network/overlays" element={<OverlaysPage />} />
        <Route path="network/overlays/:id" element={<OverlayDetailPage />} />
        <Route path="network/providers" element={<ProvidersPage />} />
        <Route path="network/services" element={<ServicesPage />} />
        <Route path="network/sites" element={<SitesPage />} />
        <Route
          path="vlans"
          element={<Navigate to="/network/vlans" replace />}
        />
        {/* Legacy device-detail bookmark (/network/:id) — preserve by
            redirecting to /network/devices/:id. */}
        <Route path="network/:id" element={<DeviceDetailView />} />
        <Route path="tools/nmap" element={<NmapToolsPage />} />
        <Route path="tools/cidr" element={<CidrCalculatorPage />} />
        <Route path="dhcp" element={<DHCPPage />} />
        <Route path="dhcp/groups/:groupId/pxe" element={<PXEProfilesPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="kubernetes" element={<KubernetesPage />} />
        <Route path="docker" element={<DockerPage />} />
        <Route path="proxmox" element={<ProxmoxPage />} />
        <Route path="tailscale" element={<TailscalePage />} />
        <Route path="admin/users" element={<UsersPage />} />
        <Route path="admin/groups" element={<GroupsPage />} />
        <Route path="admin/roles" element={<RolesPage />} />
        <Route path="admin/audit" element={<AuditPage />} />
        <Route path="admin/ai/providers" element={<AIProvidersPage />} />
        <Route path="admin/ai/prompts" element={<AIPromptsPage />} />
        <Route path="admin/ai/tools" element={<AIToolCatalogPage />} />
        <Route path="admin/features" element={<FeaturesPage />} />
        <Route path="admin/custom-fields" element={<CustomFieldsPage />} />
        <Route path="admin/ipam/templates" element={<IPAMTemplatesPage />} />
        <Route path="admin/auth-providers" element={<AuthProvidersPage />} />
        <Route path="admin/api-tokens" element={<ApiTokensPage />} />
        <Route path="admin/sessions" element={<SessionsPage />} />
        <Route path="admin/alerts" element={<AlertsPage />} />
        <Route path="admin/domains" element={<DomainsPage />} />
        <Route path="admin/domains/:id" element={<DomainDetailPage />} />
        <Route path="admin/webhooks" element={<WebhooksPage />} />
        <Route path="admin/compliance" element={<CompliancePage />} />
        <Route path="admin/conformity" element={<ConformityPage />} />
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
