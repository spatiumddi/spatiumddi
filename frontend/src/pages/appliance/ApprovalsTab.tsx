import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  HardDrive,
  KeyRound,
  Loader2,
  Network,
  Power,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  ShieldQuestion,
  Trash2,
  Upload,
  XCircle,
} from "lucide-react";

import {
  applianceApprovalApi,
  applianceSlotImagesApi,
  authApi,
  dhcpApi,
  dnsApi,
  type ApplianceRow,
  type ApplianceState,
  type SlotImage,
  type SupervisorCapabilities,
} from "@/lib/api";
import { Modal } from "@/components/ui/modal";
import { ConfirmModal } from "@/components/ui/confirm-modal";
import { cn } from "@/lib/utils";
import { PairingTab } from "./PairingTab";

/**
 * Appliance → Fleet tab (#170 Wave D1; supersedes Wave B3 "Approvals").
 *
 * Supervisors that claimed a pairing code show up here. Pending rows
 * pin at the top with Approve / Reject; approved rows render capability
 * chips + cert metadata + Re-key / Delete actions. Clicking any row
 * opens the drilldown modal carrying the full capabilities block,
 * cert serial, fingerprint, and audit metadata.
 *
 * Adaptive polling — 2 s while at least one pending row exists (so a
 * fresh supervisor registration appears within seconds of the operator
 * paging the admin to approve), 15 s otherwise.
 */

const inputCls =
  "rounded-md border bg-background px-3 py-1.5 text-sm disabled:opacity-60";

function stateBadge(state: ApplianceState): {
  label: string;
  className: string;
  Icon: typeof ShieldCheck;
} {
  if (state === "approved") {
    return {
      label: "approved",
      className:
        "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/30",
      Icon: ShieldCheck,
    };
  }
  if (state === "rejected") {
    return {
      label: "rejected",
      className:
        "bg-rose-500/10 text-rose-700 dark:text-rose-400 border-rose-500/30",
      Icon: ShieldAlert,
    };
  }
  return {
    label: "pending",
    className:
      "bg-amber-500/10 text-amber-700 dark:text-amber-400 border-amber-500/40",
    Icon: ShieldQuestion,
  };
}

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return "<1m ago";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}

function shortFingerprint(fp: string | null | undefined): string {
  if (!fp) return "—";
  return fp.length > 16 ? `${fp.slice(0, 8)}…${fp.slice(-6)}` : fp;
}

// Compact capability chips for the row. Each chip lights up only when
// the supervisor advertised that capability. has_baked_images is shown
// as a separate badge because operationally it's a deployment-mode
// signal (air-gap-ready) rather than a service capability.
function capabilityChips(caps: SupervisorCapabilities): {
  key: string;
  label: string;
}[] {
  const out: { key: string; label: string }[] = [];
  if (caps.can_run_dns_bind9) out.push({ key: "bind9", label: "BIND9" });
  if (caps.can_run_dns_powerdns)
    out.push({ key: "powerdns", label: "PowerDNS" });
  if (caps.can_run_dhcp) out.push({ key: "dhcp", label: "DHCP" });
  if (caps.can_run_observer) out.push({ key: "observer", label: "Observer" });
  return out;
}

