import { useQuery } from "@tanstack/react-query";
import { Network, Layers, Server, Globe } from "lucide-react";
import { ipamApi, type Subnet } from "@/lib/api";
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
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
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
        </div>

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
