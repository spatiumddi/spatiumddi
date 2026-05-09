import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Network,
  Globe,
  LayoutDashboard,
  Server,
  Router as RouterIcon,
  Route as RouteIcon,
  Briefcase,
  Cable,
  Code2,
  Github,
  Hash,
  MapPin,
  Package,
  Spline,
  Truck,
  Users,
  UsersRound,
  KeyRound,
  KeySquare,
  ClipboardList,
  ChevronsLeft,
  ChevronsRight,
  ChevronDown,
  ChevronRight,
  Settings,
  Tags,
  ShieldCheck,
  ScrollText,
  BellRing,
  Sparkles,
  Boxes,
  Container as ContainerIcon,
  Cpu,
  Earth,
  HardDrive,
  LayoutTemplate,
  Wifi,
  Waypoints,
  Radio,
  Shuffle,
  Trash2,
  Search,
  Calculator,
  Webhook,
  Workflow,
  Monitor,
  ToggleLeft,
  Upload,
  AlertTriangle,
  Database,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { versionApi } from "@/lib/api";
import { useFeatureModules } from "@/hooks/useFeatureModules";
import { useSessionState } from "@/lib/useSessionState";
import logoIcon from "@/assets/logo-icon.svg";

const baseMainNav = [
  { label: "Dashboard", icon: LayoutDashboard, to: "/dashboard" },
  { label: "IPAM", icon: Network, to: "/ipam", end: true },
  { label: "DHCP", icon: Server, to: "/dhcp" },
  { label: "DNS", icon: Globe, to: "/dns", end: true },
  { label: "DNS Pools", icon: Workflow, to: "/dns/pools" },
  { label: "Domains", icon: Earth, to: "/admin/domains" },
  { label: "Logs", icon: ScrollText, to: "/logs" },
  { label: "NAT Mappings", icon: Shuffle, to: "/ipam/nat" },
  { label: "Subnet Planner", icon: Workflow, to: "/ipam/plans" },
];

// Network section — grouped under a collapsible header. As the
// section grew (4 → 8 items, with #94/#95 still to come), a flat
// list got hard to scan, so we sub-group it the same way
// Administration does — ``SubNavLabel`` rows split the contents
// into two themed bunches:
//
// * **Logical** — operator-facing ownership / deliverable rows
//   (Customers / Providers / Services / Sites from #91 + #94).
//   These cross-cut every other resource type; not network entities
//   themselves.
// * **Infrastructure** — the actual network entities (ASNs,
//   Circuits, Devices, VLANs, VRFs).
//
// Each list is alphabetised so new entries slot in without
// reshuffling.
// Each network nav item carries the feature-module id that gates it
// (see ``app.services.feature_modules.MODULES`` on the backend). The
// renderer below filters by ``useFeatureModules.enabled(id)`` so a
// disabled module disappears from the sidebar entirely.
const networkLogicalNav = [
  {
    label: "Customers",
    icon: Briefcase,
    to: "/network/customers",
    module: "network.customer",
  },
  {
    label: "Providers",
    icon: Truck,
    to: "/network/providers",
    module: "network.provider",
  },
  {
    label: "Services",
    icon: Package,
    to: "/network/services",
    module: "network.service",
  },
  {
    label: "Sites",
    icon: MapPin,
    to: "/network/sites",
    module: "network.site",
  },
];
const networkInfrastructureNav = [
  {
    label: "ASNs",
    icon: Hash,
    to: "/network/asns",
    module: "network.asn",
  },
  {
    label: "Circuits",
    icon: Waypoints,
    to: "/network/circuits",
    module: "network.circuit",
  },
  {
    label: "Devices",
    icon: Cable,
    to: "/network/devices",
    module: "network.device",
  },
  {
    label: "Multicast",
    icon: Radio,
    to: "/network/multicast",
    module: "network.multicast",
  },
  {
    label: "Overlays",
    icon: Spline,
    to: "/network/overlays",
    module: "network.overlay",
  },
  {
    label: "VLANs",
    icon: RouterIcon,
    to: "/network/vlans",
    module: "network.vlan",
  },
  {
    label: "VRFs",
    icon: RouteIcon,
    to: "/network/vrfs",
    module: "network.vrf",
  },
];