export function ApprovalsTab() {
  const qc = useQueryClient();
  const { data: me } = useQuery({
    queryKey: ["me"],
    queryFn: authApi.me,
    staleTime: 60_000,
  });
  const isSuperadmin = me?.is_superadmin ?? false;

  const { data, isLoading, isFetching, refetch, error } = useQuery({
    queryKey: ["appliance", "approvals"],
    queryFn: applianceApprovalApi.list,
    refetchInterval: (query) => {
      const rows = query.state.data ?? [];
      const hasPending = rows.some((r) => r.state === "pending_approval");
      return hasPending ? 2_000 : 15_000;
    },
    enabled: isSuperadmin,
  });

  const [drilldown, setDrilldown] = useState<ApplianceRow | null>(null);
  const [rejectTarget, setRejectTarget] = useState<ApplianceRow | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ApplianceRow | null>(null);
  const [rekeyTarget, setRekeyTarget] = useState<ApplianceRow | null>(null);

  const approve = useMutation({
    mutationFn: (id: string) => applianceApprovalApi.approve(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "approvals"] });
    },
  });
  const reject = useMutation({
    mutationFn: (id: string) => applianceApprovalApi.reject(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "approvals"] });
      setRejectTarget(null);
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => applianceApprovalApi.remove(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "approvals"] });
      setDeleteTarget(null);
      setDrilldown(null);
    },
  });
  const rekey = useMutation({
    mutationFn: (id: string) => applianceApprovalApi.rekey(id),
    onSuccess: (row) => {
      qc.invalidateQueries({ queryKey: ["appliance", "approvals"] });
      setRekeyTarget(null);
      // Refresh the drilldown view if it's open on this row so the
      // operator sees the new serial + expiry immediately.
      if (drilldown && drilldown.id === row.id) setDrilldown(row);
    },
  });

  const rows = useMemo(() => data ?? [], [data]);
  const pending = rows.filter((r) => r.state === "pending_approval");
  const others = rows.filter((r) => r.state !== "pending_approval");

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
                Approving an appliance signs an X.509 cert against the
                supervisor's submitted Ed25519 pubkey. Only superadmin accounts
                can approve / reject / re-key. Ask your platform admin if a
                fleet appliance is waiting on approval.
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-6xl">
      <div className="mb-4 flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold">Appliance fleet</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            One screen for the full Application appliance lifecycle — mint
            pairing codes, approve / reject incoming supervisors, assign roles,
            schedule OS upgrades + reboots, and re-key or delete approved
            appliances. Supervisors submit their Ed25519 pubkey on{" "}
            <code>/api/v1/appliance/supervisor/register</code> after claiming a
            code; rows appear below in <code>pending_approval</code> until a
            superadmin signs the cert with the control plane&apos;s internal CA.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={() => refetch()}
            disabled={isFetching}
            className="inline-flex items-center gap-1.5 rounded-md border bg-background px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
          >
            <RefreshCw
              className={cn("h-3.5 w-3.5", isFetching && "animate-spin")}
            />
            Refresh
          </button>
        </div>
      </div>

      {/* Folded sections (#170 follow-up): pairing-code management
          + slot-image uploads (air-gap) sit above the appliance
          table, collapsed by default so the fleet table is the
          first thing operators see. */}
      <CollapsibleSection
        title="Pairing codes"
        description="Mint codes that new Application appliances claim during install."
        storageKey="fleet.pairing.expanded"
      >
        <PairingTab />
      </CollapsibleSection>

      <CollapsibleSection
        title="Slot image uploads"
        description="Air-gap support — upload .raw.xz slot images for offline appliance upgrades."
        storageKey="fleet.slotImages.expanded"
      >
        <SlotImageManager />
      </CollapsibleSection>

      <h3 className="mt-4 mb-2 text-sm font-semibold">
        Application appliances
      </h3>

      {error ? (
        <div className="rounded-md border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-700 dark:text-rose-300">
          Failed to load appliances: {(error as Error).message}
        </div>
      ) : isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading appliances…
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-md border border-dashed bg-card p-8 text-center text-sm text-muted-foreground">
          No appliances have paired yet. Mint a pairing code on the Pairing tab
          and install an Application appliance against it — the row appears here
          once the supervisor claims the code.
        </div>
      ) : (
        <div className="overflow-hidden rounded-md border bg-card">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Hostname</th>
                <th className="px-3 py-2 text-left font-medium">State</th>
                <th className="px-3 py-2 text-left font-medium">
                  Capabilities
                </th>
                <th className="px-3 py-2 text-left font-medium">Fingerprint</th>
                <th className="px-3 py-2 text-left font-medium">Paired</th>
                <th className="px-3 py-2 text-left font-medium">Last seen</th>
                <th className="px-3 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {/* Pending pinned at the top — operators come to this tab to
                  action the queue first, then check status of approved
                  rows. Same visual sort as the existing FleetTab. */}
              {pending.map((row) => (
                <ApplianceTableRow
                  key={row.id}
                  row={row}
                  highlight
                  busy={approve.isPending && approve.variables === row.id}
                  onOpen={() => setDrilldown(row)}
                  onApprove={() => approve.mutate(row.id)}
                  onReject={() => setRejectTarget(row)}
                  onRekey={() => setRekeyTarget(row)}
                  onDelete={() => setDeleteTarget(row)}
                />
              ))}
              {others.map((row) => (
                <ApplianceTableRow
                  key={row.id}
                  row={row}
                  busy={rekey.isPending && rekey.variables === row.id}
                  onOpen={() => setDrilldown(row)}
                  onApprove={() => approve.mutate(row.id)}
                  onReject={() => setRejectTarget(row)}
                  onRekey={() => setRekeyTarget(row)}
                  onDelete={() => setDeleteTarget(row)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {drilldown && (
        <ApplianceDrilldownModal
          row={drilldown}
          onClose={() => setDrilldown(null)}
          onApprove={() => approve.mutate(drilldown.id)}
          approving={approve.isPending}
          onReject={() => setRejectTarget(drilldown)}
          onRekey={() => setRekeyTarget(drilldown)}
          onDelete={() => setDeleteTarget(drilldown)}
        />
      )}

      {rejectTarget && (
        <ConfirmModal
          open
          title="Reject appliance?"
          message={
            <>
              <p className="text-sm">
                Reject <strong>{rejectTarget.hostname}</strong>? The row is
                deleted; the supervisor's next poll returns 403 and it falls
                back to bootstrapping. To re-pair, mint a fresh pairing code and
                re-install or re-trigger the supervisor.
              </p>
              <p className="mt-2 text-xs text-muted-foreground">
                Fingerprint:{" "}
                <code className="text-foreground">
                  {shortFingerprint(rejectTarget.public_key_fingerprint)}
                </code>
              </p>
            </>
          }
          confirmLabel="Reject"
          tone="destructive"
          loading={reject.isPending}
          onConfirm={() => reject.mutate(rejectTarget.id)}
          onClose={() => setRejectTarget(null)}
        />
      )}

      {deleteTarget && (
        <ConfirmModal
          open
          title="Delete appliance?"
          message={
            <>
              <p className="text-sm">
                Permanently remove <strong>{deleteTarget.hostname}</strong> from
                the fleet. The supervisor's mTLS calls will fail (cert chain
                still valid but no matching DB row); the supervisor falls back
                to bootstrapping and needs a fresh pairing code to re-join.
              </p>
              <p className="mt-2 text-xs text-muted-foreground">
                Cert serial:{" "}
                <code className="text-foreground">
                  {deleteTarget.cert_serial ?? "—"}
                </code>
              </p>
            </>
          }
          confirmLabel="Delete"
          tone="destructive"
          loading={remove.isPending}
          onConfirm={() => remove.mutate(deleteTarget.id)}
          onClose={() => setDeleteTarget(null)}
        />
      )}

      {rekeyTarget && (
        <ConfirmModal
          open
          title="Re-key appliance?"
          message={
            <>
              <p className="text-sm">
                Issue a fresh cert against the supervisor's existing pubkey on{" "}
                <strong>{rekeyTarget.hostname}</strong>. The current cert
                remains technically valid in the CA's eye until it expires (CRL
                work lands in a later wave); the supervisor picks up the new
                cert on its next poll.
              </p>
              <p className="mt-2 text-xs text-muted-foreground">
                Use this for the routine 60-day renewal or after suspected
                compromise.
              </p>
              <p className="mt-2 text-xs text-muted-foreground">
                Current serial:{" "}
                <code className="text-foreground">
                  {rekeyTarget.cert_serial ?? "—"}
                </code>
              </p>
            </>
          }
          confirmLabel="Re-key"
          loading={rekey.isPending}
          onConfirm={() => rekey.mutate(rekeyTarget.id)}
          onClose={() => setRekeyTarget(null)}
        />
      )}
    </div>
  );
}

