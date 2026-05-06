import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Boxes, RefreshCcw, RotateCcw, Save, Sparkles } from "lucide-react";
import { aiToolCatalogApi, type AIToolCatalogEntry } from "@/lib/api";
import { cn, zebraBodyCls } from "@/lib/utils";

const headerCls =
  "flex shrink-0 items-center gap-1.5 rounded-md border bg-background px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50";

const CATEGORY_LABELS: Record<string, string> = {
  ipam: "IPAM",
  dns: "DNS",
  dhcp: "DHCP",
  network: "Network",
  ops: "Operations",
};

export function AIToolCatalogPage() {
  const qc = useQueryClient();
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["ai-tool-catalog"],
    queryFn: () => aiToolCatalogApi.list(),
  });

  // Local-edit state mirrors the live catalog. Operator clicks
  // checkboxes to add / remove tool names; "Save" PUTs the explicit
  // list, "Reset to defaults" sends null (revert to registry
  // per-tool defaults). Search filter narrows the visible rows
  // without affecting the selection.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (!data) return;
    setSelected(
      new Set(data.tools.filter((t) => t.enabled).map((t) => t.name)),
    );
    setDirty(false);
  }, [data]);

  const grouped = useMemo(() => {
    if (!data) return [] as [string, AIToolCatalogEntry[]][];
    const haystack = search.trim().toLowerCase();
    const filtered = haystack
      ? data.tools.filter(
          (t) =>
            t.name.toLowerCase().includes(haystack) ||
            t.description.toLowerCase().includes(haystack) ||
            t.category.toLowerCase().includes(haystack),
        )
      : data.tools;
    const buckets: Record<string, AIToolCatalogEntry[]> = {};
    for (const t of filtered) {
      const key = t.category || "other";
      (buckets[key] ??= []).push(t);
    }
    return Object.entries(buckets).sort((a, b) => a[0].localeCompare(b[0]));
  }, [data, search]);

  const totalEnabled = selected.size;
  const totalKnown = data?.total ?? 0;

  function toggle(name: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
    setDirty(true);
  }

  function selectAll(names: string[], state: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const n of names) {
        if (state) next.add(n);
        else next.delete(n);
      }
      return next;
    });
    setDirty(true);
  }

  const save = useMutation({
    mutationFn: () => aiToolCatalogApi.update([...selected].sort()),
    onSuccess: () => {
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["ai-tool-catalog"] });
    },
  });

  const reset = useMutation({
    mutationFn: () => aiToolCatalogApi.update(null),
    onSuccess: () => {
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["ai-tool-catalog"] });
    },
  });

  return (
    <div className="h-full overflow-auto p-6">
      <div className="space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
              <Sparkles className="h-5 w-5" /> Tool Catalog
            </h1>
            <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
              Each tool here is something the Operator Copilot can call on your
              behalf. Disabled tools are still listed for the model so it can
              tell users "ask your admin to enable X" instead of giving up.
              Per-provider allowlists narrow this further on the AI Providers
              page.
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search…"
              className="w-44 rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
            <button
              onClick={() => refetch()}
              disabled={isFetching}
              className={headerCls}
            >
              <RefreshCcw
                className={cn("h-3.5 w-3.5", isFetching && "animate-spin")}
              />
              Refresh
            </button>
            <button
              onClick={() => reset.mutate()}
              disabled={reset.isPending}
              className={headerCls}
              title="Discard the explicit list and use each tool's registry default."
            >
              <RotateCcw className="h-3.5 w-3.5" />
              Reset to defaults
            </button>
            <button
              onClick={() => save.mutate()}
              disabled={!dirty || save.isPending}
              className="flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              <Save className="h-3.5 w-3.5" />
              {save.isPending ? "Saving…" : "Save"}
            </button>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3 rounded-lg border bg-muted/20 px-4 py-3 text-sm">
          <Boxes className="h-4 w-4 text-muted-foreground" />
          <span>
            <span className="font-medium">{totalEnabled}</span> of {totalKnown}{" "}
            tools enabled
          </span>
          {data?.platform_override === null && (
            <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300">
              Using registry defaults
            </span>
          )}
          {dirty && (
            <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-300">
              Unsaved changes
            </span>
          )}
        </div>

        {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

        {grouped.map(([cat, tools]) => {
          const allOn = tools.every((t) => selected.has(t.name));
          const someOn = !allOn && tools.some((t) => selected.has(t.name));
          return (
            <section key={cat} className="overflow-hidden rounded-lg border">
              <header className="flex items-center justify-between gap-3 border-b bg-muted/40 px-4 py-2.5">
                <h2 className="text-sm font-semibold">
                  {CATEGORY_LABELS[cat] ?? cat} ({tools.length})
                </h2>
                <button
                  onClick={() =>
                    selectAll(
                      tools.map((t) => t.name),
                      !allOn,
                    )
                  }
                  className="text-xs text-muted-foreground hover:text-foreground"
                >
                  {allOn ? "Disable all" : someOn ? "Enable all" : "Enable all"}
                </button>
              </header>
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/20 text-xs">
                    <th className="w-10 px-3 py-2"></th>
                    <th className="px-3 py-2 text-left font-medium">Name</th>
                    <th className="px-3 py-2 text-left font-medium">
                      Description
                    </th>
                    <th className="w-24 px-3 py-2 text-left font-medium">
                      Default
                    </th>
                  </tr>
                </thead>
                <tbody className={zebraBodyCls}>
                  {tools.map((t) => {
                    const checked = selected.has(t.name);
                    const overridden = checked !== t.default_enabled;
                    return (
                      <tr
                        key={t.name}
                        className="border-b last:border-0 hover:bg-muted/20"
                      >
                        <td className="px-3 py-2 align-top">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => toggle(t.name)}
                          />
                        </td>
                        <td className="px-3 py-2 align-top">
                          <div className="flex items-center gap-1.5">
                            <code className="font-mono text-xs">{t.name}</code>
                            {overridden && (
                              <span
                                className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-300"
                                title="Overrides the registry default"
                              >
                                override
                              </span>
                            )}
                            {t.writes && (
                              <span className="rounded-full bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-800 dark:bg-red-900/30 dark:text-red-300">
                                writes
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="px-3 py-2 align-top text-muted-foreground">
                          {t.description.split("\n")[0]}
                        </td>
                        <td className="px-3 py-2 align-top">
                          {t.default_enabled ? (
                            <span className="text-xs text-emerald-700 dark:text-emerald-400">
                              on
                            </span>
                          ) : (
                            <span className="text-xs text-muted-foreground">
                              off
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </section>
          );
        })}
      </div>
    </div>
  );
}
