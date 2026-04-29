import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  Clock,
  Cpu,
  History,
  Loader2,
  RefreshCw,
  Server,
  Workflow,
} from "lucide-react";
import { Modal } from "@/components/ui/modal";
import {
  dnsApi,
  type DNSServer,
  type DNSPendingOpEntry,
  type DNSPerServerZoneStateEntry,
  type DNSServerEventEntry,
} from "@/lib/api";

/**
 * Tabbed read-only inspector for a single DNS server. Mounted from
 * the ServersTab when an operator clicks a server row. Surfaces
 * everything we know without needing the operator to SSH in:
 *
 * - **Overview** — agent status, last heartbeat, ETag drift, JWT, version
 * - **Zones** — per-zone serial drift (target vs. what this server reports)
 * - **Sync** — pending / in-flight / failed `DNSRecordOp` rows
 * - **Events** — recent audit-log rows scoped to this server
 *
 * Live BIND9 statistics-channels output + rendered named.conf are tracked
 * in the roadmap as a follow-up — they need new agent admin endpoints
 * (`/api/v1/dns/agents/admin/*`).
 */
type Tab = "overview" | "zones" | "sync" | "events";

export function ServerDetailModal({
  server,
  onClose,
}: {
  server: DNSServer;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>("overview");

  return (
    <Modal title={server.name} onClose={onClose} wide>
      <div className="flex flex-col gap-3">
        <div className="-mt-2 mb-1 flex items-center gap-2 text-xs text-muted-foreground">
          <Cpu className="h-3.5 w-3.5" />
          <span className="rounded border px-1.5 py-0.5 text-[10px]">
            {server.driver}
          </span>
          <span className="rounded border px-1.5 py-0.5 text-[10px] font-mono">
            {server.host}:{server.port}
          </span>
          {server.is_primary && (
            <span className="rounded bg-blue-500/15 px-1.5 py-0.5 text-[10px] font-medium text-blue-600">
              primary
            </span>
          )}
        </div>
        <div className="flex gap-1 border-b">
          <TabButton
            active={tab === "overview"}
            onClick={() => setTab("overview")}
            icon={<Server className="h-3.5 w-3.5" />}
            label="Overview"
          />
          <TabButton
            active={tab === "zones"}
            onClick={() => setTab("zones")}
            icon={<Workflow className="h-3.5 w-3.5" />}
            label="Zones"
          />
          <TabButton
            active={tab === "sync"}
            onClick={() => setTab("sync")}
            icon={<RefreshCw className="h-3.5 w-3.5" />}
            label="Sync"
          />
          <TabButton
            active={tab === "events"}
            onClick={() => setTab("events")}
            icon={<History className="h-3.5 w-3.5" />}
            label="Events"
          />
        </div>

        <div className="min-h-[24rem]">
          {tab === "overview" && <OverviewTab server={server} />}
          {tab === "zones" && <ZonesTab serverId={server.id} />}
          {tab === "sync" && <SyncTab serverId={server.id} />}
          {tab === "events" && <EventsTab serverId={server.id} />}
        </div>
      </div>
    </Modal>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "flex items-center gap-1.5 border-b-2 px-3 py-2 text-xs font-medium transition-colors " +
        (active
          ? "border-primary text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground")
      }
    >
      {icon}
      {label}
    </button>
  );
}

// ── Overview tab ──────────────────────────────────────────────────────────

