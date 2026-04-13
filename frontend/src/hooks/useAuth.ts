import { useState, useCallback } from "react";
import { authApi } from "@/lib/api";

export function useAuth() {
  const [isAuthenticated, setIsAuthenticated] = useState(
    () => !!localStorage.getItem("access_token")
  );

  const login = useCallback(async (username: string, password: string) => {
    const tokens = await authApi.login(username, password);
    localStorage.setItem("access_token", tokens.access_token);
    localStorage.setItem("refresh_token", tokens.refresh_token);
    setIsAuthenticated(true);
    return tokens;
  }, []);

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } finally {
      localStorage.removeItem("access_token");
      localStorage.removeItem("refresh_token");
      setIsAuthenticated(false);
    }
  }, []);

  return { isAuthenticated, login, logout };
}
