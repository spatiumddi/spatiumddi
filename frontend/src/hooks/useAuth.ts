import { useState, useCallback } from "react";
import { authApi, type LoginResponse } from "@/lib/api";

export function useAuth() {
  const [isAuthenticated, setIsAuthenticated] = useState(
    () => !!localStorage.getItem("access_token"),
  );

  /** Run the password step. Returns the raw LoginResponse so the caller
   * can inspect ``mfa_required`` and route to the TOTP prompt without
   * touching localStorage on a half-completed login. */
  const login = useCallback(
    async (username: string, password: string): Promise<LoginResponse> => {
      const resp = await authApi.login(username, password);
      if (!resp.mfa_required && resp.access_token && resp.refresh_token) {
        localStorage.setItem("access_token", resp.access_token);
        localStorage.setItem("refresh_token", resp.refresh_token);
        setIsAuthenticated(true);
      }
      return resp;
    },
    [],
  );

  /** Complete a TOTP-gated login. ``code`` is the authenticator's
   * 6-digit number; ``recovery_code`` is the one-time recovery code.
   * Submit one or the other, not both. */
  const completeMfa = useCallback(
    async (
      mfa_token: string,
      body: { code?: string; recovery_code?: string },
    ): Promise<LoginResponse> => {
      const resp = await authApi.loginMfa(mfa_token, body);
      if (resp.access_token && resp.refresh_token) {
        localStorage.setItem("access_token", resp.access_token);
        localStorage.setItem("refresh_token", resp.refresh_token);
        setIsAuthenticated(true);
      }
      return resp;
    },
    [],
  );

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } finally {
      localStorage.removeItem("access_token");
      localStorage.removeItem("refresh_token");
      setIsAuthenticated(false);
    }
  }, []);

  return { isAuthenticated, login, completeMfa, logout };
}
