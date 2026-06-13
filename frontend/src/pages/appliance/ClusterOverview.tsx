import { useEffect, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Boxes,
  CheckCircle2,
  Cpu,
  Database,
  HardDrive,
  Layers,
  MemoryStick,
  Radio,
  RotateCcw,
  Server,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  applianceClusterApi,
  streamClusterHealth,
  type ClusterHealthSnapshot,
  type ClusterNodeVitals,
  type ClusterPodSummary,
  type ClusterWorkloadHealth,
} from "@/lib/api";

/**
 * Cluster → Overview (#402) — a live, near-real-time dashboard for the k3s
 * cluster underneath the appliance.
 *
 * Fed by an SSE stream (`/appliance/cluster/health/stream`) that pushes a
 * fresh snapshot every ~2s; each frame animates the gradient CPU/memory hero
 * chart, the per-node radial gauges, the KPI sparklines, and the top-pods
 * leaderboard. There's no metrics-server / Prometheus on the appliance — live
 * usage comes from the kubelet Summary API, the same source the TTY console
 * uses. The stream self-reconnects; a one-shot GET paints instantly on mount.
 */

const MAX_POINTS = 90; // ~3 min of history at the 2s stream cadence

const EMERALD = "#10b981";
const SKY = "#0ea5e9";
const AMBER = "#f59e0b";
const ROSE = "#f43f5e";
const VIOLET = "#8b5cf6";
const SLATE = "#64748b";

interface HistPoint {
  i: number;
  cpu: number | null;
  mem: number | null;
}

// ── formatting helpers ──────────────────────────────────────────────────────

function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  const u = ["KiB", "MiB", "GiB", "TiB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${u[i]}`;
}

function fmtCores(c: number | null | undefined): string {
  if (c == null) return "—";
  return c < 1 ? `${Math.round(c * 1000)}m` : c.toFixed(2);
}

function pct(
  used: number | null | undefined,
  cap: number | null | undefined,
): number | null {
  if (used == null || !cap) return null;
  return Math.max(0, Math.min(100, (used / cap) * 100));
}

