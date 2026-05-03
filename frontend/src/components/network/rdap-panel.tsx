import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

/** ── RDAP payload helpers ──────────────────────────────────────────────
 *
 * RDAP responses are deeply nested JSON with a couple of conventions
 * that aren't great for direct ``JSON.stringify``-display:
 *
 *  * ``entities[]`` carry vCard arrays (``vcardArray``) that are themselves
 *    nested arrays — ``[type, [...properties]]`` — where each property is
 *    ``[key, params, valueType, value]``. Operators want a flat
 *    ``Org / Email / Phone / Address`` view, not the wire shape.
 *
 *  * ``events[]`` are ``{ eventAction, eventDate }`` pairs. Useful as a
 *    timeline.
 *
 *  * ``status[]`` and ``remarks[]`` are top-level lists worth surfacing.
 *
 * This renderer pulls those into structured UI and keeps the raw JSON
 * available behind a "Show raw" toggle.
 */

type RdapPayload = Record<string, unknown>;

interface RdapEntityFlat {
  roles: string[];
  handle?: string;
  fn?: string;
  org?: string;
  email?: string;
  phone?: string;
  adr?: string;
}

function flattenVcard(
  vcard: unknown,
): Omit<RdapEntityFlat, "roles" | "handle"> {
  // vcardArray shape: [ "vcard", [ [key, params, valueType, value], ... ] ]
  if (!Array.isArray(vcard) || vcard.length < 2 || !Array.isArray(vcard[1])) {
    return {};
  }
  const out: Omit<RdapEntityFlat, "roles" | "handle"> = {};
  for (const prop of vcard[1] as unknown[]) {
    if (!Array.isArray(prop) || prop.length < 4) continue;
    const key = prop[0];
    const value = prop[3];
    if (typeof key !== "string") continue;
    if (key === "fn" && typeof value === "string") out.fn = value;
    if (key === "org" && (typeof value === "string" || Array.isArray(value))) {
      out.org = Array.isArray(value)
        ? value.filter((v) => typeof v === "string").join(", ")
        : value;
    }
    if (key === "email" && typeof value === "string") out.email = value;
    if (key === "tel" && typeof value === "string") out.phone = value;
    if (key === "adr" && Array.isArray(value)) {
      out.adr = value
        .filter((v) => typeof v === "string" && v.length > 0)
        .join(", ");
    }
  }
  return out;
}

function flattenEntities(entities: unknown): RdapEntityFlat[] {
  if (!Array.isArray(entities)) return [];
  return entities
    .map((ent): RdapEntityFlat | null => {
      if (!ent || typeof ent !== "object") return null;
      const e = ent as Record<string, unknown>;
      const roles = Array.isArray(e.roles)
        ? (e.roles as unknown[]).filter(
            (r): r is string => typeof r === "string",
          )
        : [];
      const flat = flattenVcard(e.vcardArray);
      return {
        roles: roles.length > 0 ? roles : ["—"],
        handle: typeof e.handle === "string" ? e.handle : undefined,
        ...flat,
      };
    })
    .filter((x): x is RdapEntityFlat => x !== null);
}