function ApplianceTableRow({
  row,
  highlight,
  busy,
  onOpen,
  onApprove,
  onReject,
  onRekey,
  onDelete,
}: {
  row: ApplianceRow;
  highlight?: boolean;
  busy?: boolean;
  onOpen: () => void;
  onApprove: () => void;
  onReject: () => void;
  onRekey: () => void;
  onDelete: () => void;
}) {
  const badge = stateBadge(row.state);
  const Icon = badge.Icon;
  const caps = capabilityChips(row.capabilities);

  return (
    <tr
      className={cn(
        "cursor-pointer hover:bg-muted/30",
        highlight && "bg-amber-500/5",
      )}
      onClick={onOpen}
    >
      <td className="px-3 py-2">
        <div className="font-medium">{row.hostname}</div>
        {row.supervisor_version && (
          <div className="text-xs text-muted-foreground">
            supervisor {row.supervisor_version}
          </div>
        )}
      </td>
      <td className="px-3 py-2">
        <span
          className={cn(
            "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs",
            badge.className,
          )}
        >
          <Icon className="h-3 w-3" /> {badge.label}
        </span>
      </td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap gap-1">
          {caps.length === 0 ? (
            <span className="text-xs text-muted-foreground">—</span>
          ) : (
            caps.map((c) => (
              <span
                key={c.key}
                className="rounded-full bg-muted px-1.5 py-0.5 font-mono text-[10px]"
              >
                {c.label}
              </span>
            ))
          )}
          {row.capabilities.has_baked_images && (
            <span
              className="rounded-full bg-sky-500/10 px-1.5 py-0.5 font-mono text-[10px] text-sky-700 dark:text-sky-300"
              title="Supervisor reports baked container images on the rootfs — air-gap-ready."
            >
              baked
            </span>
          )}
        </div>
      </td>
      <td className="px-3 py-2 font-mono text-xs">
        {shortFingerprint(row.public_key_fingerprint)}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {relativeTime(row.paired_at)}
        {row.paired_from_ip && (
          <div className="font-mono">{row.paired_from_ip}</div>
        )}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {relativeTime(row.last_seen_at)}
      </td>
      <td className="px-3 py-2 text-right" onClick={(e) => e.stopPropagation()}>
        <div className="inline-flex items-center gap-1">
          {row.state === "pending_approval" && (
            <>
              <button
                type="button"
                onClick={onApprove}
                disabled={busy}
                className="inline-flex items-center gap-1 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-500/20 disabled:opacity-50 dark:text-emerald-300"
                title="Approve + sign the supervisor's cert"
              >
                {busy ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <CheckCircle2 className="h-3 w-3" />
                )}
                Approve
              </button>
              <button
                type="button"
                onClick={onReject}
                className="inline-flex items-center gap-1 rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs text-rose-700 hover:bg-rose-500/20 dark:text-rose-300"
                title="Reject — deletes the row, supervisor falls back to bootstrapping"
              >
                <XCircle className="h-3 w-3" />
                Reject
              </button>
            </>
          )}
          {row.state === "approved" && (
            <>
              <button
                type="button"
                onClick={onRekey}
                className="inline-flex items-center gap-1 rounded-md border bg-background px-2 py-1 text-xs hover:bg-muted"
                title="Issue a fresh cert against the same pubkey"
              >
                <KeyRound className="h-3 w-3" />
                Re-key
              </button>
              <button
                type="button"
                onClick={onDelete}
                className="inline-flex items-center gap-1 rounded-md border bg-background px-2 py-1 text-xs hover:bg-muted"
                title="Permanently remove from the fleet"
              >
                <Trash2 className="h-3 w-3" />
                Delete
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  );
}

function ApplianceDrilldownModal({
  row,
  onClose,
  onApprove,
  approving,
  onReject,
  onRekey,
  onDelete,
}: {
  row: ApplianceRow;
  onClose: () => void;
  onApprove: () => void;
  approving: boolean;
  onReject: () => void;
  onRekey: () => void;
  onDelete: () => void;
}) {
  const caps = row.capabilities ?? {};
  const badge = stateBadge(row.state);
  const Icon = badge.Icon;

  return (
    <Modal title={`Appliance · ${row.hostname}`} onClose={onClose} wide>
      <div className="space-y-4 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={cn(
              "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs",
              badge.className,
            )}
          >
            <Icon className="h-3 w-3" /> {badge.label}
          </span>
          {row.supervisor_version && (
            <span className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-xs">
              supervisor {row.supervisor_version}
            </span>
          )}
          {caps.has_baked_images && (
            <span className="rounded-md bg-sky-500/10 px-1.5 py-0.5 font-mono text-xs text-sky-700 dark:text-sky-300">
              baked images
              {caps.baked_images_version
                ? ` · ${caps.baked_images_version}`
                : ""}
            </span>
          )}
        </div>

        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Capabilities
          </h3>
          <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
            <CapRow label="DNS — BIND9" on={!!caps.can_run_dns_bind9} />
            <CapRow label="DNS — PowerDNS" on={!!caps.can_run_dns_powerdns} />
            <CapRow label="DHCP" on={!!caps.can_run_dhcp} />
            <CapRow label="Observer" on={!!caps.can_run_observer} />
          </div>
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
            <FactRow label="CPUs" value={caps.cpu_count} />
            <FactRow
              label="Memory"
              value={
                typeof caps.memory_mb === "number"
                  ? `${(caps.memory_mb / 1024).toFixed(1)} GiB`
                  : undefined
              }
            />
            <FactRow label="Storage" value={caps.storage_type} />
            <FactRow
              label="Host NICs"
              value={
                Array.isArray(caps.host_nics)
                  ? caps.host_nics.join(", ")
                  : undefined
              }
              icon={Network}
            />
          </dl>
        </div>

        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Identity
          </h3>
          <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-xs">
            <dt className="text-muted-foreground">Appliance id</dt>
            <dd className="break-all font-mono">{row.id}</dd>
            <dt className="text-muted-foreground">Pubkey fingerprint</dt>
            <dd className="break-all font-mono">
              {row.public_key_fingerprint}
            </dd>
            <dt className="text-muted-foreground">Paired</dt>
            <dd>
              {row.paired_at ? new Date(row.paired_at).toLocaleString() : "—"}
              {row.paired_from_ip ? ` · from ${row.paired_from_ip}` : ""}
            </dd>
            <dt className="text-muted-foreground">Last seen</dt>
            <dd>
              {row.last_seen_at
                ? new Date(row.last_seen_at).toLocaleString()
                : "—"}
              {row.last_seen_ip ? ` · ${row.last_seen_ip}` : ""}
            </dd>
            {row.approved_at && (
              <>
                <dt className="text-muted-foreground">Approved</dt>
                <dd>{new Date(row.approved_at).toLocaleString()}</dd>
              </>
            )}
          </dl>
        </div>

        {row.state === "approved" && (
          <ApplianceRoleAssignmentSection row={row} />
        )}

        {row.state === "approved" && <ApplianceOsUpgradeSection row={row} />}

        {row.state === "approved" && (
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Certificate
            </h3>
            <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-xs">
              <dt className="text-muted-foreground">Serial</dt>
              <dd className="break-all font-mono">{row.cert_serial ?? "—"}</dd>
              <dt className="text-muted-foreground">Issued</dt>
              <dd>
                {row.cert_issued_at
                  ? new Date(row.cert_issued_at).toLocaleString()
                  : "—"}
              </dd>
              <dt className="text-muted-foreground">Expires</dt>
              <dd>
                {row.cert_expires_at
                  ? new Date(row.cert_expires_at).toLocaleString()
                  : "—"}
              </dd>
            </dl>
          </div>
        )}

        <div className="flex flex-wrap justify-end gap-2 border-t pt-3">
          {row.state === "pending_approval" ? (
            <>
              <button
                type="button"
                onClick={onReject}
                className="inline-flex items-center gap-1 rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-1.5 text-xs text-rose-700 hover:bg-rose-500/20 dark:text-rose-300"
              >
                <XCircle className="h-3.5 w-3.5" />
                Reject
              </button>
              <button
                type="button"
                onClick={onApprove}
                disabled={approving}
                className="inline-flex items-center gap-1 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-xs text-emerald-700 hover:bg-emerald-500/20 disabled:opacity-50 dark:text-emerald-300"
              >
                {approving ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <CheckCircle2 className="h-3.5 w-3.5" />
                )}
                Approve + sign cert
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={onDelete}
                className="inline-flex items-center gap-1 rounded-md border bg-background px-3 py-1.5 text-xs hover:bg-muted"
              >
                <Trash2 className="h-3.5 w-3.5" />
                Delete
              </button>
              <button
                type="button"
                onClick={onRekey}
                className="inline-flex items-center gap-1 rounded-md border bg-background px-3 py-1.5 text-xs hover:bg-muted"
              >
                <KeyRound className="h-3.5 w-3.5" />
                Re-key
              </button>
            </>
          )}
        </div>
      </div>
    </Modal>
  );
}

