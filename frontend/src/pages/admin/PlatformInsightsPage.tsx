import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  Box,
  Cpu,
  Database,
  HardDrive,
  Network,
  RefreshCw,
} from "lucide-react";
import {
  postgresApi,
  containersApi,
  type PostgresOverview,
  type PostgresTableSize,
  type PostgresConnection,
  type PostgresSlowQuery,
  type ContainerStat,
} from "@/lib/api";
import { cn, zebraBodyCls } from "@/lib/utils";

type TabKey = "postgres" | "containers";

function formatBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`;
}

function formatNumber(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString();
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  if (seconds < 3600)
    return `${Math.floor(seconds / 60)} m ${Math.round(seconds % 60)} s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h} h ${m} m`;
}

function StatCard({
  label,
  value,
  sub,
  icon: Icon,
  tone = "default",
}: {
  label: string;
  value: string;
  sub?: string;
  icon: React.ElementType;
  tone?: "default" | "warn" | "bad";
}) {
  const toneCls =
    tone === "bad"
      ? "border-destructive/40 bg-destructive/5"
      : tone === "warn"
        ? "border-amber-400/40 bg-amber-400/5"
        : "";
  return (
    <div className={cn("rounded-md border bg-card p-4", toneCls)}>
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-muted-foreground">
          {label}
        </span>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </div>
      <div className="mt-2 text-2xl font-semibold">{value}</div>
      {sub && <div className="mt-1 text-xs text-muted-foreground">{sub}</div>}
    </div>
  );
}

function PostgresPanel() {
  const overview = useQuery({
    queryKey: ["pg-overview"],
    queryFn: () => postgresApi.overview(),
    refetchInterval: 30_000,
  });
  const tables = useQuery({
    queryKey: ["pg-tables"],
    queryFn: () => postgresApi.tables(50),
    refetchInterval: 60_000,
  });
  const connections = useQuery({
    queryKey: ["pg-conns"],
    queryFn: () => postgresApi.connections(),
    refetchInterval: 30_000,
  });
  const slow = useQuery({
    queryKey: ["pg-slow"],
    queryFn: () => postgresApi.slowQueries(20),
    refetchInterval: 60_000,
  });

  const ov: PostgresOverview | undefined = overview.data;

  const cachePct =
    ov?.cache_hit_ratio == null ? null : ov.cache_hit_ratio * 100;
  const connTone =
    ov &&
    ov.max_connections > 0 &&
    ov.active_connections / ov.max_connections >= 0.8
      ? "bad"
      : ov &&
          ov.max_connections > 0 &&
          ov.active_connections / ov.max_connections >= 0.5
        ? "warn"
        : "default";
  const cacheTone = cachePct != null && cachePct < 90 ? "warn" : "default";

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="Database size"
          value={formatBytes(ov?.db_size_bytes)}
          sub={ov?.version.split(",")[0]}
          icon={Database}
        />
        <StatCard
          label="Cache hit ratio"
          value={cachePct == null ? "—" : `${cachePct.toFixed(1)}%`}
          sub="heap blocks hit / (hit + read)"
          icon={Activity}
          tone={cacheTone}
        />
        <StatCard
          label="Active connections"
          value={`${ov?.active_connections ?? "—"} / ${ov?.max_connections ?? "—"}`}
          sub="this database"
          icon={Network}
          tone={connTone}
        />
        <StatCard
          label="WAL position"
          value={formatBytes(ov?.wal_bytes)}
          sub={ov?.wal_bytes == null ? "replica or unavailable" : "current LSN"}
          icon={HardDrive}
        />
      </div>

      {ov?.longest_transaction && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/5 p-3">
          <div className="flex items-center gap-2 text-sm font-medium text-amber-800 dark:text-amber-300">
            <AlertTriangle className="h-4 w-4" />
            Longest running transaction
          </div>
          <div className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-4">
            <div>
              <span className="text-muted-foreground">PID</span>
              <div className="font-mono">{ov.longest_transaction.pid}</div>
            </div>
            <div>
              <span className="text-muted-foreground">State</span>
              <div>{ov.longest_transaction.state ?? "—"}</div>
            </div>
            <div>
              <span className="text-muted-foreground">Age</span>
              <div className="font-mono">
                {formatDuration(ov.longest_transaction.age_seconds)}
              </div>
            </div>
            <div>
              <span className="text-muted-foreground">App / client</span>
              <div className="truncate">
                {ov.longest_transaction.application_name ?? "—"}
                {ov.longest_transaction.client_addr
                  ? ` @ ${ov.longest_transaction.client_addr}`
                  : ""}
              </div>
            </div>
          </div>
          {ov.longest_transaction.query && (
            <pre className="mt-2 max-h-24 overflow-auto rounded bg-background/50 p-2 text-[11px] font-mono">
              {ov.longest_transaction.query}
            </pre>
          )}
        </div>
      )}

      <section>
        <h3 className="mb-2 text-sm font-semibold">Connections by state</h3>
        <div className="overflow-hidden rounded border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-xs">
              <tr>
                <th className="px-3 py-2 text-left font-medium">State</th>
                <th className="px-3 py-2 text-right font-medium">Count</th>
              </tr>
            </thead>
            <tbody className={zebraBodyCls}>
              {(connections.data ?? []).map((row: PostgresConnection) => (
                <tr key={row.state}>
                  <td className="px-3 py-1.5">
                    <span
                      className={cn(
                        "inline-flex rounded px-1.5 py-0.5 text-[10px] font-medium",
                        row.state === "idle in transaction" ||
                          row.state === "idle in transaction (aborted)"
                          ? "bg-amber-500/20 text-amber-700 dark:text-amber-400"
                          : row.state === "active"
                            ? "bg-emerald-500/20 text-emerald-700 dark:text-emerald-400"
                            : "bg-muted",
                      )}
                    >
                      {row.state}
                    </span>
                  </td>
                  <td className="px-3 py-1.5 text-right font-mono">
                    {row.count}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h3 className="mb-2 text-sm font-semibold">
          Tables by total size{" "}
          <span className="font-normal text-muted-foreground">(top 50)</span>
        </h3>
        <div className="overflow-x-auto rounded border">
          <table className="w-full min-w-[800px] text-sm">
            <thead className="bg-muted/40 text-xs">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Table</th>
                <th className="px-3 py-2 text-right font-medium">Total</th>
                <th className="px-3 py-2 text-right font-medium">Heap</th>
                <th className="px-3 py-2 text-right font-medium">Indexes</th>
                <th className="px-3 py-2 text-right font-medium">TOAST</th>
                <th className="px-3 py-2 text-right font-medium">Live rows</th>
                <th className="px-3 py-2 text-right font-medium">Dead rows</th>
                <th className="px-3 py-2 text-left font-medium">Last vacuum</th>
              </tr>
            </thead>
            <tbody className={zebraBodyCls}>
              {(tables.data ?? []).map((t: PostgresTableSize) => {
                const deadRatio =
                  t.live_rows > 0
                    ? t.dead_rows / (t.live_rows + t.dead_rows)
                    : 0;
                return (
                  <tr key={`${t.schema_name}.${t.table_name}`}>
                    <td className="px-3 py-1.5 font-mono text-xs">
                      {t.schema_name === "public"
                        ? t.table_name
                        : `${t.schema_name}.${t.table_name}`}
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono">
                      {formatBytes(t.total_bytes)}
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono text-muted-foreground">
                      {formatBytes(t.table_bytes)}
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono text-muted-foreground">
                      {formatBytes(t.index_bytes)}
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono text-muted-foreground">
                      {formatBytes(t.toast_bytes)}
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono">
                      {formatNumber(t.live_rows)}
                    </td>
                    <td
                      className={cn(
                        "px-3 py-1.5 text-right font-mono",
                        deadRatio > 0.2 && "text-amber-600 dark:text-amber-400",
                      )}
                    >
                      {formatNumber(t.dead_rows)}
                    </td>
                    <td className="px-3 py-1.5 text-xs text-muted-foreground">
                      {t.last_autovacuum
                        ? new Date(t.last_autovacuum).toLocaleString()
                        : "never"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h3 className="mb-2 text-sm font-semibold">
          Slow queries{" "}
          <span className="font-normal text-muted-foreground">
            (top 20 by total time)
          </span>
        </h3>
        {slow.data && !slow.data.available && (
          <div className="rounded border border-amber-500/40 bg-amber-500/5 p-3 text-xs">
            <div className="font-medium text-amber-800 dark:text-amber-300">
              pg_stat_statements not available
            </div>
            <p className="mt-1 text-muted-foreground">{slow.data.hint}</p>
          </div>
        )}
        {slow.data?.available && (
          <div className="overflow-x-auto rounded border">
            <table className="w-full min-w-[800px] text-sm">
              <thead className="bg-muted/40 text-xs">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Query</th>
                  <th className="px-3 py-2 text-right font-medium">Calls</th>
                  <th className="px-3 py-2 text-right font-medium">Mean</th>
                  <th className="px-3 py-2 text-right font-medium">Total</th>
                  <th className="px-3 py-2 text-right font-medium">Rows</th>
                </tr>
              </thead>
              <tbody className={zebraBodyCls}>
                {slow.data.rows.map((r: PostgresSlowQuery, i: number) => (
                  <tr key={i}>
                    <td className="px-3 py-1.5">
                      <pre className="max-w-2xl truncate font-mono text-xs">
                        {r.query}
                      </pre>
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono">
                      {formatNumber(r.calls)}
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono">
                      {r.mean_time_ms.toFixed(1)} ms
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono">
                      {(r.total_time_ms / 1000).toFixed(1)} s
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono text-muted-foreground">
                      {formatNumber(r.rows)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function ContainersPanel() {
  const [prefix, setPrefix] = useState("spatiumddi-");
  const [includeStopped, setIncludeStopped] = useState(false);
  const stats = useQuery({
    queryKey: ["container-stats", prefix, includeStopped],
    queryFn: () =>
      containersApi.stats({ prefix, include_stopped: includeStopped }),
    refetchInterval: 5_000,
  });

  if (stats.data && !stats.data.available) {
    return (
      <div className="rounded border border-amber-500/40 bg-amber-500/5 p-4">
        <div className="flex items-center gap-2 text-sm font-medium text-amber-800 dark:text-amber-300">
          <AlertTriangle className="h-4 w-4" />
          Docker socket not mounted
        </div>
        <p className="mt-2 text-xs text-muted-foreground">{stats.data.hint}</p>
      </div>
    );
  }

  const rows = stats.data?.rows ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <label className="text-xs text-muted-foreground">Name filter</label>
          <input
            value={prefix}
            onChange={(e) => setPrefix(e.target.value)}
            placeholder="(empty for all)"
            className="rounded-md border bg-background px-2 py-1 text-sm"
          />
        </div>
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={includeStopped}
            onChange={(e) => setIncludeStopped(e.target.checked)}
            className="rounded"
          />
          Include stopped
        </label>
        <span className="text-xs text-muted-foreground">
          Auto-refresh every 5 s
        </span>
      </div>
      <div className="overflow-x-auto rounded border">
        <table className="w-full min-w-[900px] text-sm">
          <thead className="bg-muted/40 text-xs">
            <tr>
              <th className="px-3 py-2 text-left font-medium">Container</th>
              <th className="px-3 py-2 text-left font-medium">State</th>
              <th className="px-3 py-2 text-right font-medium">CPU%</th>
              <th className="px-3 py-2 text-right font-medium">Memory</th>
              <th className="px-3 py-2 text-right font-medium">Mem%</th>
              <th className="px-3 py-2 text-right font-medium">Net RX</th>
              <th className="px-3 py-2 text-right font-medium">Net TX</th>
              <th className="px-3 py-2 text-right font-medium">Disk R</th>
              <th className="px-3 py-2 text-right font-medium">Disk W</th>
            </tr>
          </thead>
          <tbody className={zebraBodyCls}>
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={9}
                  className="px-3 py-4 text-center text-xs text-muted-foreground"
                >
                  No containers match.
                </td>
              </tr>
            )}
            {rows.map((c: ContainerStat) => {
              const cpuTone =
                c.cpu_percent != null && c.cpu_percent > 80
                  ? "text-red-600 dark:text-red-400"
                  : c.cpu_percent != null && c.cpu_percent > 50
                    ? "text-amber-600 dark:text-amber-400"
                    : "";
              const memTone =
                c.memory_percent != null && c.memory_percent > 90
                  ? "text-red-600 dark:text-red-400"
                  : c.memory_percent != null && c.memory_percent > 75
                    ? "text-amber-600 dark:text-amber-400"
                    : "";
              return (
                <tr key={c.id}>
                  <td className="px-3 py-1.5">
                    <div className="font-medium">{c.name}</div>
                    <div className="text-[11px] text-muted-foreground">
                      {c.image}
                    </div>
                  </td>
                  <td className="px-3 py-1.5">
                    <span
                      className={cn(
                        "inline-flex rounded px-1.5 py-0.5 text-[10px] font-medium",
                        c.state === "running"
                          ? "bg-emerald-500/20 text-emerald-700 dark:text-emerald-400"
                          : "bg-muted text-muted-foreground",
                      )}
                    >
                      {c.state}
                    </span>
                  </td>
                  <td
                    className={cn("px-3 py-1.5 text-right font-mono", cpuTone)}
                  >
                    {c.cpu_percent == null
                      ? "—"
                      : `${c.cpu_percent.toFixed(1)}%`}
                  </td>
                  <td className="px-3 py-1.5 text-right font-mono text-xs">
                    {formatBytes(c.memory_bytes)}
                    {c.memory_limit_bytes ? (
                      <span className="text-muted-foreground">
                        {" "}
                        / {formatBytes(c.memory_limit_bytes)}
                      </span>
                    ) : null}
                  </td>
                  <td
                    className={cn("px-3 py-1.5 text-right font-mono", memTone)}
                  >
                    {c.memory_percent == null
                      ? "—"
                      : `${c.memory_percent.toFixed(1)}%`}
                  </td>
                  <td className="px-3 py-1.5 text-right font-mono text-xs">
                    {formatBytes(c.network_rx_bytes)}
                  </td>
                  <td className="px-3 py-1.5 text-right font-mono text-xs">
                    {formatBytes(c.network_tx_bytes)}
                  </td>
                  <td className="px-3 py-1.5 text-right font-mono text-xs">
                    {formatBytes(c.block_read_bytes)}
                  </td>
                  <td className="px-3 py-1.5 text-right font-mono text-xs">
                    {formatBytes(c.block_write_bytes)}
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

export function PlatformInsightsPage() {
  const [tab, setTab] = useState<TabKey>("postgres");

  return (
    <div className="h-full overflow-auto p-6">
      <div className="mx-auto max-w-7xl space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-semibold">
              <Cpu className="h-5 w-5" />
              Platform Insights
            </h1>
            <p className="text-sm text-muted-foreground">
              Read-only diagnostics for the SpatiumDDI control plane.
            </p>
          </div>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted"
            title="Force a fresh fetch"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Reload
          </button>
        </div>

        <div className="border-b">
          <div className="flex gap-1">
            <button
              onClick={() => setTab("postgres")}
              className={cn(
                "border-b-2 px-3 py-2 text-sm font-medium -mb-px transition-colors",
                tab === "postgres"
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              <Database className="mr-1 inline h-3.5 w-3.5" />
              Postgres
            </button>
            <button
              onClick={() => setTab("containers")}
              className={cn(
                "border-b-2 px-3 py-2 text-sm font-medium -mb-px transition-colors",
                tab === "containers"
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              <Box className="mr-1 inline h-3.5 w-3.5" />
              Containers
            </button>
          </div>
        </div>

        {tab === "postgres" && <PostgresPanel />}
        {tab === "containers" && <ContainersPanel />}
      </div>
    </div>
  );
}