function OverviewTab({ server }: { server: DNSServer }) {
  return (
    <div className="grid grid-cols-2 gap-3">
      <InfoCard label="Status" value={server.status}>
        <StatusDot status={server.status} />
        <span className="ml-2 text-sm font-medium capitalize">
          {server.status}
        </span>
      </InfoCard>
      <InfoCard
        label="Roles"
        value={server.roles.length === 0 ? "—" : server.roles.join(", ")}
      />
      <InfoCard label="Agent ID" value={server.agent_id ?? "—"} mono />
      <InfoCard
        label="Pending approval"
        value={server.pending_approval ? "yes" : "no"}
        accent={server.pending_approval ? "warning" : undefined}
      />
      <InfoCard
        label="Last heartbeat"
        value={fmtRelative(server.last_seen_at)}
      />
      <InfoCard
        label="Last health check"
        value={fmtRelative(server.last_health_check_at)}
      />
      <InfoCard label="Last sync" value={fmtRelative(server.last_sync_at)} />
      <InfoCard
        label="Config ETag (last acked)"
        value={server.last_config_etag ?? "—"}
        mono
      />
      <InfoCard
        label="Enabled"
        value={server.is_enabled ? "yes" : "no (paused)"}
        accent={server.is_enabled ? undefined : "warning"}
      />
      <InfoCard label="Primary" value={server.is_primary ? "yes" : "no"} />
      {server.notes && (
        <div className="col-span-2 rounded-md border bg-muted/30 p-3 text-xs">
          <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Notes
          </div>
          <div className="whitespace-pre-wrap">{server.notes}</div>
        </div>
      )}
    </div>
  );
}

function InfoCard({
  label,
  value,
  mono,
  accent,
  children,
}: {
  label: string;
  value: string;
  mono?: boolean;
  accent?: "warning" | "good" | "bad";
  children?: React.ReactNode;
}) {
  const accentCls =
    accent === "warning"
      ? "text-amber-600 dark:text-amber-400"
      : accent === "good"
        ? "text-emerald-600 dark:text-emerald-400"
        : accent === "bad"
          ? "text-destructive"
          : "";
  return (
    <div className="rounded-md border bg-card p-3">
      <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      {children ?? (
        <div
          className={
            "truncate text-sm " + (mono ? "font-mono text-xs " : "") + accentCls
          }
          title={value}
        >
          {value}
        </div>
      )}
    </div>
  );
}

function StatusDot({ status }: { status: string }) {
  const cls =
    {
      active: "bg-emerald-500",
      unreachable: "bg-red-500",
      syncing: "bg-blue-500",
      error: "bg-red-500",
      disabled: "bg-muted-foreground/40",
    }[status] ?? "bg-muted";
  return (
    <span
      className={`inline-block h-2.5 w-2.5 rounded-full ${cls}`}
      title={status}
    />
  );
}

// ── Zones tab ─────────────────────────────────────────────────────────────