function CapRow({ label, on }: { label: string; on: boolean }) {
  return (
    <div className="flex items-center gap-2 rounded-md border bg-background px-2 py-1.5 text-xs">
      <span
        className={cn(
          "inline-block h-1.5 w-1.5 rounded-full",
          on ? "bg-emerald-500" : "bg-muted-foreground/30",
        )}
      />
      <span className={cn(!on && "text-muted-foreground")}>{label}</span>
    </div>
  );
}

function FactRow({
  label,
  value,
  icon: IconComp,
}: {
  label: string;
  value: string | number | undefined | null;
  icon?: typeof Network;
}) {
  if (value === undefined || value === null || value === "") return null;
  return (
    <>
      <dt className="flex items-center gap-1 text-muted-foreground">
        {IconComp ? <IconComp className="h-3 w-3" /> : null}
        {label}
      </dt>
      <dd className="break-all">{value}</dd>
    </>
  );
}

// Silence unused-vars on the input class export — kept for future
// per-row inline edits (notes / tags) without making the import
// disappear on a UI shape revisit.
void inputCls;

// ── Role assignment section (#170 Wave C2) ────────────────────────

const ROLE_OPTIONS: { value: string; label: string; capKey?: string }[] = [
  {
    value: "dns-bind9",
    label: "DNS · BIND9",
    capKey: "can_run_dns_bind9",
  },
  {
    value: "dns-powerdns",
    label: "DNS · PowerDNS",
    capKey: "can_run_dns_powerdns",
  },
  { value: "dhcp", label: "DHCP", capKey: "can_run_dhcp" },
  { value: "observer", label: "Observer", capKey: "can_run_observer" },
];

