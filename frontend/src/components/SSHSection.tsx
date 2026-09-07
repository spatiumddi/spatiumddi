import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Plus, Trash2 } from "lucide-react";

import {
  settingsApi,
  type PlatformSettings,
  type SshAuthorizedKey,
} from "@/lib/api";
import { ConfirmModal } from "@/components/ui/confirm-modal";
import { Toggle } from "@/components/ui/toggle";
import { cn } from "@/lib/utils";

interface Props {
  values: PlatformSettings;
  isSuperadmin: boolean;
  applianceMode: boolean;
  inputCls: string;
}

function Field({
  label,
  description,
  children,
}: {
  label: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-8 py-3">
      <div className="max-w-xl">
        <div className="text-sm font-medium">{label}</div>
        {description && (
          <div className="text-xs text-muted-foreground">{description}</div>
        )}
      </div>
      <div className="flex-shrink-0">{children}</div>
    </div>
  );
}

// A minimal OpenSSH public-key sanity check, mirroring the server-side
// validator just enough to flag obvious garbage in the UI before save.
// The server is the authoritative gate (it base64-decodes + checks the
// embedded algorithm name); this is a fast client-side heads-up.
const _VALID_KEY_TYPES = new Set([
  "ssh-ed25519",
  "ssh-rsa",
  "ssh-dss",
  "ecdsa-sha2-nistp256",
  "ecdsa-sha2-nistp384",
  "ecdsa-sha2-nistp521",
  "sk-ssh-ed25519@openssh.com",
  "sk-ecdsa-sha2-nistp256@openssh.com",
]);

function looksLikeValidKey(pk: string): boolean {
  const s = pk.trim();
  // Reject any control character (incl. CR / LF / NUL) — a smuggled newline
  // could inject a second authorized_keys entry. Done with charCodeAt rather
  // than a control-char regex (which eslint's no-control-regex flags).
  if (!s) return false;
  for (let i = 0; i < s.length; i++) {
    if (s.charCodeAt(i) < 0x20) return false;
  }
  const parts = s.split(/\s+/);
  if (parts.length < 2) return false;
  if (!_VALID_KEY_TYPES.has(parts[0])) return false;
  return /^[A-Za-z0-9+/]+={0,2}$/.test(parts[1]);
}

