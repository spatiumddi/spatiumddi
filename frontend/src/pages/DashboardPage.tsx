import { useState } from "react";
import { useQuery, useQueries, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  Ban,
  Boxes,
  Check,
  Container as ContainerIcon,
  Cpu,
  FileText,
  Globe,
  Globe2,
  HardDrive,
  Hash,
  Layers,
  Network,
  Plug,
  RefreshCw,
  Route,
  Server,
  Shield,
  Waypoints,
} from "lucide-react";
import {
  ipamApi,
  dnsApi,
  dhcpApi,
  natApi,
  auditApi,
  settingsApi,
  kubernetesApi,
  dockerApi,
  proxmoxApi,
  tailscaleApi,
  platformHealthApi,
  asnsApi,
  vrfsApi,
  domainsApi,
  type Subnet,
  type DNSServer,
  type DHCPServer,
  type DHCPServerGroup,
  type KubernetesCluster,
  type DockerHost,
  type ProxmoxNode,
  type TailscaleTenant,
  type PlatformHealthResponse,
  type PlatformHealthStatus,
  type ASNRead,
  type VRF,
  type Domain,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { includeInUtilization } from "@/lib/utilization";
import { DHCPTrafficCard, DNSQueryRateCard } from "@/components/MetricsCharts";

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
 * Two time-series cards under the activity row render DNS query rate
 * (BIND9 statistics-channels) and DHCP traffic (Kea statistic-get-all)
 * from the per-server `metric_sample` tables — empty when no agent
 * has reported yet.
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

// ── Status chip ─────────────────────────────────────────────────────────────

function StatusChip({
  tone,
  label,
}: {
  tone: "green" | "amber" | "red" | "gray";
  label: string;
}) {
  const cls =
    tone === "green"
      ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-400"
      : tone === "amber"
        ? "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-400"
        : tone === "red"
          ? "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-400"
          : "bg-muted text-muted-foreground";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold",
        cls,
      )}
    >
      {label}
    </span>
  );
}

// ── ASN Summary card ─────────────────────────────────────────────────────────

function AsnSummaryCard() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["asns-summary"],
    queryFn: () => asnsApi.list({ limit: 200 }),
    staleTime: 30_000,
  });

  const inner = (() => {
    if (isLoading) {
      return (
        <div className="mt-3 space-y-2">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="h-4 animate-pulse rounded bg-muted"
              style={{ width: `${60 + i * 10}%` }}
            />
          ))}
        </div>
      );
    }
    if (isError) {
      return (
        <p className="mt-3 text-xs text-red-600 dark:text-red-400">
          Failed to load ASN data.
        </p>
      );
    }
    const asns: ASNRead[] = data?.items ?? [];
    if (asns.length === 0) {
      return (
        <div className="mt-3 flex-1 flex flex-col justify-between">
          <p className="text-xs text-muted-foreground">No ASNs configured.</p>
          <Link
            to="/network/asns"
            className="mt-2 text-[11px] text-primary hover:underline"
          >
            Add one →
          </Link>
        </div>
      );
    }
    const publicCount = asns.filter((a) => a.kind === "public").length;
    const privateCount = asns.filter((a) => a.kind === "private").length;
    const whoisOk = asns.filter((a) => a.whois_state === "ok").length;
    const whoisUnreachable = asns.filter(
      (a) => a.whois_state === "unreachable",
    ).length;

    // RPKI ROAs — the field is optional (may not exist in all deployments)
    const allRoas: { state: string }[] = asns.flatMap(
      (a) => ((a as ASNRead & { rpki_roas?: { state: string }[] }).rpki_roas ?? []),
    );
    const roasExpiring = allRoas.filter((r) => r.state === "expiring").length;
    const roasExpired = allRoas.filter((r) => r.state === "expired").length;

    return (
      <div className="mt-3 flex-1 flex flex-col justify-between gap-2">
        <div className="space-y-1.5">
          <p className="text-xs text-muted-foreground">
            <span className="font-medium text-foreground">{publicCount}</span>{" "}
            public,{" "}
            <span className="font-medium text-foreground">{privateCount}</span>{" "}
            private
          </p>
          <div className="flex flex-wrap items-center gap-1.5">
            {whoisOk > 0 && (
              <span className="flex items-center gap-1 text-[11px]">
                <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500" />
                <span className="text-emerald-700 dark:text-emerald-400">
                  {whoisOk} ok
                </span>
              </span>
            )}
            {whoisUnreachable > 0 && (
              <span className="flex items-center gap-1 text-[11px]">
                <span className="inline-block h-1.5 w-1.5 rounded-full bg-red-500" />
                <span className="text-red-600 dark:text-red-400">
                  {whoisUnreachable} unreachable
                </span>
              </span>
            )}
          </div>
          {(roasExpiring > 0 || roasExpired > 0) && (
            <div className="flex flex-wrap gap-1">
              {roasExpiring > 0 && (
                <StatusChip
                  tone="amber"
                  label={`${roasExpiring} ROA${roasExpiring === 1 ? "" : "s"} expiring`}
                />
              )}
              {roasExpired > 0 && (
                <StatusChip
                  tone="red"
                  label={`${roasExpired} ROA${roasExpired === 1 ? "" : "s"} expired`}
                />
              )}
            </div>
          )}
        </div>
        <Link
          to="/network/asns"
          className="text-[11px] text-muted-foreground hover:text-foreground"
        >
          View all →
        </Link>
      </div>
    );
  })();

  return (
    <div className="rounded-lg border bg-card p-4 flex flex-col">
      <div className="flex items-center justify-between">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          ASNs
        </p>
        <Hash className="h-3.5 w-3.5 text-muted-foreground" />
      </div>
      {!isLoading && !isError && (data?.items?.length ?? 0) > 0 && (
        <p className="mt-1.5 text-2xl font-bold tabular-nums">
          {data?.total ?? data?.items?.length ?? 0}
        </p>
      )}
      {inner}
    </div>
  );
}