function fmtIso(iso: unknown): string {
  if (typeof iso !== "string" || !iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function asStringArray(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string");
}

function asString(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

// ── Public component ─────────────────────────────────────────────────

interface RdapPanelProps {
  payload: RdapPayload | null | undefined;
  /** ``"asn"`` for autnum responses, ``"domain"`` for domain responses.
   * Controls which top-level fields are surfaced. */
  kind: "asn" | "domain";
  emptyMessage?: string;
}

export function RdapPanel({ payload, kind, emptyMessage }: RdapPanelProps) {
  const [showRaw, setShowRaw] = useState(false);

  if (!payload) {
    return (
      <p className="text-xs text-muted-foreground">
        {emptyMessage ?? "No RDAP data — run Refresh WHOIS to populate."}
      </p>
    );
  }

  const entities = flattenEntities(payload.entities);
  const events = Array.isArray(payload.events)
    ? (payload.events as unknown[]).filter(
        (e): e is Record<string, unknown> => !!e && typeof e === "object",
      )
    : [];
  const statuses = asStringArray(payload.status);
  const remarks = Array.isArray(payload.remarks)
    ? (payload.remarks as unknown[]).filter(
        (r): r is Record<string, unknown> => !!r && typeof r === "object",
      )
    : [];

  const headlineFields: Array<[string, string | null]> =
    kind === "asn"
      ? [
          ["Handle", asString(payload.handle)],
          ["Name", asString(payload.name)],
          ["Type", asString(payload.type)],
          [
            "Range",
            payload.startAutnum != null
              ? `${payload.startAutnum}${payload.endAutnum != null ? ` – ${payload.endAutnum}` : ""}`
              : null,
          ],
          ["Country", asString(payload.country)],
          ["Port 43 (legacy WHOIS)", asString(payload.port43)],
        ]
      : [
          [
            "Domain",
            asString(payload.ldhName) ?? asString(payload.unicodeName),
          ],
          ["Handle", asString(payload.handle)],
          [
            "DNSSEC",
            (() => {
              const sd = payload.secureDNS as
                | Record<string, unknown>
                | undefined;
              if (!sd) return null;
              return sd.delegationSigned ? "Signed" : "Unsigned";
            })(),
          ],
          ["Port 43 (legacy WHOIS)", asString(payload.port43)],
        ];

  return (
    <div className="space-y-4">
      {/* Headline fields */}
      <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
        {headlineFields
          .filter(([, v]) => v != null)
          .map(([label, value]) => (
            <div key={label} className="flex flex-col gap-0.5">
              <dt className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                {label}
              </dt>
              <dd className="text-sm">{value}</dd>
            </div>
          ))}
      </dl>

      {/* Status flags */}
      {statuses.length > 0 && (
        <div>
          <p className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Status
          </p>
          <div className="flex flex-wrap gap-1.5">
            {statuses.map((s) => (
              <span
                key={s}
                className="rounded border bg-muted px-2 py-0.5 font-mono text-[11px]"
              >
                {s}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Domain-only: nameservers list */}
      {kind === "domain" && Array.isArray(payload.nameservers) && (
        <div>
          <p className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Nameservers
          </p>
          <div className="flex flex-wrap gap-1.5">
            {(payload.nameservers as unknown[])
              .map((ns) =>
                ns && typeof ns === "object"
                  ? asString((ns as Record<string, unknown>).ldhName)
                  : null,
              )
              .filter((s): s is string => s !== null)
              .map((s) => (
                <span
                  key={s}
                  className="rounded border bg-muted px-2 py-0.5 font-mono text-[11px]"
                >
                  {s.toLowerCase()}
                </span>
              ))}
          </div>
        </div>
      )}

      {/* Events timeline */}
      {events.length > 0 && (
        <div>
          <p className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Timeline
          </p>
          <ul className="divide-y rounded-md border bg-card">
            {events.map((e, i) => (
              <li
                key={i}
                className="flex items-baseline justify-between gap-3 px-3 py-1.5 text-xs"
              >
                <span className="font-mono capitalize">
                  {asString(e.eventAction) ?? "—"}
                </span>
                <span className="text-muted-foreground tabular-nums">
                  {fmtIso(e.eventDate)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Entities by role */}
      {entities.length > 0 && (
        <div>
          <p className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Entities
          </p>
          <div className="space-y-2">
            {entities.map((e, i) => (
              <div key={i} className="rounded-md border bg-card p-3">
                <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
                  {e.roles.map((r) => (
                    <span
                      key={r}
                      className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider"
                    >
                      {r}
                    </span>
                  ))}
                  {e.handle && (
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {e.handle}
                    </span>
                  )}
                </div>
                <dl className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
                  {e.org && <KvRow k="Org" v={e.org} />}
                  {e.fn && e.fn !== e.org && <KvRow k="Name" v={e.fn} />}
                  {e.email && <KvRow k="Email" v={e.email} mono />}
                  {e.phone && <KvRow k="Phone" v={e.phone} mono />}
                  {e.adr && <KvRow k="Address" v={e.adr} />}
                </dl>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Remarks */}
      {remarks.length > 0 && (
        <div>
          <p className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Remarks
          </p>
          <ul className="space-y-1 text-xs text-muted-foreground">
            {remarks.map((r, i) => (
              <li key={i}>{asStringArray(r.description).join(" ") || "—"}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Raw payload toggle — keep available for ops debugging. */}
      <button
        type="button"
        onClick={() => setShowRaw((s) => !s)}
        className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
      >
        {showRaw ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        {showRaw ? "Hide raw RDAP JSON" : "Show raw RDAP JSON"}
      </button>
      {showRaw && (
        <pre className="overflow-auto max-h-96 rounded-md border bg-muted/30 p-3 text-[11px] font-mono whitespace-pre-wrap">
          {JSON.stringify(payload, null, 2)}
        </pre>
      )}
    </div>
  );
}

function KvRow({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {k}
      </dt>
      <dd className={cn("text-xs", mono && "font-mono")}>{v}</dd>
    </div>
  );
}
