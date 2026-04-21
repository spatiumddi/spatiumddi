import { useQuery, useQueries } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  Ban,
  Check,
  Cpu,
  FileText,
  Globe2,
  Layers,
  Network,
  Server,
  Shield,
} from "lucide-react";
import {
  ipamApi,
  dnsApi,
  dhcpApi,
  auditApi,
  settingsApi,
  type Subnet,
  type DNSServer,
  type DHCPServer,
  type DHCPServerGroup,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { includeInUtilization } from "@/lib/utilization";

/**
 * Dashboard — the home page.
 *
 * Layout:
 *   1. Title row with a live status strip (subnet count + aggregated
 *      health pill) and a "last updated" marker.
 *   2. Six KPI cards — IP Spaces, Subnets, Allocated IPs, Utilization %,
 *      DNS Zones, Servers (DNS + DHCP aggregate).
 *   3. Subnet Utilization Heatmap — every subnet as a colored cell.
 *      The hero element of the page; gives instant at-a-glance signal on
 *      where capacity pressure is.
 *   4. Two-column row — Top Subnets by Utilization (left) + Live
 *      Activity feed (right, audit-log-driven, auto-refreshing).
 *   5. Services panel — all DNS + DHCP servers with status dots.
 *
 * Deliberately *not* included: time-series sparklines / trends / lease
 * rate / DNS query rate. Those require historical snapshots which we
 * don't collect yet. When they land, the six KPI cards gain a chart
 * strip at the bottom without needing layout changes.
 */

// ── Small building blocks ───────────────────────────────────────────────────

type Tone = "default" | "good" | "warn" | "bad" | "info";

const TONE_CLASS: Record<Tone, { value: string; accent: string }> = {
  default: { value: "text-foreground", accent: "bg-muted" },
  good: {
    value: "text-emerald-600 dark:text-emerald-400",
    accent: "bg-emerald-500",
  },
  warn: { value: "text-amber-600 dark:text-amber-400", accent: "bg-amber-500" },
  bad: { value: "text-red-600 dark:text-red-400", accent: "bg-red-500" },
  info: { value: "text-blue-600 dark:text-blue-400", accent: "bg-blue-500" },
};

function KpiCard({
  label,
  value,
  sub,
  icon: Icon,
  tone = "default",
  to,
}: {
  label: string;
  value: string | number;
  sub?: React.ReactNode;
  icon: React.ElementType;
  tone?: Tone;
  to?: string;
}) {
  const cls = TONE_CLASS[tone];
  const inner = (
    <div className="group relative rounded-lg border bg-card p-4 transition-colors hover:bg-accent/40">
      {/* Accent stripe */}
      <div
        className={cn(
          "absolute inset-y-0 left-0 w-0.5 rounded-l-lg",
          cls.accent,
        )}
      />
      <div className="flex items-center justify-between">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          {label}
        </p>
        <Icon className="h-3.5 w-3.5 text-muted-foreground" />
      </div>
      <p className={cn("mt-2 text-2xl font-bold tabular-nums", cls.value)}>
        {value}
      </p>
      {sub && <p className="mt-0.5 text-[11px] text-muted-foreground">{sub}</p>}
    </div>
  );
  return to ? <Link to={to}>{inner}</Link> : inner;
}

function UtilColor(percent: number): string {
  if (percent >= 95) return "bg-red-500";
  if (percent >= 85) return "bg-red-400";
  if (percent >= 70) return "bg-amber-400";
  if (percent >= 50) return "bg-emerald-500";
  if (percent >= 25) return "bg-emerald-400";
  if (percent > 0) return "bg-emerald-300";
  return "bg-muted/60 dark:bg-muted/40";
}

function UtilizationBar({ percent }: { percent: number }) {
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 flex-1 rounded-full bg-muted overflow-hidden">
        <div
          className={cn(
            "h-full rounded-full transition-all",
            UtilColor(percent),
          )}
          style={{ width: `${Math.min(percent, 100)}%` }}
        />
      </div>
      <span className="w-10 text-right text-xs tabular-nums text-muted-foreground">
        {percent.toFixed(0)}%
      </span>
    </div>
  );
}

