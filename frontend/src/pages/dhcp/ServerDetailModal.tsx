import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Copy,
  Cpu,
  FileText,
  History,
  Loader2,
  RefreshCw,
  ScrollText,
  Server,
} from "lucide-react";
import { Modal } from "@/components/ui/modal";
import {
  dhcpApi,
  logsApi,
  type DHCPActivityLogRow,
  type DHCPPendingOpEntry,
  type DHCPServer,
  type DHCPServerEventEntry,
} from "@/lib/api";

/**
 * Tabbed read-only inspector for a single DHCP server. Mounted from the
 * GroupServersList when an operator clicks a server row — mirrors the
 * DNS ServerDetailModal pattern (issue #181) so DHCP operators get the
 * same overview / sync / events / logs / config experience without
 * leaving the group view.
 *
 * Tabs:
 *
 * - **Overview** — driver / host:port / agent status / heartbeat / HA
 * - **Sync** — pending / in-flight / applied / failed DHCPConfigOp rows
 * - **Events** — audit-log rows scoped to this server
 * - **Logs** — Kea activity log entries (filtered by severity / IP / MAC)
 * - **Config** — rendered Kea JSON the agent would apply next reload
 *
 * Read-only Windows DHCP servers hide the Logs + Config tabs (the
 * driver doesn't push a Kea log pipeline and has no rendered config).
 */
type Tab = "overview" | "sync" | "events" | "logs" | "config";

const READ_ONLY_DRIVERS = new Set(["windows_dhcp"]);