export function SSHSection({
  values,
  isSuperadmin,
  applianceMode,
  inputCls,
}: Props) {
  const qc = useQueryClient();
  const [keys, setKeys] = useState<SshAuthorizedKey[]>(
    (values.ssh_authorized_keys || []).map((k) => ({ ...k })),
  );
  const [passwordAuth, setPasswordAuth] = useState<boolean>(
    values.ssh_password_auth_enabled,
  );
  const [allowRoot, setAllowRoot] = useState<boolean>(
    values.ssh_allow_root_login,
  );
  const [port, setPort] = useState<number>(values.ssh_port || 22);
  const [sources, setSources] = useState<string[]>(
    values.ssh_allowed_source_networks || [],
  );
  // #1009 — the allowlist above is a note until this is on. The host
  // firewall opens port 22 unconditionally from a floor that sorts ahead of
  // the scoped rule, so enforcing means retiring that floor — which is the
  // appliance's recovery channel, hence an explicit switch rather than a
  // consequence of typing a CIDR.
  const [lockdown, setLockdown] = useState<boolean>(
    values.ssh_lockdown ?? false,
  );

  const dirty =
    passwordAuth !== values.ssh_password_auth_enabled ||
    allowRoot !== values.ssh_allow_root_login ||
    port !== (values.ssh_port || 22) ||
    JSON.stringify(sources) !==
      JSON.stringify(values.ssh_allowed_source_networks || []) ||
    lockdown !== (values.ssh_lockdown ?? false) ||
    JSON.stringify(keys) !==
      JSON.stringify((values.ssh_authorized_keys || []).map((k) => ({ ...k })));

  // Lockout-safety guard (#157) — mirror the server cross-field check.
  // At least one way in must survive: password auth on, OR at least one
  // valid authorized key.
  const validKeyCount = keys.filter((k) =>
    looksLikeValidKey(k.public_key),
  ).length;
  const wouldLockOut = !passwordAuth && validKeyCount === 0;

  // Privileged-port client-side guard (server is authoritative).
  const portInvalid = port < 1 || port > 65535 || (port < 1024 && port !== 22);

  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  // Set only after the operator confirms the server's self-lockout warning;
  // cleared on every successful save so it can never be sticky.
  const [lockdownForced, setLockdownForced] = useState(false);
  const [confirmLockout, setConfirmLockout] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (patch: Partial<PlatformSettings>) => settingsApi.update(patch),
    onSuccess: (updated) => {
      qc.setQueryData(["settings"], updated);
      setKeys((updated.ssh_authorized_keys || []).map((k) => ({ ...k })));
      setPasswordAuth(updated.ssh_password_auth_enabled);
      setAllowRoot(updated.ssh_allow_root_login);
      setPort(updated.ssh_port || 22);
      setSources(updated.ssh_allowed_source_networks || []);
      setLockdown(updated.ssh_lockdown ?? false);
      setSaveErr(null);
      setSavedAt(Date.now());
      setLockdownForced(false);
      setTimeout(() => setSavedAt(null), 2500);
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err);
      // The self-lockout pre-flight is a question, not a failure — surface
      // it as a confirmation the operator can accept, with the server's own
      // wording rather than a paraphrase that could drift from it.
      if (msg.includes("not inside the allowed networks")) {
        setConfirmLockout(msg);
        setSaveErr(null);
        return;
      }
      setSaveErr(msg);
    },
  });

  function handleSave() {
    const patch: Partial<PlatformSettings> = {
      ssh_authorized_keys: keys.map((k) => ({
        name: k.name.trim(),
        public_key: k.public_key.trim(),
        comment: k.comment.trim(),
      })),
      ssh_password_auth_enabled: passwordAuth,
      ssh_allow_root_login: allowRoot,
      ssh_port: port,
      ssh_allowed_source_networks: sources.map((s) => s.trim()).filter(Boolean),
      ssh_lockdown: lockdown,
    };
    // #1009 — the server refuses turning enforcement on from an address the
    // allowlist does not cover, which is the mistake operators actually
    // make. Confirming here re-sends with the acknowledgement rather than
    // silently forcing: the operator has to read what they are accepting.
    if (lockdownForced) patch.ssh_lockdown_force = true;
    mutation.mutate(patch);
  }

  function addKey() {
    setKeys((prev) => [...prev, { name: "", public_key: "", comment: "" }]);
  }
  function updateKey(idx: number, partial: Partial<SshAuthorizedKey>) {
    setKeys((prev) =>
      prev.map((k, i) => (i === idx ? { ...k, ...partial } : k)),
    );
  }
  function removeKey(idx: number) {
    setKeys((prev) => prev.filter((_, i) => i !== idx));
  }

  function addSource() {
    setSources((prev) => [...prev, ""]);
  }
  function updateSource(idx: number, value: string) {
    setSources((prev) => prev.map((s, i) => (i === idx ? value : s)));
  }
  function removeSource(idx: number) {
    setSources((prev) => prev.filter((_, i) => i !== idx));
  }

  return (
    <div className="space-y-2">
      {!applianceMode && (
        <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs">
          <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-700 dark:text-amber-400" />
          <div className="space-y-1">
            <p className="font-medium text-amber-700 dark:text-amber-400">
              SSH config is only applied on appliance hosts
            </p>
            <p className="text-muted-foreground">
              This control plane is running in docker / k8s, where SpatiumDDI
              doesn't manage the host's sshd. Settings saved here still flow
              through the ConfigBundle to any <em>appliance agents</em>{" "}
              registered with this control plane — useful for hybrid
              deployments.
            </p>
          </div>
        </div>
      )}

      {wouldLockOut && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-xs">
          <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-destructive" />
          <div className="space-y-1">
            <p className="font-medium text-destructive">
              Lockout risk — no way in
            </p>
            <p className="text-muted-foreground">
              You've disabled password authentication with no valid authorized
              key configured. Saving would lock you out of every appliance host.
              Add at least one valid public key, or re-enable password
              authentication. Save is disabled until this is resolved.
            </p>
          </div>
        </div>
      )}

      <div className="rounded-md border bg-muted/30 p-3">
        <div className="mb-2 flex items-center justify-between">
          <div>
            <div className="text-sm font-medium">Authorized keys</div>
            <div className="text-xs text-muted-foreground">
              Public keys allowed to log in as <code>admin</code>. One per row.
              Public keys are not secrets.
            </div>
          </div>
          <button
            type="button"
            onClick={addKey}
            disabled={!isSuperadmin}
            className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent disabled:opacity-40"
          >
            <Plus className="h-3 w-3" />
            Add key
          </button>
        </div>
        {keys.length === 0 ? (
          <div className="py-2 text-xs text-muted-foreground">
            No authorized keys configured.
          </div>
        ) : (
          <div className="space-y-3">
            {keys.map((k, idx) => {
              const invalid =
                k.public_key.trim() !== "" && !looksLikeValidKey(k.public_key);
              return (
                <div key={idx} className="rounded-md border bg-background p-2">
                  <div className="flex items-center gap-2">
                    <input
                      type="text"
                      value={k.name}
                      onChange={(e) => updateKey(idx, { name: e.target.value })}
                      placeholder="Label (e.g. alice-laptop)"
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-48")}
                    />
                    <button
                      type="button"
                      onClick={() => removeKey(idx)}
                      disabled={!isSuperadmin}
                      className="ml-auto rounded p-1 text-destructive hover:bg-destructive/10 disabled:opacity-40"
                      title="Remove key"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                  <textarea
                    value={k.public_key}
                    onChange={(e) =>
                      updateKey(idx, { public_key: e.target.value })
                    }
                    placeholder="ssh-ed25519 AAAA… user@host"
                    disabled={!isSuperadmin}
                    rows={2}
                    className={cn(
                      inputCls,
                      "mt-2 w-full font-mono text-xs",
                      invalid && "border-destructive",
                    )}
                  />
                  {invalid && (
                    <div className="mt-1 text-xs text-destructive">
                      Doesn't look like a valid OpenSSH public key.
                    </div>
                  )}
                  <input
                    type="text"
                    value={k.comment}
                    onChange={(e) =>
                      updateKey(idx, { comment: e.target.value })
                    }
                    placeholder="Note (optional)"
                    disabled={!isSuperadmin}
                    className={cn(inputCls, "mt-2 w-full")}
                  />
                </div>
              );
            })}
          </div>
        )}
      </div>

      <Field
        label="Password authentication"
        description="When on, the admin user can log in with a password. Turning it off requires at least one authorized key (otherwise you'd lock yourself out)."
      >
        <Toggle
          checked={passwordAuth}
          onChange={setPasswordAuth}
          disabled={!isSuperadmin}
        />
      </Field>

      <Field
        label="Permit root login"
        description="When off (recommended), the root user cannot SSH in directly — log in as admin and escalate. Leave off unless a tool specifically needs root SSH."
      >
        <Toggle
          checked={allowRoot}
          onChange={setAllowRoot}
          disabled={!isSuperadmin}
        />
      </Field>

      <Field
        label="SSH port"
        description="The port sshd listens on. Ports below 1024 are not allowed (except 22). Port 22 always stays open in the host firewall as an escape hatch, so a bad port change can't lock you out."
      >
        <input
          type="number"
          min={1}
          max={65535}
          value={port}
          onChange={(e) => setPort(Number(e.target.value) || 22)}
          disabled={!isSuperadmin}
          className={cn(
            inputCls,
            "w-28 font-mono",
            portInvalid && "border-destructive",
          )}
        />
      </Field>

      <div className="rounded-md border bg-muted/30 p-3">
        <div className="mb-2 flex items-center justify-between">
          <div>
            <div className="text-sm font-medium">Allowed source networks</div>
            <div className="text-xs text-muted-foreground">
              CIDRs the host firewall scopes the SSH port to (e.g.{" "}
              <code>10.0.0.0/24</code>). Applied only when{" "}
              <span className="font-medium">Enforce source restriction</span> is
              on.
            </div>
          </div>
          <button
            type="button"
            onClick={addSource}
            disabled={!isSuperadmin}
            className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent disabled:opacity-40"
          >
            <Plus className="h-3 w-3" />
            Add CIDR
          </button>
        </div>
        {sources.length === 0 ? (
          <div className="py-2 text-xs text-muted-foreground">
            No source restriction — SSH reachable from anywhere.
          </div>
        ) : (
          <div className="space-y-2">
            {sources.map((s, idx) => (
              <div key={idx} className="flex items-center gap-2">
                <input
                  type="text"
                  value={s}
                  onChange={(e) => updateSource(idx, e.target.value)}
                  placeholder="10.0.0.0/24"
                  disabled={!isSuperadmin}
                  className={cn(inputCls, "w-56 font-mono")}
                />
                <button
                  type="button"
                  onClick={() => removeSource(idx)}
                  disabled={!isSuperadmin}
                  className="rounded p-1 text-destructive hover:bg-destructive/10 disabled:opacity-40"
                  title="Remove CIDR"
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* #1009 — the switch that turns the list above into enforcement.
          Separate from the list on purpose: enforcing means retiring the
          appliance's unconditional port-22 floor, which docs/design/
          FLEET_FIREWALL.md §6.1 calls the irreducible recovery channel and
          its risk register names as the mitigation for every OTHER firewall
          mistake. Typing a CIDR should not remove it; deciding to should. */}
      <div
        className={cn(
          "rounded-md border p-3",
          lockdown ? "border-amber-500/50 bg-amber-500/5" : "bg-muted/30",
        )}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-sm font-medium">
              Enforce source restriction
            </div>
            <div className="text-xs text-muted-foreground">
              {lockdown ? (
                <>
                  SSH is reachable only from the networks above, on port 22 and
                  on a moved port alike. The appliance&rsquo;s always-open
                  port-22 floor is retired while this is on.
                </>
              ) : (
                <>
                  Off: SSH is reachable from anywhere and the list above is
                  recorded but not applied. Turning this on retires the
                  always-open port-22 floor &mdash; the recovery channel a wrong
                  CIDR would otherwise leave you. Recover at the console, or by
                  turning this back off here.
                </>
              )}
            </div>
          </div>
          <Toggle
            checked={lockdown}
            onChange={setLockdown}
            disabled={!isSuperadmin || sources.length === 0}
          />
        </div>
        {sources.length === 0 && (
          <div className="mt-2 text-xs text-muted-foreground">
            Add at least one network above first &mdash; enforcing an empty list
            would close SSH from everywhere.
          </div>
        )}
      </div>

      <div className="mt-4 flex items-center gap-3 border-t pt-4">
        <button
          type="button"
          onClick={handleSave}
          disabled={
            !isSuperadmin ||
            !dirty ||
            wouldLockOut ||
            portInvalid ||
            mutation.isPending
          }
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
        >
          {mutation.isPending
            ? "Saving…"
            : savedAt
              ? "Saved!"
              : "Save SSH settings"}
        </button>
        {saveErr && (
          <span className="text-xs text-destructive">
            Failed to save: {saveErr}
          </span>
        )}
      </div>

      <ConfirmModal
        open={confirmLockout !== null}
        title="Enforce SSH restriction from outside it?"
        tone="destructive"
        message={confirmLockout ?? ""}
        confirmLabel="Enforce anyway"
        requireCheckboxLabel="I understand this may close my SSH access to every appliance"
        loading={mutation.isPending}
        onConfirm={() => {
          setConfirmLockout(null);
          setLockdownForced(true);
          // The acknowledgement rides the NEXT save, so re-issue it here
          // rather than asking the operator to press Save again — and read
          // the flag from a local rather than from state, which has not
          // committed yet.
          mutation.mutate({
            ssh_authorized_keys: keys.map((k) => ({
              name: k.name.trim(),
              public_key: k.public_key.trim(),
              comment: k.comment.trim(),
            })),
            ssh_password_auth_enabled: passwordAuth,
            ssh_allow_root_login: allowRoot,
            ssh_port: port,
            ssh_allowed_source_networks: sources
              .map((x) => x.trim())
              .filter(Boolean),
            ssh_lockdown: lockdown,
            ssh_lockdown_force: true,
          });
        }}
        onClose={() => setConfirmLockout(null)}
      />
    </div>
  );
}
