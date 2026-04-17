import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Router as RouterIcon,
  Plus,
  Pencil,
  Trash2,
  X,
  ChevronRight,
  ChevronDown,
  Tag,
} from "lucide-react";
import {
  vlansApi,
  ipamApi,
  type Router,
  type VLAN,
  type Subnet,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { useTableSort, SortableTh } from "@/lib/useTableSort";

const inputCls =
  "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

function Modal({
  title,
  onClose,
  children,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-2 sm:p-4">
      <div
        className={cn(
          "w-full rounded-lg border bg-card p-4 sm:p-6 shadow-lg max-h-[90vh] overflow-y-auto max-w-[95vw]",
          wide ? "sm:max-w-2xl" : "sm:max-w-md",
        )}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold">{title}</h2>
          <button
            onClick={onClose}
            className="rounded p-1 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs font-medium text-muted-foreground">
        {label}
      </label>
      {children}
    </div>
  );
}

type Selection =
  | { kind: "router"; id: string }
  | { kind: "vlan"; id: string; routerId: string }
  | null;

export function VLANsPage() {
  const [selection, setSelection] = useState<Selection>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [showCreateRouter, setShowCreateRouter] = useState(false);

  const { data: routers = [], isLoading } = useQuery({
    queryKey: ["vlans", "routers"],
    queryFn: vlansApi.listRouters,
  });

  function toggleExpand(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="flex h-[calc(100vh-3.5rem)]">
      {/* Sidebar */}
      <aside className="w-72 flex-shrink-0 border-r bg-card overflow-y-auto">
        <div className="flex items-center justify-between px-3 py-2 border-b">
          <h2 className="text-sm font-semibold">Routers</h2>
          <button
            onClick={() => setShowCreateRouter(true)}
            title="New Router"
            className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          >
            <Plus className="h-4 w-4" />
          </button>
        </div>
        <div className="p-2 space-y-1">
          {isLoading && (
            <p className="text-xs text-muted-foreground italic px-2">
              Loading…
            </p>
          )}
          {!isLoading && routers.length === 0 && (
            <p className="text-xs text-muted-foreground italic px-2 py-4">
              No routers yet. Click + to add one.
            </p>
          )}
          {routers.map((r) => (
            <RouterSidebarItem
              key={r.id}
              router={r}
              expanded={expanded.has(r.id)}
              selection={selection}
              onToggle={() => toggleExpand(r.id)}
              onSelect={setSelection}
            />
          ))}
        </div>
      </aside>

      {/* Main detail */}
      <main className="flex-1 overflow-y-auto p-6">
        {selection === null && (
          <div className="flex flex-col items-center justify-center h-full text-muted-foreground">
            <RouterIcon className="h-12 w-12 mb-2 opacity-40" />
            <p className="text-sm">Select a router or VLAN to view details.</p>
          </div>
        )}
        {selection?.kind === "router" && (
          <RouterDetail
            routerId={selection.id}
            onSelectVlan={(vlanId, routerId) =>
              setSelection({ kind: "vlan", id: vlanId, routerId })
            }
            onDeleted={() => setSelection(null)}
          />
        )}
        {selection?.kind === "vlan" && (
          <VLANDetail
            vlanId={selection.id}
            routerId={selection.routerId}
            onDeleted={() =>
              setSelection({ kind: "router", id: selection.routerId })
            }
          />
        )}
      </main>

      {showCreateRouter && (
        <CreateRouterModal onClose={() => setShowCreateRouter(false)} />
      )}
    </div>
  );
}

function RouterSidebarItem({
  router,
  expanded,
  selection,
  onToggle,
  onSelect,
}: {
  router: Router;
  expanded: boolean;
  selection: Selection;
  onToggle: () => void;
  onSelect: (s: Selection) => void;
}) {
  const { data: vlans = [] } = useQuery({
    queryKey: ["vlans", router.id],
    queryFn: () => vlansApi.listVlans(router.id),
    enabled: expanded,
  });
  const isActive = selection?.kind === "router" && selection.id === router.id;
  return (
    <div>
      <div
        className={cn(
          "flex items-center gap-1 rounded-md px-1 py-1 text-sm cursor-pointer",
          isActive
            ? "bg-primary/10 text-primary"
            : "hover:bg-accent hover:text-accent-foreground",
        )}
      >
        <button
          onClick={(e) => {
            e.stopPropagation();
            onToggle();
          }}
          className="rounded p-0.5 hover:bg-muted flex-shrink-0"
        >
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
        </button>
        <button
          onClick={() => onSelect({ kind: "router", id: router.id })}
          className="flex flex-1 items-center gap-2 min-w-0"
        >
          <RouterIcon className="h-4 w-4 flex-shrink-0 text-blue-600 dark:text-blue-400" />
          <span className="truncate">{router.name}</span>
          <span className="ml-auto text-[10px] text-muted-foreground">
            {vlans.length || ""}
          </span>
        </button>
      </div>
      {expanded && (
        <div className="ml-5 border-l pl-2 mt-0.5 space-y-0.5">
          {vlans.length === 0 && (
            <p className="text-[11px] text-muted-foreground italic py-1">
              No VLANs
            </p>
          )}
          {vlans.map((v) => {
            const isVSel = selection?.kind === "vlan" && selection.id === v.id;
            return (
              <button
                key={v.id}
                onClick={() =>
                  onSelect({ kind: "vlan", id: v.id, routerId: router.id })
                }
                className={cn(
                  "flex w-full items-center gap-2 rounded-md px-2 py-1 text-xs",
                  isVSel
                    ? "bg-primary/10 text-primary"
                    : "hover:bg-accent hover:text-accent-foreground",
                )}
              >
                <Tag className="h-3 w-3 flex-shrink-0 text-emerald-600 dark:text-emerald-400" />
                <span className="font-mono">{v.vlan_id}</span>
                <span className="truncate text-muted-foreground">{v.name}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function RouterDetail({
  routerId,
  onSelectVlan,
  onDeleted,
}: {
  routerId: string;
  onSelectVlan: (vlanId: string, routerId: string) => void;
  onDeleted: () => void;
}) {
  const qc = useQueryClient();
  const [showEdit, setShowEdit] = useState(false);
  const [showCreateVlan, setShowCreateVlan] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const { data: router } = useQuery({
    queryKey: ["vlans", "router", routerId],
    queryFn: () => vlansApi.getRouter(routerId),
  });
  const { data: vlans = [] } = useQuery({
    queryKey: ["vlans", routerId],
    queryFn: () => vlansApi.listVlans(routerId),
  });

  type VlanCol = "tag" | "name" | "description";
  const {
    sorted: sortedVlans,
    sort,
    toggle,
  } = useTableSort<VLAN, VlanCol>(
    vlans,
    { key: "tag", dir: "asc" },
    (row, key) => {
      if (key === "tag") return row.vlan_id;
      if (key === "name") return row.name ?? "";
      if (key === "description") return row.description ?? "";
      return "";
    },
  );

  const [deleteError, setDeleteError] = useState<string | null>(null);
  const deleteMut = useMutation({
    mutationFn: () => vlansApi.deleteRouter(routerId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["vlans", "routers"] });
      qc.invalidateQueries({ queryKey: ["vlans", routerId] });
      onDeleted();
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to delete router";
      setDeleteError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  if (!router) return <p className="text-sm text-muted-foreground">Loading…</p>;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <RouterIcon className="h-5 w-5 text-blue-600 dark:text-blue-400" />
          <h1 className="text-lg font-semibold">{router.name}</h1>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setShowEdit(true)}
            className="rounded-md border px-3 py-1 text-xs hover:bg-muted"
          >
            <Pencil className="h-3 w-3 inline mr-1" /> Edit
          </button>
          <button
            onClick={() => setConfirmDelete(true)}
            className="rounded-md border px-3 py-1 text-xs text-destructive hover:bg-destructive/10"
          >
            <Trash2 className="h-3 w-3 inline mr-1" /> Delete
          </button>
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs bg-muted/30 rounded-md p-3">
        <MetaRow label="Location" value={router.location || "—"} />
        <MetaRow label="Management IP" value={router.management_ip ?? "—"} />
        <MetaRow label="Vendor" value={router.vendor ?? "—"} />
        <MetaRow label="Model" value={router.model ?? "—"} />
        <MetaRow label="Description" value={router.description || "—"} span />
        {router.notes && <MetaRow label="Notes" value={router.notes} span />}
      </dl>

      <div className="border rounded-md">
        <div className="flex items-center justify-between border-b px-3 py-2">
          <h3 className="text-sm font-semibold">
            VLANs{" "}
            <span className="text-muted-foreground">({vlans.length})</span>
          </h3>
          <button
            onClick={() => setShowCreateVlan(true)}
            className="rounded-md border px-2 py-1 text-xs hover:bg-muted"
          >
            <Plus className="h-3 w-3 inline mr-1" /> New VLAN
          </button>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] text-xs">
            <thead className="text-muted-foreground bg-muted/30">
              <tr>
                <SortableTh
                  sortKey="tag"
                  sort={sort}
                  onSort={toggle}
                  className="w-16 px-3 py-1.5"
                >
                  Tag
                </SortableTh>
                <SortableTh
                  sortKey="name"
                  sort={sort}
                  onSort={toggle}
                  className="px-3 py-1.5"
                >
                  Name
                </SortableTh>
                <SortableTh
                  sortKey="description"
                  sort={sort}
                  onSort={toggle}
                  className="px-3 py-1.5"
                >
                  Description
                </SortableTh>
                <th className="text-right px-3 py-1.5 w-24 font-medium">
                  Subnets
                </th>
              </tr>
            </thead>
            <tbody>
              {vlans.length === 0 && (
                <tr>
                  <td
                    colSpan={4}
                    className="px-3 py-3 text-center text-muted-foreground italic"
                  >
                    No VLANs yet.
                  </td>
                </tr>
              )}
              {sortedVlans.map((v) => (
                <VLANRow
                  key={v.id}
                  vlan={v}
                  onClick={() => onSelectVlan(v.id, routerId)}
                />
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {showEdit && (
        <EditRouterModal router={router} onClose={() => setShowEdit(false)} />
      )}
      {showCreateVlan && (
        <CreateVLANModal
          routerId={routerId}
          onClose={() => setShowCreateVlan(false)}
        />
      )}
      {confirmDelete && (
        <Modal
          title="Delete Router"
          onClose={() => {
            setConfirmDelete(false);
            setDeleteError(null);
          }}
        >
          <div className="space-y-3">
            <p className="text-sm">
              Delete <strong>{router.name}</strong>? All {vlans.length} VLAN
              {vlans.length === 1 ? "" : "s"} under this router will be deleted.
              Deletion is blocked if any subnet still references a VLAN on this
              router — reassign those subnets first.
            </p>
            {deleteError && (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                {deleteError}
              </div>
            )}
            <div className="flex justify-end gap-2">
              <button
                onClick={() => {
                  setConfirmDelete(false);
                  setDeleteError(null);
                }}
                className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
              >
                Cancel
              </button>
              <button
                onClick={() => deleteMut.mutate()}
                disabled={deleteMut.isPending}
                className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
              >
                {deleteMut.isPending ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

function VLANRow({ vlan, onClick }: { vlan: VLAN; onClick: () => void }) {
  const { data: subnets = [] } = useQuery({
    queryKey: ["subnets-by-vlan", vlan.id],
    queryFn: () => ipamApi.listSubnets({ vlan_ref_id: vlan.id }),
  });
  return (
    <tr onClick={onClick} className="border-t cursor-pointer hover:bg-muted/40">
      <td className="px-3 py-1.5 font-mono">{vlan.vlan_id}</td>
      <td className="px-3 py-1.5">{vlan.name}</td>
      <td className="px-3 py-1.5 text-muted-foreground">
        {vlan.description || "—"}
      </td>
      <td className="px-3 py-1.5 text-right">{subnets.length}</td>
    </tr>
  );
}

function VLANDetail({
  vlanId,
  routerId,
  onDeleted,
}: {
  vlanId: string;
  routerId: string;
  onDeleted: () => void;
}) {
  const qc = useQueryClient();
  const [showEdit, setShowEdit] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const { data: vlan } = useQuery({
    queryKey: ["vlans", "vlan", vlanId],
    queryFn: () => vlansApi.getVlan(vlanId),
  });
  const { data: router } = useQuery({
    queryKey: ["vlans", "router", routerId],
    queryFn: () => vlansApi.getRouter(routerId),
  });
  const { data: subnets = [] } = useQuery({
    queryKey: ["subnets-by-vlan", vlanId],
    queryFn: () => ipamApi.listSubnets({ vlan_ref_id: vlanId }),
  });

  const [deleteError, setDeleteError] = useState<string | null>(null);
  const deleteMut = useMutation({
    mutationFn: () => vlansApi.deleteVlan(vlanId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["vlans", routerId] });
      qc.invalidateQueries({ queryKey: ["subnets-by-vlan", vlanId] });
      onDeleted();
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to delete VLAN";
      setDeleteError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  if (!vlan) return <p className="text-sm text-muted-foreground">Loading…</p>;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Tag className="h-5 w-5 text-emerald-600 dark:text-emerald-400" />
          <h1 className="text-lg font-semibold">
            VLAN <span className="font-mono">{vlan.vlan_id}</span> ·{" "}
            <span className="text-muted-foreground font-normal">
              {vlan.name}
            </span>
          </h1>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setShowEdit(true)}
            className="rounded-md border px-3 py-1 text-xs hover:bg-muted"
          >
            <Pencil className="h-3 w-3 inline mr-1" /> Edit
          </button>
          <button
            onClick={() => setConfirmDelete(true)}
            className="rounded-md border px-3 py-1 text-xs text-destructive hover:bg-destructive/10"
          >
            <Trash2 className="h-3 w-3 inline mr-1" /> Delete
          </button>
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs bg-muted/30 rounded-md p-3">
        <MetaRow label="Router" value={router?.name ?? "—"} />
        <MetaRow label="Tag" value={String(vlan.vlan_id)} />
        <MetaRow label="Description" value={vlan.description || "—"} span />
      </dl>

      <div className="border rounded-md">
        <div className="border-b px-3 py-2">
          <h3 className="text-sm font-semibold">
            Subnets{" "}
            <span className="text-muted-foreground">({subnets.length})</span>
          </h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] text-xs">
            <thead className="text-muted-foreground bg-muted/30">
              <tr>
                <th className="text-left px-3 py-1.5">Network</th>
                <th className="text-left px-3 py-1.5">Name</th>
                <th className="text-left px-3 py-1.5">Status</th>
              </tr>
            </thead>
            <tbody>
              {subnets.length === 0 && (
                <tr>
                  <td
                    colSpan={3}
                    className="px-3 py-3 text-center text-muted-foreground italic"
                  >
                    No subnets reference this VLAN.
                  </td>
                </tr>
              )}
              {subnets.map((s: Subnet) => (
                <tr key={s.id} className="border-t">
                  <td className="px-3 py-1.5 font-mono">{s.network}</td>
                  <td className="px-3 py-1.5">{s.name || "—"}</td>
                  <td className="px-3 py-1.5 text-muted-foreground">
                    {s.status}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {showEdit && (
        <EditVLANModal
          vlan={vlan}
          routerId={routerId}
          onClose={() => setShowEdit(false)}
        />
      )}
      {confirmDelete && (
        <Modal
          title="Delete VLAN"
          onClose={() => {
            setConfirmDelete(false);
            setDeleteError(null);
          }}
        >
          <div className="space-y-3">
            {subnets.length > 0 ? (
              <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
                <p className="font-medium">
                  Cannot delete: {subnets.length} subnet
                  {subnets.length === 1 ? "" : "s"} still reference this VLAN.
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  Reassign or clear the VLAN on the subnets below first, then
                  try again.
                </p>
                <ul className="mt-2 max-h-32 list-disc overflow-auto pl-5 text-xs">
                  {subnets.map((s) => (
                    <li key={s.id} className="font-mono">
                      {s.network}
                      {s.name && (
                        <span className="text-muted-foreground">
                          {" "}
                          — {s.name}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="text-sm">
                Delete VLAN <strong>{vlan.vlan_id}</strong> ({vlan.name})? No
                subnets currently reference it.
              </p>
            )}
            {deleteError && (
              <p className="text-sm text-destructive">{deleteError}</p>
            )}
            <div className="flex justify-end gap-2">
              <button
                onClick={() => {
                  setConfirmDelete(false);
                  setDeleteError(null);
                }}
                className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
              >
                {subnets.length > 0 ? "Close" : "Cancel"}
              </button>
              {subnets.length === 0 && (
                <button
                  onClick={() => deleteMut.mutate()}
                  disabled={deleteMut.isPending}
                  className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
                >
                  {deleteMut.isPending ? "Deleting…" : "Delete"}
                </button>
              )}
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

function MetaRow({
  label,
  value,
  span,
}: {
  label: string;
  value: string;
  span?: boolean;
}) {
  return (
    <div className={cn(span ? "col-span-2" : "")}>
      <dt className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground/70">
        {label}
      </dt>
      <dd className="text-foreground">{value}</dd>
    </div>
  );
}

// ── Modals ──────────────────────────────────────────────────────────────────

function CreateRouterModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  return (
    <RouterFormModal
      title="New Router"
      initial={{}}
      onClose={onClose}
      onSubmit={async (data) => {
        await vlansApi.createRouter(data);
        qc.invalidateQueries({ queryKey: ["vlans", "routers"] });
        onClose();
      }}
    />
  );
}

function EditRouterModal({
  router,
  onClose,
}: {
  router: Router;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  return (
    <RouterFormModal
      title={`Edit ${router.name}`}
      initial={router}
      onClose={onClose}
      onSubmit={async (data) => {
        await vlansApi.updateRouter(router.id, data);
        qc.invalidateQueries({ queryKey: ["vlans", "routers"] });
        qc.invalidateQueries({ queryKey: ["vlans", "router", router.id] });
        onClose();
      }}
    />
  );
}

function RouterFormModal({
  title,
  initial,
  onClose,
  onSubmit,
}: {
  title: string;
  initial: Partial<Router>;
  onClose: () => void;
  onSubmit: (data: Partial<Router>) => Promise<void>;
}) {
  const [name, setName] = useState(initial.name ?? "");
  const [description, setDescription] = useState(initial.description ?? "");
  const [location, setLocation] = useState(initial.location ?? "");
  const [managementIp, setManagementIp] = useState(initial.management_ip ?? "");
  const [vendor, setVendor] = useState(initial.vendor ?? "");
  const [model, setModel] = useState(initial.model ?? "");
  const [notes, setNotes] = useState(initial.notes ?? "");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit() {
    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit({
        name: name.trim(),
        description,
        location,
        management_ip: managementIp || null,
        vendor: vendor || null,
        model: model || null,
        notes,
      });
    } catch (e: unknown) {
      const msg =
        (e as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to save";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal title={title} onClose={onClose} wide>
      <div className="space-y-3">
        <Field label="Name *">
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
        </Field>
        <Field label="Description">
          <input
            className={inputCls}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Location">
            <input
              className={inputCls}
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              placeholder="Site / Rack"
            />
          </Field>
          <Field label="Management IP">
            <input
              className={inputCls}
              value={managementIp}
              onChange={(e) => setManagementIp(e.target.value)}
              placeholder="10.0.0.1"
            />
          </Field>
          <Field label="Vendor">
            <input
              className={inputCls}
              value={vendor}
              onChange={(e) => setVendor(e.target.value)}
            />
          </Field>
          <Field label="Model">
            <input
              className={inputCls}
              value={model}
              onChange={(e) => setModel(e.target.value)}
            />
          </Field>
        </div>
        <Field label="Notes">
          <textarea
            className={cn(inputCls, "min-h-[80px]")}
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
          />
        </Field>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {submitting ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function CreateVLANModal({
  routerId,
  onClose,
}: {
  routerId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  return (
    <VLANFormModal
      title="New VLAN"
      initial={{}}
      onClose={onClose}
      onSubmit={async (data) => {
        await vlansApi.createVlan(routerId, {
          vlan_id: Number(data.vlan_id),
          name: data.name ?? "",
          description: data.description,
        });
        qc.invalidateQueries({ queryKey: ["vlans", routerId] });
        onClose();
      }}
    />
  );
}

function EditVLANModal({
  vlan,
  routerId,
  onClose,
}: {
  vlan: VLAN;
  routerId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  return (
    <VLANFormModal
      title={`Edit VLAN ${vlan.vlan_id}`}
      initial={vlan}
      onClose={onClose}
      onSubmit={async (data) => {
        await vlansApi.updateVlan(vlan.id, {
          vlan_id: Number(data.vlan_id),
          name: data.name,
          description: data.description,
        });
        qc.invalidateQueries({ queryKey: ["vlans", routerId] });
        qc.invalidateQueries({ queryKey: ["vlans", "vlan", vlan.id] });
        onClose();
      }}
    />
  );
}

function VLANFormModal({
  title,
  initial,
  onClose,
  onSubmit,
}: {
  title: string;
  initial: Partial<VLAN>;
  onClose: () => void;
  onSubmit: (data: {
    vlan_id: number | string;
    name: string;
    description?: string;
  }) => Promise<void>;
}) {
  const [tag, setTag] = useState(initial.vlan_id?.toString() ?? "");
  const [name, setName] = useState(initial.name ?? "");
  const [description, setDescription] = useState(initial.description ?? "");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit() {
    const n = parseInt(tag, 10);
    if (Number.isNaN(n) || n < 1 || n > 4094) {
      setError("VLAN tag must be between 1 and 4094");
      return;
    }
    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit({ vlan_id: n, name: name.trim(), description });
    } catch (e: unknown) {
      const msg =
        (e as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to save";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal title={title} onClose={onClose}>
      <div className="space-y-3">
        <Field label="VLAN Tag * (1–4094)">
          <input
            className={inputCls}
            type="number"
            value={tag}
            onChange={(e) => setTag(e.target.value)}
            autoFocus
          />
        </Field>
        <Field label="Name *">
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <Field label="Description">
          <input
            className={inputCls}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {submitting ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
