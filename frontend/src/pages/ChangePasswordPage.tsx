import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { authApi } from "@/lib/api";

interface PolicyDetail {
  reason?: string;
  errors?: string[];
}

export function ChangePasswordPage() {
  const navigate = useNavigate();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [policyErrors, setPolicyErrors] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);

  const { data: policy } = useQuery({
    queryKey: ["password-policy"],
    queryFn: () => authApi.passwordPolicy(),
    staleTime: 60_000,
  });

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setPolicyErrors([]);

    if (newPassword !== confirmPassword) {
      setError("New passwords do not match.");
      return;
    }

    setLoading(true);
    try {
      await authApi.changePassword(currentPassword, newPassword);
      navigate("/dashboard");
    } catch (err) {
      // Server emits ``{detail: {reason: 'password_policy'|'password_history',
      // errors: [...]}}`` for rule violations and a plain string detail for
      // bad-current-password / generic failure. Surface each rule on its own
      // line so the operator can fix everything in one pass.
      if (isAxiosError(err)) {
        const detail = err.response?.data?.detail as
          | string
          | PolicyDetail
          | undefined;
        if (
          detail &&
          typeof detail === "object" &&
          Array.isArray(detail.errors)
        ) {
          setPolicyErrors(detail.errors);
          setError("");
        } else if (typeof detail === "string") {
          setError(detail);
        } else {
          setError(
            "Failed to change password. Check your current password and try again.",
          );
        }
      } else {
        setError("Unexpected error — try again.");
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <div className="w-full max-w-sm space-y-6 rounded-lg border bg-card p-8 shadow-sm">
        <div className="space-y-1 text-center">
          <h1 className="text-2xl font-bold tracking-tight">Change Password</h1>
          <p className="text-sm text-muted-foreground">
            You must set a new password before continuing.
          </p>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <label htmlFor="current-password" className="text-sm font-medium">
              Current Password
            </label>
            <input
              id="current-password"
              type="password"
              autoComplete="current-password"
              required
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>
          <div className="space-y-2">
            <label htmlFor="new-password" className="text-sm font-medium">
              New Password
            </label>
            <input
              id="new-password"
              type="password"
              autoComplete="new-password"
              required
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
            {policy && (
              <PolicyHintList policy={policy} candidate={newPassword} />
            )}
          </div>
          <div className="space-y-2">
            <label htmlFor="confirm-password" className="text-sm font-medium">
              Confirm New Password
            </label>
            <input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              required
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
          {policyErrors.length > 0 && (
            <ul className="list-disc space-y-1 pl-5 text-sm text-destructive">
              {policyErrors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          )}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {loading ? "Updating…" : "Set New Password"}
          </button>
        </form>
      </div>
    </div>
  );
}

function PolicyHintList({
  policy,
  candidate,
}: {
  policy: import("@/lib/api").PasswordPolicy;
  candidate: string;
}) {
  const rules: { ok: boolean; label: string }[] = [
    {
      ok: candidate.length >= policy.min_length,
      label: `At least ${policy.min_length} characters`,
    },
  ];
  if (policy.require_uppercase) {
    rules.push({
      ok: /[A-Z]/.test(candidate),
      label: "Contains an uppercase letter",
    });
  }
  if (policy.require_lowercase) {
    rules.push({
      ok: /[a-z]/.test(candidate),
      label: "Contains a lowercase letter",
    });
  }
  if (policy.require_digit) {
    rules.push({
      ok: /\d/.test(candidate),
      label: "Contains a digit",
    });
  }
  if (policy.require_symbol) {
    rules.push({
      ok: /[^A-Za-z0-9]/.test(candidate),
      label: "Contains a symbol",
    });
  }
  if (policy.history_count > 0) {
    rules.push({
      ok: candidate.length > 0,
      label: `Cannot match the last ${policy.history_count} passwords (checked on submit)`,
    });
  }
  return (
    <ul className="space-y-0.5 text-xs text-muted-foreground">
      {rules.map((r) => (
        <li
          key={r.label}
          className={r.ok ? "text-emerald-500" : "text-muted-foreground"}
        >
          {r.ok ? "✓" : "○"} {r.label}
        </li>
      ))}
    </ul>
  );
}