const toolsNav = [
  { label: "CIDR Calculator", icon: Calculator, to: "/tools/cidr" },
  { label: "Nmap", icon: Search, to: "/tools/nmap", module: "tools.nmap" },
];

const adminIdentityNav = [
  { label: "API Tokens", icon: KeySquare, to: "/admin/api-tokens" },
  { label: "Auth Providers", icon: ShieldCheck, to: "/admin/auth-providers" },
  { label: "Groups", icon: UsersRound, to: "/admin/groups" },
  { label: "Roles", icon: KeyRound, to: "/admin/roles" },
  { label: "Sessions", icon: Monitor, to: "/admin/sessions" },
  { label: "Users", icon: Users, to: "/admin/users" },
];

// Administration → Platform was getting unwieldy at 9-10 items; split it
// into three sub-groups rendered with small non-collapsible labels.
// "Settings" deliberately lives in the sidebar footer (above the
// GitHub link), not in this list — it was getting buried in
// Administration → Configuration as the platform grew, and it's
// the one entry every operator hits often enough that it earns
// dedicated chrome.
const adminConfigurationNav = [
  {
    label: "AI Providers",
    icon: Sparkles,
    to: "/admin/ai/providers",
    module: "ai.copilot",
  },
  {
    label: "AI Prompts",
    icon: Sparkles,
    to: "/admin/ai/prompts",
    module: "ai.copilot",
  },
  {
    label: "AI Tool Catalog",
    icon: Sparkles,
    to: "/admin/ai/tools",
    module: "ai.copilot",
  },
  { label: "Custom Fields", icon: Tags, to: "/admin/custom-fields" },
  // DNS configuration importer (issue #128). Module-gated so
  // operators who don't need the surface can hide it via Settings →
  // Features.
  {
    label: "DNS Import",
    icon: Upload,
    to: "/admin/dns-import",
    module: "dns.import",
  },
  // ``Features`` lives in the footer next to Settings — it's a
  // platform-wide control, not a per-area config, and ops teams expect
  // to find it next to "Settings" rather than buried in the sidebar.
  {
    label: "IPAM Templates",
    icon: LayoutTemplate,
    to: "/admin/ipam/templates",
  },
];

const adminNotificationsNav = [
  { label: "Alerts", icon: BellRing, to: "/admin/alerts" },
  { label: "Webhooks", icon: Webhook, to: "/admin/webhooks" },
];

const adminInsightsNav = [
  { label: "Audit Log", icon: ClipboardList, to: "/admin/audit" },
  // Backup + restore (issue #117 Phase 1a). Sits in the Insights
  // group alongside Trash + Diagnostics — all "platform-state
  // lifecycle" surfaces.
  // Backup admin (issue #117) — also hosts the Factory Reset tab
  // (issue #116). Factory reset doesn't get its own sidebar entry;
  // it lives as a third tab alongside Manual + Destinations.
  { label: "Backup", icon: Database, to: "/admin/backup" },
  { label: "Compliance", icon: ShieldCheck, to: "/admin/compliance" },
  {
    label: "Conformity",
    icon: ShieldCheck,
    to: "/admin/conformity",
    module: "compliance.conformity",
  },
  // Diagnostics → Errors (issue #123). Visible to all admins; the
  // backend enforces superadmin on read, so non-superadmins land on
  // a 403 page. We don't gate the nav entry itself so superadmins
  // discover it.
  {
    label: "Diagnostics",
    icon: AlertTriangle,
    to: "/admin/diagnostics/errors",
  },
  { label: "Platform Insights", icon: Cpu, to: "/admin/platform-insights" },
  { label: "Trash", icon: Trash2, to: "/admin/trash" },
];

