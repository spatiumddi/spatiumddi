import { useQuery, useQueries } from "@tanstack/react-query";
import { Network, Layers, Server, Globe, Globe2, FileText, Cpu } from "lucide-react";
import { ipamApi, dnsApi, type Subnet, type DNSServer } from "@/lib/api";
import { cn } from "@/lib/utils";

function StatCard({
  label,
  value,
  icon: Icon,
  sub,
}: {
  label: string;
  value: string | number;
  icon: React.ElementType;
  sub?: string;
}) {
  return (
    <div className="rounded-lg border bg-card p-5">
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-muted-foreground">{label}</p>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </div>
      <p className="mt-2 text-3xl font-bold">{value}</p>
      {sub && <p className="mt-1 text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}

function UtilizationBar({ percent }: { percent: number }) {
  const color =
    percent >= 95 ? "bg-red-500" : percent >= 80 ? "bg-amber-400" : "bg-green-500";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 flex-1 rounded-full bg-muted">
        <div
          className={cn("h-full rounded-full transition-all", color)}
          style={{ width: `${Math.min(percent, 100)}%` }}
        />
      </div>
      <span className="w-10 text-right text-xs tabular-nums text-muted-foreground">
        {percent.toFixed(0)}%
      </span>
    </div>
  );
}

export function DashboardPage() {
  const { data: spaces } = useQuery({
    queryKey: ["spaces"],
    queryFn: ipamApi.listSpaces,
  });

  const { data: subnets } = useQuery({
    queryKey: ["subnets"],
    queryFn: () => ipamApi.listSubnets(),
  });

  const { data: dnsGroups = [] } = useQuery({
    queryKey: ["dns-groups"],
    queryFn: dnsApi.listGroups,
    staleTime: 30_000,
  });

  const zoneQueries = useQueries({
    queries: dnsGroups.map((g) => ({
      queryKey: ["dns-zones", g.id],
      queryFn: () => dnsApi.listZones(g.id),
      staleTime: 30_000,
    })),
  });
  const totalZones = zoneQueries.reduce((sum, q) => sum + (q.data?.length ?? 0), 0);

  const serverQueries = useQueries({
    queries: dnsGroups.map((g) => ({
      queryKey: ["dns-servers", g.id],
      queryFn: () => dnsApi.listServers(g.id),
      refetchInterval: 30_000,
    })),
  });
  const allServers: DNSServer[] = serverQueries.flatMap((q) => q.data ?? []);
  const serverCounts = allServers.reduce(
    (acc, s) => {
      acc[s.status] = (acc[s.status] ?? 0) + 1;
      return acc;
    },
    {} as Record<string, number>,
  );
  const activeServers = serverCounts.active ?? 0;
  const unhealthyServers = (serverCounts.unreachable ?? 0) + (serverCounts.error ?? 0);

  const totalIPs = subnets?.reduce((s, n) => s + n.total_ips, 0) ?? 0;
  const allocatedIPs = subnets?.reduce((s, n) => s + n.allocated_ips, 0) ?? 0;
  const overallUtil = totalIPs > 0 ? (allocatedIPs / totalIPs) * 100 : 0;

  const topSubnets = subnets
    ? [...subnets]
        .filter((s) => s.total_ips > 0)
        .sort((a, b) => b.utilization_percent - a.utilization_percent)
        .slice(0, 8)
    : [];

  const critical = subnets?.filter((s) => s.utilization_percent >= 95).length ?? 0;
  const warning = subnets?.filter((s) => s.utilization_percent >= 80 && s.utilization_percent < 95).length ?? 0;

  return (
    <div className="h-full overflow-auto p-6">
      <div className="space-y-6">
        <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>

        {/* Stat cards */}
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <StatCard
            label="IP Spaces"
            value={spaces?.length ?? "—"}
            icon={Layers}
          />
          <StatCard
            label="Subnets"
            value={subnets?.length ?? "—"}
            icon={Network}
            sub={
              critical > 0
                ? `${critical} critical, ${warning} warning`
                : warning > 0
                  ? `${warning} near capacity`
                  : "All healthy"
            }
          />
          <StatCard
            label="IP Addresses"
            value={allocatedIPs.toLocaleString()}
            icon={Server}
            sub={`of ${totalIPs.toLocaleString()} total`}
          />
          <StatCard
            label="Overall Utilization"
            value={`${overallUtil.toFixed(1)}%`}
            icon={Globe}
            sub={`${allocatedIPs.toLocaleString()} allocated`}
          />
          <StatCard
            label="DNS Server Groups"
            value={dnsGroups.length}
            icon={Globe2}
          />
          <StatCard
            label="DNS Zones"
            value={totalZones}
            icon={FileText}
            sub={dnsGroups.length > 0 ? `across ${dnsGroups.length} group${dnsGroups.length !== 1 ? "s" : ""}` : undefined}
          />
          <StatCard
            label="DNS Servers"
            value={allServers.length}
            icon={Cpu}
            sub={
              allServers.length === 0
                ? "none registered"
                : unhealthyServers > 0
                  ? `${activeServers} active, ${unhealthyServers} unhealthy`
                  : `${activeServers} active`
            }
          />
        </div>

        {/* DNS server health */}
        {allServers.length > 0 && (
          <div className="rounded-lg border">
            <div className="flex items-center justify-between border-b px-4 py-3">
              <h2 className="text-sm font-semibold">DNS Server Status</h2>
              <div className="flex items-center gap-3 text-xs">
                {(["active", "syncing", "unreachable", "error"] as const).map((s) =>
                  serverCounts[s] ? (
                    <span key={s} className="flex items-center gap-1.5">
                      <span className={cn("inline-block h-2 w-2 rounded-full", {
                        active: "bg-emerald-500",
                        syncing: "bg-blue-500",
                        unreachable: "bg-red-500",
                        error: "bg-red-500",
                      }[s])} />
                      {serverCounts[s]} {s}
                    </span>
                  ) : null,
                )}
              </div>
            </div>
            <div className="divide-y">
              {allServers.map((s) => {
                const group = dnsGroups.find((g) => g.id === s.group_id);
                const dotCls = {
                  active: "bg-emerald-500",
                  syncing: "bg-blue-500",
                  unreachable: "bg-red-500",
                  error: "bg-red-500",
                }[s.status] ?? "bg-muted";
                return (
                  <div key={s.id} className="flex items-center gap-4 px-4 py-2.5">
                    <span
                      className={cn("inline-block h-2 w-2 rounded-full flex-shrink-0", dotCls)}
                      title={s.status}
                    />
                    <span className="w-48 truncate text-xs font-medium">{s.name}</span>
                    <span className="w-56 truncate font-mono text-xs text-muted-foreground">
                      {s.host}:{s.port}
                    </span>
                    <span className="w-40 truncate text-xs text-muted-foreground">
                      {group?.name ?? "—"}
                    </span>
                    <span className="w-20 text-xs text-muted-foreground">{s.driver}</span>
                    <span className="flex-1 text-right text-xs text-muted-foreground">
                      {s.last_health_check_at
                        ? `checked ${new Date(s.last_health_check_at).toLocaleTimeString()}`
                        : "never checked"}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Top subnets by utilization */}
        {topSubnets.length > 0 && (
          <div className="rounded-lg border">
            <div className="border-b px-4 py-3">
              <h2 className="text-sm font-semibold">Top Subnets by Utilization</h2>
            </div>
            <div className="divide-y">
              {topSubnets.map((subnet: Subnet) => (
                <div key={subnet.id} className="flex items-center gap-4 px-4 py-2.5">
                  <span className="w-36 flex-shrink-0 font-mono text-xs">{subnet.network}</span>
                  <span className="w-40 truncate text-xs text-muted-foreground">
                    {subnet.name || <span className="text-muted-foreground/40">—</span>}
                  </span>
                  <div className="flex-1">
                    <UtilizationBar percent={subnet.utilization_percent} />
                  </div>
                  <span className="w-24 text-right text-xs text-muted-foreground">
                    {subnet.allocated_ips} / {subnet.total_ips}
                  </span>
                  <span
                    className={cn(
                      "rounded-full px-2 py-0.5 text-xs font-medium",
                      subnet.status === "active"
                        ? "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"
                        : "bg-muted text-muted-foreground"
                    )}
                  >
                    {subnet.status}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Empty state */}
        {subnets?.length === 0 && (
          <div className="rounded-lg border border-dashed p-10 text-center">
            <Network className="mx-auto mb-3 h-10 w-10 text-muted-foreground/30" />
            <p className="text-sm font-medium">No subnets yet</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Create an IP space and add subnets to get started.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