/**
 * Utilisation heatmap. Every managed subnet is one cell; the cell color
 * is keyed to its `utilization_percent`. Hover fires a native tooltip
 * (title attr) so no portal machinery is needed. Clicking jumps to the
 * IPAM page — keeps the dashboard as a launcher rather than a dead
 * end. Cells stay square at every breakpoint via aspect-square +
 * grid-cols-autofill.
 */
function SubnetHeatmap({ subnets }: { subnets: Subnet[] }) {
  if (subnets.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center">
        <Network className="mx-auto mb-2 h-8 w-8 text-muted-foreground/30" />
        <p className="text-xs text-muted-foreground">
          No subnets yet — create an IP space + subnet to light up the heatmap.
        </p>
      </div>
    );
  }
  const active = subnets.filter((s) => s.total_ips > 0);
  const avg =
    active.length > 0
      ? active.reduce((s, n) => s + n.utilization_percent, 0) / active.length
      : 0;
  const sorted = [...active]
    .map((s) => s.utilization_percent)
    .sort((a, b) => a - b);
  const p95Index = Math.max(0, Math.floor(sorted.length * 0.95) - 1);
  const p95 = sorted.length > 0 ? sorted[p95Index] : 0;
  const hot = active.filter((s) => s.utilization_percent >= 85).length;

  return (
    <div className="rounded-lg border bg-card">
      <div className="flex items-center justify-between border-b px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500" />
          <h3 className="text-xs font-semibold uppercase tracking-wider">
            Subnet Utilization
          </h3>
          <span className="text-[11px] text-muted-foreground">
            / {subnets.length} subnet{subnets.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px]">
          <div className="flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-sm bg-muted/60" />
            <span className="text-muted-foreground">0%</span>
          </div>
          <div className="flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-sm bg-emerald-300" />
            <span className="text-muted-foreground">25</span>
          </div>
          <div className="flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-sm bg-emerald-500" />
            <span className="text-muted-foreground">50</span>
          </div>
          <div className="flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-sm bg-amber-400" />
            <span className="text-muted-foreground">70</span>
          </div>
          <div className="flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-sm bg-red-400" />
            <span className="text-muted-foreground">85</span>
          </div>
          <div className="flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-sm bg-red-500" />
            <span className="text-muted-foreground">100%</span>
          </div>
        </div>
      </div>
      <div className="p-4">
        <div
          className="grid gap-1.5"
          style={{
            gridTemplateColumns: "repeat(auto-fill, minmax(28px, 1fr))",
          }}
        >
          {subnets.map((s) => (
            <Link
              key={s.id}
              to={`/ipam?subnet=${s.id}`}
              title={`${s.network}${s.name ? ` — ${s.name}` : ""}\n${s.utilization_percent.toFixed(1)}% · ${s.allocated_ips} / ${s.total_ips}`}
              className={cn(
                "aspect-square rounded transition-all hover:scale-110 hover:ring-2 hover:ring-primary/40",
                UtilColor(s.utilization_percent),
              )}
            />
          ))}
        </div>
        {active.length > 0 && (
          <div className="mt-4 flex items-center justify-between border-t pt-3 text-[11px] text-muted-foreground">
            <span>// hover a cell to inspect · click to open</span>
            <div className="flex gap-4 tabular-nums">
              <span>
                <span className="text-muted-foreground/70">AVG</span>{" "}
                <span className="font-semibold text-foreground">
                  {avg.toFixed(1)}%
                </span>
              </span>
              <span>
                <span className="text-muted-foreground/70">P95</span>{" "}
                <span className="font-semibold text-foreground">
                  {p95.toFixed(0)}%
                </span>
              </span>
              <span>
                <span className="text-muted-foreground/70">HOT</span>{" "}
                <span
                  className={cn(
                    "font-semibold",
                    hot > 0 ? "text-red-500" : "text-foreground",
                  )}
                >
                  {hot}
                </span>
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Audit-log-driven live activity feed. One row per audit entry, color-
 * coded by action family (create=green, update=blue, delete=red,
 * denied=amber, failed=red, login/logout=purple). Clickable rows link
 * to the audit log page where the user can filter more.
 */
function ActionBadge({ action, result }: { action: string; result: string }) {
  // Map both action and result to one of a few colour families.
  let tone: Tone = "info";
  let label = action.toUpperCase();
  if (result === "failed") {
    tone = "bad";
    label = "FAIL";
  } else if (result === "denied") {
    tone = "warn";
    label = "DENY";
  } else if (action === "create") tone = "good";
  else if (action === "delete") tone = "bad";
  else if (action === "update") tone = "info";
  else if (action === "login" || action === "logout") tone = "info";

  const cls = TONE_CLASS[tone];
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={cn("inline-block h-1.5 w-1.5 rounded-full", cls.accent)}
      />
      <span className={cn("font-semibold tracking-wider", cls.value)}>
        {label}
      </span>
    </span>
  );
}

function humanTime(ts: string): string {
  const d = new Date(ts);
  const now = Date.now();
  const diff = Math.floor((now - d.getTime()) / 1000);
  if (diff < 10) return "just now";
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return d.toLocaleDateString();
}

// ── Page ────────────────────────────────────────────────────────────────────

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

  // Platform settings drive the utilization filter (excludes /30, /31,
  // /127 etc.) — once loaded, derived stats below use `reportSubnets`
  // instead of the raw list.
  const { data: settings } = useQuery({
    queryKey: ["settings"],
    queryFn: settingsApi.get,
    staleTime: 60_000,
  });
  const reportSubnets = subnets?.filter((s) =>
    includeInUtilization(s, settings),
  );

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
  const allDnsServers: DNSServer[] = serverQueries.flatMap((q) => q.data ?? []);

  // DHCP
  const { data: dhcpServers = [] } = useQuery<DHCPServer[]>({
    queryKey: ["dhcp-servers"],
    queryFn: () => dhcpApi.listServers(),
    refetchInterval: 30_000,
  });
  // Single groups fetch — drives both the DHCP server-row group-name
  // lookup and the HA panel (groups with >= 2 Kea members).
  const { data: dhcpGroups = [] } = useQuery({
    queryKey: ["dhcp-groups"],
    queryFn: dhcpApi.listGroups,
    refetchInterval: 30_000,
  });
  const haGroups = dhcpGroups.filter((g) => g.kea_member_count >= 2);

  // Audit
  const { data: recent } = useQuery({
    queryKey: ["audit", "recent"],
    queryFn: () => auditApi.list({ limit: 15, offset: 0 }),
    staleTime: 10_000,
    refetchInterval: 15_000,
  });

  // Derived stats — every utilization-driven counter reads from
  // `reportSubnets` so small PTP / loopback subnets don't skew the
  // dashboard. `subnets` (the unfiltered list) is still used for
  // inventory counts like "N subnets".
  const reporting = reportSubnets ?? [];
  const totalIPs = reporting.reduce((s, n) => s + n.total_ips, 0);
  const allocatedIPs = reporting.reduce((s, n) => s + n.allocated_ips, 0);
  const freeIPs = totalIPs - allocatedIPs;
  const overallUtil = totalIPs > 0 ? (allocatedIPs / totalIPs) * 100 : 0;
  const topSubnets = [...reporting]
    .filter((s) => s.total_ips > 0)
    .sort((a, b) => b.utilization_percent - a.utilization_percent)
    .slice(0, 6);
  const critical = reporting.filter((s) => s.utilization_percent >= 95).length;
  const warning = reporting.filter(
    (s) => s.utilization_percent >= 80 && s.utilization_percent < 95,
  ).length;

  const allServers = [
    ...allDnsServers.map((s) => ({ ...s, kind: "dns" as const })),
    ...dhcpServers.map((s) => ({ ...s, kind: "dhcp" as const })),
  ];
  const unhealthyServers = allServers.filter(
    (s) => s.status === "unreachable" || s.status === "error",
  ).length;
  const activeServers = allServers.filter((s) => s.status === "active").length;

  // Aggregate alerts — shown as a pill next to the title.
  const alertCount = critical + warning + unhealthyServers;
  const healthTone: Tone =
    unhealthyServers > 0 || critical > 0
      ? "bad"
      : warning > 0
        ? "warn"
        : "good";
  const healthLabel =
    unhealthyServers > 0 || critical > 0
      ? "degraded"
      : warning > 0
        ? "near capacity"
        : "healthy";

  return (
    <div className="h-full overflow-auto p-6">
      <div className="mx-auto max-w-[1400px] space-y-5">
        {/* ── Title + status pill ────────────────────────────────────── */}
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
            <p className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
              <span className="font-mono">//</span>
              <span>
                {spaces?.length ?? 0} space{spaces?.length === 1 ? "" : "s"}
              </span>
              <span>·</span>
              <span>
                {subnets?.length ?? 0} subnet{subnets?.length === 1 ? "" : "s"}
              </span>
              <span>·</span>
              <span className="inline-flex items-center gap-1.5">
                <span
                  className={cn(
                    "inline-block h-1.5 w-1.5 rounded-full",
                    TONE_CLASS[healthTone].accent,
                  )}
                />
                <span className={TONE_CLASS[healthTone].value}>
                  {healthLabel}
                </span>
              </span>
            </p>
          </div>
          <div className="flex items-center gap-2">
            {alertCount > 0 && (
              <Link
                to="/ipam"
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium",
                  "border-red-200 bg-red-50 text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400",
                  "hover:bg-red-100 dark:hover:bg-red-950/50",
                )}
              >
                <AlertTriangle className="h-3.5 w-3.5" />
                {alertCount} alert{alertCount === 1 ? "" : "s"} open
              </Link>
            )}
          </div>
        </div>

        {/* ── KPI grid ───────────────────────────────────────────────── */}
        <div className="grid gap-3 grid-cols-2 md:grid-cols-3 lg:grid-cols-6">
          <KpiCard
            label="IP Spaces"
            value={spaces?.length ?? "—"}
            sub={spaces?.[0]?.name?.toUpperCase()}
            icon={Layers}
            to="/ipam"
          />
          <KpiCard
            label="Subnets"
            value={subnets?.length ?? "—"}
            sub={
              <>
                <span className="text-emerald-600 dark:text-emerald-400">
                  {(subnets?.length ?? 0) - critical - warning} healthy
                </span>
                {(critical > 0 || warning > 0) && (
                  <>
                    {" · "}
                    <span className="text-red-600 dark:text-red-400">
                      {critical + warning} alert
                      {critical + warning === 1 ? "" : "s"}
                    </span>
                  </>
                )}
              </>
            }
            icon={Network}
            tone={critical > 0 ? "bad" : warning > 0 ? "warn" : "good"}
            to="/ipam"
          />
          <KpiCard
            label="Allocated IPs"
            value={allocatedIPs.toLocaleString()}
            sub={`${freeIPs.toLocaleString()} free`}
            icon={Activity}
            to="/ipam"
          />
          <KpiCard
            label="Utilization"
            value={`${overallUtil.toFixed(1)}%`}
            sub={`${allocatedIPs.toLocaleString()} / ${totalIPs.toLocaleString()}`}
            icon={Server}
            tone={
              overallUtil >= 95 ? "bad" : overallUtil >= 80 ? "warn" : "default"
            }
          />
          <KpiCard
            label="DNS Zones"
            value={totalZones}
            sub={
              dnsGroups.length > 0
                ? `${dnsGroups.length} group${dnsGroups.length === 1 ? "" : "s"}`
                : "no groups"
            }
            icon={Globe2}
            to="/dns"
          />
          <KpiCard
            label="Servers"
            value={allServers.length}
            sub={
              unhealthyServers > 0 ? (
                <span className="text-red-600 dark:text-red-400">
                  {activeServers} active · {unhealthyServers} unhealthy
                </span>
              ) : allServers.length > 0 ? (
                `${activeServers} active`
              ) : (
                "none registered"
              )
            }
            icon={Cpu}
            tone={unhealthyServers > 0 ? "bad" : "default"}
          />
        </div>

        {/* ── Heatmap (hero) ─────────────────────────────────────────── */}
        <SubnetHeatmap subnets={reporting} />

        {/* ── Top subnets + Live activity ────────────────────────────── */}
        <div className="grid gap-5 lg:grid-cols-2">
          {/* Top Subnets */}
          <div className="rounded-lg border bg-card">
            <div className="flex items-center justify-between border-b px-4 py-2.5">
              <div className="flex items-center gap-2">
                <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500" />
                <h3 className="text-xs font-semibold uppercase tracking-wider">
                  Top Subnets by Utilization
                </h3>
              </div>
              <span className="text-[11px] text-muted-foreground">
                Showing {topSubnets.length} of {subnets?.length ?? 0}
              </span>
            </div>
            <div className="divide-y">
              {topSubnets.length === 0 ? (
                <div className="px-4 py-8 text-center text-xs text-muted-foreground">
                  No subnets have allocated IPs yet.
                </div>
              ) : (
                topSubnets.map((subnet) => (
                  <Link
                    key={subnet.id}
                    to={`/ipam?subnet=${subnet.id}`}
                    className="flex items-center gap-4 px-4 py-2.5 transition-colors hover:bg-accent/40"
                  >
                    <span className="w-32 flex-shrink-0 font-mono text-xs">
                      {subnet.network}
                    </span>
                    <span className="w-32 truncate text-xs text-muted-foreground">
                      {subnet.name || (
                        <span className="text-muted-foreground/40">—</span>
                      )}
                    </span>
                    <div className="flex-1">
                      <UtilizationBar percent={subnet.utilization_percent} />
                    </div>
                    <span className="w-20 text-right text-[11px] tabular-nums text-muted-foreground">
                      {subnet.allocated_ips} / {subnet.total_ips}
                    </span>
                  </Link>
                ))
              )}
            </div>
          </div>

          {/* Live activity */}
          <div className="rounded-lg border bg-card">
            <div className="flex items-center justify-between border-b px-4 py-2.5">
              <div className="flex items-center gap-2">
                <span className="relative inline-flex h-1.5 w-1.5">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
                  <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-500" />
                </span>
                <h3 className="text-xs font-semibold uppercase tracking-wider">
                  Live Activity
                </h3>
              </div>
              <Link
                to="/admin/audit"
                className="text-[11px] text-muted-foreground hover:text-foreground"
              >
                view all →
              </Link>
            </div>
            <div className="divide-y">
              {!recent || recent.items.length === 0 ? (
                <div className="px-4 py-8 text-center text-xs text-muted-foreground">
                  No recent activity. Try creating a subnet or a DNS record.
                </div>
              ) : (
                recent.items.slice(0, 12).map((entry) => (
                  <div
                    key={entry.id}
                    className="flex items-center gap-3 px-4 py-2 text-[11px]"
                  >
                    <span className="w-14 flex-shrink-0 tabular-nums text-muted-foreground">
                      {humanTime(entry.timestamp)}
                    </span>
                    <span className="w-36 flex-shrink-0 min-w-0">
                      <ActionBadge
                        action={entry.action}
                        result={entry.result}
                      />
                    </span>
                    <span className="w-20 flex-shrink-0 truncate text-muted-foreground">
                      {entry.resource_type.replace(/_/g, " ")}
                    </span>
                    <span className="flex-1 truncate font-mono">
                      {entry.resource_display}
                    </span>
                    <span className="w-20 flex-shrink-0 truncate text-right text-muted-foreground">
                      {entry.user_display_name}
                    </span>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>

        {/* ── Services panel ─────────────────────────────────────────── */}
        {allServers.length > 0 && (
          <div className="rounded-lg border bg-card">
            <div className="flex items-center justify-between border-b px-4 py-2.5">
              <div className="flex items-center gap-2">
                <Shield className="h-3.5 w-3.5 text-muted-foreground" />
                <h3 className="text-xs font-semibold uppercase tracking-wider">
                  Services
                </h3>
                <span className="text-[11px] text-muted-foreground">
                  {allServers.length} total · {activeServers} active
                </span>
              </div>
            </div>
            <div className="grid divide-y md:grid-cols-2 md:divide-x md:divide-y-0">
              {/* DNS column */}
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 bg-muted/30 px-4 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  <Globe2 className="h-3 w-3" />
                  DNS ({allDnsServers.length})
                </div>
                {allDnsServers.length === 0 ? (
                  <p className="px-4 py-3 text-[11px] italic text-muted-foreground">
                    No DNS servers registered.
                  </p>
                ) : (
                  <div className="divide-y">
                    {allDnsServers.map((s) => {
                      const group = dnsGroups.find((g) => g.id === s.group_id);
                      return (
                        <ServerRow
                          key={s.id}
                          name={s.name}
                          host={`${s.host}:${s.port}`}
                          driver={s.driver}
                          status={s.status}
                          groupName={group?.name ?? "—"}
                          lastSeen={s.last_health_check_at}
                          isEnabled={s.is_enabled !== false}
                        />
                      );
                    })}
                  </div>
                )}
              </div>
              {/* DHCP column */}
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 bg-muted/30 px-4 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  <Server className="h-3 w-3" />
                  DHCP ({dhcpServers.length})
                </div>
                {dhcpServers.length === 0 ? (
                  <p className="px-4 py-3 text-[11px] italic text-muted-foreground">
                    No DHCP servers registered.
                  </p>
                ) : (
                  <div className="divide-y">
                    {dhcpServers.map((s) => {
                      const group = dhcpGroups.find(
                        (g) => g.id === s.server_group_id,
                      );
                      return (
                        <ServerRow
                          key={s.id}
                          name={s.name}
                          host={`${s.host}:${s.port}`}
                          driver={s.driver}
                          status={
                            !s.agent_approved
                              ? "pending"
                              : (s.status ?? "unknown")
                          }
                          groupName={group?.name ?? "ungrouped"}
                          lastSeen={s.last_health_check_at}
                        />
                      );
                    })}
                  </div>
                )}
                {haGroups.length > 0 && (
                  <>
                    <div className="flex items-center gap-1.5 bg-muted/30 px-4 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                      <Shield className="h-3 w-3" />
                      HA Pairs ({haGroups.length})
                    </div>
                    <div className="divide-y">
                      {haGroups.map((g) => (
                        <FailoverRow key={g.id} group={g} />
                      ))}
                    </div>
                  </>
                )}
              </div>
            </div>
          </div>
        )}

        {/* ── Empty state ────────────────────────────────────────────── */}
        {subnets?.length === 0 && spaces?.length === 0 && (
          <div className="rounded-lg border border-dashed p-10 text-center">
            <FileText className="mx-auto mb-3 h-10 w-10 text-muted-foreground/30" />
            <p className="text-sm font-medium">Welcome to SpatiumDDI</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Head to{" "}
              <Link
                to="/ipam"
                className="text-primary underline underline-offset-2"
              >
                IPAM
              </Link>{" "}
              to create your first IP space + subnet.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

function ServerRow({
  name,
  host,
  driver,
  status,
  groupName,
  lastSeen,
  isEnabled = true,
}: {
  name: string;
  host: string;
  driver: string;
  status: string;
  groupName: string;
  lastSeen?: string | null;
  isEnabled?: boolean;
}) {
  const dotCls =
    status === "active"
      ? "bg-emerald-500"
      : status === "syncing"
        ? "bg-blue-500"
        : status === "pending"
          ? "bg-amber-500"
          : status === "unreachable" || status === "error"
            ? "bg-red-500"
            : "bg-muted-foreground/40";
  const StatusIcon =
    status === "active"
      ? Check
      : status === "unreachable" || status === "error"
        ? Ban
        : Activity;
  return (
    <div className="flex items-center gap-3 px-4 py-2 text-[11px]">
      <span className="relative inline-flex h-2 w-2 flex-shrink-0">
        {status === "active" && isEnabled && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
        )}
        <span
          className={cn(
            "relative inline-flex h-2 w-2 rounded-full",
            isEnabled ? dotCls : "bg-muted-foreground/40",
          )}
          title={isEnabled ? status : "disabled"}
        />
      </span>
      <span className="w-28 truncate font-semibold" title={name}>
        {name}
      </span>
      <span
        className="w-36 truncate font-mono text-muted-foreground"
        title={host}
      >
        {host}
      </span>
      <span className="w-20 truncate text-muted-foreground" title={driver}>
        {driver}
      </span>
      <span className="w-24 truncate text-muted-foreground" title={groupName}>
        {groupName}
      </span>
      <StatusIcon className="ml-auto h-3 w-3 flex-shrink-0 text-muted-foreground/50" />
      <span className="w-20 flex-shrink-0 text-right text-muted-foreground">
        {!isEnabled ? "disabled" : lastSeen ? humanTime(lastSeen) : "never"}
      </span>
    </div>
  );
}

// ── Failover channel row ───────────────────────────────────────────────────
// Kea HA states: normal / hot-standby / load-balancing / ready → green;
// waiting / syncing / communications-interrupted → amber;
// partner-down / terminated → red; null/unknown → muted.
function haStateDotCls(state: string | null | undefined): string {
  if (!state) return "bg-muted-foreground/40";
  if (
    state === "normal" ||
    state === "hot-standby" ||
    state === "load-balancing" ||
    state === "ready"
  )
    return "bg-emerald-500";
  if (state === "partner-down" || state === "terminated") return "bg-red-500";
  return "bg-amber-500";
}

function FailoverRow({ group }: { group: DHCPServerGroup }) {
  // Only Kea members participate in HA. Sort by name for stable display.
  const kea = [...(group.servers ?? [])]
    .filter((s) => s.driver === "kea")
    .sort((a, b) => a.name.localeCompare(b.name));
  return (
    <Link
      to="/dhcp"
      className="flex items-center gap-3 px-4 py-2 text-[11px] hover:bg-muted/30"
    >
      <Shield className="h-3 w-3 flex-shrink-0 text-muted-foreground/60" />
      <span className="w-28 truncate font-semibold" title={group.name}>
        {group.name}
      </span>
      <span className="w-24 truncate text-muted-foreground" title={group.mode}>
        {group.mode}
      </span>
      <span className="ml-auto flex items-center gap-3">
        {kea.map((s) => (
          <span key={s.id} className="flex items-center gap-1">
            <span
              className={cn(
                "inline-block h-2 w-2 rounded-full",
                haStateDotCls(s.ha_state),
              )}
              title={`${s.name}: ${s.ha_state ?? "unknown"}`}
            />
            <span className="text-muted-foreground">
              {s.name}
              <span className="text-muted-foreground/60">
                {" · "}
                {s.ha_state ?? "unknown"}
              </span>
            </span>
          </span>
        ))}
      </span>
    </Link>
  );
}