// External documentation links — opened in a new tab via
// ``NavExternalItem``. Kept in their own group so the visual
// separator below the in-app entries reads as "you're leaving the
// app". Matches the pattern from issue #96 (preferred ReDoc for
// browsing; Swagger UI for interactive try-it-out).
const adminReferenceNav: {
  label: string;
  icon: React.ElementType;
  href: string;
}[] = [
  { label: "API Docs", icon: Code2, href: "/api/redoc" },
  { label: "API Docs (interactive)", icon: Code2, href: "/api/docs" },
];

function NavSection({
  label,
  storageKey,
  collapsed,
  children,
  showDivider = false,
}: {
  label: string;
  storageKey: string;
  collapsed: boolean;
  children: React.ReactNode;
  showDivider?: boolean;
}) {
  const [open, setOpen] = useSessionState<boolean>(storageKey, true);

  if (collapsed) {
    return (
      <>
        {showDivider && <div className="my-2 border-t border-sidebar-border" />}
        <div className="space-y-1">{children}</div>
      </>
    );
  }

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="group flex w-full items-center gap-1 px-2 py-1 text-xs font-semibold uppercase tracking-wider text-sidebar-muted-foreground/70 hover:text-sidebar-foreground"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-3 w-3 flex-shrink-0" />
        ) : (
          <ChevronRight className="h-3 w-3 flex-shrink-0" />
        )}
        <span>{label}</span>
      </button>
      {open && <div className="space-y-1">{children}</div>}
    </div>
  );
}

// Small non-collapsible label that breaks a long NavSection (like
// Administration → Platform) into themed sub-groups. Lighter weight
// than NavSection — no chevron, no collapse, no own storageKey.
function SubNavLabel({
  label,
  collapsed,
}: {
  label: string;
  collapsed: boolean;
}) {
  if (collapsed) {
    return <div className="my-1 border-t border-sidebar-border/60" />;
  }
  return (
    <div className="px-3 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-sidebar-muted-foreground/50">
      {label}
    </div>
  );
}

function NavItem({
  label,
  icon: Icon,
  to,
  disabled,
  collapsed,
  onNavigate,
  end,
}: {
  label: string;
  icon: React.ElementType;
  to: string;
  disabled?: boolean;
  collapsed: boolean;
  onNavigate?: () => void;
  end?: boolean;
}) {
  return (
    <NavLink
      to={to}
      end={end}
      aria-disabled={disabled}
      tabIndex={disabled ? -1 : undefined}
      title={collapsed ? label : undefined}
      onClick={onNavigate}
      className={({ isActive }) =>
        cn(
          "flex items-center rounded-md px-3 py-2 text-sm font-medium transition-colors",
          collapsed ? "justify-center gap-0" : "gap-3",
          isActive
            ? "bg-sidebar-primary text-sidebar-primary-foreground"
            : "text-sidebar-foreground/70 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
          disabled && "pointer-events-none opacity-40",
        )
      }
    >
      <Icon className="h-4 w-4 flex-shrink-0" />
      {!collapsed && label}
    </NavLink>
  );
}

/** External-link variant of :func:`NavItem`. Renders a plain ``<a>``
 *  rather than a ``NavLink`` so links to non-SPA URLs (FastAPI's
 *  ``/api/redoc`` etc.) leave the React Router tree cleanly. The
 *  visual style mirrors ``NavItem``'s inactive state — there's no
 *  "active" highlighting because we never navigate to it.
 */
function NavExternalItem({
  label,
  icon: Icon,
  href,
  collapsed,
  title,
}: {
  label: string;
  icon: React.ElementType;
  href: string;
  collapsed: boolean;
  /** Override for the hover tooltip — defaults to the label. Used
   *  when the collapsed-rail tooltip should differ from the visible
   *  label (or when an external link wants to advertise it opens
   *  in a new tab). */
  title?: string;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={title ?? (collapsed ? label : `${label} (opens in new tab)`)}
      className={cn(
        "flex items-center rounded-md px-3 py-2 text-sm font-medium transition-colors",
        collapsed ? "justify-center gap-0" : "gap-3",
        "text-sidebar-foreground/70 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
      )}
    >
      <Icon className="h-4 w-4 flex-shrink-0" />
      {!collapsed && label}
    </a>
  );
}