function fmtAge(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function usageColor(p: number | null): string {
  if (p == null) return SLATE;
  if (p < 60) return EMERALD;
  if (p < 85) return AMBER;
  return ROSE;
}

function shortPodName(p: { name: string }): string {
  return p.name
    .replace(/^spatium-control-spatiumddi-/, "")
    .replace(/^spatium-bootstrap-/, "")
    .replace(/^spatium-/, "");
}

// ── live stream hook ─────────────────────────────────────────────────────────

function useClusterHealthStream() {
  const [snapshot, setSnapshot] = useState<ClusterHealthSnapshot | null>(null);
  const [history, setHistory] = useState<HistPoint[]>([]);
  const [connected, setConnected] = useState(false);
  const seq = useRef(0);

  useEffect(() => {
    const ctrl = new AbortController();
    let stopped = false;

    const ingest = (snap: ClusterHealthSnapshot) => {
      setSnapshot(snap);
      if (snap.available) {
        const cpu = pct(snap.cpu_usage_cores, snap.cpu_capacity_cores);
        const mem = pct(
          snap.memory_working_set_bytes,
          snap.memory_capacity_bytes,
        );
        setHistory((prev) => {
          const next = [...prev, { i: seq.current++, cpu, mem }];
          return next.length > MAX_POINTS
            ? next.slice(next.length - MAX_POINTS)
            : next;
        });
      }
    };

    // Instant first paint while the SSE stream warms up.
    applianceClusterApi
      .health()
      .then((s) => {
        if (!stopped) ingest(s);
      })
      .catch(() => {
        /* stream will deliver shortly */
      });

    const run = async () => {
      while (!stopped) {
        try {
          for await (const snap of streamClusterHealth(ctrl.signal)) {
            if (stopped) break;
            setConnected(true);
            ingest(snap);
          }
        } catch {
          if (stopped) break;
        }
        setConnected(false);
        if (stopped) break;
        await new Promise((r) => setTimeout(r, 2000)); // backoff then reconnect
      }
    };
    void run();

    return () => {
      stopped = true;
      ctrl.abort();
    };
  }, []);

  return { snapshot, history, connected };
}

// ── small visual primitives ──────────────────────────────────────────────────

function RadialGauge({
  value,
  label,
  sub,
}: {
  value: number | null;
  label: string;
  sub?: string;
}) {
  const p = value == null ? 0 : Math.max(0, Math.min(100, value));
  const R = 32;
  const C = 2 * Math.PI * R;
  const off = C * (1 - p / 100);
  const color = usageColor(value);
  return (
    <div className="flex flex-col items-center">
      <div className="relative h-[76px] w-[76px]">
        <svg viewBox="0 0 80 80" className="h-[76px] w-[76px] -rotate-90">
          <circle
            cx="40"
            cy="40"
            r={R}
            fill="none"
            strokeWidth="7"
            className="stroke-muted/40"
          />
          <circle
            cx="40"
            cy="40"
            r={R}
            fill="none"
            stroke={color}
            strokeWidth="7"
            strokeLinecap="round"
            strokeDasharray={C}
            strokeDashoffset={off}
            style={{
              transition: "stroke-dashoffset 0.7s ease, stroke 0.7s ease",
            }}
          />
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          <span
            className="text-base font-semibold tabular-nums"
            style={{ color }}
          >
            {value == null ? "—" : `${Math.round(value)}%`}
          </span>
        </div>
      </div>
      <span className="mt-1 text-[11px] font-medium text-muted-foreground">
        {label}
      </span>
      {sub && <span className="text-[10px] text-muted-foreground">{sub}</span>}
    </div>
  );
}

function Sparkline({
  data,
  color,
  max,
}: {
  data: number[];
  color: string;
  max?: number;
}) {
  const w = 100;
  const h = 28;
  if (data.length < 2) {
    return (
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="h-7 w-full"
        preserveAspectRatio="none"
      />
    );
  }
  const hi = max ?? Math.max(...data, 1);
  const lo = 0;
  const range = hi - lo || 1;
  const x = (i: number) => (i / (data.length - 1)) * w;
  const y = (v: number) =>
    h - ((Math.max(lo, Math.min(hi, v)) - lo) / range) * h;
  const line = data.map((v, i) => `${x(i)},${y(v)}`).join(" ");
  const area = `0,${h} ${line} ${w},${h}`;
  const gid = `spark-${color.replace("#", "")}`;
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      className="h-7 w-full"
      preserveAspectRatio="none"
    >
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.35} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      <polygon points={area} fill={`url(#${gid})`} />
      <polyline
        points={line}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

function Bar({ value, color }: { value: number; color: string }) {
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-muted/40">
      <div
        className="h-full rounded-full"
        style={{
          width: `${Math.max(2, Math.min(100, value))}%`,
          background: color,
          transition: "width 0.7s ease, background 0.7s ease",
        }}
      />
    </div>
  );
}

function KpiTile({
  icon: Icon,
  label,
  value,
  sub,
  accent,
  children,
}: {
  icon: typeof Cpu;
  label: string;
  value: string;
  sub?: string;
  accent: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="relative overflow-hidden rounded-xl border bg-card p-3 shadow-sm">
      <div
        className="pointer-events-none absolute inset-x-0 top-0 h-0.5"
        style={{ background: accent }}
      />
      <div className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        <Icon className="h-3.5 w-3.5" style={{ color: accent }} />
        {label}
      </div>
      <div className="mt-1 flex items-baseline gap-1.5">
        <span className="text-2xl font-semibold tabular-nums">{value}</span>
        {sub && <span className="text-xs text-muted-foreground">{sub}</span>}
      </div>
      {children && <div className="mt-1">{children}</div>}
    </div>
  );
}

function RoleBadge({ role }: { role: string }) {
  const color =
    role === "control-plane" || role === "master"
      ? VIOLET
      : role === "etcd"
        ? AMBER
        : SLATE;
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[10px] font-medium"
      style={{ color, background: `${color}1f` }}
    >
      {role}
    </span>
  );
}

