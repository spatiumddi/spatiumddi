import { cn } from "@/lib/utils";
import { humanTime } from "@/pages/network/_shared";

/**
 * Recency indicator for an IPAddress row.
 *
 * "Alive on the wire" is orthogonal to lifecycle status — an
 * ``allocated`` IP can be down, a ``discovered`` IP can be currently
 * responding. We render a coloured dot derived from
 * ``IPAddress.last_seen_at``, with the source written in the tooltip
 * (``via dhcp`` / ``via nmap`` / ``via snmp`` / ``via arp`` / etc).
 *
 * Thresholds:
 *   < 24 h    → green   "alive"
 *   24h–7d    → amber   "stale"
 *   > 7d      → red     "cold"
 *   never     → grey    "never seen"
 */
export type SeenState = "alive" | "stale" | "cold" | "never";

const HOUR_MS = 3600_000;
const DAY_MS = 24 * HOUR_MS;
const WEEK_MS = 7 * DAY_MS;

// eslint-disable-next-line react-refresh/only-export-components
export function getSeenState(lastSeenAt: string | null | undefined): SeenState {
  if (!lastSeenAt) return "never";
  const t = new Date(lastSeenAt).getTime();
  if (Number.isNaN(t)) return "never";
  const age = Date.now() - t;
  if (age < 0) return "alive";
  if (age < DAY_MS) return "alive";
  if (age < WEEK_MS) return "stale";
  return "cold";
}

export function SeenDot({
  lastSeenAt,
  lastSeenMethod,
  size = "sm",
}: {
  lastSeenAt: string | null | undefined;
  lastSeenMethod?: string | null;
  size?: "sm" | "md";
}) {
  const state = getSeenState(lastSeenAt);
  const dotCls = {
    alive: "bg-emerald-500",
    stale: "bg-amber-500",
    cold: "bg-rose-500",
    never: "bg-zinc-300 dark:bg-zinc-600",
  }[state];
  const sizeCls = size === "md" ? "h-2.5 w-2.5" : "h-2 w-2";
  const tip = (() => {
    if (state === "never") return "Never seen on the wire";
    const rel = humanTime(lastSeenAt);
    const via = lastSeenMethod ? ` via ${lastSeenMethod}` : "";
    if (state === "alive") return `Alive — last seen ${rel}${via}`;
    if (state === "stale") return `Stale — last seen ${rel}${via}`;
    return `Cold — last seen ${rel}${via}`;
  })();
  return (
    <span
      className={cn(
        "inline-flex items-center justify-center rounded-full",
        sizeCls,
        dotCls,
      )}
      title={tip}
      aria-label={tip}
    />
  );
}
