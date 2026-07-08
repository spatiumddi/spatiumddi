import { useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  Clipboard,
  ClipboardCheck,
  Loader2,
  Pencil,
  Play,
  Plus,
  Power,
  RefreshCw,
  Trash2,
} from "lucide-react";

import { Modal, ModalTabs } from "@/components/ui/modal";
import { ConfirmModal } from "@/components/ui/confirm-modal";
import { HeaderButton } from "@/components/ui/header-button";
import { TagFilterChips } from "@/components/TagFilterChips";
import {
  applianceApprovalApi,
  formatApiError,
  ipamApi,
  wakeSchedulesApi,
  type ApplianceRow,
  type Subnet,
  type WolRun,
  type WolSchedule,
  type WolScheduleCreate,
  type WolSelectorMode,
  type WolTargetPreview,
  type WolTargetSelector,
  type WolVantage,
} from "@/lib/api";
import { cn } from "@/lib/utils";

// ─────────────────────────────────────────────────────────────────────
// Scheduled Wake-on-LAN (#586 Phase 1) — Tools page.
//
// Builds on the shipped one-shot WoL (#533): reuse the same server /
// Fleet-appliance vantage concept, extended with a recurring cron
// schedule, tag-targeted fleet fan-out, and a BUILT-IN holiday gate
// (blackout dates + term range). No external iCal / CalDAV here — that
// is Phase 2, deliberately absent.
//
// Wire prefix is ``/wake-scheduler`` (see ``wakeSchedulesApi``); the
// Python package is ``wol_schedules``.
// ─────────────────────────────────────────────────────────────────────

const inputCls =
  "block w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm focus:border-ring focus:outline-none focus:ring-1 focus:ring-ring";
const labelCls = "mb-1 block text-xs font-medium text-muted-foreground";

// An appliance is offerable as a vantage only when approved AND fresh
// (mirrors #533 / the backend ``agent_cmd.appliance_ready`` gate).
const APPLIANCE_ONLINE_STALE_MS = 90_000;
function applianceOnline(a: ApplianceRow): boolean {
  if (a.state !== "approved") return false;
  if (!a.last_seen_at) return false;
  const seen = Date.parse(a.last_seen_at);
  if (Number.isNaN(seen)) return false;
  return Date.now() - seen <= APPLIANCE_ONLINE_STALE_MS;
}

// WoL-flavoured cron presets. Unlike the backup pattern (UTC-only)
// these are interpreted in the schedule's own IANA timezone, so a
// "weekday 07:00" wake follows local DST.
const WOL_CRON_PRESETS: { label: string; value: string }[] = [
  { label: "Weekdays 07:00", value: "0 7 * * 1-5" },
  { label: "Weekdays 06:30", value: "30 6 * * 1-5" },
  { label: "Every day 07:00", value: "0 7 * * *" },
  { label: "Weekly Monday 06:00", value: "0 6 * * 1" },
  { label: "Every hour", value: "0 * * * *" },
];

const SELECTOR_MODES: {
  value: WolSelectorMode;
  label: string;
  help: string;
}[] = [
  {
    value: "address_tags",
    label: "By IP-address tags",
    help: "Wake every tracked IP whose tags match (the FOG-style per-workstation tagging). Primary mode.",
  },
  {
    value: "subnet_tags",
    label: "By subnet tags",
    help: "Wake every allocated IP inside subnets whose tags match.",
  },
  {
    value: "subnet",
    label: "By explicit subnets",
    help: "Wake every allocated IP inside the chosen subnets.",
  },
  {
    value: "hosts",
    label: "Explicit hosts",
    help: "An explicit IP-address list (built from the IPAM tables — pick hosts there, coming to this modal in a follow-up).",
  },
];

// ── date / status helpers ─────────────────────────────────────────────
function localDateTime(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function relTime(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  const diff = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diff < 0) return d.toLocaleString();
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
  return d.toLocaleDateString();
}