// ── VRF Summary card ─────────────────────────────────────────────────────────

function VrfSummaryCard() {
  const { data: vrfs = [], isLoading, isError } = useQuery<VRF[]>({
    queryKey: ["vrfs-summary"],
    queryFn: () => vrfsApi.list(),
    staleTime: 30_000,
  });

  const inner = (() => {
    if (isLoading) {
      return (
        <div className="mt-3 space-y-2">
          {[1, 2].map((i) => (
            <div
              key={i}
              className="h-4 animate-pulse rounded bg-muted"
              style={{ width: `${55 + i * 15}%` }}
            />
          ))}
        </div>
      );
    }
    if (isError) {
      return (
        <p className="mt-3 text-xs text-red-600 dark:text-red-400">
          Failed to load VRF data.
        </p>
      );
    }
    if (vrfs.length === 0) {
      return (
        <div className="mt-3 flex-1 flex flex-col justify-between">
          <p className="text-xs text-muted-foreground">No VRFs configured.</p>
          <Link
            to="/network/vrfs"
            className="mt-2 text-[11px] text-primary hover:underline"
          >
            Add one →
          </Link>
        </div>
      );
    }
    const missingRd = vrfs.filter(
      (v) => !v.route_distinguisher || v.route_distinguisher.trim() === "",
    ).length;
    const unlinked = vrfs.filter((v) => v.asn_id === null).length;

    return (
      <div className="mt-3 flex-1 flex flex-col justify-between gap-2">
        <div className="flex flex-wrap gap-1">
          {missingRd > 0 && (
            <StatusChip
              tone="amber"
              label={`${missingRd} missing RD`}
            />
          )}
          {unlinked > 0 && (
            <StatusChip
              tone="gray"
              label={`${unlinked} unlinked (no ASN)`}
            />
          )}
          {missingRd === 0 && unlinked === 0 && (
            <StatusChip tone="green" label="all linked" />
          )}
        </div>
        <Link
          to="/network/vrfs"
          className="text-[11px] text-muted-foreground hover:text-foreground"
        >
          View all →
        </Link>
      </div>
    );
  })();

  return (
    <div className="rounded-lg border bg-card p-4 flex flex-col">
      <div className="flex items-center justify-between">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          VRFs
        </p>
        <Route className="h-3.5 w-3.5 text-muted-foreground" />
      </div>
      {!isLoading && !isError && vrfs.length > 0 && (
        <p className="mt-1.5 text-2xl font-bold tabular-nums">{vrfs.length}</p>
      )}
      {inner}
    </div>
  );
}

// ── Domains Summary card ──────────────────────────────────────────────────────

