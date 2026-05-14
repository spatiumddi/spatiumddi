import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  Copy,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";

import {
  appliancePairingApi,
  authApi,
  dhcpApi,
  dnsApi,
  type PairingCodeCreated,
  type PairingCodeRow,
  type PairingDeploymentKind,
} from "@/lib/api";
import { Modal } from "@/components/ui/modal";
import { ConfirmModal } from "@/components/ui/confirm-modal";
import { cn } from "@/lib/utils";

/**
 * Appliance → Pairing codes tab (issue #169).
 *
 * Operator clicks "New pairing code" → modal lets them pick the agent
 * kind (DNS / DHCP), optionally pin a server group + expiry, click
 * "Generate" → the cleartext 8-digit code is shown in a large mono
 * box with a copy button + live countdown. The same modal carries an
 * inline "Regenerate" button so an operator who left and came back
 * just mints another code without re-opening anything.
 *
 * The table below shows every code (pending + recent terminal rows).
 * Polls every 5 s while the tab is open so a freshly-claimed code's
 * row state flips from "pending" → "claimed" without the operator
 * having to refresh.
 */

const EXPIRY_OPTIONS = [
  { value: 5, label: "5 minutes" },
  { value: 15, label: "15 minutes" },
  { value: 30, label: "30 minutes" },
  { value: 60, label: "1 hour" },
];

const inputCls =
  "rounded-md border bg-background px-3 py-1.5 text-sm disabled:opacity-60";

// Shared kind → human label map for chips + confirm prose.
const KIND_LABEL: Record<PairingDeploymentKind, string> = {
  dns: "DNS",
  dhcp: "DHCP",
  both: "DNS + DHCP",
};

// Card-style radio options for the Agent kind picker — three pinned
// values map 1:1 to the API's deployment_kind. Lives at module scope
// so it isn't rebuilt on every modal render.
const KIND_OPTIONS: {
  value: PairingDeploymentKind;
  title: string;
  subtitle: string;
}[] = [
  { value: "dns", title: "DNS", subtitle: "BIND9 / PowerDNS" },
  { value: "dhcp", title: "DHCP", subtitle: "Kea" },
  { value: "both", title: "DNS + DHCP", subtitle: "BIND9 + Kea, one box" },
];

