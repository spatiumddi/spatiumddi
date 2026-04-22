/**
 * Built-in dashboard charts for DNS queries and DHCP traffic.
 *
 * Data comes from the agent-driven `metric_sample` tables via
 * `metricsApi.{dnsTimeseries,dhcpTimeseries}`. The cards render empty
 * states that explain where data comes from when there is none — new
 * installs will land on the dashboard before any agent has reported,
 * and we don't want "no data" to look like a bug.
 *
 * Selectors:
 *   - window picker (1h / 6h / 24h / 7d)
 *   - server picker ("All servers" default, or scope to one)
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Activity, Network } from "lucide-react";

import {
  metricsApi,
  type DHCPServer,
  type DNSServer,
  type MetricsWindow,
} from "@/lib/api";

const WINDOWS: { key: MetricsWindow; label: string }[] = [
  { key: "1h", label: "1h" },
  { key: "6h", label: "6h" },
  { key: "24h", label: "24h" },
  { key: "7d", label: "7d" },
];

const ALL_SERVERS = "__all__";

function formatT(iso: string, bucketSeconds: number): string {
  const d = new Date(iso);
  if (bucketSeconds >= 300) {
    return d.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function perSecond(value: number, bucketSeconds: number): number {
  if (bucketSeconds <= 0) return 0;
  return +(value / bucketSeconds).toFixed(2);
}

function WindowPicker({
  value,
  onChange,
}: {
  value: MetricsWindow;
  onChange: (w: MetricsWindow) => void;
}) {
  return (
    <div className="flex items-center gap-0.5 rounded-md border bg-muted/40 p-0.5 text-[11px]">
      {WINDOWS.map((w) => (
        <button
          key={w.key}
          type="button"
          onClick={() => onChange(w.key)}
          className={
            "rounded px-2 py-0.5 transition-colors " +
            (value === w.key
              ? "bg-background font-semibold shadow-sm"
              : "text-muted-foreground hover:bg-background/60")
          }
        >
          {w.label}
        </button>
      ))}
    </div>
  );
}

function ServerPicker({
  value,
  onChange,
  servers,
}: {
  value: string;
  onChange: (id: string) => void;
  servers: { id: string; name: string }[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-md border bg-background px-2 py-0.5 text-[11px] hover:bg-accent/40 focus:outline-none focus:ring-1 focus:ring-ring"
    >
      <option value={ALL_SERVERS}>All servers</option>
      {servers.map((s) => (
        <option key={s.id} value={s.id}>
          {s.name}
        </option>
      ))}
    </select>
  );
}

export function DNSQueryRateCard({
  dnsServers = [],
}: {
  dnsServers?: DNSServer[];
}) {
  const [win, setWin] = useState<MetricsWindow>("24h");
  const [serverId, setServerId] = useState<string>(ALL_SERVERS);
  const scopedId = serverId === ALL_SERVERS ? undefined : serverId;
  const { data, isLoading } = useQuery({
    queryKey: ["metrics", "dns", win, scopedId ?? ""],
    queryFn: () =>
      metricsApi.dnsTimeseries({ window: win, server_id: scopedId }),
    refetchInterval: 60_000,
  });

  const points = (data?.points ?? []).map((p) => ({
    t: formatT(p.t, data?.bucket_seconds ?? 60),
    queries: perSecond(p.queries_total, data?.bucket_seconds ?? 60),
    noerror: perSecond(p.noerror, data?.bucket_seconds ?? 60),
    nxdomain: perSecond(p.nxdomain, data?.bucket_seconds ?? 60),
    servfail: perSecond(p.servfail, data?.bucket_seconds ?? 60),
  }));

  return (
    <div className="rounded-lg border bg-card">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Activity className="h-3.5 w-3.5 text-blue-500" />
          <h3 className="text-xs font-semibold uppercase tracking-wider">
            DNS Query Rate
          </h3>
        </div>
        <div className="flex items-center gap-2">
          <ServerPicker
            value={serverId}
            onChange={setServerId}
            servers={dnsServers}
          />
          <WindowPicker value={win} onChange={setWin} />
        </div>
      </div>
      <div className="h-64 p-3">
        {isLoading ? (
          <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
            Loading…
          </div>
        ) : points.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-1 text-center text-xs text-muted-foreground">
            <span>No query data yet.</span>
            <span className="text-[11px]">
              BIND9 agents report counters every 60&nbsp;s.
            </span>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={points}
              margin={{ top: 5, right: 12, left: 0, bottom: 0 }}
            >
              <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
              <XAxis dataKey="t" tick={{ fontSize: 10 }} minTickGap={32} />
              <YAxis
                tick={{ fontSize: 10 }}
                width={44}
                label={{
                  value: "qps",
                  angle: -90,
                  position: "insideLeft",
                  style: { fontSize: 10, textAnchor: "middle" },
                }}
              />
              <Tooltip
                contentStyle={{ fontSize: 11, borderRadius: 6 }}
                labelStyle={{ fontWeight: 600 }}
              />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Line
                type="monotone"
                dataKey="queries"
                stroke="#3b82f6"
                strokeWidth={2}
                dot={false}
                name="Total"
              />
              <Line
                type="monotone"
                dataKey="noerror"
                stroke="#10b981"
                strokeWidth={1.5}
                dot={false}
                name="NOERROR"
              />
              <Line
                type="monotone"
                dataKey="nxdomain"
                stroke="#f59e0b"
                strokeWidth={1.5}
                dot={false}
                name="NXDOMAIN"
              />
              <Line
                type="monotone"
                dataKey="servfail"
                stroke="#ef4444"
                strokeWidth={1.5}
                dot={false}
                name="SERVFAIL"
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}

export function DHCPTrafficCard({
  dhcpServers = [],
}: {
  dhcpServers?: DHCPServer[];
}) {
  const [win, setWin] = useState<MetricsWindow>("24h");
  const [serverId, setServerId] = useState<string>(ALL_SERVERS);
  const scopedId = serverId === ALL_SERVERS ? undefined : serverId;
  const { data, isLoading } = useQuery({
    queryKey: ["metrics", "dhcp", win, scopedId ?? ""],
    queryFn: () =>
      metricsApi.dhcpTimeseries({ window: win, server_id: scopedId }),
    refetchInterval: 60_000,
  });

  const points = (data?.points ?? []).map((p) => ({
    t: formatT(p.t, data?.bucket_seconds ?? 60),
    discover: perSecond(p.discover, data?.bucket_seconds ?? 60),
    request: perSecond(p.request, data?.bucket_seconds ?? 60),
    ack: perSecond(p.ack, data?.bucket_seconds ?? 60),
    nak: perSecond(p.nak, data?.bucket_seconds ?? 60),
  }));

  return (
    <div className="rounded-lg border bg-card">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Network className="h-3.5 w-3.5 text-violet-500" />
          <h3 className="text-xs font-semibold uppercase tracking-wider">
            DHCP Traffic
          </h3>
        </div>
        <div className="flex items-center gap-2">
          <ServerPicker
            value={serverId}
            onChange={setServerId}
            servers={dhcpServers}
          />
          <WindowPicker value={win} onChange={setWin} />
        </div>
      </div>
      <div className="h-64 p-3">
        {isLoading ? (
          <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
            Loading…
          </div>
        ) : points.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-1 text-center text-xs text-muted-foreground">
            <span>No DHCP traffic yet.</span>
            <span className="text-[11px]">
              Kea agents report packet counters every 60&nbsp;s.
            </span>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={points}
              margin={{ top: 5, right: 12, left: 0, bottom: 0 }}
            >
              <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
              <XAxis dataKey="t" tick={{ fontSize: 10 }} minTickGap={32} />
              <YAxis
                tick={{ fontSize: 10 }}
                width={44}
                label={{
                  value: "pkt/s",
                  angle: -90,
                  position: "insideLeft",
                  style: { fontSize: 10, textAnchor: "middle" },
                }}
              />
              <Tooltip
                contentStyle={{ fontSize: 11, borderRadius: 6 }}
                labelStyle={{ fontWeight: 600 }}
              />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Area
                type="monotone"
                dataKey="discover"
                stackId="1"
                stroke="#8b5cf6"
                fill="#8b5cf6"
                fillOpacity={0.35}
                name="DISCOVER"
              />
              <Area
                type="monotone"
                dataKey="request"
                stackId="1"
                stroke="#3b82f6"
                fill="#3b82f6"
                fillOpacity={0.35}
                name="REQUEST"
              />
              <Area
                type="monotone"
                dataKey="ack"
                stackId="1"
                stroke="#10b981"
                fill="#10b981"
                fillOpacity={0.35}
                name="ACK"
              />
              <Area
                type="monotone"
                dataKey="nak"
                stackId="1"
                stroke="#ef4444"
                fill="#ef4444"
                fillOpacity={0.35}
                name="NAK"
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
