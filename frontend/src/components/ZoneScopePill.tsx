import type { ZoneNameScope, ZoneNameScopeDetail } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * TLD scope of a zone / domain name (#986).
 *
 * `validate_fqdn` only ever said a name was *syntactically* a domain, so
 * `corp.example.com`, `ad.contoso.local`, `lab` and `acme.lan` all rendered
 * identically — while the first is a name the public internet resolves, the
 * second collides with mDNS, and the last two sit on TLDs nobody has
 * delegated. This is the one place that difference is visible.
 *
 * Nothing here refuses anything: `.local` is amber because Microsoft told a
 * generation of admins to build Active Directory on it and plenty of real
 * installs run it, and `undelegated` is a hint for the same reason. Both are
 * a pill and a tooltip, never a block.
 */

const LABEL: Record<ZoneNameScope, string> = {
  public: "Public",
  reserved: "Private",
  undelegated: "Undelegated",
  reverse: "Reverse",
};

// `reverse` is neutral on purpose — those zones are always ours (#41
// auto-creates them) and must not read as a problem. `reserved` is the
// blessed internal namespaces, so it is informational sky rather than a
// warning; the one amber case is `.local`, handled below.
const TONE: Record<ZoneNameScope, string> = {
  public: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  reserved: "bg-sky-500/15 text-sky-700 dark:text-sky-400",
  undelegated: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  reverse: "bg-zinc-500/15 text-zinc-600 dark:text-zinc-400",
};

const MDNS_TONE = "bg-amber-500/15 text-amber-700 dark:text-amber-400";

/** Fallback copy when only the bare scope is available (import preview). */
const FALLBACK_REASON: Record<ZoneNameScope, string> = {
  public:
    "The last label is a delegated top-level domain in the IANA root zone.",
  reserved: "A special-use namespace reserved by RFC or by ICANN.",
  undelegated:
    "Not a delegated top-level domain. This resolves inside your network today, but nothing protects the name — .internal is the name reserved for this.",
  reverse: "Reverse-lookup zone under in-addr.arpa / ip6.arpa.",
};

function zoneScopeTooltip(
  scope: ZoneNameScope,
  detail?: ZoneNameScopeDetail | null,
): string {
  const parts: string[] = [detail?.reason || FALLBACK_REASON[scope]];
  if (detail?.rfc) parts.push(`Reserved by ${detail.rfc}.`);
  if (detail?.mdns_conflict) {
    parts.push(
      "An authoritative .local zone collides with mDNS / Bonjour resolution on the same LAN — clients may get either answer.",
    );
  }
  return parts.join(" ");
}

export function ZoneScopePill({
  scope,
  detail,
  className = "",
}: {
  scope?: ZoneNameScope | null;
  detail?: ZoneNameScopeDetail | null;
  className?: string;
}) {
  if (!scope) return null;
  const amber = scope === "reserved" && detail?.mdns_conflict;
  return (
    <span
      title={zoneScopeTooltip(scope, detail)}
      className={cn(
        "inline-flex shrink-0 items-center rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        amber ? MDNS_TONE : TONE[scope],
        className,
      )}
    >
      {LABEL[scope]}
    </span>
  );
}

/**
 * Inline advisory under a zone-name field, shown live as the operator types.
 * Deliberately silent for `public` and `reverse` — the common cases, where a
 * hint would just be noise the operator learns to ignore.
 */
export function ZoneScopeHint({
  scope,
  detail,
}: {
  scope?: ZoneNameScope | null;
  detail?: ZoneNameScopeDetail | null;
}) {
  if (!scope || scope === "public" || scope === "reverse") return null;
  const amber = scope === "undelegated" || !!detail?.mdns_conflict;
  return (
    <p
      className={cn(
        "mt-1 text-[11px] leading-snug",
        amber ? "text-amber-600 dark:text-amber-400" : "text-muted-foreground",
      )}
    >
      <span className="font-medium">{LABEL[scope]}</span>
      {" — "}
      {zoneScopeTooltip(scope, detail)}
    </p>
  );
}
