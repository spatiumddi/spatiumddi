import { useQuery } from "@tanstack/react-query";

import { lookingGlassApi } from "@/lib/api";
import { BgpRouteMiniTable } from "@/components/network/bgp-route-table";

/** "Learned Routes" tab on the ASN detail page (issue #566 Phase 3) —
 *  the internal Looking Glass RIB view, sitting next to BGP Monitoring's
 *  external hijack-detection view.
 *
 *  Filters by ``origin_asn`` (the raw wire value), NOT ``matched_asn_id``
 *  — origin_asn is always immediately correct on ingest, while
 *  matched_asn_id only gets populated once the ingest/re-resolve cache
 *  has run (up to a 5-minute lag on a route that was already in the RIB
 *  before this ASN row was created). Filtering by origin_asn avoids a
 *  confusing "empty tab" window for an ASN the operator just tracked.
 */
export function LearnedRoutesTab({
  asnNumber,
  asnId,
}: {
  asnNumber: number;
  asnId: string;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["bgp-lg-routes-by-asn", asnId, asnNumber],
    queryFn: () =>
      lookingGlassApi.searchRoutes({
        origin_asn: asnNumber,
        withdrawn: false,
        limit: 200,
      }),
    staleTime: 15_000,
  });

  if (isLoading) {
    return <p className="p-6 text-sm text-muted-foreground">Loading…</p>;
  }
  const items = data?.items ?? [];
  if (items.length === 0) {
    return (
      <div className="flex flex-col items-center gap-2 p-10 text-center">
        <p className="text-sm text-muted-foreground">
          No routes with origin AS{asnNumber} in the current RIB.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <p className="text-xs text-muted-foreground">
        Every active BGP Looking Glass route whose origin AS is AS
        {asnNumber}.
      </p>
      <BgpRouteMiniTable items={items} />
    </div>
  );
}
