import { useQuery, useQueries } from "@tanstack/react-query";
import {
  Network,
  Layers,
  Server,
  Globe,
  Globe2,
  FileText,
  Cpu,
  Router as RouterIcon,
  Tag,
  ClipboardList,
  CheckCircle2,
  AlertTriangle,
  AlertCircle,
} from "lucide-react";
import {
  ipamApi,
  dnsApi,
  dhcpApi,
  vlansApi,
  auditApi,
  type Subnet,
  type DNSServer,
  type DHCPServer,
  type VLAN,
} from "@/lib/api";
import { cn } from "@/lib/utils";

// ── Building blocks ──────────────────────────────────────────────────────────

function StatCard({
  label,
  value,
  icon: Icon,
  sub,
  tone,
}: {
  label: string;
  value: string | number;
  icon: React.ElementType;
  sub?: string;
  tone?: "default" | "warn" | "bad" | "good";
}) {
  const toneCls = {
    default: "",
    good: "text-emerald-600 dark:text-emerald-400",
    warn: "text-amber-600 dark:text-amber-400",
    bad: "text-red-600 dark:text-red-400",
  }[tone ?? "default"];
  return (
    <div className="rounded-lg border bg-card p-5">
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-muted-foreground">{label}</p>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </div>
      <p className={cn("mt-2 text-3xl font-bold", toneCls)}>{value}</p>
      {sub && <p className="mt-1 text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}

function SectionHeader({
  title,
  icon: Icon,
  hint,
}: {
  title: string;
  icon: React.ElementType;
  hint?: React.ReactNode;
}) {
  return (
    <div className="mb-3 flex items-end justify-between">
      <h2 className="flex items-center gap-2 text-base font-semibold">
        <Icon className="h-4 w-4 text-muted-foreground" />
        {title}
      </h2>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function UtilizationBar({ percent }: { percent: number }) {
  const color =
    percent >= 95
      ? "bg-red-500"
      : percent >= 80
        ? "bg-amber-400"
        : "bg-green-500";
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

const DOT_CLS: Record<string, string> = {
  active: "bg-emerald-500",
  syncing: "bg-blue-500",
  unreachable: "bg-red-500",
  error: "bg-red-500",
};

// ── Page ─────────────────────────────────────────────────────────────────────

export function DashboardPage() {
  // IPAM
  const { data: spaces } = useQuery({
    queryKey: ["spaces"],
    queryFn: ipamApi.listSpaces,
  });
  const { data: subnets } = useQuery({
    queryKey: ["subnets"],
    queryFn: () => ipamApi.listSubnets(),
  });

  // VLANs
  const { data: routers = [] } = useQuery({
    queryKey: ["vlans", "routers"],
    queryFn: vlansApi.listRouters,
    staleTime: 30_000,
  });
  const vlanQueries = useQueries({
    queries: routers.map((r) => ({
      queryKey: ["vlans", r.id],
      queryFn: () => vlansApi.listVlans(r.id),
      staleTime: 30_000,
    })),
  });
  const allVlans: VLAN[] = vlanQueries.flatMap((q) => q.data ?? []);

  // DNS
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
  const totalZones = zoneQueries.reduce(
    (sum, q) => sum + (q.data?.length ?? 0),
    0,
  );
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
  const unhealthyServers =
    (serverCounts.unreachable ?? 0) + (serverCounts.error ?? 0);

  // DHCP
  const { data: dhcpServers = [] } = useQuery<DHCPServer[]>({
    queryKey: ["dhcp-servers"],
    queryFn: () => dhcpApi.listServers(),
    refetchInterval: 30_000,
  });
  const { data: dhcpGroups = [] } = useQuery({
    queryKey: ["dhcp-groups"],
    queryFn: dhcpApi.listGroups,
    staleTime: 60_000,
  });
  const dhcpCounts = dhcpServers.reduce(
    (acc, s) => {
      acc[s.status] = (acc[s.status] ?? 0) + 1;
      return acc;
    },
    {} as Record<string, number>,
  );
  const dhcpActive = dhcpCounts.active ?? 0;
  const dhcpUnhealthy =
    (dhcpCounts.unreachable ?? 0) + (dhcpCounts.error ?? 0);
  const dhcpPending = dhcpServers.filter((s) => !s.agent_approved).length;

  // Recent activity
  const { data: recent } = useQuery({
    queryKey: ["audit", "recent"],
    queryFn: () => auditApi.list({ limit: 10, offset: 0 }),
    staleTime: 15_000,
    refetchInterval: 60_000,
  });

  // Derived IPAM stats
  const totalIPs = subnets?.reduce((s, n) => s + n.total_ips, 0) ?? 0;
  const allocatedIPs = subnets?.reduce((s, n) => s + n.allocated_ips, 0) ?? 0;
  const overallUtil = totalIPs > 0 ? (allocatedIPs / totalIPs) * 100 : 0;
  const topSubnets = subnets
    ? [...subnets]
        .filter((s) => s.total_ips > 0)
        .sort((a, b) => b.utilization_percent - a.utilization_percent)
        .slice(0, 8)
    : [];
  const critical =
    subnets?.filter((s) => s.utilization_percent >= 95).length ?? 0;
  const warning =
    subnets?.filter(
      (s) => s.utilization_percent >= 80 && s.utilization_percent < 95,
    ).length ?? 0;

  // Derived VLAN stats
  const subnetsWithVlan =
    subnets?.filter((s) => s.vlan_ref_id != null).length ?? 0;
  const subnetsWithRawTagOnly =
    subnets?.filter((s) => s.vlan_id != null && s.vlan_ref_id == null)
      .length ?? 0;
  // Aggregate per-router: vlan_count + subnet_count (derived from the
  // already-cached `subnets` list and per-router vlan queries — no extra
  // requests beyond what the VLANs page would make anyway).
  const vlanByRouter = new Map<string, VLAN[]>();
  routers.forEach((r, i) => {
    vlanByRouter.set(r.id, vlanQueries[i]?.data ?? []);
  });
  const subnetCountByVlanId = new Map<string, number>();
  subnets?.forEach((s) => {
    if (s.vlan_ref_id) {
      subnetCountByVlanId.set(
        s.vlan_ref_id,
        (subnetCountByVlanId.get(s.vlan_ref_id) ?? 0) + 1,
      );
    }
  });
  const routersSummary = routers
    .map((r) => {
      const vlans = vlanByRouter.get(r.id) ?? [];
      const subnetCount = vlans.reduce(
        (sum, v) => sum + (subnetCountByVlanId.get(v.id) ?? 0),
        0,
      );
      return {
        id: r.id,
        name: r.name,
        location: r.location,
        vlanCount: vlans.length,
        subnetCount,
      };
    })
    .sort((a, b) => b.subnetCount - a.subnetCount || b.vlanCount - a.vlanCount)
    .slice(0, 8);

  return (
    <div className="h-full overflow-auto p-6">
      <div className="space-y-8">
        <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>

        {/* ── Overview ───────────────────────────────────────────────────── */}
        <section>
          <SectionHeader title="Overview" icon={Globe} />
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
              tone={critical > 0 ? "bad" : warning > 0 ? "warn" : undefined}
            />
            <StatCard
              label="Overall Utilization"
              value={`${overallUtil.toFixed(1)}%`}
              icon={Server}
              sub={`${allocatedIPs.toLocaleString()} of ${totalIPs.toLocaleString()} IPs`}
              tone={
                overallUtil >= 95
                  ? "bad"
                  : overallUtil >= 80
                    ? "warn"
                    : undefined
              }
            />
            <StatCard
              label="Routers / VLANs"
              value={`${routers.length} / ${allVlans.length}`}
              icon={RouterIcon}
              sub={
                subnetsWithVlan > 0
                  ? `${subnetsWithVlan} subnet${subnetsWithVlan === 1 ? "" : "s"} assigned`
                  : "no subnets assigned yet"
              }
            />
            <StatCard
              label="DNS Groups"
              value={dnsGroups.length}
              icon={Globe2}
            />
            <StatCard
              label="DNS Zones"
              value={totalZones}
              icon={FileText}
              sub={
                dnsGroups.length > 0
                  ? `across ${dnsGroups.length} group${dnsGroups.length === 1 ? "" : "s"}`
                  : undefined
              }
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
              tone={unhealthyServers > 0 ? "bad" : undefined}
            />
            <StatCard
              label="DHCP Servers"
              value={dhcpServers.length}
              icon={Server}
              sub={
                dhcpServers.length === 0
                  ? "none registered"
                  : dhcpPending > 0
                    ? `${dhcpActive} active, ${dhcpPending} pending approval`
                    : dhcpUnhealthy > 0
                      ? `${dhcpActive} active, ${dhcpUnhealthy} unhealthy`
                      : `${dhcpActive} active`
              }
              tone={
                dhcpUnhealthy > 0
                  ? "bad"
                  : dhcpPending > 0
                    ? "warn"
                    : undefined
              }
            />
            <StatCard
              label="Allocated IPs"
              value={allocatedIPs.toLocaleString()}
              icon={Network}
              sub={`${(totalIPs - allocatedIPs).toLocaleString()} free`}
            />
          </div>
        </section>

        {/* ── IPAM ──────────────────────────────────────────────────────── */}
        {topSubnets.length > 0 && (
          <section>
            <SectionHeader
              title="IPAM — Top Subnets by Utilization"
              icon={Network}
              hint={`Showing ${topSubnets.length} of ${subnets?.length ?? 0}`}
            />
            <div className="rounded-lg border">
              <div className="divide-y">
                {topSubnets.map((subnet: Subnet) => (
                  <div
                    key={subnet.id}
                    className="flex items-center gap-4 px-4 py-2.5"
                  >
                    <span className="w-36 flex-shrink-0 font-mono text-xs">
                      {subnet.network}
                    </span>
                    <span className="w-40 truncate text-xs text-muted-foreground">
                      {subnet.name || (
                        <span className="text-muted-foreground/40">—</span>
                      )}
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
                          : "bg-muted text-muted-foreground",
                      )}
                    >
                      {subnet.status}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </section>
        )}

        {/* ── VLANs ─────────────────────────────────────────────────────── */}
        {routers.length > 0 && (
          <section>
            <SectionHeader
              title="VLANs"
              icon={Tag}
              hint={
                subnetsWithRawTagOnly > 0
                  ? `${subnetsWithRawTagOnly} subnet${subnetsWithRawTagOnly === 1 ? "" : "s"} still on a legacy raw tag`
                  : undefined
              }
            />
            <div className="rounded-lg border">
              <div className="border-b bg-muted/30 px-4 py-2 text-xs font-medium text-muted-foreground">
                <div className="grid grid-cols-[1fr_1fr_80px_80px] gap-4">
                  <span>Router</span>
                  <span>Location</span>
                  <span className="text-right">VLANs</span>
                  <span className="text-right">Subnets</span>
                </div>
              </div>
              <div className="divide-y">
                {routersSummary.map((r) => (
                  <div
                    key={r.id}
                    className="grid grid-cols-[1fr_1fr_80px_80px] items-center gap-4 px-4 py-2.5 text-xs"
                  >
                    <span className="truncate font-medium">{r.name}</span>
                    <span className="truncate text-muted-foreground">
                      {r.location || (
                        <span className="text-muted-foreground/40">—</span>
                      )}
                    </span>
                    <span className="text-right tabular-nums">
                      {r.vlanCount}
                    </span>
                    <span
                      className={cn(
                        "text-right tabular-nums",
                        r.subnetCount === 0 && "text-muted-foreground/50",
                      )}
                    >
                      {r.subnetCount}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </section>
        )}

        {/* ── DNS ───────────────────────────────────────────────────────── */}
        {allServers.length > 0 && (
          <section>
            <SectionHeader
              title="DNS — Server Status"
              icon={Cpu}
              hint={
                <>
                  {(
                    ["active", "syncing", "unreachable", "error"] as const
                  ).map((s) =>
                    serverCounts[s] ? (
                      <span
                        key={s}
                        className="mr-3 inline-flex items-center gap-1.5"
                      >
                        <span
                          className={cn(
                            "inline-block h-2 w-2 rounded-full",
                            DOT_CLS[s],
                          )}
                        />
                        {serverCounts[s]} {s}
                      </span>
                    ) : null,
                  )}
                </>
              }
            />
            <div className="rounded-lg border">
              <div className="divide-y">
                {allServers.map((s) => {
                  const group = dnsGroups.find((g) => g.id === s.group_id);
                  const dotCls = DOT_CLS[s.status] ?? "bg-muted";
                  return (
                    <div
                      key={s.id}
                      className="flex items-center gap-4 px-4 py-2.5"
                    >
                      <span
                        className={cn(
                          "inline-block h-2 w-2 rounded-full flex-shrink-0",
                          dotCls,
                        )}
                        title={s.status}
                      />
                      <span className="w-48 truncate text-xs font-medium">
                        {s.name}
                      </span>
                      <span className="w-56 truncate font-mono text-xs text-muted-foreground">
                        {s.host}:{s.port}
                      </span>
                      <span className="w-40 truncate text-xs text-muted-foreground">
                        {group?.name ?? "—"}
                      </span>
                      <span className="w-20 text-xs text-muted-foreground">
                        {s.driver}
                      </span>
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
          </section>
        )}

        {/* ── DHCP ──────────────────────────────────────────────────────── */}
        {dhcpServers.length > 0 && (
          <section>
            <SectionHeader
              title="DHCP — Server Status"
              icon={Server}
              hint={
                <>
                  {(
                    ["active", "syncing", "unreachable", "error"] as const
                  ).map((s) =>
                    dhcpCounts[s] ? (
                      <span
                        key={s}
                        className="mr-3 inline-flex items-center gap-1.5"
                      >
                        <span
                          className={cn(
                            "inline-block h-2 w-2 rounded-full",
                            DOT_CLS[s],
                          )}
                        />
                        {dhcpCounts[s]} {s}
                      </span>
                    ) : null,
                  )}
                  {dhcpPending > 0 && (
                    <span className="mr-3 inline-flex items-center gap-1.5 text-amber-600">
                      <span className="inline-block h-2 w-2 rounded-full bg-amber-500" />
                      {dhcpPending} pending approval
                    </span>
                  )}
                </>
              }
            />
            <div className="rounded-lg border">
              <div className="divide-y">
                {dhcpServers.map((s) => {
                  const group = dhcpGroups.find(
                    (g) => g.id === s.server_group_id,
                  );
                  const dotCls = DOT_CLS[s.status] ?? "bg-muted";
                  return (
                    <div
                      key={s.id}
                      className="flex items-center gap-4 px-4 py-2.5"
                    >
                      <span
                        className={cn(
                          "inline-block h-2 w-2 rounded-full flex-shrink-0",
                          dotCls,
                        )}
                        title={s.status}
                      />
                      <span className="w-48 truncate text-xs font-medium">
                        {s.name}
                      </span>
                      <span className="w-56 truncate font-mono text-xs text-muted-foreground">
                        {s.host}:{s.port}
                      </span>
                      <span className="w-40 truncate text-xs text-muted-foreground">
                        {group?.name ?? "ungrouped"}
                      </span>
                      <span className="w-20 text-xs text-muted-foreground">
                        {s.driver}
                      </span>
                      <span className="flex-1 text-right text-xs text-muted-foreground">
                        {!s.agent_approved
                          ? "pending approval"
                          : s.last_health_check_at
                            ? `checked ${new Date(s.last_health_check_at).toLocaleTimeString()}`
                            : "never checked"}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          </section>
        )}

        {/* ── Recent activity ───────────────────────────────────────────── */}
        {recent && recent.items.length > 0 && (
          <section>
            <SectionHeader
              title="Recent Activity"
              icon={ClipboardList}
              hint={`Latest ${recent.items.length} of ${recent.total}`}
            />
            <div className="rounded-lg border">
              <div className="divide-y">
                {recent.items.map((entry) => {
                  const Icon =
                    entry.result === "failed"
                      ? AlertCircle
                      : entry.result === "denied"
                        ? AlertTriangle
                        : CheckCircle2;
                  const iconCls =
                    entry.result === "failed"
                      ? "text-red-500"
                      : entry.result === "denied"
                        ? "text-amber-500"
                        : "text-emerald-500";
                  return (
                    <div
                      key={entry.id}
                      className="flex items-center gap-4 px-4 py-2.5"
                    >
                      <Icon className={cn("h-3.5 w-3.5 flex-shrink-0", iconCls)} />
                      <span className="w-24 text-xs text-muted-foreground">
                        {new Date(entry.timestamp).toLocaleTimeString()}
                      </span>
                      <span className="w-32 truncate text-xs font-medium">
                        {entry.user_display_name}
                      </span>
                      <span className="w-20 truncate text-xs uppercase tracking-wider text-muted-foreground">
                        {entry.action}
                      </span>
                      <span className="w-24 truncate text-xs text-muted-foreground">
                        {entry.resource_type}
                      </span>
                      <span className="flex-1 truncate text-xs">
                        {entry.resource_display}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          </section>
        )}

        {/* ── Empty state ───────────────────────────────────────────────── */}
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