function DomainsSummaryCard() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["domains-summary"],
    queryFn: () => domainsApi.list({ page_size: 200 }),
    staleTime: 30_000,
  });

  const inner = (() => {
    if (isLoading) {
      return (
        <div className="mt-3 space-y-2">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="h-4 animate-pulse rounded bg-muted"
              style={{ width: `${50 + i * 12}%` }}
            />
          ))}
        </div>
      );
    }
    if (isError) {
      return (
        <p className="mt-3 text-xs text-red-600 dark:text-red-400">
          Failed to load domain data.
        </p>
      );
    }
    const domains: Domain[] = data?.items ?? [];
    if (domains.length === 0) {
      return (
        <div className="mt-3 flex-1 flex flex-col justify-between">
          <p className="text-xs text-muted-foreground">No domains configured.</p>
          <Link
            to="/admin/domains"
            className="mt-2 text-[11px] text-primary hover:underline"
          >
            Add one →
          </Link>
        </div>
      );
    }

    const now = Date.now();
    const thirtyDaysMs = 30 * 24 * 60 * 60 * 1000;
    const expired = domains.filter(
      (d) => d.expires_at && new Date(d.expires_at).getTime() < now,
    ).length;
    const expiringSoon = domains.filter((d) => {
      if (!d.expires_at) return false;
      const exp = new Date(d.expires_at).getTime();
      return exp >= now && exp - now < thirtyDaysMs;
    }).length;
    const healthy = domains.length - expired - expiringSoon;
    const driftCount = domains.filter((d) => d.nameserver_drift).length;

    return (
      <div className="mt-3 flex-1 flex flex-col justify-between gap-2">
        <div className="space-y-1.5">
          <div className="flex flex-wrap gap-1">
            {expired > 0 && (
              <StatusChip
                tone="red"
                label={`${expired} expired`}
              />
            )}
            {expiringSoon > 0 && (
              <StatusChip
                tone="amber"
                label={`${expiringSoon} expiring soon`}
              />
            )}
            {healthy > 0 && (
              <StatusChip tone="green" label={`${healthy} healthy`} />
            )}
          </div>
          {driftCount > 0 && (
            <div>
              <StatusChip
                tone="amber"
                label={`${driftCount} NS drift detected`}
              />
            </div>
          )}
        </div>
        <Link
          to="/admin/domains"
          className="text-[11px] text-muted-foreground hover:text-foreground"
        >
          View all →
        </Link>
      </div>
    );
  })();

  return (
    <div className="rounded-lg border bg-card p-4 flex flex-col">
      <div className="flex items-center justify-between">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Domains
        </p>
        <Globe className="h-3.5 w-3.5 text-muted-foreground" />
      </div>
      {!isLoading && !isError && (data?.items?.length ?? 0) > 0 && (
        <p className="mt-1.5 text-2xl font-bold tabular-nums">
          {data?.total ?? data?.items?.length ?? 0}
        </p>
      )}
      {inner}
    </div>
  );
}

// ── Page ────────────────────────────────────────────────────────────────────

type DashboardTab = "overview" | "ipam" | "dns" | "dhcp";

