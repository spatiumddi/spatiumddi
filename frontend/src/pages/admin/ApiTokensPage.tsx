import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Eye, EyeOff, Plus, Power, PowerOff, Trash2 } from "lucide-react";
import { apiTokensApi, type ApiToken, type ApiTokenCreated } from "@/lib/api";
import { copyToClipboard } from "@/lib/clipboard";
import { cn, zebraBodyCls } from "@/lib/utils";
import { Modal } from "@/components/ui/modal";

const inputCls =
  "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs font-medium text-muted-foreground">
        {label}
      </label>
      {children}
      {hint && <p className="text-[11px] text-muted-foreground/80">{hint}</p>}
    </div>
  );
}

// ── Create Token Modal ─────────────────────────────────────────────────────

function CreateTokenModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (token: ApiTokenCreated) => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  // Sensible default: 90-day TTL. "Never" is an explicit choice since
  // long-lived bearers are a real security footgun — the UI makes it
  // slightly annoying on purpose.
  const [expiryMode, setExpiryMode] = useState<"days" | "never">("days");
  const [days, setDays] = useState<number>(90);
  const [error, setError] = useState<string | null>(null);

  const mut = useMutation({
    mutationFn: () =>
      apiTokensApi.create({
        name,
        description,
        expires_in_days: expiryMode === "never" ? null : days,
      }),
    onSuccess: (token) => {
      qc.invalidateQueries({ queryKey: ["api-tokens"] });
      onCreated(token);
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      setError(
        typeof detail === "string"
          ? detail
          : detail
            ? JSON.stringify(detail)
            : "Failed to create token.",
      );
    },
  });

  return (
    <Modal title="New API Token" onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          mut.mutate();
        }}
        className="space-y-3"
      >
        <Field
          label="Name"
          hint="Shown in the list — pick something you'll recognise later (e.g. ‘terraform-ci’)."
        >
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. ansible-automation"
            required
            autoFocus
          />
        </Field>
        <Field label="Description (optional)">
          <input
            className={inputCls}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What's this token used for?"
          />
        </Field>
        <Field
          label="Expires"
          hint="Long-lived tokens are a security risk — prefer a date unless you have a reason."
        >
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5 text-sm">
              <input
                type="radio"
                checked={expiryMode === "days"}
                onChange={() => setExpiryMode("days")}
              />
              <span>In</span>
              <input
                type="number"
                min={1}
                max={3650}
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
                disabled={expiryMode !== "days"}
                className={cn(inputCls, "w-20")}
              />
              <span>days</span>
            </label>
            <label className="flex items-center gap-1.5 text-sm">
              <input
                type="radio"
                checked={expiryMode === "never"}
                onChange={() => setExpiryMode("never")}
              />
              Never
            </label>
          </div>
        </Field>
        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
            {error}
          </div>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={!name || mut.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mut.isPending ? "Creating…" : "Create"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

// ── Reveal-once Modal ──────────────────────────────────────────────────────
// Shown after a successful create. The raw token is NEVER retrievable
// again so we force the operator to copy it, and close the modal only
// after explicit confirmation.

function RevealTokenModal({
  token,
  onClose,
}: {
  token: ApiTokenCreated;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [visible, setVisible] = useState(false);

  async function copy() {
    const ok = await copyToClipboard(token.token);
    if (ok) {
      setCopied(true);
      setCopyFailed(false);
      setTimeout(() => setCopied(false), 2000);
    } else {
      // Both paths failed — reveal the value and prompt the user to
      // copy it manually. Don't silently swallow.
      setVisible(true);
      setCopyFailed(true);
    }
  }

  return (
    <Modal title={`Token "${token.name}" — copy now`} onClose={onClose}>
      <div className="space-y-4">
        <div className="rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
          This is the <strong>only time</strong> the raw token is visible.
          SpatiumDDI stores only a hash — if you lose this value, delete the
          token and create a new one.
        </div>
        {copyFailed && (
          <div className="rounded-md border border-rose-500/40 bg-rose-500/5 px-3 py-2 text-xs text-rose-700 dark:text-rose-300">
            Automatic copy failed (browser blocked the clipboard API — usually
            because the page is served over plain HTTP on a non-localhost host).
            The token is now visible below — select it manually and copy with{" "}
            <kbd>Ctrl</kbd>/<kbd>⌘</kbd>+<kbd>C</kbd>.
          </div>
        )}
        <div className="flex items-center gap-2 rounded-md border bg-muted/30 px-3 py-2 font-mono text-sm">
          <code className="flex-1 truncate" title={token.token}>
            {visible ? token.token : "•".repeat(40)}
          </code>
          <button
            type="button"
            onClick={() => setVisible((v) => !v)}
            className="rounded p-1 text-muted-foreground hover:bg-background hover:text-foreground"
            title={visible ? "Hide" : "Reveal"}
          >
            {visible ? (
              <EyeOff className="h-3.5 w-3.5" />
            ) : (
              <Eye className="h-3.5 w-3.5" />
            )}
          </button>
          <button
            type="button"
            onClick={copy}
            className="rounded p-1 text-muted-foreground hover:bg-background hover:text-foreground"
            title="Copy to clipboard"
          >
            <Copy className="h-3.5 w-3.5" />
          </button>
        </div>
        <p className="text-xs text-muted-foreground">
          Use as a Bearer token:{" "}
          <code className="rounded bg-muted px-1 py-0.5 font-mono text-[11px]">
            Authorization: Bearer {token.prefix}…
          </code>
        </p>
        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={onClose}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
          >
            {copied ? "Copied — Done" : "I've copied it — Done"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Main page ──────────────────────────────────────────────────────────────

export function ApiTokensPage() {
  const qc = useQueryClient();
  const { data: tokens = [], isLoading } = useQuery({
    queryKey: ["api-tokens"],
    queryFn: apiTokensApi.list,
  });
  const [showCreate, setShowCreate] = useState(false);
  const [justCreated, setJustCreated] = useState<ApiTokenCreated | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<ApiToken | null>(null);

  const toggleMut = useMutation({
    mutationFn: (t: ApiToken) =>
      apiTokensApi.update(t.id, { is_active: !t.is_active }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["api-tokens"] }),
  });
  const deleteMut = useMutation({
    mutationFn: (id: string) => apiTokensApi.delete(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["api-tokens"] });
      setConfirmDelete(null);
    },
  });

  return (
    <div className="h-full overflow-auto p-6">
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-2xl font-bold tracking-tight">API Tokens</h1>
            <p className="mt-1 text-xs text-muted-foreground">
              Long-lived bearer credentials for scripts and automation. Each
              token inherits its owner's permissions.
            </p>
          </div>
          <button
            onClick={() => setShowCreate(true)}
            className="flex flex-shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
          >
            <Plus className="h-3.5 w-3.5" />
            New Token
          </button>
        </div>

        <div className="rounded-lg border bg-card">
          {isLoading ? (
            <p className="px-4 py-6 text-sm text-muted-foreground">Loading…</p>
          ) : tokens.length === 0 ? (
            <p className="px-4 py-6 text-sm text-muted-foreground">
              No tokens yet. Create one to use SpatiumDDI's REST API from
              scripts or CI.
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead className="border-b text-left text-xs font-medium uppercase tracking-wider text-muted-foreground">
                <tr>
                  <th className="px-3 py-2">Name</th>
                  <th className="px-3 py-2">Prefix</th>
                  <th className="px-3 py-2">Expires</th>
                  <th className="px-3 py-2">Last Used</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className={zebraBodyCls}>
                {tokens.map((t) => (
                  <tr key={t.id} className="border-b last:border-0">
                    <td className="px-3 py-2">
                      <div className="font-medium">{t.name}</div>
                      {t.description && (
                        <div
                          className="truncate text-xs text-muted-foreground"
                          title={t.description}
                        >
                          {t.description}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">{t.prefix}…</td>
                    <td className="px-3 py-2 text-xs">
                      {t.expires_at ? (
                        <ExpiryCell iso={t.expires_at} />
                      ) : (
                        <span className="text-amber-600 dark:text-amber-400">
                          Never
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">
                      {t.last_used_at
                        ? new Date(t.last_used_at).toLocaleString()
                        : "—"}
                    </td>
                    <td className="px-3 py-2">
                      <span
                        className={cn(
                          "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium",
                          t.is_active
                            ? "bg-emerald-500/15 text-emerald-600"
                            : "bg-muted text-muted-foreground",
                        )}
                      >
                        {t.is_active ? "Active" : "Revoked"}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex justify-end gap-1">
                        <button
                          onClick={() => toggleMut.mutate(t)}
                          className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                          title={t.is_active ? "Revoke" : "Re-enable"}
                        >
                          {t.is_active ? (
                            <PowerOff className="h-3.5 w-3.5" />
                          ) : (
                            <Power className="h-3.5 w-3.5" />
                          )}
                        </button>
                        <button
                          onClick={() => setConfirmDelete(t)}
                          className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                          title="Delete"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {showCreate && (
          <CreateTokenModal
            onClose={() => setShowCreate(false)}
            onCreated={(t) => {
              setShowCreate(false);
              setJustCreated(t);
            }}
          />
        )}
        {justCreated && (
          <RevealTokenModal
            token={justCreated}
            onClose={() => setJustCreated(null)}
          />
        )}
        {confirmDelete && (
          <Modal
            title="Delete API Token"
            onClose={() => setConfirmDelete(null)}
          >
            <div className="space-y-4">
              <p className="text-sm text-muted-foreground">
                Permanently delete{" "}
                <strong className="text-foreground">
                  {confirmDelete.name}
                </strong>
                ? Any script still using it will start getting 401 responses.
              </p>
              <div className="flex justify-end gap-2">
                <button
                  onClick={() => setConfirmDelete(null)}
                  className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
                >
                  Cancel
                </button>
                <button
                  onClick={() => deleteMut.mutate(confirmDelete.id)}
                  disabled={deleteMut.isPending}
                  className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
                >
                  {deleteMut.isPending ? "Deleting…" : "Delete"}
                </button>
              </div>
            </div>
          </Modal>
        )}
      </div>
    </div>
  );
}

function ExpiryCell({ iso }: { iso: string }) {
  const expires = new Date(iso);
  const now = Date.now();
  const daysOut = Math.round((expires.getTime() - now) / (24 * 60 * 60 * 1000));
  let cls = "";
  if (daysOut < 0) cls = "text-destructive";
  else if (daysOut < 7) cls = "text-amber-600 dark:text-amber-400";
  else cls = "text-muted-foreground";
  return (
    <span className={cls} title={expires.toLocaleString()}>
      {daysOut < 0
        ? `Expired ${-daysOut}d ago`
        : daysOut === 0
          ? "Expires today"
          : `In ${daysOut}d`}
    </span>
  );
}
