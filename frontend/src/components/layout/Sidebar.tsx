import { NavLink } from "react-router-dom";
import { Network, Globe, LayoutDashboard, Server, Clock, Github } from "lucide-react";
import { cn } from "@/lib/utils";
import logoIcon from "@/assets/logo-icon.svg";

const navItems = [
  { label: "Dashboard", icon: LayoutDashboard, to: "/dashboard" },
  { label: "IPAM", icon: Network, to: "/ipam" },
  { label: "DHCP", icon: Server, to: "/dhcp", disabled: true },
  { label: "DNS", icon: Globe, to: "/dns", disabled: true },
  { label: "NTP", icon: Clock, to: "/ntp", disabled: true },
];

export function Sidebar() {
  return (
    <aside className="flex w-56 flex-col border-r bg-card">
      <div className="flex h-14 items-center gap-2 border-b px-4">
        <img src={logoIcon} alt="SpatiumDDI" className="h-7 w-7" />
        <span className="font-semibold tracking-tight">SpatiumDDI</span>
      </div>
      <nav className="flex-1 space-y-1 p-3">
        {navItems.map(({ label, icon: Icon, to, disabled }) => (
          <NavLink
            key={to}
            to={to}
            aria-disabled={disabled}
            tabIndex={disabled ? -1 : undefined}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                isActive
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                disabled && "pointer-events-none opacity-40"
              )
            }
          >
            <Icon className="h-4 w-4" />
            {label}
          </NavLink>
        ))}
      </nav>
      <div className="border-t p-3 space-y-1">
        <div className="px-3 py-1">
          <span className="text-xs font-mono text-muted-foreground/60">v{__APP_VERSION__}</span>
        </div>
        <a
          href="https://github.com/spatiumddi/spatiumddi"
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors"
        >
          <Github className="h-4 w-4" />
          GitHub
        </a>
      </div>
    </aside>
  );
}
