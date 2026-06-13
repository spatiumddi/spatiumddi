/**
 * #408 — shared re-confirmation inputs for sensitive reveals.
 *
 * Local users confirm with their password; external-auth (LDAP / OIDC /
 * SAML / RADIUS / TACACS+) users have no local password, so they confirm
 * with a TOTP code from their authenticator (a local user with MFA enrolled
 * may use either). The backend `reverify_operator` helper decides; the UI
 * just offers both fields and lets the operator fill whichever applies.
 */

const inputCls =
  "mt-1 w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

export function ReauthFields({
  password,
  onPassword,
  totp,
  onTotp,
  autoFocus = false,
}: {
  password: string;
  onPassword: (v: string) => void;
  totp: string;
  onTotp: (v: string) => void;
  autoFocus?: boolean;
}) {
  return (
    <div className="space-y-2">
      <label className="block text-xs font-medium">
        Password{" "}
        <span className="font-normal text-muted-foreground">
          (local accounts)
        </span>
        <input
          type="password"
          autoComplete="current-password"
          autoFocus={autoFocus}
          value={password}
          onChange={(e) => onPassword(e.target.value)}
          className={inputCls}
        />
      </label>
      <label className="block text-xs font-medium">
        Authenticator code{" "}
        <span className="font-normal text-muted-foreground">
          (SSO accounts)
        </span>
        <input
          inputMode="numeric"
          autoComplete="one-time-code"
          placeholder="123456"
          value={totp}
          onChange={(e) => onTotp(e.target.value)}
          className={inputCls}
        />
      </label>
      <p className="text-[11px] text-muted-foreground">
        Local users: enter your password. SSO users (LDAP / OIDC / SAML / RADIUS
        / TACACS+): enter a code from your authenticator — enrol under{" "}
        <span className="font-medium">Account → Two-factor</span> first if you
        haven't.
      </p>
    </div>
  );
}
