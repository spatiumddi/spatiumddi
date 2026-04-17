import { useEffect, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "@/hooks/useAuth";
import { authApi, type PublicAuthProvider } from "@/lib/api";
import { KeyRound, ShieldCheck } from "lucide-react";

function humanizeError(code: string | null): string {
  if (!code) return "";
  if (code.startsWith("oidc_rejected_")) {
    const reason = code.slice("oidc_rejected_".length);
    if (reason === "no_group_mapping_match") {
      return "OIDC login rejected: your groups do not match any configured mapping.";
    }
    if (reason === "username_collision") {
      return "OIDC login rejected: a local user with the same username already exists.";
    }
    if (reason === "auto_create_disabled") {
      return "OIDC login rejected: auto-creating users is disabled for this provider.";
    }
    return `OIDC login rejected (${reason}).`;
  }
  if (code === "oidc_exchange_failed")
    return "OIDC token exchange failed. Check provider configuration.";
  if (
    code === "oidc_state_missing" ||
    code === "oidc_state_invalid" ||
    code === "oidc_state_mismatch"
  )
    return "OIDC flow state was invalid or expired. Please try again.";
  if (code === "oidc_no_code")
    return "OIDC provider returned no authorization code.";
  if (code === "oidc_discovery_failed")
    return "OIDC discovery failed — the provider's metadata URL is unreachable.";
  if (code === "oidc_misconfigured")
    return "OIDC provider is misconfigured. Contact an administrator.";
  if (code.startsWith("oidc_idp_"))
    return `Identity provider returned: ${code.slice(9)}`;
  return "Login failed.";
}

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const initialError = humanizeError(searchParams.get("error"));

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(initialError);
  const [loading, setLoading] = useState(false);
  const [providers, setProviders] = useState<PublicAuthProvider[]>([]);

  useEffect(() => {
    authApi
      .publicProviders()
      .then(setProviders)
      .catch(() => setProviders([]));
  }, []);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const tokens = await login(username, password);
      if (tokens.force_password_change) {
        navigate("/change-password");
      } else {
        navigate("/dashboard");
      }
    } catch {
      setError("Invalid username or password.");
    } finally {
      setLoading(false);
    }
  }

  function dismissErrorBanner() {
    setError("");
    if (searchParams.get("error")) {
      searchParams.delete("error");
      setSearchParams(searchParams, { replace: true });
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <div className="w-full max-w-sm space-y-6 rounded-lg border bg-card p-8 shadow-sm">
        <div className="space-y-1 text-center">
          <h1 className="text-2xl font-bold tracking-tight">SpatiumDDI</h1>
          <p className="text-sm text-muted-foreground">
            Sign in to your account
          </p>
        </div>

        {error && (
          <div className="flex items-start justify-between gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            <span>{error}</span>
            <button
              onClick={dismissErrorBanner}
              className="font-semibold hover:underline"
              type="button"
            >
              ×
            </button>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <label htmlFor="username" className="text-sm font-medium">
              Username
            </label>
            <input
              id="username"
              type="text"
              autoComplete="username"
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>
          <div className="space-y-2">
            <label htmlFor="password" className="text-sm font-medium">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>

        {providers.length > 0 && (
          <>
            <div className="relative">
              <div className="absolute inset-0 flex items-center">
                <div className="w-full border-t" />
              </div>
              <div className="relative flex justify-center text-xs uppercase">
                <span className="bg-card px-2 text-muted-foreground">or</span>
              </div>
            </div>
            <div className="space-y-2">
              {providers.map((p) => (
                <a
                  key={p.id}
                  href={`/api/v1/auth/${p.id}/authorize`}
                  className="flex w-full items-center justify-center gap-2 rounded-md border bg-background px-4 py-2 text-sm font-medium transition-colors hover:bg-accent"
                >
                  {p.type === "oidc" ? (
                    <KeyRound className="h-4 w-4" />
                  ) : (
                    <ShieldCheck className="h-4 w-4" />
                  )}
                  Sign in with {p.name}
                </a>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
