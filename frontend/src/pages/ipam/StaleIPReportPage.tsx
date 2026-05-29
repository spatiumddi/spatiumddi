import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { History, RefreshCw, Trash2 } from "lucide-react";

import {
  ipamApi,
  type StaleIPDeprecateRequest,
  type StaleIPEntry,
} from "@/lib/api";
import { HeaderButton } from "@/components/ui/header-button";
import { ConfirmModal } from "@/components/ui/confirm-modal";
import { SeenDot } from "@/pages/ipam/SeenDot";
import { humanTime } from "@/pages/network/_shared";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 200;

const inputCls =
  "rounded-md border bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring";

/**
 * Stale-IP report (issue #45). Cross-subnet hygiene view over the
 * discovery (#23) ``last_seen_at`` signal: allocated IPs nothing has
 * answered for in N days. One-click bulk-deprecate flips the selected
 * rows (or every matching row) to ``deprecated`` — reversible from the
 * normal IPAM edit path.
 */
export function StaleIPReportPage() {
  const qc = useQueryClient();
  const [staleDays, setStaleDays] = useState(90);
  const [includeNeverSeen, setIncludeNeverSeen] = useState(false);
  const [spaceId, setSpaceId] = useState<string>("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Pending deprecate action — null when no modal open. ``all`` deprecates
  // every matching row server-side; otherwise the ticked ``ids``.
  const [pending, setPending] = useState<
    { kind: "selected"; ids: string[] } | { kind: "all"; count: number } | null
  >(null);

  const params = useMemo(
    () => ({
      stale_days: staleDays,
      include_never_seen: includeNeverSeen,
      space_id: spaceId || undefined,
      limit: PAGE_SIZE,
      offset,
    }),
    [staleDays, includeNeverSeen, spaceId, offset],
  );

  const spaces = useQuery({
    queryKey: ["ipam-spaces"],
    queryFn: ipamApi.listSpaces,
  });

  const report = useQuery({
    queryKey: ["stale-ips", params],
    queryFn: () => ipamApi.getStaleIPs(params),
  });

  const deprecate = useMutation({
    mutationFn: (body: StaleIPDeprecateRequest) =>
      ipamApi.deprecateStaleIPs(body),
    onSuccess: () => {
      setSelected(new Set());
      setPending(null);
      qc.invalidateQueries({ queryKey: ["stale-ips"] });
    },
  });

  const entries = report.data?.entries ?? [];
  const total = report.data?.total ?? 0;
  const selectableIds = useMemo(() => entries.map((e) => e.id), [entries]);
  const allSelected =
    selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const toggleAll = () =>
    setSelected((prev) =>
      allSelected ? new Set() : new Set([...prev, ...selectableIds]),
    );

  const resetPage = () => {
    setOffset(0);
    setSelected(new Set());
  };

  const confirmDeprecate = () => {
    if (!pending) return;
    if (pending.kind === "selected") {
      deprecate.mutate({ ip_ids: pending.ids });
    } else {
      deprecate.mutate({
        all_matching: true,
        stale_days: staleDays,
        include_never_seen: includeNeverSeen,
        space_id: spaceId || undefined,
      });
    }
  };

  return (
    <div className="flex min-w-0 flex-1 flex-col gap-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            <History className="h-5 w-5 shrink-0" /> Stale IPs
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Allocated IPs nothing has seen on the wire in {staleDays}+ days,
            drawn from the discovery last-seen signal. Deprecate to reclaim
            address space — it&rsquo;s reversible from the normal IP edit path.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <HeaderButton
            variant="secondary"
            onClick={() => report.refetch()}
            disabled={report.isFetching}
          >
            <RefreshCw
              className={cn("h-4 w-4", report.isFetching && "animate-spin")}
            />
            Refresh
          </HeaderButton>
          <HeaderButton
            variant="destructive"
            disabled={total === 0 || deprecate.isPending}
            onClick={() => setPending({ kind: "all", count: total })}
          >
            <Trash2 className="h-4 w-4" />
            Deprecate all {total}
          </HeaderButton>
        </div>
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-end gap-4 rounded-md border bg-muted/20 p-3">
        <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
          Stale window (days)
          <input
            type="number"
            min={1}
            max={3650}
            className={cn(inputCls, "w-28")}
            value={staleDays}
            onChange={(e) => {
              setStaleDays(Math.max(1, Number(e.target.value) || 1));
              resetPage();
            }}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
          IP space
          <select
            className={cn(inputCls, "w-48")}
            value={spaceId}
            onChange={(e) => {
              setSpaceId(e.target.value);
              resetPage();
            }}
          >
            <option value="">All spaces</option>
            {(spaces.data ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            className="cursor-pointer"
            checked={includeNeverSeen}
            onChange={(e) => {
              setIncludeNeverSeen(e.target.checked);
              resetPage();
            }}
          />
          Include never-seen
        </label>
      </div>

      {/* Bulk toolbar — only when rows are ticked */}
      {selected.size > 0 && (
        <div className="flex items-center justify-between rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm dark:border-amber-700 dark:bg-amber-950/30">
          <span>{selected.size} selected</span>
          <HeaderButton
            variant="destructive"
            disabled={deprecate.isPending}
            onClick={() => setPending({ kind: "selected", ids: [...selected] })}
          >
            <Trash2 className="h-4 w-4" />
            Deprecate selected
          </HeaderButton>
        </div>
      )}

      <div className="min-w-0 overflow-x-auto rounded-md border">
        <table className="w-full min-w-[760px] text-sm">
          <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
            <tr>
              <th className="w-8 px-3 py-2">
                <input
                  type="checkbox"
                  className="cursor-pointer"
                  checked={allSelected}
                  onChange={toggleAll}
                  aria-label="Select all on this page"
                />
              </th>
              <th className="px-3 py-2">Address</th>
              <th className="px-3 py-2">Hostname</th>
              <th className="px-3 py-2">Subnet</th>
              <th className="px-3 py-2">Last seen</th>
              <th className="px-3 py-2 text-right">Days stale</th>
            </tr>
          </thead>
          <tbody>
            {report.isLoading ? (
              <tr>
                <td
                  colSpan={6}
                  className="px-3 py-8 text-center text-muted-foreground"
                >
                  Loading…
                </td>
              </tr>
            ) : entries.length === 0 ? (
              <tr>
                <td
                  colSpan={6}
                  className="px-3 py-8 text-center text-muted-foreground"
                >
                  No stale IPs in this window. Address space looks healthy. ✨
                </td>
              </tr>
            ) : (
              entries.map((e: StaleIPEntry) => (
                <tr key={e.id} className="border-t hover:bg-muted/20">
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      className="cursor-pointer"
                      checked={selected.has(e.id)}
                      onChange={() => toggle(e.id)}
                      aria-label={`Select ${e.address}`}
                    />
                  </td>
                  <td className="px-3 py-2 font-mono">
                    <Link
                      to={`/ipam?subnet=${e.subnet_id}`}
                      className="text-primary hover:underline"
                    >
                      {e.address}
                    </Link>
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {e.hostname || "—"}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">
                    <span className="font-mono">{e.subnet_network ?? "?"}</span>
                    {e.subnet_name ? (
                      <span className="ml-1">— {e.subnet_name}</span>
                    ) : null}
                  </td>
                  <td className="px-3 py-2">
                    <span className="inline-flex items-center gap-1.5">
                      <SeenDot
                        lastSeenAt={e.last_seen_at}
                        lastSeenMethod={e.last_seen_method}
                      />
                      <span className="text-muted-foreground">
                        {e.last_seen_at ? humanTime(e.last_seen_at) : "never"}
                      </span>
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                    {e.days_stale ?? "—"}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-sm text-muted-foreground">
          <span>
            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
          </span>
          <div className="flex gap-2">
            <HeaderButton
              variant="secondary"
              disabled={offset === 0}
              onClick={() => {
                setOffset(Math.max(0, offset - PAGE_SIZE));
                setSelected(new Set());
              }}
            >
              Prev
            </HeaderButton>
            <HeaderButton
              variant="secondary"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => {
                setOffset(offset + PAGE_SIZE);
                setSelected(new Set());
              }}
            >
              Next
            </HeaderButton>
          </div>
        </div>
      )}

      <ConfirmModal
        open={pending !== null}
        tone="destructive"
        title="Deprecate stale IPs?"
        confirmLabel={
          pending?.kind === "all"
            ? `Deprecate all ${pending.count}`
            : `Deprecate ${pending?.kind === "selected" ? pending.ids.length : 0}`
        }
        loading={deprecate.isPending}
        message={
          pending?.kind === "all" ? (
            <>
              This flips <strong>all {pending.count}</strong> matching allocated
              IPs to <strong>deprecated</strong> in the current filter (
              {staleDays}+ days stale
              {includeNeverSeen ? ", including never-seen" : ""}). DHCP-lease
              mirrors and system rows are skipped. This is reversible — edit any
              row back from the IPAM page.
            </>
          ) : (
            <>
              This flips the{" "}
              <strong>
                {pending?.kind === "selected" ? pending.ids.length : 0}
              </strong>{" "}
              selected allocated IP(s) to <strong>deprecated</strong>. This is
              reversible — edit any row back from the IPAM page.
            </>
          )
        }
        onConfirm={confirmDeprecate}
        onClose={() => setPending(null)}
      />
    </div>
  );
}
