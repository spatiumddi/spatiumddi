import { useState } from "react";
import { NavLink } from "react-router-dom";
import {
  Network,
  Globe,
  LayoutDashboard,
  Server,
  Router as RouterIcon,
  Github,
  Users,
  ClipboardList,
  ChevronsLeft,
  ChevronsRight,
  Settings,
  Tags,
} from "lucide-react";
import { cn } from "@/lib/utils";
import logoIcon from "@/assets/logo-icon.svg";

const mainNav = [
  { label: "Dashboard", icon: LayoutDashboard, to: "/dashboard" },
  { label: "IPAM", icon: Network, to: "/ipam" },
  { label: "VLANs", icon: RouterIcon, to: "/vlans" },
  { label: "DHCP", icon: Server, to: "/dhcp", disabled: true },
  { label: "DNS", icon: Globe, to: "/dns" },
];

const adminNav = [
  { label: "Users", icon: Users, to: "/admin/users" },
  { label: "Audit Log", icon: ClipboardList, to: "/admin/audit" },
  { label: "Custom Fields", icon: Tags, to: "/admin/custom-fields" },
  { label: "Settings", icon: Settings, to: "/settings" },
];

function NavItem({
  label,
  icon: Icon,
  to,
  disabled,
  collapsed,
}: {
  label: string;
  icon: React.ElementType;
  to: string;
  disabled?: boolean;
  collapsed: boolean;
}) {
  return (
    <NavLink
      to={to}
      aria-disabled={disabled}
      tabIndex={disabled ? -1 : undefined}
      title={collapsed ? label : undefined}
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

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("sidebar-collapsed") === "true",
  );

  function toggle() {
    setCollapsed((v) => {
      const next = !v;
      localStorage.setItem("sidebar-collapsed", String(next));
      return next;
    });
  }

  return (
    <aside
      className={cn(
        "flex flex-shrink-0 flex-col border-r bg-card transition-all duration-200",
        collapsed ? "w-14" : "w-56",
      )}
    >
      {/* Logo */}
      <div
        className={cn(
          "flex h-14 items-center border-b",
          collapsed ? "justify-center px-0" : "gap-2 px-4",
        )}
      >
        <img
          src={logoIcon}
          alt="SpatiumDDI"
          className="h-7 w-7 flex-shrink-0"
        />
        {!collapsed && (
          <span className="font-semibold tracking-tight">SpatiumDDI</span>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto p-2">
        <div className="space-y-1">
          {mainNav.map((item) => (
            <NavItem key={item.to} {...item} collapsed={collapsed} />
          ))}
        </div>

        <div className="mt-4">
          {!collapsed && (
            <p className="mb-1 px-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground/50">
              Admin
            </p>
          )}
          {collapsed && <div className="my-2 border-t" />}
          <div className="space-y-1">
            {adminNav.map((item) => (
              <NavItem key={item.to} {...item} collapsed={collapsed} />
            ))}
          </div>
        </div>
      </nav>

      {/* Footer */}
      <div
        className={cn(
          "border-t p-2 space-y-1",
          collapsed && "flex flex-col items-center",
        )}
      >
        {!collapsed && (
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
          title={collapsed ? "GitHub" : undefined}
          className={cn(
            "flex items-center rounded-md px-3 py-2 text-sm font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors",
            collapsed ? "justify-center" : "gap-3",
          )}
        >
          <Github className="h-4 w-4 flex-shrink-0" />
          {!collapsed && "GitHub"}
        </a>

        {/* Collapse toggle */}
        <button
          onClick={toggle}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className={cn(
            "flex w-full items-center rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors",
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
  );
}