export function PairingTab() {
  const qc = useQueryClient();
  const { data: me } = useQuery({
    queryKey: ["me"],
    queryFn: authApi.me,
    staleTime: 60_000,
  });
  const isSuperadmin = me?.is_superadmin ?? false;

  // Codes table refresh — 5 s poll while the tab is mounted picks up
  // claims and expiries without the operator clicking Refresh.
  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["appliance", "pairing-codes"],
    queryFn: () => appliancePairingApi.list({ include_terminal: true }),
    refetchInterval: 5_000,
    enabled: isSuperadmin,
  });

  const [modalOpen, setModalOpen] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<PairingCodeRow | null>(null);
  const revokeMutation = useMutation({
    mutationFn: (id: string) => appliancePairingApi.revoke(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "pairing-codes"] });
      setRevokeTarget(null);
    },
  });

  if (!isSuperadmin) {
    return (
      <div className="mx-auto max-w-4xl">
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-4 text-sm">
          <div className="flex items-start gap-2">
            <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-700 dark:text-amber-400" />
            <div>
              <p className="font-medium text-amber-700 dark:text-amber-400">
                Superadmin only
              </p>
              <p className="mt-1 text-muted-foreground">
                Pairing codes hand out agent bootstrap secrets, so only
                superadmin accounts can mint or list them. Ask your platform
                admin if you need to register a new appliance.
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const codes = data?.codes ?? [];

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold">Appliance pairing codes</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Short-lived 8-digit codes the agent installer swaps for the real{" "}
            <code className="rounded bg-muted px-1">DNS_AGENT_KEY</code> /{" "}
            <code className="rounded bg-muted px-1">DHCP_AGENT_KEY</code> on
            first boot. Default 15-minute expiry, single-use. The cleartext code
            is shown exactly once at generation; this table only ever displays
            the last two digits for visual correlation.
          </p>
        </div>
        <div className="flex flex-shrink-0 gap-2">
          <button
            type="button"
            onClick={() => refetch()}
            className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            disabled={isFetching}
            title="Refresh"
          >
            <RefreshCw
              className={cn("h-3.5 w-3.5", isFetching && "animate-spin")}
            />
            Refresh
          </button>
          <button
            type="button"
            onClick={() => setModalOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          >
            <Plus className="h-3.5 w-3.5" />
            New pairing code
          </button>
        </div>
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading pairing codes…
        </div>
      ) : codes.length === 0 ? (
        <div className="rounded-md border bg-muted/30 p-6 text-center text-sm text-muted-foreground">
          No pairing codes yet. Click{" "}
          <span className="font-medium">New pairing code</span> to mint one.
        </div>
      ) : (
        <CodesTable codes={codes} onRevoke={(row) => setRevokeTarget(row)} />
      )}

      {modalOpen && (
        <GenerateCodeModal
          onClose={() => setModalOpen(false)}
          onCreated={() => {
            qc.invalidateQueries({ queryKey: ["appliance", "pairing-codes"] });
          }}
        />
      )}

      <ConfirmModal
        open={revokeTarget !== null}
        title="Revoke pairing code?"
        message={
          revokeTarget ? (
            <>
              Revoke the pending{" "}
              <span className="font-mono">••{revokeTarget.code_last_two}</span>{" "}
              {KIND_LABEL[revokeTarget.deployment_kind]} code? Any agent still
              holding it will get a generic "invalid code" response on its next
              pair attempt — they'll need a fresh code.
            </>
          ) : (
            ""
          )
        }
        confirmLabel="Revoke"
        tone="destructive"
        loading={revokeMutation.isPending}
        onConfirm={() => {
          if (revokeTarget) revokeMutation.mutate(revokeTarget.id);
        }}
        onClose={() => setRevokeTarget(null)}
      />
    </div>
  );
}

// ── Active codes table ─────────────────────────────────────────────

