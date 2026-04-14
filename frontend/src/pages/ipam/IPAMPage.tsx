import { useState, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import {
  ChevronRight,
  Network,
  Layers,
  Plus,
  Trash2,
  Pencil,
  RefreshCw,
  X,
  Copy,
  Check,
} from "lucide-react";
import { ipamApi, type IPSpace, type IPBlock, type Subnet, type IPAddress } from "@/lib/api";
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
    network: "bg-zinc-100 text-zinc-500 dark:bg-zinc-800/50 dark:text-zinc-400",
    broadcast: "bg-zinc-100 text-zinc-500 dark:bg-zinc-800/50 dark:text-zinc-400",
    orphan: "bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400",
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

function UtilizationDot({ percent }: { percent: number }) {
  const color =
    percent >= 95
      ? "bg-red-500"
      : percent >= 80
        ? "bg-amber-400"
        : "bg-green-500";
  return (
    <span
      title={`${percent.toFixed(0)}% utilized`}
      className={cn("inline-block h-2 w-2 flex-shrink-0 rounded-full", color)}
    />
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  function handleCopy(e: React.MouseEvent) {
    e.stopPropagation();
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }
  return (
    <button
      onClick={handleCopy}
      title="Copy to clipboard"
      className="ml-1 rounded p-0.5 text-muted-foreground/0 hover:text-muted-foreground group-hover/addr:text-muted-foreground/60 hover:!text-foreground transition-colors"
    >
      {copied
        ? <Check className="h-3 w-3 text-green-500" />
        : <Copy className="h-3 w-3" />
      }
    </button>
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
  defaultBlockId,
  onClose,
}: {
  spaceId: string;
  defaultBlockId?: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [network, setNetwork] = useState("");
  const [name, setName] = useState("");
  const [blockId, setBlockId] = useState(defaultBlockId ?? "");
  const [gateway, setGateway] = useState("");
  const [vlanId, setVlanId] = useState("");
  const [skipAuto, setSkipAuto] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: blocks } = useQuery({
    queryKey: ["blocks", spaceId],
    queryFn: () => ipamApi.listBlocks(spaceId),
  });

  // Auto-select if only one block
  useEffect(() => {
    if (!blockId && blocks?.length === 1) setBlockId(blocks[0].id);
  }, [blocks, blockId]);

  const mutation = useMutation({
    mutationFn: () =>
      ipamApi.createSubnet({
        space_id: spaceId,
        block_id: blockId,
        network,
        name: name || undefined,
        gateway: gateway || undefined,
        vlan_id: vlanId ? parseInt(vlanId) : undefined,
        status: "active",
        skip_auto_addresses: skipAuto,
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
        <Field label="Block *">
          <select className={inputCls} value={blockId} onChange={(e) => setBlockId(e.target.value)}>
            <option value="">Select a block…</option>
            {blocks?.map((b: IPBlock) => (
              <option key={b.id} value={b.id}>
                {b.network}{b.name ? ` — ${b.name}` : ""}
              </option>
            ))}
          </select>
          {blocks?.length === 0 && (
            <p className="text-xs text-amber-600 mt-1">No blocks in this space. Create a block first.</p>
          )}
        </Field>
        <Field label="Network (CIDR)">
          <input
            className={inputCls}
            value={network}
            onChange={(e) => { setNetwork(e.target.value); setError(null); }}
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
            disabled={skipAuto}
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
        <label className="flex items-center gap-2 text-xs text-muted-foreground cursor-pointer">
          <input
            type="checkbox"
            checked={skipAuto}
            onChange={(e) => setSkipAuto(e.target.checked)}
            className="rounded"
          />
          Skip network / broadcast / gateway records (loopback, P2P)
        </label>
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
            disabled={!network || !blockId || mutation.isPending}
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

const IP_STATUS_OPTIONS = ["allocated", "reserved", "dhcp", "static_dhcp", "deprecated"] as const;

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
  const [mac, setMac] = useState("");
  const [description, setDescription] = useState("");
  const [ipStatus, setIpStatus] = useState("allocated");
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => {
      if (mode === "next") {
        return ipamApi.nextAddress(subnetId, {
          hostname,
          status: ipStatus,
          mac_address: mac || undefined,
          description: description || undefined,
        });
      }
      return ipamApi.createAddress({
        subnet_id: subnetId,
        address,
        hostname,
        mac_address: mac || undefined,
        description: description || undefined,
        status: ipStatus,
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

  const canSubmit = !!hostname.trim() && (mode === "next" || !!address);

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
        <Field label="Hostname *">
          <input
            className={inputCls}
            value={hostname}
            onChange={(e) => setHostname(e.target.value)}
            placeholder="Required"
            autoFocus={mode === "next"}
          />
        </Field>
        <div className="grid grid-cols-2 gap-2">
          <Field label="MAC Address">
            <input
              className={inputCls}
              value={mac}
              onChange={(e) => setMac(e.target.value)}
              placeholder="e.g. aa:bb:cc:dd:ee:ff"
            />
          </Field>
          <Field label="Type / Status">
            <select
              className={inputCls}
              value={ipStatus}
              onChange={(e) => setIpStatus(e.target.value)}
            >
              {IP_STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Description">
          <input
            className={inputCls}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional"
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
            disabled={!canSubmit || mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Allocating…" : "Allocate"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Colored breadcrumb pills ─────────────────────────────────────────────────

type PillVariant = "space" | "block" | "subnet";

const PILL_STYLES: Record<PillVariant, string> = {
  space: "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300 hover:bg-blue-200 dark:hover:bg-blue-800/50",
  block: "bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300 hover:bg-violet-200 dark:hover:bg-violet-800/50",
  subnet: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300 cursor-default",
};

interface BreadcrumbItem {
  label: string;
  variant: PillVariant;
  onClick?: () => void;
}

function BreadcrumbPills({ items }: { items: BreadcrumbItem[] }) {
  // Compress if more than 4 items
  let visible = items;
  if (items.length > 4) {
    visible = [items[0], { label: "…", variant: items[1].variant }, ...items.slice(-2)];
  }
  return (
    <div className="mb-2 flex flex-wrap items-center gap-1">
      {visible.map((item, i) => (
        <span key={i} className="contents">
          {item.label === "…" ? (
            <span className="px-1 text-xs text-muted-foreground/60">…</span>
          ) : (
            <button
              onClick={item.onClick}
              disabled={!item.onClick}
              className={cn(
                "rounded-full px-2 py-0.5 text-xs font-medium transition-colors",
                PILL_STYLES[item.variant]
              )}
            >
              {item.label}
            </button>
          )}
          {i < visible.length - 1 && item.label !== "…" && visible[i + 1]?.label !== "…" && (
            <ChevronRight className="h-3 w-3 flex-shrink-0 text-muted-foreground/40" />
          )}
        </span>
      ))}
    </div>
  );
}

// ─── Subnet Detail Panel (right pane) ────────────────────────────────────────

function SubnetDetail({
  subnet,
  spaceName,
  block,
  blockAncestors,
  onSelectSpace,
  onSelectBlock,
  onSubnetEdited,
}: {
  subnet: Subnet;
  spaceName?: string;
  block?: IPBlock;
  blockAncestors?: IPBlock[];
  onSelectSpace?: () => void;
  onSelectBlock?: (b: IPBlock) => void;
  onSubnetEdited: (updated: Subnet) => void;
}) {
  const qc = useQueryClient();
  const [showAddModal, setShowAddModal] = useState(false);
  const [showEditSubnet, setShowEditSubnet] = useState(false);
  const [editingAddress, setEditingAddress] = useState<IPAddress | null>(null);
  const [colFilters, setColFilters] = useState({ address: "", hostname: "", mac: "", status: "", description: "" });

  // Clear column filters whenever the viewed subnet changes
  useEffect(() => {
    setColFilters({ address: "", hostname: "", mac: "", status: "", description: "" });
  }, [subnet.id]);

  const { data: addresses, isLoading } = useQuery({
    queryKey: ["addresses", subnet.id],
    queryFn: () => ipamApi.listAddresses(subnet.id),
  });

  const filteredAddresses = addresses?.filter((a) => {
    const cf = colFilters;
    if (cf.address && !a.address.toLowerCase().includes(cf.address.toLowerCase())) return false;
    if (cf.hostname && !(a.hostname ?? "").toLowerCase().includes(cf.hostname.toLowerCase())) return false;
    if (cf.mac && !(a.mac_address ?? "").toLowerCase().replace(/[:\-\.]/g, "").includes(cf.mac.toLowerCase().replace(/[:\-\.]/g, ""))) return false;
    if (cf.status && !a.status.toLowerCase().includes(cf.status.toLowerCase())) return false;
    if (cf.description && !(a.description ?? "").toLowerCase().includes(cf.description.toLowerCase())) return false;
    return true;
  });
  const hasActiveFilter = Object.values(colFilters).some(Boolean);

  const [confirmDeleteAddr, setConfirmDeleteAddr] = useState<IPAddress | null>(null);
  const [confirmPurgeAddr, setConfirmPurgeAddr] = useState<IPAddress | null>(null);

  const deleteAddr = useMutation({
    mutationFn: (id: string) => ipamApi.deleteAddress(id),  // soft-delete → orphan
    onSuccess: () => {
      setConfirmDeleteAddr(null);
      qc.invalidateQueries({ queryKey: ["addresses", subnet.id] });
      qc.invalidateQueries({ queryKey: ["subnets"] });
    },
  });

  const purgeAddr = useMutation({
    mutationFn: (id: string) => ipamApi.deleteAddress(id, true),  // permanent
    onSuccess: () => {
      setConfirmPurgeAddr(null);
      qc.invalidateQueries({ queryKey: ["addresses", subnet.id] });
      qc.invalidateQueries({ queryKey: ["subnets"] });
    },
  });

  const restoreAddr = useMutation({
    mutationFn: (id: string) => ipamApi.updateAddress(id, { status: "allocated" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["addresses", subnet.id] });
      qc.invalidateQueries({ queryKey: ["subnets"] });
    },
  });

  // Non-editable statuses (infrastructure or orphaned addresses)
  const isReadOnly = (status: string) =>
    status === "network" || status === "broadcast" || status === "orphan";

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Header */}
      <div className="border-b px-6 py-3">
        {/* Breadcrumb pills */}
        {spaceName && (() => {
          const crumbs: BreadcrumbItem[] = [
            { label: spaceName, variant: "space", onClick: onSelectSpace },
            ...(blockAncestors ?? []).map((b): BreadcrumbItem => ({
              label: b.network + (b.name ? ` (${b.name})` : ""),
              variant: "block",
              onClick: onSelectBlock ? () => onSelectBlock(b) : undefined,
            })),
            ...(block ? [{
              label: block.network + (block.name ? ` (${block.name})` : ""),
              variant: "block" as const,
              onClick: onSelectBlock ? () => onSelectBlock(block) : undefined,
            }] : []),
            { label: subnet.network + (subnet.name ? ` (${subnet.name})` : ""), variant: "subnet" },
          ];
          return <BreadcrumbPills items={crumbs} />;
        })()}
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-2">
              <span className="font-mono text-lg font-semibold">{subnet.network}</span>
              <StatusBadge status={subnet.status} />
              <button
                onClick={() => setShowEditSubnet(true)}
                className="rounded p-1 text-muted-foreground hover:text-foreground"
                title="Edit subnet"
              >
                <Pencil className="h-3.5 w-3.5" />
              </button>
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

      {/* Per-column filter cleared button */}
      {hasActiveFilter && (
        <div className="flex items-center justify-end border-b px-6 py-1">
          <button
            onClick={() => setColFilters({ address: "", hostname: "", mac: "", status: "", description: "" })}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <X className="h-3 w-3" />
            Clear filters
          </button>
        </div>
      )}

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
              <tr className="bg-muted/40 text-xs">
                <th className="px-4 pt-2.5 pb-1 text-left font-medium">Address</th>
                <th className="px-4 pt-2.5 pb-1 text-left font-medium">Hostname</th>
                <th className="px-4 pt-2.5 pb-1 text-left font-medium">MAC</th>
                <th className="px-4 pt-2.5 pb-1 text-left font-medium">Status</th>
                <th className="px-4 pt-2.5 pb-1 text-left font-medium">Description</th>
                <th className="px-4 pt-2.5 pb-1" />
              </tr>
              <tr className="border-b bg-muted/40">
                {(["address", "hostname", "mac", "status", "description"] as const).map((col) => (
                  <td key={col} className="px-4 pb-2">
                    <div className="relative">
                      <input
                        type="text"
                        value={colFilters[col]}
                        onChange={(e) => setColFilters((prev) => ({ ...prev, [col]: e.target.value }))}
                        placeholder="Filter…"
                        className="w-full rounded border bg-background px-2 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                      />
                      {colFilters[col] && (
                        <button
                          onClick={() => setColFilters((prev) => ({ ...prev, [col]: "" }))}
                          className="absolute right-1 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                        >
                          <X className="h-2.5 w-2.5" />
                        </button>
                      )}
                    </div>
                  </td>
                ))}
                <td className="px-4 pb-2" />
              </tr>
            </thead>
            <tbody>
              {filteredAddresses?.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-6 text-center text-sm text-muted-foreground">
                    No addresses match the active filters.
                  </td>
                </tr>
              )}
              {filteredAddresses?.map((addr: IPAddress) => (
                <tr
                  key={addr.id}
                  className={cn(
                    "group/addr border-b last:border-0 hover:bg-muted/20",
                    (addr.status === "network" || addr.status === "broadcast") && "opacity-50",
                    addr.status === "orphan" && "opacity-40 line-through-[addresses]"
                  )}
                >
                  <td className="px-4 py-2 font-mono font-medium">
                    <span className="inline-flex items-center gap-0.5">
                      {addr.address}
                      <CopyButton text={addr.address} />
                    </span>
                  </td>
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
                    <div className="flex items-center justify-end gap-1">
                      {addr.status === "orphan" ? (
                        <>
                          <button
                            onClick={() => restoreAddr.mutate(addr.id)}
                            disabled={restoreAddr.isPending}
                            className="rounded p-1 text-xs text-muted-foreground hover:text-green-600"
                            title="Restore (mark as allocated)"
                          >
                            <RefreshCw className="h-3.5 w-3.5" />
                          </button>
                          <button
                            onClick={() => setConfirmPurgeAddr(addr)}
                            className="rounded p-1 text-muted-foreground hover:text-destructive"
                            title="Permanently delete"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </>
                      ) : !isReadOnly(addr.status) ? (
                        <>
                          <button
                            onClick={() => setEditingAddress(addr)}
                            className="rounded p-1 text-muted-foreground hover:text-foreground"
                            title="Edit"
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </button>
                          <button
                            onClick={() => setConfirmDeleteAddr(addr)}
                            className="rounded p-1 text-muted-foreground hover:text-destructive"
                            title="Delete"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </>
                      ) : null}
                    </div>
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
      {showEditSubnet && (
        <EditSubnetModal
          subnet={subnet}
          onClose={(updated) => {
            setShowEditSubnet(false);
            if (updated) onSubnetEdited(updated);
          }}
        />
      )}
      {editingAddress && (
        <EditAddressModal
          address={editingAddress}
          onClose={() => setEditingAddress(null)}
        />
      )}
      {confirmDeleteAddr && (
        <ConfirmDeleteModal
          title="Delete IP Address"
          message={`Mark ${confirmDeleteAddr.address} as orphaned? The record will be kept and can be restored or permanently deleted later.`}
          confirmLabel="Mark as Orphan"
          onConfirm={() => deleteAddr.mutate(confirmDeleteAddr.id)}
          onClose={() => setConfirmDeleteAddr(null)}
          isPending={deleteAddr.isPending}
        />
      )}
      {confirmPurgeAddr && (
        <ConfirmDeleteModal
          title="Permanently Delete"
          message={`Permanently delete ${confirmPurgeAddr.address}? This cannot be undone.`}
          confirmLabel="Delete Forever"
          onConfirm={() => purgeAddr.mutate(confirmPurgeAddr.id)}
          onClose={() => setConfirmPurgeAddr(null)}
          isPending={purgeAddr.isPending}
        />
      )}
    </div>
  );
}

// ─── Edit Subnet Modal ────────────────────────────────────────────────────────

const SUBNET_STATUSES = ["active", "reserved", "deprecated", "quarantine"] as const;

function EditSubnetModal({
  subnet,
  onClose,
}: {
  subnet: Subnet;
  onClose: (updated?: Subnet) => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(subnet.name ?? "");
  const [description, setDescription] = useState(subnet.description ?? "");
  const [gateway, setGateway] = useState(subnet.gateway ?? "");
  const [vlanId, setVlanId] = useState(subnet.vlan_id?.toString() ?? "");
  const [status, setStatus] = useState(subnet.status);
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      ipamApi.updateSubnet(subnet.id, {
        name: name || undefined,
        description,
        gateway: gateway || undefined,
        vlan_id: vlanId ? parseInt(vlanId) : null,
        status,
      }),
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ["subnets", subnet.space_id] });
      onClose(updated);
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to save";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  return (
    <Modal title={`Edit ${subnet.network}`} onClose={() => onClose()}>
      <div className="space-y-3">
        <Field label="Name">
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Optional"
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
        <Field label="Gateway">
          <input
            className={inputCls}
            value={gateway}
            onChange={(e) => setGateway(e.target.value)}
            placeholder="e.g. 10.0.1.1"
          />
        </Field>
        <Field label="VLAN ID">
          <input
            className={inputCls}
            value={vlanId}
            onChange={(e) => setVlanId(e.target.value)}
            type="number"
            placeholder="Optional"
          />
        </Field>
        <Field label="Status">
          <select
            className={inputCls}
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            {SUBNET_STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </Field>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button onClick={() => onClose()} className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted">
            Cancel
          </button>
          <button
            onClick={() => { setError(null); mutation.mutate(); }}
            disabled={mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Edit Address Modal ───────────────────────────────────────────────────────

const ADDRESS_STATUSES = ["allocated", "reserved", "deprecated", "static_dhcp", "dhcp"] as const;

function EditAddressModal({
  address,
  onClose,
}: {
  address: IPAddress;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [hostname, setHostname] = useState(address.hostname ?? "");
  const [description, setDescription] = useState(address.description ?? "");
  const [macAddress, setMacAddress] = useState(address.mac_address ?? "");
  const [status, setStatus] = useState(address.status);
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      ipamApi.updateAddress(address.id, {
        hostname: hostname || undefined,
        description: description || undefined,
        mac_address: macAddress || undefined,
        status,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["addresses", address.subnet_id] });
      onClose();
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to save";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  return (
    <Modal title={`Edit ${address.address}`} onClose={onClose}>
      <div className="space-y-3">
        <Field label="Hostname">
          <input
            className={inputCls}
            value={hostname}
            onChange={(e) => setHostname(e.target.value)}
            placeholder="Optional"
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
        <Field label="MAC Address">
          <input
            className={inputCls}
            value={macAddress}
            onChange={(e) => setMacAddress(e.target.value)}
            placeholder="e.g. 00:1a:2b:3c:4d:5e"
          />
        </Field>
        <Field label="Status">
          <select
            className={inputCls}
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            {ADDRESS_STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </Field>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button onClick={onClose} className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted">
            Cancel
          </button>
          <button
            onClick={() => { setError(null); mutation.mutate(); }}
            disabled={mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Subnet Row in tree ───────────────────────────────────────────────────────

function SubnetRow({
  subnet,
  isSelected,
  onSelect,
  onDelete,
  onEdited,
}: {
  subnet: Subnet;
  isSelected: boolean;
  onSelect: () => void;
  onDelete: () => void;
  onEdited: (updated: Subnet) => void;
}) {
  const [showEdit, setShowEdit] = useState(false);

  return (
    <>
      <div
        onClick={onSelect}
        className={cn(
          "group flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1.5 text-sm",
          isSelected
            ? "bg-primary/10 text-primary font-medium"
            : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
        )}
      >
        {/* leaf node box indicator */}
        <div className="flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-sm border border-border/30 bg-background text-[10px] text-muted-foreground/30">
          ·
        </div>
        <Network className="h-3.5 w-3.5 flex-shrink-0" />
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="truncate font-mono text-xs">{subnet.network}</span>
          {subnet.name && (
            <span className="truncate text-xs text-muted-foreground/60">{subnet.name}</span>
          )}
        </div>
        <UtilizationDot percent={subnet.utilization_percent} />
        <button
          onClick={(e) => { e.stopPropagation(); setShowEdit(true); }}
          className="hidden rounded p-0.5 text-muted-foreground hover:text-foreground group-hover:flex"
          title="Edit subnet"
        >
          <Pencil className="h-3 w-3" />
        </button>
        <button
          onClick={(e) => { e.stopPropagation(); onDelete(); }}
          className="hidden rounded p-0.5 text-muted-foreground hover:text-destructive group-hover:flex"
          title="Delete subnet"
        >
          <Trash2 className="h-3 w-3" />
        </button>
      </div>
      {showEdit && (
        <EditSubnetModal
          subnet={subnet}
          onClose={(updated) => {
            setShowEdit(false);
            if (updated) onEdited(updated);
          }}
        />
      )}
    </>
  );
}

// ─── Space Section in tree ────────────────────────────────────────────────────

// ─── Confirm Delete Modal ─────────────────────────────────────────────────────

function ConfirmDeleteModal({
  title,
  message,
  confirmLabel = "Delete",
  onConfirm,
  onClose,
  isPending,
}: {
  title: string;
  message: string;
  confirmLabel?: string;
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
            {isPending ? "…" : confirmLabel}
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

// ─── Helpers: build a recursive block tree from a flat list ──────────────────

interface BlockNode {
  block: IPBlock;
  children: BlockNode[];
  subnets: Subnet[];
}

function buildBlockTree(blocks: IPBlock[], subnets: Subnet[], parentId: string | null): BlockNode[] {
  return blocks
    .filter((b) => b.parent_block_id === parentId)
    .map((b) => ({
      block: b,
      children: buildBlockTree(blocks, subnets, b.id),
      subnets: subnets.filter((s) => s.block_id === b.id),
    }));
}

// Flatten blocks into an indented label list for dropdowns
function flattenBlocks(nodes: BlockNode[], depth = 0): { id: string; label: string }[] {
  return nodes.flatMap(({ block, children }) => [
    { id: block.id, label: `${"  ".repeat(depth)}${block.network}${block.name ? ` — ${block.name}` : ""}` },
    ...flattenBlocks(children, depth + 1),
  ]);
}

// ─── Create Block Modal ───────────────────────────────────────────────────────

function CreateBlockModal({
  spaceId,
  defaultParentBlockId,
  onClose,
}: {
  spaceId: string;
  defaultParentBlockId?: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [network, setNetwork] = useState("");
  const [name, setName] = useState("");
  const [parentBlockId, setParentBlockId] = useState(defaultParentBlockId ?? "");
  const [error, setError] = useState<string | null>(null);

  const { data: existingBlocks } = useQuery({
    queryKey: ["blocks", spaceId],
    queryFn: () => ipamApi.listBlocks(spaceId),
  });

  const flatBlocks = existingBlocks
    ? flattenBlocks(buildBlockTree(existingBlocks, [], null))
    : [];

  const mutation = useMutation({
    mutationFn: () =>
      ipamApi.createBlock({
        space_id: spaceId,
        network,
        name: name || undefined,
        parent_block_id: parentBlockId || undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["blocks", spaceId] });
      onClose();
    },
    onError: (err: unknown) => {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to create block";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  return (
    <Modal title="New IP Block" onClose={onClose}>
      <div className="space-y-3">
        <Field label="Network (CIDR)">
          <input className={inputCls} value={network} onChange={(e) => { setNetwork(e.target.value); setError(null); }} placeholder="e.g. 10.0.0.0/8" autoFocus />
        </Field>
        <Field label="Name">
          <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="Optional" />
        </Field>
        {flatBlocks.length > 0 && (
          <Field label="Parent Block (optional)">
            <select className={inputCls} value={parentBlockId} onChange={(e) => setParentBlockId(e.target.value)}>
              <option value="">— None (top-level) —</option>
              {flatBlocks.map((b) => (
                <option key={b.id} value={b.id}>{b.label}</option>
              ))}
            </select>
          </Field>
        )}
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button onClick={onClose} className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted">Cancel</button>
          <button onClick={() => { setError(null); mutation.mutate(); }} disabled={!network || mutation.isPending} className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
            {mutation.isPending ? "Creating…" : "Create Block"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Recursive Block Tree Row ─────────────────────────────────────────────────

function BlockTreeRow({
  node,
  selectedSubnetId,
  selectedBlockId,
  onSelectBlock,
  onSelectSubnet,
  onDeleteSubnet,
  onCreateSubnet,
  onCreateChildBlock,
  depth,
}: {
  node: BlockNode;
  selectedSubnetId: string | null;
  selectedBlockId: string | null;
  onSelectBlock: (b: IPBlock) => void;
  onSelectSubnet: (s: Subnet) => void;
  onDeleteSubnet: (s: Subnet) => void;
  onCreateSubnet: (blockId: string) => void;
  onCreateChildBlock: (parentBlockId: string) => void;
  depth: number;
}) {
  const [expanded, setExpanded] = useState(true);
  const hasContent = node.children.length > 0 || node.subnets.length > 0;
  const isSelected = selectedBlockId === node.block.id;

  return (
    <div>
      {/* Block header row */}
      <div
        className={cn(
          "group flex items-center gap-1 rounded-md px-2 py-1 text-xs hover:bg-muted/30 cursor-pointer",
          isSelected && "bg-primary/10"
        )}
      >
        {/* [+] / [-] toggle box */}
        {hasContent ? (
          <button
            onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}
            className="flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-sm border border-border bg-background text-[10px] font-bold text-muted-foreground hover:border-primary hover:text-primary"
            title={expanded ? "Collapse" : "Expand"}
          >
            {expanded ? "−" : "+"}
          </button>
        ) : (
          <div className="flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-sm border border-border/30 bg-background text-[10px] text-muted-foreground/30">
            ·
          </div>
        )}

        {/* Block name — clickable to navigate */}
        <button
          onClick={() => onSelectBlock(node.block)}
          className={cn(
            "flex flex-1 items-center gap-1 min-w-0 text-left",
            isSelected ? "text-primary" : "text-muted-foreground hover:text-foreground"
          )}
        >
          <Layers className="h-3 w-3 flex-shrink-0" />
          <span className="font-mono font-medium flex-1 truncate">{node.block.network}</span>
          {node.block.name && (
            <span className="truncate text-[10px] opacity-60 mr-1">{node.block.name}</span>
          )}
        </button>

        {/* Hover actions */}
        <button
          onClick={(e) => { e.stopPropagation(); onCreateChildBlock(node.block.id); }}
          className="hidden group-hover:flex rounded p-0.5 text-muted-foreground hover:text-foreground"
          title="Add child block"
        >
          <Layers className="h-3 w-3" />
        </button>
        <button
          onClick={(e) => { e.stopPropagation(); onCreateSubnet(node.block.id); }}
          className="hidden group-hover:flex rounded p-0.5 text-muted-foreground hover:text-foreground"
          title="Add subnet to this block"
        >
          <Plus className="h-3 w-3" />
        </button>
      </div>

      {/* Children with vertical tree line */}
      {expanded && hasContent && (
        <div className="ml-[9px] pl-3 border-l border-border/40 space-y-0.5">
          {node.children.map((child) => (
            <BlockTreeRow
              key={child.block.id}
              node={child}
              selectedSubnetId={selectedSubnetId}
              selectedBlockId={selectedBlockId}
              onSelectBlock={onSelectBlock}
              onSelectSubnet={onSelectSubnet}
              onDeleteSubnet={onDeleteSubnet}
              onCreateSubnet={onCreateSubnet}
              onCreateChildBlock={onCreateChildBlock}
              depth={depth + 1}
            />
          ))}
          {node.subnets.map((subnet) => (
            <SubnetRow
              key={subnet.id}
              subnet={subnet}
              isSelected={selectedSubnetId === subnet.id}
              onSelect={() => onSelectSubnet(subnet)}
              onDelete={() => onDeleteSubnet(subnet)}
              onEdited={(updated) => onSelectSubnet(updated)}
            />
          ))}
          {node.children.length === 0 && node.subnets.length === 0 && (
            <p className="py-0.5 pl-2 text-xs text-muted-foreground/40">Empty</p>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Block Detail View (right panel when block is selected) ──────────────────

function BlockDetailView({
  block,
  spaceName,
  ancestors,
  allBlocks,
  allSubnets,
  onSelectSpace,
  onSelectBlock,
  onSelectSubnet,
}: {
  block: IPBlock;
  spaceName: string;
  ancestors: IPBlock[];
  allBlocks: IPBlock[];
  allSubnets: Subnet[];
  onSelectSpace: () => void;
  onSelectBlock: (b: IPBlock) => void;
  onSelectSubnet: (s: Subnet) => void;
}) {
  const directSubnets = allSubnets.filter((s) => s.block_id === block.id);

  const crumbs: BreadcrumbItem[] = [
    { label: spaceName, variant: "space", onClick: onSelectSpace },
    ...ancestors.map((a): BreadcrumbItem => ({
      label: a.network + (a.name ? ` (${a.name})` : ""),
      variant: "block",
      onClick: () => onSelectBlock(a),
    })),
    { label: block.network + (block.name ? ` (${block.name})` : ""), variant: "block" },
  ];

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="border-b px-6 py-3">
        <BreadcrumbPills items={crumbs} />
        <div className="flex items-center gap-2">
          <Layers className="h-4 w-4 text-muted-foreground" />
          <span className="font-mono text-lg font-semibold">{block.network}</span>
          {block.name && <span className="text-sm text-muted-foreground">{block.name}</span>}
        </div>
        {block.description && (
          <p className="mt-0.5 text-xs text-muted-foreground">{block.description}</p>
        )}
      </div>
      <div className="flex-1 overflow-auto">
        {(() => {
          // Build a synthetic BlockNode for the current block so flattenToTableRows
          // renders its full subtree (child blocks + direct subnets) at depth 0
          const syntheticNode: BlockNode = {
            block,
            children: buildBlockTree(allBlocks, allSubnets, block.id),
            subnets: directSubnets,
          };
          // flattenToTableRows renders: block_row, children..., subnets...
          // but we don't want a row for the block itself — skip it (depth -1 trick)
          const rawRows = flattenToTableRows([syntheticNode], -1);
          // The first row is the block itself at depth -1 — skip it
          const allRows = rawRows.slice(1).map((r) => ({ ...r, depth: Math.max(0, r.depth) }));

          if (allRows.length === 0) {
            return (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <Layers className="mb-3 h-10 w-10 text-muted-foreground/20" />
                <p className="text-sm text-muted-foreground">This block has no child blocks or subnets yet.</p>
              </div>
            );
          }

          return (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/40 text-xs">
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">Network</th>
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">Name</th>
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">VLAN</th>
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">Used IPs</th>
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">Utilization</th>
                  <th className="px-4 py-2 text-right font-medium text-muted-foreground">Size</th>
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">Status</th>
                </tr>
              </thead>
              <tbody>
                {allRows.map((item) => {
                  const indent = item.depth * 20;
                  if (item.type === "block" && item.block) {
                    const b = item.block;
                    return (
                      <tr key={item.key} onClick={() => onSelectBlock(b)} className="border-b last:border-0 cursor-pointer hover:bg-muted/30 bg-muted/10">
                        <td className="py-2 pr-4" style={{ paddingLeft: `${indent + 16}px` }}>
                          <span className="inline-flex items-center gap-1.5 font-mono font-semibold text-foreground">
                            <Layers className="h-3.5 w-3.5 flex-shrink-0 text-violet-500" />{b.network}
                          </span>
                        </td>
                        <td className="px-4 py-2 text-muted-foreground">{b.name || <span className="text-muted-foreground/40">—</span>}</td>
                        <td className="px-4 py-2 text-muted-foreground/40">—</td>
                        <td className="px-4 py-2 text-muted-foreground/40">—</td>
                        <td className="px-4 py-2">
                          {b.utilization_percent > 0
                            ? <UtilizationBar percent={b.utilization_percent} />
                            : <span className="text-muted-foreground/40">—</span>}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">{cidrSize(b.network).toLocaleString()}</td>
                        <td className="px-4 py-2 text-muted-foreground/40">—</td>
                      </tr>
                    );
                  }
                  if (item.type === "subnet" && item.subnet) {
                    const s = item.subnet;
                    return (
                      <tr key={item.key} onClick={() => onSelectSubnet(s)} className="border-b last:border-0 cursor-pointer hover:bg-muted/30">
                        <td className="py-2 pr-4" style={{ paddingLeft: `${indent + 16}px` }}>
                          <span className="inline-flex items-center gap-1.5 font-mono font-medium">
                            <Network className="h-3.5 w-3.5 flex-shrink-0 text-blue-500" />{s.network}
                          </span>
                        </td>
                        <td className="px-4 py-2 text-muted-foreground">{s.name || <span className="text-muted-foreground/40">—</span>}</td>
                        <td className="px-4 py-2 text-muted-foreground">{s.vlan_id ?? <span className="text-muted-foreground/40">—</span>}</td>
                        <td className="px-4 py-2 tabular-nums text-muted-foreground">{s.allocated_ips} / {s.total_ips}</td>
                        <td className="px-4 py-2"><UtilizationBar percent={s.utilization_percent} /></td>
                        <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">{s.total_ips.toLocaleString()}</td>
                        <td className="px-4 py-2"><StatusBadge status={s.status} /></td>
                      </tr>
                    );
                  }
                  return null;
                })}
              </tbody>
            </table>
          );
        })()}
      </div>
    </div>
  );
}

// ─── Space Table View (right panel when space is selected) ────────────────────

// Flatten a block tree into ordered rows for the table view
interface TreeTableItem {
  type: "block" | "subnet";
  depth: number;
  block?: IPBlock;
  subnet?: Subnet;
  key: string;
}

function flattenToTableRows(nodes: BlockNode[], depth = 0): TreeTableItem[] {
  return nodes.flatMap((node) => [
    { type: "block" as const, depth, block: node.block, key: `b-${node.block.id}` },
    ...flattenToTableRows(node.children, depth + 1),
    ...node.subnets.map((s): TreeTableItem => ({
      type: "subnet",
      depth: depth + 1,
      subnet: s,
      key: `s-${s.id}`,
    })),
  ]);
}

function cidrSize(network: string): number {
  const prefix = parseInt(network.split("/")[1] ?? "32");
  return Math.pow(2, 32 - prefix);
}

function SpaceTableView({
  space,
  onSelectSubnet,
  onSelectBlock,
}: {
  space: IPSpace;
  onSelectSubnet: (subnet: Subnet) => void;
  onSelectBlock: (block: IPBlock) => void;
}) {
  const { data: blocks, isLoading: blocksLoading } = useQuery({
    queryKey: ["blocks", space.id],
    queryFn: () => ipamApi.listBlocks(space.id),
  });

  const { data: subnets, isLoading: subnetsLoading } = useQuery({
    queryKey: ["subnets", space.id],
    queryFn: () => ipamApi.listSubnets({ space_id: space.id }),
  });

  const isLoading = blocksLoading || subnetsLoading;
  const rows =
    blocks && subnets ? flattenToTableRows(buildBlockTree(blocks, subnets, null)) : [];
  const isEmpty = !isLoading && rows.length === 0;

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="border-b px-6 py-3">
        <div className="mb-1">
          <BreadcrumbPills
            items={[{ label: space.name, variant: "space" }]}
          />
        </div>
        <h2 className="text-base font-semibold">{space.name}</h2>
        {space.description && (
          <p className="text-xs text-muted-foreground">{space.description}</p>
        )}
      </div>
      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <p className="px-6 py-4 text-sm text-muted-foreground">Loading…</p>
        ) : isEmpty ? (
          <p className="px-6 py-4 text-sm text-muted-foreground">
            No blocks or subnets in this space yet.
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/40 text-xs">
                <th className="px-4 py-2 text-left font-medium text-muted-foreground">
                  Network
                </th>
                <th className="px-4 py-2 text-left font-medium text-muted-foreground">Name</th>
                <th className="px-4 py-2 text-left font-medium text-muted-foreground">VLAN</th>
                <th className="px-4 py-2 text-left font-medium text-muted-foreground">
                  Used IPs
                </th>
                <th className="px-4 py-2 text-left font-medium text-muted-foreground">
                  Utilization
                </th>
                <th className="px-4 py-2 text-right font-medium text-muted-foreground">Size</th>
                <th className="px-4 py-2 text-left font-medium text-muted-foreground">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((item) => {
                const indent = item.depth * 20;
                if (item.type === "block" && item.block) {
                  const b = item.block;
                  const size = cidrSize(b.network);
                  return (
                    <tr
                      key={item.key}
                      onClick={() => onSelectBlock(b)}
                      className="border-b last:border-0 cursor-pointer hover:bg-muted/30 bg-muted/10"
                    >
                      <td className="py-2 pr-4" style={{ paddingLeft: `${indent + 16}px` }}>
                        <span className="inline-flex items-center gap-1.5 font-mono font-semibold text-foreground">
                          <Layers className="h-3.5 w-3.5 flex-shrink-0 text-violet-500" />
                          {b.network}
                        </span>
                      </td>
                      <td className="px-4 py-2 text-muted-foreground">
                        {b.name || <span className="text-muted-foreground/40">—</span>}
                      </td>
                      <td className="px-4 py-2 text-muted-foreground/40">—</td>
                      <td className="px-4 py-2 text-muted-foreground/40">—</td>
                      <td className="px-4 py-2">
                        {b.utilization_percent > 0 ? (
                          <UtilizationBar percent={b.utilization_percent} />
                        ) : (
                          <span className="text-muted-foreground/40">—</span>
                        )}
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
                        {size.toLocaleString()}
                      </td>
                      <td className="px-4 py-2 text-muted-foreground/40">—</td>
                    </tr>
                  );
                }
                if (item.type === "subnet" && item.subnet) {
                  const s = item.subnet;
                  return (
                    <tr
                      key={item.key}
                      onClick={() => onSelectSubnet(s)}
                      className="border-b last:border-0 cursor-pointer hover:bg-muted/30"
                    >
                      <td className="py-2 pr-4" style={{ paddingLeft: `${indent + 16}px` }}>
                        <span className="inline-flex items-center gap-1.5 font-mono font-medium">
                          <Network className="h-3.5 w-3.5 flex-shrink-0 text-blue-500" />
                          {s.network}
                        </span>
                      </td>
                      <td className="px-4 py-2 text-muted-foreground">
                        {s.name || <span className="text-muted-foreground/40">—</span>}
                      </td>
                      <td className="px-4 py-2 text-muted-foreground">
                        {s.vlan_id ?? <span className="text-muted-foreground/40">—</span>}
                      </td>
                      <td className="px-4 py-2 tabular-nums text-muted-foreground">
                        {s.allocated_ips} / {s.total_ips}
                      </td>
                      <td className="px-4 py-2">
                        <UtilizationBar percent={s.utilization_percent} />
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
                        {s.total_ips.toLocaleString()}
                      </td>
                      <td className="px-4 py-2">
                        <StatusBadge status={s.status} />
                      </td>
                    </tr>
                  );
                }
                return null;
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ─── Space Section in tree ────────────────────────────────────────────────────

function SpaceSection({
  space,
  selectedSubnetId,
  selectedBlockId,
  isSpaceSelected,
  onSelectSpace,
  onSelectSubnet,
  onSelectBlock,
}: {
  space: IPSpace;
  selectedSubnetId: string | null;
  selectedBlockId: string | null;
  isSpaceSelected: boolean;
  onSelectSpace: () => void;
  onSelectSubnet: (subnet: Subnet | null) => void;
  onSelectBlock: (b: IPBlock) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const [showCreateSubnet, setShowCreateSubnet] = useState<string | true | false>(false); // string = default block_id
  const [showCreateBlock, setShowCreateBlock] = useState<string | true | false>(false); // string = parent block_id
  const [showEditSpace, setShowEditSpace] = useState(false);
  const [subnetToDelete, setSubnetToDelete] = useState<Subnet | null>(null);
  const qc = useQueryClient();

  const { data: subnets, isLoading } = useQuery({
    queryKey: ["subnets", space.id],
    queryFn: () => ipamApi.listSubnets({ space_id: space.id }),
    enabled: expanded,
  });

  const { data: blocks } = useQuery({
    queryKey: ["blocks", space.id],
    queryFn: () => ipamApi.listBlocks(space.id),
    enabled: expanded,
  });

  const deleteSubnet = useMutation({
    mutationFn: (id: string) => ipamApi.deleteSubnet(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnets", space.id] });
      setSubnetToDelete(null);
    },
  });

  // (block_id is now required; all subnets appear under their block)

  return (
    <div>
      {/* Space header */}
      <div className={cn(
        "group flex items-center gap-1 rounded-md px-1 py-1.5 hover:bg-muted/50",
        isSpaceSelected && "bg-primary/5"
      )}>
        <button
          onClick={() => setExpanded((v) => !v)}
          className="flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-sm border border-border bg-background text-[10px] font-bold text-muted-foreground hover:border-primary hover:text-primary"
          title={expanded ? "Collapse" : "Expand"}
        >
          {expanded ? "−" : "+"}
        </button>
        <button
          onClick={onSelectSpace}
          className="flex flex-1 items-center gap-1 min-w-0"
        >
          <Layers className="h-3.5 w-3.5 flex-shrink-0 text-muted-foreground" />
          <span className={cn("flex-1 truncate text-left text-sm font-medium", isSpaceSelected && "text-primary")}>{space.name}</span>
        </button>
        <button onClick={(e) => { e.stopPropagation(); setShowEditSpace(true); }} className="hidden group-hover:flex flex-shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground" title="Edit space">
          <Pencil className="h-3 w-3" />
        </button>
        <button onClick={(e) => { e.stopPropagation(); setShowCreateBlock(true as true); }} className="hidden group-hover:flex flex-shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground" title="Add top-level block">
          <Layers className="h-3 w-3" />
        </button>
        <button onClick={(e) => { e.stopPropagation(); setShowCreateSubnet(true as true); }} className="hidden group-hover:flex flex-shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground" title="Add subnet">
          <Plus className="h-3 w-3" />
        </button>
      </div>

      {/* Tree with vertical connecting line */}
      {expanded && (
        <div className="ml-[9px] pl-2 border-l border-border/40 space-y-0.5">
          {isLoading && <p className="py-1 pl-2 text-xs text-muted-foreground">Loading…</p>}

          {/* Block tree (recursive) */}
          {blocks && subnets && buildBlockTree(blocks, subnets, null).map((node) => (
            <BlockTreeRow
              key={node.block.id}
              node={node}
              selectedSubnetId={selectedSubnetId}
              selectedBlockId={selectedBlockId}
              onSelectBlock={onSelectBlock}
              onSelectSubnet={onSelectSubnet}
              onDeleteSubnet={(s) => setSubnetToDelete(s)}
              onCreateSubnet={(blockId) => setShowCreateSubnet(blockId)}
              onCreateChildBlock={(parentId) => setShowCreateBlock(parentId)}
              depth={0}
            />
          ))}

          {!isLoading && !subnets?.length && !blocks?.length && (
            <p className="py-1 pl-2 text-xs text-muted-foreground">No blocks yet.</p>
          )}
        </div>
      )}

      {showCreateSubnet && (
        <CreateSubnetModal
          spaceId={space.id}
          defaultBlockId={typeof showCreateSubnet === "string" ? showCreateSubnet : undefined}
          onClose={() => setShowCreateSubnet(false)}
        />
      )}
      {showCreateBlock && (
        <CreateBlockModal
          spaceId={space.id}
          defaultParentBlockId={typeof showCreateBlock === "string" ? showCreateBlock : undefined}
          onClose={() => setShowCreateBlock(false)}
        />
      )}

      {showEditSpace && (
        <EditSpaceModal
          space={space}
          onClose={() => setShowEditSpace(false)}
          onDeleted={() => { setShowEditSpace(false); onSelectSubnet(null); }}
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

// ─── Helpers ──────────────────────────────────────────────────────────────────

function getBlockAncestors(block: IPBlock, allBlocks: IPBlock[]): IPBlock[] {
  const ancestors: IPBlock[] = [];
  let current: IPBlock | undefined = block;
  while (current?.parent_block_id) {
    const parent = allBlocks.find((b) => b.id === current!.parent_block_id);
    if (!parent) break;
    ancestors.unshift(parent);
    current = parent;
  }
  return ancestors;
}

// ─── Main IPAM Page ───────────────────────────────────────────────────────────

export function IPAMPage() {
  const [selectedSubnet, setSelectedSubnet] = useState<Subnet | null>(null);
  const [selectedSpace, setSelectedSpace] = useState<IPSpace | null>(null);
  const [selectedBlock, setSelectedBlock] = useState<IPBlock | null>(null);
  const [showCreateSpace, setShowCreateSpace] = useState(false);
  const qc = useQueryClient();
  const location = useLocation();
  const deepLinkHandled = useRef(false);

  const { data: spaces, isLoading } = useQuery({
    queryKey: ["spaces"],
    queryFn: ipamApi.listSpaces,
  });

  // Fetch ALL blocks so deep-link can look up any block by ID
  const { data: allBlocks } = useQuery({
    queryKey: ["blocks"],
    queryFn: () => ipamApi.listBlocks(),
  });

  // Fetch ALL subnets (limited) for deep-link resolution
  const { data: allSubnets } = useQuery({
    queryKey: ["subnets"],
    queryFn: () => ipamApi.listSubnets(),
  });

  // Deep-link: read router state set by GlobalSearch navigation
  useEffect(() => {
    if (deepLinkHandled.current) return;
    const state = location.state as {
      selectSpace?: string;
      selectBlock?: string;
      selectSubnet?: string;
      highlightAddress?: string;
    } | null;
    if (!state) return;
    if (state.selectSpace && spaces) {
      const sp = spaces.find((s) => s.id === state.selectSpace);
      if (sp) { selectSpace(sp); deepLinkHandled.current = true; }
    } else if (state.selectBlock && allBlocks) {
      const bl = allBlocks.find((b) => b.id === state.selectBlock);
      if (bl) { selectBlock(bl); deepLinkHandled.current = true; }
    } else if (state.selectSubnet && allSubnets) {
      const sn = allSubnets.find((s) => s.id === state.selectSubnet);
      if (sn) { selectSubnet(sn); deepLinkHandled.current = true; }
    }
  }, [location.state, spaces, allBlocks, allSubnets]);

  // Fetch blocks + subnets for whichever space the selected item belongs to
  const activeSpaceId =
    selectedBlock?.space_id ?? selectedSubnet?.space_id ?? selectedSpace?.id;

  const { data: detailBlocks } = useQuery({
    queryKey: ["blocks", activeSpaceId],
    queryFn: () => ipamApi.listBlocks(activeSpaceId!),
    enabled: !!activeSpaceId,
  });

  const { data: detailSubnets } = useQuery({
    queryKey: ["subnets", activeSpaceId],
    queryFn: () => ipamApi.listSubnets({ space_id: activeSpaceId }),
    enabled: !!activeSpaceId,
  });

  function selectSubnet(subnet: Subnet | null) {
    setSelectedSubnet(subnet);
    setSelectedBlock(null);
    if (subnet) setSelectedSpace(null);
  }

  function selectSpace(space: IPSpace) {
    setSelectedSpace(space);
    setSelectedSubnet(null);
    setSelectedBlock(null);
  }

  function selectBlock(block: IPBlock) {
    setSelectedBlock(block);
    setSelectedSubnet(null);
    setSelectedSpace(null);
  }

  const selectedSubnetBlock = detailBlocks?.find((b) => b.id === selectedSubnet?.block_id);
  const selectedSubnetBlockAncestors = selectedSubnetBlock && detailBlocks
    ? getBlockAncestors(selectedSubnetBlock, detailBlocks)
    : [];
  const selectedBlockAncestors = selectedBlock && detailBlocks
    ? getBlockAncestors(selectedBlock, detailBlocks)
    : [];

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
              selectedBlockId={selectedBlock?.id ?? null}
              isSpaceSelected={selectedSpace?.id === space.id}
              onSelectSpace={() => selectSpace(space)}
              onSelectSubnet={selectSubnet}
              onSelectBlock={selectBlock}
            />
          ))}
        </div>
      </div>

      {/* ── Right detail panel ── */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {selectedSubnet ? (
          <SubnetDetail
            subnet={selectedSubnet}
            spaceName={spaces?.find((s: IPSpace) => s.id === selectedSubnet.space_id)?.name}
            block={selectedSubnetBlock}
            blockAncestors={selectedSubnetBlockAncestors}
            onSelectSpace={() => {
              const sp = spaces?.find((s: IPSpace) => s.id === selectedSubnet.space_id);
              if (sp) selectSpace(sp);
            }}
            onSelectBlock={selectBlock}
            onSubnetEdited={(updated) => setSelectedSubnet(updated)}
          />
        ) : selectedBlock ? (
          <BlockDetailView
            block={selectedBlock}
            spaceName={spaces?.find((s: IPSpace) => s.id === selectedBlock.space_id)?.name ?? ""}
            ancestors={selectedBlockAncestors}
            allBlocks={detailBlocks ?? []}
            allSubnets={detailSubnets ?? []}
            onSelectSpace={() => {
              const sp = spaces?.find((s: IPSpace) => s.id === selectedBlock.space_id);
              if (sp) selectSpace(sp);
            }}
            onSelectBlock={selectBlock}
            onSelectSubnet={selectSubnet}
          />
        ) : selectedSpace ? (
          <SpaceTableView
            space={selectedSpace}
            onSelectSubnet={selectSubnet}
            onSelectBlock={selectBlock}
          />
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center text-center">
            <Network className="mb-3 h-12 w-12 text-muted-foreground/20" />
            <p className="text-sm text-muted-foreground">
              Select a space, block, or subnet from the tree.
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
