import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  lookingGlassApi,
  type BGPLGRoute,
  type BGPLGRpkiStatus,
} from "@/lib/api";
import { cn } from "@/lib/utils";

/** Shared RPKI pill colours + mini route table, reused across every
 *  BGP-linkage surface issue #566 Phase 3 adds (subnet BGP tab, block
 *  BGP panel, ASN "Learned Routes" tab, VRF "Routes" tab) instead of a
 *  fourth copy-pasted table. Mirrors the RPKI_COLOR/Pill pair already
 *  duplicated in RoutesTab.tsx and BgpMonitorTab.tsx — not hoisted into
 *  this file to avoid churning those two, but new call sites should
 *  import from here. */

// eslint-disable-next-line react-refresh/only-export-components
export const RPKI_COLOR: Record<BGPLGRpkiStatus, string> = {
  invalid: "bg-rose-500/15 text-rose-700 dark:text-rose-400",
  unknown: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  valid: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
};

export function RpkiPill({ status }: { status: BGPLGRpkiStatus }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide",
        RPKI_COLOR[status],
      )}
    >
      {status}
    </span>
  );
}

/** One shared peer_id -> peer_name lookup backing every mini-table below —
 *  React Query dedupes the ``listSessions()`` call across instances. */
// eslint-disable-next-line react-refresh/only-export-components
export function usePeerNameById() {
  const q = useQuery({
    queryKey: ["bgp-lg-sessions"],
    queryFn: () => lookingGlassApi.listSessions(),
    staleTime: 30_000,
  });
  return useMemo(() => {
    const m = new Map<string, string>();
    for (const s of q.data ?? []) m.set(s.peer_id, s.peer_name);
    return m;
  }, [q.data]);
}

/** Strip a vendor-style label prefix down to the bare "ASN:N"/"IP:N" RT
 *  value — mirrors the backend's ``normalize_rt`` in
 *  ``app.services.looking_glass.vrf_match`` (issue #566 Phase 6). Kept in
 *  sync by hand since this is the one frontend call site that needs it. */
function normalizeRt(raw: string): string {
  return raw.replace(/^(rt|target|route-target)\s*[:=]\s*/i, "").trim();
}

/** A VRF's import/export route-target lists — passed in by the VRF
 *  "Routes" tab (issue #566 Phase 6) to render an RD + route-target
 *  cross-check column on top of the base table. Omitted by every other
 *  caller (subnet/block/ASN tabs, Query tab), which just get the
 *  original five columns. */
export interface VrfRtContext {
  importTargets: string[];
  exportTargets: string[];
}

/** Prefix / peer / origin ASN / RPKI / best-path table. Read-only —
 *  every caller so far just wants "which routes matched this IPAM
 *  object/ASN/VRF", not an editable grid. */
export function BgpRouteMiniTable({
  items,
  vrfRtContext,
}: {
  items: BGPLGRoute[];
  vrfRtContext?: VrfRtContext;
}) {
  const peerNameById = usePeerNameById();
  const importSet = useMemo(
    () => new Set(vrfRtContext?.importTargets ?? []),
    [vrfRtContext],
  );
  const exportSet = useMemo(
    () => new Set(vrfRtContext?.exportTargets ?? []),
    [vrfRtContext],
  );
  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b bg-muted/30 text-left text-[10px] uppercase tracking-wider text-muted-foreground">
            <th className="px-3 py-2">Prefix</th>
            {vrfRtContext && <th className="px-3 py-2">RD</th>}
            <th className="px-3 py-2">Peer</th>
            <th className="px-3 py-2">Origin ASN</th>
            <th className="px-3 py-2">RPKI</th>
            <th className="px-3 py-2">Best</th>
            {vrfRtContext && <th className="px-3 py-2">Route targets</th>}
          </tr>
        </thead>
        <tbody>
          {items.map((r) => (
            <tr key={r.id} className="border-b last:border-0 hover:bg-muted/20">
              <td className="break-all px-3 py-2 font-mono">{r.prefix}</td>
              {vrfRtContext && (
                <td className="px-3 py-2 font-mono text-muted-foreground">
                  {r.route_distinguisher || "—"}
                </td>
              )}
              <td className="px-3 py-2">
                {peerNameById.get(r.peer_id) ?? "—"}
              </td>
              <td className="px-3 py-2 font-mono">
                {r.origin_asn == null ? "—" : `AS${r.origin_asn}`}
              </td>
              <td className="px-3 py-2">
                <RpkiPill status={r.rpki_status} />
              </td>
              <td className="px-3 py-2">{r.is_best ? "✓" : "—"}</td>
              {vrfRtContext && (
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    {r.ext_communities.length === 0 && (
                      <span className="text-muted-foreground">—</span>
                    )}
                    {r.ext_communities.map((rt) => {
                      const norm = normalizeRt(rt);
                      const kind = importSet.has(norm)
                        ? "import"
                        : exportSet.has(norm)
                          ? "export"
                          : null;
                      return (
                        <span
                          key={rt}
                          className={cn(
                            "rounded px-1.5 py-0.5 font-mono text-[11px]",
                            kind === "import" &&
                              "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
                            kind === "export" &&
                              "bg-sky-500/15 text-sky-700 dark:text-sky-400",
                            !kind && "bg-muted text-muted-foreground",
                          )}
                        >
                          {rt}
                          {kind ? ` (${kind})` : ""}
                        </span>
                      );
                    })}
                  </div>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