function ApplianceRoleAssignmentSection({ row }: { row: ApplianceRow }) {
  const qc = useQueryClient();
  const caps = row.capabilities ?? {};
  const initialRoles = new Set(row.assigned_roles ?? []);
  const [roles, setRoles] = useState<Set<string>>(initialRoles);
  const [dnsGroupId, setDnsGroupId] = useState<string | null>(
    row.assigned_dns_group_id ?? null,
  );
  const [dhcpGroupId, setDhcpGroupId] = useState<string | null>(
    row.assigned_dhcp_group_id ?? null,
  );
  // #170 Wave C3 — operator-pasted nft fragment. Empty string clears
  // it server-side (the model column flips to NULL); the supervisor's
  // renderer skips the override block when nothing is set.
  const [firewallExtra, setFirewallExtra] = useState<string>(
    row.firewall_extra ?? "",
  );

  const dnsGroupsQuery = useQuery({
    queryKey: ["dns", "groups"],
    queryFn: dnsApi.listGroups,
    staleTime: 60_000,
  });
  const dhcpGroupsQuery = useQuery({
    queryKey: ["dhcp", "groups"],
    queryFn: dhcpApi.listGroups,
    staleTime: 60_000,
  });

  const save = useMutation({
    mutationFn: () =>
      applianceApprovalApi.updateRoles(row.id, {
        roles: Array.from(roles),
        dns_group_id: dnsGroupId,
        dhcp_group_id: dhcpGroupId,
        firewall_extra: firewallExtra,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "approvals"] });
    },
  });

  function toggleRole(role: string) {
    setRoles((current) => {
      const next = new Set(current);
      if (next.has(role)) {
        next.delete(role);
      } else {
        // Mutually-exclusive DNS engines — selecting one clears the
        // other so the operator can't submit an invalid combo.
        if (role === "dns-bind9") next.delete("dns-powerdns");
        if (role === "dns-powerdns") next.delete("dns-bind9");
        next.add(role);
      }
      return next;
    });
  }

  const dnsRoleActive = roles.has("dns-bind9") || roles.has("dns-powerdns");
  const dhcpRoleActive = roles.has("dhcp");
  const dirty =
    JSON.stringify(Array.from(roles).sort()) !==
      JSON.stringify([...initialRoles].sort()) ||
    dnsGroupId !== (row.assigned_dns_group_id ?? null) ||
    dhcpGroupId !== (row.assigned_dhcp_group_id ?? null) ||
    firewallExtra !== (row.firewall_extra ?? "");

  // Live preview of the role-derived firewall profile name + opened
  // service ports. Mirrors the supervisor's firewall_renderer.py
  // logic so operators see what will actually land on the host.
  const firewallProfile = (() => {
    const hasDns = roles.has("dns-bind9") || roles.has("dns-powerdns");
    const hasDhcp = roles.has("dhcp");
    if (hasDns && hasDhcp) return "dns-and-dhcp";
    if (hasDns) return "dns-only";
    if (hasDhcp) return "dhcp-only";
    return "idle";
  })();
  const firewallOpenPorts: string[] = [];
  if (roles.has("dns-bind9") || roles.has("dns-powerdns")) {
    firewallOpenPorts.push("UDP/53", "TCP/53");
  }
  if (roles.has("dhcp")) {
    firewallOpenPorts.push("UDP/67", "UDP/68");
  }

  return (
    <div>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Role assignment
      </h3>
      <p className="mt-1 text-xs text-muted-foreground">
        Pick a subset of roles the supervisor brings up. DNS engines are
        mutually exclusive — one per appliance. The supervisor reads this on its
        next heartbeat (≤ 30s) and starts / stops the matching service
        containers.
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        {ROLE_OPTIONS.map((opt) => {
          const cap = opt.capKey
            ? (caps[opt.capKey as keyof SupervisorCapabilities] as
                | boolean
                | undefined)
            : true;
          const disabled = !cap;
          const active = roles.has(opt.value);
          return (
            <button
              key={opt.value}
              type="button"
              disabled={disabled}
              onClick={() => toggleRole(opt.value)}
              title={
                disabled
                  ? `Supervisor doesn't advertise ${opt.capKey}=true; cannot assign.`
                  : undefined
              }
              className={cn(
                "rounded-md border px-2 py-1 text-xs",
                active
                  ? "border-primary bg-primary/10 text-foreground"
                  : "border-input bg-background text-muted-foreground hover:bg-muted",
                disabled && "cursor-not-allowed opacity-40",
              )}
            >
              {opt.label}
            </button>
          );
        })}
      </div>

      {dnsRoleActive && (
        <div className="mt-3">
          <label className="text-xs text-muted-foreground">DNS group</label>
          <select
            value={dnsGroupId ?? ""}
            onChange={(e) => setDnsGroupId(e.target.value || null)}
            className="mt-1 w-full rounded-md border bg-background px-2 py-1 text-xs"
          >
            <option value="">(unassigned)</option>
            {dnsGroupsQuery.data?.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </div>
      )}
      {dhcpRoleActive && (
        <div className="mt-3">
          <label className="text-xs text-muted-foreground">DHCP group</label>
          <select
            value={dhcpGroupId ?? ""}
            onChange={(e) => setDhcpGroupId(e.target.value || null)}
            className="mt-1 w-full rounded-md border bg-background px-2 py-1 text-xs"
          >
            <option value="">(unassigned)</option>
            {dhcpGroupsQuery.data?.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name} ({g.network_mode ?? "host"})
              </option>
            ))}
          </select>
        </div>
      )}

      {/* #170 Wave C3 — firewall preview + operator-override textarea.
          The preview mirrors the supervisor's firewall_renderer.py
          output for the currently-selected roles so the operator can
          tell what nft drop-in will land before saving. */}
      <div className="mt-4 rounded-md border bg-muted/30 p-3">
        <div className="flex items-center justify-between">
          <div className="text-xs font-medium">Firewall profile</div>
          <span className="rounded-full bg-muted px-2 py-0.5 font-mono text-[10px]">
            {firewallProfile}
          </span>
        </div>
        <p className="mt-1 text-[11px] text-muted-foreground">
          Always open: <code>tcp/22</code> · <code>icmp echo</code> · loopback.
          {firewallOpenPorts.length > 0 ? (
            <>
              {" "}
              Per-role:{" "}
              {firewallOpenPorts.map((p, i) => (
                <span key={p}>
                  {i > 0 ? " · " : ""}
                  <code>{p}</code>
                </span>
              ))}
              .
            </>
          ) : (
            " No per-role ports (idle)."
          )}
        </p>
        <label className="mt-3 block text-xs text-muted-foreground">
          Operator override (raw nft fragment)
        </label>
        <textarea
          value={firewallExtra}
          onChange={(e) => setFirewallExtra(e.target.value)}
          placeholder={`# e.g. allow SNMP from monitoring subnet\n# udp dport 161 ip saddr 10.0.0.0/24 accept`}
          rows={4}
          className="mt-1 w-full rounded-md border bg-background px-2 py-1 font-mono text-[11px]"
        />
        <p className="mt-1 text-[11px] text-muted-foreground">
          Appended verbatim after the role-driven block. Supervisor runs{" "}
          <code>nft -c -f</code> dry-run before live-swap — a syntactically
          invalid value is rejected on the host without leaving the firewall
          half-rendered.
        </p>
      </div>

      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          disabled={!dirty || save.isPending}
          onClick={() => save.mutate()}
          className="inline-flex items-center gap-1 rounded-md border border-primary bg-primary/10 px-3 py-1.5 text-xs text-foreground disabled:cursor-not-allowed disabled:opacity-50"
        >
          {save.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : null}
          Save role assignment
        </button>
        {save.error && (
          <span className="text-xs text-rose-700 dark:text-rose-300">
            {(save.error as Error).message}
          </span>
        )}
      </div>
    </div>
  );
}

