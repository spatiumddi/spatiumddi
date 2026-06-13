import { useState, useCallback } from "react";
import { authApi, type LoginResponse } from "@/lib/api";

// SECURITY (#400, L1): both the JWT access token and the refresh
// token are persisted in localStorage. This is XSS-stealable —
// any script injected into the SPA's origin can read
// localStorage.getItem("access_token") / "refresh_token" and
// exfiltrate a full session (including the long-lived refresh
// token). The CSP added in #400/L2 (script-src 'self', no
// 'unsafe-inline'/'unsafe-eval') is the primary mitigation, but
// localStorage remains the weak link.
//
// INTENDED FIX (deferred — too large for this PR): move the refresh
// token into an HttpOnly + Secure + SameSite=Strict cookie issued by
// the backend (/auth/login + /auth/refresh), and keep ONLY the
// short-lived access token in JS memory (a module-level variable /
// React context), never localStorage. The axios interceptor in
// lib/api.ts would then call /auth/refresh with credentials:'include'
// and read the new access token from the JSON body, with the cookie
// invisible to JS. That refactor touches the backend auth router,
// the login/callback pages, and this hook together, so it's tracked
// as a follow-up rather than bundled into the #400 hardening pass.

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