export function ServerDetailModal({
  server,
  onClose,
}: {
  server: DHCPServer;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>("overview");
  const isReadOnly = READ_ONLY_DRIVERS.has(server.driver);

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
          {server.ha_state && (
            <span className="rounded bg-blue-500/15 px-1.5 py-0.5 text-[10px] font-medium text-blue-600">
              HA: {server.ha_state}
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-1 border-b">
          <TabButton
            active={tab === "overview"}
            onClick={() => setTab("overview")}
            icon={<Server className="h-3.5 w-3.5" />}
            label="Overview"
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
          {!isReadOnly && (
            <>
              <TabButton
                active={tab === "logs"}
                onClick={() => setTab("logs")}
                icon={<ScrollText className="h-3.5 w-3.5" />}
                label="Logs"
              />
              <TabButton
                active={tab === "config"}
                onClick={() => setTab("config")}
                icon={<FileText className="h-3.5 w-3.5" />}
                label="Config"
              />
            </>
          )}
        </div>

        <div className="min-h-[24rem]">
          {tab === "overview" && <OverviewTab server={server} />}
          {tab === "sync" && <SyncTab serverId={server.id} />}
          {tab === "events" && <EventsTab serverId={server.id} />}
          {tab === "logs" && !isReadOnly && <LogsTab serverId={server.id} />}
          {tab === "config" && !isReadOnly && (
            <ConfigTab serverId={server.id} />
          )}
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

function OverviewTab({ server }: { server: DHCPServer }) {
  // Kea agents send heartbeats; their ``agent_last_seen`` is the right
  // liveness signal. Windows DHCP is polled, so ``last_sync_at`` is
  // meaningful. Fall back to whichever exists.
  const seenAt =
    server.driver === "kea"
      ? (server.agent_last_seen ?? server.last_sync_at)
      : (server.last_sync_at ?? server.agent_last_seen);
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3">
        <InfoCard label="Status" value={server.status}>
          <StatusDot status={server.status} />
          <span className="ml-2 text-sm font-medium capitalize">
            {server.status}
          </span>
        </InfoCard>
        <InfoCard label="Last heartbeat" value={fmtRelative(seenAt)} />
        <InfoCard label="Last sync" value={fmtRelative(server.last_sync_at)} />
        <InfoCard
          label="Last seen IP"
          value={server.last_seen_ip ?? "—"}
          mono
        />
        <InfoCard
          label="Agent approved"
          value={
            server.is_agentless
              ? "n/a (agentless)"
              : server.agent_approved
                ? "yes"
                : "no (pending)"
          }
          accent={
            !server.is_agentless && !server.agent_approved
              ? "warning"
              : undefined
          }
        />
        <InfoCard
          label="HA state"
          value={server.ha_state ?? "—"}
          accent={
            server.ha_state === "partner-down" ||
            server.ha_state === "terminated"
              ? "bad"
              : server.ha_state === "normal" ||
                  server.ha_state === "load-balancing" ||
                  server.ha_state === "hot-standby" ||
                  server.ha_state === "ready"
                ? "good"
                : server.ha_state
                  ? "warning"
                  : undefined
          }
        />
        <InfoCard
          label="Config ETag (last acked)"
          value={server.config_etag ?? "—"}
          mono
        />
        <InfoCard
          label="Mode"
          value={
            server.is_read_only
              ? "read-only"
              : server.is_agentless
                ? "agentless"
                : "agent"
          }
        />
      </div>
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
      ok: "bg-emerald-500",
      online: "bg-emerald-500",
      unreachable: "bg-red-500",
      offline: "bg-red-500",
      syncing: "bg-blue-500",
      error: "bg-red-500",
      disabled: "bg-muted-foreground/40",
      unknown: "bg-muted-foreground/40",
    }[status] ?? "bg-muted";
  return (
    <span
      className={`inline-block h-2.5 w-2.5 rounded-full ${cls}`}
      title={status}
    />
  );
}

// ── Sync tab ──────────────────────────────────────────────────────────────

function SyncTab({ serverId }: { serverId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["dhcp-server-pending-ops", serverId],
    queryFn: () => dhcpApi.getServerPendingOps(serverId),
    refetchInterval: 15_000,
  });

  if (isLoading) return <LoadingBlock />;
  if (isError || !data) return <ErrorBlock />;

  // Kea ops use ``status`` for state ("pending" / "in_flight" /
  // "applied" / "failed") — same vocabulary as DNS so the counts grid
  // is symmetric across the two server types.
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
          No config ops queued for this server.
        </p>
      ) : (
        <div className="overflow-hidden rounded-md border">
          <table className="w-full text-sm">
            <thead className="border-b bg-muted/30 text-xs">
              <tr>
                <th className="px-3 py-2 text-left font-medium">When</th>
                <th className="px-3 py-2 text-left font-medium">Op</th>
                <th className="px-3 py-2 text-left font-medium">Status</th>
                <th className="px-3 py-2 text-right font-medium">Tries</th>
                <th className="px-3 py-2 text-left font-medium">Acked</th>
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

function OpRow({ op }: { op: DHCPPendingOpEntry }) {
  const stateCls: Record<string, string> = {
    pending: "bg-amber-500/15 text-amber-600",
    in_flight: "bg-blue-500/15 text-blue-600",
    applied: "bg-emerald-500/15 text-emerald-600",
    failed: "bg-red-500/15 text-red-600",
  };
  return (
    <tr className="border-b last:border-0" title={op.error_msg ?? undefined}>
      <td className="px-3 py-1.5 text-xs text-muted-foreground">
        {fmtRelative(op.created_at)}
      </td>
      <td className="px-3 py-1.5 text-xs font-mono">{op.op_type}</td>
      <td className="px-3 py-1.5">
        <span
          className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${
            stateCls[op.status] ?? "bg-muted text-muted-foreground"
          }`}
        >
          {op.status}
        </span>
      </td>
      <td className="px-3 py-1.5 text-right text-xs tabular-nums">
        {op.attempts}
      </td>
      <td className="px-3 py-1.5 text-xs text-muted-foreground">
        {fmtRelative(op.acked_at)}
      </td>
    </tr>
  );
}

// ── Events tab ────────────────────────────────────────────────────────────

function EventsTab({ serverId }: { serverId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["dhcp-server-recent-events", serverId],
    queryFn: () => dhcpApi.getServerRecentEvents(serverId),
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

function EventRow({ event }: { event: DHCPServerEventEntry }) {
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

// ── Logs tab ──────────────────────────────────────────────────────────────

function LogsTab({ serverId }: { serverId: string }) {
  const [q, setQ] = useState("");
  const [severity, setSeverity] = useState("");
  const [mac, setMac] = useState("");
  const [ip, setIp] = useState("");
  const filterKey = `${q}|${severity}|${mac}|${ip}`;

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["dhcp-server-activity", serverId, filterKey],
    queryFn: () =>
      logsApi.dhcpActivity({
        server_id: serverId,
        q: q || null,
        severity: severity || null,
        mac_address: mac || null,
        ip_address: ip || null,
        max_events: 200,
      }),
    refetchInterval: 30_000,
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 rounded-md border bg-card px-3 py-2">
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filter raw / code…"
          className="flex-1 min-w-[10rem] rounded border bg-background px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <select
          value={severity}
          onChange={(e) => setSeverity(e.target.value)}
          className="rounded border bg-background px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
        >
          <option value="">any sev</option>
          <option value="DEBUG">DEBUG</option>
          <option value="INFO">INFO</option>
          <option value="WARN">WARN</option>
          <option value="ERROR">ERROR</option>
        </select>
        <input
          type="text"
          value={mac}
          onChange={(e) => setMac(e.target.value)}
          placeholder="MAC"
          className="w-32 rounded border bg-background px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <input
          type="text"
          value={ip}
          onChange={(e) => setIp(e.target.value)}
          placeholder="IP"
          className="w-32 rounded border bg-background px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <button
          type="button"
          onClick={() => refetch()}
          className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-accent"
          disabled={isFetching}
        >
          <RefreshCw
            className={`h-3 w-3 ${isFetching ? "animate-spin" : ""}`}
          />
          Refresh
        </button>
      </div>

      {isLoading ? (
        <LoadingBlock />
      ) : isError ? (
        <ErrorBlock />
      ) : !data || data.events.length === 0 ? (
        <p className="rounded-md border bg-card p-4 text-center text-sm text-muted-foreground">
          No activity log entries match — Kea agents ship file-output{" "}
          <code className="font-mono">/var/log/kea/kea-dhcp4.log</code> lines on
          a rolling window.
          {data?.truncated && " (older entries were truncated)"}
        </p>
      ) : (
        <div className="overflow-hidden rounded-md border">
          <table className="w-full text-sm">
            <thead className="border-b bg-muted/30 text-xs">
              <tr>
                <th className="px-3 py-2 text-left font-medium">When</th>
                <th className="px-3 py-2 text-left font-medium">Sev</th>
                <th className="px-3 py-2 text-left font-medium">Code</th>
                <th className="px-3 py-2 text-left font-medium">MAC</th>
                <th className="px-3 py-2 text-left font-medium">IP</th>
                <th className="px-3 py-2 text-left font-medium">Detail</th>
              </tr>
            </thead>
            <tbody>
              {data.events.map((row) => (
                <LogRow key={row.id} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data?.truncated && (
        <p className="text-[10px] text-amber-600">
          Older entries truncated — narrow the filter to find them.
        </p>
      )}
    </div>
  );
}

function LogRow({ row }: { row: DHCPActivityLogRow }) {
  const sev = row.severity ?? "";
  const sevCls: Record<string, string> = {
    DEBUG: "text-muted-foreground",
    INFO: "text-foreground",
    WARN: "text-amber-600",
    ERROR: "text-destructive",
  };
  return (
    <tr className="border-b last:border-0">
      <td
        className="px-3 py-1.5 text-xs text-muted-foreground"
        title={new Date(row.ts).toLocaleString()}
      >
        {fmtRelative(row.ts)}
      </td>
      <td
        className={`px-3 py-1.5 text-[11px] font-medium ${sevCls[sev] ?? ""}`}
      >
        {sev || "—"}
      </td>
      <td className="px-3 py-1.5 font-mono text-[11px]">{row.code ?? "—"}</td>
      <td className="px-3 py-1.5 font-mono text-[11px]">
        {row.mac_address ?? "—"}
      </td>
      <td className="px-3 py-1.5 font-mono text-[11px]">
        {row.ip_address ?? "—"}
      </td>
      <td className="px-3 py-1.5 truncate text-[11px] text-muted-foreground">
        {row.raw}
      </td>
    </tr>
  );
}

// ── Config tab ────────────────────────────────────────────────────────────

function ConfigTab({ serverId }: { serverId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["dhcp-server-rendered-config", serverId],
    queryFn: () => dhcpApi.getServerRenderedConfig(serverId),
    refetchInterval: 60_000,
  });
  const [copied, setCopied] = useState(false);

  if (isLoading) return <LoadingBlock />;
  if (isError || !data) return <ErrorBlock />;

  if (!data.config) {
    return (
      <p className="rounded-md border bg-card p-4 text-center text-sm text-muted-foreground">
        This driver doesn't render a config we can preview.
      </p>
    );
  }

  // Try to JSON-pretty the body. Kea drivers return JSON text already
  // but we re-format defensively so a future driver that emits a tighter
  // form still renders nicely.
  let pretty = data.config;
  try {
    pretty = JSON.stringify(JSON.parse(data.config), null, 2);
  } catch {
    // Leave raw if the driver returned something other than JSON.
  }

  return (
    <div className="rounded-md border bg-card">
      <div className="flex items-center justify-between border-b px-3 py-2">
        <div className="flex flex-col">
          <span className="font-mono text-xs">
            {data.driver} config (live preview)
          </span>
          <span className="text-[10px] text-muted-foreground">
            Generated {fmtRelative(data.rendered_at)} · etag{" "}
            <code className="font-mono">
              {data.etag ? data.etag.slice(0, 16) + "…" : "—"}
            </code>
          </span>
        </div>
        <button
          type="button"
          onClick={() => {
            navigator.clipboard.writeText(pretty).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            });
          }}
          className="inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] hover:bg-accent"
        >
          <Copy className="h-3 w-3" />
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-[11px] leading-snug">
        {pretty}
      </pre>
    </div>
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
