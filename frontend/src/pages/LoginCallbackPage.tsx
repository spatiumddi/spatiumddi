import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

/**
 * Landing route that consumes the tokens delivered by the OIDC/SAML callback
 * as a URL hash fragment (#access_token=…&refresh_token=…&force_password_change=…).
 *
 * Hash fragments are never sent to the server, so the tokens stay on the
 * client. We lift them into localStorage (same storage the password flow
 * uses) and redirect into the app.
 */
export function LoginCallbackPage() {
  const navigate = useNavigate();

  useEffect(() => {
    const hash = window.location.hash.replace(/^#/, "");
    const params = new URLSearchParams(hash);
    const access = params.get("access_token");
    const refresh = params.get("refresh_token");
    const forcePwd = params.get("force_password_change") === "true";

    if (!access || !refresh) {
      navigate("/login?error=oidc_no_tokens", { replace: true });
      return;
    }

    localStorage.setItem("access_token", access);
    localStorage.setItem("refresh_token", refresh);

    // Clear the hash so tokens don't linger in the URL bar.
    window.history.replaceState(
      null,
      "",
      window.location.pathname + window.location.search,
    );

    navigate(forcePwd ? "/change-password" : "/dashboard", { replace: true });
  }, [navigate]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <p className="text-sm text-muted-foreground">Finishing sign-in…</p>
    </div>
  );
}
