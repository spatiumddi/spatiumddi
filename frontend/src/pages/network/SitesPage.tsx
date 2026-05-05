import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import {
  sitesApi,
  type SiteCreate,
  type SiteKind,
  type SiteRead,
  type SiteUpdate,
} from "@/lib/api";
import { cn, zebraBodyCls } from "@/lib/utils";
import { Modal } from "@/components/ui/modal";
import { HeaderButton } from "@/components/ui/header-button";

const inputCls =
  "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

const KINDS: SiteKind[] = [
  "datacenter",
  "branch",
  "pop",
  "colo",
  "cloud_region",
  "customer_premise",
];

const KIND_LABELS: Record<SiteKind, string> = {
  datacenter: "Datacenter",
  branch: "Branch",
  pop: "PoP",
  colo: "Colo",
  cloud_region: "Cloud region",
  customer_premise: "Customer premise",
};

function SiteEditorModal({
  existing,
  sites,
  onClose,
}: {
  existing: SiteRead | null;
  sites: SiteRead[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(existing?.name ?? "");
  const [code, setCode] = useState(existing?.code ?? "");
  const [kind, setKind] = useState<SiteKind>(existing?.kind ?? "datacenter");
  const [region, setRegion] = useState(existing?.region ?? "");
  const [parentId, setParentId] = useState(existing?.parent_site_id ?? "");
  const [notes, setNotes] = useState(existing?.notes ?? "");
  const [error, setError] = useState<string | null>(null);

  // Self + descendants are illegal parent picks (would form a cycle).
  const cycleBlockedIds = useMemo(() => {
    if (!existing) return new Set<string>();
    const blocked = new Set<string>([existing.id]);
    let added = true;
    while (added) {
      added = false;
      for (const s of sites) {
        if (
          !blocked.has(s.id) &&
          s.parent_site_id &&
          blocked.has(s.parent_site_id)
        ) {
          blocked.add(s.id);
          added = true;
        }
      }
    }
    return blocked;
  }, [existing, sites]);

  const mut = useMutation({
    mutationFn: async () => {
      if (!name.trim()) throw new Error("Name is required");
      if (existing) {
        const body: SiteUpdate = {
          name,
          code: code || null,
          kind,
          region: region || null,
          parent_site_id: parentId || null,
          notes,
        };
        return sitesApi.update(existing.id, body);
      }
      const body: SiteCreate = {
        name,
        code: code || null,
        kind,
        region: region || null,
        parent_site_id: parentId || null,
        notes,
      };
      return sitesApi.create(body);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sites"] });
      onClose();
    },
    onError: (e: unknown) => {
      const err = e as {
        message?: string;
        response?: { data?: { detail?: string } };
      };
      setError(err?.response?.data?.detail ?? err?.message ?? "Save failed");
    },
  });

  return (
    <Modal onClose={onClose} title={existing ? "Edit site" : "New site"} wide>
      <div className="space-y-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1 sm:col-span-2">
            <label className="text-xs font-medium text-muted-foreground">
              Name
            </label>
            <input
              className={inputCls}
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus={!existing}
              placeholder="NYC East Datacenter"
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">
              Code
            </label>
            <input
              className={inputCls}
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="DC-EAST"
            />
            <p className="text-[11px] text-muted-foreground/80">
              Unique within a parent. Use in CLI / runbooks.
            </p>
          </div>
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">
              Kind
            </label>
            <select
              className={inputCls}
              value={kind}
              onChange={(e) => setKind(e.target.value as SiteKind)}
            >
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {KIND_LABELS[k]}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">
              Region
            </label>
            <input
              className={inputCls}
              value={region}
              onChange={(e) => setRegion(e.target.value)}
              placeholder="us-east-1 / EMEA / NYC metro"
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">
              Parent site
            </label>
            <select
              className={inputCls}
              value={parentId}
              onChange={(e) => setParentId(e.target.value)}
            >
              <option value="">— none —</option>
              {sites
                .filter((s) => !cycleBlockedIds.has(s.id))
                .map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                    {s.code ? ` (${s.code})` : ""}
                  </option>
                ))}
            </select>
          </div>
          <div className="space-y-1 sm:col-span-2">
            <label className="text-xs font-medium text-muted-foreground">
              Notes
            </label>
            <textarea
              className={cn(inputCls, "min-h-[80px]")}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
            />
          </div>
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={mut.isPending}
            onClick={() => mut.mutate()}
            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mut.isPending ? "Saving…" : existing ? "Save" : "Create"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

export function SitesPage() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [kindFilter, setKindFilter] = useState<SiteKind | "">("");
  const [editing, setEditing] = useState<SiteRead | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  const query = useQuery({
    queryKey: ["sites", search, kindFilter],
    queryFn: () =>
      sitesApi.list({
        limit: 500,
        search: search || undefined,
        kind: (kindFilter || undefined) as SiteKind | undefined,
      }),
  });

  // For the parent picker — a separate fetch so that filter changes
  // on the table don't shrink the picker's options.
  const allSitesQuery = useQuery({
    queryKey: ["sites", "all"],
    queryFn: () => sitesApi.list({ limit: 500 }),
  });

  const items = query.data?.items ?? [];
  const allSites = allSitesQuery.data?.items ?? [];
  const siteName = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of allSites) m.set(s.id, s.name);
    return m;
  }, [allSites]);

  const allChecked = useMemo(
    () => items.length > 0 && items.every((c) => selectedIds.has(c.id)),
    [items, selectedIds],
  );

  const bulkDelete = useMutation({
    mutationFn: (ids: string[]) => sitesApi.bulkDelete(ids),
    onSuccess: () => {
      setSelectedIds(new Set());
      qc.invalidateQueries({ queryKey: ["sites"] });
    },
  });

  const removeOne = useMutation({
    mutationFn: (id: string) => sitesApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sites"] }),
  });

  function toggle(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    if (allChecked) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(items.map((c) => c.id)));
    }
  }

  return (
    <div className="h-full overflow-auto p-6">
      <div className="space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h1 className="text-xl font-semibold">Sites</h1>
            <p className="text-sm text-muted-foreground">
              Physical locations resources are deployed at. Hierarchical: a
              campus site can have building / floor sites under it. Distinct
              from CMDB — no rack / U / floor-plan tracking.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <HeaderButton
              icon={RefreshCw}
              onClick={() => query.refetch()}
              iconClassName={query.isFetching ? "animate-spin" : undefined}
            >
              Refresh
            </HeaderButton>
            <HeaderButton
              variant="primary"
              icon={Plus}
              onClick={() => setShowNew(true)}
            >
              New site
            </HeaderButton>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <input
            className={cn(inputCls, "max-w-xs")}
            placeholder="Search name / code / region…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select
            className={cn(inputCls, "max-w-[180px]")}
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value as SiteKind | "")}
          >
            <option value="">All kinds</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {KIND_LABELS[k]}
              </option>
            ))}
          </select>
        </div>

        {selectedIds.size > 0 && (
          <div className="flex items-center justify-between rounded-md border bg-muted/50 px-3 py-2 text-sm">
            <span>{selectedIds.size} selected</span>
            <HeaderButton
              variant="destructive"
              icon={Trash2}
              disabled={bulkDelete.isPending}
              onClick={() => {
                if (window.confirm(`Delete ${selectedIds.size} site(s)?`)) {
                  bulkDelete.mutate(Array.from(selectedIds));
                }
              }}
            >
              Delete selected
            </HeaderButton>
          </div>
        )}

        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-xs uppercase tracking-wider text-muted-foreground">
              <tr>
                <th className="w-8 px-3 py-2">
                  <input
                    type="checkbox"
                    checked={allChecked}
                    onChange={toggleAll}
                    aria-label="Select all"
                  />
                </th>
                <th className="px-3 py-2 text-left">Name</th>
                <th className="px-3 py-2 text-left">Code</th>
                <th className="px-3 py-2 text-left">Kind</th>
                <th className="px-3 py-2 text-left">Region</th>
                <th className="px-3 py-2 text-left">Parent</th>
                <th className="w-24 px-3 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className={zebraBodyCls}>
              {query.isLoading && (
                <tr>
                  <td
                    className="px-3 py-6 text-center text-muted-foreground"
                    colSpan={7}
                  >
                    Loading…
                  </td>
                </tr>
              )}
              {!query.isLoading && items.length === 0 && (
                <tr>
                  <td
                    className="px-3 py-6 text-center text-muted-foreground"
                    colSpan={7}
                  >
                    No sites yet — click "New site" to add one.
                  </td>
                </tr>
              )}
              {items.map((s) => (
                <tr key={s.id} className="border-t">
                  <td className="px-3 py-2 align-top">
                    <input
                      type="checkbox"
                      checked={selectedIds.has(s.id)}
                      onChange={() => toggle(s.id)}
                    />
                  </td>
                  <td className="px-3 py-2 align-top break-words font-medium">
                    {s.name}
                  </td>
                  <td className="px-3 py-2 align-top break-all text-muted-foreground tabular-nums">
                    {s.code ?? "—"}
                  </td>
                  <td className="px-3 py-2 align-top text-muted-foreground">
                    {KIND_LABELS[s.kind]}
                  </td>
                  <td className="px-3 py-2 align-top break-words text-muted-foreground">
                    {s.region ?? "—"}
                  </td>
                  <td className="px-3 py-2 align-top break-words text-muted-foreground">
                    {s.parent_site_id
                      ? (siteName.get(s.parent_site_id) ?? "—")
                      : "—"}
                  </td>
                  <td className="px-3 py-2 align-top text-right">
                    <button
                      type="button"
                      title="Edit"
                      onClick={() => setEditing(s)}
                      className="rounded p-1 hover:bg-muted"
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                    <button
                      type="button"
                      title="Delete"
                      onClick={() => {
                        if (window.confirm(`Delete site "${s.name}"?`)) {
                          removeOne.mutate(s.id);
                        }
                      }}
                      className="ml-1 rounded p-1 text-destructive hover:bg-destructive/10"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {showNew && (
          <SiteEditorModal
            existing={null}
            sites={allSites}
            onClose={() => setShowNew(false)}
          />
        )}
        {editing && (
          <SiteEditorModal
            existing={editing}
            sites={allSites}
            onClose={() => setEditing(null)}
          />
        )}
      </div>
    </div>
  );
}