function CodesTable({
  codes,
  onRevoke,
}: {
  codes: PairingCodeRow[];
  onRevoke: (row: PairingCodeRow) => void;
}) {
  // Re-render every second so the "expires in" countdown stays live —
  // without this the column would stay frozen at the value it had on
  // last query refresh.
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-sm">
        <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-2">Code</th>
            <th className="px-3 py-2">Kind</th>
            <th className="px-3 py-2">Group</th>
            <th className="px-3 py-2">State</th>
            <th className="px-3 py-2">Note</th>
            <th className="px-3 py-2">Created</th>
            <th className="px-3 py-2">Expires / claimed</th>
            <th className="px-3 py-2"></th>
          </tr>
        </thead>
        <tbody>
          {codes.map((row) => (
            <CodeRow key={row.id} row={row} onRevoke={onRevoke} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CodeRow({
  row,
  onRevoke,
}: {
  row: PairingCodeRow;
  onRevoke: (row: PairingCodeRow) => void;
}) {
  const isPending = row.state === "pending";
  return (
    <tr className="border-t hover:bg-muted/20">
      <td className="px-3 py-2 font-mono text-xs">••••••{row.code_last_two}</td>
      <td className="px-3 py-2">
        <KindChip kind={row.deployment_kind} />
      </td>
      <td className="px-3 py-2 text-xs">
        {row.server_group_name || (
          <span className="text-muted-foreground">—</span>
        )}
      </td>
      <td className="px-3 py-2">
        <StateChip state={row.state} />
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {row.note || "—"}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {formatRelative(row.created_at)}
      </td>
      <td className="px-3 py-2 text-xs">
        {row.state === "pending" ? (
          <CountdownCell expiresAt={row.expires_at} />
        ) : row.state === "claimed" && row.used_at ? (
          <span className="text-emerald-700 dark:text-emerald-300">
            claimed {formatRelative(row.used_at)}
            {row.used_by_hostname && (
              <span className="ml-1 text-muted-foreground">
                · {row.used_by_hostname}
              </span>
            )}
          </span>
        ) : row.state === "revoked" && row.revoked_at ? (
          <span className="text-muted-foreground">
            revoked {formatRelative(row.revoked_at)}
          </span>
        ) : (
          <span className="text-muted-foreground">
            expired {formatRelative(row.expires_at)}
          </span>
        )}
      </td>
      <td className="px-3 py-2 text-right">
        {isPending && (
          <button
            type="button"
            onClick={() => onRevoke(row)}
            className="rounded p-1 text-destructive hover:bg-destructive/10"
            title="Revoke code"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </td>
    </tr>
  );
}

function KindChip({ kind }: { kind: PairingDeploymentKind }) {
  // 'both' gets emerald to telegraph "more than one service" at a
  // glance — visually distinct from the per-service blue / purple.
  const styles: Record<PairingDeploymentKind, string> = {
    dns: "bg-blue-500/10 text-blue-700 dark:text-blue-300",
    dhcp: "bg-purple-500/10 text-purple-700 dark:text-purple-300",
    both: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  };
  return (
    <span
      className={cn(
        "rounded-md px-1.5 py-0.5 text-xs font-medium",
        styles[kind],
      )}
    >
      {KIND_LABEL[kind]}
    </span>
  );
}

function StateChip({ state }: { state: PairingCodeRow["state"] }) {
  const styles: Record<PairingCodeRow["state"], string> = {
    pending: "bg-blue-500/10 text-blue-700 dark:text-blue-300",
    claimed: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
    expired: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
    revoked: "bg-zinc-500/10 text-muted-foreground",
  };
  return (
    <span
      className={cn(
        "rounded-md px-1.5 py-0.5 text-xs font-medium",
        styles[state],
      )}
    >
      {state}
    </span>
  );
}

function CountdownCell({ expiresAt }: { expiresAt: string }) {
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (ms <= 0)
    return (
      <span className="text-amber-600 dark:text-amber-400">expiring…</span>
    );
  const totalSec = Math.floor(ms / 1000);
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  const pad = (n: number) => n.toString().padStart(2, "0");
  return (
    <span className="inline-flex items-center gap-1 font-mono">
      <Clock className="h-3 w-3 text-muted-foreground" />
      {mins}:{pad(secs)}
    </span>
  );
}

function formatRelative(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return "just now";
  const mins = Math.floor(ms / 60_000);
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} h ago`;
  const days = Math.floor(hrs / 24);
  return `${days} d ago`;
}

// Card-style radio button for the Agent kind picker. The whole card
// is clickable; selected state gets a primary border + subtle bg
// tint so it's obvious at a glance which kind is active.
function KindRadio({
  value,
  title,
  subtitle,
  checked,
  onChange,
}: {
  value: PairingDeploymentKind;
  title: string;
  subtitle: string;
  checked: boolean;
  onChange: () => void;
}) {
  return (
    <label
      className={cn(
        "flex cursor-pointer items-start gap-2 rounded-md border p-2.5 text-sm transition-colors",
        checked
          ? "border-primary bg-primary/5"
          : "border-input hover:bg-muted/50",
      )}
    >
      <input
        type="radio"
        name="deployment_kind"
        value={value}
        checked={checked}
        onChange={onChange}
        className="mt-0.5 cursor-pointer"
      />
      <div className="min-w-0">
        <div className="font-medium leading-tight">{title}</div>
        <div className="text-xs text-muted-foreground">{subtitle}</div>
      </div>
    </label>
  );
}

// ── Generate-code modal ────────────────────────────────────────────

function GenerateCodeModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const [deploymentKind, setDeploymentKind] =
    useState<PairingDeploymentKind>("dns");
  const [serverGroupId, setServerGroupId] = useState<string>("");
  const [expiresInMinutes, setExpiresInMinutes] = useState<number>(15);
  const [note, setNote] = useState<string>("");
  const [generated, setGenerated] = useState<PairingCodeCreated | null>(null);
  const [copied, setCopied] = useState<boolean>(false);
  const copiedTimer = useRef<number | null>(null);

  const { data: dnsGroups } = useQuery({
    queryKey: ["dns-groups"],
    queryFn: dnsApi.listGroups,
    staleTime: 60_000,
  });
  const { data: dhcpGroups } = useQuery({
    queryKey: ["dhcp-groups"],
    queryFn: dhcpApi.listGroups,
    staleTime: 60_000,
  });

  // 'both' codes can't pre-assign a group (one column would have to
  // carry either a DNS or DHCP group id ambiguously). Empty list keeps
  // the dropdown rendered but disabled.
  const groupOptions = useMemo(() => {
    if (deploymentKind === "dns") return dnsGroups ?? [];
    if (deploymentKind === "dhcp") return dhcpGroups ?? [];
    return [];
  }, [deploymentKind, dnsGroups, dhcpGroups]);
  const groupPickerDisabled = deploymentKind === "both";

  // Reset the group selection whenever the kind changes — a DNS group
  // id makes no sense paired with a DHCP code, and 'both' rejects any
  // group entirely.
  useEffect(() => {
    setServerGroupId("");
  }, [deploymentKind]);

  const createMutation = useMutation({
    mutationFn: () =>
      appliancePairingApi.create({
        deployment_kind: deploymentKind,
        // ``both`` ignores server_group_id at the API layer; sending
        // null keeps the wire shape clean.
        server_group_id: groupPickerDisabled ? null : serverGroupId || null,
        expires_in_minutes: expiresInMinutes,
        note: note.trim() || null,
      }),
    onSuccess: (result) => {
      setGenerated(result);
      onCreated();
    },
  });

  async function handleCopy() {
    if (!generated) return;
    try {
      await navigator.clipboard.writeText(generated.code);
      setCopied(true);
      if (copiedTimer.current) window.clearTimeout(copiedTimer.current);
      copiedTimer.current = window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard may be blocked on insecure contexts. The big mono
      // box stays selectable — operator can select-and-copy manually.
    }
  }

  function handleRegenerate() {
    setGenerated(null);
    setCopied(false);
    // Pre-fill stays — operator just wants another code with the same
    // shape. The create button below re-triggers the mutation.
  }

  const errMsg =
    createMutation.error instanceof Error
      ? createMutation.error.message
      : createMutation.error
        ? String(createMutation.error)
        : null;

  return (
    <Modal title="New pairing code" onClose={onClose}>
      <div className="space-y-4">
        {generated ? (
          <GeneratedView
            generated={generated}
            copied={copied}
            onCopy={handleCopy}
            onRegenerate={handleRegenerate}
            onClose={onClose}
          />
        ) : (
          <>
            <div className="space-y-3 text-sm">
              <fieldset>
                <legend className="text-xs font-medium text-muted-foreground">
                  Agent kind
                </legend>
                <div className="mt-1 grid gap-2 sm:grid-cols-3">
                  {KIND_OPTIONS.map((opt) => (
                    <KindRadio
                      key={opt.value}
                      value={opt.value}
                      title={opt.title}
                      subtitle={opt.subtitle}
                      checked={deploymentKind === opt.value}
                      onChange={() => setDeploymentKind(opt.value)}
                    />
                  ))}
                </div>
              </fieldset>

              <label className="block">
                <span className="text-xs font-medium text-muted-foreground">
                  Pre-assign group (optional)
                </span>
                <select
                  value={serverGroupId}
                  onChange={(e) => setServerGroupId(e.target.value)}
                  disabled={groupPickerDisabled}
                  className={cn(inputCls, "mt-1 w-full")}
                >
                  <option value="">— No pre-assignment —</option>
                  {groupOptions.map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.name}
                    </option>
                  ))}
                </select>
                <p className="mt-1 text-xs text-muted-foreground">
                  {groupPickerDisabled
                    ? "Combined DNS + DHCP codes don't support pre-assignment — set the agent's per-service groups through the existing DNS / DHCP UI after it registers."
                    : "When set, the agent joins this group directly on first contact instead of landing in the default group."}
                </p>
              </label>

              <label className="block">
                <span className="text-xs font-medium text-muted-foreground">
                  Expires in
                </span>
                <select
                  value={expiresInMinutes}
                  onChange={(e) => setExpiresInMinutes(Number(e.target.value))}
                  className={cn(inputCls, "mt-1 w-full")}
                >
                  {EXPIRY_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              </label>

              <label className="block">
                <span className="text-xs font-medium text-muted-foreground">
                  Note (optional)
                </span>
                <input
                  type="text"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder="e.g. for dns-west-2"
                  maxLength={255}
                  className={cn(inputCls, "mt-1 w-full")}
                />
              </label>
            </div>

            {errMsg && (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
                {errMsg}
              </div>
            )}

            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={onClose}
                className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => createMutation.mutate()}
                disabled={createMutation.isPending}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {createMutation.isPending ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    Generating…
                  </>
                ) : (
                  "Generate code"
                )}
              </button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

function GeneratedView({
  generated,
  copied,
  onCopy,
  onRegenerate,
  onClose,
}: {
  generated: PairingCodeCreated;
  copied: boolean;
  onCopy: () => void;
  onRegenerate: () => void;
  onClose: () => void;
}) {
  return (
    <div className="space-y-4">
      <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-xs">
        <div className="flex items-start gap-2">
          <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0 text-emerald-700 dark:text-emerald-300" />
          <div className="space-y-1">
            <p className="font-medium text-emerald-700 dark:text-emerald-300">
              Code generated
            </p>
            <p className="text-muted-foreground">
              Note it down or copy it — it is shown only this once. Anyone with
              this code can claim{" "}
              {generated.deployment_kind === "both"
                ? "both DNS + DHCP agent bootstrap keys"
                : "one agent bootstrap key"}
              .
            </p>
          </div>
        </div>
      </div>

      <div className="flex items-center justify-between gap-2 rounded-md border bg-muted/30 px-4 py-3">
        <span className="select-all text-3xl font-mono tracking-[0.2em]">
          {generated.code}
        </span>
        <button
          type="button"
          onClick={onCopy}
          className="inline-flex items-center gap-1.5 rounded-md border bg-background px-2 py-1 text-xs hover:bg-muted"
        >
          {copied ? (
            <>
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
              Copied
            </>
          ) : (
            <>
              <Copy className="h-3.5 w-3.5" />
              Copy
            </>
          )}
        </button>
      </div>

      <ExpiryCountdown expiresAt={generated.expires_at} />

      <p className="text-xs text-muted-foreground">
        On the agent appliance, hit{" "}
        <code className="rounded bg-muted px-1">
          POST /api/v1/appliance/pair
        </code>{" "}
        with{" "}
        <code className="rounded bg-muted px-1">
          {"{"}"code": "{generated.code}", "hostname": "&lt;name&gt;"{"}"}
        </code>{" "}
        to swap the code for the real {KIND_LABEL[generated.deployment_kind]}{" "}
        bootstrap {generated.deployment_kind === "both" ? "keys" : "key"}. The
        installer wizard prompt for this lands in Phase 4.
      </p>

      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onRegenerate}
          className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          Generate another
        </button>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90"
        >
          Done
        </button>
      </div>
    </div>
  );
}

function ExpiryCountdown({ expiresAt }: { expiresAt: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const ms = new Date(expiresAt).getTime() - now;
  if (ms <= 0) {
    return (
      <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
        This code has expired. Generate another.
      </div>
    );
  }
  const totalSec = Math.floor(ms / 1000);
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  const pad = (n: number) => n.toString().padStart(2, "0");
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <Clock className="h-3.5 w-3.5" />
      Expires in{" "}
      <span className="font-mono">
        {mins}:{pad(secs)}
      </span>
    </div>
  );
}
