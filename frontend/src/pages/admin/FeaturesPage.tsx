import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCcw, ToggleLeft } from "lucide-react";
import { featureModulesApi, type FeatureModuleEntry } from "@/lib/api";
import { useSessionState } from "@/lib/useSessionState";
import { cn } from "@/lib/utils";
import { Toggle } from "@/components/ui/toggle";

const headerCls =
  "flex shrink-0 items-center gap-1.5 rounded-md border bg-background px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50";

// Tab → which catalog ``group`` values land on it. Each tab gets its
// own React Query refetch but shares the underlying cache. New tabs
// drop in here when we add module groups (e.g. "compliance" graduates
// to its own tab once the alert rules + audit-tamper-detection items
// land).
type TabId = "features" | "integrations";
const TABS: { id: TabId; label: string; groups: string[] }[] = [
  {
    id: "features",
    label: "Features",
    groups: ["Network", "AI", "Compliance", "Tools"],
  },
  {
    id: "integrations",
    label: "Integrations",
    groups: ["Integrations"],
  },
];

/** Settings → Features.
 *
 * Operator-controlled visibility for whole sidebar / REST / MCP
 * surfaces. Toggling a row immediately persists (no batch Save) and
 * busts the React Query cache for both the sidebar and this page so
 * the disabled module disappears in real time.
 *
 * Default policy on a fresh install is everything-on so admins
 * discover what exists. New features added in upgrades default
 * enabled too — operators turn them off after the fact if they
 * don't want them. Off-prem / secret-touching modules can override
 * by declaring ``default_enabled=False`` in the backend catalog
 * (the integrations all do).
 *
 * Layout: compact two-column grid — module identity on the left,
 * toggle on the right. Bottom padding leaves room for the floating
 * Copilot button so the last row's checkbox isn't covered.
 */
