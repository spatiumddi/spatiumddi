import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ChevronRight,
  ChevronDown,
  Network,
  Layers,
  Plus,
  Trash2,
  Pencil,
  RefreshCw,
  X,
} from "lucide-react";
import { ipamApi, type IPSpace, type Subnet, type IPAddress } from "@/lib/api";
import { cn } from "@/lib/utils";

// ─── Status Badge ────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    active: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
    reserved: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
    deprecated: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
    quarantine: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400",
    allocated: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400",
    available: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
    dhcp: "bg-cyan-100 text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-400",
    static_dhcp: "bg-teal-100 text-teal-800 dark:bg-teal-900/30 dark:text-teal-400",
  };
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-xs font-medium",
        colors[status] ?? "bg-muted text-muted-foreground"
      )}
    >
      {status}
    </span>
  );
}

function UtilizationBar({ percent }: { percent: number }) {
  const color =
    percent >= 95
      ? "bg-red-500"
      : percent >= 80
        ? "bg-amber-400"
        : "bg-green-500";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-20 rounded-full bg-muted">
        <div
          className={cn("h-full rounded-full transition-all", color)}
          style={{ width: `${Math.min(percent, 100)}%` }}
        />
      </div>
      <span className="text-xs tabular-nums text-muted-foreground">
        {percent.toFixed(0)}%
      </span>
    </div>
  );
}

// ─── Modal helpers ────────────────────────────────────────────────────────────

function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="w-full max-w-md rounded-lg border bg-card p-6 shadow-lg">
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
      <label className="text-xs font-medium text-muted-foreground">{label}</label>
      {children}
    </div>
  );
}

const inputCls =
  "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

// ─── Create Space Modal ───────────────────────────────────────────────────────

function CreateSpaceModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const mutation = useMutation({
    mutationFn: () => ipamApi.createSpace({ name, description, is_default: false }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["spaces"] });
      onClose();
    },
  });
  return (
    <Modal title="New IP Space" onClose={onClose}>
      <div className="space-y-3">
        <Field label="Name">
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Corporate"
            autoFocus
          />
        </Field>
        <Field label="Description">
          <input
            className={inputCls}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional"
          />
        </Field>
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => mutation.mutate()}
            disabled={!name || mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Create Subnet Modal ──────────────────────────────────────────────────────

function CreateSubnetModal({
  spaceId,
  onClose,
}: {
  spaceId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [network, setNetwork] = useState("");
  const [name, setName] = useState("");
  const [gateway, setGateway] = useState("");
  const [vlanId, setVlanId] = useState("");
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      ipamApi.createSubnet({
        space_id: spaceId,
        network,
        name: name || undefined,
        gateway: gateway || undefined,
        vlan_id: vlanId ? parseInt(vlanId) : undefined,
        status: "active",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnets", spaceId] });
      qc.invalidateQueries({ queryKey: ["spaces"] });
      onClose();
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to create subnet";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  return (
    <Modal title="New Subnet" onClose={onClose}>
      <div className="space-y-3">
        <Field label="Network (CIDR)">
          <input
            className={inputCls}
            value={network}
            onChange={(e) => setNetwork(e.target.value)}
            placeholder="e.g. 10.0.1.0/24"
            autoFocus
          />
        </Field>
        <Field label="Name">
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Optional"
          />
        </Field>
        <Field label="Gateway">
          <input
            className={inputCls}
            value={gateway}
            onChange={(e) => setGateway(e.target.value)}
            placeholder="Auto-assigned if blank"
          />
        </Field>
        <Field label="VLAN ID">
          <input
            className={inputCls}
            value={vlanId}
            onChange={(e) => setVlanId(e.target.value)}
            placeholder="Optional"
            type="number"
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
            onClick={() => { setError(null); mutation.mutate(); }}
            disabled={!network || mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Add Address Modal ────────────────────────────────────────────────────────

function AddAddressModal({
  subnetId,
  onClose,
}: {
  subnetId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<"manual" | "next">("next");
  const [address, setAddress] = useState("");
  const [hostname, setHostname] = useState("");
  const [description, setDescription] = useState("");
  const [status, setStatus] = useState("allocated");
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => {
      if (mode === "next") {
        return ipamApi.nextAddress(subnetId, {
          hostname: hostname || undefined,
          description: description || undefined,
        });
      }
      return ipamApi.createAddress({
        subnet_id: subnetId,
        address,
        hostname: hostname || undefined,
        description: description || undefined,
        status,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["addresses", subnetId] });
      qc.invalidateQueries({ queryKey: ["subnets"] });
      onClose();
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to allocate address";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  return (
    <Modal title="Allocate IP Address" onClose={onClose}>
      <div className="space-y-3">
        <div className="flex gap-2">
          {(["next", "manual"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={cn(
                "flex-1 rounded-md border px-3 py-1.5 text-sm",
                mode === m
                  ? "bg-primary text-primary-foreground border-primary"
                  : "hover:bg-muted"
              )}
            >
              {m === "next" ? "Next available" : "Specific IP"}
            </button>
          ))}
        </div>
        {mode === "manual" && (
          <Field label="IP Address">
            <input
              className={inputCls}
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              placeholder="e.g. 10.0.1.42"
              autoFocus
            />
          </Field>
        )}
        <Field label="Hostname">
          <input
            className={inputCls}
            value={hostname}
            onChange={(e) => setHostname(e.target.value)}
            placeholder="Optional"
          />
        </Field>
        <Field label="Description">
          <input
            className={inputCls}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional"
          />
        </Field>
        {mode === "manual" && (
          <Field label="Status">
            <select
              className={inputCls}
              value={status}
              onChange={(e) => setStatus(e.target.value)}
            >
              {["allocated", "reserved", "deprecated"].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
        )}
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => { setError(null); mutation.mutate(); }}
            disabled={(mode === "manual" && !address) || mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Allocating…" : "Allocate"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Subnet Detail Panel (right pane) ────────────────────────────────────────

function SubnetDetail({ subnet }: { subnet: Subnet }) {
  const qc = useQueryClient();
  const [showAddModal, setShowAddModal] = useState(false);

  const { data: addresses, isLoading } = useQuery({
    queryKey: ["addresses", subnet.id],
    queryFn: () => ipamApi.listAddresses(subnet.id),
  });

  const deleteAddr = useMutation({
    mutationFn: (id: string) => ipamApi.deleteAddress(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["addresses", subnet.id] });
      qc.invalidateQueries({ queryKey: ["subnets"] });
    },
  });

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Header */}
      <div className="border-b px-6 py-4">
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-2">
              <span className="font-mono text-lg font-semibold">{subnet.network}</span>
              <StatusBadge status={subnet.status} />
            </div>
            {subnet.name && (
              <p className="mt-0.5 text-sm text-muted-foreground">{subnet.name}</p>
            )}
          </div>
          <button
            onClick={() => setShowAddModal(true)}
            className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
          >
            <Plus className="h-3.5 w-3.5" />
            Allocate IP
          </button>
        </div>

        {/* Subnet stats */}
        <div className="mt-3 flex flex-wrap gap-6 text-sm">
          {subnet.gateway && (
            <div>
              <span className="text-xs text-muted-foreground">Gateway</span>
              <p className="font-mono">{subnet.gateway}</p>
            </div>
          )}
          {subnet.vlan_id != null && (
            <div>
              <span className="text-xs text-muted-foreground">VLAN</span>
              <p>{subnet.vlan_id}</p>
            </div>
          )}
          <div>
            <span className="text-xs text-muted-foreground">Total IPs</span>
            <p>{subnet.total_ips}</p>
          </div>
          <div>
            <span className="text-xs text-muted-foreground">Allocated</span>
            <p>
              {subnet.allocated_ips} / {subnet.total_ips}
            </p>
          </div>
          <div>
            <span className="text-xs text-muted-foreground">Utilization</span>
            <div className="mt-1">
              <UtilizationBar percent={subnet.utilization_percent} />
            </div>
          </div>
        </div>
      </div>

      {/* IP Address table */}
      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <p className="p-6 text-sm text-muted-foreground">Loading addresses…</p>
        ) : !addresses?.length ? (
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <Network className="mb-3 h-10 w-10 text-muted-foreground/30" />
            <p className="text-sm text-muted-foreground">No IP addresses allocated yet.</p>
            <button
              onClick={() => setShowAddModal(true)}
              className="mt-3 flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
            >
              <Plus className="h-3.5 w-3.5" />
              Allocate first IP
            </button>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/40 text-xs">
                <th className="px-4 py-2.5 text-left font-medium">Address</th>
                <th className="px-4 py-2.5 text-left font-medium">Hostname</th>
                <th className="px-4 py-2.5 text-left font-medium">MAC</th>
                <th className="px-4 py-2.5 text-left font-medium">Status</th>
                <th className="px-4 py-2.5 text-left font-medium">Description</th>
                <th className="px-4 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {addresses.map((addr: IPAddress) => (
                <tr
                  key={addr.id}
                  className="border-b last:border-0 hover:bg-muted/20"
                >
                  <td className="px-4 py-2 font-mono font-medium">{addr.address}</td>
                  <td className="px-4 py-2 text-muted-foreground">
                    {addr.hostname ?? <span className="text-muted-foreground/40">—</span>}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs">
                    {addr.mac_address ?? <span className="text-muted-foreground/40">—</span>}
                  </td>
                  <td className="px-4 py-2">
                    <StatusBadge status={addr.status} />
                  </td>
                  <td className="px-4 py-2 text-muted-foreground">
                    {addr.description ?? <span className="text-muted-foreground/40">—</span>}
                  </td>
                  <td className="px-4 py-2 text-right">
                    <button
                      onClick={() => deleteAddr.mutate(addr.id)}
                      disabled={deleteAddr.isPending}
                      className="rounded p-1 text-muted-foreground hover:text-destructive"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showAddModal && (
        <AddAddressModal subnetId={subnet.id} onClose={() => setShowAddModal(false)} />
      )}
    </div>
  );
}

// ─── Subnet Row in tree ───────────────────────────────────────────────────────

function SubnetRow({
  subnet,
  isSelected,
  onSelect,
  onDelete,
}: {
  subnet: Subnet;
  isSelected: boolean;
  onSelect: () => void;
  onDelete: () => void;
}) {
  return (
    <div
      onClick={onSelect}
      className={cn(
        "group flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1.5 text-sm",
        isSelected
          ? "bg-primary/10 text-primary font-medium"
          : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
      )}
    >
      <div className="w-3.5 flex-shrink-0" />
      <Network className="h-3.5 w-3.5 flex-shrink-0" />
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate font-mono text-xs">{subnet.network}</span>
        {subnet.name && (
          <span className="truncate text-xs text-muted-foreground/60">{subnet.name}</span>
        )}
      </div>
      <button
        onClick={(e) => {
          e.stopPropagation();
          onDelete();
        }}
        className="hidden rounded p-0.5 text-muted-foreground hover:text-destructive group-hover:flex"
      >
        <Trash2 className="h-3 w-3" />
      </button>
    </div>
  );
}

// ─── Space Section in tree ────────────────────────────────────────────────────

// ─── Confirm Delete Modal ─────────────────────────────────────────────────────

function ConfirmDeleteModal({
  title,
  message,
  onConfirm,
  onClose,
  isPending,
}: {
  title: string;
  message: string;
  onConfirm: () => void;
  onClose: () => void;
  isPending?: boolean;
}) {
  return (
    <Modal title={title} onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-muted-foreground">{message}</p>
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={isPending}
            className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
          >
            {isPending ? "Deleting…" : "Delete"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Edit IP Space Modal (name/description + delete trigger) ─────────────────

function EditSpaceModal({
  space,
  onClose,
  onDeleted,
}: {
  space: IPSpace;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(space.name);
  const [description, setDescription] = useState(space.description ?? "");
  const [deleteStep, setDeleteStep] = useState<0 | 1 | 2>(0);
  const [deleteChecked, setDeleteChecked] = useState(false);

  const saveMutation = useMutation({
    mutationFn: () => ipamApi.updateSpace(space.id, { name, description }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["spaces"] });
      onClose();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => ipamApi.deleteSpace(space.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["spaces"] });
      onDeleted();
    },
  });

  // ── Delete step 1: first confirm ──
  if (deleteStep === 1) {
    return (
      <Modal title="Delete IP Space" onClose={() => setDeleteStep(0)}>
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Are you sure you want to delete{" "}
            <strong className="text-foreground">{space.name}</strong>? This will
            permanently delete all blocks, subnets, and IP addresses within it.
          </p>
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setDeleteStep(0)}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              Cancel
            </button>
            <button
              onClick={() => setDeleteStep(2)}
              className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90"
            >
              Continue
            </button>
          </div>
        </div>
      </Modal>
    );
  }

  // ── Delete step 2: final confirm with checkbox ──
  if (deleteStep === 2) {
    return (
      <Modal title="Confirm Permanent Deletion" onClose={() => setDeleteStep(0)}>
        <div className="space-y-4">
          <p className="text-sm font-medium text-destructive">
            This action cannot be undone.
          </p>
          <p className="text-sm text-muted-foreground">
            All subnets and IP address records in{" "}
            <strong className="text-foreground">{space.name}</strong> will be permanently
            removed from the database.
          </p>
          <label className="flex cursor-pointer items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={deleteChecked}
              onChange={(e) => setDeleteChecked(e.target.checked)}
            />
            I understand this will permanently delete all data in this IP space.
          </label>
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setDeleteStep(0)}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              Cancel
            </button>
            <button
              onClick={() => deleteMutation.mutate()}
              disabled={!deleteChecked || deleteMutation.isPending}
              className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
            >
              {deleteMutation.isPending ? "Deleting…" : "Delete permanently"}
            </button>
          </div>
        </div>
      </Modal>
    );
  }

  // ── Normal edit view ──
  return (
    <Modal title="Edit IP Space" onClose={onClose}>
      <div className="space-y-3">
        <Field label="Name">
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
            placeholder="Optional"
          />
        </Field>

        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => saveMutation.mutate()}
            disabled={!name || saveMutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {saveMutation.isPending ? "Saving…" : "Save"}
          </button>
        </div>

        {/* Delete zone */}
        <div className="mt-2 border-t pt-3">
          <button
            onClick={() => setDeleteStep(1)}
            className="text-xs text-destructive hover:underline"
          >
            Delete this IP space…
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Space Section in tree ────────────────────────────────────────────────────

function SpaceSection({
  space,
  selectedSubnetId,
  onSelectSubnet,
}: {
  space: IPSpace;
  selectedSubnetId: string | null;
  onSelectSubnet: (subnet: Subnet | null) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const [showCreateSubnet, setShowCreateSubnet] = useState(false);
  const [showEditSpace, setShowEditSpace] = useState(false);
  const [subnetToDelete, setSubnetToDelete] = useState<Subnet | null>(null);
  const qc = useQueryClient();

  const { data: subnets, isLoading } = useQuery({
    queryKey: ["subnets", space.id],
    queryFn: () => ipamApi.listSubnets({ space_id: space.id }),
    enabled: expanded,
  });

  const deleteSubnet = useMutation({
    mutationFn: (id: string) => ipamApi.deleteSubnet(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnets", space.id] });
      setSubnetToDelete(null);
    },
  });

  return (
    <div>
      {/* Space header */}
      <div className="flex items-center gap-1 rounded-md px-1 py-1.5 hover:bg-muted/50">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="flex flex-1 items-center gap-1 min-w-0"
        >
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5 flex-shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 flex-shrink-0 text-muted-foreground" />
          )}
          <Layers className="h-3.5 w-3.5 flex-shrink-0 text-muted-foreground" />
          <span className="flex-1 truncate text-left text-sm font-medium">{space.name}</span>
        </button>
        {/* Edit — always visible, left of + */}
        <button
          onClick={(e) => { e.stopPropagation(); setShowEditSpace(true); }}
          className="flex-shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground"
          title="Edit space"
        >
          <Pencil className="h-3 w-3" />
        </button>
        {/* Add subnet — always visible */}
        <button
          onClick={(e) => { e.stopPropagation(); setShowCreateSubnet(true); }}
          className="flex-shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground"
          title="Add subnet"
        >
          <Plus className="h-3 w-3" />
        </button>
      </div>

      {/* Subnets */}
      {expanded && (
        <div className="ml-3 space-y-0.5">
          {isLoading && (
            <p className="py-1 pl-4 text-xs text-muted-foreground">Loading…</p>
          )}
          {subnets?.length === 0 && (
            <p className="py-1 pl-4 text-xs text-muted-foreground">No subnets.</p>
          )}
          {subnets?.map((subnet: Subnet) => (
            <SubnetRow
              key={subnet.id}
              subnet={subnet}
              isSelected={selectedSubnetId === subnet.id}
              onSelect={() => onSelectSubnet(subnet)}
              onDelete={() => setSubnetToDelete(subnet)}
            />
          ))}
        </div>
      )}

      {showCreateSubnet && (
        <CreateSubnetModal
          spaceId={space.id}
          onClose={() => setShowCreateSubnet(false)}
        />
      )}

      {showEditSpace && (
        <EditSpaceModal
          space={space}
          onClose={() => setShowEditSpace(false)}
          onDeleted={() => {
            setShowEditSpace(false);
            onSelectSubnet(null);
          }}
        />
      )}

      {subnetToDelete && (
        <ConfirmDeleteModal
          title="Delete Subnet"
          message={`Delete subnet ${subnetToDelete.network}${subnetToDelete.name ? ` (${subnetToDelete.name})` : ""}? All IP address records within it will be permanently deleted.`}
          isPending={deleteSubnet.isPending}
          onClose={() => setSubnetToDelete(null)}
          onConfirm={() => {
            if (selectedSubnetId === subnetToDelete.id) onSelectSubnet(null);
            deleteSubnet.mutate(subnetToDelete.id);
          }}
        />
      )}
    </div>
  );
}

// ─── Main IPAM Page ───────────────────────────────────────────────────────────

export function IPAMPage() {
  const [selectedSubnet, setSelectedSubnet] = useState<Subnet | null>(null);
  const [showCreateSpace, setShowCreateSpace] = useState(false);
  const qc = useQueryClient();

  const { data: spaces, isLoading } = useQuery({
    queryKey: ["spaces"],
    queryFn: ipamApi.listSpaces,
  });

  return (
    <div className="flex h-full">
      {/* ── Left tree panel ── */}
      <div className="flex w-72 flex-shrink-0 flex-col border-r">
        <div className="flex h-12 items-center justify-between border-b px-3">
          <span className="text-sm font-semibold">IP Spaces</span>
          <div className="flex gap-1">
            <button
              onClick={() => qc.invalidateQueries({ queryKey: ["spaces"] })}
              className="rounded p-1 text-muted-foreground hover:text-foreground"
              title="Refresh"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setShowCreateSpace(true)}
              className="rounded p-1 text-muted-foreground hover:text-foreground"
              title="New IP Space"
            >
              <Plus className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-2 space-y-1">
          {isLoading && (
            <p className="px-2 py-3 text-xs text-muted-foreground">Loading…</p>
          )}
          {spaces?.length === 0 && !isLoading && (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <Layers className="mb-2 h-8 w-8 text-muted-foreground/30" />
              <p className="text-xs text-muted-foreground">No IP spaces yet.</p>
              <button
                onClick={() => setShowCreateSpace(true)}
                className="mt-2 text-xs text-primary hover:underline"
              >
                Create one
              </button>
            </div>
          )}
          {spaces?.map((space: IPSpace) => (
            <SpaceSection
              key={space.id}
              space={space}
              selectedSubnetId={selectedSubnet?.id ?? null}
              onSelectSubnet={(s) => setSelectedSubnet(s)}
            />
          ))}
        </div>
      </div>

      {/* ── Right detail panel ── */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {selectedSubnet ? (
          <SubnetDetail subnet={selectedSubnet} />
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center text-center">
            <Network className="mb-3 h-12 w-12 text-muted-foreground/20" />
            <p className="text-sm text-muted-foreground">
              Select a subnet from the tree to view its addresses.
            </p>
          </div>
        )}
      </div>

      {showCreateSpace && (
        <CreateSpaceModal onClose={() => setShowCreateSpace(false)} />
      )}
    </div>
  );
}
