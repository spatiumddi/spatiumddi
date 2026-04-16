import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import {
  Network,
  Globe,
  LayoutDashboard,
  Server,
  Router as RouterIcon,
  Github,
  Users,
  UsersRound,
  KeyRound,
  ClipboardList,
  ChevronsLeft,
  ChevronsRight,
  Settings,
  Tags,
  ShieldCheck,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import logoIcon from "@/assets/logo-icon.svg";

const mainNav = [
  { label: "Dashboard", icon: LayoutDashboard, to: "/dashboard" },
  { label: "IPAM", icon: Network, to: "/ipam" },
  { label: "DHCP", icon: Server, to: "/dhcp" },
  { label: "DNS", icon: Globe, to: "/dns" },
  { label: "VLANs", icon: RouterIcon, to: "/vlans" },
];

const adminNav = [
  { label: "Audit Log", icon: ClipboardList, to: "/admin/audit" },
  { label: "Auth Providers", icon: ShieldCheck, to: "/admin/auth-providers" },
  { label: "Custom Fields", icon: Tags, to: "/admin/custom-fields" },
  { label: "Groups", icon: UsersRound, to: "/admin/groups" },
  { label: "Roles", icon: KeyRound, to: "/admin/roles" },
  { label: "Settings", icon: Settings, to: "/settings" },
  { label: "Users", icon: Users, to: "/admin/users" },
];

function NavItem({
  label,
  icon: Icon,
  to,
  disabled,
  collapsed,
  onNavigate,
}: {
  label: string;
  icon: React.ElementType;
  to: string;
  disabled?: boolean;
  collapsed: boolean;
  onNavigate?: () => void;
}) {
  return (
    <NavLink
      to={to}
      aria-disabled={disabled}
      tabIndex={disabled ? -1 : undefined}
      title={collapsed ? label : undefined}
      onClick={onNavigate}
      className={({ isActive }) =>
        cn(
          "flex items-center rounded-md px-3 py-2 text-sm font-medium transition-colors",
          collapsed ? "justify-center gap-0" : "gap-3",
          isActive
            ? "bg-primary text-primary-foreground"
            : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
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
          "flex flex-col border-r bg-card transition-all duration-200",
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
            "flex h-14 items-center border-b",
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
              className="ml-auto rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground md:hidden"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>

        {/* Nav */}
        <nav className="flex-1 overflow-y-auto p-2">
          <div className="space-y-1">
            {mainNav.map((item) => (
              <NavItem
                key={item.to}
                {...item}
                collapsed={effectiveCollapsed}
                onNavigate={mobileOpen ? onMobileClose : undefined}
              />
            ))}
          </div>

          <div className="mt-4">
            {!effectiveCollapsed && (
              <p className="mb-1 px-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground/50">
                Admin
              </p>
            )}
            {effectiveCollapsed && <div className="my-2 border-t" />}
            <div className="space-y-1">
              {adminNav.map((item) => (
                <NavItem
                  key={item.to}
                  {...item}
                  collapsed={effectiveCollapsed}
                  onNavigate={mobileOpen ? onMobileClose : undefined}
                />
              ))}
            </div>
          </div>
        </nav>

        {/* Footer */}
        <div
          className={cn(
            "border-t p-2 space-y-1",
            effectiveCollapsed && "flex flex-col items-center",
          )}
        >
          {!effectiveCollapsed && (
            <div className="px-3 py-1">
              <span className="text-xs font-mono text-muted-foreground/60">
                v{__APP_VERSION__}
              </span>
            </div>
          )}

          <a
            href="https://github.com/spatiumddi/spatiumddi"
            target="_blank"
            rel="noopener noreferrer"
            title={effectiveCollapsed ? "GitHub" : undefined}
            className={cn(
              "flex items-center rounded-md px-3 py-2 text-sm font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors",
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
              "hidden md:flex w-full items-center rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors",
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