const STATUS_TONE: Record<string, string> = {
  ok: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  partial: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  skipped: "bg-zinc-500/15 text-zinc-600 dark:text-zinc-400",
  failed: "bg-rose-500/15 text-rose-600 dark:text-rose-400",
  in_progress: "bg-blue-500/15 text-blue-600 dark:text-blue-400",
};
function StatusChip({ status }: { status: string | null }) {
  if (!status) return <span className="text-muted-foreground">never</span>;
  return (
    <span
      className={cn(
        "inline-block rounded px-1.5 py-0.5 text-[11px] font-medium",
        STATUS_TONE[status] ?? "bg-muted text-muted-foreground",
      )}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}

// ── IPv4 CIDR math — for the router-setup-help snippets ────────────────
// Directed broadcast only exists for IPv4; IPv6 has no broadcast so the
// help block skips v6 subnets entirely.
function ipv4Parts(
  cidr: string,
): { network: string; wildcard: string; broadcast: string } | null {
  const m = /^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\/(\d{1,2})$/.exec(
    cidr.trim(),
  );
  if (!m) return null;
  const octets = m[1].split(".").map((o) => Number(o));
  if (octets.some((o) => o < 0 || o > 255)) return null;
  const prefix = Number(m[2]);
  if (prefix < 0 || prefix > 32) return null;
  const ipInt =
    ((octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]) >>>
    0;
  const maskInt = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  const netInt = (ipInt & maskInt) >>> 0;
  const bcastInt = (netInt | (~maskInt >>> 0)) >>> 0;
  const wildInt = (~maskInt >>> 0) >>> 0;
  const toDotted = (n: number) =>
    [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join(".");
  return {
    network: toDotted(netInt),
    wildcard: toDotted(wildInt),
    broadcast: toDotted(bcastInt),
  };
}

// ─────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────
type PageTab = "schedules" | "history";

export function WakeSchedulesPage() {
  const [tab, setTab] = useState<PageTab>("schedules");

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="border-b bg-card px-6 py-4">
        <div className="flex items-center gap-2">
          <Power className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">Scheduled Wake-on-LAN</h1>
        </div>
        <p className="mt-1 max-w-3xl text-xs text-muted-foreground">
          Wake a tag-targeted fleet on a recurring, calendar-aware schedule —
          skip holidays and stay inside the school / term calendar. Magic
          packets originate from the control-plane server or a Fleet appliance
          on the target segment (reusing the one-shot Wake-on-LAN vantage).
          Wake-on-LAN only <em>wakes</em> hosts; scheduled shutdown is out of
          scope (there is no on-host agent to power a PC down).
        </p>
      </div>

      <div className="flex-1 overflow-auto p-6">
        <div className="mb-4 flex items-center gap-1 border-b">
          <TabButton
            active={tab === "schedules"}
            onClick={() => setTab("schedules")}
          >
            Schedules
          </TabButton>
          <TabButton
            active={tab === "history"}
            onClick={() => setTab("history")}
          >
            History
          </TabButton>
        </div>

        {tab === "schedules" && <SchedulesTab />}
        {tab === "history" && <HistoryTab />}
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "-mb-px border-b-2 px-4 py-2 text-sm transition-colors",
        active
          ? "border-primary font-medium text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Schedules tab
// ─────────────────────────────────────────────────────────────────────
function SchedulesTab() {
  const qc = useQueryClient();
  const schedulesQ = useQuery({
    queryKey: ["wol-schedules"],
    queryFn: () => wakeSchedulesApi.list(),
  });
  const [editing, setEditing] = useState<
    { mode: "create" } | { mode: "edit"; schedule: WolSchedule } | null
  >(null);
  const [confirm, setConfirm] = useState<{
    title: string;
    message: ReactNode;
    confirmLabel: string;
    tone: "default" | "destructive";
    run: () => void;
  } | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  const toggle = useMutation({
    mutationFn: (s: WolSchedule) =>
      wakeSchedulesApi.update(s.id, { enabled: !s.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["wol-schedules"] }),
    onError: (e) => setBanner(formatApiError(e, "Failed to toggle schedule")),
  });

  const runNow = useMutation({
    mutationFn: (id: string) => wakeSchedulesApi.runNow(id),
    onSuccess: (run) => {
      setBanner(
        `Wake dispatched — ${run.sent_count} sent, ${run.skipped_count} skipped, ${run.failed_count} failed.`,
      );
      qc.invalidateQueries({ queryKey: ["wol-schedules"] });
      qc.invalidateQueries({ queryKey: ["wol-runs"] });
      qc.invalidateQueries({ queryKey: ["wol-schedule-preview"] });
      setConfirm(null);
    },
    onError: (e) => {
      setBanner(formatApiError(e, "Run-now failed"));
      setConfirm(null);
    },
  });

  const del = useMutation({
    mutationFn: (id: string) => wakeSchedulesApi.remove(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["wol-schedules"] });
      setConfirm(null);
    },
    onError: (e) => {
      setBanner(formatApiError(e, "Delete failed"));
      setConfirm(null);
    },
  });

  const rows = schedulesQ.data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-2xl text-xs text-muted-foreground">
          Each schedule fans a magic packet out to every matching host on its
          cron tick, unless the day falls on a blackout date or outside the term
          range. Stagger large fleets — waking hundreds of PCs in the same
          second is a power inrush and a DHCP / PXE thundering herd.
        </p>
        <div className="flex shrink-0 items-center gap-2">
          <HeaderButton
            icon={RefreshCw}
            iconClassName={schedulesQ.isFetching ? "animate-spin" : undefined}
            onClick={() => {
              schedulesQ.refetch();
              qc.invalidateQueries({ queryKey: ["wol-schedule-preview"] });
            }}
          >
            Refresh
          </HeaderButton>
          <HeaderButton
            variant="primary"
            icon={Plus}
            onClick={() => setEditing({ mode: "create" })}
          >
            New schedule
          </HeaderButton>
        </div>
      </div>

      {banner && (
        <div className="flex items-start justify-between gap-3 rounded-md border bg-muted/40 px-3 py-2 text-sm">
          <span className="min-w-0 flex-1 break-words">{banner}</span>
          <button
            type="button"
            className="shrink-0 text-muted-foreground hover:text-foreground"
            onClick={() => setBanner(null)}
          >
            ✕
          </button>
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border">
        <table className="min-w-[880px] w-full text-sm">
          <thead className="border-b bg-muted/40 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">Name</th>
              <th className="px-3 py-2 font-medium">Targets</th>
              <th className="px-3 py-2 font-medium">Next run (local)</th>
              <th className="px-3 py-2 font-medium">Last run</th>
              <th className="px-3 py-2 font-medium">Enabled</th>
              <th className="px-3 py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {schedulesQ.isLoading && (
              <tr>
                <td
                  colSpan={6}
                  className="px-3 py-8 text-center text-muted-foreground"
                >
                  <Loader2 className="mx-auto h-5 w-5 animate-spin" />
                </td>
              </tr>
            )}
            {!schedulesQ.isLoading && rows.length === 0 && (
              <tr>
                <td
                  colSpan={6}
                  className="px-3 py-8 text-center text-muted-foreground"
                >
                  No wake schedules yet. Create one to wake a tagged fleet on a
                  recurring, holiday-aware schedule.
                </td>
              </tr>
            )}
            {rows.map((s) => (
              <ScheduleRow
                key={s.id}
                schedule={s}
                busyToggle={toggle.isPending && toggle.variables?.id === s.id}
                onToggle={() => toggle.mutate(s)}
                onEdit={() => setEditing({ mode: "edit", schedule: s })}
                onRunNow={() =>
                  setConfirm({
                    title: "Run this schedule now?",
                    message: (
                      <>
                        Sends magic packets to every matching host immediately,
                        bypassing the built-in holiday gate. This is a
                        fire-and-forget wake, not a shutdown.
                      </>
                    ),
                    confirmLabel: "Wake now",
                    tone: "default",
                    run: () => runNow.mutate(s.id),
                  })
                }
                onDelete={() =>
                  setConfirm({
                    title: `Delete "${s.name}"?`,
                    message: (
                      <>
                        Removes the schedule and stops all future wakes. Run
                        history is retained. This cannot be undone.
                      </>
                    ),
                    confirmLabel: "Delete",
                    tone: "destructive",
                    run: () => del.mutate(s.id),
                  })
                }
              />
            ))}
          </tbody>
        </table>
      </div>

      {editing && (
        <WolScheduleModal
          existing={editing.mode === "edit" ? editing.schedule : null}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            qc.invalidateQueries({ queryKey: ["wol-schedules"] });
            qc.invalidateQueries({ queryKey: ["wol-schedule-preview"] });
          }}
        />
      )}

      <ConfirmModal
        open={!!confirm}
        title={confirm?.title ?? ""}
        message={confirm?.message ?? ""}
        confirmLabel={confirm?.confirmLabel}
        tone={confirm?.tone}
        loading={runNow.isPending || del.isPending}
        onConfirm={() => confirm?.run()}
        onClose={() => setConfirm(null)}
      />
    </div>
  );
}

