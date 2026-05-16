import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Ban,
  CheckCircle2,
  HardDrive,
  KeyRound,
  Loader2,
  Network,
  Power,
  RefreshCw,
  RotateCcw,
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
import { useSessionState } from "@/lib/useSessionState";
import { cn } from "@/lib/utils";
import { NTPTab } from "./NTPTab";
import { PairingTab } from "./PairingTab";
import { SNMPTab } from "./SNMPTab";

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
  if (state === "revoked") {
    // Issue #170 Wave E follow-up — soft-deleted. Visually distinct
    // from ``rejected`` (which is an admin saying "I don't want this
    // pairing") via the amber palette; rejected stays rose.
    return {
      label: "revoked",
      className:
        "bg-amber-500/10 text-amber-700 dark:text-amber-400 border-amber-500/30",
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

// Service chips rendered in the appliance-list Services column.
// Distinct from ``capabilityChips``: capabilities = what the supervisor
// CAN run (image is loaded), services = what the supervisor IS running
// (operator assigned the role + the compose lifecycle reports it healthy).
// Colour follows ``role_switch_state``: ``ready`` → green, ``failed`` →
// rose, anything else (``idle`` / null / pre-first-apply) → amber.
// ``observer`` always renders neutral because there's no service
// container behind it — the supervisor IS the observer.
function serviceChips(row: ApplianceRow): {
  key: string;
  label: string;
  status: "ready" | "failed" | "pending" | "neutral";
}[] {
  const roles = row.assigned_roles ?? [];
  if (roles.length === 0) return [];
  const lifecycle = row.role_switch_state;
  const serviceStatus: "ready" | "failed" | "pending" =
    lifecycle === "ready"
      ? "ready"
      : lifecycle === "failed"
        ? "failed"
        : "pending";
  const out: {
    key: string;
    label: string;
    status: "ready" | "failed" | "pending" | "neutral";
  }[] = [];
  if (roles.includes("dns-bind9"))
    out.push({ key: "dns-bind9", label: "DNS · BIND9", status: serviceStatus });
  if (roles.includes("dns-powerdns"))
    out.push({
      key: "dns-powerdns",
      label: "DNS · PowerDNS",
      status: serviceStatus,
    });
  if (roles.includes("dhcp"))
    out.push({ key: "dhcp", label: "DHCP", status: serviceStatus });
  if (roles.includes("observer"))
    out.push({ key: "observer", label: "Observer", status: "neutral" });
  return out;
}

const SERVICE_CHIP_STYLES: Record<
  "ready" | "failed" | "pending" | "neutral",
  string
> = {
  ready:
    "bg-emerald-500/15 text-emerald-700 border-emerald-500/40 dark:text-emerald-300",
  failed: "bg-rose-500/15 text-rose-700 border-rose-500/40 dark:text-rose-300",
  pending:
    "bg-amber-500/15 text-amber-700 border-amber-500/40 dark:text-amber-300",
  neutral: "bg-muted text-muted-foreground border-border",
};

export function FleetTab() {
  const qc = useQueryClient();
  const { data: me } = useQuery({
    queryKey: ["me"],
    queryFn: authApi.me,
    staleTime: 60_000,
  });
  const isSuperadmin = me?.is_superadmin ?? false;

  const { data, isLoading, isFetching, refetch, error } = useQuery({
    queryKey: ["appliance", "fleet"],
    queryFn: applianceApprovalApi.list,
    refetchInterval: (query) => {
      const rows = query.state.data ?? [];
      const hasPending = rows.some((r) => r.state === "pending_approval");
      return hasPending ? 2_000 : 15_000;
    },
    enabled: isSuperadmin,
  });

  // #170 follow-up — left-sidebar nav mirroring SettingsPage's
  // shape. Sections: the appliance fleet table, pairing-code
  // management, air-gap slot-image uploads, plus NTP + SNMP
  // (fleet-wide platform_settings the supervisor pushes to every
  // appliance host via the ConfigBundle long-poll). ``useSessionState``
  // persists the operator's pick so a refresh inside the same tab
  // lands them back on the same section.
  const [view, setView] = useSessionState<
    "appliances" | "pairing" | "slot-images" | "ntp" | "snmp"
  >("appliance.fleet.section", "appliances");

  const [drilldown, setDrilldown] = useState<ApplianceRow | null>(null);
  const [rejectTarget, setRejectTarget] = useState<ApplianceRow | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ApplianceRow | null>(null);
  const [rekeyTarget, setRekeyTarget] = useState<ApplianceRow | null>(null);

  const approve = useMutation({
    mutationFn: (id: string) => applianceApprovalApi.approve(id),
    onSuccess: (row) => {
      qc.invalidateQueries({ queryKey: ["appliance", "fleet"] });
      // Refresh the open drilldown with the updated row so the
      // operator sees ``approved`` state + cert serial + role
      // assignment section immediately, instead of staring at a
      // still-pending modal with no feedback.
      if (drilldown && drilldown.id === row.id) setDrilldown(row);
    },
  });
  const reject = useMutation({
    mutationFn: (id: string) => applianceApprovalApi.reject(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "fleet"] });
      setRejectTarget(null);
    },
  });
  // #170 follow-up — password re-auth required for delete (the
  // destructive action that removes a fleet row + breaks the
  // supervisor's mTLS chain to the control plane). The mutation
  // takes a {id, password} pair; the ConfirmModal's password input
  // surfaces the server's 403 response inline so a typo doesn't
  // bounce the operator out of the modal.
  const [deletePwError, setDeletePwError] = useState<string | null>(null);
  const remove = useMutation({
    mutationFn: ({ id, password }: { id: string; password: string }) =>
      applianceApprovalApi.remove(id, password),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "fleet"] });
      setDeleteTarget(null);
      setDrilldown(null);
      setDeletePwError(null);
    },
    onError: (err: unknown) => {
      // FastAPI 403 lands in axios as ``{response: {status, data:
      // {detail}}}``. Surface the detail inline; fall back to a
      // generic message otherwise.
      const e = err as {
        response?: { status?: number; data?: { detail?: string } };
      };
      if (e?.response?.status === 403) {
        setDeletePwError(
          e.response.data?.detail || "Current password incorrect.",
        );
      } else {
        setDeletePwError("Delete failed. Try again.");
      }
    },
  });
  const rekey = useMutation({
    mutationFn: (id: string) => applianceApprovalApi.rekey(id),
    onSuccess: (row) => {
      qc.invalidateQueries({ queryKey: ["appliance", "fleet"] });
      setRekeyTarget(null);
      // Refresh the drilldown view if it's open on this row so the
      // operator sees the new serial + expiry immediately.
      if (drilldown && drilldown.id === row.id) setDrilldown(row);
    },
  });
  // Issue #170 Wave E follow-up — re-authorize a revoked appliance.
  // No password gate (low-risk: just flipping back to approved); the
  // supervisor's three-strike detector self-clears on the next 200.
  // The operator may still need to re-fire role assignment to bring
  // services back up since the revoke teardown ran a ``compose stop``.
  const reauthorize = useMutation({
    mutationFn: (id: string) => applianceApprovalApi.reauthorize(id),
    onSuccess: (row) => {
      qc.invalidateQueries({ queryKey: ["appliance", "fleet"] });
      if (drilldown && drilldown.id === row.id) setDrilldown(row);
    },
  });
  const [permanentDeleteTarget, setPermanentDeleteTarget] =
    useState<ApplianceRow | null>(null);
  const [permanentDeletePwError, setPermanentDeletePwError] = useState<
    string | null
  >(null);
  const permanentDelete = useMutation({
    mutationFn: ({ id, password }: { id: string; password: string }) =>
      applianceApprovalApi.permanentDelete(id, password),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "fleet"] });
      setPermanentDeleteTarget(null);
      setDrilldown(null);
      setPermanentDeletePwError(null);
    },
    onError: (err: unknown) => {
      const e = err as {
        response?: { status?: number; data?: { detail?: string } };
      };
      if (e?.response?.status === 403) {
        setPermanentDeletePwError(
          e.response.data?.detail || "Current password incorrect.",
        );
      } else {
        setPermanentDeletePwError("Permanent delete failed. Try again.");
      }
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

  // Two sections — Infrastructure (appliance lifecycle: approve / pair /
  // upgrade) and Services (fleet-wide host-OS subsystems that ride the
  // supervisor's ConfigBundle long-poll). Items inside a section stay
  // alphabetical; sections are deliberately ordered (Infrastructure
  // first because it's the more frequently-used). Future Wave-E
  // host-OS surfaces (#155-#166 — APT proxy, syslog forwarder, SSH
  // authorized_keys, etc.) drop into Services without restructuring.
  type NavItem = {
    key: "appliances" | "pairing" | "slot-images" | "ntp" | "snmp";
    label: string;
    summary: string;
    badge?: string | number;
  };
  const navGroups: { heading: string; items: NavItem[] }[] = [
    {
      heading: "Infrastructure",
      items: [
        {
          key: "appliances",
          label: "Appliances",
          summary: "Approve / manage paired supervisors.",
          badge: pending.length > 0 ? pending.length : undefined,
        },
        {
          key: "pairing",
          label: "Pairing codes",
          summary: "Mint codes for new appliances.",
        },
        {
          key: "slot-images",
          label: "Upgrade images",
          summary: "Air-gap .raw.xz upload + browse.",
        },
      ],
    },
    {
      heading: "Services",
      items: [
        {
          key: "ntp",
          label: "NTP",
          summary: "Fleet-wide chrony config.",
        },
        {
          key: "snmp",
          label: "SNMP",
          summary: "Fleet-wide snmpd config.",
        },
      ],
    },
  ];

  return (
    <div className="-m-6 flex h-[calc(100%+3rem)] overflow-hidden">
      {/* ── Sidebar ── */}
      <aside className="w-56 flex-shrink-0 overflow-y-auto border-r bg-card">
        <div className="border-b px-4 py-3">
          <h1 className="text-sm font-semibold">Appliance fleet</h1>
          <p className="text-xs text-muted-foreground">
            Lifecycle for Application appliances.
          </p>
        </div>
        <nav className="p-2">
          {navGroups.map((group, gi) => (
            <div key={group.heading} className={cn(gi > 0 && "mt-3")}>
              <div className="px-3 pb-1 pt-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/70">
                {group.heading}
              </div>
              {group.items.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => setView(item.key)}
                  className={cn(
                    "block w-full rounded-md px-3 py-2 text-left text-sm hover:bg-accent",
                    view === item.key && "bg-accent font-medium",
                  )}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span>{item.label}</span>
                    {item.badge !== undefined && (
                      <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300">
                        {item.badge}
                      </span>
                    )}
                  </span>
                  <span className="mt-0.5 block text-[11px] text-muted-foreground">
                    {item.summary}
                  </span>
                </button>
              ))}
            </div>
          ))}
        </nav>
      </aside>

      {/* ── Main pane ── */}
      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl p-6">
          {view === "pairing" && (
            <div>
              <h2 className="mb-1 text-base font-semibold">Pairing codes</h2>
              <p className="mb-4 text-xs text-muted-foreground">
                Mint 8-digit codes that a new supervisor appliance swaps for a
                pending-approval registration on{" "}
                <code>/api/v1/appliance/supervisor/register</code>. Ephemeral
                codes are single-use with a short expiry; persistent codes admit
                many appliances and can be re-revealed.
              </p>
              <PairingTab />
            </div>
          )}

          {view === "slot-images" && (
            <div>
              <div className="mb-1 flex items-center justify-between gap-2">
                <h2 className="text-base font-semibold">Upgrade images</h2>
                <button
                  type="button"
                  onClick={() =>
                    qc.invalidateQueries({
                      queryKey: ["appliance", "slot-images"],
                    })
                  }
                  title="Refresh the upgrade images list"
                  className="inline-flex items-center gap-1 rounded-md border bg-background px-2 py-1 text-xs hover:bg-muted"
                >
                  <RefreshCw className="h-3 w-3" />
                  Refresh
                </button>
              </div>
              <p className="mb-4 text-xs text-muted-foreground">
                Air-gap support — upload <code>.raw.xz</code> upgrade images for
                offline appliance upgrades. The supervisor downloads through the
                control plane via an authenticated internal URL once an OS
                upgrade points at the uploaded row.
              </p>
              <SlotImageManager />
            </div>
          )}

          {view === "ntp" && (
            <div>
              <h2 className="mb-1 text-base font-semibold">NTP (chrony)</h2>
              <p className="mb-4 text-xs text-muted-foreground">
                Fleet-wide chrony configuration. The rendered{" "}
                <code>chrony.conf</code> ships through the ConfigBundle
                long-poll to every appliance host (local + every registered
                supervisor), validated host-side before activation. Reloaded
                without a daemon restart.
              </p>
              <NTPTab />
            </div>
          )}

          {view === "snmp" && (
            <div>
              <h2 className="mb-1 text-base font-semibold">SNMP (snmpd)</h2>
              <p className="mb-4 text-xs text-muted-foreground">
                Fleet-wide snmpd configuration — v2c with community +
                source-CIDR allowlist, or v3 USM with per-user auth/priv. The
                rendered <code>snmpd.conf</code> ships through the ConfigBundle
                long-poll to every appliance host. Disabled by default —
                operators opt in here.
              </p>
              <SNMPTab />
            </div>
          )}

          {view === "appliances" && (
            <>
              <div className="mb-4 flex items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <h2 className="text-base font-semibold">
                    Application appliances
                  </h2>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Supervisors that claimed a pairing code sit here until a
                    superadmin clicks Approve. Approval signs an X.509 cert
                    against the submitted Ed25519 pubkey using the control
                    plane&apos;s internal CA (lazy-bootstrapped on the first
                    approve). The supervisor picks the cert up on its next poll
                    and switches from session-token auth to mTLS.
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
                      className={cn(
                        "h-3.5 w-3.5",
                        isFetching && "animate-spin",
                      )}
                    />
                    Refresh
                  </button>
                </div>
              </div>

              {error ? (
                <div className="rounded-md border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-700 dark:text-rose-300">
                  Failed to load appliances: {(error as Error).message}
                </div>
              ) : isLoading ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading
                  appliances…
                </div>
              ) : rows.length === 0 ? (
                <div className="rounded-md border border-dashed bg-card p-8 text-center text-sm text-muted-foreground">
                  No appliances have paired yet. Open the{" "}
                  <button
                    type="button"
                    onClick={() => setView("pairing")}
                    className="underline decoration-dotted underline-offset-2 hover:text-foreground"
                  >
                    Pairing codes
                  </button>{" "}
                  section to mint one, then install an Application appliance
                  against it — the row appears here once the supervisor claims
                  the code.
                </div>
              ) : (
                <div className="overflow-hidden rounded-md border bg-card">
                  <table className="w-full text-sm">
                    <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
                      <tr>
                        <th className="px-3 py-2 text-left font-medium">
                          Hostname
                        </th>
                        <th className="px-3 py-2 text-left font-medium">
                          State
                        </th>
                        <th className="px-3 py-2 text-left font-medium">
                          Services
                        </th>
                        <th className="px-3 py-2 text-left font-medium">
                          Capabilities
                        </th>
                        <th className="px-3 py-2 text-left font-medium">
                          Slots
                        </th>
                        <th className="px-3 py-2 text-left font-medium">
                          Fingerprint
                        </th>
                        <th className="px-3 py-2 text-left font-medium">
                          Paired
                        </th>
                        <th className="px-3 py-2 text-left font-medium">
                          Last seen
                        </th>
                        <th className="px-3 py-2 text-right font-medium">
                          Actions
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y">
                      {pending.map((row) => (
                        <ApplianceTableRow
                          key={row.id}
                          row={row}
                          highlight
                          busy={
                            approve.isPending && approve.variables === row.id
                          }
                          onOpen={() => setDrilldown(row)}
                          onApprove={() => approve.mutate(row.id)}
                          onReject={() => setRejectTarget(row)}
                          onRekey={() => setRekeyTarget(row)}
                          onDelete={() => setDeleteTarget(row)}
                          onReauthorize={() => reauthorize.mutate(row.id)}
                          onPermanentDelete={() =>
                            setPermanentDeleteTarget(row)
                          }
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
                          onReauthorize={() => reauthorize.mutate(row.id)}
                          onPermanentDelete={() =>
                            setPermanentDeleteTarget(row)
                          }
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      </main>

      {drilldown && (
        <ApplianceDrilldownModal
          row={drilldown}
          onClose={() => setDrilldown(null)}
          onApprove={() => approve.mutate(drilldown.id)}
          approving={approve.isPending}
          onReject={() => setRejectTarget(drilldown)}
          onRekey={() => setRekeyTarget(drilldown)}
          onDelete={() => setDeleteTarget(drilldown)}
          onRowUpdated={(next) => setDrilldown(next)}
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
          title="Revoke appliance?"
          message={
            <>
              <p className="text-sm">
                Revoke <strong>{deleteTarget.hostname}</strong>. The row flips
                to{" "}
                <span className="rounded bg-amber-500/15 px-1 font-medium text-amber-700 dark:text-amber-400">
                  revoked
                </span>{" "}
                — heartbeats start returning 403, the supervisor's three-strike
                detector tears down its DNS / DHCP service containers within ~3
                min, and the chip on the appliance console flips to red.
              </p>
              <p className="mt-2 text-sm">
                The row stays for <strong>30 days</strong> by default — long
                enough for an operator to <em>Re-authorize</em> if they revoked
                by mistake. The <em>Delete</em> button appears on revoked rows
                for permanent removal.
              </p>
              <p className="mt-2 text-xs text-muted-foreground">
                Cert serial:{" "}
                <code className="text-foreground">
                  {deleteTarget.cert_serial ?? "—"}
                </code>
              </p>
            </>
          }
          confirmLabel="Revoke"
          tone="destructive"
          loading={remove.isPending}
          requireCheckboxLabel={`I understand ${deleteTarget.hostname} will stop heartbeating successfully and its service containers will tear down within ~3 minutes.`}
          requirePassword
          passwordError={deletePwError}
          onConfirm={(password) =>
            remove.mutate({ id: deleteTarget.id, password: password ?? "" })
          }
          onClose={() => {
            setDeleteTarget(null);
            setDeletePwError(null);
          }}
        />
      )}

      {permanentDeleteTarget && (
        <ConfirmModal
          open
          title="Delete appliance?"
          message={
            <>
              <p className="text-sm">
                Hard DELETE the{" "}
                <strong>{permanentDeleteTarget.hostname}</strong> row from the
                database. <strong>This cannot be undone.</strong> The
                supervisor's mTLS calls will fail; the supervisor's cached
                identity will be orphaned until the operator re-pairs against a
                fresh pairing code.
              </p>
              <p className="mt-2 text-xs text-muted-foreground">
                Cert serial:{" "}
                <code className="text-foreground">
                  {permanentDeleteTarget.cert_serial ?? "—"}
                </code>
              </p>
            </>
          }
          confirmLabel="Delete"
          tone="destructive"
          loading={permanentDelete.isPending}
          requireCheckboxLabel={`I understand this permanently removes ${permanentDeleteTarget.hostname} and cannot be reversed.`}
          requirePassword
          passwordError={permanentDeletePwError}
          onConfirm={(password) =>
            permanentDelete.mutate({
              id: permanentDeleteTarget.id,
              password: password ?? "",
            })
          }
          onClose={() => {
            setPermanentDeleteTarget(null);
            setPermanentDeletePwError(null);
          }}
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

// Compact "what's actually running on this appliance" cell rendered
// in the Appliances list. Empty assigned_roles → ``—``; non-empty
// renders one chip per role with colour driven by the supervisor's
// last ``role_switch_state`` heartbeat.
function ServiceChipList({ row }: { row: ApplianceRow }) {
  const services = serviceChips(row);
  if (services.length === 0) {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  return (
    <div className="flex flex-wrap gap-1">
      {services.map((s) => (
        <span
          key={s.key}
          className={cn(
            "inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium",
            SERVICE_CHIP_STYLES[s.status],
          )}
          title={
            s.status === "ready"
              ? `Assigned + healthy (supervisor reports role_switch_state=ready)`
              : s.status === "failed"
                ? `Assigned, supervisor lifecycle apply failed — inspect drilldown`
                : s.status === "pending"
                  ? `Assigned, supervisor hasn't reported ready yet`
                  : `Assigned (no service container — supervisor is the runtime)`
          }
        >
          {s.status === "ready" && <CheckCircle2 className="h-2.5 w-2.5" />}
          {s.status === "failed" && <AlertCircle className="h-2.5 w-2.5" />}
          {s.status === "pending" && <Loader2 className="h-2.5 w-2.5" />}
          {s.label}
        </span>
      ))}
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
  onReauthorize,
  onPermanentDelete,
}: {
  row: ApplianceRow;
  highlight?: boolean;
  busy?: boolean;
  onOpen: () => void;
  onApprove: () => void;
  onReject: () => void;
  onRekey: () => void;
  onDelete: () => void;
  onReauthorize: () => void;
  onPermanentDelete: () => void;
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
        <ServiceChipList row={row} />
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
      <td className="px-3 py-2">
        <ApplianceSlotsCell row={row} />
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
                className="inline-flex items-center gap-1 rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-xs text-amber-700 hover:bg-amber-500/20 dark:text-amber-400"
                title="Revoke — flip to revoked state, supervisor tears down service containers. Re-authorize on the same row to recover."
              >
                <Ban className="h-3 w-3" />
                Revoke
              </button>
            </>
          )}
          {row.state === "revoked" && (
            <>
              <button
                type="button"
                onClick={onReauthorize}
                className="inline-flex items-center gap-1 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-500/20 dark:text-emerald-300"
                title="Re-authorize — flip back to approved; supervisor resumes on next heartbeat"
              >
                <CheckCircle2 className="h-3 w-3" />
                Re-authorize
              </button>
              <button
                type="button"
                onClick={onPermanentDelete}
                className="inline-flex items-center gap-1 rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs text-rose-700 hover:bg-rose-500/20 dark:text-rose-300"
                title="Permanently delete this row — cannot be undone"
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
  onRowUpdated,
}: {
  row: ApplianceRow;
  onClose: () => void;
  onApprove: () => void;
  approving: boolean;
  onReject: () => void;
  onRekey: () => void;
  onDelete: () => void;
  onRowUpdated: (next: ApplianceRow) => void;
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
          <ApplianceRoleAssignmentSection row={row} onSaved={onRowUpdated} />
        )}

        {row.state === "approved" &&
          Object.keys(row.role_health ?? {}).length > 0 && (
            <ApplianceRoleHealthSection row={row} />
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

// ── Service-container watchdog section (#170 Wave E) ───────────
//
// Renders the supervisor's per-service health verdict (refreshed
// every 5 min on the appliance). Each entry carries status +
// ``since`` (when the supervisor first observed this status) +
// container_id, so a regression like "dhcp-kea unhealthy for 12 m"
// surfaces without SSH'ing in.

function formatRelativeSince(iso: string): string {
  const since = new Date(iso).getTime();
  if (!Number.isFinite(since)) return iso;
  const seconds = Math.max(0, Math.round((Date.now() - since) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

const WATCHDOG_STATUS_STYLES: Record<
  "healthy" | "missing" | "unhealthy" | "starting",
  { className: string; label: string; Icon: typeof CheckCircle2 }
> = {
  healthy: {
    className:
      "bg-emerald-500/15 text-emerald-700 border-emerald-500/40 dark:text-emerald-300",
    label: "healthy",
    Icon: CheckCircle2,
  },
  missing: {
    className:
      "bg-rose-500/15 text-rose-700 border-rose-500/40 dark:text-rose-300",
    label: "missing",
    Icon: AlertCircle,
  },
  unhealthy: {
    className:
      "bg-rose-500/15 text-rose-700 border-rose-500/40 dark:text-rose-300",
    label: "unhealthy",
    Icon: AlertCircle,
  },
  starting: {
    className:
      "bg-amber-500/15 text-amber-700 border-amber-500/40 dark:text-amber-300",
    label: "starting",
    Icon: Loader2,
  },
};

function ApplianceRoleHealthSection({ row }: { row: ApplianceRow }) {
  const entries = Object.entries(row.role_health ?? {});
  // Show services in a stable order — alphabetical by service name.
  entries.sort(([a], [b]) => a.localeCompare(b));
  return (
    <div className="border-t pt-4">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Service health
      </h3>
      <p className="mt-1 text-xs text-muted-foreground">
        Supervisor watchdog snapshot — refreshed every 5 min. ``missing`` /
        ``unhealthy`` for more than one cadence means the auto-heal kicker
        didn&apos;t bring the container back; SSH in and check{" "}
        <code>docker logs &lt;service&gt;</code>.
      </p>
      <div className="mt-2 overflow-hidden rounded-md border">
        <table className="w-full text-xs">
          <thead className="bg-muted/40 text-[10px] uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="px-3 py-1.5 text-left font-medium">Service</th>
              <th className="px-3 py-1.5 text-left font-medium">Role</th>
              <th className="px-3 py-1.5 text-left font-medium">Status</th>
              <th className="px-3 py-1.5 text-left font-medium">Since</th>
              <th className="px-3 py-1.5 text-left font-medium">Container</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {entries.map(([svc, h]) => {
              const style =
                WATCHDOG_STATUS_STYLES[h.status] ??
                WATCHDOG_STATUS_STYLES.unhealthy;
              const Icon = style.Icon;
              return (
                <tr key={svc}>
                  <td className="px-3 py-1.5 font-mono">{svc}</td>
                  <td className="px-3 py-1.5">{h.role}</td>
                  <td className="px-3 py-1.5">
                    <span
                      className={cn(
                        "inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium",
                        style.className,
                      )}
                    >
                      <Icon className="h-2.5 w-2.5" />
                      {style.label}
                    </span>
                  </td>
                  <td
                    className="px-3 py-1.5 text-muted-foreground"
                    title={h.since}
                  >
                    {formatRelativeSince(h.since)}
                  </td>
                  <td className="px-3 py-1.5 font-mono text-muted-foreground">
                    {h.container_id ?? "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ApplianceRoleAssignmentSection({
  row,
  onSaved,
}: {
  row: ApplianceRow;
  onSaved: (next: ApplianceRow) => void;
}) {
  const qc = useQueryClient();
  const caps = row.capabilities ?? {};
  const [roles, setRoles] = useState<Set<string>>(
    () => new Set(row.assigned_roles ?? []),
  );
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
  // Transient ``✓ Saved`` indicator next to the Save button. Cleared
  // after 2.5 s so the affirmative feedback doesn't linger forever.
  const [savedAt, setSavedAt] = useState<number | null>(null);
  useEffect(() => {
    if (savedAt === null) return;
    const t = setTimeout(() => setSavedAt(null), 2500);
    return () => clearTimeout(t);
  }, [savedAt]);
  // When the parent feeds us a refreshed row (after approve / save /
  // re-key) re-baseline the form state so ``dirty`` reads correctly.
  // Using JSON-stringified role list as the effect dep keeps Set
  // identity changes from causing infinite re-renders.
  const rolesKey = (row.assigned_roles ?? []).slice().sort().join(",");
  useEffect(() => {
    setRoles(new Set(row.assigned_roles ?? []));
    setDnsGroupId(row.assigned_dns_group_id ?? null);
    setDhcpGroupId(row.assigned_dhcp_group_id ?? null);
    setFirewallExtra(row.firewall_extra ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    row.id,
    rolesKey,
    row.assigned_dns_group_id,
    row.assigned_dhcp_group_id,
    row.firewall_extra,
  ]);

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
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ["appliance", "fleet"] });
      setSavedAt(Date.now());
      onSaved(updated);
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
  // ``dirty`` compares the live form state against the latest
  // server-side row (the row prop refreshes after save), not the
  // snapshot taken at first mount — otherwise the Save button would
  // stay enabled after a successful save until the operator closes
  // the modal.
  const dirty =
    JSON.stringify(Array.from(roles).sort()) !==
      JSON.stringify((row.assigned_roles ?? []).slice().sort()) ||
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

      {/* #170 Phase E2 — banner conflicts the operator should action
          before the role they just picked would actually bind on the
          host. We map the role chip to the port(s) it needs + only
          show the warning when there's a conflict on a port the
          chosen role would bind. */}
      <PortConflictBanner row={row} roles={roles} />

      {/* #170 Wave D follow-up — outcome of the supervisor's last
          docker-compose apply. Red banner with stderr-first-line on
          failure. Green-tinted chip on ready. */}
      <RoleSwitchStateBanner row={row} />

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
        {savedAt !== null && !save.isPending && (
          <span className="inline-flex items-center gap-1 text-xs text-emerald-700 dark:text-emerald-300">
            <CheckCircle2 className="h-3.5 w-3.5" />
            Saved
          </span>
        )}
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

// Normalise a per-slot version string. The supervisor's sidecar uses
// ``"unstamped"`` / ``"unreadable"`` / ``"unknown"`` for slots whose
// /etc/spatiumddi/appliance-release can't be read; render those as
// ``"—"`` since the actual content isn't useful to the operator.
function slotVersionLabel(version: string | null | undefined): string {
  if (!version) return "—";
  if (version === "unstamped" || version === "unreadable" || version === "unknown") {
    return "—";
  }
  return version;
}

// Pick the per-slot version off the row by slot name. Keeps the
// callers small + flat (one ternary instead of mismatched lookups).
function rowSlotVersion(
  row: Pick<ApplianceRow, "slot_a_version" | "slot_b_version">,
  slot: "slot_a" | "slot_b",
): string {
  return slotVersionLabel(
    slot === "slot_a" ? row.slot_a_version : row.slot_b_version,
  );
}

// Compact two-line slot column for the appliances list. One line per
// slot, each carrying the version + a tiny chip for the booted /
// default role. Hidden entirely on docker / k8s rows where the A/B
// partition layout doesn't exist.
function ApplianceSlotsCell({ row }: { row: ApplianceRow }) {
  if (row.deployment_kind && row.deployment_kind !== "appliance") {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  if (!row.slot_a_version && !row.slot_b_version) {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  return (
    <div className="flex flex-col gap-0.5 text-xs">
      {(["slot_a", "slot_b"] as const).map((slot) => {
        const isBooted = row.current_slot === slot;
        const isDefault = row.durable_default === slot;
        return (
          <div key={slot} className="flex items-center gap-1.5">
            <span className="font-mono text-muted-foreground">
              {slotLabel(slot)}
            </span>
            <span className="font-mono">{rowSlotVersion(row, slot)}</span>
            {isBooted && (
              <span className="rounded-full bg-emerald-500/10 px-1.5 py-0 text-[10px] font-medium uppercase text-emerald-700 dark:text-emerald-300">
                run
              </span>
            )}
            {isDefault && !isBooted && (
              <span className="rounded-full bg-blue-500/10 px-1.5 py-0 text-[10px] font-medium uppercase text-blue-700 dark:text-blue-300">
                def
              </span>
            )}
          </div>
        );
      })}
      {row.is_trial_boot && (
        <span className="rounded-full bg-amber-500/10 px-1.5 py-0 text-[10px] font-medium uppercase text-amber-700 dark:text-amber-300">
          trial boot
        </span>
      )}
    </div>
  );
}

// Per-slot card shown in the Fleet drilldown's OS & lifecycle
// section — two of these render side-by-side (slot A on the left,
// slot B on the right) carrying the version installed on the slot
// + role badges + action buttons. Mirrors the local-appliance OS
// Image card's ``SlotCardView`` styling so the visual language is
// the same on both surfaces.
//
// Action buttons fire the heartbeat-pickup pipeline:
//   * "Boot once"     → POST /set-next-boot      (grub-reboot, one-shot)
//   * "Set as default" → POST /set-default-slot   (grub-set-default,
//                                                  durable)
function ApplianceSlotCard({
  row,
  slot,
  onSetNextBoot,
  onSetDefault,
  busyNextBoot,
  busyDefault,
}: {
  row: ApplianceRow;
  slot: "slot_a" | "slot_b";
  onSetNextBoot: () => void;
  onSetDefault: () => void;
  busyNextBoot: boolean;
  busyDefault: boolean;
}) {
  const isBooted = row.current_slot === slot;
  const isDefault = row.durable_default === slot;
  const isTrial = isBooted && row.is_trial_boot;
  const desiredNext = row.desired_next_boot_slot === slot;
  const desiredDefault = row.desired_default_slot === slot;
  const otherSlot = slot === "slot_a" ? "slot_b" : "slot_a";

  // Outer card colouring follows the most-relevant role so the pair
  // has visual rhythm at a glance.
  const borderClass = isTrial
    ? "border-amber-500/50 bg-amber-500/5"
    : isBooted
      ? "border-emerald-500/40 bg-emerald-500/5"
      : "border-border bg-muted/40";

  // One-line subtext explaining the slot's role in plain English.
  let subtext: string;
  if (isTrial) {
    subtext = `Trial boot — reverts to slot ${slotLabel(row.durable_default)} on next reboot unless committed.`;
  } else if (isBooted && isDefault) {
    subtext = "Active · this is where the appliance boots.";
  } else if (isDefault && !isBooted) {
    subtext = "Durable default · next normal reboot lands here.";
  } else if (isBooted) {
    subtext = "Active · trial state without durable backing.";
  } else {
    subtext = "Inactive · candidate for upgrades or trial boot.";
  }

  const version = rowSlotVersion(row, slot);

  return (
    <div className={cn("flex flex-col rounded-md border p-2.5 text-xs", borderClass)}>
      <div className="flex items-center justify-between gap-2">
        <div className="font-mono font-semibold">Slot {slotLabel(slot)}</div>
        <div className="flex flex-wrap items-center gap-1">
          {isBooted && (
            <span className="inline-flex items-center rounded-md bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-emerald-700 dark:text-emerald-300">
              Booted
            </span>
          )}
          {isDefault && (
            <span className="inline-flex items-center rounded-md bg-blue-500/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-blue-700 dark:text-blue-300">
              Default
            </span>
          )}
          {isTrial && (
            <span className="inline-flex items-center rounded-md bg-yellow-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-yellow-700 dark:text-yellow-300">
              Trial
            </span>
          )}
        </div>
      </div>
      <div className="mt-1 font-mono text-[11px] text-muted-foreground">
        {version}
      </div>
      <div className="mt-1 text-[11px] text-muted-foreground">{subtext}</div>

      {/* Pending-intent banner. Auto-clears server-side once the
          supervisor reports the requested state landed. */}
      {(desiredNext || desiredDefault) && (
        <div className="mt-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-700 dark:text-amber-300">
          {desiredNext && <div>Boot-once requested · supervisor will arm on next heartbeat.</div>}
          {desiredDefault && <div>Set-as-default requested · supervisor will commit on next heartbeat.</div>}
        </div>
      )}

      {/* Action buttons. Only render the option that's meaningful:
          - "Boot once" only when this is NOT the running slot
            (one-shot grub-reboot into the other slot)
          - "Set as default" only when this slot isn't already the
            durable default. Doubles as the trial-commit affordance. */}
      <div className="mt-2 flex flex-wrap gap-1.5">
        {!isBooted && (
          <button
            type="button"
            onClick={onSetNextBoot}
            disabled={busyNextBoot || desiredNext}
            title={`Boot slot ${slotLabel(slot)} on the next reboot (one-shot — auto-reverts to slot ${slotLabel(otherSlot)} after that boot unless committed).`}
            className="inline-flex items-center gap-1 rounded-md border border-input bg-background px-2 py-1 text-[11px] hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busyNextBoot ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <RotateCcw className="h-3 w-3" />
            )}
            Boot once
          </button>
        )}
        {!isDefault && (
          <button
            type="button"
            onClick={onSetDefault}
            disabled={busyDefault || desiredDefault}
            title={
              isTrial
                ? `Commit this trial boot as durable (grub-set-default ${slot}).`
                : `Make slot ${slotLabel(slot)} the durable default boot.`
            }
            className="inline-flex items-center gap-1 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-2 py-1 text-[11px] text-emerald-700 hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-50 dark:text-emerald-300"
          >
            {busyDefault ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <CheckCircle2 className="h-3 w-3" />
            )}
            {isTrial ? "Approve trial" : "Set as default"}
          </button>
        )}
      </div>
    </div>
  );
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
      qc.invalidateQueries({ queryKey: ["appliance", "fleet"] });
      setTag("");
      setImageUrl("");
      setSlotImageId("");
    },
  });
  const clearUpgrade = useMutation({
    mutationFn: () => applianceApprovalApi.clearUpgrade(row.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["appliance", "fleet"] }),
  });
  const setNextBoot = useMutation({
    mutationFn: (slot: "slot_a" | "slot_b") =>
      applianceApprovalApi.setNextBootSlot(row.id, slot),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["appliance", "fleet"] }),
  });
  const setDefault = useMutation({
    mutationFn: (slot: "slot_a" | "slot_b") =>
      applianceApprovalApi.setDefaultSlot(row.id, slot),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["appliance", "fleet"] }),
  });
  const reboot = useMutation({
    mutationFn: () => applianceApprovalApi.scheduleReboot(row.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "fleet"] });
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

      {/* Per-slot version + boot-control cards. Two cards side-by-
          side carry the version installed on each A/B slot plus
          action buttons that ride the heartbeat-pickup pipeline. */}
      {isApplianceHost && (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {(["slot_a", "slot_b"] as const).map((slot) => (
            <ApplianceSlotCard
              key={slot}
              row={row}
              slot={slot}
              onSetNextBoot={() => setNextBoot.mutate(slot)}
              onSetDefault={() => setDefault.mutate(slot)}
              busyNextBoot={
                setNextBoot.isPending && setNextBoot.variables === slot
              }
              busyDefault={
                setDefault.isPending && setDefault.variables === slot
              }
            />
          ))}
        </div>
      )}
      {(setNextBoot.error || setDefault.error) && (
        <p className="mt-1 text-xs text-rose-700 dark:text-rose-300">
          {((setNextBoot.error ?? setDefault.error) as Error).message}
        </p>
      )}

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
                  No upgrade images uploaded yet. Open the &ldquo;Upgrade
                  images&rdquo; section above to upload one.
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
  // Slot images are heavy (typically ~700 MiB raw.xz) and a misclick
  // wipes the only on-server copy of an air-gap-cached release — gate
  // the delete behind a typed-confirm modal.
  const [deleteTarget, setDeleteTarget] = useState<SlotImage | null>(null);

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
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "slot-images"] });
      setDeleteTarget(null);
    },
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
        <p className="text-muted-foreground">No uploaded upgrade images yet.</p>
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
                    onClick={() => setDeleteTarget(img)}
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

      {deleteTarget && (
        <ConfirmModal
          open
          title="Delete upgrade image?"
          message={
            <>
              <p className="text-sm">
                Delete the uploaded <code>.raw.xz</code> for{" "}
                <strong>{deleteTarget.appliance_version}</strong>? The on-server
                copy is removed and any in-flight slot upgrade pointing at it
                will fail with a 404 on the next fetch. Re-uploading is
                operator-effort — typically a few hundred MiB.
              </p>
              {deleteTarget.notes && (
                <p className="mt-2 text-xs text-muted-foreground">
                  Notes:{" "}
                  <span className="text-foreground">{deleteTarget.notes}</span>
                </p>
              )}
              <p className="mt-2 text-xs text-muted-foreground">
                SHA-256:{" "}
                <code className="text-foreground">
                  {deleteTarget.sha256.slice(0, 12)}…
                  {deleteTarget.sha256.slice(-6)}
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
    </div>
  );
}

// ── PortConflictBanner (#170 Phase E2) ──────────────────────────

// Map a role-chip token to the heartbeat-body keys it would bind.
// Mirrors the supervisor's role_orchestrator probe list so the banner
// only fires when there's a conflict on a port the operator's picked
// role would actually need.
const _ROLE_PORT_KEYS: Record<string, string[]> = {
  "dns-bind9": ["udp_53", "tcp_53"],
  "dns-powerdns": ["udp_53", "tcp_53"],
  dhcp: ["udp_67"],
};

function _formatPortKey(key: string): string {
  // udp_53 → "UDP/53", tcp_53 → "TCP/53"
  const [proto, port] = key.split("_", 2);
  return `${proto.toUpperCase()}/${port}`;
}

function PortConflictBanner({
  row,
  roles,
}: {
  row: ApplianceRow;
  roles: Set<string>;
}) {
  const conflicts = row.port_conflicts ?? {};
  // Surface only conflicts on a port a currently-picked role would
  // bind. Operators on idle appliances don't care about a stray UDP/53
  // listener; they care once they assign a DNS role.
  const relevant: { key: string; users: string }[] = [];
  for (const role of roles) {
    for (const key of _ROLE_PORT_KEYS[role] ?? []) {
      if (conflicts[key] && !relevant.some((r) => r.key === key)) {
        relevant.push({ key, users: conflicts[key] });
      }
    }
  }
  if (relevant.length === 0) return null;
  return (
    <div className="mt-4 rounded-md border border-rose-500/40 bg-rose-500/10 p-3 text-xs">
      <div className="flex items-start gap-2">
        <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-rose-700 dark:text-rose-300" />
        <div>
          <p className="font-medium text-rose-700 dark:text-rose-300">
            Host port conflict — supervisor pre-flight failed
          </p>
          <p className="mt-1 text-muted-foreground">
            The supervisor probed{" "}
            {relevant.map((r, i) => (
              <span key={r.key}>
                {i > 0 ? ", " : ""}
                <code className="text-foreground">{_formatPortKey(r.key)}</code>
              </span>
            ))}{" "}
            and found a competing listener on the host. The service
            container&apos;s bind will silently lose to that daemon. SSH in +
            stop the conflicting process before applying the role assignment.
          </p>
          <ul className="mt-2 space-y-0.5 text-[11px]">
            {relevant.map((r) => (
              <li key={r.key} className="font-mono">
                {_formatPortKey(r.key)} → {r.users}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

// ── RoleSwitchStateBanner (#170 Wave D follow-up) ───────────────

function RoleSwitchStateBanner({ row }: { row: ApplianceRow }) {
  const state = row.role_switch_state;
  // Null / idle = nothing to surface (operator hasn't assigned a
  // role yet, or the supervisor cleared the state). ``ready`` is the
  // happy path — a soft green chip; we don't need to shout.
  if (!state || state === "idle") return null;
  if (state === "ready") {
    return (
      <div className="mt-3 inline-flex items-center gap-2 rounded-full bg-emerald-500/10 px-3 py-1 text-xs text-emerald-700 dark:text-emerald-300">
        <CheckCircle2 className="h-3.5 w-3.5" />
        Service containers up — supervisor reports{" "}
        <code className="font-mono">role_switch_state=ready</code>.
      </div>
    );
  }
  // ``failed`` — operator needs to know what broke.
  return (
    <div className="mt-3 rounded-md border border-rose-500/40 bg-rose-500/10 p-3 text-xs">
      <div className="flex items-start gap-2">
        <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-rose-700 dark:text-rose-300" />
        <div>
          <p className="font-medium text-rose-700 dark:text-rose-300">
            Service lifecycle apply failed
          </p>
          <p className="mt-1 text-muted-foreground">
            The supervisor's <code>docker compose</code> against the assigned
            role(s) returned a non-zero exit. The previous container state
            remains; SSH into the appliance to triage, or fix the underlying
            cause + the next heartbeat will retry automatically.
          </p>
          {row.role_switch_reason && (
            <p className="mt-2 font-mono text-[11px] text-foreground">
              {row.role_switch_reason}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