// ── OS upgrade + reboot section (#170 Wave D1) ────────────────────

function slotLabel(slot: string | null): string {
  if (slot === "slot_a") return "A";
  if (slot === "slot_b") return "B";
  return "—";
}

function ApplianceOsUpgradeSection({ row }: { row: ApplianceRow }) {
  const qc = useQueryClient();
  const [sourceKind, setSourceKind] = useState<"url" | "uploaded">("uploaded");
  const [tag, setTag] = useState("");
  const [imageUrl, setImageUrl] = useState("");
  const [slotImageId, setSlotImageId] = useState<string>("");
  const [rebootConfirm, setRebootConfirm] = useState(false);

  const isApplianceHost =
    row.deployment_kind === "appliance" || row.deployment_kind === null;
  const trialBoot = row.is_trial_boot;
  const upgradeInFlight = row.desired_appliance_version !== null;

  // Uploaded slot images — fetched only when the operator picks the
  // ``uploaded`` source, so non-air-gapped flows skip the round trip.
  const uploadedQuery = useQuery({
    queryKey: ["appliance", "slot-images"],
    queryFn: applianceSlotImagesApi.list,
    staleTime: 30_000,
    enabled: isApplianceHost,
  });

  // When the operator picks an uploaded image, auto-fill the version
  // tag from the row's appliance_version. Saves them re-typing it +
  // keeps the supervisor's auto-clear logic aligned with the bytes.
  function pickUploadedImage(id: string) {
    setSlotImageId(id);
    const image = uploadedQuery.data?.find((i) => i.id === id);
    if (image) setTag(image.appliance_version);
  }

  const scheduleUpgrade = useMutation({
    mutationFn: () =>
      applianceApprovalApi.scheduleUpgrade(
        row.id,
        tag.trim(),
        sourceKind === "url"
          ? { kind: "url", url: imageUrl.trim() }
          : { kind: "uploaded", slot_image_id: slotImageId },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "approvals"] });
      setTag("");
      setImageUrl("");
      setSlotImageId("");
    },
  });
  const clearUpgrade = useMutation({
    mutationFn: () => applianceApprovalApi.clearUpgrade(row.id),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["appliance", "approvals"] }),
  });
  const reboot = useMutation({
    mutationFn: () => applianceApprovalApi.scheduleReboot(row.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "approvals"] });
      setRebootConfirm(false);
    },
  });

  return (
    <div>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        OS &amp; lifecycle
      </h3>
      <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-xs">
        <dt className="text-muted-foreground">Deployment</dt>
        <dd>
          <span className="rounded-full bg-muted px-1.5 py-0.5 font-mono text-[10px]">
            {row.deployment_kind ?? "unknown"}
          </span>
        </dd>
        <dt className="text-muted-foreground">Installed</dt>
        <dd className="font-mono">{row.installed_appliance_version ?? "—"}</dd>
        <dt className="text-muted-foreground">Slots</dt>
        <dd>
          <span className="font-mono">
            running={slotLabel(row.current_slot)}
          </span>
          {" · "}
          <span className="font-mono">
            default={slotLabel(row.durable_default)}
          </span>
          {trialBoot && (
            <span className="ml-2 rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-300">
              trial boot
            </span>
          )}
        </dd>
        <dt className="text-muted-foreground">Last upgrade</dt>
        <dd>
          {row.last_upgrade_state ? (
            <span
              className={cn(
                "rounded-full px-1.5 py-0.5 font-mono text-[10px]",
                row.last_upgrade_state === "done" ||
                  row.last_upgrade_state === "ready"
                  ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                  : row.last_upgrade_state === "failed"
                    ? "bg-rose-500/10 text-rose-700 dark:text-rose-300"
                    : "bg-amber-500/10 text-amber-700 dark:text-amber-300",
              )}
            >
              {row.last_upgrade_state}
            </span>
          ) : (
            "—"
          )}
          {row.last_upgrade_state_at && (
            <span className="ml-2 text-muted-foreground">
              {new Date(row.last_upgrade_state_at).toLocaleString()}
            </span>
          )}
        </dd>
      </dl>

      {!isApplianceHost ? (
        <p className="mt-3 text-xs text-muted-foreground">
          OS slot upgrades + host reboot are only available on the SpatiumDDI
          appliance OS. Use the docker compose / helm upgrade flow for{" "}
          <code>{row.deployment_kind}</code> deployments.
        </p>
      ) : upgradeInFlight ? (
        <div className="mt-3 rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-xs">
          <div className="flex items-center gap-2">
            <Upload className="h-3.5 w-3.5 text-amber-700 dark:text-amber-300" />
            <span className="font-medium">Upgrade pending</span>
          </div>
          <p className="mt-1 text-muted-foreground">
            Target version <code>{row.desired_appliance_version}</code>.
            Supervisor will fire the slot-upgrade trigger on its next heartbeat
            (≤ 30 s).
          </p>
          <button
            type="button"
            onClick={() => clearUpgrade.mutate()}
            disabled={clearUpgrade.isPending}
            className="mt-2 inline-flex items-center gap-1 rounded-md border bg-background px-2 py-1 text-[11px] hover:bg-muted disabled:opacity-50"
          >
            {clearUpgrade.isPending ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <XCircle className="h-3 w-3" />
            )}
            Cancel pending upgrade
          </button>
        </div>
      ) : (
        <div className="mt-3 space-y-2">
          {/* Source picker — uploaded image (air-gap-friendly) vs
              external URL (lab / direct internet appliances). */}
          <div className="flex gap-1 text-xs">
            <button
              type="button"
              onClick={() => setSourceKind("uploaded")}
              className={cn(
                "rounded-md border px-2 py-1",
                sourceKind === "uploaded"
                  ? "border-primary bg-primary/10"
                  : "border-input bg-background text-muted-foreground hover:bg-muted",
              )}
            >
              From uploaded image
            </button>
            <button
              type="button"
              onClick={() => setSourceKind("url")}
              className={cn(
                "rounded-md border px-2 py-1",
                sourceKind === "url"
                  ? "border-primary bg-primary/10"
                  : "border-input bg-background text-muted-foreground hover:bg-muted",
              )}
            >
              From external URL
            </button>
          </div>
          {sourceKind === "uploaded" ? (
            <>
              <select
                value={slotImageId}
                onChange={(e) => pickUploadedImage(e.target.value)}
                className="w-full rounded-md border bg-background px-2 py-1 text-xs"
              >
                <option value="">(pick an uploaded image)</option>
                {(uploadedQuery.data ?? []).map((img) => (
                  <option key={img.id} value={img.id}>
                    {img.filename} · v{img.appliance_version} ·{" "}
                    {(img.size_bytes / (1024 * 1024)).toFixed(0)} MiB
                  </option>
                ))}
              </select>
              {uploadedQuery.data && uploadedQuery.data.length === 0 && (
                <p className="text-[11px] text-muted-foreground">
                  No slot images uploaded yet. Open the &ldquo;Slot image
                  uploads&rdquo; section above to upload one.
                </p>
              )}
              <input
                value={tag}
                onChange={(e) => setTag(e.target.value)}
                placeholder="target version (auto-filled from the picked image)"
                className="w-full rounded-md border bg-background px-2 py-1 text-xs"
              />
            </>
          ) : (
            <div className="flex flex-col gap-1.5 sm:flex-row">
              <input
                value={tag}
                onChange={(e) => setTag(e.target.value)}
                placeholder="target version (e.g. 2026.06.01-1)"
                className="flex-1 rounded-md border bg-background px-2 py-1 text-xs"
              />
              <input
                value={imageUrl}
                onChange={(e) => setImageUrl(e.target.value)}
                placeholder="slot raw.xz URL"
                className="flex-[2] rounded-md border bg-background px-2 py-1 text-xs"
              />
            </div>
          )}
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => scheduleUpgrade.mutate()}
              disabled={
                !tag.trim() ||
                scheduleUpgrade.isPending ||
                (sourceKind === "url" && !imageUrl.trim()) ||
                (sourceKind === "uploaded" && !slotImageId)
              }
              className="inline-flex items-center gap-1 rounded-md border border-primary bg-primary/10 px-3 py-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-50"
            >
              {scheduleUpgrade.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <HardDrive className="h-3.5 w-3.5" />
              )}
              Schedule OS upgrade
            </button>
            {scheduleUpgrade.error && (
              <span className="text-xs text-rose-700 dark:text-rose-300">
                {(scheduleUpgrade.error as Error).message}
              </span>
            )}
          </div>
          <p className="text-[11px] text-muted-foreground">
            Stamps <code>desired_appliance_version</code> on the appliance row.
            The supervisor reads it on its next heartbeat + writes the
            slot-upgrade trigger; the host runner dd&apos;s the image to the
            inactive slot + reboots into it (auto-revert if{" "}
            <code>/health/live</code> fails).
          </p>
        </div>
      )}

      {isApplianceHost && (
        <div className="mt-3 flex items-center gap-2 border-t pt-3">
          {row.reboot_requested ? (
            <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-700 dark:text-amber-300">
              reboot queued
            </span>
          ) : null}
          <button
            type="button"
            onClick={() => setRebootConfirm(true)}
            disabled={row.reboot_requested}
            className="inline-flex items-center gap-1 rounded-md border bg-background px-3 py-1.5 text-xs hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Power className="h-3.5 w-3.5" />
            Reboot host
          </button>
          {reboot.error && (
            <span className="text-xs text-rose-700 dark:text-rose-300">
              {(reboot.error as Error).message}
            </span>
          )}
        </div>
      )}

      {rebootConfirm && (
        <ConfirmModal
          open
          title="Reboot appliance host?"
          message={
            <>
              <p className="text-sm">
                Reboot <strong>{row.hostname}</strong>? The supervisor will pick
                this up on its next heartbeat (≤ 30 s) and the host will drop
                offline for ~30–60 s while it restarts.
              </p>
              <p className="mt-2 text-xs text-muted-foreground">
                Use sparingly. Service containers will be brought back up by the
                supervisor on the next boot.
              </p>
            </>
          }
          confirmLabel="Reboot"
          tone="destructive"
          loading={reboot.isPending}
          onConfirm={() => reboot.mutate()}
          onClose={() => setRebootConfirm(false)}
          requireCheckboxLabel={`I understand ${row.hostname} will go offline for ~30–60 s`}
        />
      )}
    </div>
  );
}