function StatusChip({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    healthy: [EMERALD, "Healthy"],
    degraded: [AMBER, "Degraded"],
    down: [ROSE, "Down"],
  };
  const [color, text] = map[status] ?? [SLATE, status];
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{ color, background: `${color}1f` }}
    >
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: color }}
      />
      {text}
    </span>
  );
}

// ── node card ────────────────────────────────────────────────────────────────

function NodeCard({ node }: { node: ClusterNodeVitals }) {
  const cpuPct = pct(node.cpu_usage_cores, node.cpu_capacity_cores);
  const memPct = pct(node.memory_working_set_bytes, node.memory_capacity_bytes);
  const diskPct = pct(node.fs_used_bytes, node.fs_capacity_bytes);
  const pressure =
    node.memory_pressure || node.disk_pressure || node.pid_pressure;
  return (
    <div className="rounded-xl border bg-card p-4 shadow-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span
              className={`h-2.5 w-2.5 shrink-0 rounded-full ${node.ready ? "animate-pulse" : ""}`}
              style={{ background: node.ready ? EMERALD : ROSE }}
              title={node.ready ? "Ready" : "Not Ready"}
            />
            <span className="truncate font-semibold">{node.name}</span>
            {!node.schedulable && (
              <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-600 dark:text-amber-400">
                cordoned
              </span>
            )}
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {node.roles.map((r) => (
              <RoleBadge key={r} role={r} />
            ))}
          </div>
        </div>
        <div className="shrink-0 text-right text-[10px] text-muted-foreground">
          <div className="font-mono">{node.internal_ip ?? "—"}</div>
          <div>up {fmtAge(node.age_seconds)}</div>
        </div>
      </div>

      <div className="mt-3 flex items-center gap-4">
        <RadialGauge
          value={cpuPct}
          label="CPU"
          sub={`${fmtCores(node.cpu_usage_cores)} / ${fmtCores(node.cpu_capacity_cores)}`}
        />
        <RadialGauge
          value={memPct}
          label="Memory"
          sub={`${fmtBytes(node.memory_working_set_bytes)} / ${fmtBytes(node.memory_capacity_bytes)}`}
        />
        <div className="min-w-0 flex-1 space-y-2 text-[11px]">
          <div
            title={
              "Kubelet node filesystem — container images, pod volumes, and " +
              "ephemeral storage on the appliance's /var data partition. This " +
              "is the disk-pressure signal the kubelet evicts on. The OS slot " +
              "partitions (root A/B) and the ESP are managed separately and " +
              "aren't shown here."
            }
          >
            <div className="flex justify-between text-muted-foreground">
              <span className="flex items-center gap-1">
                <HardDrive className="h-3 w-3" /> Disk
                <span className="rounded bg-muted px-1 text-[9px] font-medium uppercase tracking-wide">
                  node /var
                </span>
              </span>
              <span className="tabular-nums">
                {fmtBytes(node.fs_used_bytes)} /{" "}
                {fmtBytes(node.fs_capacity_bytes)}
                {diskPct != null && (
                  <span className="ml-1 text-foreground">
                    · {Math.round(diskPct)}%
                  </span>
                )}
              </span>
            </div>
            <div className="mt-1">
              <Bar value={diskPct ?? 0} color={usageColor(diskPct)} />
            </div>
          </div>
          <div className="flex items-center justify-between text-muted-foreground">
            <span className="flex items-center gap-1">
              <Boxes className="h-3 w-3" /> Pods
            </span>
            <span className="tabular-nums text-foreground">
              {node.pods_running}
              {node.pods_capacity ? ` / ${node.pods_capacity}` : ""}
            </span>
          </div>
          <div
            className="truncate text-muted-foreground"
            title={node.os_image ?? ""}
          >
            {node.kubelet_version ?? "—"} · {node.architecture ?? "—"}
          </div>
          {pressure && (
            <div className="flex items-center gap-1 text-rose-500">
              <AlertTriangle className="h-3 w-3" />
              {[
                node.memory_pressure && "memory",
                node.disk_pressure && "disk",
                node.pid_pressure && "pid",
              ]
                .filter(Boolean)
                .join(" / ")}{" "}
              pressure
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── top-pods leaderboard ──────────────────────────────────────────────────────

function TopPods({
  cpu,
  mem,
}: {
  cpu: ClusterPodSummary[];
  mem: ClusterPodSummary[];
}) {
  const [mode, setMode] = useState<"cpu" | "mem">("cpu");
  const rows = mode === "cpu" ? cpu : mem;
  const peak = Math.max(
    1e-9,
    ...rows.map((p) =>
      mode === "cpu"
        ? (p.cpu_usage_cores ?? 0)
        : (p.memory_working_set_bytes ?? 0),
    ),
  );
  return (
    <div className="rounded-xl border bg-card p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Activity className="h-4 w-4 text-muted-foreground" />
          Top pods
        </h3>
        <div className="flex overflow-hidden rounded-md border text-[11px]">
          {(["cpu", "mem"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              className={`px-2 py-1 ${
                mode === m
                  ? "bg-accent text-foreground"
                  : "text-muted-foreground hover:bg-accent/50"
              }`}
            >
              {m === "cpu" ? "CPU" : "Memory"}
            </button>
          ))}
        </div>
      </div>
      {rows.length === 0 ? (
        <p className="mt-4 text-xs text-muted-foreground">
          No live pod usage yet (waiting for the kubelet Summary API).
        </p>
      ) : (
        <div className="mt-3 space-y-2">
          {rows.map((p) => {
            const v =
              mode === "cpu"
                ? (p.cpu_usage_cores ?? 0)
                : (p.memory_working_set_bytes ?? 0);
            return (
              <div key={`${p.namespace}/${p.name}`}>
                <div className="flex items-baseline justify-between gap-2 text-[11px]">
                  <span className="truncate font-mono">{shortPodName(p)}</span>
                  <span className="shrink-0 tabular-nums text-muted-foreground">
                    {mode === "cpu" ? `${fmtCores(v)} c` : fmtBytes(v)}
                  </span>
                </div>
                <div className="mt-0.5">
                  <Bar
                    value={(v / peak) * 100}
                    color={mode === "cpu" ? EMERALD : SKY}
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── hero live chart ───────────────────────────────────────────────────────────

function HeroChart({
  history,
  cpuPct,
  memPct,
}: {
  history: HistPoint[];
  cpuPct: number | null;
  memPct: number | null;
}) {
  return (
    <div className="rounded-xl border bg-card p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Activity className="h-4 w-4 text-muted-foreground" />
          Cluster load
          <span className="text-xs font-normal text-muted-foreground">
            · last ~3 min
          </span>
        </h3>
        <div className="flex items-center gap-4 text-xs">
          <span className="flex items-center gap-1.5">
            <span
              className="h-2 w-2 rounded-full"
              style={{ background: EMERALD }}
            />
            CPU{" "}
            <span
              className="font-semibold tabular-nums"
              style={{ color: EMERALD }}
            >
              {cpuPct == null ? "—" : `${Math.round(cpuPct)}%`}
            </span>
          </span>
          <span className="flex items-center gap-1.5">
            <span
              className="h-2 w-2 rounded-full"
              style={{ background: SKY }}
            />
            Mem{" "}
            <span className="font-semibold tabular-nums" style={{ color: SKY }}>
              {memPct == null ? "—" : `${Math.round(memPct)}%`}
            </span>
          </span>
        </div>
      </div>
      <div className="mt-3 h-56">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart
            data={history}
            margin={{ top: 6, right: 6, bottom: 0, left: -18 }}
          >
            <defs>
              <linearGradient id="cpuGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={EMERALD} stopOpacity={0.45} />
                <stop offset="100%" stopColor={EMERALD} stopOpacity={0} />
              </linearGradient>
              <linearGradient id="memGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={SKY} stopOpacity={0.4} />
                <stop offset="100%" stopColor={SKY} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid
              strokeDasharray="3 3"
              stroke="#94a3b8"
              strokeOpacity={0.18}
              vertical={false}
            />
            <XAxis dataKey="i" hide />
            <YAxis
              domain={[0, 100]}
              width={42}
              tick={{ fill: "#94a3b8", fontSize: 10 }}
              tickFormatter={(v: number) => `${v}%`}
              stroke="#94a3b8"
              strokeOpacity={0.3}
            />
            <Tooltip
              isAnimationActive={false}
              formatter={(v) => `${Math.round(Number(v))}%`}
              labelFormatter={() => ""}
              contentStyle={{
                background: "hsl(var(--card))",
                border: "1px solid hsl(var(--border))",
                borderRadius: 8,
                fontSize: 12,
              }}
            />
            <Area
              type="monotone"
              dataKey="mem"
              name="Memory"
              stroke={SKY}
              strokeWidth={2}
              fill="url(#memGrad)"
              isAnimationActive={false}
              dot={false}
              connectNulls
              style={{ filter: "drop-shadow(0 1px 4px rgba(14,165,233,0.4))" }}
            />
            <Area
              type="monotone"
              dataKey="cpu"
              name="CPU"
              stroke={EMERALD}
              strokeWidth={2}
              fill="url(#cpuGrad)"
              isAnimationActive={false}
              dot={false}
              connectNulls
              style={{ filter: "drop-shadow(0 1px 4px rgba(16,185,129,0.45))" }}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

// ── workload health ───────────────────────────────────────────────────────────

function WorkloadHealth({ workloads }: { workloads: ClusterWorkloadHealth[] }) {
  return (
    <div className="rounded-xl border bg-card p-4 shadow-sm">
      <h3 className="flex items-center gap-1.5 text-sm font-semibold">
        <Layers className="h-4 w-4 text-muted-foreground" />
        Workloads
      </h3>
      {workloads.length === 0 ? (
        <p className="mt-4 text-xs text-muted-foreground">
          No workloads reported.
        </p>
      ) : (
        <div className="mt-3 space-y-1.5">
          {workloads.map((w) => (
            <div
              key={w.component}
              className="flex items-center justify-between gap-2 rounded-md px-1 py-1 text-xs"
            >
              <div className="flex min-w-0 items-center gap-2">
                <StatusChip status={w.status} />
                <span className="truncate font-medium">{w.component}</span>
                {w.kind && (
                  <span className="hidden text-[10px] text-muted-foreground sm:inline">
                    {w.kind}
                  </span>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-3 text-muted-foreground">
                {w.restarts > 0 && (
                  <span
                    className="flex items-center gap-0.5 text-amber-600 dark:text-amber-400"
                    title={`${w.restarts} restarts`}
                  >
                    <RotateCcw className="h-3 w-3" />
                    {w.restarts}
                  </span>
                )}
                <span className="tabular-nums text-foreground">
                  {w.ready}/{w.total}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── main ──────────────────────────────────────────────────────────────────────

export function ClusterOverview() {
  const { snapshot, history, connected } = useClusterHealthStream();

  if (snapshot === null) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
        <Radio className="mr-2 h-4 w-4 animate-pulse" />
        Connecting to the cluster…
      </div>
    );
  }

  if (!snapshot.available) {
    return (
      <div className="mx-auto max-w-2xl rounded-xl border border-dashed bg-muted/30 px-6 py-12 text-center">
        <Server className="mx-auto h-8 w-8 text-muted-foreground" />
        <p className="mt-3 text-sm font-medium">Cluster metrics unavailable</p>
        <p className="mt-1 text-xs text-muted-foreground">
          {snapshot.detail ??
            "The api ServiceAccount can't read the cluster yet."}
        </p>
      </div>
    );
  }

  const s = snapshot;
  const cpuPct = pct(s.cpu_usage_cores, s.cpu_capacity_cores);
  const memPct = pct(s.memory_working_set_bytes, s.memory_capacity_bytes);
  const healthyWorkloads = s.workloads.filter(
    (w) => w.status === "healthy",
  ).length;
  const cpuSpark = history.map((h) => h.cpu ?? 0);
  const memSpark = history.map((h) => h.mem ?? 0);
  const phaseSummary = Object.entries(s.pods_by_phase)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k} ${v}`)
    .join(" · ");

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="flex items-center gap-2 text-base font-semibold">
          <Boxes className="h-4 w-4 text-muted-foreground" />
          Cluster overview
        </h2>
        <span
          className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium"
          style={{
            color: connected ? EMERALD : AMBER,
            borderColor: `${connected ? EMERALD : AMBER}55`,
            background: `${connected ? EMERALD : AMBER}12`,
          }}
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${connected ? "animate-pulse" : ""}`}
            style={{ background: connected ? EMERALD : AMBER }}
          />
          {connected ? "LIVE" : "reconnecting…"}
        </span>
        <span className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
          {s.kubelet_version && (
            <span className="rounded-md bg-muted px-1.5 py-0.5 font-mono">
              {s.kubelet_version}
            </span>
          )}
          <span
            className="rounded-md px-1.5 py-0.5 font-medium"
            style={
              s.is_ha
                ? { color: EMERALD, background: `${EMERALD}1f` }
                : { color: SLATE, background: `${SLATE}1f` }
            }
          >
            {s.is_ha ? `HA · ${s.control_plane_nodes} nodes` : "single node"}
          </span>
          {!s.metrics_available && (
            <span className="rounded-md bg-amber-500/15 px-1.5 py-0.5 text-amber-600 dark:text-amber-400">
              live usage unavailable
            </span>
          )}
        </span>
      </div>

      {/* KPI ribbon */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <KpiTile
          icon={Cpu}
          label="CPU"
          value={cpuPct == null ? "—" : `${Math.round(cpuPct)}%`}
          sub={`${fmtCores(s.cpu_usage_cores)} / ${fmtCores(s.cpu_capacity_cores)} c`}
          accent={EMERALD}
        >
          <Sparkline data={cpuSpark} color={EMERALD} max={100} />
        </KpiTile>
        <KpiTile
          icon={MemoryStick}
          label="Memory"
          value={memPct == null ? "—" : `${Math.round(memPct)}%`}
          sub={`${fmtBytes(s.memory_working_set_bytes)} / ${fmtBytes(s.memory_capacity_bytes)}`}
          accent={SKY}
        >
          <Sparkline data={memSpark} color={SKY} max={100} />
        </KpiTile>
        <KpiTile
          icon={Server}
          label="Nodes ready"
          value={`${s.nodes_ready}/${s.nodes_total}`}
          accent={s.nodes_ready === s.nodes_total ? EMERALD : ROSE}
        >
          <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
            <CheckCircle2 className="h-3 w-3" style={{ color: EMERALD }} />
            {s.control_plane_nodes} control-plane
          </div>
        </KpiTile>
        <KpiTile
          icon={Boxes}
          label="Pods running"
          value={`${s.pods_running}/${s.pods_total}`}
          accent={VIOLET}
        >
          <div
            className="truncate text-[11px] text-muted-foreground"
            title={phaseSummary}
          >
            {phaseSummary || "—"}
          </div>
        </KpiTile>
        <KpiTile
          icon={Database}
          label="Workloads"
          value={`${healthyWorkloads}/${s.workloads.length}`}
          sub="healthy"
          accent={healthyWorkloads === s.workloads.length ? EMERALD : AMBER}
        >
          <div className="flex flex-wrap gap-0.5">
            {s.workloads.slice(0, 14).map((w) => (
              <span
                key={w.component}
                className="h-1.5 w-1.5 rounded-full"
                title={`${w.component}: ${w.status}`}
                style={{
                  background:
                    w.status === "healthy"
                      ? EMERALD
                      : w.status === "degraded"
                        ? AMBER
                        : ROSE,
                }}
              />
            ))}
          </div>
        </KpiTile>
      </div>

      {/* Hero live chart */}
      <HeroChart history={history} cpuPct={cpuPct} memPct={memPct} />

      {/* Nodes */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        {s.nodes.map((n) => (
          <NodeCard key={n.name} node={n} />
        ))}
      </div>

      {/* Workloads + top pods */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <WorkloadHealth workloads={s.workloads} />
        <TopPods cpu={s.top_pods_cpu} mem={s.top_pods_mem} />
      </div>
    </div>
  );
}
