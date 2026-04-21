import { Routes, Route, Navigate } from "react-router-dom";

import { AppLayout } from "@/components/layout/AppLayout";
import { LoginPage } from "@/pages/LoginPage";
import { LoginCallbackPage } from "@/pages/LoginCallbackPage";
import { ChangePasswordPage } from "@/pages/ChangePasswordPage";
import { DashboardPage } from "@/pages/DashboardPage";
import { IPAMPage } from "@/pages/ipam/IPAMPage";
import { DNSPage } from "@/pages/dns/DNSPage";
import { VLANsPage } from "@/pages/vlans/VLANsPage";
import { DHCPPage } from "@/pages/dhcp/DHCPPage";
import { LogsPage } from "@/pages/LogsPage";
import { UsersPage } from "@/pages/admin/UsersPage";
import { AuditPage } from "@/pages/admin/AuditPage";
import { CustomFieldsPage } from "@/pages/admin/CustomFieldsPage";
import { ApiTokensPage } from "@/pages/admin/ApiTokensPage";
import { AlertsPage } from "@/pages/admin/AlertsPage";
import { AuthProvidersPage } from "@/pages/admin/AuthProvidersPage";
import { GroupsPage } from "@/pages/admin/GroupsPage";
import { RolesPage } from "@/pages/admin/RolesPage";
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
        <Route path="dns" element={<DNSPage />} />
        <Route path="vlans" element={<VLANsPage />} />
        <Route path="dhcp" element={<DHCPPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="admin/users" element={<UsersPage />} />
        <Route path="admin/groups" element={<GroupsPage />} />
        <Route path="admin/roles" element={<RolesPage />} />
        <Route path="admin/audit" element={<AuditPage />} />
        <Route path="admin/custom-fields" element={<CustomFieldsPage />} />
        <Route path="admin/auth-providers" element={<AuthProvidersPage />} />
        <Route path="admin/api-tokens" element={<ApiTokensPage />} />
        <Route path="admin/alerts" element={<AlertsPage />} />
        <Route
          path="admin/failover-channels"
          element={<Navigate to="/dhcp" replace />}
        />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
    </Routes>
  );
}