export function FeaturesPage() {
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useSessionState<TabId>(
    "features-page-tab",
    "features",
  );
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["feature-modules"],
    queryFn: featureModulesApi.list,
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      featureModulesApi.toggle(id, enabled),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["feature-modules"] });
      // Settings page also reads PlatformSettings.integration_*_enabled
      // (the toggle endpoint mirrors the value). Bust that cache too
      // so any open Settings tab catches the change without a manual
      // refresh.
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });

  const activeTabDef = TABS.find((t) => t.id === activeTab) ?? TABS[0];
  const grouped = useMemo(() => {
    if (!data) return [] as [string, FeatureModuleEntry[]][];
    const visible = data.filter((m) => activeTabDef.groups.includes(m.group));
    const buckets: Record<string, FeatureModuleEntry[]> = {};
    for (const m of visible) {
      (buckets[m.group] ??= []).push(m);
    }
    // Stable per-tab group order: follow the order declared in
    // ``activeTabDef.groups`` rather than alphabetical, so the
    // Features tab reads Network → AI → Compliance → Tools.
    return activeTabDef.groups
      .filter((g) => buckets[g])
      .map((g) => [g, buckets[g]] as [string, FeatureModuleEntry[]]);
  }, [data, activeTabDef]);

  const enabledCount = data?.filter((m) => m.enabled).length ?? 0;
  const totalCount = data?.length ?? 0;

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b p-4">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <ToggleLeft className="h-5 w-5 flex-shrink-0 text-primary" />
          <div className="min-w-0">
            <h1 className="text-lg font-semibold">Features</h1>
            <p className="text-xs text-muted-foreground">
              Hide platform features your deployment doesn't use. Disabled
              modules disappear from the sidebar, the REST API, and the AI
              copilot's tool surface.
            </p>
          </div>
        </div>
        <div className="shrink-0 text-xs text-muted-foreground">
          {enabledCount} of {totalCount} enabled
        </div>
        <button
          type="button"
          onClick={() => refetch()}
          disabled={isFetching}
          className={headerCls}
          title="Reload from server"
        >
          <RefreshCcw
            className={cn("h-3.5 w-3.5", isFetching && "animate-spin")}
          />
          Refresh
        </button>
      </div>

      {/* Tabs — each tab shows a different slice of the catalog. */}
      <div className="flex shrink-0 items-center gap-1 border-b px-4 pt-2">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setActiveTab(t.id)}
            className={cn(
              "rounded-t-md border-b-2 px-3 py-2 text-sm font-medium transition-colors",
              activeTab === t.id
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {isLoading && (
        <div className="p-4 text-sm text-muted-foreground">Loading…</div>
      )}

      {/* Layout strategy:
       *   - "wide" groups (≥3 modules) take a full row and lay their
       *     modules out in up to 3 columns internally.
       *   - "narrow" groups (≤2 modules) cluster: consecutive narrows
       *     pack 3-per-row into a shared cluster row so 1-item
       *     sections like AI / Compliance / Tools sit side-by-side
       *     instead of stacking with three rows of dead space on the
       *     right. When the buffer fills mid-stream because a wide
       *     group interrupts, we flush the buffer first.
       *   - Inside a clustered section, internal column count is
       *     forced to 1 — the section is already only ~1/3 of the page
       *     wide, so multi-column inside would crush the descriptions.
       *   - pb-24 keeps the last row above the floating Copilot
       *     button.
       */}
      <div className="flex-1 space-y-3 overflow-auto p-4 pb-24">
        {(() => {
          type Block =
            | {
                kind: "wide";
                group: string;
                modules: FeatureModuleEntry[];
              }
            | {
                kind: "cluster";
                items: { group: string; modules: FeatureModuleEntry[] }[];
              };
          const blocks: Block[] = [];
          let buffer: { group: string; modules: FeatureModuleEntry[] }[] = [];
          const flush = () => {
            if (buffer.length) {
              blocks.push({ kind: "cluster", items: buffer });
              buffer = [];
            }
          };
          for (const [group, modules] of grouped) {
            if (modules.length >= 3) {
              flush();
              blocks.push({ kind: "wide", group, modules });
            } else {
              buffer.push({ group, modules });
            }
          }
          flush();

          const renderSection = (
            group: string,
            modules: FeatureModuleEntry[],
            clustered: boolean,
          ) => {
            // Clustered sections live in ~1/3 page width — keep them
            // single-column internally so the description never gets
            // squeezed into a single chip-width line.
            const cols = clustered ? 1 : Math.min(3, modules.length);
            const fillerCount =
              cols > 0 ? (cols - (modules.length % cols)) % cols : 0;
            const colsClass =
              cols === 3
                ? "md:grid-cols-3"
                : cols === 2
                  ? "md:grid-cols-2"
                  : "";
            return (
              <section
                key={group}
                className="overflow-hidden rounded-md border bg-background"
              >
                <header className="border-b bg-muted/30 px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  {group}
                </header>
                <div
                  className={cn(
                    "grid grid-cols-1 gap-px bg-border/40",
                    colsClass,
                  )}
                >
                  {modules.map((m) => {
                    const isOverridden = m.enabled !== m.default_enabled;
                    return (
                      <div
                        key={m.id}
                        className="flex items-start gap-3 bg-background px-3 py-2 hover:bg-muted/40"
                      >
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-baseline gap-x-2">
                            <span className="text-sm font-medium">
                              {m.label}
                            </span>
                            <code className="font-mono text-[10px] text-muted-foreground/70">
                              {m.id}
                            </code>
                            {isOverridden && (
                              <span
                                className="rounded bg-amber-500/15 px-1 py-px text-[9px] font-medium text-amber-700 dark:text-amber-400"
                                title={`Default: ${m.default_enabled ? "on" : "off"}`}
                              >
                                overridden
                              </span>
                            )}
                          </div>
                          <p className="mt-0.5 text-xs leading-snug text-muted-foreground">
                            {m.description}
                          </p>
                        </div>
                        <div className="mt-0.5">
                          <Toggle
                            label={`${m.enabled ? "Disable" : "Enable"} ${m.label}`}
                            checked={m.enabled}
                            disabled={toggleMutation.isPending}
                            onChange={(v) =>
                              toggleMutation.mutate({ id: m.id, enabled: v })
                            }
                          />
                        </div>
                      </div>
                    );
                  })}
                  {Array.from({ length: fillerCount }).map((_, idx) => (
                    <div
                      key={`filler-${idx}`}
                      className="hidden bg-background md:block"
                      aria-hidden
                    />
                  ))}
                </div>
              </section>
            );
          };

          return blocks.map((block, idx) => {
            if (block.kind === "wide") {
              return renderSection(block.group, block.modules, false);
            }
            return (
              <div
                key={`cluster-${idx}`}
                className="grid grid-cols-1 gap-3 md:grid-cols-3"
              >
                {block.items.map(({ group, modules }) =>
                  renderSection(group, modules, true),
                )}
              </div>
            );
          });
        })()}
        {!isLoading && grouped.length === 0 && (
          <div className="rounded-md border bg-muted/20 p-6 text-center text-sm text-muted-foreground">
            No modules in this tab.
          </div>
        )}
      </div>
    </div>
  );
}
