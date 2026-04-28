import { useQuery } from "@tanstack/react-query";
import { Combine } from "lucide-react";
import { ipamApi } from "@/lib/api";

/**
 * Read-only banner that surfaces contiguous sibling subnets which would
 * pack into a clean supernet (e.g. 10.0.0.0/24 + 10.0.1.0/24 → /23).
 *
 * Today this is purely advisory — the operator decides what to do with
 * the suggestion (delete the children, recreate as the supernet). A
 * one-click merge flow is a deferred follow-up and would need to handle
 * the cascade across IP rows + DNS records owned by the deleted siblings.
 */
export function AggregationSuggestions({ blockId }: { blockId: string }) {
  const { data = [] } = useQuery({
    queryKey: ["block-aggregation-suggestions", blockId],
    queryFn: () => ipamApi.blockAggregationSuggestions(blockId),
    staleTime: 60 * 1000,
  });

  if (data.length === 0) return null;

  return (
    <div className="border-t bg-amber-500/5 px-6 py-2">
      <div className="flex items-start gap-2">
        <Combine className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-amber-600 dark:text-amber-500" />
        <div className="flex-1 space-y-1">
          <p className="text-[10px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-500">
            Aggregation candidate{data.length > 1 ? "s" : ""}
          </p>
          <ul className="space-y-0.5">
            {data.map((s) => (
              <li key={s.supernet} className="text-xs">
                <span className="font-mono">{s.supernet}</span>{" "}
                <span className="text-muted-foreground">←</span>{" "}
                <span className="font-mono text-muted-foreground">
                  {s.subnet_networks.join(", ")}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
