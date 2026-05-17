import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  ExternalLink,
  Loader2,
  Search,
  Sparkles,
} from "lucide-react";
import { Modal } from "@/components/ui/modal";
import {
  dnsBlocklistApi,
  type BlocklistCatalogSource,
  type DNSBlockList,
  formatApiError,
} from "@/lib/api";

/**
 * Curated catalog browser — operator picks a source from the bundled
 * registry and clicks Subscribe; backend creates a `DNSBlockList` row
 * with the catalog entry's URL / format / category prefilled.
 *
 * Already-subscribed sources are flagged so operators don't double-add.
 */
export function BlocklistCatalogModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [filter, setFilter] = useState("");
  const [category, setCategory] = useState<string>("all");

  const { data: catalog } = useQuery({
    queryKey: ["dns", "blocklist-catalog"],
    queryFn: () => dnsBlocklistApi.catalog(),
    staleTime: 60 * 60 * 1000,
  });

  const { data: existing = [] } = useQuery<DNSBlockList[]>({
    queryKey: ["dns-blocklists"],
    queryFn: () => dnsBlocklistApi.list(),
  });

  const subscribedUrls = useMemo(
    () => new Set(existing.map((b) => b.feed_url ?? "")),
    [existing],
  );

  const subscribeMut = useMutation({
    mutationFn: (sourceId: string) =>
      dnsBlocklistApi.subscribeFromCatalog({ source_id: sourceId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-blocklists"] });
    },
  });

  const sources = catalog?.sources ?? [];
  const categories = useMemo(() => {
    const set = new Set(sources.map((s) => s.category));
    return ["all", ...Array.from(set).sort()];
  }, [sources]);

  const filtered = useMemo(() => {
    const f = filter.toLowerCase();
    return sources.filter((s) => {
      if (category !== "all" && s.category !== category) return false;
      if (!f) return true;
      return (
        s.name.toLowerCase().includes(f) ||
        s.description.toLowerCase().includes(f) ||
        s.id.toLowerCase().includes(f)
      );
    });
  }, [sources, filter, category]);

  return (
    <Modal title="Subscribe to a curated blocklist" onClose={onClose} wide>
      <div className="space-y-3 text-sm">
        <p className="text-xs text-muted-foreground">
          Public DNS blocklist sources curated from AdGuard's HostlistsRegistry,
          Pi-hole defaults, and Hagezi / OISD. Subscribing creates a
          ``url``-sourced blocklist with the entry's URL prefilled — the
          existing refresh pipeline parses + ingests entries on the configured
          cadence.
        </p>

        <div className="flex flex-wrap items-center gap-2">
          <div className="relative flex-1 min-w-[200px]">
            <Search className="absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground" />
            <input
              type="text"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter by name or description…"
              className="w-full rounded border bg-background pl-7 pr-2 py-1 text-xs"
            />
          </div>
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="rounded border bg-background px-2 py-1 text-xs"
          >
            {categories.map((c) => (
              <option key={c} value={c}>
                {c === "all" ? "All categories" : c}
              </option>
            ))}
          </select>
          {catalog && (
            <span className="ml-auto text-[11px] text-muted-foreground">
              Catalog version {catalog.version}
            </span>
          )}
        </div>

        <div className="space-y-2 max-h-[60vh] overflow-y-auto">
          {filtered.map((s) => (
            <CatalogRow
              key={s.id}
              source={s}
              alreadySubscribed={subscribedUrls.has(s.feed_url)}
              onSubscribe={() => subscribeMut.mutate(s.id)}
              isPending={
                subscribeMut.isPending && subscribeMut.variables === s.id
              }
            />
          ))}
          {filtered.length === 0 && (
            <p className="text-xs text-muted-foreground">
              No matching sources.
            </p>
          )}
        </div>

        {subscribeMut.isError && (
          <p className="text-xs text-destructive">
            {formatApiError(subscribeMut.error)}
          </p>
        )}

        <div className="flex justify-end pt-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded border px-3 py-1.5 text-xs hover:bg-muted/50"
          >
            Close
          </button>
        </div>
      </div>
    </Modal>
  );
}

function CatalogRow({
  source,
  alreadySubscribed,
  onSubscribe,
  isPending,
}: {
  source: BlocklistCatalogSource;
  alreadySubscribed: boolean;
  onSubscribe: () => void;
  isPending: boolean;
}) {
  return (
    <div className="rounded border bg-card p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 text-sm font-medium">
            {source.name}
            {source.recommended && (
              <span
                className="inline-flex items-center gap-0.5 rounded bg-amber-500/15 px-1 py-0.5 text-[10px] text-amber-700 dark:text-amber-400"
                title="Recommended starting point"
              >
                <Sparkles className="h-2.5 w-2.5" />
                Recommended
              </span>
            )}
            <span className="rounded bg-muted px-1 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
              {source.category}
            </span>
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {source.description}
          </p>
          <div className="mt-1 flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
            <span className="font-mono">{source.feed_format}</span>
            <span>License: {source.license}</span>
            {source.homepage && (
              <a
                href={source.homepage}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-0.5 hover:text-foreground"
              >
                Homepage
                <ExternalLink className="h-2.5 w-2.5" />
              </a>
            )}
          </div>
        </div>
        <div className="flex flex-shrink-0 flex-col items-end gap-1">
          {alreadySubscribed ? (
            <span className="inline-flex items-center gap-1 rounded bg-emerald-500/15 px-2 py-1 text-xs text-emerald-700 dark:text-emerald-400">
              <CheckCircle2 className="h-3 w-3" />
              Subscribed
            </span>
          ) : (
            <button
              type="button"
              onClick={onSubscribe}
              disabled={isPending}
              className="inline-flex items-center gap-1 rounded bg-primary px-2 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {isPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <AlertCircle className="h-3 w-3" />
              )}
              Subscribe
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