function ZonesTab({ serverId }: { serverId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["dns-server-zone-state", serverId],
    queryFn: () => dnsApi.getServerZoneState(serverId),
    refetchInterval: 30_000,
  });

  if (isLoading) return <LoadingBlock />;
  if (isError || !data) return <ErrorBlock />;

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-4 gap-2 text-xs">
        <Stat label="Zones" value={data.summary.total} />
        <Stat
          label="In sync"
          value={data.summary.in_sync}
          accent={data.summary.in_sync > 0 ? "good" : undefined}
        />
        <Stat
          label="Drift"
          value={data.summary.drift}
          accent={data.summary.drift > 0 ? "bad" : undefined}
        />
        <Stat
          label="Not reported"
          value={data.summary.not_reported}
          accent={data.summary.not_reported > 0 ? "warning" : undefined}
        />
      </div>
      {data.zones.length === 0 ? (
        <p className="rounded-md border bg-card p-4 text-center text-sm text-muted-foreground">
          No zones in this server's group.
        </p>
      ) : (
        <div className="overflow-hidden rounded-md border">
          <table className="w-full text-sm">
            <thead className="border-b bg-muted/30 text-xs">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Zone</th>
                <th className="px-3 py-2 text-left font-medium">Type</th>
                <th className="px-3 py-2 text-right font-medium">Target</th>
                <th className="px-3 py-2 text-right font-medium">Current</th>
                <th className="px-3 py-2 text-right font-medium">Reported</th>
                <th className="px-3 py-2 text-center font-medium">Sync</th>
              </tr>
            </thead>
            <tbody>
              {data.zones.map((z) => (
                <ZoneRow key={z.zone_id} z={z} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ZoneRow({ z }: { z: DNSPerServerZoneStateEntry }) {
  const indicator =
    z.current_serial === null ? (
      <Clock className="h-3.5 w-3.5 text-amber-500" />
    ) : z.in_sync ? (
      <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
    ) : (
      <AlertCircle className="h-3.5 w-3.5 text-red-500" />
    );
  return (
    <tr className="border-b last:border-0">
      <td className="px-3 py-1.5 font-medium">{z.zone_name}</td>
      <td className="px-3 py-1.5 text-muted-foreground">{z.zone_type}</td>
      <td className="px-3 py-1.5 text-right font-mono text-xs">
        {z.target_serial}
      </td>
      <td className="px-3 py-1.5 text-right font-mono text-xs">
        {z.current_serial ?? "—"}
      </td>
      <td className="px-3 py-1.5 text-right text-xs text-muted-foreground">
        {fmtRelative(z.reported_at)}
      </td>
      <td className="px-3 py-1.5">
        <div className="flex items-center justify-center">{indicator}</div>
      </td>
    </tr>
  );
}

// ── Sync tab ──────────────────────────────────────────────────────────────

function SyncTab({ serverId }: { serverId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["dns-server-pending-ops", serverId],
    queryFn: () => dnsApi.getServerPendingOps(serverId),
    refetchInterval: 15_000,
  });

  if (isLoading) return <LoadingBlock />;
  if (isError || !data) return <ErrorBlock />;

  const states: Array<[string, "warning" | "good" | "bad" | undefined]> = [
    ["pending", "warning"],
    ["in_flight", "warning"],
    ["applied", "good"],
    ["failed", "bad"],
  ];

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-4 gap-2 text-xs">
        {states.map(([state, accent]) => (
          <Stat
            key={state}
            label={state.replace("_", " ")}
            value={data.counts[state] ?? 0}
            accent={(data.counts[state] ?? 0) > 0 ? accent : undefined}
          />
        ))}
      </div>
      {data.items.length === 0 ? (
        <p className="rounded-md border bg-card p-4 text-center text-sm text-muted-foreground">
          No record ops queued for this server.
        </p>
      ) : (
        <div className="overflow-hidden rounded-md border">
          <table className="w-full text-sm">
            <thead className="border-b bg-muted/30 text-xs">
              <tr>
                <th className="px-3 py-2 text-left font-medium">When</th>
                <th className="px-3 py-2 text-left font-medium">Zone</th>
                <th className="px-3 py-2 text-left font-medium">Op</th>
                <th className="px-3 py-2 text-left font-medium">Record</th>
                <th className="px-3 py-2 text-left font-medium">State</th>
                <th className="px-3 py-2 text-right font-medium">Tries</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((op) => (
                <OpRow key={op.op_id} op={op} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function OpRow({ op }: { op: DNSPendingOpEntry }) {
  const stateCls: Record<string, string> = {
    pending: "bg-amber-500/15 text-amber-600",
    in_flight: "bg-blue-500/15 text-blue-600",
    applied: "bg-emerald-500/15 text-emerald-600",
    failed: "bg-red-500/15 text-red-600",
  };
  const r = op.record as { name?: string; type?: string; value?: string };
  const recordSummary = r.name
    ? `${r.name} ${r.type ?? ""} ${r.value ?? ""}`.trim()
    : JSON.stringify(op.record);
  return (
    <tr className="border-b last:border-0" title={op.last_error ?? undefined}>
      <td className="px-3 py-1.5 text-xs text-muted-foreground">
        {fmtRelative(op.created_at)}
      </td>
      <td className="px-3 py-1.5 font-medium">{op.zone_name}</td>
      <td className="px-3 py-1.5 text-xs uppercase">{op.op}</td>
      <td className="px-3 py-1.5 truncate font-mono text-xs">
        {recordSummary}
      </td>
      <td className="px-3 py-1.5">
        <span
          className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${
            stateCls[op.state] ?? "bg-muted text-muted-foreground"
          }`}
        >
          {op.state.replace("_", " ")}
        </span>
      </td>
      <td className="px-3 py-1.5 text-right text-xs tabular-nums">
        {op.attempts}
      </td>
    </tr>
  );
}

// ── Events tab ────────────────────────────────────────────────────────────

function EventsTab({ serverId }: { serverId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["dns-server-recent-events", serverId],
    queryFn: () => dnsApi.getServerRecentEvents(serverId),
    refetchInterval: 60_000,
  });

  if (isLoading) return <LoadingBlock />;
  if (isError || !data) return <ErrorBlock />;

  if (data.items.length === 0) {
    return (
      <p className="rounded-md border bg-card p-4 text-center text-sm text-muted-foreground">
        No audit events recorded for this server.
      </p>
    );
  }

  return (
    <div className="overflow-hidden rounded-md border">
      <table className="w-full text-sm">
        <thead className="border-b bg-muted/30 text-xs">
          <tr>
            <th className="px-3 py-2 text-left font-medium">When</th>
            <th className="px-3 py-2 text-left font-medium">Action</th>
            <th className="px-3 py-2 text-left font-medium">User</th>
            <th className="px-3 py-2 text-left font-medium">Detail</th>
            <th className="px-3 py-2 text-left font-medium">Result</th>
          </tr>
        </thead>
        <tbody>
          {data.items.map((e) => (
            <EventRow key={e.id} event={e} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EventRow({ event }: { event: DNSServerEventEntry }) {
  const resultCls =
    event.result === "success"
      ? "bg-emerald-500/15 text-emerald-600"
      : "bg-red-500/15 text-red-600";
  return (
    <tr className="border-b last:border-0">
      <td
        className="px-3 py-1.5 text-xs text-muted-foreground"
        title={new Date(event.timestamp).toLocaleString()}
      >
        {fmtRelative(event.timestamp)}
      </td>
      <td className="px-3 py-1.5 text-xs uppercase">{event.action}</td>
      <td className="px-3 py-1.5 text-xs">{event.user_display_name}</td>
      <td className="px-3 py-1.5 truncate text-xs text-muted-foreground">
        {event.resource_display}
      </td>
      <td className="px-3 py-1.5">
        <span
          className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${resultCls}`}
        >
          {event.result}
        </span>
      </td>
    </tr>
  );
}

// ── Shared bits ───────────────────────────────────────────────────────────

function Stat({
  label,
  value,
  accent,
}: {
  label: string;
  value: number;
  accent?: "good" | "bad" | "warning";
}) {
  const accentCls =
    accent === "good"
      ? "text-emerald-600 dark:text-emerald-400"
      : accent === "bad"
        ? "text-destructive"
        : accent === "warning"
          ? "text-amber-600 dark:text-amber-400"
          : "text-foreground";
  return (
    <div className="rounded-md border bg-card px-2.5 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className={`text-lg font-semibold tabular-nums ${accentCls}`}>
        {value}
      </div>
    </div>
  );
}

function LoadingBlock() {
  return (
    <div className="flex items-center justify-center gap-2 rounded-md border bg-card py-8 text-sm text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin" />
      Loading…
    </div>
  );
}

function ErrorBlock() {
  return (
    <div className="flex items-center justify-center gap-2 rounded-md border border-destructive/40 bg-destructive/5 py-8 text-sm text-destructive">
      <Activity className="h-4 w-4" />
      Failed to load — try again
    </div>
  );
}

function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diff = Date.now() - t;
  const sec = Math.floor(diff / 1000);
  if (sec < 0) return "in the future";
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 48) return `${hr}h ago`;
  const days = Math.floor(hr / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}