export function DashboardPage() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<DashboardTab>(() => {
    const saved = localStorage.getItem("dashboard-tab");
    if (saved === "ipam" || saved === "dns" || saved === "dhcp") return saved;
    return "overview";
  });
  function selectTab(next: DashboardTab) {
    setTab(next);
    localStorage.setItem("dashboard-tab", next);
  }

  // IPAM
  const { data: spaces } = useQuery({
    queryKey: ["spaces"],
    queryFn: ipamApi.listSpaces,
  });
  const { data: subnets } = useQuery({
    queryKey: ["subnets"],
    queryFn: () => ipamApi.listSubnets(),
  });

  // NAT mapping count — cheap (single page=1, per_page=1 just for total).
  const { data: natTotal } = useQuery({
    queryKey: ["nat-mappings", "count"],
    queryFn: () => natApi.list({ page: 1, per_page: 1 }).then((r) => r.total),
    staleTime: 60_000,
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

  // Integrations — only fetched when the corresponding toggle is on,
  // so default deployments don't pay for the queries.
  const kubernetesEnabled = settings?.integration_kubernetes_enabled ?? false;
  const dockerEnabled = settings?.integration_docker_enabled ?? false;
  const proxmoxEnabled = settings?.integration_proxmox_enabled ?? false;
  const tailscaleEnabled = settings?.integration_tailscale_enabled ?? false;
  const { data: k8sClusters = [] } = useQuery<KubernetesCluster[]>({
    queryKey: ["kubernetes-clusters"],
    queryFn: kubernetesApi.listClusters,
    enabled: kubernetesEnabled,
    refetchInterval: 30_000,
  });
  const { data: dockerHosts = [] } = useQuery<DockerHost[]>({
    queryKey: ["docker-hosts"],
    queryFn: dockerApi.listHosts,
    enabled: dockerEnabled,
    refetchInterval: 30_000,
  });
  const { data: tailscaleTenants = [] } = useQuery<TailscaleTenant[]>({
    queryKey: ["tailscale-tenants"],
    queryFn: tailscaleApi.listTenants,
    enabled: tailscaleEnabled,
  });

  const { data: proxmoxNodes = [] } = useQuery<ProxmoxNode[]>({
    queryKey: ["proxmox-nodes"],
    queryFn: proxmoxApi.listNodes,
    enabled: proxmoxEnabled,
    refetchInterval: 30_000,
  });

  // Platform health — covers api / postgres / redis / celery workers /
  // celery beat. Unlike DNS/DHCP server health (which is user-managed),
  // these are the pieces *we* ship, so surfacing their liveness makes
  // the dashboard a one-stop check for "is the control plane healthy".
  const { data: platformHealth } = useQuery<PlatformHealthResponse>({
    queryKey: ["platform-health"],
    queryFn: platformHealthApi.get,
    refetchInterval: 30_000,
  });

  // Derived stats — every utilization-driven counter reads from
  // `reportSubnets` so small PTP / loopback subnets don't skew the
  // dashboard. `subnets` (the unfiltered list) is still used for
  // inventory counts like "N subnets".
  const reporting = reportSubnets ?? [];
  // IPv6 subnets (typically /64) carry 2^64 hosts each — counting them in
  // "free addresses" or overall utilization makes the headline numbers
  // meaningless (a single /64 swamps every IPv4 subnet combined). Restrict
  // the top-line counters to IPv4. The heatmap + per-subnet stats keep
  // IPv6 since per-subnet utilization_percent is still meaningful.
  const reportingV4 = reporting.filter((s) => !s.network.includes(":"));
  const reportingV6 = reporting.filter((s) => s.network.includes(":"));
  const totalIPs = reportingV4.reduce((s, n) => s + n.total_ips, 0);
  const allocatedIPs = reportingV4.reduce((s, n) => s + n.allocated_ips, 0);
  const freeIPs = totalIPs - allocatedIPs;
  const overallUtil = totalIPs > 0 ? (allocatedIPs / totalIPs) * 100 : 0;
  // IPv6 allocation count is meaningful per-subnet but the totals don't
  // make sense (a /64 has 2^64 hosts); track subnet count + alloc count
  // separately so the IPv4 vs IPv6 split panel can show both dimensions.
  const v6SubnetCount = reportingV6.length;
  const v6AllocCount = reportingV6.reduce((s, n) => s + n.allocated_ips, 0);
  const v4SubnetCount = reportingV4.length;
  const sortedSubnets = [...reporting]
    .filter((s) => s.total_ips > 0)
    .sort((a, b) => b.utilization_percent - a.utilization_percent);
  const topSubnets = sortedSubnets.slice(0, 6);
  const ipamTopSubnets = sortedSubnets.slice(0, 20);
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
            <button
              type="button"
              onClick={() => {
                for (const key of [
                  ["spaces"],
                  ["subnets"],
                  ["settings"],
                  ["dns-groups"],
                  ["dns-zones"],
                  ["dns-servers"],
                  ["dhcp-servers"],
                  ["dhcp-groups"],
                  ["audit", "recent"],
                  ["metrics"],
                  ["nat-mappings", "count"],
                  ["platform-health"],
                ]) {
                  qc.invalidateQueries({ queryKey: key });
                }
              }}
              title="Reload every panel on the dashboard — IPAM, DNS, DHCP, activity feed, metrics charts."
              className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-accent"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              Refresh
            </button>
          </div>
        </div>

        {/* ── Tab bar ─────────────────────────────────────────────────
            Sub-tabs scope the dashboard to one subsystem at a time.
            Overview keeps the headline KPI grid + heatmap + activity
            feed + platform health; the per-subsystem tabs surface the
            subsystem-scoped panels (charts, server lists, integration
            status) without overcrowding the home view. */}
        <div className="border-b">
          <div className="flex gap-1">
            {(
              [
                { key: "overview", label: "Overview", Icon: Activity },
                { key: "ipam", label: "IPAM", Icon: Network },
                { key: "dns", label: "DNS", Icon: Globe2 },
                { key: "dhcp", label: "DHCP", Icon: Server },
              ] as const
            ).map(({ key, label, Icon }) => (
              <button
                key={key}
                type="button"
                onClick={() => selectTab(key)}
                className={cn(
                  "inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium -mb-px transition-colors",
                  tab === key
                    ? "border-primary text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                <Icon className="h-3.5 w-3.5" />
                {label}
              </button>
            ))}
          </div>
        </div>

        {/* ── KPI grid (always visible — same data, different lens) ─── */}
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
            label="Allocated IPs (IPv4)"
            value={allocatedIPs.toLocaleString()}
            sub={`${freeIPs.toLocaleString()} free`}
            icon={Activity}
            to="/ipam"
          />
          <KpiCard
            label="Utilization (IPv4)"
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

        {/* ── Network overview cards (Overview tab) ─────────────────── */}
        {tab === "overview" && (
          <div className="grid gap-3 grid-cols-1 sm:grid-cols-3">
            <AsnSummaryCard />
            <VrfSummaryCard />
            <DomainsSummaryCard />
          </div>
        )}

        {/* ── DNS query rate (DNS tab only) ──────────────────────────── */}
        {tab === "dns" && <DNSQueryRateCard dnsServers={allDnsServers} />}

        {/* ── DHCP traffic (DHCP tab only) ───────────────────────────── */}
        {tab === "dhcp" && <DHCPTrafficCard dhcpServers={dhcpServers} />}

        {/* ── Heatmap (Overview + IPAM) ──────────────────────────────── */}
        {(tab === "overview" || tab === "ipam") && (
          <SubnetHeatmap subnets={reporting} />
        )}

        {/* ── IPAM-specific summary cards ───────────────────────────── */}
        {tab === "ipam" && (
          <div className="grid gap-3 grid-cols-2 md:grid-cols-3">
            {/* IPv4 vs IPv6 split — counts only; v6 host counts are
                meaningless. */}
            <div className="rounded-lg border bg-card p-4">
              <div className="flex items-center justify-between">
                <span className="text-xs uppercase tracking-wide text-muted-foreground">
                  IPv4 / IPv6 split
                </span>
                <Layers className="h-4 w-4 text-muted-foreground" />
              </div>
              <div className="mt-3 space-y-2">
                <div>
                  <div className="flex items-center justify-between text-xs">
                    <span className="font-mono">IPv4</span>
                    <span className="text-muted-foreground">
                      {v4SubnetCount} subnet
                      {v4SubnetCount === 1 ? "" : "s"} ·{" "}
                      {allocatedIPs.toLocaleString()} alloc
                    </span>
                  </div>
                  <div className="mt-1 h-2 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full bg-blue-500"
                      style={{
                        width: `${
                          v4SubnetCount + v6SubnetCount === 0
                            ? 0
                            : (v4SubnetCount /
                                (v4SubnetCount + v6SubnetCount)) *
                              100
                        }%`,
                      }}
                    />
                  </div>
                </div>
                <div>
                  <div className="flex items-center justify-between text-xs">
                    <span className="font-mono">IPv6</span>
                    <span className="text-muted-foreground">
                      {v6SubnetCount} subnet
                      {v6SubnetCount === 1 ? "" : "s"} ·{" "}
                      {v6AllocCount.toLocaleString()} alloc
                    </span>
                  </div>
                  <div className="mt-1 h-2 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full bg-purple-500"
                      style={{
                        width: `${
                          v4SubnetCount + v6SubnetCount === 0
                            ? 0
                            : (v6SubnetCount /
                                (v4SubnetCount + v6SubnetCount)) *
                              100
                        }%`,
                      }}
                    />
                  </div>
                </div>
              </div>
            </div>

            <KpiCard
              label="NAT mappings"
              value={natTotal ?? "—"}
              sub={natTotal === 0 ? "none configured" : "operator-curated"}
              icon={Plug}
              to="/ipam/nat"
            />

            <KpiCard
              label="Capacity headroom"
              value={`${(100 - overallUtil).toFixed(1)}%`}
              sub={`${freeIPs.toLocaleString()} IPv4 free`}
              icon={HardDrive}
              tone={
                overallUtil >= 95 ? "bad" : overallUtil >= 80 ? "warn" : "good"
              }
            />
          </div>
        )}

        {/* ── Overview: Top subnets (compact) + Live activity ─────────── */}
        {tab === "overview" && (
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
        )}

        {/* ── IPAM tab: Top Subnets (extended list) ──────────────────── */}
        {tab === "ipam" && (
          <div className="rounded-lg border bg-card">
            <div className="flex items-center justify-between border-b px-4 py-2.5">
              <div className="flex items-center gap-2">
                <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500" />
                <h3 className="text-xs font-semibold uppercase tracking-wider">
                  Top Subnets by Utilization
                </h3>
              </div>
              <span className="text-[11px] text-muted-foreground">
                Showing {ipamTopSubnets.length} of {subnets?.length ?? 0}
              </span>
            </div>
            <div className="divide-y">
              {ipamTopSubnets.length === 0 ? (
                <div className="px-4 py-8 text-center text-xs text-muted-foreground">
                  No subnets have allocated IPs yet.
                </div>
              ) : (
                ipamTopSubnets.map((subnet) => (
                  <Link
                    key={subnet.id}
                    to={`/ipam?subnet=${subnet.id}`}
                    className="flex items-center gap-4 px-4 py-2.5 transition-colors hover:bg-accent/40"
                  >
                    <span className="w-32 flex-shrink-0 font-mono text-xs">
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
                    <span className="w-24 text-right text-[11px] tabular-nums text-muted-foreground">
                      {subnet.allocated_ips} / {subnet.total_ips}
                    </span>
                  </Link>
                ))
              )}
            </div>
          </div>
        )}

        {/* ── Platform health (Overview only) ────────────────────────── */}
        {tab === "overview" && platformHealth && (
          <PlatformHealthCard health={platformHealth} />
        )}

        {/* ── DNS tab: Server list ──────────────────────────────────── */}
        {tab === "dns" && allDnsServers.length > 0 && (
          <div className="rounded-lg border bg-card">
            <div className="flex items-center justify-between border-b px-4 py-2.5">
              <div className="flex items-center gap-2">
                <Globe2 className="h-3.5 w-3.5 text-muted-foreground" />
                <h3 className="text-xs font-semibold uppercase tracking-wider">
                  DNS Servers ({allDnsServers.length})
                </h3>
                <span className="text-[11px] text-muted-foreground">
                  {totalZones} zone{totalZones === 1 ? "" : "s"} ·{" "}
                  {dnsGroups.length} group{dnsGroups.length === 1 ? "" : "s"}
                </span>
              </div>
            </div>
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
          </div>
        )}
        {tab === "dns" && allDnsServers.length === 0 && (
          <div className="rounded-lg border border-dashed p-10 text-center">
            <Globe2 className="mx-auto mb-3 h-10 w-10 text-muted-foreground/30" />
            <p className="text-sm font-medium">No DNS servers registered</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Register a DNS server to see query rates, zones, and health here.
            </p>
          </div>
        )}

        {/* ── DHCP tab: Server list + HA pairs ──────────────────────── */}
        {tab === "dhcp" && dhcpServers.length > 0 && (
          <div className="rounded-lg border bg-card">
            <div className="flex items-center justify-between border-b px-4 py-2.5">
              <div className="flex items-center gap-2">
                <Server className="h-3.5 w-3.5 text-muted-foreground" />
                <h3 className="text-xs font-semibold uppercase tracking-wider">
                  DHCP Servers ({dhcpServers.length})
                </h3>
                <span className="text-[11px] text-muted-foreground">
                  {dhcpGroups.length} group
                  {dhcpGroups.length === 1 ? "" : "s"}
                  {haGroups.length > 0 &&
                    ` · ${haGroups.length} HA pair${haGroups.length === 1 ? "" : "s"}`}
                </span>
              </div>
            </div>
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
                      !s.agent_approved ? "pending" : (s.status ?? "unknown")
                    }
                    groupName={group?.name ?? "ungrouped"}
                    lastSeen={s.last_health_check_at}
                  />
                );
              })}
            </div>
            {haGroups.length > 0 && (
              <>
                <div className="flex items-center gap-1.5 border-t bg-muted/30 px-4 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
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
        )}
        {tab === "dhcp" && dhcpServers.length === 0 && (
          <div className="rounded-lg border border-dashed p-10 text-center">
            <Server className="mx-auto mb-3 h-10 w-10 text-muted-foreground/30" />
            <p className="text-sm font-medium">No DHCP servers registered</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Register a DHCP server to see lease activity, scopes, and HA state
              here.
            </p>
          </div>
        )}

        {/* ── Integrations panel (IPAM tab — they populate IPAM) ────── */}
        {tab === "ipam" &&
          (kubernetesEnabled ||
            dockerEnabled ||
            proxmoxEnabled ||
            tailscaleEnabled) && (
            <IntegrationsPanel
              kubernetesEnabled={kubernetesEnabled}
              dockerEnabled={dockerEnabled}
              proxmoxEnabled={proxmoxEnabled}
              tailscaleEnabled={tailscaleEnabled}
              clusters={k8sClusters}
              hosts={dockerHosts}
              proxmoxNodes={proxmoxNodes}
              tailscaleTenants={tailscaleTenants}
            />
          )}

        {/* ── Empty state (Overview only) ───────────────────────────── */}
        {tab === "overview" &&
          subnets?.length === 0 &&
          spaces?.length === 0 && (
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

// ── Platform health card ────────────────────────────────────────────────
// One row per control-plane component (api / postgres / redis / celery
// workers / celery beat). Each comes back from /health/platform with a
// green/amber/red status — we render them as a compact inline strip so
// the whole card fits in roughly the height of a single KPI row.
function platformStatusDotCls(status: PlatformHealthStatus): string {
  return status === "ok"
    ? "bg-emerald-500"
    : status === "warn"
      ? "bg-amber-500"
      : "bg-red-500";
}

function prettyComponentName(name: string): string {
  const map: Record<string, string> = {
    api: "API",
    postgres: "PostgreSQL",
    redis: "Redis",
    "celery-workers": "Workers",
    "celery-beat": "Beat",
  };
  return map[name] ?? name;
}

function PlatformHealthCard({ health }: { health: PlatformHealthResponse }) {
  const headlineTone = health.status === "ok" ? "bg-emerald-500" : "bg-red-500";
  return (
    <div className="rounded-lg border bg-card">
      <div className="flex items-center justify-between border-b px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className={cn("h-1.5 w-1.5 rounded-full", headlineTone)} />
          <h3 className="text-xs font-semibold uppercase tracking-wider">
            Platform Health
          </h3>
          <span className="text-[11px] text-muted-foreground">
            {health.status === "ok" ? "all good" : "degraded"}
          </span>
        </div>
      </div>
      <div className="divide-y sm:grid sm:grid-cols-2 sm:divide-y-0 sm:divide-x lg:grid-cols-5">
        {health.components.map((c) => (
          <div
            key={c.name}
            className="flex min-w-0 items-center gap-2 px-4 py-2.5"
            title={
              c.workers && c.workers.length > 0
                ? `${c.detail}\n${c.workers.join("\n")}`
                : c.detail
            }
          >
            <span
              className={cn(
                "h-1.5 w-1.5 flex-shrink-0 rounded-full",
                platformStatusDotCls(c.status),
              )}
            />
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs font-medium">
                {prettyComponentName(c.name)}
              </div>
              <div className="truncate text-[10px] text-muted-foreground">
                {c.detail}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Integrations panel ─────────────────────────────────────────────────────
// One section per enabled integration — each row shows name, endpoint
// hint, sync age, mirrored counts, and a status dot that folds
// last_sync_error + staleness into a single green/amber/red signal.
function integrationDotCls(
  lastSyncedAt: string | null,
  lastSyncError: string | null,
  intervalSeconds: number,
): string {
  if (lastSyncError) return "bg-red-500";
  if (!lastSyncedAt) return "bg-muted-foreground/40";
  const age = (Date.now() - new Date(lastSyncedAt).getTime()) / 1000;
  // Amber when the last sync is older than ~3 intervals — implies the
  // reconcile beat sweep is stalled or the target is unreachable.
  return age > intervalSeconds * 3 ? "bg-amber-500" : "bg-emerald-500";
}

function IntegrationsPanel({
  kubernetesEnabled,
  dockerEnabled,
  proxmoxEnabled,
  tailscaleEnabled,
  clusters,
  hosts,
  proxmoxNodes,
  tailscaleTenants,
}: {
  kubernetesEnabled: boolean;
  dockerEnabled: boolean;
  proxmoxEnabled: boolean;
  tailscaleEnabled: boolean;
  clusters: KubernetesCluster[];
  hosts: DockerHost[];
  proxmoxNodes: ProxmoxNode[];
  tailscaleTenants: TailscaleTenant[];
}) {
  const hasK8s = kubernetesEnabled;
  const hasDocker = dockerEnabled;
  const hasProxmox = proxmoxEnabled;
  const hasTailscale = tailscaleEnabled;
  const cols = [hasK8s, hasDocker, hasProxmox, hasTailscale].filter(
    Boolean,
  ).length;
  const totalTargets =
    clusters.length +
    hosts.length +
    proxmoxNodes.length +
    tailscaleTenants.length;
  return (
    <div className="rounded-lg border bg-card">
      <div className="flex items-center justify-between border-b px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Plug className="h-3.5 w-3.5 text-muted-foreground" />
          <h3 className="text-xs font-semibold uppercase tracking-wider">
            Integrations
          </h3>
          <span className="text-[11px] text-muted-foreground">
            {totalTargets} target
            {totalTargets === 1 ? "" : "s"}
          </span>
        </div>
      </div>
      <div
        className={cn(
          "grid divide-y",
          cols === 2 && "md:grid-cols-2 md:divide-x md:divide-y-0",
          cols === 3 && "md:grid-cols-3 md:divide-x md:divide-y-0",
          cols === 4 && "md:grid-cols-4 md:divide-x md:divide-y-0",
        )}
      >
        {hasK8s && (
          <div className="min-w-0">
            <Link
              to="/kubernetes"
              className="flex items-center gap-1.5 bg-muted/30 px-4 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground hover:bg-muted/50"
            >
              <Boxes className="h-3 w-3" />
              Kubernetes ({clusters.length})
              <span className="ml-auto text-[10px] text-muted-foreground/70">
                view all →
              </span>
            </Link>
            {clusters.length === 0 ? (
              <p className="px-4 py-3 text-[11px] italic text-muted-foreground">
                No clusters registered.
              </p>
            ) : (
              <div className="divide-y">
                {clusters.map((c) => (
                  <IntegrationRow
                    key={c.id}
                    to={`/kubernetes`}
                    name={c.name}
                    subtitle={c.api_server_url}
                    meta={c.node_count != null ? `${c.node_count} nodes` : "—"}
                    lastSyncedAt={c.last_synced_at}
                    lastSyncError={c.last_sync_error}
                    intervalSeconds={c.sync_interval_seconds}
                    enabled={c.enabled}
                  />
                ))}
              </div>
            )}
          </div>
        )}
        {hasDocker && (
          <div className="min-w-0">
            <Link
              to="/docker"
              className="flex items-center gap-1.5 bg-muted/30 px-4 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground hover:bg-muted/50"
            >
              <ContainerIcon className="h-3 w-3" />
              Docker ({hosts.length})
              <span className="ml-auto text-[10px] text-muted-foreground/70">
                view all →
              </span>
            </Link>
            {hosts.length === 0 ? (
              <p className="px-4 py-3 text-[11px] italic text-muted-foreground">
                No hosts registered.
              </p>
            ) : (
              <div className="divide-y">
                {hosts.map((h) => (
                  <IntegrationRow
                    key={h.id}
                    to={`/docker`}
                    name={h.name}
                    subtitle={h.endpoint}
                    meta={
                      h.container_count != null
                        ? `${h.container_count} containers`
                        : "—"
                    }
                    lastSyncedAt={h.last_synced_at}
                    lastSyncError={h.last_sync_error}
                    intervalSeconds={h.sync_interval_seconds}
                    enabled={h.enabled}
                  />
                ))}
              </div>
            )}
          </div>
        )}
        {hasProxmox && (
          <div className="min-w-0">
            <Link
              to="/proxmox"
              className="flex items-center gap-1.5 bg-muted/30 px-4 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground hover:bg-muted/50"
            >
              <HardDrive className="h-3 w-3" />
              Proxmox ({proxmoxNodes.length})
              <span className="ml-auto text-[10px] text-muted-foreground/70">
                view all →
              </span>
            </Link>
            {proxmoxNodes.length === 0 ? (
              <p className="px-4 py-3 text-[11px] italic text-muted-foreground">
                No endpoints registered.
              </p>
            ) : (
              <div className="divide-y">
                {proxmoxNodes.map((p) => (
                  <IntegrationRow
                    key={p.id}
                    to={`/proxmox`}
                    name={p.name}
                    subtitle={`${p.host}:${p.port}`}
                    meta={
                      p.cluster_name
                        ? `${p.cluster_name} (${p.node_count ?? "?"})`
                        : p.node_count != null
                          ? `${p.node_count} node${p.node_count === 1 ? "" : "s"}`
                          : "—"
                    }
                    lastSyncedAt={p.last_synced_at}
                    lastSyncError={p.last_sync_error}
                    intervalSeconds={p.sync_interval_seconds}
                    enabled={p.enabled}
                  />
                ))}
              </div>
            )}
          </div>
        )}
        {hasTailscale && (
          <div className="min-w-0">
            <Link
              to="/tailscale"
              className="flex items-center gap-1.5 bg-muted/30 px-4 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground hover:bg-muted/50"
            >
              <Waypoints className="h-3 w-3" />
              Tailscale ({tailscaleTenants.length})
              <span className="ml-auto text-[10px] text-muted-foreground/70">
                view all →
              </span>
            </Link>
            {tailscaleTenants.length === 0 ? (
              <p className="px-4 py-3 text-[11px] italic text-muted-foreground">
                No tenants registered.
              </p>
            ) : (
              <div className="divide-y">
                {tailscaleTenants.map((t) => (
                  <IntegrationRow
                    key={t.id}
                    to={`/tailscale`}
                    name={t.name}
                    subtitle={t.tailnet_domain ?? `tailnet ${t.tailnet}`}
                    meta={
                      t.device_count != null
                        ? `${t.device_count} device${t.device_count === 1 ? "" : "s"}`
                        : "—"
                    }
                    lastSyncedAt={t.last_synced_at}
                    lastSyncError={t.last_sync_error}
                    intervalSeconds={t.sync_interval_seconds}
                    enabled={t.enabled}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function IntegrationRow({
  to,
  name,
  subtitle,
  meta,
  lastSyncedAt,
  lastSyncError,
  intervalSeconds,
  enabled,
}: {
  to: string;
  name: string;
  subtitle: string;
  meta: string;
  lastSyncedAt: string | null;
  lastSyncError: string | null;
  intervalSeconds: number;
  enabled: boolean;
}) {
  const dotCls = !enabled
    ? "bg-muted-foreground/40"
    : integrationDotCls(lastSyncedAt, lastSyncError, intervalSeconds);
  return (
    <Link
      to={to}
      className="flex items-center gap-3 px-4 py-2 text-[11px] hover:bg-muted/30"
      title={lastSyncError ?? undefined}
    >
      <span className={cn("h-1.5 w-1.5 flex-shrink-0 rounded-full", dotCls)} />
      <span className="w-28 truncate font-semibold" title={name}>
        {name}
      </span>
      <span
        className="w-48 truncate font-mono text-muted-foreground"
        title={subtitle}
      >
        {subtitle}
      </span>
      <span className="w-28 truncate text-muted-foreground" title={meta}>
        {meta}
      </span>
      <span className="ml-auto w-20 flex-shrink-0 text-right text-muted-foreground">
        {!enabled
          ? "disabled"
          : lastSyncedAt
            ? humanTime(lastSyncedAt)
            : "never"}
      </span>
    </Link>
  );
}
