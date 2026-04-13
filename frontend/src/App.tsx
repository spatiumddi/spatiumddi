import { Routes, Route, Navigate } from "react-router-dom";

import { AppLayout } from "@/components/layout/AppLayout";
import { LoginPage } from "@/pages/LoginPage";
import { ChangePasswordPage } from "@/pages/ChangePasswordPage";
import { DashboardPage } from "@/pages/DashboardPage";
import { IPSpacesPage } from "@/pages/ipam/IPSpacesPage";
import { SubnetsPage } from "@/pages/ipam/SubnetsPage";
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
        <Route path="ipam/spaces" element={<IPSpacesPage />} />
        <Route path="ipam/subnets" element={<SubnetsPage />} />
      </Route>
    </Routes>
  );
}