// ── CollapsibleSection ──────────────────────────────────────────

function CollapsibleSection({
  title,
  description,
  storageKey,
  children,
}: {
  title: string;
  description: string;
  storageKey: string;
  children: React.ReactNode;
}) {
  // Persist expanded state in sessionStorage so the operator's
  // pick survives a page refresh inside the same tab. Default
  // collapsed so the Fleet table is what the operator sees first.
  const [open, setOpen] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.sessionStorage.getItem(storageKey) === "1";
  });
  function toggle() {
    setOpen((next) => {
      const v = !next;
      try {
        window.sessionStorage.setItem(storageKey, v ? "1" : "0");
      } catch {
        // sessionStorage unavailable (private mode); state still
        // toggles in-memory.
      }
      return v;
    });
  }
  return (
    <div className="mb-3 rounded-md border bg-card">
      <button
        type="button"
        onClick={toggle}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-muted/40"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
        )}
        <span className="font-medium">{title}</span>
        <span className="text-xs text-muted-foreground">— {description}</span>
      </button>
      {open && <div className="border-t p-3">{children}</div>}
    </div>
  );
}

// ── SlotImageManager (#170 follow-up) ───────────────────────────

function SlotImageManager() {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [sha256, setSha256] = useState("");
  const [applianceVersion, setApplianceVersion] = useState("");
  const [notes, setNotes] = useState("");
  const [progress, setProgress] = useState<{
    loaded: number;
    total: number;
  } | null>(null);

  const imagesQuery = useQuery({
    queryKey: ["appliance", "slot-images"],
    queryFn: applianceSlotImagesApi.list,
    staleTime: 30_000,
  });

  const upload = useMutation({
    mutationFn: () =>
      applianceSlotImagesApi.upload(
        file!,
        sha256.trim().toLowerCase(),
        applianceVersion.trim(),
        notes.trim() || undefined,
        (loaded, total) => setProgress({ loaded, total }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "slot-images"] });
      setFile(null);
      setSha256("");
      setApplianceVersion("");
      setNotes("");
      setProgress(null);
    },
    onError: () => setProgress(null),
  });

  const remove = useMutation({
    mutationFn: (id: string) => applianceSlotImagesApi.remove(id),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["appliance", "slot-images"] }),
  });

  return (
    <div className="space-y-3 text-xs">
      <p className="text-muted-foreground">
        Air-gapped appliances can&apos;t reach the GitHub release page. Download
        the <code>.raw.xz</code> + its <code>.sha256</code> sidecar to a
        workstation, then upload here. The backend verifies the SHA-256 on the
        received bytes; the appliance downloads through the control plane via an
        authenticated internal URL when an OS upgrade is scheduled.
      </p>

      <div className="rounded-md border p-3">
        <div className="grid gap-2 sm:grid-cols-2">
          <div>
            <label className="text-muted-foreground">.raw.xz file</label>
            <input
              type="file"
              accept=".xz,.raw.xz,application/octet-stream"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="mt-1 block w-full text-xs"
            />
            {file && (
              <p className="mt-1 text-[11px] text-muted-foreground">
                {file.name} · {(file.size / (1024 * 1024)).toFixed(1)} MiB
              </p>
            )}
          </div>
          <div>
            <label className="text-muted-foreground">Appliance version</label>
            <input
              value={applianceVersion}
              onChange={(e) => setApplianceVersion(e.target.value)}
              placeholder="e.g. 2026.06.01-1"
              className="mt-1 w-full rounded-md border bg-background px-2 py-1"
            />
          </div>
          <div className="sm:col-span-2">
            <label className="text-muted-foreground">SHA-256 (hex)</label>
            <input
              value={sha256}
              onChange={(e) => setSha256(e.target.value)}
              placeholder="paste from the .sha256 sidecar (64 lowercase hex chars)"
              className="mt-1 w-full rounded-md border bg-background px-2 py-1 font-mono"
            />
          </div>
          <div className="sm:col-span-2">
            <label className="text-muted-foreground">Notes (optional)</label>
            <input
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="e.g. RC1 — verified by ops on 2026-06-01"
              className="mt-1 w-full rounded-md border bg-background px-2 py-1"
            />
          </div>
        </div>
        <div className="mt-3 flex items-center gap-2">
          <button
            type="button"
            onClick={() => upload.mutate()}
            disabled={
              !file ||
              sha256.trim().length !== 64 ||
              !applianceVersion.trim() ||
              upload.isPending
            }
            className="inline-flex items-center gap-1 rounded-md border border-primary bg-primary/10 px-3 py-1.5 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {upload.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Upload className="h-3.5 w-3.5" />
            )}
            Upload
          </button>
          {progress && progress.total > 0 && (
            <span className="text-[11px] text-muted-foreground">
              {((progress.loaded / progress.total) * 100).toFixed(0)}% ·{" "}
              {(progress.loaded / (1024 * 1024)).toFixed(1)} /{" "}
              {(progress.total / (1024 * 1024)).toFixed(1)} MiB
            </span>
          )}
          {upload.error && (
            <span className="text-rose-700 dark:text-rose-300">
              {(upload.error as Error).message}
            </span>
          )}
        </div>
      </div>

      {imagesQuery.isLoading ? (
        <div className="flex items-center gap-2 text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
        </div>
      ) : (imagesQuery.data ?? []).length === 0 ? (
        <p className="text-muted-foreground">No uploaded slot images yet.</p>
      ) : (
        <table className="w-full">
          <thead className="text-[10px] uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="px-2 py-1 text-left">Filename</th>
              <th className="px-2 py-1 text-left">Version</th>
              <th className="px-2 py-1 text-left">Size</th>
              <th className="px-2 py-1 text-left">SHA-256</th>
              <th className="px-2 py-1 text-left">Uploaded</th>
              <th className="px-2 py-1 text-right"></th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {(imagesQuery.data ?? []).map((img: SlotImage) => (
              <tr key={img.id}>
                <td className="px-2 py-1">
                  <div className="font-medium">{img.filename}</div>
                  {img.notes && (
                    <div className="text-[11px] text-muted-foreground">
                      {img.notes}
                    </div>
                  )}
                </td>
                <td className="px-2 py-1 font-mono">{img.appliance_version}</td>
                <td className="px-2 py-1 font-mono">
                  {(img.size_bytes / (1024 * 1024)).toFixed(0)} MiB
                </td>
                <td className="px-2 py-1 font-mono">
                  {img.sha256.slice(0, 12)}…{img.sha256.slice(-6)}
                </td>
                <td className="px-2 py-1">{relativeTime(img.uploaded_at)}</td>
                <td className="px-2 py-1 text-right">
                  <button
                    type="button"
                    onClick={() => remove.mutate(img.id)}
                    className="inline-flex items-center gap-1 rounded-md border bg-background px-2 py-0.5 hover:bg-muted"
                  >
                    <Trash2 className="h-3 w-3" />
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
