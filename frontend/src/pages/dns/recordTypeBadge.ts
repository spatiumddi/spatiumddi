// Single source of truth for record-type badge colours so the
// server-group RecordsTab and the per-zone view stay in sync.
// Adding a new record type? Drop a tailwind class pair here and
// every consumer picks it up. The default fallback below covers
// rarer types (CAA / SSHFP / TLSA / NAPTR / LOC) without having
// to enumerate them all.
//
// Lives in its own module rather than next to the components in
// DNSPage.tsx so React's fast-refresh stays happy: a file that
// exports both components AND constants triggers
// react-refresh/only-export-components.
export const RECORD_TYPE_BADGE: Record<string, string> = {
  A: "bg-blue-500/15 text-blue-600",
  AAAA: "bg-violet-500/15 text-violet-600",
  CNAME: "bg-amber-500/15 text-amber-600",
  ALIAS: "bg-fuchsia-500/15 text-fuchsia-600",
  LUA: "bg-rose-500/15 text-rose-600",
  MX: "bg-emerald-500/15 text-emerald-600",
  TXT: "bg-muted text-muted-foreground",
  NS: "bg-orange-500/15 text-orange-600",
  PTR: "bg-cyan-500/15 text-cyan-600",
  SRV: "bg-teal-500/15 text-teal-600",
  SOA: "bg-stone-500/15 text-stone-600",
};

export const RECORD_TYPE_BADGE_FALLBACK = "bg-muted text-muted-foreground";
