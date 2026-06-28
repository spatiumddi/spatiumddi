import {
  Archive,
  CheckCircle2,
  Circle,
  CircleDot,
  Lock,
  type LucideIcon,
  Minus,
  Radar,
  ShieldAlert,
  Pin,
  Unlink,
  Wifi,
} from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Unified status pill — **icon + text + color** in one primitive, so a
 * status is never communicated by color alone (WCAG 1.4.1) and stays legible
 * to color-blind operators and on monochrome/printed output. The shape of the
 * icon disambiguates statuses that share a hue (e.g. green `allocated` vs
 * green `available`).
 *
 * One source of truth for IPAM lifecycle statuses (IP addresses, subnets,
 * blocks). Other surfaces should import this rather than re-deriving a color
 * map. The color values match the long-standing IPAM `StatusBadge` palette so
 * adopting it is not a visual regression.
 */

type StatusStyle = { icon: LucideIcon; cls: string };

const STATUS_STYLES: Record<string, StatusStyle> = {
  active: {
    icon: CheckCircle2,
    cls: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  },
  available: {
    icon: Circle,
    cls: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  },
  allocated: {
    icon: CircleDot,
    cls: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400",
  },
  reserved: {
    icon: Lock,
    cls: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  },
  deprecated: {
    icon: Archive,
    cls: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
  },
  quarantine: {
    icon: ShieldAlert,
    cls: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400",
  },
  dhcp: {
    icon: Wifi,
    cls: "bg-cyan-100 text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-400",
  },
  static_dhcp: {
    icon: Pin,
    cls: "bg-teal-100 text-teal-800 dark:bg-teal-900/30 dark:text-teal-400",
  },
  network: {
    icon: Minus,
    cls: "bg-zinc-100 text-zinc-500 dark:bg-zinc-800/50 dark:text-zinc-400",
  },
  broadcast: {
    icon: Minus,
    cls: "bg-zinc-100 text-zinc-500 dark:bg-zinc-800/50 dark:text-zinc-400",
  },
  orphan: {
    icon: Unlink,
    cls: "bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400",
  },
  discovered: {
    icon: Radar,
    cls: "bg-sky-100 text-sky-800 dark:bg-sky-900/30 dark:text-sky-400",
  },
};

const FALLBACK: StatusStyle = {
  icon: Circle,
  cls: "bg-muted text-muted-foreground",
};

export function StatusTag({
  status,
  className,
  showIcon = true,
}: {
  status: string;
  className?: string;
  /** Drop the icon for very tight cells where text+color already suffices. */
  showIcon?: boolean;
}) {
  const { icon: Icon, cls } = STATUS_STYLES[status] ?? FALLBACK;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
        cls,
        className,
      )}
    >
      {showIcon && <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />}
      {status}
    </span>
  );
}
