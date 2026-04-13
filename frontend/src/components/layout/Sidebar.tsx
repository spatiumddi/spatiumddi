import { NavLink } from "react-router-dom";
import { Network, Globe, LayoutDashboard, Server, Clock } from "lucide-react";
import { cn } from "@/lib/utils";

const navItems = [
  { label: "Dashboard", icon: LayoutDashboard, to: "/dashboard" },
  { label: "IP Spaces", icon: Network, to: "/ipam/spaces" },
  { label: "Subnets", icon: Network, to: "/ipam/subnets" },
  { label: "DHCP", icon: Server, to: "/dhcp", disabled: true },
  { label: "DNS", icon: Globe, to: "/dns", disabled: true },
  { label: "NTP", icon: Clock, to: "/ntp", disabled: true },
];

export function Sidebar() {
  return (
    <aside className="flex w-56 flex-col border-r bg-card">
      <div className="flex h-14 items-center border-b px-4 font-semibold tracking-tight">
        SpatiumDDI
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
    </aside>
  );
}