export function Sidebar({
  mobileOpen = false,
  onMobileClose,
}: {
  mobileOpen?: boolean;
  onMobileClose?: () => void;
} = {}) {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("sidebar-collapsed") === "true",
  );
  const location = useLocation();

  // Close the mobile drawer whenever the user navigates.
  useEffect(() => {
    if (mobileOpen) onMobileClose?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  function toggle() {
    setCollapsed((v) => {
      const next = !v;
      localStorage.setItem("sidebar-collapsed", String(next));
      return next;
    });
  }

  // In the mobile drawer, ignore the "collapsed" state — always show labels.
  const effectiveCollapsed = mobileOpen ? false : collapsed;

  // Pull the running version from the backend so the sidebar always
  // reflects the deployed image, not the value baked in at build time.
  // Falls back to ``__APP_VERSION__`` (the build-time stamp) if the
  // API is unreachable — the login screen still renders a version
  // that way. Refresh hourly; release checks are daily so there's
  // nothing to gain from polling faster.
  const { data: versionInfo } = useQuery({
    queryKey: ["version"],
    queryFn: versionApi.get,
    staleTime: 60 * 60 * 1000,
    refetchInterval: 60 * 60 * 1000,
  });
  const displayVersion = versionInfo?.version ?? __APP_VERSION__;
  const updateAvailable = versionInfo?.update_available ?? false;
  const latestVersion = versionInfo?.latest_version ?? null;
  const latestReleaseUrl = versionInfo?.latest_release_url ?? null;

  // Feature-module toggles — disabled modules drop their nav items
  // entirely (drive both the togglable feature surfaces and the
  // integration visibility flags formerly read from PlatformSettings).
  // Loading / error state defaults to "everything visible" so the
  // sidebar never blinks empty on a slow network.
  const { enabled: moduleEnabled } = useFeatureModules();
  const filterByModule = <T extends { module?: string }>(items: T[]): T[] =>
    items.filter((it) => !it.module || moduleEnabled(it.module));

  // Integrations live in their own sidebar section, rendered between
  // the main nav and the admin nav, but only when at least one
  // integration is enabled. Each integration's visibility is gated
  // by its feature_module id (Settings → Features → Integrations);
  // the matching ``PlatformSettings.integration_*_enabled`` columns
  // are kept in lock-step by the toggle endpoint so reconciler tasks
  // don't need to migrate.
  // Sorted alphabetically by label so the order is stable regardless
  // of the order we added integrations here — adding a new one later
  // shouldn't re-shuffle the sidebar for operators already using it.
  const integrationsNav = [
    ...(moduleEnabled("integrations.kubernetes")
      ? [{ label: "Kubernetes", icon: Boxes, to: "/kubernetes" }]
      : []),
    ...(moduleEnabled("integrations.docker")
      ? [{ label: "Docker", icon: ContainerIcon, to: "/docker" }]
      : []),
    ...(moduleEnabled("integrations.proxmox")
      ? [{ label: "Proxmox", icon: HardDrive, to: "/proxmox" }]
      : []),
    ...(moduleEnabled("integrations.tailscale")
      ? [{ label: "Tailscale", icon: Waypoints, to: "/tailscale" }]
      : []),
    ...(moduleEnabled("integrations.unifi")
      ? [{ label: "UniFi", icon: Wifi, to: "/unifi" }]
      : []),
  ].sort((a, b) => a.label.localeCompare(b.label));
  const mainNav = baseMainNav;

  return (
    <>
      {/* Backdrop — click outside to close (mobile only) */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 md:hidden"
          onClick={onMobileClose}
          aria-hidden
        />
      )}
      <aside
        className={cn(
          "flex flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground transition-all duration-200",
          // Desktop: inline, fixed width, always visible (md+).
          "md:flex-shrink-0",
          collapsed ? "md:w-14" : "md:w-56",
          // Mobile: hidden by default, fixed-positioned drawer when open.
          mobileOpen
            ? "fixed inset-y-0 left-0 z-50 w-64 md:static md:z-auto"
            : "hidden md:flex",
        )}
      >
        {/* Logo + mobile close button */}
        <div
          className={cn(
            "flex h-14 items-center border-b border-sidebar-border",
            effectiveCollapsed ? "justify-center px-0" : "gap-2 px-4",
          )}
        >
          <img
            src={logoIcon}
            alt="SpatiumDDI"
            className="h-7 w-7 flex-shrink-0"
          />
          {!effectiveCollapsed && (
            <span className="font-semibold tracking-tight">SpatiumDDI</span>
          )}
          {mobileOpen && (
            <button
              type="button"
              onClick={onMobileClose}
              aria-label="Close navigation"
              className="ml-auto rounded-md p-1 text-sidebar-foreground/70 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground md:hidden"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>

        {/* Nav */}
        <nav className="flex-1 overflow-y-auto p-2 space-y-4">
          <NavSection
            label="Core"
            storageKey="sidebar-section-core-open"
            collapsed={effectiveCollapsed}
          >
            {mainNav.map((item) => (
              <NavItem
                key={item.to}
                {...item}
                collapsed={effectiveCollapsed}
                onNavigate={mobileOpen ? onMobileClose : undefined}
              />
            ))}
          </NavSection>

          {(() => {
            const visibleLogical = filterByModule(networkLogicalNav);
            const visibleInfra = filterByModule(networkInfrastructureNav);
            // Hide the whole Network section if everything inside is
            // disabled — otherwise we'd render an empty header.
            if (visibleLogical.length + visibleInfra.length === 0) return null;
            return (
              <NavSection
                label="Network"
                storageKey="sidebar-section-network-open"
                collapsed={effectiveCollapsed}
                showDivider
              >
                {visibleLogical.length > 0 && (
                  <>
                    <SubNavLabel
                      label="Logical"
                      collapsed={effectiveCollapsed}
                    />
                    {visibleLogical.map((item) => (
                      <NavItem
                        key={item.to}
                        {...item}
                        collapsed={effectiveCollapsed}
                        onNavigate={mobileOpen ? onMobileClose : undefined}
                      />
                    ))}
                  </>
                )}
                {visibleInfra.length > 0 && (
                  <>
                    <SubNavLabel
                      label="Infrastructure"
                      collapsed={effectiveCollapsed}
                    />
                    {visibleInfra.map((item) => (
                      <NavItem
                        key={item.to}
                        {...item}
                        collapsed={effectiveCollapsed}
                        onNavigate={mobileOpen ? onMobileClose : undefined}
                      />
                    ))}
                  </>
                )}
              </NavSection>
            );
          })()}

          {(() => {
            const visibleTools = filterByModule(toolsNav);
            if (visibleTools.length === 0) return null;
            return (
              <NavSection
                label="Tools"
                storageKey="sidebar-section-tools-open"
                collapsed={effectiveCollapsed}
                showDivider
              >
                {visibleTools.map((item) => (
                  <NavItem
                    key={item.to}
                    {...item}
                    collapsed={effectiveCollapsed}
                    onNavigate={mobileOpen ? onMobileClose : undefined}
                  />
                ))}
              </NavSection>
            );
          })()}

          {integrationsNav.length > 0 && (
            <NavSection
              label="Integrations"
              storageKey="sidebar-section-integrations-open"
              collapsed={effectiveCollapsed}
              showDivider
            >
              {integrationsNav.map((item) => (
                <NavItem
                  key={item.to}
                  {...item}
                  collapsed={effectiveCollapsed}
                  onNavigate={mobileOpen ? onMobileClose : undefined}
                />
              ))}
            </NavSection>
          )}

          <NavSection
            label="Administration"
            storageKey="sidebar-section-admin-open"
            collapsed={effectiveCollapsed}
            showDivider
          >
            <SubNavLabel label="Identity" collapsed={effectiveCollapsed} />
            {adminIdentityNav.map((item) => (
              <NavItem
                key={item.to}
                {...item}
                collapsed={effectiveCollapsed}
                onNavigate={mobileOpen ? onMobileClose : undefined}
              />
            ))}
            <SubNavLabel label="Configuration" collapsed={effectiveCollapsed} />
            {filterByModule(adminConfigurationNav).map((item) => (
              <NavItem
                key={item.to}
                {...item}
                collapsed={effectiveCollapsed}
                onNavigate={mobileOpen ? onMobileClose : undefined}
              />
            ))}
            <SubNavLabel label="Notifications" collapsed={effectiveCollapsed} />
            {adminNotificationsNav.map((item) => (
              <NavItem
                key={item.to}
                {...item}
                collapsed={effectiveCollapsed}
                onNavigate={mobileOpen ? onMobileClose : undefined}
              />
            ))}
            <SubNavLabel
              label="Insights & Audit"
              collapsed={effectiveCollapsed}
            />
            {filterByModule(adminInsightsNav).map((item) => (
              <NavItem
                key={item.to}
                {...item}
                collapsed={effectiveCollapsed}
                onNavigate={mobileOpen ? onMobileClose : undefined}
              />
            ))}
            <SubNavLabel label="Reference" collapsed={effectiveCollapsed} />
            {adminReferenceNav.map((item) => (
              <NavExternalItem
                key={item.href}
                {...item}
                collapsed={effectiveCollapsed}
              />
            ))}
            <div className="my-1 border-t border-sidebar-border/60" />
          </NavSection>
        </nav>

        {/* Footer */}
        <div
          className={cn(
            "border-t border-sidebar-border p-2 space-y-1",
            effectiveCollapsed && "flex flex-col items-center",
          )}
        >
          {!effectiveCollapsed && (
            <div className="flex items-center gap-2 px-3 py-1">
              <span className="text-xs font-mono text-sidebar-muted-foreground/80">
                v{displayVersion}
              </span>
              {updateAvailable && latestReleaseUrl && (
                <a
                  href={latestReleaseUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={`Update available: ${latestVersion ?? "newer release"}`}
                  className="inline-flex items-center gap-1 rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 hover:bg-emerald-500/25 dark:text-emerald-400"
                >
                  <Sparkles className="h-3 w-3" />
                  update
                </a>
              )}
            </div>
          )}
          {effectiveCollapsed && updateAvailable && latestReleaseUrl && (
            <a
              href={latestReleaseUrl}
              target="_blank"
              rel="noopener noreferrer"
              title={`Update available: ${latestVersion ?? "newer release"}`}
              className="flex items-center justify-center rounded-md p-2 text-emerald-600 hover:bg-sidebar-accent dark:text-emerald-400"
            >
              <Sparkles className="h-4 w-4" />
            </a>
          )}

          <NavItem
            label="Features & Integrations"
            icon={ToggleLeft}
            to="/admin/features"
            collapsed={effectiveCollapsed}
            onNavigate={mobileOpen ? onMobileClose : undefined}
          />

          <NavItem
            label="Settings"
            icon={Settings}
            to="/settings"
            collapsed={effectiveCollapsed}
            onNavigate={mobileOpen ? onMobileClose : undefined}
          />

          <a
            href="https://github.com/spatiumddi/spatiumddi"
            target="_blank"
            rel="noopener noreferrer"
            title={effectiveCollapsed ? "GitHub" : undefined}
            className={cn(
              "flex items-center rounded-md px-3 py-2 text-sm font-medium text-sidebar-foreground/70 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground transition-colors",
              effectiveCollapsed ? "justify-center" : "gap-3",
            )}
          >
            <Github className="h-4 w-4 flex-shrink-0" />
            {!effectiveCollapsed && "GitHub"}
          </a>

          {/* Collapse toggle — desktop only */}
          <button
            onClick={toggle}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className={cn(
              "hidden md:flex w-full items-center rounded-md px-3 py-2 text-sm text-sidebar-foreground/70 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground transition-colors",
              collapsed ? "justify-center" : "gap-3",
            )}
          >
            {collapsed ? (
              <ChevronsRight className="h-4 w-4 flex-shrink-0" />
            ) : (
              <>
                <ChevronsLeft className="h-4 w-4 flex-shrink-0" />
                <span>Collapse</span>
              </>
            )}
          </button>
        </div>
      </aside>
    </>
  );
}
