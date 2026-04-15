import { Routes, Route, Navigate } from "react-router-dom";

import { AppLayout } from "@/components/layout/AppLayout";
import { LoginPage } from "@/pages/LoginPage";
import { ChangePasswordPage } from "@/pages/ChangePasswordPage";
import { DashboardPage } from "@/pages/DashboardPage";
import { IPAMPage } from "@/pages/ipam/IPAMPage";
import { DNSPage } from "@/pages/dns/DNSPage";
import { DHCPPage } from "@/pages/dhcp/DHCPPage";
import { UsersPage } from "@/pages/admin/UsersPage";
import { AuditPage } from "@/pages/admin/AuditPage";
import { CustomFieldsPage } from "@/pages/admin/CustomFieldsPage";
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
        <Route path="dhcp" element={<DHCPPage />} />
        <Route path="admin/users" element={<UsersPage />} />
        <Route path="admin/audit" element={<AuditPage />} />
        <Route path="admin/custom-fields" element={<CustomFieldsPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
    </Routes>
  );
}