function ScheduleRow({
  schedule,
  busyToggle,
  onToggle,
  onEdit,
  onRunNow,
  onDelete,
}: {
  schedule: WolSchedule;
  busyToggle: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onRunNow: () => void;
  onDelete: () => void;
}) {
  // Live per-schedule target preview → "N hosts · M no-MAC".
  const previewQ = useQuery({
    queryKey: ["wol-schedule-preview", schedule.id],
    queryFn: () => wakeSchedulesApi.previewScheduleTargets(schedule.id),
    staleTime: 60_000,
  });
  const p = previewQ.data;

  return (
    <tr className="border-b last:border-0 align-top">
      <td className="px-3 py-2">
        <div className="font-medium">{schedule.name}</div>
        {schedule.description && (
          <div className="text-xs text-muted-foreground">
            {schedule.description}
          </div>
        )}
        <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
          {schedule.schedule_cron ? schedule.schedule_cron : "manual only"} ·{" "}
          {schedule.timezone}
        </div>
      </td>
      <td className="px-3 py-2">
        {previewQ.isLoading ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
        ) : p ? (
          <span className="text-sm">
            <span className="font-medium">{p.wake_count}</span> hosts
            {p.mac_less_count > 0 && (
              <span className="text-amber-600 dark:text-amber-400">
                {" "}
                · {p.mac_less_count} no-MAC
              </span>
            )}
          </span>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </td>
      <td className="px-3 py-2 text-sm">
        {localDateTime(schedule.next_run_at)}
      </td>
      <td className="px-3 py-2">
        <div className="flex flex-col gap-0.5">
          <StatusChip status={schedule.last_run_status} />
          <span className="text-xs text-muted-foreground">
            {relTime(schedule.last_run_at)}
          </span>
          {schedule.last_run_skip_reason && (
            <span className="text-[11px] text-muted-foreground">
              skip: {schedule.last_run_skip_reason.replace(/_/g, " ")}
            </span>
          )}
        </div>
      </td>
      <td className="px-3 py-2">
        <button
          type="button"
          onClick={onToggle}
          disabled={busyToggle}
          aria-label={schedule.enabled ? "Disable" : "Enable"}
          className={cn(
            "relative inline-flex h-5 w-9 items-center rounded-full transition-colors disabled:opacity-50",
            schedule.enabled ? "bg-primary" : "bg-muted-foreground/30",
          )}
        >
          <span
            className={cn(
              "inline-block h-4 w-4 transform rounded-full bg-white transition-transform",
              schedule.enabled ? "translate-x-4" : "translate-x-0.5",
            )}
          />
        </button>
      </td>
      <td className="px-3 py-2">
        <div className="flex shrink-0 items-center justify-end gap-1">
          <IconBtn title="Run now" onClick={onRunNow}>
            <Play className="h-4 w-4" />
          </IconBtn>
          <IconBtn title="Edit" onClick={onEdit}>
            <Pencil className="h-4 w-4" />
          </IconBtn>
          <IconBtn title="Delete" tone="destructive" onClick={onDelete}>
            <Trash2 className="h-4 w-4" />
          </IconBtn>
        </div>
      </td>
    </tr>
  );
}

function IconBtn({
  title,
  tone = "default",
  onClick,
  children,
}: {
  title: string;
  tone?: "default" | "destructive";
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={cn(
        "rounded p-1.5 hover:bg-muted",
        tone === "destructive"
          ? "text-muted-foreground hover:text-destructive"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

// ─────────────────────────────────────────────────────────────────────
// History tab
// ─────────────────────────────────────────────────────────────────────
function HistoryTab() {
  const runsQ = useQuery({
    queryKey: ["wol-runs"],
    queryFn: () => wakeSchedulesApi.listRuns({ limit: 100 }),
  });
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const rows = runsQ.data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-2xl text-xs text-muted-foreground">
          Every fire — scheduled or manual — is logged here, including wakes
          skipped because the day was a blackout date or fell outside the term
          range. Expand a row for the per-host outcome.
        </p>
        <HeaderButton
          icon={RefreshCw}
          iconClassName={runsQ.isFetching ? "animate-spin" : undefined}
          onClick={() => runsQ.refetch()}
        >
          Refresh
        </HeaderButton>
      </div>

      <div className="overflow-x-auto rounded-lg border">
        <table className="min-w-[820px] w-full text-sm">
          <thead className="border-b bg-muted/40 text-left text-xs text-muted-foreground">
            <tr>
              <th className="w-8 px-3 py-2" />
              <th className="px-3 py-2 font-medium">Started</th>
              <th className="px-3 py-2 font-medium">Trigger</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Targets</th>
              <th className="px-3 py-2 font-medium">Sent</th>
              <th className="px-3 py-2 font-medium">Skipped</th>
              <th className="px-3 py-2 font-medium">Failed</th>
            </tr>
          </thead>
          <tbody>
            {runsQ.isLoading && (
              <tr>
                <td
                  colSpan={8}
                  className="px-3 py-8 text-center text-muted-foreground"
                >
                  <Loader2 className="mx-auto h-5 w-5 animate-spin" />
                </td>
              </tr>
            )}
            {!runsQ.isLoading && rows.length === 0 && (
              <tr>
                <td
                  colSpan={8}
                  className="px-3 py-8 text-center text-muted-foreground"
                >
                  No runs recorded yet.
                </td>
              </tr>
            )}
            {rows.map((run) => (
              <RunRow
                key={run.id}
                run={run}
                open={openRunId === run.id}
                onToggle={() =>
                  setOpenRunId(openRunId === run.id ? null : run.id)
                }
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RunRow({
  run,
  open,
  onToggle,
}: {
  run: WolRun;
  open: boolean;
  onToggle: () => void;
}) {
  const detailQ = useQuery({
    queryKey: ["wol-run", run.id],
    queryFn: () => wakeSchedulesApi.getRun(run.id),
    enabled: open,
  });

  return (
    <>
      <tr
        className="cursor-pointer border-b last:border-0 hover:bg-muted/30"
        onClick={onToggle}
      >
        <td className="px-3 py-2 text-muted-foreground">
          {open ? (
            <ChevronDown className="h-4 w-4" />
          ) : (
            <ChevronRight className="h-4 w-4" />
          )}
        </td>
        <td className="px-3 py-2">{localDateTime(run.started_at)}</td>
        <td className="px-3 py-2">
          <span className="rounded bg-muted px-1.5 py-0.5 text-[11px]">
            {run.trigger}
          </span>
        </td>
        <td className="px-3 py-2">
          <StatusChip status={run.status} />
          {run.skip_reason && (
            <span className="ml-1 text-[11px] text-muted-foreground">
              ({run.skip_reason.replace(/_/g, " ")})
            </span>
          )}
        </td>
        <td className="px-3 py-2">{run.target_count}</td>
        <td className="px-3 py-2 text-emerald-600 dark:text-emerald-400">
          {run.sent_count}
        </td>
        <td className="px-3 py-2">{run.skipped_count}</td>
        <td className="px-3 py-2 text-rose-600 dark:text-rose-400">
          {run.failed_count}
        </td>
      </tr>
      {open && (
        <tr className="border-b bg-muted/20">
          <td colSpan={8} className="px-3 py-3">
            {run.error && (
              <div className="mb-2 rounded border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs text-rose-600 dark:text-rose-400">
                {run.error}
              </div>
            )}
            {detailQ.isLoading ? (
              <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
            ) : detailQ.data && detailQ.data.targets.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="min-w-[620px] w-full text-xs">
                  <thead className="text-left text-muted-foreground">
                    <tr>
                      <th className="px-2 py-1 font-medium">IP</th>
                      <th className="px-2 py-1 font-medium">MAC</th>
                      <th className="px-2 py-1 font-medium">Broadcast</th>
                      <th className="px-2 py-1 font-medium">MAC source</th>
                      <th className="px-2 py-1 font-medium">Result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detailQ.data.targets.map((t) => (
                      <tr key={t.id} className="border-t border-border/50">
                        <td className="px-2 py-1 font-mono break-all">
                          {t.address ?? "—"}
                        </td>
                        <td className="px-2 py-1 font-mono break-all">
                          {t.mac ?? "—"}
                        </td>
                        <td className="px-2 py-1 font-mono break-all">
                          {t.broadcast ?? "—"}
                        </td>
                        <td className="px-2 py-1">{t.mac_source ?? "—"}</td>
                        <td className="px-2 py-1">
                          {t.sent ? (
                            <span className="text-emerald-600 dark:text-emerald-400">
                              sent
                            </span>
                          ) : (
                            <span className="text-rose-600 dark:text-rose-400">
                              {t.skip_reason ?? t.error ?? "not sent"}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">
                No per-host detail recorded for this run.
              </p>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Create / edit modal
// ─────────────────────────────────────────────────────────────────────
type ModalTab = "target" | "schedule" | "holiday" | "send";

const browserTz = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
})();

function useIanaTimezones(): string[] {
  return useMemo(() => {
    type IntlWithList = typeof Intl & {
      supportedValuesOf?: (key: "timeZone") => string[];
    };
    const I = Intl as IntlWithList;
    if (typeof I.supportedValuesOf === "function") {
      try {
        return I.supportedValuesOf("timeZone");
      } catch {
        return ["UTC"];
      }
    }
    return ["UTC"];
  }, []);
}

function WolScheduleModal({
  existing,
  onClose,
  onSaved,
}: {
  existing: WolSchedule | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [tab, setTab] = useState<ModalTab>("target");

  // ── form state ──────────────────────────────────────────────────────
  const [name, setName] = useState(existing?.name ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);

  const [mode, setMode] = useState<WolSelectorMode>(
    existing?.target_selector.mode ?? "address_tags",
  );
  const [tags, setTags] = useState<string[]>(
    existing?.target_selector.tags ?? [],
  );
  const [subnetIds, setSubnetIds] = useState<string[]>(
    existing?.target_selector.subnet_ids ?? [],
  );

  const [cron, setCron] = useState(existing?.schedule_cron ?? "0 7 * * 1-5");
  const [timezone, setTimezone] = useState(existing?.timezone ?? browserTz);

  const [blackoutDates, setBlackoutDates] = useState<string[]>(
    existing?.blackout_dates ?? [],
  );
  const [activeFrom, setActiveFrom] = useState(existing?.active_from ?? "");
  const [activeUntil, setActiveUntil] = useState(existing?.active_until ?? "");

  const [vantage, setVantage] = useState<WolVantage>(
    existing?.vantage ?? { kind: "server", id: null },
  );
  const [repeatCount, setRepeatCount] = useState(existing?.repeat_count ?? 2);
  const [repeatIntervalMs, setRepeatIntervalMs] = useState(
    existing?.repeat_interval_ms ?? 150,
  );
  const [staggerMs, setStaggerMs] = useState(existing?.stagger_ms ?? 40);
  const [port, setPort] = useState(existing?.port ?? 9);
  // Router-help only — the vantage's source IP. Never persisted.
  const [senderIp, setSenderIp] = useState("");

  const [error, setError] = useState<string | null>(null);
  const ianaList = useIanaTimezones();
  const tzValid = ianaList.includes(timezone.trim());

  const selector: WolTargetSelector = useMemo(
    () => ({
      mode,
      tags,
      subnet_ids: subnetIds,
      address_ids: existing?.target_selector.address_ids ?? [],
    }),
    [mode, tags, subnetIds, existing],
  );

  // Live preview of the unsaved selector.
  const hasSelection =
    (mode === "address_tags" || mode === "subnet_tags") && tags.length > 0
      ? true
      : mode === "subnet" && subnetIds.length > 0
        ? true
        : false;
  const previewQ = useQuery({
    queryKey: ["wol-preview", JSON.stringify(selector)],
    queryFn: () =>
      wakeSchedulesApi.previewTargets({ target_selector: selector }),
    enabled: hasSelection,
    staleTime: 15_000,
  });

  const save = useMutation({
    mutationFn: () => {
      const body: WolScheduleCreate = {
        name: name.trim(),
        description: description.trim() || null,
        enabled,
        target_selector: selector,
        schedule_cron: cron.trim() || null,
        timezone: timezone.trim(),
        blackout_dates: blackoutDates,
        active_from: activeFrom || null,
        active_until: activeUntil || null,
        vantage,
        repeat_count: repeatCount,
        repeat_interval_ms: repeatIntervalMs,
        stagger_ms: staggerMs,
        port,
      };
      return existing
        ? wakeSchedulesApi.update(existing.id, body)
        : wakeSchedulesApi.create(body);
    },
    onSuccess: onSaved,
    onError: (e) => setError(formatApiError(e, "Failed to save schedule")),
  });

  // Send-option ranges mirror the backend schema (schemas.py) so an
  // out-of-range value fails inline instead of as a generic 422 banner.
  const sendErrors = {
    repeatCount:
      Number.isInteger(repeatCount) && repeatCount >= 1 && repeatCount <= 10
        ? null
        : "Must be 1–10.",
    repeatIntervalMs:
      Number.isInteger(repeatIntervalMs) &&
      repeatIntervalMs >= 0 &&
      repeatIntervalMs <= 10_000
        ? null
        : "Must be 0–10000.",
    staggerMs:
      Number.isInteger(staggerMs) && staggerMs >= 0 && staggerMs <= 60_000
        ? null
        : "Must be 0–60000.",
    port:
      Number.isInteger(port) && port >= 1 && port <= 65535
        ? null
        : "Must be 1–65535.",
  };
  const sendValid = Object.values(sendErrors).every((e) => e === null);

  const canSave =
    name.trim().length > 0 && tzValid && sendValid && !save.isPending;

  return (
    <Modal
      title={existing ? `Edit "${existing.name}"` : "New wake schedule"}
      onClose={onClose}
      wide
    >
      <div className="space-y-4">
        {/* Pinned identity fields above the tabs */}
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className={labelCls}>Name</label>
            <input
              className={inputCls}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Classroom PCs — morning boot"
              autoFocus
            />
          </div>
          <div>
            <label className={labelCls}>Description</label>
            <input
              className={inputCls}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Optional"
            />
          </div>
        </div>

        <ModalTabs<ModalTab>
          tabs={[
            { key: "target", label: "Target" },
            { key: "schedule", label: "Schedule" },
            { key: "holiday", label: "Holiday gate" },
            { key: "send", label: "Send options" },
          ]}
          active={tab}
          onChange={setTab}
        />

        {tab === "target" && (
          <TargetStep
            mode={mode}
            setMode={setMode}
            tags={tags}
            setTags={setTags}
            subnetIds={subnetIds}
            setSubnetIds={setSubnetIds}
            preview={previewQ.data}
            previewing={previewQ.isFetching}
            hasSelection={hasSelection}
          />
        )}

        {tab === "schedule" && (
          <ScheduleStep
            cron={cron}
            setCron={setCron}
            timezone={timezone}
            setTimezone={setTimezone}
            tzValid={tzValid}
            ianaList={ianaList}
          />
        )}

        {tab === "holiday" && (
          <HolidayStep
            blackoutDates={blackoutDates}
            setBlackoutDates={setBlackoutDates}
            activeFrom={activeFrom}
            setActiveFrom={setActiveFrom}
            activeUntil={activeUntil}
            setActiveUntil={setActiveUntil}
          />
        )}

        {tab === "send" && (
          <SendStep
            vantage={vantage}
            setVantage={setVantage}
            repeatCount={repeatCount}
            setRepeatCount={setRepeatCount}
            repeatIntervalMs={repeatIntervalMs}
            setRepeatIntervalMs={setRepeatIntervalMs}
            staggerMs={staggerMs}
            setStaggerMs={setStaggerMs}
            port={port}
            setPort={setPort}
            senderIp={senderIp}
            setSenderIp={setSenderIp}
            preview={previewQ.data}
            subnetIds={subnetIds}
            mode={mode}
            errors={sendErrors}
          />
        )}

        {error && (
          <div className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-600 dark:text-rose-400">
            {error}
          </div>
        )}

        <div className="flex items-center justify-between gap-2 border-t pt-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            Enabled (swept by the beat task)
          </label>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={!canSave}
              onClick={() => {
                setError(null);
                save.mutate();
              }}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {save.isPending && (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              )}
              {existing ? "Save changes" : "Create schedule"}
            </button>
          </div>
        </div>
      </div>
    </Modal>
  );
}

// ── Target step ───────────────────────────────────────────────────────
function TargetStep({
  mode,
  setMode,
  tags,
  setTags,
  subnetIds,
  setSubnetIds,
  preview,
  previewing,
  hasSelection,
}: {
  mode: WolSelectorMode;
  setMode: (m: WolSelectorMode) => void;
  tags: string[];
  setTags: (t: string[]) => void;
  subnetIds: string[];
  setSubnetIds: (s: string[]) => void;
  preview: WolTargetPreview | undefined;
  previewing: boolean;
  hasSelection: boolean;
}) {
  const subnetsQ = useQuery({
    queryKey: ["ipam-subnets-all"],
    queryFn: () => ipamApi.listSubnets(),
    enabled: mode === "subnet",
  });
  const modeInfo = SELECTOR_MODES.find((m) => m.value === mode)!;

  return (
    <div className="space-y-3">
      <div>
        <label className={labelCls}>Match hosts</label>
        <select
          className={inputCls}
          value={mode}
          onChange={(e) => setMode(e.target.value as WolSelectorMode)}
        >
          {SELECTOR_MODES.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </select>
        <p className="mt-1 text-[11px] text-muted-foreground">
          {modeInfo.help}
        </p>
      </div>

      {(mode === "address_tags" || mode === "subnet_tags") && (
        <div>
          <label className={labelCls}>
            Tags (ANDed — <code>key</code> or <code>key:value</code>)
          </label>
          <TagFilterChips value={tags} onChange={setTags} />
        </div>
      )}

      {mode === "subnet" && (
        <div>
          <label className={labelCls}>Subnets</label>
          {subnetsQ.isLoading ? (
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
          ) : (
            <div className="max-h-48 space-y-1 overflow-auto rounded-md border p-2">
              {(subnetsQ.data ?? [])
                .filter((s: Subnet) => (s.kind ?? "unicast") === "unicast")
                .map((s: Subnet) => (
                  <label key={s.id} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={subnetIds.includes(s.id)}
                      onChange={(e) =>
                        setSubnetIds(
                          e.target.checked
                            ? [...subnetIds, s.id]
                            : subnetIds.filter((x) => x !== s.id),
                        )
                      }
                    />
                    <span className="font-mono">{s.network}</span>
                    {s.name && (
                      <span className="text-muted-foreground">{s.name}</span>
                    )}
                  </label>
                ))}
            </div>
          )}
        </div>
      )}

      {mode === "hosts" && (
        <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
          Explicit host lists are built from the IPAM tables. Tag your target
          IPs and use “By IP-address tags” for now — direct host picking lands
          in a follow-up.
        </p>
      )}

      {/* Live match count */}
      <div className="rounded-md border bg-muted/30 px-3 py-2 text-sm">
        {!hasSelection ? (
          <span className="text-muted-foreground">
            Pick tags or subnets to see the live match count.
          </span>
        ) : previewing ? (
          <span className="inline-flex items-center gap-2 text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Resolving fleet…
          </span>
        ) : preview ? (
          <span>
            <span className="font-medium">{preview.wake_count}</span> hosts will
            wake
            {preview.mac_less_count > 0 && (
              <span className="text-amber-600 dark:text-amber-400">
                {" "}
                · {preview.mac_less_count} matched but have no known MAC
                (skipped)
              </span>
            )}
            {preview.matched_count === 0 && (
              <span className="text-muted-foreground"> · no matches yet</span>
            )}
          </span>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </div>
    </div>
  );
}

// ── Schedule step ─────────────────────────────────────────────────────
const CUSTOM_CRON = "__custom__";
function ScheduleStep({
  cron,
  setCron,
  timezone,
  setTimezone,
  tzValid,
  ianaList,
}: {
  cron: string;
  setCron: (c: string) => void;
  timezone: string;
  setTimezone: (t: string) => void;
  tzValid: boolean;
  ianaList: string[];
}) {
  const presetMatch = WOL_CRON_PRESETS.find((p) => p.value === cron.trim());
  const selectValue =
    cron.trim() === "" ? "" : presetMatch ? cron.trim() : CUSTOM_CRON;

  return (
    <div className="space-y-3">
      <div>
        <label className={labelCls}>Recurrence</label>
        <select
          className={inputCls}
          value={selectValue}
          onChange={(e) => {
            const v = e.target.value;
            if (v === "") setCron("");
            else if (v === CUSTOM_CRON) setCron(cron.trim() || "0 7 * * 1-5");
            else setCron(v);
          }}
        >
          <option value="">Manual only (no automatic wake)</option>
          {WOL_CRON_PRESETS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label} ({p.value})
            </option>
          ))}
          <option value={CUSTOM_CRON}>Custom cron…</option>
        </select>
      </div>

      <div>
        <label className={labelCls}>Cron expression (5-field)</label>
        <input
          className={cn(inputCls, "font-mono")}
          value={cron}
          onChange={(e) => setCron(e.target.value)}
          placeholder="0 7 * * 1-5"
        />
        <p className="mt-1 text-[11px] text-muted-foreground">
          Interpreted in the timezone below (unlike the UTC-only backup cron),
          so a “07:00 weekdays” wake follows local DST. Empty = manual only.
        </p>
      </div>

      <div>
        <label className={labelCls}>Timezone (IANA)</label>
        <input
          list="wol-iana-timezones"
          className={cn(
            inputCls,
            "font-mono",
            !tzValid && "border-destructive focus:ring-destructive",
          )}
          value={timezone}
          onChange={(e) => setTimezone(e.target.value)}
          placeholder="America/New_York"
        />
        <datalist id="wol-iana-timezones">
          {ianaList.map((n) => (
            <option key={n} value={n} />
          ))}
        </datalist>
        {!tzValid && (
          <p className="mt-1 text-[11px] text-destructive">
            Not a recognised IANA timezone name.
          </p>
        )}
      </div>
    </div>
  );
}

// ── Holiday gate step (built-in — NO external calendar in Phase 1) ─────
function HolidayStep({
  blackoutDates,
  setBlackoutDates,
  activeFrom,
  setActiveFrom,
  activeUntil,
  setActiveUntil,
}: {
  blackoutDates: string[];
  setBlackoutDates: (d: string[]) => void;
  activeFrom: string;
  setActiveFrom: (d: string) => void;
  activeUntil: string;
  setActiveUntil: (d: string) => void;
}) {
  const [draft, setDraft] = useState("");

  const addBlackout = () => {
    const v = draft.trim();
    if (!v || blackoutDates.includes(v)) return;
    setBlackoutDates([...blackoutDates, v].sort());
    setDraft("");
  };

  return (
    <div className="space-y-4">
      <p className="rounded-md border bg-muted/30 px-3 py-2 text-[11px] text-muted-foreground">
        Phase 1 uses a built-in calendar: specific blackout dates (holidays) and
        an optional term range. A recurring wake is skipped — and logged with a
        skip reason, not silently dropped — on any blackout date or outside the
        term window. Subscribing to an external iCal / CalDAV calendar is a
        later phase.
      </p>

      <div>
        <label className={labelCls}>Blackout dates (holidays)</label>
        <div className="flex items-center gap-2">
          <input
            type="date"
            className={inputCls}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <button
            type="button"
            onClick={addBlackout}
            disabled={!draft}
            className="shrink-0 rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
          >
            Add
          </button>
        </div>
        {blackoutDates.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {blackoutDates.map((d) => (
              <span
                key={d}
                className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-xs font-mono"
              >
                {d}
                <button
                  type="button"
                  onClick={() =>
                    setBlackoutDates(blackoutDates.filter((x) => x !== d))
                  }
                  className="text-muted-foreground hover:text-destructive"
                >
                  ✕
                </button>
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label className={labelCls}>Term active from</label>
          <input
            type="date"
            className={inputCls}
            value={activeFrom}
            onChange={(e) => setActiveFrom(e.target.value)}
          />
        </div>
        <div>
          <label className={labelCls}>Term active until</label>
          <input
            type="date"
            className={inputCls}
            value={activeUntil}
            onChange={(e) => setActiveUntil(e.target.value)}
          />
        </div>
      </div>
      <p className="text-[11px] text-muted-foreground">
        Leave the term range empty to run year-round (blackout dates still
        apply). Both bounds are inclusive and evaluated in the schedule’s
        timezone.
      </p>
    </div>
  );
}

// ── Send options step ─────────────────────────────────────────────────
function SendStep({
  vantage,
  setVantage,
  repeatCount,
  setRepeatCount,
  repeatIntervalMs,
  setRepeatIntervalMs,
  staggerMs,
  setStaggerMs,
  port,
  setPort,
  senderIp,
  setSenderIp,
  preview,
  subnetIds,
  mode,
  errors,
}: {
  vantage: WolVantage;
  setVantage: (v: WolVantage) => void;
  repeatCount: number;
  setRepeatCount: (n: number) => void;
  repeatIntervalMs: number;
  setRepeatIntervalMs: (n: number) => void;
  staggerMs: number;
  setStaggerMs: (n: number) => void;
  port: number;
  setPort: (n: number) => void;
  senderIp: string;
  setSenderIp: (s: string) => void;
  preview: WolTargetPreview | undefined;
  subnetIds: string[];
  mode: WolSelectorMode;
  errors: {
    repeatCount: string | null;
    repeatIntervalMs: string | null;
    staggerMs: string | null;
    port: string | null;
  };
}) {
  const appliancesQ = useQuery({
    queryKey: ["appliance-approval-list"],
    queryFn: () => applianceApprovalApi.list(),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
  const onlineAppliances = useMemo(
    () => (appliancesQ.data ?? []).filter(applianceOnline),
    [appliancesQ.data],
  );

  const vantageSelection =
    vantage.kind === "server" ? "server" : (vantage.id ?? "server");

  // Resolve target subnets for the router-help block. We map the preview
  // sample's subnet_id → its CIDR (via the subnet list) and directed
  // broadcast (the sample carries it directly).
  const subnetsQ = useQuery({
    queryKey: ["ipam-subnets-all"],
    queryFn: () => ipamApi.listSubnets(),
    enabled: vantage.kind === "server",
  });
  const targetSubnets = useMemo(() => {
    const byId = new Map<string, Subnet>();
    for (const s of subnetsQ.data ?? []) byId.set(s.id, s);
    const seen = new Map<
      string,
      { cidr: string; name: string; broadcast: string }
    >();
    // From explicit-subnet mode, use the picked ids directly.
    if (mode === "subnet") {
      for (const id of subnetIds) {
        const s = byId.get(id);
        if (s) {
          const parts = ipv4Parts(s.network);
          if (parts)
            seen.set(id, {
              cidr: s.network,
              name: s.name,
              broadcast: parts.broadcast,
            });
        }
      }
    }
    // From the resolved preview sample, group by subnet_id.
    for (const w of preview?.sample ?? []) {
      if (!w.subnet_id || seen.has(w.subnet_id)) continue;
      const s = byId.get(w.subnet_id);
      const cidr = s?.network ?? "";
      if (cidr && !ipv4Parts(cidr)) continue; // skip IPv6
      seen.set(w.subnet_id, {
        cidr,
        name: s?.name ?? "",
        broadcast: w.broadcast,
      });
    }
    return Array.from(seen.values()).filter((x) => !!x.broadcast);
  }, [subnetsQ.data, preview, mode, subnetIds]);

  const showRouterHelp = vantage.kind === "server" && targetSubnets.length > 0;

  return (
    <div className="space-y-4">
      {/* Vantage picker — reuses #533's server-vs-appliance concept */}
      <div>
        <label className={labelCls}>Send from (vantage)</label>
        <select
          className={inputCls}
          value={vantageSelection}
          onChange={(e) => {
            const v = e.target.value;
            setVantage(
              v === "server"
                ? { kind: "server", id: null }
                : { kind: "appliance", id: v },
            );
          }}
        >
          <option value="server">Server (control plane broadcast)</option>
          {onlineAppliances.map((a) => (
            <option key={a.id} value={a.id}>
              Appliance · {a.hostname}
            </option>
          ))}
        </select>
        <p className="mt-1 text-[11px] text-muted-foreground">
          {onlineAppliances.length === 0
            ? "No Fleet appliances online — the packet broadcasts from the control-plane server (only reaches segments it can reach directly)."
            : "A magic packet only reaches the L2 segment it is sent on. Pick a Fleet appliance on the target LAN for reliable delivery across an L3 boundary."}
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label className={labelCls}>Repeat count</label>
          <input
            type="number"
            min={1}
            max={10}
            className={cn(
              inputCls,
              errors.repeatCount && "border-destructive focus:ring-destructive",
            )}
            value={repeatCount}
            onChange={(e) => setRepeatCount(Number(e.target.value))}
          />
          {errors.repeatCount ? (
            <p className="mt-1 text-[11px] text-destructive">
              {errors.repeatCount}
            </p>
          ) : (
            <p className="mt-1 text-[11px] text-muted-foreground">
              UDP is fire-and-forget; 2–3 improves odds.
            </p>
          )}
        </div>
        <div>
          <label className={labelCls}>Repeat interval (ms)</label>
          <input
            type="number"
            min={0}
            max={10000}
            className={cn(
              inputCls,
              errors.repeatIntervalMs &&
                "border-destructive focus:ring-destructive",
            )}
            value={repeatIntervalMs}
            onChange={(e) => setRepeatIntervalMs(Number(e.target.value))}
          />
          {errors.repeatIntervalMs && (
            <p className="mt-1 text-[11px] text-destructive">
              {errors.repeatIntervalMs}
            </p>
          )}
        </div>
        <div>
          <label className={labelCls}>Stagger between hosts (ms)</label>
          <input
            type="number"
            min={0}
            max={60000}
            className={cn(
              inputCls,
              errors.staggerMs && "border-destructive focus:ring-destructive",
            )}
            value={staggerMs}
            onChange={(e) => setStaggerMs(Number(e.target.value))}
          />
          {errors.staggerMs && (
            <p className="mt-1 text-[11px] text-destructive">
              {errors.staggerMs}
            </p>
          )}
        </div>
        <div>
          <label className={labelCls}>WoL port</label>
          <input
            type="number"
            min={1}
            max={65535}
            className={cn(
              inputCls,
              errors.port && "border-destructive focus:ring-destructive",
            )}
            value={port}
            onChange={(e) => setPort(Number(e.target.value))}
          />
          {errors.port ? (
            <p className="mt-1 text-[11px] text-destructive">{errors.port}</p>
          ) : (
            <p className="mt-1 text-[11px] text-muted-foreground">
              Usually 9 (some stacks use 7).
            </p>
          )}
        </div>
      </div>

      <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-700 dark:text-amber-300">
        <strong>Stagger large fleets.</strong> Waking hundreds of PCs in the
        same second is a power-inrush event <em>and</em> a DHCP / PXE thundering
        herd — especially with FOG re-imaging where every host PXE-boots at
        once.{" "}
        {preview && preview.wake_count > 50 && (
          <span>
            This target resolves to {preview.wake_count} hosts — a {staggerMs}ms
            stagger spreads the wake over ~
            {Math.round((preview.wake_count * staggerMs) / 1000)}s.
          </span>
        )}
      </div>

      {showRouterHelp && (
        <RouterSetupHelp
          senderIp={senderIp}
          setSenderIp={setSenderIp}
          port={port}
          subnets={targetSubnets}
        />
      )}
    </div>
  );
}

// ── Router setup help (server-vantage + remote/L3 target only) ─────────
// Directed-broadcast forwarding is disabled by default on virtually all
// modern gear (smurf-amplification vector), so a server-vantage wake
// across an L3 boundary silently fails until the operator scopes it open
// to just our sender. The appliance vantage needs NONE of this.
function RouterSetupHelp({
  senderIp,
  setSenderIp,
  port,
  subnets,
}: {
  senderIp: string;
  setSenderIp: (s: string) => void;
  port: number;
  subnets: { cidr: string; name: string; broadcast: string }[];
}) {
  const [open, setOpen] = useState(false);
  const [subnetIdx, setSubnetIdx] = useState(0);
  const sub = subnets[Math.min(subnetIdx, subnets.length - 1)] ?? subnets[0];
  const parts = ipv4Parts(sub.cidr);

  const sender = senderIp.trim() || "<sender-ip>";
  const cidr = sub.cidr || "<target-cidr>";
  const netwild =
    parts && sub.cidr
      ? `${parts.network} ${parts.wildcard}`
      : "<network> <wildcard>";
  const network = parts?.network ?? "<network>";
  const wildcard = parts?.wildcard ?? "<wildcard>";
  const bcast = sub.broadcast || "<target-directed-broadcast>";
  const p = String(port || 9);

  const snippets: { vendor: string; code: string; note?: string }[] = [
    {
      vendor: "Cisco IOS / IOS-XE",
      code: `access-list 110 permit udp host ${sender} ${netwild} eq ${p}
!
interface Vlan10
 ip directed-broadcast 110`,
      note: `The ACL scopes directed-broadcast forwarding to only ${sender} → ${cidr}, so it is not left open as a smurf reflector.`,
    },
    {
      vendor: "Juniper Junos",
      code: `firewall {
    family inet {
        filter WOL-ONLY {
            term permit-wol {
                from {
                    source-address {
                        ${sender}/32;
                    }
                    destination-address {
                        ${bcast}/32;
                    }
                    protocol udp;
                    destination-port [ 7 ${p} ];
                }
                then accept;
            }
            term default {
                then accept;   # or your normal policy
            }
        }
    }
}`,
      note: "Junos also needs the receiving IRB / interface to allow the directed broadcast — apply this filter alongside that.",
    },
    {
      vendor: "Arista EOS",
      code: `ip access-list WOL-DIRECTED-BCAST
   permit udp host ${sender} ${netwild} eq ${p}
!
interface Vlan10
   ip directed-broadcast WOL-DIRECTED-BCAST`,
      note: "IOS-like: the ACL pins the single sender so the interface only forwards our directed broadcast.",
    },
    {
      vendor: "MikroTik RouterOS 7",
      code: `/ip firewall filter
add chain=forward action=accept protocol=udp \\
    src-address=${sender} dst-address=${bcast} \\
    dst-port=${p} comment="WOL directed broadcast"`,
      note: "Scope the rule to the single sender + the subnet's directed-broadcast address; never src-address=0.0.0.0/0.",
    },
    {
      vendor: "VyOS / EdgeOS",
      code: `set firewall name WOL-ONLY rule 10 action accept
set firewall name WOL-ONLY rule 10 protocol udp
set firewall name WOL-ONLY rule 10 source address ${sender}
set firewall name WOL-ONLY rule 10 destination address ${bcast}
set firewall name WOL-ONLY rule 10 destination port ${p}
set interfaces ethernet eth0 ip enable-directed-broadcast`,
      note: "EdgeOS / VyOS forward subnet-directed broadcasts only when enable-directed-broadcast is set on the egress interface — keep the firewall rule pinned to our sender.",
    },
    {
      vendor: "pfSense / OPNsense",
      code: `# Firewall → Rules → (transit interface)
#   Action:      Pass
#   Protocol:    UDP
#   Source:      ${sender}
#   Destination: ${bcast}/32
#   Dest port:   ${p}`,
      note: "FreeBSD/pf does not forward subnet-directed broadcasts by default. Prefer a Fleet appliance on the target segment; the pass rule only helps where directed-broadcast forwarding is otherwise enabled.",
    },
  ];

  return (
    <div className="rounded-md border">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm font-medium"
      >
        {open ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronRight className="h-4 w-4" />
        )}
        Router setup help — server vantage across an L3 boundary
      </button>

      {open && (
        <div className="space-y-3 border-t px-3 py-3">
          <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-[11px] text-emerald-700 dark:text-emerald-300">
            <strong>The appliance vantage needs none of this.</strong> Enabling
            directed broadcast is a security downgrade — steer to a Fleet
            appliance on the target segment first, where the packet is broadcast
            locally and no router change is required. Only use the snippets
            below when no on-segment appliance is available. When you do, always
            pin the ACL / filter to our single sender host — never{" "}
            <code>any</code> — so the router is not left as a smurf reflector.
          </div>

          <div className="grid gap-2 sm:grid-cols-2">
            <div>
              <label className={labelCls}>Sender IP (this vantage)</label>
              <input
                className={cn(inputCls, "font-mono")}
                value={senderIp}
                onChange={(e) => setSenderIp(e.target.value)}
                placeholder="10.0.0.5"
              />
            </div>
            {subnets.length > 1 && (
              <div>
                <label className={labelCls}>Target subnet</label>
                <select
                  className={inputCls}
                  value={subnetIdx}
                  onChange={(e) => setSubnetIdx(Number(e.target.value))}
                >
                  {subnets.map((s, i) => (
                    <option key={s.cidr + i} value={i}>
                      {s.cidr} {s.name ? `(${s.name})` : ""}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>

          <div className="rounded-md border bg-muted/30 px-3 py-2 text-[11px] font-mono">
            sender {sender} · target {cidr} · directed-broadcast {bcast} · port{" "}
            {p}
            {parts && (
              <>
                {" "}
                · network {network} · wildcard {wildcard}
              </>
            )}
          </div>

          {snippets.map((s) => (
            <RouterSnippet key={s.vendor} {...s} />
          ))}
        </div>
      )}
    </div>
  );
}

function RouterSnippet({
  vendor,
  code,
  note,
}: {
  vendor: string;
  code: string;
  note?: string;
}) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <div className="rounded-md border">
      <div className="flex items-center justify-between border-b bg-muted/40 px-3 py-1.5">
        <span className="text-xs font-semibold">{vendor}</span>
        <button
          type="button"
          onClick={copy}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          {copied ? (
            <>
              <ClipboardCheck className="h-3.5 w-3.5" /> Copied
            </>
          ) : (
            <>
              <Clipboard className="h-3.5 w-3.5" /> Copy
            </>
          )}
        </button>
      </div>
      <pre className="overflow-x-auto px-3 py-2 text-[11px] leading-relaxed">
        <code>{code}</code>
      </pre>
      {note && (
        <p className="border-t px-3 py-1.5 text-[11px] text-muted-foreground">
          {note}
        </p>
      )}
    </div>
  );
}
