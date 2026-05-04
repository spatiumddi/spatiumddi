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
  Cable,
  Github,
  Hash,
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
  Waypoints,
  Shuffle,
  Trash2,
  Search,
  Calculator,
  Webhook,
  Workflow,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { settingsApi, versionApi } from "@/lib/api";
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

// Network section — grouped under a non-clickable section header
// (mirrors Administration). Devices replaces the old top-level
// "Network" entry; VLANs lifts up from its own top-level slot;
// VRFs / ASNs are real first-class pages from issues #85 / #86.
const networkNav = [
  { label: "ASNs", icon: Hash, to: "/network/asns" },
  { label: "Devices", icon: Cable, to: "/network/devices" },
  { label: "VLANs", icon: RouterIcon, to: "/network/vlans" },
  { label: "VRFs", icon: RouteIcon, to: "/network/vrfs" },
];

const toolsNav = [
  { label: "CIDR Calculator", icon: Calculator, to: "/tools/cidr" },
  { label: "Nmap", icon: Search, to: "/tools/nmap" },
];

const adminIdentityNav = [
  { label: "API Tokens", icon: KeySquare, to: "/admin/api-tokens" },
  { label: "Auth Providers", icon: ShieldCheck, to: "/admin/auth-providers" },
  { label: "Groups", icon: UsersRound, to: "/admin/groups" },
  { label: "Roles", icon: KeyRound, to: "/admin/roles" },
  { label: "Users", icon: Users, to: "/admin/users" },
];

const adminPlatformNav = [
  { label: "Alerts", icon: BellRing, to: "/admin/alerts" },
  { label: "Audit Log", icon: ClipboardList, to: "/admin/audit" },
  { label: "Compliance", icon: ShieldCheck, to: "/admin/compliance" },
  { label: "Custom Fields", icon: Tags, to: "/admin/custom-fields" },
  { label: "Platform Insights", icon: Cpu, to: "/admin/platform-insights" },
  { label: "Settings", icon: Settings, to: "/settings" },
  { label: "Trash", icon: Trash2, to: "/admin/trash" },
  { label: "Webhooks", icon: Webhook, to: "/admin/webhooks" },
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

  // Platform settings drive which integration nav items are visible.
  // The settings endpoint is post-login only, so no need to guard —
  // the sidebar itself doesn't render on the login page.
  const { data: platformSettings } = useQuery({
    queryKey: ["settings"],
    queryFn: settingsApi.get,
    staleTime: 5 * 60 * 1000,
  });

  // Integrations live in their own sidebar section, rendered between
  // the main nav and the admin nav, but only when at least one
  // integration is enabled. Each integration contributes one entry
  // — kept declarative so adding a future integration is a one-line
  // extension here and a toggle on PlatformSettings.
  // Sorted alphabetically by label so the order is stable regardless
  // of the order we added integrations here — adding a new one later
  // shouldn't re-shuffle the sidebar for operators already using it.
  const integrationsNav = [
    ...(platformSettings?.integration_kubernetes_enabled
      ? [{ label: "Kubernetes", icon: Boxes, to: "/kubernetes" }]
      : []),
    ...(platformSettings?.integration_docker_enabled
      ? [{ label: "Docker", icon: ContainerIcon, to: "/docker" }]
      : []),
    ...(platformSettings?.integration_proxmox_enabled
      ? [{ label: "Proxmox", icon: HardDrive, to: "/proxmox" }]
      : []),
    ...(platformSettings?.integration_tailscale_enabled
      ? [{ label: "Tailscale", icon: Waypoints, to: "/tailscale" }]
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

          <NavSection
            label="Network"
            storageKey="sidebar-section-network-open"
            collapsed={effectiveCollapsed}
            showDivider
          >
            {networkNav.map((item) => (
              <NavItem
                key={item.to}
                {...item}
                collapsed={effectiveCollapsed}
                onNavigate={mobileOpen ? onMobileClose : undefined}
              />
            ))}
          </NavSection>

          <NavSection
            label="Tools"
            storageKey="sidebar-section-tools-open"
            collapsed={effectiveCollapsed}
            showDivider
          >
            {toolsNav.map((item) => (
              <NavItem
                key={item.to}
                {...item}
                collapsed={effectiveCollapsed}
                onNavigate={mobileOpen ? onMobileClose : undefined}
              />
            ))}
          </NavSection>

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
            {adminIdentityNav.map((item) => (
              <NavItem
                key={item.to}
                {...item}
                collapsed={effectiveCollapsed}
                onNavigate={mobileOpen ? onMobileClose : undefined}
              />
            ))}
            <div className="my-1 border-t border-sidebar-border/60" />
            {adminPlatformNav.map((item) => (
              <NavItem
                key={item.to}
                {...item}
                collapsed={effectiveCollapsed}
                onNavigate={mobileOpen ? onMobileClose : undefined}
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
