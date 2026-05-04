import { useState, useEffect, useRef } from "react";
import { DHCPSubnetPanel } from "@/pages/dhcp/DHCPSubnetPanel";
import {
  useQuery,
  useQueries,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { useLocation, useSearchParams } from "react-router-dom";
import {
  ChevronDown,
  ChevronRight,
  Network,
  Layers,
  Plus,
  Server,
  Trash2,
  Pencil,
  RefreshCw,
  X,
  Copy,
  Check,
  Upload,
  Globe2,
  AlertTriangle,
  Filter,
  Lock,
  Search,
  Radar,
  Wrench,
  Scissors,
  GitMerge,
  Maximize2,
  ShieldCheck,
} from "lucide-react";
import {
  DndContext,
  PointerSensor,
  useSensor,
  useSensors,
  useDraggable,
  useDroppable,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  ipamApi,
  dnsApi,
  dhcpApi,
  customFieldsApi,
  vlansApi,
  natApi,
  networkApi,
  asnsApi,
  vrfsApi,
  IP_ROLE_OPTIONS,
  type IPSpace,
  type IPBlock,
  type Subnet,
  type IPAddress,
  type IPRole,
  type CustomField,
  type DNSZone,
  type FreeCidrRange,
  type Router as NetworkRouter,
  type VLAN,
  type DHCPLeaseSyncResult,
  type MacHistoryEntry,
  type NATMapping,
  type NetworkContextEntry,
} from "@/lib/api";
import { copyToClipboard } from "@/lib/clipboard";
import { cn, swatchTintCls, zebraBodyCls } from "@/lib/utils";
import { SwatchPicker } from "@/components/ui/swatch-picker";
import { useStickyLocation } from "@/lib/stickyLocation";
import { useSessionState } from "@/lib/useSessionState";
import { useRowHighlight } from "@/lib/useRowHighlight";
import { Modal } from "@/components/ui/modal";
import { AsnPicker } from "@/components/ipam/asn-picker";
import { VrfPicker } from "@/components/ipam/vrf-picker";
import {
  MODAL_BACKDROP_CLS,
  useDraggableModal,
} from "@/components/ui/use-draggable-modal";
import { HeaderButton } from "@/components/ui/header-button";
import {
  ImportModal,
  ExportButton,
  SubnetImportExportButton,
} from "./ImportExportModals";
import { ResizeBlockModal, ResizeSubnetModal } from "./ResizeModals";
import { MoveBlockModal } from "./MoveBlockModal";
import {
  IPNetworkTab,
  NetworkTabBadge,
  useNetworkContext,
} from "./IPNetworkTab";
import { NmapScanModal } from "@/pages/nmap/NmapScanModal";
import { BulkAllocateModal } from "./BulkAllocateModal";
import { IPDetailModal } from "./IPDetailModal";
import { SeenDot } from "./SeenDot";
import {
  FindFreeModal,
  MergeSubnetSiblingPicker,
  SplitSubnetModal,
} from "./SubnetOpsModals";
import { cidrContains, compareNetwork } from "@/lib/cidr";
import { FreeSpaceBand } from "@/components/ipam/FreeSpaceBand";
import { PlanAllocationModal } from "@/components/ipam/PlanAllocationModal";
import { AggregationSuggestions } from "@/components/ipam/AggregationSuggestions";
import { FreeSpaceTreemap } from "@/components/ipam/FreeSpaceTreemap";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
  ContextMenuLabel,
} from "@/components/ui/context-menu";

// ─── Status Badge ────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    active:
      "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
    reserved:
      "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
    deprecated:
      "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
    quarantine: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400",
    allocated:
      "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400",
    available:
      "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
    dhcp: "bg-cyan-100 text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-400",
    static_dhcp:
      "bg-teal-100 text-teal-800 dark:bg-teal-900/30 dark:text-teal-400",
    network: "bg-zinc-100 text-zinc-500 dark:bg-zinc-800/50 dark:text-zinc-400",
    broadcast:
      "bg-zinc-100 text-zinc-500 dark:bg-zinc-800/50 dark:text-zinc-400",
    orphan:
      "bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400",
    // ``discovered`` — passive observation only, no operator intent.
    // The Seen column's recency dot tells the operator whether the
    // row is currently up; the badge here just labels the source.
    discovered: "bg-sky-100 text-sky-800 dark:bg-sky-900/30 dark:text-sky-400",
  };
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-xs font-medium",
        colors[status] ?? "bg-muted text-muted-foreground",
      )}
    >
      {status}
    </span>
  );
}

// Compact role badge — paired with StatusBadge in the IP table.
// ``anycast`` / ``vip`` / ``vrrp`` are intentionally shared roles
// (the API skips MAC-collision warnings for them) so they get a
// hint colour the operator can spot at a glance.
function RoleBadge({ role }: { role: string }) {
  const SHARED = new Set(["anycast", "vip", "vrrp"]);
  const cls = SHARED.has(role)
    ? "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300 border border-amber-200/60 dark:border-amber-900/60"
    : "bg-slate-100 text-slate-700 dark:bg-slate-800/40 dark:text-slate-300 border border-slate-200/60 dark:border-slate-800/60";
  const tip = SHARED.has(role)
    ? `${role} — shared-by-design (MAC collisions suppressed)`
    : role;
  return (
    <span
      title={tip}
      className={cn(
        "ml-1 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium",
        cls,
      )}
    >
      {role}
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
    copyToClipboard(text).then((ok) => {
      if (ok) {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }
    });
  }
  return (
    <button
      onClick={handleCopy}
      title="Copy to clipboard"
      className="ml-1 rounded p-0.5 text-muted-foreground/0 hover:text-muted-foreground group-hover/addr:text-muted-foreground/60 hover:!text-foreground transition-colors"
    >
      {copied ? (
        <Check className="h-3 w-3 text-green-500" />
      ) : (
        <Copy className="h-3 w-3" />
      )}
    </button>
  );
}

// ─── Network discovery cell ───────────────────────────────────────────────
//
// Renders the SNMP-discovered switch/port/VLAN context for an IP. Inputs
// come from the batched ``/ipam/subnets/{id}/network-context`` join — see
// the ``subnetNetworkContext`` query in ``SubnetView``.
//
// Cardinality matters: a single MAC can legitimately appear on multiple
// (device, port, VLAN) tuples — the canonical case is a hypervisor host
// and its VMs all egressing through one trunk port, learned per-VLAN. We
// surface the most-recent entry by default and expose the rest via a
// ``+N more`` badge with a hover tooltip listing all of them.

function NetworkContextCell({ entries }: { entries: NetworkContextEntry[] }) {
  if (!entries.length)
    return <span className="text-muted-foreground/40">—</span>;
  const primary = entries[0];
  const more = entries.length - 1;
  const tooltipLines = entries
    .map(
      (e) =>
        `${e.device_name} : ${e.interface_name}` +
        (e.vlan_id != null ? ` (VLAN ${e.vlan_id})` : ""),
    )
    .join("\n");
  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs"
      title={tooltipLines}
    >
      <span className="font-medium">{primary.device_name}</span>
      <span className="text-muted-foreground">·</span>
      <span className="font-mono text-[11px]">{primary.interface_name}</span>
      {primary.vlan_id != null && (
        <span className="rounded bg-amber-500/15 px-1 py-0.5 font-mono text-[10px] font-semibold text-amber-700 dark:text-amber-300">
          VLAN {primary.vlan_id}
        </span>
      )}
      {more > 0 && (
        <span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">
          +{more} more
        </span>
      )}
    </span>
  );
}

// ─── Custom Fields Section ───────────────────────────────────────────────────

function CustomFieldsSection({
  definitions,
  values,
  onChange,
  inherited,
  inheritedLabels,
}: {
  definitions: CustomField[];
  values: Record<string, unknown>;
  onChange: (key: string, value: unknown) => void;
  /**
   * Optional effective-field values inherited from ancestors (block/space).
   * When a key is present here but missing (or empty) from ``values``, the
   * field is rendered with the inherited value as a placeholder plus an
   * "inherited from …" badge. Typing replaces the inherited value; clearing
   * the input reveals the placeholder again.
   */
  inherited?: Record<string, unknown>;
  /**
   * Optional human-friendly label per inherited key (e.g. "block Corp" /
   * "IP Space Corporate"). Keyed by the custom-field name. Missing entries
   * fall back to a generic "inherited".
   */
  inheritedLabels?: Record<string, string>;
}) {
  if (definitions.length === 0) return null;
  return (
    <>
      <div className="border-t pt-3">
        <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Custom Fields
        </p>
        <div className="space-y-3">
          {definitions.map((def) => {
            const rawLocal = values[def.name];
            const localUnset =
              rawLocal === undefined || rawLocal === null || rawLocal === "";
            const inheritedVal = inherited?.[def.name];
            const hasInherited =
              inheritedVal !== undefined &&
              inheritedVal !== null &&
              inheritedVal !== "";
            const effectivePlaceholder =
              localUnset && hasInherited
                ? String(inheritedVal)
                : def.is_required
                  ? "Required"
                  : "Optional";
            // Displayed value: local if set, else empty (so the inherited
            // value shows through the HTML placeholder). We never pre-fill
            // the input with the inherited value — that would flip it from
            // "inherited" to "locally set" the moment the user saves.
            const val = rawLocal ?? def.default_value ?? "";
            const inheritedBadge =
              localUnset && hasInherited ? (
                <span className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                  inherited from {inheritedLabels?.[def.name] ?? "ancestor"}
                </span>
              ) : null;
            return (
              <Field
                key={def.name}
                label={`${def.label}${def.is_required ? " *" : ""}`}
              >
                {(def.description || inheritedBadge) && (
                  <div className="mb-1 flex items-center justify-between gap-2">
                    {def.description ? (
                      <p className="text-xs text-muted-foreground">
                        {def.description}
                      </p>
                    ) : (
                      <span />
                    )}
                    {inheritedBadge}
                  </div>
                )}
                {def.field_type === "boolean" ? (
                  <input
                    type="checkbox"
                    className="rounded"
                    checked={!!val}
                    onChange={(e) => onChange(def.name, e.target.checked)}
                  />
                ) : def.field_type === "select" && def.options ? (
                  <select
                    className={inputCls}
                    value={String(val)}
                    onChange={(e) => onChange(def.name, e.target.value)}
                  >
                    {!def.is_required && (
                      <option value="">
                        {localUnset && hasInherited
                          ? `— inherited: ${String(inheritedVal)} —`
                          : "— None —"}
                      </option>
                    )}
                    {def.options.map((opt) => (
                      <option key={opt} value={opt}>
                        {opt}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    className={inputCls}
                    type={
                      def.field_type === "number"
                        ? "number"
                        : def.field_type === "email"
                          ? "email"
                          : def.field_type === "url"
                            ? "url"
                            : "text"
                    }
                    value={String(val)}
                    onChange={(e) => onChange(def.name, e.target.value)}
                    placeholder={effectivePlaceholder}
                  />
                )}
              </Field>
            );
          })}
        </div>
      </div>
    </>
  );
}

// ─── Modal helpers ────────────────────────────────────────────────────────────

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

// `focus:ring-inset` draws the focus ring inside the element, which prevents
// the left/right edges of the ring from being clipped by the modal's
// `overflow-y-auto` container (browsers default `overflow-x` to `auto` too
// when `overflow-y` is set).
const inputCls =
  "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-inset focus:ring-ring";

/**
 * Shared DNS-zone dropdown options renderer.
 *
 * When the subnet/block has explicit zone assignments, shows the primary
 * zone at the top as a flat option, then an <optgroup label="Additional
 * zones"> containing the rest. Otherwise renders a flat list.
 */
function ZoneOptions({
  zones,
  primaryId,
  additionalIds,
  noneOption,
}: {
  zones: DNSZone[];
  primaryId: string | null | undefined;
  additionalIds: string[];
  /** When set, render a leading "— <label> —" option with empty value. */
  noneOption?: string;
}) {
  const fmt = (z: DNSZone) => z.name.replace(/\.$/, "");
  const primary = primaryId ? zones.find((z) => z.id === primaryId) : null;
  const additional = zones.filter(
    (z) => additionalIds.includes(z.id) && z.id !== primaryId,
  );
  const others = zones.filter(
    (z) => z.id !== primaryId && !additionalIds.includes(z.id),
  );
  return (
    <>
      {noneOption && <option value="">— {noneOption} —</option>}
      {primary && <option value={primary.id}>{fmt(primary)} (primary)</option>}
      {additional.length > 0 && (
        <optgroup label="Additional zones">
          {additional.map((z) => (
            <option key={z.id} value={z.id}>
              {fmt(z)}
            </option>
          ))}
        </optgroup>
      )}
      {/* Zones neither pinned nor additional — only surfaces when the
          subnet's groups expose extras (e.g. nothing explicitly pinned). */}
      {others.length > 0 && !primary && additional.length === 0 && (
        <>
          {others.map((z) => (
            <option key={z.id} value={z.id}>
              {fmt(z)}
            </option>
          ))}
        </>
      )}
      {others.length > 0 && (primary || additional.length > 0) && (
        <optgroup label="Other">
          {others.map((z) => (
            <option key={z.id} value={z.id}>
              {fmt(z)}
            </option>
          ))}
        </optgroup>
      )}
    </>
  );
}

// ─── Create Space Modal ───────────────────────────────────────────────────────

function CreateSpaceModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [color, setColor] = useState<string | null>(null);
  const [dnsGroupIds, setDnsGroupIds] = useState<string[]>([]);
  const [dnsZoneId, setDnsZoneId] = useState<string | null>(null);
  const [dnsAdditionalZoneIds, setDnsAdditionalZoneIds] = useState<string[]>(
    [],
  );
  const [dhcpServerGroupId, setDhcpServerGroupId] = useState<string | null>(
    null,
  );
  // VRF / BGP annotation — pure metadata, parity with EditSpaceModal so
  // operators don't have to round-trip through Edit just to set their
  // routing context on a freshly-created space. Collapsed by default
  // since most homelab / SMB deployments don't run a multi-VRF fabric.
  const [showVrf, setShowVrf] = useState<boolean>(false);
  const [vrfId, setVrfId] = useState<string | null>(null);
  const [asnId, setAsnId] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => {
      return ipamApi.createSpace({
        name,
        description,
        is_default: false,
        color,
        dns_group_ids: dnsGroupIds,
        dns_zone_id: dnsZoneId,
        dns_additional_zone_ids: dnsAdditionalZoneIds,
        dhcp_server_group_id: dhcpServerGroupId,
        vrf_id: vrfId,
        asn_id: asnId,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["spaces"] });
      onClose();
    },
  });
  return (
    <Modal title="New IP Space" onClose={onClose} wide>
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
        <Field label="Color">
          <SwatchPicker value={color} onChange={setColor} />
        </Field>
        <div className="border-t pt-3">
          <p className="mb-2 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
            DNS Defaults (inherited by child blocks and subnets)
          </p>
          <DnsSettingsSection
            inherit={false}
            hideInheritToggle
            groupIds={dnsGroupIds}
            zoneId={dnsZoneId}
            additionalZoneIds={dnsAdditionalZoneIds}
            onInheritChange={() => {}}
            onGroupIdsChange={setDnsGroupIds}
            onZoneIdChange={setDnsZoneId}
            onAdditionalZoneIdsChange={setDnsAdditionalZoneIds}
          />
        </div>
        <div className="border-t pt-3">
          <p className="mb-2 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
            DHCP Defaults (inherited by child blocks and subnets)
          </p>
          <DhcpSettingsSection
            inherit={false}
            hideInheritToggle
            serverGroupId={dhcpServerGroupId}
            onInheritChange={() => {}}
            onServerGroupIdChange={setDhcpServerGroupId}
          />
        </div>

        {/* VRF / BGP annotation — collapsible to keep the form tidy
            for operators who don't run multiple VRFs. Both fields are
            FK pickers backed by first-class entities; RD / RT are
            stored on the VRF row and surfaced read-only when picked. */}
        <div className="border-t pt-3">
          <button
            type="button"
            onClick={() => setShowVrf((s) => !s)}
            className="flex items-center gap-1 text-xs font-semibold text-muted-foreground uppercase tracking-wide hover:text-foreground"
          >
            {showVrf ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            VRF / BGP (optional)
          </button>
          {showVrf && (
            <div className="mt-2 space-y-2">
              <Field label="VRF">
                <VrfPicker
                  className={inputCls}
                  value={vrfId}
                  onChange={setVrfId}
                />
                <p className="mt-1 text-[11px] text-muted-foreground">
                  Manage VRFs (RD + import / export RTs) under{" "}
                  <a
                    href="/network/vrfs"
                    className="underline hover:text-foreground"
                  >
                    Network → VRFs
                  </a>
                  .
                </p>
              </Field>
              <Field label="Origin ASN (BGP)">
                <AsnPicker
                  className={inputCls}
                  value={asnId}
                  onChange={setAsnId}
                />
              </Field>
              <p className="text-xs text-muted-foreground">
                Pure annotation — address allocation does not consult these
                fields. Different VRFs with overlapping IPs already work via
                separate IPSpace rows.
              </p>
            </div>
          )}
        </div>

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

// ─── DNS Settings Section (reused in block & subnet modals) ──────────────────

/**
 * Shows DNS group / zone assignment with an "Inherit from parent" toggle.
 * When inheriting the fields are grayed out showing what would be inherited.
 * parentBlockId: pass the parent block's id to fetch effective inherited values.
 */
function DnsSettingsSection({
  inherit,
  groupIds,
  zoneId,
  additionalZoneIds,
  onInheritChange,
  onGroupIdsChange,
  onZoneIdChange,
  onAdditionalZoneIdsChange,
  parentBlockId,
  fallbackSpaceId,
  hideInheritToggle,
}: {
  inherit: boolean;
  groupIds: string[];
  zoneId: string | null;
  additionalZoneIds: string[];
  onInheritChange: (v: boolean) => void;
  onGroupIdsChange: (v: string[]) => void;
  onZoneIdChange: (v: string | null) => void;
  onAdditionalZoneIdsChange: (v: string[]) => void;
  parentBlockId?: string | null;
  fallbackSpaceId?: string | null;
  hideInheritToggle?: boolean;
}) {
  const { data: allGroups = [] } = useQuery({
    queryKey: ["dns-groups"],
    queryFn: dnsApi.listGroups,
    staleTime: 60_000,
  });

  // Fetch effective inherited DNS from parent block (if provided)
  const { data: blockDns } = useQuery({
    queryKey: ["effective-dns-block", parentBlockId],
    queryFn: () => ipamApi.getEffectiveBlockDns(parentBlockId!),
    enabled: !!parentBlockId,
    staleTime: 30_000,
  });

  // Fallback: fetch space-level DNS when no parent block (top-level block creation)
  const { data: spaceDns } = useQuery({
    queryKey: ["effective-dns-space", fallbackSpaceId],
    queryFn: () => ipamApi.getEffectiveSpaceDns(fallbackSpaceId!),
    enabled: !parentBlockId && !!fallbackSpaceId,
    staleTime: 30_000,
  });

  const effectiveDns = blockDns ?? spaceDns ?? null;

  // The group IDs to drive zone loading (own or inherited depending on mode)
  const activeGroupIds = inherit
    ? (effectiveDns?.dns_group_ids ?? [])
    : groupIds;

  const zoneQueries = useQueries({
    queries: (activeGroupIds as string[]).map((gId: string) => ({
      queryKey: ["dns-zones", gId],
      queryFn: () => dnsApi.listZones(gId),
      staleTime: 60_000,
    })),
  });
  const allAvailableZones: DNSZone[] = zoneQueries.flatMap(
    (q: { data?: DNSZone[] }) => q.data ?? [],
  );
  // Exclude reverse-lookup zones (in-addr.arpa, ip6.arpa) from primary/additional pickers
  const availableZones = allAvailableZones.filter(
    (z) => !z.name.toLowerCase().includes("arpa"),
  );

  // Displayed values when inheriting
  const displayGroupIds = inherit
    ? (effectiveDns?.dns_group_ids ?? [])
    : groupIds;
  const displayZoneId = inherit ? (effectiveDns?.dns_zone_id ?? null) : zoneId;
  const displayAdditionalIds = inherit
    ? (effectiveDns?.dns_additional_zone_ids ?? [])
    : additionalZoneIds;

  const inheritedFrom = effectiveDns?.inherited_from_block_id
    ? "a parent block"
    : null;

  return (
    <div className="space-y-2">
      {!hideInheritToggle && (
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5">
            <Globe2 className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="text-xs font-medium">DNS Settings</span>
          </div>
          <label className="flex items-center gap-1.5 text-xs cursor-pointer select-none">
            <input
              type="checkbox"
              checked={inherit}
              onChange={(e) => onInheritChange(e.target.checked)}
              className="h-3.5 w-3.5"
            />
            Inherit from parent
          </label>
        </div>
      )}

      {inherit && (
        <p className="text-xs text-muted-foreground italic">
          {effectiveDns &&
          (effectiveDns.dns_group_ids.length > 0 || effectiveDns.dns_zone_id)
            ? `Inheriting from ${inheritedFrom ?? "parent"}: ${effectiveDns.dns_group_ids.length} group(s), zone: ${effectiveDns.dns_zone_id ? (availableZones.find((z) => z.id === effectiveDns.dns_zone_id)?.name ?? effectiveDns.dns_zone_id) : "none"}`
            : "No DNS settings configured in parent chain."}
        </p>
      )}

      <fieldset disabled={inherit} className="space-y-2 disabled:opacity-50">
        {/* DNS Server Group — single-select dropdown to match the DHCP
            picker visually. The API still stores a list for backwards
            compatibility; we round-trip through a single-element array. */}
        <div>
          <p className="text-xs text-muted-foreground mb-1">DNS Server Group</p>
          {allGroups.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">
              No groups configured.
            </p>
          ) : (
            <select
              value={displayGroupIds[0] ?? ""}
              onChange={(e) => {
                if (inherit) return;
                const id = e.target.value;
                // Switching groups invalidates zone picks from the old
                // group — clear primary and additional zones.
                onGroupIdsChange(id ? [id] : []);
                onZoneIdChange(null);
                onAdditionalZoneIdsChange([]);
              }}
              className={`${inputCls} w-full`}
            >
              <option value="">— None —</option>
              {allGroups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          )}
        </div>

        {/* Primary Zone */}
        {availableZones.length > 0 && (
          <div>
            <p className="text-xs text-muted-foreground mb-1">Primary Zone</p>
            <select
              value={displayZoneId ?? ""}
              onChange={(e) =>
                !inherit && onZoneIdChange(e.target.value || null)
              }
              className={`${inputCls} w-full`}
            >
              <option value="">— None —</option>
              {availableZones.map((z) => (
                <option key={z.id} value={z.id}>
                  {z.name}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* Additional Zones — collapsed by default. The dual-listbox is
            bulky so we hide it behind a <details> expander; most users
            don't need to override the primary zone's sibling zones. When
            inheriting we skip it entirely. */}
        {availableZones.length > 0 && !inherit && (
          <details className="group rounded-md border border-primary/40 bg-primary/[0.03]">
            <summary className="flex cursor-pointer select-none items-center justify-between gap-2 rounded-md px-2 py-1.5 text-xs font-medium text-foreground hover:bg-primary/10 [&::-webkit-details-marker]:hidden">
              <span className="flex items-center gap-1.5">
                <svg
                  className="h-3.5 w-3.5 shrink-0 text-primary transition-transform group-open:rotate-90"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.5"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M9 5l7 7-7 7"
                  />
                </svg>
                <span>Additional Zones</span>
              </span>
              <span className="rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                {displayAdditionalIds.length} selected
              </span>
            </summary>
            <div className="border-t border-primary/20 px-2 py-2">
              <AdditionalZonesPicker
                allZones={availableZones.filter((z) => z.id !== displayZoneId)}
                selectedIds={displayAdditionalIds}
                onChange={(ids) => onAdditionalZoneIdsChange(ids)}
                disabled={false}
              />
            </div>
          </details>
        )}
      </fieldset>
    </div>
  );
}

// ─── DHCP Settings Section (reused in space/block/subnet modals) ────────────

/**
 * Parallels DnsSettingsSection for DHCP. Picks a single server group that
 * cascades down the IPAM hierarchy. When ``inherit`` is true the dropdown
 * shows the effective group from the parent chain and is disabled. The
 * space variant skips the inherit toggle (the space is the root).
 */
function DhcpSettingsSection({
  inherit,
  serverGroupId,
  onInheritChange,
  onServerGroupIdChange,
  parentBlockId,
  fallbackSpaceId,
  hideInheritToggle,
}: {
  inherit: boolean;
  serverGroupId: string | null;
  onInheritChange: (v: boolean) => void;
  onServerGroupIdChange: (v: string | null) => void;
  parentBlockId?: string | null;
  fallbackSpaceId?: string | null;
  hideInheritToggle?: boolean;
}) {
  const { data: allGroups = [] } = useQuery({
    queryKey: ["dhcp-groups"],
    queryFn: () => dhcpApi.listGroups(),
    staleTime: 60_000,
  });

  const { data: blockDhcp } = useQuery({
    queryKey: ["effective-dhcp-block", parentBlockId],
    queryFn: () => ipamApi.getEffectiveBlockDhcp(parentBlockId!),
    enabled: !!parentBlockId,
    staleTime: 30_000,
  });
  const { data: spaceDhcp } = useQuery({
    queryKey: ["effective-dhcp-space", fallbackSpaceId],
    queryFn: () => ipamApi.getEffectiveSpaceDhcp(fallbackSpaceId!),
    enabled: !parentBlockId && !!fallbackSpaceId,
    staleTime: 30_000,
  });
  const effectiveDhcp = blockDhcp ?? spaceDhcp ?? null;

  const displayGroupId = inherit
    ? (effectiveDhcp?.dhcp_server_group_id ?? null)
    : serverGroupId;
  const displayGroup = allGroups.find((g) => g.id === displayGroupId);

  const inheritedFrom = effectiveDhcp?.inherited_from_block_id
    ? "a parent block"
    : effectiveDhcp?.inherited_from_space
      ? "the space"
      : null;

  return (
    <div className="space-y-2">
      {!hideInheritToggle && (
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5">
            <Network className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="text-xs font-medium">DHCP Settings</span>
          </div>
          <label className="flex items-center gap-1.5 text-xs cursor-pointer select-none">
            <input
              type="checkbox"
              checked={inherit}
              onChange={(e) => onInheritChange(e.target.checked)}
              className="h-3.5 w-3.5"
            />
            Inherit from parent
          </label>
        </div>
      )}

      {inherit && (
        <p className="text-xs text-muted-foreground italic">
          {effectiveDhcp?.dhcp_server_group_id
            ? `Inheriting from ${inheritedFrom ?? "parent"}: group "${displayGroup?.name ?? "unknown"}".`
            : "No DHCP group configured in parent chain."}
        </p>
      )}

      <fieldset disabled={inherit} className="space-y-2 disabled:opacity-50">
        <div>
          <p className="text-xs text-muted-foreground mb-1">
            DHCP Server Group
          </p>
          {allGroups.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">
              No DHCP server groups configured.
            </p>
          ) : (
            <select
              value={displayGroupId ?? ""}
              onChange={(e) =>
                !inherit && onServerGroupIdChange(e.target.value || null)
              }
              className={`${inputCls} w-full`}
            >
              <option value="">— None —</option>
              {allGroups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          )}
        </div>
      </fieldset>
    </div>
  );
}

/**
 * DDNS — dynamic DNS from DHCP leases. Subnet-only setting (unlike DNS /
 * DHCP which inherit through space → block → subnet). Four knobs:
 *
 *   ``enabled``       — master toggle. When off, leases on this subnet
 *                       don't publish A/AAAA/PTR regardless of policy.
 *   ``policy``        — ``client_provided`` | ``client_or_generated`` |
 *                       ``always_generate`` | ``disabled`` — governs
 *                       how the DDNS service picks a hostname when the
 *                       lease's client hostname is missing / unwanted.
 *   ``domainOverride`` — optional different zone for DDNS writes.
 *   ``ttl``            — optional TTL override for auto-generated records.
 *
 * The preview row shows what hostname the ``always_generate`` path
 * would produce for the subnet's first usable IP, so the operator can
 * sanity-check the pattern before enabling.
 */
type DdnsPolicy =
  | "client_provided"
  | "client_or_generated"
  | "always_generate"
  | "disabled";

/**
 * Device profiling — auto-nmap on new DHCP leases. Subnet-only setting
 * (Phase 1: no inheritance). Three knobs:
 *
 *   ``enabled``       — master toggle. Default off because nmap is loud:
 *                       corporate IDS will flag the SpatiumDDI host as a
 *                       port-scanner once enabled.
 *   ``preset``        — nmap preset key. ``service_and_os`` is the
 *                       default: services + OS fingerprint in one pass
 *                       (``-T4 -sV -O --version-light``), without the
 *                       heavyweight ``-A`` aggressive scripts.
 *   ``refreshDays``   — dedupe window. The same IP won't re-scan within
 *                       this many days of its last successful profile.
 *                       Wi-Fi clients churning leases on roam events
 *                       won't fire-hose nmap.
 */
type AutoProfilePreset =
  | "quick"
  | "service_version"
  | "os_fingerprint"
  | "service_and_os"
  | "default_scripts"
  | "udp_top100"
  | "aggressive";

function ProfilingSettingsSection({
  enabled,
  preset,
  refreshDays,
  onEnabledChange,
  onPresetChange,
  onRefreshDaysChange,
}: {
  enabled: boolean;
  preset: AutoProfilePreset;
  refreshDays: number;
  onEnabledChange: (v: boolean) => void;
  onPresetChange: (v: AutoProfilePreset) => void;
  onRefreshDaysChange: (v: number) => void;
}) {
  const disabled = !enabled;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <Network className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="text-xs font-medium">
            Device profiling (auto-nmap on new DHCP lease)
          </span>
        </div>
        <label className="flex items-center gap-1.5 text-xs cursor-pointer select-none">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => onEnabledChange(e.target.checked)}
            className="h-3.5 w-3.5"
          />
          Enabled
        </label>
      </div>
      {!enabled && (
        <p className="text-xs text-muted-foreground italic">
          When enabled, a fresh DHCP lease in this subnet kicks off an nmap scan
          to fingerprint the device. Loud — corporate IDS will see the
          SpatiumDDI host as a port-scanner. Authorise the source first.
        </p>
      )}
      {enabled && (
        <div className="space-y-2 pl-5 border-l-2 border-muted">
          <label className="block text-xs">
            <span className="block text-muted-foreground mb-0.5">
              Scan preset
            </span>
            <select
              value={preset}
              onChange={(e) =>
                onPresetChange(e.target.value as AutoProfilePreset)
              }
              disabled={disabled}
              className="w-full rounded-md border bg-background px-2 py-1 text-sm disabled:opacity-60"
            >
              <option value="service_and_os">
                service_and_os — services + OS guess (default)
              </option>
              <option value="quick">quick — top 100 ports, no banner</option>
              <option value="service_version">
                service_version — top 1000 ports + service banners
              </option>
              <option value="os_fingerprint">
                os_fingerprint — TCP stack OS guess only
              </option>
              <option value="default_scripts">
                default_scripts — NSE -sC checks
              </option>
              <option value="udp_top100">udp_top100 — UDP top 100 ports</option>
              <option value="aggressive">
                aggressive — -A (loud, slow, full)
              </option>
            </select>
          </label>
          <label className="block text-xs">
            <span className="block text-muted-foreground mb-0.5">
              Refresh window (days)
            </span>
            <input
              type="number"
              min={1}
              max={365}
              value={refreshDays}
              onChange={(e) => {
                const v = e.target.value.trim();
                if (!v) return;
                const n = parseInt(v, 10);
                if (!isNaN(n)) onRefreshDaysChange(n);
              }}
              disabled={disabled}
              className="w-full rounded-md border bg-background px-2 py-1 text-sm disabled:opacity-60"
            />
            <span className="mt-0.5 block text-[11px] text-muted-foreground">
              Same (IP, MAC) pair won't re-scan within this many days. 30 is a
              sane default — Wi-Fi roam churn won't re-trigger.
            </span>
          </label>
        </div>
      )}
    </div>
  );
}

/**
 * Compliance classification — first-class boolean flags on the
 * subnet for PCI / HIPAA / internet-facing scope. Used by the
 * Compliance dashboard at /admin/compliance to answer auditor
 * queries ("show me every PCI subnet") with indexed predicates
 * rather than freeform-tag scans. Subnet-level only, no
 * inheritance — a parent block being PCI-scope doesn't
 * automatically tag its children. Operators tag deliberately.
 */
function ClassificationSection({
  pciScope,
  hipaaScope,
  internetFacing,
  onPciChange,
  onHipaaChange,
  onInternetFacingChange,
}: {
  pciScope: boolean;
  hipaaScope: boolean;
  internetFacing: boolean;
  onPciChange: (v: boolean) => void;
  onHipaaChange: (v: boolean) => void;
  onInternetFacingChange: (v: boolean) => void;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-1.5">
        <ShieldCheck className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-xs font-medium">Compliance classification</span>
      </div>
      <p className="text-[11px] text-muted-foreground">
        First-class scope flags surfaced on the Compliance dashboard. Indexed
        for auditor queries.
      </p>
      <div className="space-y-1.5 pl-5 border-l-2 border-muted">
        <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
          <input
            type="checkbox"
            checked={pciScope}
            onChange={(e) => onPciChange(e.target.checked)}
            className="h-3.5 w-3.5"
          />
          <span>
            <span className="font-medium">PCI scope</span>
            <span className="text-muted-foreground">
              {" "}
              — handles cardholder data
            </span>
          </span>
        </label>
        <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
          <input
            type="checkbox"
            checked={hipaaScope}
            onChange={(e) => onHipaaChange(e.target.checked)}
            className="h-3.5 w-3.5"
          />
          <span>
            <span className="font-medium">HIPAA scope</span>
            <span className="text-muted-foreground"> — handles ePHI</span>
          </span>
        </label>
        <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
          <input
            type="checkbox"
            checked={internetFacing}
            onChange={(e) => onInternetFacingChange(e.target.checked)}
            className="h-3.5 w-3.5"
          />
          <span>
            <span className="font-medium">Internet-facing</span>
            <span className="text-muted-foreground">
              {" "}
              — directly reachable from the public internet
            </span>
          </span>
        </label>
      </div>
    </div>
  );
}

function DdnsSettingsSection({
  enabled,
  policy,
  domainOverride,
  ttl,
  subnetNetwork,
  onEnabledChange,
  onPolicyChange,
  onDomainOverrideChange,
  onTtlChange,
}: {
  enabled: boolean;
  policy: DdnsPolicy;
  domainOverride: string | null;
  ttl: number | null;
  subnetNetwork?: string;
  onEnabledChange: (v: boolean) => void;
  onPolicyChange: (v: DdnsPolicy) => void;
  onDomainOverrideChange: (v: string | null) => void;
  onTtlChange: (v: number | null) => void;
}) {
  // Preview: synthesise what ``dhcp-<tail>`` would look like for the
  // first host IP in this subnet. Mirrors the Python implementation in
  // ``backend/app/services/dns/ddns.py::_generate_hostname`` — keep in
  // sync when policy generators change.
  const preview = (() => {
    if (!subnetNetwork) return null;
    const match = subnetNetwork.match(/^([\d.]+)\/(\d+)$/);
    if (!match) return null;
    const octets = match[1].split(".").map((s) => parseInt(s, 10));
    if (octets.length !== 4 || octets.some((n) => isNaN(n))) return null;
    const prefix = parseInt(match[2], 10);
    if (prefix < 16) return null;
    // First usable: network + 1 for /30 and shorter, + 1 for /31 / /32
    octets[3] = (octets[3] + 1) & 0xff;
    return `dhcp-${octets[2]}-${octets[3]}`;
  })();

  const disabled = !enabled;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <Network className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="text-xs font-medium">Dynamic DNS (from DHCP)</span>
        </div>
        <label className="flex items-center gap-1.5 text-xs cursor-pointer select-none">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => onEnabledChange(e.target.checked)}
            className="h-3.5 w-3.5"
          />
          Enabled
        </label>
      </div>
      {!enabled && (
        <p className="text-xs text-muted-foreground italic">
          When enabled, DHCP leases in this subnet will publish A/AAAA + PTR
          records into the subnet's forward / reverse zones.
        </p>
      )}
      {enabled && (
        <div className="space-y-2 pl-5 border-l-2 border-muted">
          <label className="block text-xs">
            <span className="block text-muted-foreground mb-0.5">
              Hostname policy
            </span>
            <select
              value={policy}
              onChange={(e) => onPolicyChange(e.target.value as DdnsPolicy)}
              disabled={disabled}
              className="w-full rounded-md border bg-background px-2 py-1 text-sm disabled:opacity-60"
            >
              <option value="client_or_generated">
                client_or_generated — use client hostname else generate
              </option>
              <option value="client_provided">
                client_provided — skip if no client hostname
              </option>
              <option value="always_generate">
                always_generate — ignore client, always synthesise
              </option>
              <option value="disabled">disabled — no DDNS records</option>
            </select>
          </label>
          {preview && policy !== "client_provided" && policy !== "disabled" && (
            <p className="text-[11px] text-muted-foreground">
              Generated names look like{" "}
              <code className="rounded bg-muted px-1">{preview}</code>.
            </p>
          )}
          <label className="block text-xs">
            <span className="block text-muted-foreground mb-0.5">
              Domain override (optional)
            </span>
            <input
              type="text"
              value={domainOverride ?? ""}
              onChange={(e) =>
                onDomainOverrideChange(e.target.value.trim() || null)
              }
              placeholder="dhcp.corp.example.com"
              disabled={disabled}
              className="w-full rounded-md border bg-background px-2 py-1 text-sm disabled:opacity-60"
            />
            <span className="mt-0.5 block text-[11px] text-muted-foreground">
              Publish DDNS records into this zone instead of the subnet's
              primary forward zone. Leave blank to use the subnet's zone.
            </span>
          </label>
          <label className="block text-xs">
            <span className="block text-muted-foreground mb-0.5">
              TTL override (seconds, optional)
            </span>
            <input
              type="number"
              min={30}
              value={ttl ?? ""}
              onChange={(e) => {
                const v = e.target.value.trim();
                onTtlChange(v ? parseInt(v, 10) : null);
              }}
              placeholder="300"
              disabled={disabled}
              className="w-full rounded-md border bg-background px-2 py-1 text-sm disabled:opacity-60"
            />
          </label>
        </div>
      )}
    </div>
  );
}

// ─── Additional Zones dual-listbox ──────────────────────────────────────────

function AdditionalZonesPicker({
  allZones,
  selectedIds,
  onChange,
  disabled,
}: {
  allZones: DNSZone[];
  selectedIds: string[];
  onChange: (ids: string[]) => void;
  disabled: boolean;
}) {
  const [leftFilter, setLeftFilter] = useState("");
  const [rightFilter, setRightFilter] = useState("");
  const [leftPick, setLeftPick] = useState<Set<string>>(new Set());
  const [rightPick, setRightPick] = useState<Set<string>>(new Set());

  const selected = allZones.filter((z) => selectedIds.includes(z.id));
  const available = allZones.filter((z) => !selectedIds.includes(z.id));

  const leftFiltered = available.filter((z) =>
    z.name.toLowerCase().includes(leftFilter.toLowerCase()),
  );
  const rightFiltered = selected.filter((z) =>
    z.name.toLowerCase().includes(rightFilter.toLowerCase()),
  );

  const moveRight = () => {
    if (leftPick.size === 0) return;
    onChange([...selectedIds, ...Array.from(leftPick)]);
    setLeftPick(new Set());
  };
  const moveLeft = () => {
    if (rightPick.size === 0) return;
    onChange(selectedIds.filter((id) => !rightPick.has(id)));
    setRightPick(new Set());
  };
  const moveAllRight = () =>
    onChange([...selectedIds, ...leftFiltered.map((z) => z.id)]);
  const moveAllLeft = () =>
    onChange(
      selectedIds.filter((id) => !rightFiltered.some((z) => z.id === id)),
    );

  function List({
    label,
    items,
    filter,
    onFilter,
    picks,
    onPicks,
    onDouble,
  }: {
    label: string;
    items: DNSZone[];
    filter: string;
    onFilter: (v: string) => void;
    picks: Set<string>;
    onPicks: (s: Set<string>) => void;
    onDouble: (id: string) => void;
  }) {
    return (
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[11px] font-medium text-muted-foreground">
            {label}
          </span>
          <span className="text-[11px] text-muted-foreground/70">
            {items.length}
          </span>
        </div>
        <input
          type="text"
          value={filter}
          onChange={(e) => onFilter(e.target.value)}
          placeholder="Filter…"
          disabled={disabled}
          className="w-full rounded-t-md border border-b-0 bg-background px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        />
        <div className="h-28 min-h-[5rem] max-h-[24rem] resize-y overflow-y-auto rounded-b-md border bg-background">
          {items.length === 0 ? (
            <p className="p-2 text-center text-[11px] text-muted-foreground italic">
              —
            </p>
          ) : (
            items.map((z) => {
              const on = picks.has(z.id);
              return (
                <button
                  key={z.id}
                  type="button"
                  disabled={disabled}
                  onDoubleClick={() => onDouble(z.id)}
                  onClick={() => {
                    const next = new Set(picks);
                    if (on) next.delete(z.id);
                    else next.add(z.id);
                    onPicks(next);
                  }}
                  className={`block w-full truncate px-2 py-0.5 text-left text-xs ${
                    on ? "bg-primary/20" : "hover:bg-muted/50"
                  } disabled:opacity-50`}
                >
                  {z.name}
                </button>
              );
            })
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <List
        label="Available"
        items={leftFiltered}
        filter={leftFilter}
        onFilter={setLeftFilter}
        picks={leftPick}
        onPicks={setLeftPick}
        onDouble={(id) => onChange([...selectedIds, id])}
      />
      <div className="flex items-center justify-center gap-1">
        <button
          type="button"
          onClick={moveAllLeft}
          disabled={disabled || rightFiltered.length === 0}
          title="Remove all (filtered)"
          className="rounded border px-2 py-0.5 text-xs hover:bg-accent disabled:opacity-40"
        >
          ▲▲
        </button>
        <button
          type="button"
          onClick={moveLeft}
          disabled={disabled || rightPick.size === 0}
          title="Remove selected"
          className="rounded border px-2 py-0.5 text-xs hover:bg-accent disabled:opacity-40"
        >
          ▲
        </button>
        <span className="mx-2 text-xs text-muted-foreground">
          {selectedIds.length} selected
        </span>
        <button
          type="button"
          onClick={moveRight}
          disabled={disabled || leftPick.size === 0}
          title="Add selected"
          className="rounded border px-2 py-0.5 text-xs hover:bg-accent disabled:opacity-40"
        >
          ▼
        </button>
        <button
          type="button"
          onClick={moveAllRight}
          disabled={disabled || leftFiltered.length === 0}
          title="Add all (filtered)"
          className="rounded border px-2 py-0.5 text-xs hover:bg-accent disabled:opacity-40"
        >
          ▼▼
        </button>
      </div>
      <List
        label="Selected"
        items={rightFiltered}
        filter={rightFilter}
        onFilter={setRightFilter}
        picks={rightPick}
        onPicks={setRightPick}
        onDouble={(id) => onChange(selectedIds.filter((x) => x !== id))}
      />
    </div>
  );
}

// ─── VLAN Picker (Router + VLAN selects with inline create) ───────────────────

function VlanPicker({
  vlanRefId,
  onChange,
}: {
  vlanRefId: string | null;
  onChange: (vlanRefId: string | null) => void;
}) {
  const qc = useQueryClient();
  const { data: routers = [] } = useQuery({
    queryKey: ["vlans", "routers"],
    queryFn: vlansApi.listRouters,
  });

  // Seed the router from the selected VLAN, if any.
  const [routerId, setRouterId] = useState<string>("");
  const { data: selectedVlan } = useQuery({
    queryKey: ["vlans", "vlan", vlanRefId],
    queryFn: () => vlansApi.getVlan(vlanRefId as string),
    enabled: !!vlanRefId && !routerId,
  });
  useEffect(() => {
    if (selectedVlan && !routerId) setRouterId(selectedVlan.router_id);
  }, [selectedVlan, routerId]);

  const { data: vlans = [] } = useQuery({
    queryKey: ["vlans", routerId],
    queryFn: () => vlansApi.listVlans(routerId),
    enabled: !!routerId,
  });

  const [showInline, setShowInline] = useState(false);
  const [inlineTag, setInlineTag] = useState("");
  const [inlineName, setInlineName] = useState("");
  const [inlineError, setInlineError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  async function handleInlineCreate() {
    const n = parseInt(inlineTag, 10);
    if (Number.isNaN(n) || n < 1 || n > 4094) {
      setInlineError("VLAN tag must be 1–4094");
      return;
    }
    if (!inlineName.trim()) {
      setInlineError("Name is required");
      return;
    }
    setCreating(true);
    setInlineError(null);
    try {
      const created = await vlansApi.createVlan(routerId, {
        vlan_id: n,
        name: inlineName.trim(),
      });
      qc.invalidateQueries({ queryKey: ["vlans", routerId] });
      onChange(created.id);
      setShowInline(false);
      setInlineTag("");
      setInlineName("");
    } catch (e: unknown) {
      const msg =
        (e as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to create VLAN";
      setInlineError(typeof msg === "string" ? msg : JSON.stringify(msg));
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <Field label="Router">
          <select
            className={inputCls}
            value={routerId}
            onChange={(e) => {
              setRouterId(e.target.value);
              onChange(null);
              setShowInline(false);
            }}
          >
            <option value="">— None —</option>
            {routers.map((r: NetworkRouter) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="VLAN">
          <select
            className={inputCls}
            value={vlanRefId ?? ""}
            onChange={(e) => onChange(e.target.value || null)}
            disabled={!routerId}
          >
            <option value="">— None —</option>
            {vlans.map((v: VLAN) => (
              <option key={v.id} value={v.id}>
                {v.vlan_id} — {v.name}
              </option>
            ))}
          </select>
        </Field>
      </div>
      {routerId && !showInline && (
        <button
          type="button"
          onClick={() => setShowInline(true)}
          className="text-xs text-primary hover:underline"
        >
          + Create VLAN
        </button>
      )}
      {routerId && showInline && (
        <div className="rounded-md border p-2 space-y-2 bg-muted/30">
          <div className="grid grid-cols-[80px_1fr] gap-2">
            <input
              className={inputCls}
              type="number"
              placeholder="Tag"
              value={inlineTag}
              onChange={(e) => setInlineTag(e.target.value)}
              autoFocus
            />
            <input
              className={inputCls}
              placeholder="Name"
              value={inlineName}
              onChange={(e) => setInlineName(e.target.value)}
            />
          </div>
          {inlineError && (
            <p className="text-[11px] text-destructive">{inlineError}</p>
          )}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setShowInline(false);
                setInlineError(null);
              }}
              className="rounded-md border px-2 py-0.5 text-xs hover:bg-muted"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleInlineCreate}
              disabled={creating}
              className="rounded-md bg-primary px-2 py-0.5 text-xs text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {creating ? "Adding…" : "Add VLAN"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Create Subnet Modal ──────────────────────────────────────────────────────

// Prefix options for the "Find by size" picker. We offer the full IPv4 range
// (/8-/32) plus the common IPv6 sizes. The CreateSubnetModal filters this
// list to just the values larger than the selected block's prefix.
const PREFIX_OPTIONS_V4 = Array.from({ length: 25 }, (_, i) => i + 8); // /8 – /32
const PREFIX_OPTIONS_V6 = [
  32, 40, 44, 48, 52, 56, 60, 64, 72, 80, 96, 112, 120, 124, 127, 128,
];

function isIPv6Cidr(cidr: string): boolean {
  return cidr.includes(":");
}

function CreateSubnetModal({
  spaceId,
  defaultBlockId,
  defaultNetwork,
  onClose,
}: {
  spaceId: string;
  defaultBlockId?: string;
  defaultNetwork?: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [subnetMode, setSubnetMode] = useState<"manual" | "size">("manual");
  const [network, setNetwork] = useState(defaultNetwork ?? "");
  const [name, setName] = useState("");
  const [blockId, setBlockId] = useState(defaultBlockId ?? "");
  const [gateway, setGateway] = useState("");
  const [vlanRefId, setVlanRefId] = useState<string | null>(null);
  const [vxlanId, setVxlanId] = useState<string>("");
  const [skipAuto, setSkipAuto] = useState(false);
  const [customFields, setCustomFields] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);

  // "Find by size" state
  const [prefixLen, setPrefixLen] = useState("24");
  const [selectedNet, setSelectedNet] = useState("");

  // DNS state
  const [dnsInherit, setDnsInherit] = useState(true);
  const [dnsGroupIds, setDnsGroupIds] = useState<string[]>([]);
  const [dnsZoneId, setDnsZoneId] = useState<string | null>(null);
  const [dnsAdditionalZoneIds, setDnsAdditionalZoneIds] = useState<string[]>(
    [],
  );
  // DHCP state
  const [dhcpInherit, setDhcpInherit] = useState(true);
  const [dhcpServerGroupId, setDhcpServerGroupId] = useState<string | null>(
    null,
  );
  // DDNS state — subnet-only, no inheritance for MVP
  const [ddnsEnabled, setDdnsEnabled] = useState(false);
  const [ddnsPolicy, setDdnsPolicy] = useState<DdnsPolicy>(
    "client_or_generated",
  );
  const [ddnsDomainOverride, setDdnsDomainOverride] = useState<string | null>(
    null,
  );
  const [ddnsTtl, setDdnsTtl] = useState<number | null>(null);
  // Device profiling state — Phase 1 active layer.
  const [autoProfileEnabled, setAutoProfileEnabled] = useState(false);
  const [autoProfilePreset, setAutoProfilePreset] =
    useState<AutoProfilePreset>("service_and_os");
  const [autoProfileRefreshDays, setAutoProfileRefreshDays] = useState(30);
  // Compliance / classification flags (issue #75).
  const [pciScope, setPciScope] = useState(false);
  const [hipaaScope, setHipaaScope] = useState(false);
  const [internetFacing, setInternetFacing] = useState(false);
  // Optional template pre-fill (issue #26).
  const [templateId, setTemplateId] = useState<string>("");

  const { data: blocks } = useQuery({
    queryKey: ["blocks", spaceId],
    queryFn: () => ipamApi.listBlocks(spaceId),
  });
  const { data: subnetTemplates } = useQuery({
    queryKey: ["ipam-templates", "subnet"],
    queryFn: () => ipamApi.listTemplates({ applies_to: "subnet" }),
  });

  // Narrow the prefix picker to values valid for the selected block's family
  // and larger than the block's own prefix. /24 stays the default for IPv4,
  // /64 for IPv6.
  const selectedBlock = blocks?.find((b) => b.id === blockId);
  const blockIsV6 = selectedBlock ? isIPv6Cidr(selectedBlock.network) : false;
  const blockPrefixOptions = (() => {
    if (!selectedBlock) return PREFIX_OPTIONS_V4;
    const own = parseInt(selectedBlock.network.split("/")[1] ?? "0", 10);
    const pool = blockIsV6 ? PREFIX_OPTIONS_V6 : PREFIX_OPTIONS_V4;
    return pool.filter((p) => p > own);
  })();
  useEffect(() => {
    if (blockPrefixOptions.length === 0) return;
    const current = parseInt(prefixLen, 10);
    if (!blockPrefixOptions.includes(current)) {
      const preferred = blockIsV6 ? 64 : 24;
      const next = blockPrefixOptions.includes(preferred)
        ? preferred
        : blockPrefixOptions[0];
      setPrefixLen(String(next));
      setSelectedNet("");
    }
  }, [blockId, blockIsV6]); // eslint-disable-line react-hooks/exhaustive-deps

  const { data: cfDefs = [] } = useQuery({
    queryKey: ["custom-fields", "subnet"],
    queryFn: () => customFieldsApi.list("subnet"),
  });

  // Available subnets query (only active when in size mode and block + prefix are set)
  const { data: availableNets = [], isFetching: searchingNets } = useQuery({
    queryKey: ["available-subnets", blockId, prefixLen],
    queryFn: () => ipamApi.availableSubnets(blockId, parseInt(prefixLen)),
    enabled: subnetMode === "size" && !!blockId && !!prefixLen,
    staleTime: 15_000,
  });

  // Auto-select if only one block
  useEffect(() => {
    if (!blockId && blocks?.length === 1) setBlockId(blocks[0].id);
  }, [blocks, blockId]);

  // When switching to size mode, clear manually typed network
  function switchMode(m: "manual" | "size") {
    setSubnetMode(m);
    setNetwork("");
    setSelectedNet("");
    setError(null);
  }

  // The actual network to submit — typed value in manual mode, selected in size mode
  const effectiveNetwork = subnetMode === "manual" ? network : selectedNet;

  const mutation = useMutation({
    mutationFn: () =>
      ipamApi.createSubnet({
        space_id: spaceId,
        block_id: blockId,
        network: effectiveNetwork,
        name: name || undefined,
        gateway: gateway || undefined,
        vlan_ref_id: vlanRefId ?? undefined,
        vxlan_id: vxlanId.trim() ? Number(vxlanId.trim()) : null,
        status: "active",
        skip_auto_addresses: skipAuto,
        custom_fields: customFields,
        dns_inherit_settings: dnsInherit,
        ...(dnsInherit
          ? {}
          : {
              dns_group_ids: dnsGroupIds,
              dns_zone_id: dnsZoneId,
              dns_additional_zone_ids: dnsAdditionalZoneIds,
            }),
        dhcp_inherit_settings: dhcpInherit,
        ...(dhcpInherit ? {} : { dhcp_server_group_id: dhcpServerGroupId }),
        ddns_enabled: ddnsEnabled,
        ddns_hostname_policy: ddnsPolicy,
        ddns_domain_override: ddnsDomainOverride,
        ddns_ttl: ddnsTtl,
        auto_profile_on_dhcp_lease: autoProfileEnabled,
        auto_profile_preset: autoProfilePreset,
        auto_profile_refresh_days: autoProfileRefreshDays,
        pci_scope: pciScope,
        hipaa_scope: hipaaScope,
        internet_facing: internetFacing,
        ...(templateId ? { template_id: templateId } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnets", spaceId] });
      qc.invalidateQueries({ queryKey: ["spaces"] });
      // Refresh the VLAN detail page's "Subnets using this VLAN" list
      qc.invalidateQueries({ queryKey: ["subnets-by-vlan"] });
      onClose();
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to create subnet";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  return (
    <Modal title="New Subnet" onClose={onClose} wide>
      <div className="space-y-3 max-h-[75vh] overflow-y-auto pr-1">
        {/* Mode toggle */}
        <div className="flex gap-2">
          {(["manual", "size"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => switchMode(m)}
              className={cn(
                "flex-1 rounded-md border px-3 py-1.5 text-sm",
                subnetMode === m
                  ? "bg-primary text-primary-foreground border-primary"
                  : "hover:bg-muted",
              )}
            >
              {m === "manual" ? "Manual CIDR" : "Find by size"}
            </button>
          ))}
        </div>

        {(subnetTemplates ?? []).length > 0 && (
          <Field label="Apply template (optional)">
            <select
              className={inputCls}
              value={templateId}
              onChange={(e) => setTemplateId(e.target.value)}
            >
              <option value="">— none —</option>
              {(subnetTemplates ?? []).map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                  {t.description ? ` — ${t.description}` : ""}
                </option>
              ))}
            </select>
            {templateId && (
              <p className="mt-1 text-xs text-muted-foreground">
                Operator-supplied fields below override the template's defaults.
              </p>
            )}
          </Field>
        )}

        <Field label="Block *">
          <select
            className={inputCls}
            value={blockId}
            onChange={(e) => {
              setBlockId(e.target.value);
              setSelectedNet("");
            }}
          >
            <option value="">Select a block…</option>
            {blocks?.map((b: IPBlock) => (
              <option key={b.id} value={b.id}>
                {b.network}
                {b.name ? ` — ${b.name}` : ""}
              </option>
            ))}
          </select>
          {blocks?.length === 0 && (
            <p className="text-xs text-amber-600 mt-1">
              No blocks in this space. Create a block first.
            </p>
          )}
        </Field>

        {subnetMode === "manual" ? (
          <Field label="Network (CIDR)">
            <input
              className={inputCls}
              value={network}
              onChange={(e) => {
                setNetwork(e.target.value);
                setError(null);
              }}
              placeholder="e.g. 10.0.1.0/24 or 2001:db8:1::/64"
              autoFocus
            />
          </Field>
        ) : (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Field label="Prefix size">
                <select
                  className={inputCls}
                  value={prefixLen}
                  onChange={(e) => {
                    setPrefixLen(e.target.value);
                    setSelectedNet("");
                  }}
                >
                  {blockPrefixOptions.map((n) => (
                    <option key={n} value={String(n)}>
                      /{n}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            {!blockId ? (
              <p className="text-xs text-muted-foreground italic">
                Select a block first.
              </p>
            ) : searchingNets ? (
              <p className="text-xs text-muted-foreground italic">Searching…</p>
            ) : availableNets.length === 0 ? (
              <p className="text-xs text-amber-600 italic">
                No available /{prefixLen} subnets in this block.
              </p>
            ) : (
              <div>
                <p className="text-xs text-muted-foreground mb-1">
                  Available /{prefixLen} subnets (click to select):
                </p>
                <div className="flex flex-wrap gap-1.5 max-h-36 overflow-y-auto">
                  {availableNets.map((net: string) => (
                    <button
                      key={net}
                      type="button"
                      onClick={() => setSelectedNet(net)}
                      className={cn(
                        "font-mono rounded border px-2 py-0.5 text-xs transition-colors",
                        selectedNet === net
                          ? "bg-primary text-primary-foreground border-primary"
                          : "bg-background hover:border-primary/50",
                      )}
                    >
                      {net}
                    </button>
                  ))}
                </div>
                {selectedNet && (
                  <p className="text-xs text-emerald-600 dark:text-emerald-400 mt-1">
                    Selected: <span className="font-mono">{selectedNet}</span>
                  </p>
                )}
              </div>
            )}
          </div>
        )}

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
        <VlanPicker vlanRefId={vlanRefId} onChange={setVlanRefId} />
        <Field label="VXLAN ID (optional)">
          <input
            type="number"
            min={1}
            max={16777214}
            placeholder="1 – 16777214"
            value={vxlanId}
            onChange={(e) => setVxlanId(e.target.value)}
            className={inputCls}
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
        <CustomFieldsSection
          definitions={cfDefs}
          values={customFields}
          onChange={(k, v) => setCustomFields((prev) => ({ ...prev, [k]: v }))}
        />
        <div className="border-t pt-3">
          <DnsSettingsSection
            inherit={dnsInherit}
            groupIds={dnsGroupIds}
            zoneId={dnsZoneId}
            additionalZoneIds={dnsAdditionalZoneIds}
            onInheritChange={setDnsInherit}
            onGroupIdsChange={setDnsGroupIds}
            onZoneIdChange={setDnsZoneId}
            onAdditionalZoneIdsChange={setDnsAdditionalZoneIds}
            parentBlockId={blockId || null}
          />
        </div>
        <div className="border-t pt-3">
          <DhcpSettingsSection
            inherit={dhcpInherit}
            serverGroupId={dhcpServerGroupId}
            onInheritChange={setDhcpInherit}
            onServerGroupIdChange={setDhcpServerGroupId}
            parentBlockId={blockId || null}
            fallbackSpaceId={spaceId}
          />
        </div>
        <div className="border-t pt-3">
          <DdnsSettingsSection
            enabled={ddnsEnabled}
            policy={ddnsPolicy}
            domainOverride={ddnsDomainOverride}
            ttl={ddnsTtl}
            subnetNetwork={effectiveNetwork}
            onEnabledChange={setDdnsEnabled}
            onPolicyChange={setDdnsPolicy}
            onDomainOverrideChange={setDdnsDomainOverride}
            onTtlChange={setDdnsTtl}
          />
        </div>
        <div className="border-t pt-3">
          <ProfilingSettingsSection
            enabled={autoProfileEnabled}
            preset={autoProfilePreset}
            refreshDays={autoProfileRefreshDays}
            onEnabledChange={setAutoProfileEnabled}
            onPresetChange={setAutoProfilePreset}
            onRefreshDaysChange={setAutoProfileRefreshDays}
          />
        </div>
        <div className="border-t pt-3">
          <ClassificationSection
            pciScope={pciScope}
            hipaaScope={hipaaScope}
            internetFacing={internetFacing}
            onPciChange={setPciScope}
            onHipaaChange={setHipaaScope}
            onInternetFacingChange={setInternetFacing}
          />
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              setError(null);
              mutation.mutate();
            }}
            disabled={!effectiveNetwork || !blockId || mutation.isPending}
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

const IP_STATUS_OPTIONS = [
  "allocated",
  "reserved",
  "dhcp",
  "static_dhcp",
  "deprecated",
] as const;

// IP allocate/edit/delete cascades into DNS via ``_sync_dns_record``
// (auto A + PTR) and can land a DHCP static when ``status='static_dhcp'``.
// Every mutation that invalidates ``["addresses", ...]`` below also
// invalidates ``["dns-records"]`` / ``["dns-group-records"]`` /
// ``["dns-zones"]`` so a PTR created from IPAM appears in the Windows
// reverse zone's record list immediately instead of after a full page
// reload. Partial keys match hierarchically in react-query, so the
// unkeyed variants catch every per-zone and per-group query in one pass.

// Non-fatal collision surfaced by the backend on IP assign / edit. The
// server returns 409 with ``detail = { warnings: [...], requires_confirmation: true }``
// when ``force=false`` and the pending (hostname, zone) or MAC already
// exists on another IP. The user sees the list inline and re-submits with
// ``force=true`` to proceed.
type CollisionWarning =
  | {
      kind: "fqdn_collision";
      fqdn: string;
      existing_ip: string;
      existing_subnet: string;
      existing_ip_id: string;
    }
  | {
      kind: "mac_collision";
      mac_address: string;
      existing_ip: string;
      existing_hostname: string | null;
      existing_subnet: string;
      existing_ip_id: string;
    };

function parseCollisionWarnings(err: unknown): CollisionWarning[] | null {
  const e = err as {
    response?: { status?: number; data?: { detail?: unknown } };
  };
  if (e?.response?.status !== 409) return null;
  const detail = e.response.data?.detail;
  if (
    !detail ||
    typeof detail !== "object" ||
    !Array.isArray((detail as { warnings?: unknown }).warnings)
  ) {
    return null;
  }
  return (detail as { warnings: CollisionWarning[] }).warnings;
}

function CollisionWarningBanner({
  warnings,
}: {
  warnings: CollisionWarning[];
}) {
  return (
    <div className="rounded-md border border-amber-500/60 bg-amber-500/10 p-2 text-xs">
      <p className="mb-1 font-medium text-amber-700 dark:text-amber-400">
        Heads up — this assignment conflicts with existing IPs:
      </p>
      <ul className="ml-4 list-disc space-y-0.5">
        {warnings.map((w, i) => (
          <li key={i}>
            {w.kind === "fqdn_collision" ? (
              <>
                FQDN <span className="font-mono">{w.fqdn}</span> is already on{" "}
                <span className="font-mono">{w.existing_ip}</span> in{" "}
                <span className="font-mono">{w.existing_subnet}</span>
              </>
            ) : (
              <>
                MAC <span className="font-mono">{w.mac_address}</span> is
                already on <span className="font-mono">{w.existing_ip}</span>
                {w.existing_hostname ? ` (${w.existing_hostname})` : ""} in{" "}
                <span className="font-mono">{w.existing_subnet}</span>
              </>
            )}
          </li>
        ))}
      </ul>
      <p className="mt-1 text-muted-foreground">
        Click the button again to save anyway, or cancel to adjust.
      </p>
    </div>
  );
}

function AddAddressModal({
  subnetId,
  presetRange,
  onClose,
}: {
  subnetId: string;
  /** When set (e.g. operator clicked a "free range" gap row), the
   * modal opens locked to manual mode with the address pre-filled to
   * the start of the range and a row of quick-pick buttons for first
   * / next / last / random within the range. */
  presetRange?: { startIpInt: number; endIpInt: number } | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<"manual" | "next">(
    presetRange ? "manual" : "next",
  );
  const [address, setAddress] = useState(
    presetRange ? intToIpv4(presetRange.startIpInt) : "",
  );
  const [hostname, setHostname] = useState("");
  const [mac, setMac] = useState("");
  const [description, setDescription] = useState("");
  const [ipStatus, setIpStatus] = useState("allocated");
  const [role, setRole] = useState<string>("");
  const [reservedUntil, setReservedUntil] = useState<string>("");
  const [customFields, setCustomFields] = useState<Record<string, unknown>>({});
  const [dnsZoneId, setDnsZoneId] = useState<string>("");
  // Issue #25 — split-horizon publishing. Extra zone UUIDs to
  // publish beyond the singular primary. Only surfaced when the
  // subnet has ``dns_split_horizon`` on (effective).
  const [extraZoneIds, setExtraZoneIds] = useState<string[]>([]);
  const [dhcpScopeId, setDhcpScopeId] = useState<string>("");
  const [aliases, setAliases] = useState<
    { name: string; record_type: "CNAME" | "A" }[]
  >([]);
  const [error, setError] = useState<string | null>(null);
  const [pendingWarnings, setPendingWarnings] = useState<
    CollisionWarning[] | null
  >(null);
  const needsDhcpScope = ipStatus === "dhcp" || ipStatus === "static_dhcp";

  // Scopes load unconditionally (cheap) so we can do the dynamic-pool
  // check + pool warnings even before the user flips to ``static_dhcp``.
  const { data: dhcpScopes = [] } = useQuery({
    queryKey: ["dhcp-scopes-subnet", subnetId],
    queryFn: () => dhcpApi.listScopesBySubnet(subnetId),
  });
  const poolQueries = useQueries({
    queries: dhcpScopes.map((sc) => ({
      queryKey: ["dhcp-pools", sc.id],
      queryFn: () => dhcpApi.listPools(sc.id),
      staleTime: 60_000,
    })),
  });
  const allPools = poolQueries.flatMap((q) => q.data ?? []);

  useEffect(() => {
    if (needsDhcpScope && !dhcpScopeId && dhcpScopes.length > 0) {
      setDhcpScopeId(dhcpScopes[0].id);
    }
  }, [needsDhcpScope, dhcpScopes.length]); // eslint-disable-line react-hooks/exhaustive-deps

  // Which dynamic pool does the manually-entered IP fall in, if any?
  // Server-side ``create_address`` will reject with 422 regardless, but
  // a red inline warning + disabled submit button is friendlier.
  const typedDynamicPool = (() => {
    if (mode !== "manual" || !address) return null;
    const ipInt = ipStringToInt(address);
    if (!Number.isFinite(ipInt)) return null;
    for (const p of allPools) {
      if (p.pool_type !== "dynamic") continue;
      const s = ipStringToInt(p.start_ip);
      const e = ipStringToInt(p.end_ip);
      if (
        Number.isFinite(s) &&
        Number.isFinite(e) &&
        ipInt >= s &&
        ipInt <= e
      ) {
        return p;
      }
    }
    return null;
  })();

  // Preview the IP the backend would hand out on "next available". Only
  // meaningful for IPv4; the endpoint returns ``address: null`` on v6
  // or when the subnet is exhausted — the UI handles both.
  const { data: nextPreview, isFetching: previewFetching } = useQuery({
    queryKey: ["next-ip-preview", subnetId],
    queryFn: () => ipamApi.previewNextIp(subnetId, "sequential"),
    enabled: mode === "next",
    refetchOnMount: "always",
    staleTime: 0,
  });

  const { data: cfDefs = [] } = useQuery({
    queryKey: ["custom-fields", "ip_address"],
    queryFn: () => customFieldsApi.list("ip_address"),
  });

  // Fetch effective DNS for this subnet to know which zones are available
  const { data: effectiveDns } = useQuery({
    queryKey: ["effective-dns-subnet", subnetId],
    queryFn: () => ipamApi.getEffectiveSubnetDns(subnetId),
    staleTime: 30_000,
  });
  // Issue #25 — split-horizon mode flips the modal into multi-zone
  // picker rendering. Read straight off the subnet row.
  const { data: subnetDetailForSplit } = useQuery({
    queryKey: ["subnet", subnetId],
    queryFn: () => ipamApi.getSubnet(subnetId),
    staleTime: 30_000,
  });
  const splitHorizon = subnetDetailForSplit?.dns_split_horizon ?? false;

  // Load zones from all effective group IDs
  const zoneGroupIds: string[] = effectiveDns?.dns_group_ids ?? [];
  const zoneQueries = useQueries({
    queries: (zoneGroupIds as string[]).map((gId: string) => ({
      queryKey: ["dns-zones", gId],
      queryFn: () => dnsApi.listZones(gId),
      staleTime: 60_000,
    })),
  });
  const allGroupZones: DNSZone[] = zoneQueries
    .flatMap((q: { data?: DNSZone[] }) => q.data ?? [])
    .filter((z: DNSZone) => !z.name.toLowerCase().includes("arpa"));

  // When the block/subnet has an explicit primary zone and/or additional
  // zones, restrict the picker to just those. Falling back to every zone in
  // the group only happens when the admin picked a group without pinning
  // specific zones.
  const explicitZoneIds = [
    ...(effectiveDns?.dns_zone_id ? [effectiveDns.dns_zone_id] : []),
    ...(effectiveDns?.dns_additional_zone_ids ?? []),
  ];
  const availableZones: DNSZone[] =
    explicitZoneIds.length > 0
      ? allGroupZones.filter((z: DNSZone) => explicitZoneIds.includes(z.id))
      : allGroupZones;

  // Pre-select the primary zone (dns_zone_id) or first zone when there's only one
  useEffect(() => {
    if (!dnsZoneId && availableZones.length > 0) {
      const primary = effectiveDns?.dns_zone_id;
      setDnsZoneId(
        primary && availableZones.some((z: DNSZone) => z.id === primary)
          ? primary
          : availableZones[0].id,
      );
    }
  }, [availableZones.length, effectiveDns?.dns_zone_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const mutation = useMutation({
    mutationFn: async (force: boolean) => {
      const zoneParam = dnsZoneId || undefined;
      const cleanedAliases = aliases
        .map((a) => ({ ...a, name: a.name.trim() }))
        .filter((a) => a.name.length > 0);
      // Local datetime input is naive (browser TZ); convert to ISO
      // before sending so the API stores an absolute instant. Empty
      // string means "no TTL" — don't send the field at all.
      const reservedIso =
        ipStatus === "reserved" && reservedUntil
          ? new Date(reservedUntil).toISOString()
          : undefined;
      const roleParam = role || undefined;
      const created =
        mode === "next"
          ? await ipamApi.nextAddress(subnetId, {
              hostname,
              status: ipStatus,
              mac_address: mac || undefined,
              description: description || undefined,
              custom_fields: customFields,
              dns_zone_id: zoneParam,
              extra_zone_ids: extraZoneIds.length ? extraZoneIds : undefined,
              aliases: cleanedAliases.length ? cleanedAliases : undefined,
              role: roleParam as IPRole | undefined,
              reserved_until: reservedIso,
              force,
            })
          : await ipamApi.createAddress({
              subnet_id: subnetId,
              address,
              hostname,
              mac_address: mac || undefined,
              description: description || undefined,
              status: ipStatus,
              custom_fields: customFields,
              dns_zone_id: zoneParam,
              extra_zone_ids: extraZoneIds.length ? extraZoneIds : undefined,
              aliases: cleanedAliases.length ? cleanedAliases : undefined,
              role: roleParam as IPRole | undefined,
              reserved_until: reservedIso,
              force,
            });
      // If the user picked a static_dhcp status and a scope, mirror the row
      // into the DHCP side so the two stay in sync (the backend
      // `_upsert_ipam_for_static` helper will find the existing IPAM row and
      // just link / update it — no duplicate is created).
      if (ipStatus === "static_dhcp" && dhcpScopeId && mac) {
        await dhcpApi.createStatic(dhcpScopeId, {
          ip_address: String(created.address),
          mac_address: mac,
          hostname: hostname || "",
          description: description || "",
        });
      }
      return created;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["addresses", subnetId] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      qc.invalidateQueries({ queryKey: ["dns-group-records"] });
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
      qc.invalidateQueries({ queryKey: ["subnet-aliases", subnetId] });
      qc.invalidateQueries({ queryKey: ["subnets"] });
      onClose();
    },
    onError: (err: unknown) => {
      const warnings = parseCollisionWarnings(err);
      if (warnings && warnings.length > 0) {
        setPendingWarnings(warnings);
        setError(null);
        return;
      }
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to allocate address";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  // Any edit to a collision-relevant field clears the pending warning —
  // the user has changed the assignment, so the prior warning may no
  // longer apply and the next submit should re-run the check fresh.
  useEffect(() => {
    setPendingWarnings(null);
  }, [hostname, mac, dnsZoneId, address]);

  const canSubmit =
    !!hostname.trim() &&
    (mode === "next" ? !!nextPreview?.address : !!address && !typedDynamicPool);

  // Compute preview FQDN
  const selectedZone = availableZones.find((z: DNSZone) => z.id === dnsZoneId);
  const fqdnPreview =
    hostname && selectedZone
      ? `${hostname}.${selectedZone.name.replace(/\.$/, "")}`
      : null;

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
                  : "hover:bg-muted",
              )}
            >
              {m === "next" ? "Next available" : "Specific IP"}
            </button>
          ))}
        </div>
        {mode === "next" && (
          <div className="rounded-md border bg-muted/30 px-3 py-2 text-xs">
            {previewFetching && !nextPreview ? (
              <span className="text-muted-foreground">
                Finding next available IP…
              </span>
            ) : nextPreview?.address ? (
              <>
                <span className="text-muted-foreground">Next available:</span>{" "}
                <span className="font-mono text-sm font-semibold text-emerald-600 dark:text-emerald-400">
                  {nextPreview.address}
                </span>
                <span className="ml-2 text-muted-foreground">
                  (skips dynamic DHCP pools)
                </span>
              </>
            ) : (
              <span className="text-destructive">
                No free IPs in this subnet (all in use or inside a dynamic
                pool).
              </span>
            )}
          </div>
        )}
        {mode === "manual" && (
          <Field label="IP Address">
            {presetRange && (
              <div className="mb-2 rounded-md border border-emerald-400/40 bg-emerald-500/[0.06] px-3 py-2 text-xs">
                <div className="mb-1 text-emerald-700 dark:text-emerald-400">
                  Allocating from free range{" "}
                  <span className="font-mono">
                    {intToIpv4(presetRange.startIpInt)} –{" "}
                    {intToIpv4(presetRange.endIpInt)}
                  </span>{" "}
                  ({presetRange.endIpInt - presetRange.startIpInt + 1} free)
                </div>
                <div className="flex flex-wrap gap-1">
                  <button
                    type="button"
                    onClick={() =>
                      setAddress(intToIpv4(presetRange.startIpInt))
                    }
                    className="rounded border px-2 py-0.5 text-[11px] hover:bg-accent"
                  >
                    First
                  </button>
                  <button
                    type="button"
                    onClick={() => setAddress(intToIpv4(presetRange.endIpInt))}
                    className="rounded border px-2 py-0.5 text-[11px] hover:bg-accent"
                  >
                    Last
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      const span =
                        presetRange.endIpInt - presetRange.startIpInt + 1;
                      const pick =
                        presetRange.startIpInt +
                        Math.floor(Math.random() * span);
                      setAddress(intToIpv4(pick));
                    }}
                    className="rounded border px-2 py-0.5 text-[11px] hover:bg-accent"
                  >
                    Random
                  </button>
                </div>
              </div>
            )}
            <input
              className={inputCls}
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              placeholder="e.g. 10.0.1.42"
              autoFocus
            />
            {typedDynamicPool && (
              <p className="mt-1 rounded-md border border-destructive/40 bg-destructive/10 px-2 py-1 text-xs text-destructive">
                {address} is inside the dynamic DHCP pool{" "}
                <span className="font-mono">
                  {typedDynamicPool.start_ip}–{typedDynamicPool.end_ip}
                </span>
                {typedDynamicPool.name ? ` (${typedDynamicPool.name})` : ""}.
                The DHCP server owns this range — pick an address outside it.
              </p>
            )}
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
        {/* DNS zone selector — only shown when zones are available */}
        {availableZones.length > 0 && (
          <Field label="DNS Zone">
            {availableZones.length === 1 ? (
              <p className="text-xs text-muted-foreground py-1">
                <Globe2 className="inline h-3 w-3 mr-1" />
                {availableZones[0].name.replace(/\.$/, "")}
                {fqdnPreview && (
                  <span className="ml-2 font-mono text-emerald-600 dark:text-emerald-400">
                    → {fqdnPreview}
                  </span>
                )}
              </p>
            ) : (
              <div className="space-y-1">
                <select
                  className={inputCls}
                  value={dnsZoneId}
                  onChange={(e) => setDnsZoneId(e.target.value)}
                >
                  <ZoneOptions
                    zones={availableZones}
                    primaryId={effectiveDns?.dns_zone_id}
                    additionalIds={effectiveDns?.dns_additional_zone_ids ?? []}
                    noneOption="None (no DNS record)"
                  />
                </select>
                {fqdnPreview && (
                  <p className="text-xs font-mono text-emerald-600 dark:text-emerald-400">
                    → {fqdnPreview}
                  </p>
                )}
              </div>
            )}
          </Field>
        )}
        {/* Issue #25 — extra-zone picker for split-horizon publishing.
            Only surfaced when the subnet opted in + at least 2 zones are
            available (otherwise there's nothing to fan out to). */}
        {splitHorizon && availableZones.length > 1 && (
          <Field label="Also publish in (extra zones)">
            <div className="space-y-1 rounded border bg-muted/20 p-2">
              {availableZones
                .filter((z) => z.id !== dnsZoneId)
                .map((z) => {
                  const checked = extraZoneIds.includes(z.id);
                  return (
                    <label
                      key={z.id}
                      className="flex items-center gap-2 text-xs cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5"
                        checked={checked}
                        onChange={(e) => {
                          setExtraZoneIds((prev) =>
                            e.target.checked
                              ? [...prev, z.id]
                              : prev.filter((id) => id !== z.id),
                          );
                        }}
                      />
                      <Globe2 className="h-3 w-3 text-muted-foreground" />
                      <span className="font-mono">
                        {z.name.replace(/\.$/, "")}
                      </span>
                    </label>
                  );
                })}
              <p className="pt-1 text-[11px] text-muted-foreground">
                Each checked zone publishes its own A/AAAA record. Useful for
                split-horizon deployments where the same hostname must resolve
                through both internal and external resolvers.
              </p>
            </div>
          </Field>
        )}
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
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Role">
            <select
              className={inputCls}
              value={role}
              onChange={(e) => setRole(e.target.value)}
            >
              <option value="">— None —</option>
              {IP_ROLE_OPTIONS.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
            {role === "vrrp" || role === "vip" || role === "anycast" ? (
              <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-400">
                Shared-by-design role — MAC collision warnings are suppressed
                for this IP.
              </p>
            ) : null}
          </Field>
          {ipStatus === "reserved" ? (
            <Field label="Reserved until">
              <input
                type="datetime-local"
                className={inputCls}
                value={reservedUntil}
                onChange={(e) => setReservedUntil(e.target.value)}
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                Optional TTL. The reservation sweep returns this IP to{" "}
                <em>available</em> after this time.
              </p>
            </Field>
          ) : (
            <div />
          )}
        </div>
        {needsDhcpScope && (
          <Field label="DHCP Scope">
            {dhcpScopes.length === 0 ? (
              <div className="rounded-md border bg-amber-500/10 border-amber-500/40 px-3 py-2 text-xs">
                No DHCP scope exists for this subnet. Create one from the{" "}
                <span className="font-medium">DHCP Pools</span> tab first.
              </div>
            ) : (
              <select
                className={inputCls}
                value={dhcpScopeId}
                onChange={(e) => setDhcpScopeId(e.target.value)}
              >
                {dhcpScopes.map((sc) => (
                  <option key={sc.id} value={sc.id}>
                    {sc.name || `Scope ${sc.id.slice(0, 8)}`}
                    {" — group "}
                    {sc.group_id.slice(0, 8)}
                  </option>
                ))}
              </select>
            )}
            {ipStatus === "static_dhcp" && !mac && (
              <p className="mt-1 text-xs text-amber-600">
                MAC address required to create a static DHCP reservation.
              </p>
            )}
          </Field>
        )}
        <Field label="Description">
          <input
            className={inputCls}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional"
          />
        </Field>
        <Field label="DNS Aliases">
          <div className="space-y-1.5">
            <p className="text-[11px] text-muted-foreground -mt-0.5">
              Extra records pointing to this IP. CNAMEs point to{" "}
              <span className="font-mono">{hostname || "<hostname>"}</span>; A
              records point to the IP. Deleted automatically when the IP is
              purged.
              {!dnsZoneId && (
                <span className="ml-1 text-amber-600">
                  Requires a DNS zone on this subnet.
                </span>
              )}
            </p>
            {aliases.map((a, idx) => (
              <div key={idx} className="flex items-center gap-2">
                <select
                  className={cn(inputCls, "w-24")}
                  value={a.record_type}
                  onChange={(e) =>
                    setAliases((prev) =>
                      prev.map((x, i) =>
                        i === idx
                          ? {
                              ...x,
                              record_type: e.target.value as "CNAME" | "A",
                            }
                          : x,
                      ),
                    )
                  }
                >
                  <option value="CNAME">CNAME</option>
                  <option value="A">A</option>
                </select>
                <input
                  className={cn(inputCls, "flex-1 min-w-0")}
                  placeholder="alias (e.g. www, mail)"
                  value={a.name}
                  onChange={(e) =>
                    setAliases((prev) =>
                      prev.map((x, i) =>
                        i === idx ? { ...x, name: e.target.value } : x,
                      ),
                    )
                  }
                />
                <button
                  type="button"
                  onClick={() =>
                    setAliases((prev) => prev.filter((_, i) => i !== idx))
                  }
                  className="flex-shrink-0 rounded p-1 text-muted-foreground hover:text-destructive"
                  title="Remove alias"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={() =>
                setAliases((prev) => [
                  ...prev,
                  { name: "", record_type: "CNAME" },
                ])
              }
              disabled={!hostname || !dnsZoneId}
              className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent disabled:opacity-40"
            >
              <Plus className="h-3 w-3" /> Add alias
            </button>
          </div>
        </Field>
        <CustomFieldsSection
          definitions={cfDefs}
          values={customFields}
          onChange={(k, v) => setCustomFields((prev) => ({ ...prev, [k]: v }))}
        />
        {error && <p className="text-xs text-destructive">{error}</p>}
        {pendingWarnings && (
          <CollisionWarningBanner warnings={pendingWarnings} />
        )}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              setError(null);
              mutation.mutate(pendingWarnings != null);
            }}
            disabled={!canSubmit || mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending
              ? "Allocating…"
              : pendingWarnings
                ? "Allocate anyway"
                : "Allocate"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Colored breadcrumb pills ─────────────────────────────────────────────────

type PillVariant = "space" | "block" | "subnet";

const PILL_STYLES: Record<PillVariant, string> = {
  space:
    "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300 hover:bg-blue-200 dark:hover:bg-blue-800/50",
  block:
    "bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300 hover:bg-violet-200 dark:hover:bg-violet-800/50",
  subnet:
    "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300 cursor-default",
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
    visible = [
      items[0],
      { label: "…", variant: items[1].variant },
      ...items.slice(-2),
    ];
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
                PILL_STYLES[item.variant],
              )}
            >
              {item.label}
            </button>
          )}
          {i < visible.length - 1 &&
            item.label !== "…" &&
            visible[i + 1]?.label !== "…" && (
              <ChevronRight className="h-3 w-3 flex-shrink-0 text-muted-foreground/40" />
            )}
        </span>
      ))}
    </div>
  );
}

// ─── Sync dropdown — DNS / DHCP / All ────────────────────────────────────────
//
// Replaces the per-surface "Sync DNS" button with a single ``[Sync ▾]`` menu
// at the subnet level, where the subnet may have both a DNS zone AND one or
// more DHCP scopes attached. DHCP + "All" entries are gated on
// ``hasDhcp`` — blocks/spaces don't carry scopes and keep the old single
// button. Closes on outside click via a mousedown listener on the document.
function SyncMenu({
  onSyncDns,
  onSyncDhcp,
  onSyncAll,
  hasDhcp,
  isPending,
}: {
  onSyncDns: () => void;
  onSyncDhcp: () => void;
  onSyncAll: () => void;
  hasDhcp: boolean;
  isPending: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);

  const itemCls =
    "flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-muted disabled:opacity-50";

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={isPending}
        title="Sync IPAM with DNS and/or DHCP servers"
        className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
      >
        <RefreshCw className={cn("h-3.5 w-3.5", isPending && "animate-spin")} />
        Sync
        <ChevronDown className="h-3.5 w-3.5" />
      </button>
      {open && (
        <div className="absolute right-0 z-20 mt-1 w-44 overflow-hidden rounded-md border bg-popover shadow-md">
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onSyncDns();
            }}
            className={itemCls}
          >
            <Globe2 className="h-3.5 w-3.5" /> DNS
          </button>
          {hasDhcp && (
            <>
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  onSyncDhcp();
                }}
                className={itemCls}
              >
                <Server className="h-3.5 w-3.5" /> DHCP
              </button>
              <div className="border-t" />
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  onSyncAll();
                }}
                className={itemCls}
              >
                <RefreshCw className="h-3.5 w-3.5" /> All
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Tools dropdown — alphabetical bundle of low-frequency subnet ops ───────
//
// Collapses Clean Orphans / Merge / Resize / Scan with nmap / Split into a
// single dropdown so the subnet header doesn't accumulate a row of 9+ buttons
// as we add features. Items are alphabetical to make discovery predictable —
// operators don't have to scan a custom ordering. Closes on outside click via
// a mousedown listener (same pattern as SyncMenu above).
function ToolsMenu({
  onBulkAllocate,
  onCleanOrphans,
  onMerge,
  onResize,
  onScan,
  onSplit,
}: {
  onBulkAllocate: () => void;
  onCleanOrphans: () => void;
  onMerge: () => void;
  onResize: () => void;
  onScan: () => void;
  onSplit: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);

  const itemCls =
    "flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-muted disabled:opacity-50";

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title="Subnet tools — scan, clean, reshape"
        className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
      >
        <Wrench className="h-3.5 w-3.5" />
        Tools
        <ChevronDown className="h-3.5 w-3.5" />
      </button>
      {open && (
        <div className="absolute right-0 z-20 mt-1 w-52 overflow-hidden rounded-md border bg-popover shadow-md">
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onBulkAllocate();
            }}
            className={itemCls}
          >
            <Layers className="h-3.5 w-3.5" /> Bulk allocate…
          </button>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onCleanOrphans();
            }}
            className={itemCls}
          >
            <Trash2 className="h-3.5 w-3.5" /> Clean Orphans
          </button>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onMerge();
            }}
            className={itemCls}
          >
            <GitMerge className="h-3.5 w-3.5" /> Merge…
          </button>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onResize();
            }}
            className={itemCls}
          >
            <Maximize2 className="h-3.5 w-3.5" /> Resize…
          </button>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onScan();
            }}
            className={itemCls}
          >
            <Radar className="h-3.5 w-3.5" /> Scan with nmap
          </button>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onSplit();
            }}
            className={itemCls}
          >
            <Scissors className="h-3.5 w-3.5" /> Split…
          </button>
        </div>
      )}
    </div>
  );
}

// ─── IP <-> int helpers + pool-boundary row type ────────────────────────────
//
// IPv4-only utilities for interleaving pool markers with IP rows in the
// subnet IP list. IPv6 pool markers are out of scope until the IPv6
// allocation story lands.

function ipStringToInt(ip: string): number {
  const p = ip.split(".").map(Number);
  if (p.length !== 4 || p.some((n) => Number.isNaN(n) || n < 0 || n > 255))
    return NaN;
  return ((p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3]) >>> 0;
}

function intToIpv4(n: number): string {
  return [
    (n >>> 24) & 0xff,
    (n >>> 16) & 0xff,
    (n >>> 8) & 0xff,
    n & 0xff,
  ].join(".");
}

type PoolMeta = {
  id: string;
  name: string;
  pool_type: string;
  start_ip: string;
  end_ip: string;
  _start: number;
  _end: number;
};

type AddressOrPoolRow =
  | { kind: "ip"; addr: IPAddress }
  | {
      kind: "pool-boundary";
      pool: PoolMeta;
      boundary: "start" | "end";
    }
  // Visual marker between two non-adjacent IPAM rows (eg .10 and .12,
  // showing .11 is unallocated). Subtle by design — it's a heads-up
  // for "you deleted something and might have missed the hole", not
  // a primary navigation aid. Skipped inside dynamic DHCP pools where
  // gaps are owned by the DHCP server and aren't operator-allocatable.
  | {
      kind: "gap";
      startIpInt: number;
      endIpInt: number;
    };

// ─── Subnet Detail Panel (right pane) ────────────────────────────────────────

function SubnetDetail({
  subnet,
  spaceName,
  block,
  blockAncestors,
  highlightAddressId,
  onSelectSpace,
  onSelectBlock,
  onSubnetEdited,
  onSubnetDeleted,
}: {
  subnet: Subnet;
  spaceName?: string;
  block?: IPBlock;
  blockAncestors?: IPBlock[];
  highlightAddressId?: string | null;
  onSelectSpace?: () => void;
  onSelectBlock?: (b: IPBlock) => void;
  onSubnetEdited: (updated: Subnet) => void;
  onSubnetDeleted?: () => void;
}) {
  const qc = useQueryClient();
  const [showAddModal, setShowAddModal] = useState(false);
  // Optional seed for ``AddAddressModal`` — set when the operator clicks
  // a gap-marker row so the modal opens in manual mode constrained to
  // that contiguous free range.
  const [addModalRange, setAddModalRange] = useState<{
    startIpInt: number;
    endIpInt: number;
  } | null>(null);
  const [showEditSubnet, setShowEditSubnet] = useState(false);
  const [showResizeSubnet, setShowResizeSubnet] = useState(false);
  const [showSplitSubnet, setShowSplitSubnet] = useState(false);
  const [showMergeSubnet, setShowMergeSubnet] = useState(false);
  // Scan-the-whole-subnet trigger from the Tools menu. Re-uses the
  // existing NmapScanModal with the subnet CIDR + subnet_sweep preset
  // pre-filled so the operator just hits "Start scan".
  const [showSubnetScan, setShowSubnetScan] = useState(false);
  // Bulk-allocate (range + name template) from the Tools menu.
  const [showBulkAllocate, setShowBulkAllocate] = useState(false);
  const [showDnsSync, setShowDnsSync] = useState(false);
  const [showDhcpSync, setShowDhcpSync] = useState(false);
  const [showSyncAll, setShowSyncAll] = useState(false);
  const [showOrphans, setShowOrphans] = useState(false);
  // Search-landing highlight — ``highlightAddressId`` is passed down
  // by IPAMPage, which captured it from ``location.state`` before
  // calling ``selectSubnet`` (selectSubnet triggers
  // ``setSearchParams(..., { replace: true })`` which silently drops
  // ``location.state``, so we can't lazy-read it here).
  const { register: registerHighlightRow, isActive: isHighlightedRow } =
    useRowHighlight(highlightAddressId ?? null);

  // Lightweight drift count for the banner — cheap enough to refetch on
  // every subnet detail load. Invalidated when the user applies a sync,
  // so the banner clears without a manual refresh.
  const { data: dnsDriftSummary } = useQuery({
    queryKey: ["dns-sync-summary", subnet.id],
    queryFn: () => ipamApi.dnsSyncSummary(subnet.id),
    refetchOnMount: "always",
  });
  const [editingAddress, setEditingAddress] = useState<IPAddress | null>(null);
  const [viewingAddress, setViewingAddress] = useState<IPAddress | null>(null);
  const [scanFromDetail, setScanFromDetail] = useState<IPAddress | null>(null);
  const [showFilters, setShowFilters] = useState(false);
  const [activeSubnetTab, setActiveSubnetTab] = useState<
    "addresses" | "dhcp" | "aliases" | "nat"
  >("addresses");
  const [natModalIp, setNatModalIp] = useState<IPAddress | null>(null);
  const [selectedIpIds, setSelectedIpIds] = useState<Set<string>>(new Set());
  // Shift-click range select. ``onChange`` doesn't carry shiftKey, so we
  // stash both the modifier state (set in ``onClick`` which fires first)
  // and the previously-clicked id, then read both from ``onChange`` to
  // do range vs. single toggle. Cleared after each toggle.
  const lastClickedIpIdRef = useRef<string | null>(null);
  const shiftDownAtClickRef = useRef(false);
  const [showBulkEdit, setShowBulkEdit] = useState(false);
  const [showBulkDelete, setShowBulkDelete] = useState(false);

  type FilterMode = "contains" | "begins" | "ends" | "regex";
  const [colFilters, setColFilters] = useState({
    address: "",
    hostname: "",
    mac: "",
    description: "",
    tags: "",
    status: "",
    dns: "",
    pool: "",
  });
  const [filterModes, setFilterModes] = useState<Record<string, FilterMode>>(
    {},
  );
  const [openFilterMenu, setOpenFilterMenu] = useState<string | null>(null);

  // Clear column filters whenever the viewed subnet changes
  useEffect(() => {
    setColFilters({
      address: "",
      hostname: "",
      mac: "",
      description: "",
      tags: "",
      status: "",
      dns: "",
      pool: "",
    });
    setFilterModes({});
    setShowFilters(false);
    setSelectedIpIds(new Set());
  }, [subnet.id]);

  const { data: addresses, isLoading } = useQuery({
    queryKey: ["addresses", subnet.id],
    queryFn: () => ipamApi.listAddresses(subnet.id),
  });

  // Per-IP DNS sync state derived from the same drift report the sync modal
  // uses. Refreshed alongside addresses; stale-while-revalidate is fine since
  // the column is informational.

  // Network discovery cross-reference — one batched call per subnet
  // returns ``{ip_id: NetworkContextEntry[]}``. Drives the "Network"
  // column; absent IPs render an em-dash. Slightly long stale time
  // because FDB tables only refresh on a 5-min poll cadence anyway,
  // so per-keystroke refetching adds zero value.
  const { data: subnetNetworkContext } = useQuery({
    queryKey: ["subnet-network-context", subnet.id],
    queryFn: () => networkApi.getSubnetNetworkContext(subnet.id),
    staleTime: 30_000,
  });

  // DHCP pool membership — derive which pool (if any) each IP falls within.
  const { data: dhcpScopes = [] } = useQuery({
    queryKey: ["dhcp-scopes-subnet", subnet.id],
    queryFn: () => dhcpApi.listScopesBySubnet(subnet.id),
  });
  const allPoolQueries = useQueries({
    queries: dhcpScopes.map((sc) => ({
      queryKey: ["dhcp-pools", sc.id],
      queryFn: () => dhcpApi.listPools(sc.id),
      staleTime: 60_000,
    })),
  });
  const allPools = allPoolQueries.flatMap((q) => q.data ?? []);

  // Manual refresh — bust every query the subnet panel consumes. Broad
  // keys (``dhcp-pools``, ``dhcp-leases``) match all per-scope variants
  // by prefix and are cheap to re-run.
  const refreshSubnet = () => {
    qc.invalidateQueries({ queryKey: ["addresses", subnet.id] });
    qc.invalidateQueries({ queryKey: ["dhcp-scopes-subnet", subnet.id] });
    qc.invalidateQueries({ queryKey: ["dhcp-pools"] });
    qc.invalidateQueries({ queryKey: ["dhcp-leases"] });
    qc.invalidateQueries({ queryKey: ["dns-sync-summary", subnet.id] });
    qc.invalidateQueries({
      queryKey: ["dns-sync-preview", "subnet", subnet.id],
    });
    qc.invalidateQueries({ queryKey: ["effective-dns-subnet", subnet.id] });
    qc.invalidateQueries({ queryKey: ["subnet-aliases", subnet.id] });
  };

  function ipPoolInfo(addr: IPAddress): { type: string; name: string } | null {
    const ipParts = String(addr.address).split(".").map(Number);
    if (ipParts.length !== 4) return null;
    const ipInt =
      ((ipParts[0] << 24) |
        (ipParts[1] << 16) |
        (ipParts[2] << 8) |
        ipParts[3]) >>>
      0;
    for (const p of allPools) {
      const sParts = p.start_ip.split(".").map(Number);
      const eParts = p.end_ip.split(".").map(Number);
      const sInt =
        ((sParts[0] << 24) |
          (sParts[1] << 16) |
          (sParts[2] << 8) |
          sParts[3]) >>>
        0;
      const eInt =
        ((eParts[0] << 24) |
          (eParts[1] << 16) |
          (eParts[2] << 8) |
          eParts[3]) >>>
        0;
      if (ipInt >= sInt && ipInt <= eInt)
        return { type: p.pool_type, name: p.name || p.pool_type };
    }
    return null;
  }

  const { data: dnsDrift } = useQuery({
    queryKey: ["dns-sync-preview", "subnet", subnet.id],
    queryFn: () => ipamApi.dnsSyncPreview(subnet.id),
    staleTime: 30_000,
  });
  const outOfSyncIpIds = new Set<string>([
    ...(dnsDrift?.missing.map((m) => m.ip_id) ?? []),
    ...(dnsDrift?.mismatched.map((m) => m.ip_id) ?? []),
  ]);
  const subnetHasDnsZone = !!(
    dnsDrift?.forward_zone_id || dnsDrift?.reverse_zone_id
  );

  function ipDnsState(addr: IPAddress): "in-sync" | "out-of-sync" | "n/a" {
    if (!subnetHasDnsZone) return "n/a";
    if (
      addr.status === "network" ||
      addr.status === "broadcast" ||
      addr.status === "orphan"
    )
      return "n/a";
    if (!addr.hostname || addr.hostname === "gateway") return "n/a";
    return outOfSyncIpIds.has(addr.id) ? "out-of-sync" : "in-sync";
  }

  function applyFilter(
    value: string,
    filter: string,
    mode: FilterMode = "contains",
  ): boolean {
    if (!filter) return true;
    const v = value.toLowerCase();
    const f = filter.toLowerCase();
    if (mode === "begins") return v.startsWith(f);
    if (mode === "ends") return v.endsWith(f);
    if (mode === "regex") {
      try {
        return new RegExp(filter, "i").test(value);
      } catch {
        return true;
      }
    }
    return v.includes(f);
  }

  const filteredAddresses = addresses?.filter((a) => {
    const cf = colFilters;
    const fm = filterModes;
    if (!applyFilter(a.address, cf.address, fm.address)) return false;
    if (!applyFilter(a.hostname ?? "", cf.hostname, fm.hostname)) return false;
    // MAC column filters either the MAC itself (with punctuation stripped
    // so ``00:11`` and ``0011`` both match) or the OUI vendor name, so
    // "apple" / "cisco" work when the operator knows the maker but not
    // the prefix. Vendor only matches when OUI lookup is enabled and the
    // row carries a vendor value.
    const macNorm = (a.mac_address ?? "").replace(/[:\-.]/g, "");
    const macFilter = cf.mac.replace(/[:\-.]/g, "");
    const macHit = applyFilter(macNorm, macFilter, fm.mac);
    const vendorHit = applyFilter(a.vendor ?? "", cf.mac, fm.mac);
    if (cf.mac && !macHit && !vendorHit) return false;
    if (!applyFilter(a.description ?? "", cf.description, fm.description))
      return false;
    if (cf.tags) {
      // Tag filter matches either `key`, `value`, or `key=value`. Clicking a
      // chip in the row fills in the exact `key=value` form for an exact hit.
      const t = (a.tags as Record<string, unknown> | null) ?? {};
      const entries = Object.entries(t).map(
        ([k, v]) => `${k}=${v == null ? "" : String(v)}`,
      );
      const hay = [
        ...Object.keys(t),
        ...Object.values(t).map((v) => (v == null ? "" : String(v))),
        ...entries,
      ].join("\n");
      if (!applyFilter(hay, cf.tags, fm.tags)) return false;
    }
    if (cf.status && a.status !== cf.status) return false;
    if (cf.dns && ipDnsState(a) !== cf.dns) return false;
    return true;
  });
  const hasActiveFilter = Object.values(colFilters).some(Boolean);

  // Interleave DHCP pool boundary markers with the IP rows so the user
  // can see where a pool begins / ends, even when no IPs are assigned
  // inside it yet. Dynamic pools are the important case (they can't be
  // manually allocated); static / excluded pools are shown for parity.
  // IPv4-only — matches the existing ``ipPoolInfo`` helper.
  const tableRows = (() => {
    if (!filteredAddresses) return [] as AddressOrPoolRow[];
    const rows: AddressOrPoolRow[] = [];
    const sortedPools = [...allPools]
      .map((p) => ({
        ...p,
        _start: ipStringToInt(p.start_ip),
        _end: ipStringToInt(p.end_ip),
      }))
      .filter((p) => Number.isFinite(p._start) && Number.isFinite(p._end))
      .sort((a, b) => a._start - b._start);

    const started = new Set<string>();
    const ended = new Set<string>();

    const emitStarts = (ipInt: number) => {
      for (const p of sortedPools) {
        if (!started.has(p.id) && p._start <= ipInt) {
          rows.push({ kind: "pool-boundary", pool: p, boundary: "start" });
          started.add(p.id);
        }
      }
    };
    const emitEnds = (ipInt: number) => {
      for (const p of sortedPools) {
        if (started.has(p.id) && !ended.has(p.id) && p._end < ipInt) {
          rows.push({ kind: "pool-boundary", pool: p, boundary: "end" });
          ended.add(p.id);
        }
      }
    };

    // Helper for the gap detector: a gap fully inside a dynamic pool
    // is suppressed because those slots belong to the DHCP server, not
    // to IPAM allocation. Reserved/excluded pools still surface gaps
    // since the operator can manually allocate inside them.
    const gapInsideDynamicPool = (s: number, e: number) =>
      sortedPools.some(
        (p) => p.pool_type === "dynamic" && p._start <= s && e <= p._end,
      );

    let prevIpInt: number | null = null;
    for (const addr of filteredAddresses) {
      const ipInt = ipStringToInt(String(addr.address));
      if (!Number.isFinite(ipInt)) {
        rows.push({ kind: "ip", addr });
        prevIpInt = null;
        continue;
      }
      emitStarts(ipInt);
      emitEnds(ipInt);

      // Gap detection between the previous IP and this one. Skipped
      // when a pool boundary just got emitted (the boundary already
      // signals the discontinuity) or when the gap falls inside a
      // dynamic pool (DHCP-owned, not operator-allocatable).
      if (prevIpInt !== null && ipInt - prevIpInt > 1) {
        const lastWasPool = rows[rows.length - 1]?.kind === "pool-boundary";
        const gapStart = prevIpInt + 1;
        const gapEnd = ipInt - 1;
        if (!lastWasPool && !gapInsideDynamicPool(gapStart, gapEnd)) {
          rows.push({ kind: "gap", startIpInt: gapStart, endIpInt: gapEnd });
        }
      }

      rows.push({ kind: "ip", addr });
      prevIpInt = ipInt;
    }
    // Close out any pools still open, then emit markers for pools that
    // sit entirely past the last assigned IP so the range is still
    // visible.
    for (const p of sortedPools) {
      if (started.has(p.id) && !ended.has(p.id)) {
        rows.push({ kind: "pool-boundary", pool: p, boundary: "end" });
        ended.add(p.id);
      }
    }
    for (const p of sortedPools) {
      if (!started.has(p.id)) {
        rows.push({ kind: "pool-boundary", pool: p, boundary: "start" });
        rows.push({ kind: "pool-boundary", pool: p, boundary: "end" });
      }
    }
    return rows;
  })();

  const [confirmDeleteAddr, setConfirmDeleteAddr] = useState<IPAddress | null>(
    null,
  );
  const [confirmPurgeAddr, setConfirmPurgeAddr] = useState<IPAddress | null>(
    null,
  );

  const deleteAddr = useMutation({
    mutationFn: (id: string) => ipamApi.deleteAddress(id), // soft-delete → orphan
    onSuccess: () => {
      setConfirmDeleteAddr(null);
      qc.invalidateQueries({ queryKey: ["addresses", subnet.id] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      qc.invalidateQueries({ queryKey: ["dns-group-records"] });
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
      qc.invalidateQueries({ queryKey: ["subnets"] });
    },
  });

  const purgeAddr = useMutation({
    mutationFn: (id: string) => ipamApi.deleteAddress(id, true), // permanent
    onSuccess: () => {
      // Purge can be triggered from either modal: the orphan-row trash
      // icon (``confirmPurgeAddr``) or the allocated-row delete-modal's
      // "Delete Permanently" button (``confirmDeleteAddr``). Clear both
      // so whichever one is open closes cleanly.
      setConfirmPurgeAddr(null);
      setConfirmDeleteAddr(null);
      qc.invalidateQueries({ queryKey: ["addresses", subnet.id] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      qc.invalidateQueries({ queryKey: ["dns-group-records"] });
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
      qc.invalidateQueries({ queryKey: ["subnets"] });
    },
  });

  const restoreAddr = useMutation({
    mutationFn: (id: string) =>
      ipamApi.updateAddress(id, { status: "allocated" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["addresses", subnet.id] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      qc.invalidateQueries({ queryKey: ["dns-group-records"] });
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
      qc.invalidateQueries({ queryKey: ["subnets"] });
    },
  });

  // Non-editable statuses (infrastructure or orphaned addresses)
  const isReadOnly = (status: string) =>
    status === "network" || status === "broadcast" || status === "orphan";

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Header */}
      <div className="border-b">
        {/* Top bar: breadcrumb + actions */}
        <div className="flex items-center justify-between gap-4 px-6 pt-3 pb-2">
          <div className="min-w-0 flex-1">
            {spaceName &&
              (() => {
                const crumbs: BreadcrumbItem[] = [
                  {
                    label: spaceName,
                    variant: "space",
                    onClick: onSelectSpace,
                  },
                  ...(blockAncestors ?? []).map(
                    (b): BreadcrumbItem => ({
                      label: b.network + (b.name ? ` (${b.name})` : ""),
                      variant: "block",
                      onClick: onSelectBlock
                        ? () => onSelectBlock(b)
                        : undefined,
                    }),
                  ),
                  ...(block
                    ? [
                        {
                          label:
                            block.network +
                            (block.name ? ` (${block.name})` : ""),
                          variant: "block" as const,
                          onClick: onSelectBlock
                            ? () => onSelectBlock(block)
                            : undefined,
                        },
                      ]
                    : []),
                  {
                    label:
                      subnet.network + (subnet.name ? ` (${subnet.name})` : ""),
                    variant: "subnet",
                  },
                ];
                return <BreadcrumbPills items={crumbs} />;
              })()}
          </div>
          <div className="flex flex-shrink-0 items-center gap-2">
            <HeaderButton
              icon={RefreshCw}
              onClick={refreshSubnet}
              title="Refresh address list, DHCP scopes, and DNS drift status"
            >
              Refresh
            </HeaderButton>
            <SyncMenu
              hasDhcp={dhcpScopes.length > 0}
              isPending={false}
              onSyncDns={() => setShowDnsSync(true)}
              onSyncDhcp={() => setShowDhcpSync(true)}
              onSyncAll={() => setShowSyncAll(true)}
            />
            <SubnetImportExportButton
              subnet={subnet}
              onCommitted={() => {
                qc.invalidateQueries({ queryKey: ["addresses"] });
                qc.invalidateQueries({ queryKey: ["subnets"] });
              }}
            />
            <ToolsMenu
              onBulkAllocate={() => setShowBulkAllocate(true)}
              onCleanOrphans={() => setShowOrphans(true)}
              onMerge={() => setShowMergeSubnet(true)}
              onResize={() => setShowResizeSubnet(true)}
              onScan={() => setShowSubnetScan(true)}
              onSplit={() => setShowSplitSubnet(true)}
            />
            <HeaderButton icon={Pencil} onClick={() => setShowEditSubnet(true)}>
              Edit
            </HeaderButton>
            <HeaderButton
              variant="primary"
              icon={Plus}
              onClick={() => setShowAddModal(true)}
            >
              Allocate IP
            </HeaderButton>
          </div>
        </div>

        {/* Identity row */}
        <div className="flex items-center gap-3 px-6 pb-2">
          <span className="font-mono text-xl font-bold tracking-tight">
            {subnet.network}
          </span>
          <StatusBadge status={subnet.status} />
          {subnet.name && (
            <span className="text-sm text-muted-foreground">{subnet.name}</span>
          )}
        </div>

        {/* Stats row */}
        <div className="flex flex-wrap items-center gap-x-8 gap-y-1 border-t bg-muted/30 px-6 py-2 text-sm">
          {subnet.gateway && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-muted-foreground">Gateway</span>
              <span className="font-mono text-xs font-medium">
                {subnet.gateway}
              </span>
            </div>
          )}
          {subnet.vlan?.router_name && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-muted-foreground">Router</span>
              <span className="text-xs font-medium">
                {subnet.vlan.router_name}
              </span>
            </div>
          )}
          {subnet.vlan ? (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-muted-foreground">VLAN</span>
              <span className="text-xs font-medium">
                {subnet.vlan.vlan_id}
                {subnet.vlan.name && (
                  <span className="ml-1 text-muted-foreground">
                    ({subnet.vlan.name})
                  </span>
                )}
              </span>
            </div>
          ) : subnet.vlan_id != null ? (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-muted-foreground">VLAN</span>
              <span
                className="text-xs font-medium"
                title="Legacy tag — assign a Router/VLAN from the Edit modal to manage"
              >
                {subnet.vlan_id}
              </span>
            </div>
          ) : null}
          {subnet.vxlan_id != null && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-muted-foreground">VXLAN</span>
              <span className="text-xs font-medium font-mono">
                {subnet.vxlan_id}
              </span>
            </div>
          )}
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-muted-foreground">Total IPs</span>
            <span className="text-xs font-medium">{subnet.total_ips}</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-muted-foreground">Allocated</span>
            <span className="text-xs font-medium">
              {subnet.allocated_ips} / {subnet.total_ips}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Utilization</span>
            <UtilizationBar percent={subnet.utilization_percent} />
          </div>
          {Object.entries(subnet.custom_fields ?? {}).map(([k, v]) => (
            <div key={k} className="flex items-center gap-1.5">
              <span className="text-xs text-muted-foreground">{k}</span>
              <span className="text-xs font-medium">{String(v)}</span>
            </div>
          ))}
        </div>

        {/* DNS drift banner — only shows when records exist out-of-sync
            with IPAM's expected state. Clicking opens the Sync DNS modal.
            The banner clears on its own once the user applies a sync. */}
        {dnsDriftSummary && dnsDriftSummary.has_drift && (
          <div className="border-t border-amber-500/40 bg-amber-500/10 px-6 py-2 text-xs">
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-amber-900 dark:text-amber-200">
                <AlertTriangle className="h-3.5 w-3.5 flex-shrink-0" />
                <span>
                  {dnsDriftSummary.total} DNS record
                  {dnsDriftSummary.total === 1 ? "" : "s"} out of sync
                  {dnsDriftSummary.stale > 0 &&
                    ` · ${dnsDriftSummary.stale} stale`}
                  {dnsDriftSummary.mismatched > 0 &&
                    ` · ${dnsDriftSummary.mismatched} mismatched`}
                  {dnsDriftSummary.missing > 0 &&
                    ` · ${dnsDriftSummary.missing} missing`}
                </span>
              </div>
              <button
                onClick={() => setShowDnsSync(true)}
                className="flex items-center gap-1 rounded-md border border-amber-500/50 bg-amber-500/20 px-2 py-0.5 text-xs font-medium text-amber-900 hover:bg-amber-500/30 dark:text-amber-200"
              >
                <Globe2 className="h-3 w-3" />
                Open Sync DNS
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Tabs: Addresses | DHCP | Aliases — bulk actions appear inline when IPs selected */}
      <div className="border-b bg-card px-4">
        <div className="flex items-center gap-1">
          <button
            onClick={() => setActiveSubnetTab("addresses")}
            className={cn(
              "px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors",
              activeSubnetTab === "addresses"
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            IP Addresses
          </button>
          <button
            onClick={() => setActiveSubnetTab("dhcp")}
            className={cn(
              "px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors",
              activeSubnetTab === "dhcp"
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            DHCP Pools
          </button>
          <button
            onClick={() => setActiveSubnetTab("aliases")}
            className={cn(
              "px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors",
              activeSubnetTab === "aliases"
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            Aliases
          </button>
          <button
            onClick={() => setActiveSubnetTab("nat")}
            className={cn(
              "px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors",
              activeSubnetTab === "nat"
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
            title="NAT mappings whose internal IP falls inside this subnet"
          >
            NAT
          </button>
          {activeSubnetTab === "addresses" && selectedIpIds.size > 0 && (
            <div className="ml-auto flex items-center gap-2 py-1">
              <span className="text-xs font-medium text-muted-foreground">
                {selectedIpIds.size} {selectedIpIds.size === 1 ? "IP" : "IPs"}{" "}
                selected
              </span>
              <button
                onClick={() => setShowBulkEdit(true)}
                className="flex items-center gap-1.5 rounded-md border bg-background px-2.5 py-1 text-xs hover:bg-accent"
              >
                <Pencil className="h-3 w-3" />
                Bulk edit
              </button>
              <button
                onClick={() => setShowBulkDelete(true)}
                className="flex items-center gap-1.5 rounded-md border border-destructive/40 bg-background px-2.5 py-1 text-xs text-destructive hover:bg-destructive/10"
              >
                <Trash2 className="h-3 w-3" />
                Bulk delete
              </button>
              <button
                onClick={() => setSelectedIpIds(new Set())}
                className="rounded-md p-1 text-muted-foreground hover:text-foreground"
                title="Clear selection"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
        </div>
      </div>

      {activeSubnetTab === "dhcp" && (
        <div className="flex-1 overflow-auto">
          <DHCPSubnetPanel subnetId={subnet.id} />
        </div>
      )}

      {activeSubnetTab === "aliases" && (
        <div className="flex-1 overflow-auto">
          <AliasesSubnetPanel subnetId={subnet.id} />
        </div>
      )}

      {activeSubnetTab === "nat" && (
        <div className="flex-1 overflow-auto">
          <NatSubnetPanel subnetId={subnet.id} />
        </div>
      )}

      {/* IP Address table */}
      {activeSubnetTab === "addresses" && (
        <div className="flex-1 overflow-auto">
          {isLoading ? (
            <p className="p-6 text-sm text-muted-foreground">
              Loading addresses…
            </p>
          ) : !addresses?.length ? (
            <div className="flex flex-col items-center justify-center py-16 text-center">
              <Network className="mb-3 h-10 w-10 text-muted-foreground/30" />
              <p className="text-sm text-muted-foreground">
                No IP addresses allocated yet.
              </p>
              <button
                onClick={() => setShowAddModal(true)}
                className="mt-3 flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
              >
                <Plus className="h-3.5 w-3.5" />
                Allocate first IP
              </button>
            </div>
          ) : (
            <>
              {/* No nested overflow wrapper — Chrome/WebKit treat
                  ``overflow-x: auto`` as establishing a Y-scroll
                  context too (CSS spec: paired axis can't be
                  ``visible`` when the other isn't), which would
                  defeat the sticky thead by anchoring it to a
                  non-scrolling intermediate parent. The outer
                  ``flex-1 overflow-auto`` handles both axes. */}
              <table className="w-full min-w-[640px] text-sm">
                {/* Sticky header — pinned to the parent
                      ``flex-1 overflow-auto`` scroll container so the
                      column headers stay visible while scrolling a
                      long IP list. ``bg-card`` is an opaque base so
                      the muted overlay on the <tr> doesn't let body
                      rows bleed through as the user scrolls. */}
                <thead className="sticky top-0 z-10 bg-card">
                  <tr className="border-b bg-muted/40 text-xs">
                    <th className="w-8 px-2 py-2">
                      {(() => {
                        const selectable = (filteredAddresses ?? []).filter(
                          (a: IPAddress) =>
                            a.status !== "network" &&
                            a.status !== "broadcast" &&
                            !a.auto_from_lease,
                        );
                        const allSelected =
                          selectable.length > 0 &&
                          selectable.every((a: IPAddress) =>
                            selectedIpIds.has(a.id),
                          );
                        return (
                          <input
                            type="checkbox"
                            checked={allSelected}
                            aria-label="Select all"
                            onChange={(e) => {
                              if (e.target.checked) {
                                setSelectedIpIds(
                                  new Set(
                                    selectable.map((a: IPAddress) => a.id),
                                  ),
                                );
                              } else {
                                setSelectedIpIds(new Set());
                              }
                            }}
                          />
                        );
                      })()}
                    </th>
                    {(
                      [
                        "address",
                        "hostname",
                        "mac",
                        "description",
                        "tags",
                        "status",
                        "pool",
                        "dns",
                      ] as const
                    ).map((col) => {
                      const label =
                        col === "mac"
                          ? "MAC"
                          : col === "dns"
                            ? "DNS"
                            : col === "pool"
                              ? "DHCP Pool"
                              : col;
                      return (
                        <th
                          key={col}
                          className="px-4 py-2 text-left font-medium"
                        >
                          <span className="inline-flex items-center gap-1">
                            <span className="capitalize">{label}</span>
                            <button
                              onClick={() => setShowFilters((v) => !v)}
                              title={`Filter by ${label}`}
                              className={cn(
                                "rounded p-0.5 hover:bg-accent",
                                colFilters[col]
                                  ? "text-primary"
                                  : showFilters
                                    ? "text-primary/40"
                                    : "text-muted-foreground/30 hover:text-muted-foreground",
                              )}
                            >
                              <Filter className="h-2.5 w-2.5" />
                            </button>
                          </span>
                        </th>
                      );
                    })}
                    <th
                      className="px-4 py-2 text-center font-medium"
                      title="Alive: green = seen <24h, amber = 24h–7d, red = >7d, grey = never"
                    >
                      Seen
                    </th>
                    <th className="px-4 py-2 text-left font-medium">Network</th>
                    <th className="px-4 py-2 text-right">
                      {hasActiveFilter && (
                        <button
                          onClick={() => {
                            setColFilters({
                              address: "",
                              hostname: "",
                              mac: "",
                              description: "",
                              tags: "",
                              status: "",
                              dns: "",
                              pool: "",
                            });
                            setFilterModes({});
                          }}
                          title="Clear all filters"
                          className="rounded p-0.5 text-primary hover:text-destructive"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      )}
                    </th>
                  </tr>
                  {showFilters && (
                    <tr className="border-b bg-muted/10 text-xs">
                      <td />
                      {(
                        [
                          "address",
                          "hostname",
                          "mac",
                          "description",
                          "tags",
                          "status",
                          "pool",
                          "dns",
                        ] as const
                      ).map((col) => (
                        <td key={col} className="px-2 py-1">
                          {col === "status" ? (
                            <select
                              value={colFilters.status}
                              onChange={(e) =>
                                setColFilters((p) => ({
                                  ...p,
                                  status: e.target.value,
                                }))
                              }
                              className="w-full rounded border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                            >
                              <option value="">All</option>
                              {[
                                "allocated",
                                "available",
                                "reserved",
                                "dhcp",
                                "static_dhcp",
                                "network",
                                "broadcast",
                                "orphan",
                              ].map((s) => (
                                <option key={s} value={s}>
                                  {s}
                                </option>
                              ))}
                            </select>
                          ) : col === "dns" ? (
                            <select
                              value={colFilters.dns}
                              onChange={(e) =>
                                setColFilters((p) => ({
                                  ...p,
                                  dns: e.target.value,
                                }))
                              }
                              className="w-full rounded border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                            >
                              <option value="">All</option>
                              <option value="in-sync">In sync</option>
                              <option value="out-of-sync">Out of sync</option>
                              <option value="n/a">N/A</option>
                            </select>
                          ) : (
                            <div className="flex items-center">
                              <input
                                type="text"
                                value={colFilters[col]}
                                onChange={(e) =>
                                  setColFilters((p) => ({
                                    ...p,
                                    [col]: e.target.value,
                                  }))
                                }
                                placeholder="Filter…"
                                className="w-full min-w-0 rounded-l border border-r-0 bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                              />
                              <div className="relative">
                                <button
                                  type="button"
                                  onClick={() =>
                                    setOpenFilterMenu(
                                      openFilterMenu === col ? null : col,
                                    )
                                  }
                                  className="rounded-r border bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent"
                                  title="Filter mode"
                                >
                                  {filterModes[col] === "begins"
                                    ? "^"
                                    : filterModes[col] === "ends"
                                      ? "$"
                                      : filterModes[col] === "regex"
                                        ? ".*"
                                        : "⊂"}
                                </button>
                                {openFilterMenu === col && (
                                  <div className="absolute left-0 top-full z-30 mt-0.5 w-32 rounded-md border bg-popover shadow-md">
                                    {(
                                      [
                                        "contains",
                                        "begins",
                                        "ends",
                                        "regex",
                                      ] as const
                                    ).map((m) => (
                                      <button
                                        key={m}
                                        type="button"
                                        onClick={() => {
                                          setFilterModes((p) => ({
                                            ...p,
                                            [col]: m,
                                          }));
                                          setOpenFilterMenu(null);
                                        }}
                                        className={cn(
                                          "w-full px-3 py-1.5 text-left text-xs hover:bg-accent",
                                          filterModes[col] === m &&
                                            "font-semibold text-primary",
                                        )}
                                      >
                                        {m === "contains"
                                          ? "⊂ Contains"
                                          : m === "begins"
                                            ? "^ Begins"
                                            : m === "ends"
                                              ? "$ Ends"
                                              : ".* Regex"}
                                      </button>
                                    ))}
                                  </div>
                                )}
                              </div>
                            </div>
                          )}
                        </td>
                      ))}
                      {/* Network column has no filter input yet — operators
                            can still filter via the per-device pages. */}
                      <td />
                      <td />
                    </tr>
                  )}
                </thead>
                <tbody className={zebraBodyCls}>
                  {filteredAddresses?.length === 0 && (
                    <tr>
                      <td
                        colSpan={12}
                        className="px-4 py-6 text-center text-sm text-muted-foreground"
                      >
                        No addresses match the active filters.
                      </td>
                    </tr>
                  )}
                  {tableRows.map((row, rowIdx) => {
                    if (row.kind === "pool-boundary") {
                      const pool = row.pool;
                      const isDynamic = pool.pool_type === "dynamic";
                      const tint = isDynamic
                        ? "bg-cyan-500/10 text-cyan-700 dark:text-cyan-300 border-y border-cyan-500/30"
                        : pool.pool_type === "reserved"
                          ? "bg-violet-500/10 text-violet-700 dark:text-violet-300 border-y border-violet-500/30"
                          : "bg-zinc-500/10 text-zinc-700 dark:text-zinc-300 border-y border-zinc-500/30";
                      const arrow = row.boundary === "start" ? "▼" : "▲";
                      const label =
                        row.boundary === "start"
                          ? `Start of ${pool.pool_type} pool`
                          : `End of ${pool.pool_type} pool`;
                      const anchorIp =
                        row.boundary === "start" ? pool.start_ip : pool.end_ip;
                      return (
                        <tr key={`pool-${pool.id}-${row.boundary}-${rowIdx}`}>
                          <td colSpan={12} className={cn("px-4 py-1.5", tint)}>
                            <span className="mr-2 font-mono text-xs">
                              {arrow}
                            </span>
                            <span className="text-xs font-semibold uppercase tracking-wide">
                              {label}
                            </span>
                            {pool.name && (
                              <span className="ml-2 text-xs">
                                — {pool.name}
                              </span>
                            )}
                            <span className="ml-2 font-mono text-xs opacity-80">
                              {anchorIp}
                            </span>
                            <span className="ml-3 text-[11px] opacity-70">
                              range {pool.start_ip} – {pool.end_ip}
                            </span>
                          </td>
                        </tr>
                      );
                    }
                    if (row.kind === "gap") {
                      const count = row.endIpInt - row.startIpInt + 1;
                      const startIp = intToIpv4(row.startIpInt);
                      const endIp = intToIpv4(row.endIpInt);
                      const label =
                        count === 1 ? startIp : `${startIp} – ${endIp}`;
                      return (
                        <tr
                          key={`gap-${row.startIpInt}-${row.endIpInt}`}
                          aria-label={`${count} unallocated IP${count === 1 ? "" : "s"} between rows`}
                          className="cursor-pointer hover:bg-emerald-500/[0.10]"
                          onClick={() => {
                            setAddModalRange({
                              startIpInt: row.startIpInt,
                              endIpInt: row.endIpInt,
                            });
                            setShowAddModal(true);
                          }}
                          title={`Allocate an IP from this free range (${count} available)`}
                        >
                          <td
                            colSpan={12}
                            className="border-y border-dashed border-emerald-400/30 bg-emerald-500/[0.04] px-4 py-0.5 text-[11px] text-emerald-700/80 dark:border-emerald-500/30 dark:text-emerald-300/70"
                          >
                            <span className="font-mono">{label}</span>
                            <span className="ml-2 opacity-70">
                              · {count} free
                            </span>
                            <span className="ml-2 opacity-60">
                              · click to allocate
                            </span>
                          </td>
                        </tr>
                      );
                    }
                    const addr = row.addr;
                    const dnsState = ipDnsState(addr);
                    const systemRow =
                      addr.status === "network" ||
                      addr.status === "broadcast" ||
                      !!addr.auto_from_lease;
                    const rowSelected = selectedIpIds.has(addr.id);
                    const canEdit =
                      !systemRow &&
                      addr.status !== "orphan" &&
                      !isReadOnly(addr.status);
                    return (
                      <ContextMenu key={addr.id}>
                        <ContextMenuTrigger asChild>
                          <tr
                            ref={registerHighlightRow(addr.id)}
                            onClick={() => setViewingAddress(addr)}
                            className={cn(
                              "group/addr border-b last:border-0 hover:bg-muted/20 cursor-pointer",
                              (addr.status === "network" ||
                                addr.status === "broadcast") &&
                                "opacity-50",
                              addr.status === "orphan" && "opacity-40",
                              rowSelected && "bg-primary/5",
                              isHighlightedRow(addr.id) &&
                                "spatium-row-highlight",
                            )}
                          >
                            <td
                              className="w-8 px-2 py-2"
                              onClick={(e) => e.stopPropagation()}
                            >
                              {!systemRow && (
                                <input
                                  type="checkbox"
                                  checked={rowSelected}
                                  aria-label={`Select ${addr.address}`}
                                  onClick={(e) => {
                                    // ``onClick`` fires before ``onChange``
                                    // and exposes shiftKey; stash it so
                                    // the change handler can decide
                                    // single vs. range toggle.
                                    shiftDownAtClickRef.current = e.shiftKey;
                                  }}
                                  onChange={(e) => {
                                    const newChecked = e.target.checked;
                                    const lastId = lastClickedIpIdRef.current;
                                    const useRange =
                                      shiftDownAtClickRef.current &&
                                      lastId !== null &&
                                      lastId !== addr.id;
                                    shiftDownAtClickRef.current = false;
                                    lastClickedIpIdRef.current = addr.id;

                                    setSelectedIpIds((prev) => {
                                      const next = new Set(prev);
                                      if (useRange) {
                                        // Build the IP-only selectable
                                        // order from the same tableRows
                                        // that drives rendering, so the
                                        // range matches what the user
                                        // sees on screen.
                                        const ids: string[] = [];
                                        for (const r of tableRows) {
                                          if (r.kind !== "ip") continue;
                                          const a = r.addr;
                                          if (
                                            a.status === "network" ||
                                            a.status === "broadcast" ||
                                            a.auto_from_lease
                                          )
                                            continue;
                                          ids.push(a.id);
                                        }
                                        const lo = ids.indexOf(lastId!);
                                        const hi = ids.indexOf(addr.id);
                                        if (lo !== -1 && hi !== -1) {
                                          const [s, e2] =
                                            lo < hi ? [lo, hi] : [hi, lo];
                                          for (let i = s; i <= e2; i++) {
                                            if (newChecked) next.add(ids[i]);
                                            else next.delete(ids[i]);
                                          }
                                          return next;
                                        }
                                      }
                                      if (newChecked) next.add(addr.id);
                                      else next.delete(addr.id);
                                      return next;
                                    });
                                  }}
                                />
                              )}
                            </td>
                            <td className="px-4 py-2 font-mono font-medium">
                              <span className="inline-flex items-center gap-0.5">
                                {addr.address}
                                <CopyButton text={addr.address} />
                              </span>
                            </td>
                            <td className="px-4 py-2">
                              <span className="inline-flex items-center gap-1.5">
                                {addr.fqdn ? (
                                  <span className="font-mono text-xs">
                                    {addr.fqdn}
                                  </span>
                                ) : addr.hostname ? (
                                  <span className="text-muted-foreground">
                                    {addr.hostname}
                                  </span>
                                ) : (
                                  <span className="text-muted-foreground/40">
                                    —
                                  </span>
                                )}
                                {(addr.alias_count ?? 0) > 0 && (
                                  <span
                                    className="inline-flex items-center rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400"
                                    title={`${addr.alias_count} alias${(addr.alias_count ?? 0) === 1 ? "" : "es"} — edit IP to view`}
                                  >
                                    +{addr.alias_count}{" "}
                                    {addr.alias_count === 1
                                      ? "alias"
                                      : "aliases"}
                                  </span>
                                )}
                                {(addr.nat_mapping_count ?? 0) > 0 && (
                                  <button
                                    type="button"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setNatModalIp(addr);
                                    }}
                                    className="inline-flex items-center rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 hover:bg-amber-200 dark:bg-amber-900/30 dark:text-amber-400 dark:hover:bg-amber-900/50"
                                    title={`Click to view ${addr.nat_mapping_count} NAT mapping${(addr.nat_mapping_count ?? 0) === 1 ? "" : "s"}`}
                                  >
                                    NAT {addr.nat_mapping_count}
                                  </button>
                                )}
                              </span>
                            </td>
                            <td className="px-4 py-2 font-mono text-xs">
                              {addr.mac_address ? (
                                <>
                                  {addr.mac_address}
                                  {addr.vendor && (
                                    <span className="ml-1 font-sans text-[11px] text-muted-foreground">
                                      ({addr.vendor})
                                    </span>
                                  )}
                                </>
                              ) : (
                                <span className="text-muted-foreground/40">
                                  —
                                </span>
                              )}
                            </td>
                            <td className="px-4 py-2 text-muted-foreground">
                              {addr.description ?? (
                                <span className="text-muted-foreground/40">
                                  —
                                </span>
                              )}
                            </td>
                            <td className="px-4 py-2">
                              {(() => {
                                const t =
                                  (addr.tags as Record<
                                    string,
                                    unknown
                                  > | null) ?? {};
                                const entries = Object.entries(t);
                                if (entries.length === 0)
                                  return (
                                    <span className="text-muted-foreground/40">
                                      —
                                    </span>
                                  );
                                return (
                                  <div className="flex flex-wrap gap-1">
                                    {entries.map(([k, v]) => {
                                      const vStr = v == null ? "" : String(v);
                                      const label = vStr ? `${k}=${vStr}` : k;
                                      return (
                                        <button
                                          key={k}
                                          type="button"
                                          onClick={() => {
                                            setColFilters((p) => ({
                                              ...p,
                                              tags: label,
                                            }));
                                            setShowFilters(true);
                                          }}
                                          title={`Filter by ${label}`}
                                          className="inline-flex max-w-[14rem] items-center truncate rounded border border-sky-200 bg-sky-50 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 hover:border-sky-300 hover:bg-sky-100 dark:border-sky-900/60 dark:bg-sky-900/30 dark:text-sky-300 dark:hover:bg-sky-900/50"
                                        >
                                          {label}
                                        </button>
                                      );
                                    })}
                                  </div>
                                );
                              })()}
                            </td>
                            <td className="px-4 py-2">
                              <span className="inline-flex items-center">
                                <StatusBadge status={addr.status} />
                                {addr.role ? (
                                  <RoleBadge role={addr.role} />
                                ) : null}
                              </span>
                            </td>
                            <td className="px-4 py-2">
                              {(() => {
                                const pi = ipPoolInfo(addr);
                                if (!pi)
                                  return (
                                    <span className="text-muted-foreground/40">
                                      —
                                    </span>
                                  );
                                const cls =
                                  pi.type === "dynamic"
                                    ? "bg-cyan-100 text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-400"
                                    : pi.type === "reserved"
                                      ? "bg-violet-100 text-violet-800 dark:bg-violet-900/30 dark:text-violet-400"
                                      : "bg-zinc-100 text-zinc-600 dark:bg-zinc-800/30 dark:text-zinc-400";
                                return (
                                  <span
                                    className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${cls}`}
                                  >
                                    {pi.name}
                                  </span>
                                );
                              })()}
                            </td>
                            <td className="px-4 py-2">
                              {dnsState === "in-sync" ? (
                                <span
                                  className="inline-flex items-center gap-1 text-xs text-emerald-600"
                                  title="DNS records match IPAM"
                                >
                                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500" />
                                  in sync
                                </span>
                              ) : dnsState === "out-of-sync" ? (
                                <span
                                  className="inline-flex items-center gap-1 text-xs text-amber-600"
                                  title="DNS records are missing or differ — open Sync DNS to reconcile"
                                >
                                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-amber-500" />
                                  out of sync
                                </span>
                              ) : (
                                <span className="text-muted-foreground/40">
                                  —
                                </span>
                              )}
                            </td>
                            {/* Seen — recency dot derived from
                                  ``last_seen_at``. Orthogonal to status:
                                  an ``allocated`` row can still be cold,
                                  a ``discovered`` row can be alive right
                                  now. Tooltip carries exact age + method. */}
                            <td className="px-4 py-2 text-center">
                              <SeenDot
                                lastSeenAt={addr.last_seen_at}
                                lastSeenMethod={addr.last_seen_method}
                              />
                            </td>
                            {/* Network discovery — switch / port / VLAN
                                  from the SNMP-discovered FDB. May render
                                  multiple lines for trunk ports / hypervisor
                                  hosts where one MAC is learned across VLANs. */}
                            <td className="px-4 py-2">
                              <NetworkContextCell
                                entries={subnetNetworkContext?.[addr.id] ?? []}
                              />
                            </td>
                            <td
                              className="px-4 py-2 text-right"
                              onClick={(e) => e.stopPropagation()}
                            >
                              <div className="flex items-center justify-end gap-1">
                                {addr.status === "orphan" ? (
                                  <>
                                    <button
                                      onClick={() =>
                                        restoreAddr.mutate(addr.id)
                                      }
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
                                ) : addr.auto_from_lease ? (
                                  // Mirror of a dynamic DHCP lease — the DHCP
                                  // server owns the state; editing or deleting
                                  // from IPAM would just get overwritten on
                                  // the next pull. Show a lock hint instead.
                                  <span
                                    className="inline-flex items-center rounded p-1 text-muted-foreground/60"
                                    title="Managed by DHCP server — edit the lease or reservation at the source. This row is refreshed by the lease-pull task."
                                  >
                                    <Lock className="h-3.5 w-3.5" />
                                  </span>
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
                        </ContextMenuTrigger>
                        <ContextMenuContent>
                          <ContextMenuLabel>{addr.address}</ContextMenuLabel>
                          <ContextMenuSeparator />
                          <ContextMenuItem
                            onSelect={() => copyToClipboard(addr.address)}
                          >
                            Copy IP
                          </ContextMenuItem>
                          {addr.fqdn && (
                            <ContextMenuItem
                              onSelect={() => copyToClipboard(addr.fqdn!)}
                            >
                              Copy FQDN
                            </ContextMenuItem>
                          )}
                          {addr.mac_address && (
                            <ContextMenuItem
                              onSelect={() =>
                                copyToClipboard(addr.mac_address!)
                              }
                            >
                              Copy MAC
                            </ContextMenuItem>
                          )}
                          {canEdit && (
                            <>
                              <ContextMenuSeparator />
                              <ContextMenuItem
                                onSelect={() => setEditingAddress(addr)}
                              >
                                Edit…
                              </ContextMenuItem>
                              <ContextMenuItem
                                destructive
                                onSelect={() => setConfirmDeleteAddr(addr)}
                              >
                                Delete…
                              </ContextMenuItem>
                            </>
                          )}
                          {addr.status === "orphan" && (
                            <>
                              <ContextMenuSeparator />
                              <ContextMenuItem
                                onSelect={() => restoreAddr.mutate(addr.id)}
                              >
                                Restore
                              </ContextMenuItem>
                              <ContextMenuItem
                                destructive
                                onSelect={() => setConfirmPurgeAddr(addr)}
                              >
                                Delete Forever…
                              </ContextMenuItem>
                            </>
                          )}
                          {addr.auto_from_lease && (
                            <>
                              <ContextMenuSeparator />
                              <ContextMenuItem disabled>
                                Managed by DHCP — read-only
                              </ContextMenuItem>
                            </>
                          )}
                        </ContextMenuContent>
                      </ContextMenu>
                    );
                  })}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}

      {showAddModal && (
        <AddAddressModal
          subnetId={subnet.id}
          presetRange={addModalRange}
          onClose={() => {
            setShowAddModal(false);
            setAddModalRange(null);
          }}
        />
      )}
      {showEditSubnet && (
        <EditSubnetModal
          subnet={subnet}
          onClose={(updated) => {
            setShowEditSubnet(false);
            if (updated) onSubnetEdited(updated);
          }}
          onDeleted={() => {
            setShowEditSubnet(false);
            onSubnetDeleted?.();
          }}
        />
      )}
      {showResizeSubnet && (
        <ResizeSubnetModal
          subnet={subnet}
          onClose={() => setShowResizeSubnet(false)}
          onCommitted={(result) => {
            // Refresh the subnet in-place so the header reflects the new
            // CIDR without remounting the whole view.
            onSubnetEdited(result.subnet);
          }}
        />
      )}
      {showSplitSubnet && (
        <SplitSubnetModal
          subnet={subnet}
          onClose={() => setShowSplitSubnet(false)}
          onCommitted={() => {
            qc.invalidateQueries({ queryKey: ["subnets"] });
            qc.invalidateQueries({ queryKey: ["blocks"] });
            // Parent subnet was deleted; bounce back to the space.
            onSubnetDeleted?.();
          }}
        />
      )}
      {showMergeSubnet && (
        <MergeSubnetSiblingPicker
          subnet={subnet}
          onClose={() => setShowMergeSubnet(false)}
          onCommitted={() => {
            qc.invalidateQueries({ queryKey: ["subnets"] });
            qc.invalidateQueries({ queryKey: ["blocks"] });
            onSubnetDeleted?.();
          }}
        />
      )}
      {showSubnetScan && (
        <NmapScanModal
          ip={subnet.network}
          defaultPreset="subnet_sweep"
          title={`Scan subnet — ${subnet.network}${subnet.name ? ` (${subnet.name})` : ""}`}
          onClose={() => setShowSubnetScan(false)}
        />
      )}
      {showBulkAllocate && (
        <BulkAllocateModal
          subnet={subnet}
          onClose={() => setShowBulkAllocate(false)}
          onCommitted={() => {
            qc.invalidateQueries({ queryKey: ["addresses", subnet.id] });
            qc.invalidateQueries({ queryKey: ["subnets"] });
          }}
        />
      )}
      {showDnsSync && (
        <DnsSyncModal
          scope={{ kind: "subnet", id: subnet.id, label: subnet.network }}
          onClose={() => setShowDnsSync(false)}
        />
      )}
      {showDhcpSync && (
        <DhcpSyncModal
          subnetId={subnet.id}
          onClose={() => setShowDhcpSync(false)}
        />
      )}
      {showSyncAll && (
        <SyncAllModal
          subnet={subnet}
          onClose={() => setShowSyncAll(false)}
          onOpenDnsDetails={() => {
            setShowSyncAll(false);
            setShowDnsSync(true);
          }}
        />
      )}
      {showOrphans && (
        <OrphansModal
          subnetId={subnet.id}
          subnetLabel={subnet.network}
          onClose={() => setShowOrphans(false)}
        />
      )}
      {editingAddress && (
        <EditAddressModal
          address={editingAddress}
          onClose={() => setEditingAddress(null)}
        />
      )}
      {viewingAddress && (
        <IPDetailModal
          address={viewingAddress}
          subnet={subnet}
          canEdit={
            !(
              viewingAddress.status === "network" ||
              viewingAddress.status === "broadcast" ||
              !!viewingAddress.auto_from_lease ||
              viewingAddress.status === "orphan" ||
              isReadOnly(viewingAddress.status)
            )
          }
          onClose={() => setViewingAddress(null)}
          onEdit={() => {
            const a = viewingAddress;
            setViewingAddress(null);
            setEditingAddress(a);
          }}
          onScan={() => setScanFromDetail(viewingAddress)}
          onDelete={() => {
            const a = viewingAddress;
            setViewingAddress(null);
            setConfirmDeleteAddr(a);
          }}
        />
      )}
      {scanFromDetail && (
        <NmapScanModal
          ip={scanFromDetail.address}
          ipAddressId={scanFromDetail.id}
          onClose={() => setScanFromDetail(null)}
        />
      )}
      {natModalIp && (
        <NatMappingsForIpModal
          ip={natModalIp}
          onClose={() => setNatModalIp(null)}
        />
      )}
      {confirmDeleteAddr && (
        <DeleteOrOrphanModal
          address={confirmDeleteAddr}
          onOrphan={() => deleteAddr.mutate(confirmDeleteAddr.id)}
          onPurge={() => purgeAddr.mutate(confirmDeleteAddr.id)}
          onClose={() => setConfirmDeleteAddr(null)}
          isOrphanPending={deleteAddr.isPending}
          isPurgePending={purgeAddr.isPending}
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
      {showBulkEdit && (
        <BulkEditAddressesModal
          ipIds={[...selectedIpIds]}
          subnetId={subnet.id}
          onClose={() => setShowBulkEdit(false)}
          onDone={() => {
            setShowBulkEdit(false);
            setSelectedIpIds(new Set());
          }}
        />
      )}
      {showBulkDelete && (
        <BulkDeleteAddressesModal
          ipIds={[...selectedIpIds]}
          subnetId={subnet.id}
          onClose={() => setShowBulkDelete(false)}
          onDone={() => {
            setShowBulkDelete(false);
            setSelectedIpIds(new Set());
          }}
        />
      )}
    </div>
  );
}

// ─── Edit Subnet Modal ────────────────────────────────────────────────────────

const SUBNET_STATUSES = [
  "active",
  "reserved",
  "deprecated",
  "quarantine",
] as const;

// ─── DNS Sync Modal ──────────────────────────────────────────────────────────

type DnsSyncScope =
  | { kind: "subnet"; id: string; label: string }
  | { kind: "block"; id: string; label: string }
  | { kind: "space"; id: string; label: string };

function DnsSyncModal({
  scope,
  onClose,
}: {
  scope: DnsSyncScope;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const fetchPreview = () =>
    scope.kind === "subnet"
      ? ipamApi.dnsSyncPreview(scope.id)
      : scope.kind === "block"
        ? ipamApi.dnsSyncPreviewBlock(scope.id)
        : ipamApi.dnsSyncPreviewSpace(scope.id);
  const commitFn = (body: {
    create_for_ip_ids?: string[];
    update_record_ids?: string[];
    delete_stale_record_ids?: string[];
  }) =>
    scope.kind === "subnet"
      ? ipamApi.dnsSyncCommit(scope.id, body)
      : scope.kind === "block"
        ? ipamApi.dnsSyncCommitBlock(scope.id, body)
        : ipamApi.dnsSyncCommitSpace(scope.id, body);

  // Before we compute drift, make sure every subnet's reverse zone exists —
  // otherwise "missing PTR" rows will show for subnets whose reverse zone
  // hasn't been created yet, and the commit will fail. Backfill is
  // idempotent, so it's safe to run on every modal open.
  const [backfillDone, setBackfillDone] = useState(false);
  const [backfillResult, setBackfillResult] = useState<{
    created: { subnet: string; zone: string }[];
    skipped: number;
  } | null>(null);
  const [backfillError, setBackfillError] = useState<string | null>(null);
  useEffect(() => {
    const fn =
      scope.kind === "subnet"
        ? ipamApi.backfillReverseZonesSubnet
        : scope.kind === "block"
          ? ipamApi.backfillReverseZonesBlock
          : ipamApi.backfillReverseZonesSpace;
    fn(scope.id)
      .then((r) => setBackfillResult(r))
      .catch((e: Error) => setBackfillError(e.message || "Backfill failed"))
      .finally(() => setBackfillDone(true));
  }, [scope.id, scope.kind]);

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["dns-sync-preview", scope.kind, scope.id],
    queryFn: fetchPreview,
    refetchOnMount: "always",
    enabled: backfillDone,
  });

  // Per-row selection. Empty Set = nothing chosen → Apply disabled.
  const [selMissing, setSelMissing] = useState<Set<string>>(new Set());
  const [selMismatched, setSelMismatched] = useState<Set<string>>(new Set());
  const [selStale, setSelStale] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<{
    created: number;
    updated: number;
    deleted: number;
    errors: string[];
  } | null>(null);

  // Default-select everything when the report first arrives so the common
  // case (user wants to fix all drift) is one click. They can untick
  // individual rows to skip suspected manual edits.
  useEffect(() => {
    if (!data) return;
    setSelMissing(
      new Set(data.missing.map((m) => m.ip_id + ":" + m.record_type)),
    );
    setSelMismatched(new Set(data.mismatched.map((m) => m.record_id)));
    setSelStale(new Set(data.stale.map((s) => s.record_id)));
  }, [data]);

  const commitMut = useMutation({
    mutationFn: () => {
      if (!data) throw new Error("No preview");
      // missing items keyed as `ip_id:record_type` so a single IP can have
      // both A and PTR ticked or unticked independently. Backend just needs
      // unique IP IDs to re-sync — sync_dns_record handles both records.
      const ipIds = new Set<string>();
      for (const m of data.missing) {
        if (selMissing.has(m.ip_id + ":" + m.record_type)) ipIds.add(m.ip_id);
      }
      return commitFn({
        create_for_ip_ids: Array.from(ipIds),
        update_record_ids: Array.from(selMismatched),
        delete_stale_record_ids: Array.from(selStale),
      });
    },
    onSuccess: (res) => {
      setResult(res);
      qc.invalidateQueries({ queryKey: ["addresses"] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      qc.invalidateQueries({ queryKey: ["dns-group-records"] });
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
      // Drop the drift-banner count so the subnet header refreshes after
      // the user applies the sync.
      qc.invalidateQueries({ queryKey: ["dns-sync-summary"] });
      // Invalidate alone only marks stale — the modal's useQuery doesn't
      // pick that up while the component stays mounted. Force a refetch
      // so the missing/mismatched/stale lists reflect the new DB state.
      // Also clear the selections since the old keys (ip_id / record_id)
      // no longer match rows in the refreshed preview.
      setSelMissing(new Set());
      setSelMismatched(new Set());
      setSelStale(new Set());
      refetch();
    },
  });

  const totalSelected = selMissing.size + selMismatched.size + selStale.size;

  function toggle(
    set: Set<string>,
    setter: (s: Set<string>) => void,
    key: string,
  ) {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setter(next);
  }
  function toggleAll(
    items: string[],
    set: Set<string>,
    setter: (s: Set<string>) => void,
  ) {
    setter(items.every((k) => set.has(k)) ? new Set() : new Set(items));
  }

  const { dialogStyle, dragHandleProps } = useDraggableModal(onClose);

  return (
    <div className={MODAL_BACKDROP_CLS}>
      <div
        className="w-full max-w-[95vw] sm:max-w-3xl rounded-lg border bg-card shadow-lg flex flex-col max-h-[85vh]"
        style={dialogStyle}
      >
        <div
          {...dragHandleProps}
          className={cn(
            "flex items-center justify-between border-b px-5 py-3",
            dragHandleProps.className,
          )}
        >
          <div>
            <h2 className="text-base font-semibold">
              DNS Sync — {scope.label}
            </h2>
            <p className="text-xs text-muted-foreground">
              Reconcile IPAM-managed DNS records with the database
              {scope.kind !== "subnet" &&
                ` across all subnets in this ${scope.kind}`}
              . Untick anything you want to leave alone.
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded p-1 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 overflow-auto px-5 py-4 space-y-5">
          {!backfillDone && (
            <p className="text-sm text-muted-foreground">
              Backfilling missing reverse zones…
            </p>
          )}
          {backfillDone &&
            backfillResult &&
            backfillResult.created.length > 0 && (
              <div className="rounded-md border border-blue-500/40 bg-blue-500/10 px-3 py-2 text-xs">
                Backfill created {backfillResult.created.length} reverse zone
                {backfillResult.created.length === 1 ? "" : "s"}:{" "}
                <span className="font-mono">
                  {backfillResult.created.map((c) => c.zone).join(", ")}
                </span>
              </div>
            )}
          {backfillError && (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
              Reverse-zone backfill skipped: {backfillError}
            </div>
          )}
          {backfillDone && isLoading && (
            <p className="text-sm text-muted-foreground">Computing drift…</p>
          )}
          {error && (
            <p className="text-sm text-destructive">
              Failed to load preview. {(error as Error).message}
            </p>
          )}
          {data && (
            <>
              {/* Zone summary (subnet scope only — block/space span many zones) */}
              {scope.kind === "subnet" && (
                <div className="rounded-md border bg-muted/20 px-3 py-2 text-xs space-y-0.5">
                  <div>
                    <span className="text-muted-foreground">Forward zone:</span>{" "}
                    {data.forward_zone_name ? (
                      <span className="font-mono">
                        {data.forward_zone_name}
                      </span>
                    ) : (
                      <span className="italic text-muted-foreground">
                        none (subnet has no DNS assignment)
                      </span>
                    )}
                  </div>
                  <div>
                    <span className="text-muted-foreground">Reverse zone:</span>{" "}
                    {data.reverse_zone_name ? (
                      <span className="font-mono">
                        {data.reverse_zone_name}
                      </span>
                    ) : (
                      <span className="italic text-muted-foreground">none</span>
                    )}
                  </div>
                </div>
              )}

              {result && (
                <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs">
                  Applied: {result.created} created · {result.updated} updated ·{" "}
                  {result.deleted} deleted
                  {result.errors.length > 0 && (
                    <div className="mt-1 text-destructive">
                      {result.errors.length} error
                      {result.errors.length === 1 ? "" : "s"}:{" "}
                      {result.errors.join("; ")}
                    </div>
                  )}
                </div>
              )}

              {data.missing.length === 0 &&
                data.mismatched.length === 0 &&
                data.stale.length === 0 &&
                !result && (
                  <p className="text-sm text-emerald-600">
                    ✓ In sync — no drift detected.
                  </p>
                )}

              {/* Missing */}
              {data.missing.length > 0 && (
                <DriftSection
                  title="Missing in DNS"
                  description="IPAM expects these records but they don't exist (or were deleted out-of-band). Selecting will re-create them via the agent."
                  count={data.missing.length}
                  selected={selMissing.size}
                  onToggleAll={() =>
                    toggleAll(
                      data.missing.map((m) => m.ip_id + ":" + m.record_type),
                      selMissing,
                      setSelMissing,
                    )
                  }
                >
                  {data.missing.map((m) => {
                    const key = m.ip_id + ":" + m.record_type;
                    return (
                      <DriftRow
                        key={key}
                        checked={selMissing.has(key)}
                        onToggle={() => toggle(selMissing, setSelMissing, key)}
                        type={m.record_type}
                        zone={m.zone_name}
                        primary={`${m.expected_name} → ${m.expected_value}`}
                        secondary={`${m.ip_address} (${m.hostname})`}
                      />
                    );
                  })}
                </DriftSection>
              )}

              {/* Mismatched */}
              {data.mismatched.length > 0 && (
                <DriftSection
                  title="Mismatched"
                  description="The record exists but the name or value differs from what IPAM would create today. Selecting will overwrite the record."
                  count={data.mismatched.length}
                  selected={selMismatched.size}
                  onToggleAll={() =>
                    toggleAll(
                      data.mismatched.map((m) => m.record_id),
                      selMismatched,
                      setSelMismatched,
                    )
                  }
                >
                  {data.mismatched.map((m) => (
                    <DriftRow
                      key={m.record_id}
                      checked={selMismatched.has(m.record_id)}
                      onToggle={() =>
                        toggle(selMismatched, setSelMismatched, m.record_id)
                      }
                      type={m.record_type}
                      zone={m.zone_name}
                      primary={
                        <>
                          <span className="text-destructive line-through">
                            {m.current_name} → {m.current_value}
                          </span>
                          <span className="mx-2 text-muted-foreground">→</span>
                          <span>
                            {m.expected_name} → {m.expected_value}
                          </span>
                        </>
                      }
                      secondary={m.ip_address}
                    />
                  ))}
                </DriftSection>
              )}

              {/* Stale */}
              {data.stale.length > 0 && (
                <DriftSection
                  title="Stale records"
                  description="Auto-generated records that no longer have a live IPAM address. Selecting will permanently delete them and push the delete to BIND."
                  count={data.stale.length}
                  selected={selStale.size}
                  onToggleAll={() =>
                    toggleAll(
                      data.stale.map((s) => s.record_id),
                      selStale,
                      setSelStale,
                    )
                  }
                >
                  {data.stale.map((s) => (
                    <DriftRow
                      key={s.record_id}
                      checked={selStale.has(s.record_id)}
                      onToggle={() =>
                        toggle(selStale, setSelStale, s.record_id)
                      }
                      type={s.record_type}
                      zone={s.zone_name}
                      primary={`${s.name} → ${s.value}`}
                      secondary={`reason: ${s.reason}`}
                      destructive
                    />
                  ))}
                </DriftSection>
              )}
            </>
          )}
        </div>

        <div className="flex items-center justify-between border-t px-5 py-3">
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw
              className={cn("h-3 w-3", isFetching && "animate-spin")}
            />
            Re-check
          </button>
          <div className="flex items-center gap-2">
            {(() => {
              const noDrift =
                data &&
                data.missing.length === 0 &&
                data.mismatched.length === 0 &&
                data.stale.length === 0;
              const closeOnly = result || noDrift;
              return (
                <>
                  <button
                    onClick={onClose}
                    className={cn(
                      "rounded-md px-3 py-1.5 text-sm",
                      closeOnly
                        ? "bg-primary text-primary-foreground hover:bg-primary/90"
                        : "border hover:bg-muted",
                    )}
                  >
                    {closeOnly ? "Close" : "Cancel"}
                  </button>
                  {!closeOnly && data && (
                    <button
                      onClick={() => commitMut.mutate()}
                      disabled={totalSelected === 0 || commitMut.isPending}
                      className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                    >
                      {commitMut.isPending
                        ? "Applying…"
                        : `Apply (${totalSelected})`}
                    </button>
                  )}
                </>
              );
            })()}
          </div>
        </div>
      </div>
    </div>
  );
}

function OrphansModal({
  subnetId,
  subnetLabel,
  onClose,
}: {
  subnetId: string;
  subnetLabel: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const { data: addrs, isLoading } = useQuery({
    queryKey: ["addresses", subnetId],
    queryFn: () => ipamApi.listAddresses(subnetId),
  });
  const orphans = (addrs ?? []).filter((a) => a.status === "orphan");

  const mut = useMutation({
    mutationFn: () => ipamApi.purgeOrphans(subnetId, Array.from(selected)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["addresses", subnetId] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      qc.invalidateQueries({ queryKey: ["dns-group-records"] });
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
      qc.invalidateQueries({ queryKey: ["subnets"] });
      onClose();
    },
  });

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const toggleAll = () => {
    if (selected.size === orphans.length) setSelected(new Set());
    else setSelected(new Set(orphans.map((o) => o.id)));
  };

  return (
    <Modal title={`Clean Orphans — ${subnetLabel}`} onClose={onClose} wide>
      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">
          Orphans are IP addresses that were soft-deleted. Selected rows will be
          <span className="font-medium"> permanently removed</span> and their
          auto-generated DNS records torn down.
        </p>
        {isLoading ? (
          <div className="py-6 text-center text-sm text-muted-foreground">
            Loading…
          </div>
        ) : orphans.length === 0 ? (
          <div className="rounded-md border bg-muted/30 py-6 text-center text-sm">
            No orphans in this subnet.
          </div>
        ) : (
          <div className="rounded-md border">
            <div className="flex items-center gap-3 border-b px-3 py-2 bg-muted/30 text-xs">
              <input
                type="checkbox"
                checked={selected.size === orphans.length}
                onChange={toggleAll}
              />
              <span className="flex-1 font-medium">
                {selected.size} of {orphans.length} selected
              </span>
            </div>
            <div className="max-h-80 overflow-y-auto divide-y">
              {orphans.map((o) => (
                <label
                  key={o.id}
                  className="flex items-center gap-3 px-3 py-2 text-sm hover:bg-muted/40 cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={selected.has(o.id)}
                    onChange={() => toggle(o.id)}
                  />
                  <span className="font-mono text-xs w-36 truncate">
                    {o.address}
                  </span>
                  <span className="flex-1 truncate text-xs text-muted-foreground">
                    {o.fqdn || o.hostname || "—"}
                  </span>
                  {o.mac_address && (
                    <span className="font-mono text-xs text-muted-foreground">
                      {o.mac_address}
                    </span>
                  )}
                </label>
              ))}
            </div>
          </div>
        )}
        <div className="flex justify-end gap-2 pt-2">
          {orphans.length === 0 ? (
            <button
              onClick={onClose}
              className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
            >
              Close
            </button>
          ) : (
            <>
              <button
                onClick={onClose}
                className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
              >
                Cancel
              </button>
              <button
                disabled={selected.size === 0 || mut.isPending}
                onClick={() => mut.mutate()}
                className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90 disabled:opacity-40"
              >
                {mut.isPending ? "Purging…" : `Purge ${selected.size}`}
              </button>
            </>
          )}
        </div>
      </div>
    </Modal>
  );
}

function DriftSection({
  title,
  description,
  count,
  selected,
  onToggleAll,
  children,
}: {
  title: string;
  description: string;
  count: number;
  selected: number;
  onToggleAll: () => void;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <div>
          <h3 className="text-sm font-semibold">
            {title}{" "}
            <span className="text-muted-foreground font-normal">
              ({selected}/{count})
            </span>
          </h3>
          <p className="text-xs text-muted-foreground">{description}</p>
        </div>
        <button
          onClick={onToggleAll}
          className="text-xs text-primary hover:underline"
        >
          {selected === count ? "Deselect all" : "Select all"}
        </button>
      </div>
      <div className="rounded-md border divide-y">{children}</div>
    </div>
  );
}

function DriftRow({
  checked,
  onToggle,
  type,
  zone,
  primary,
  secondary,
  destructive,
}: {
  checked: boolean;
  onToggle: () => void;
  type: string;
  zone: string;
  primary: React.ReactNode;
  secondary: React.ReactNode;
  destructive?: boolean;
}) {
  return (
    <label className="flex items-center gap-3 px-3 py-2 cursor-pointer hover:bg-muted/30">
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        className="h-3.5 w-3.5 flex-shrink-0"
      />
      <span
        className={cn(
          "inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium flex-shrink-0",
          type === "A"
            ? "bg-blue-500/15 text-blue-600"
            : type === "PTR"
              ? "bg-cyan-500/15 text-cyan-600"
              : "bg-muted text-muted-foreground",
        )}
      >
        {type}
      </span>
      <div className="flex-1 min-w-0">
        <div
          className={cn(
            "font-mono text-xs truncate",
            destructive && "text-destructive",
          )}
        >
          {primary}
        </div>
        <div className="text-[11px] text-muted-foreground truncate">
          {secondary} · {zone}
        </div>
      </div>
    </label>
  );
}

function EditSubnetModal({
  subnet,
  onClose,
  onDeleted,
}: {
  subnet: Subnet;
  onClose: (updated?: Subnet) => void;
  onDeleted?: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(subnet.name ?? "");
  const [description, setDescription] = useState(subnet.description ?? "");
  const [gateway, setGateway] = useState(subnet.gateway ?? "");
  const [vlanRefId, setVlanRefId] = useState<string | null>(
    subnet.vlan_ref_id ?? null,
  );
  const [vxlanId, setVxlanId] = useState<string>(
    subnet.vxlan_id != null ? String(subnet.vxlan_id) : "",
  );
  const [status, setStatus] = useState(subnet.status);
  const [customFields, setCustomFields] = useState<Record<string, unknown>>(
    (subnet.custom_fields as Record<string, unknown>) ?? {},
  );
  const [error, setError] = useState<string | null>(null);
  const [deleteStep, setDeleteStep] = useState<0 | 1 | 2>(0);
  const [deleteChecked, setDeleteChecked] = useState(false);

  // DNS state — initialized from subnet
  const [dnsInherit, setDnsInherit] = useState(
    subnet.dns_inherit_settings ?? true,
  );
  const [dnsGroupIds, setDnsGroupIds] = useState<string[]>(
    subnet.dns_group_ids ?? [],
  );
  const [dnsZoneId, setDnsZoneId] = useState<string | null>(
    subnet.dns_zone_id ?? null,
  );
  const [dnsAdditionalZoneIds, setDnsAdditionalZoneIds] = useState<string[]>(
    subnet.dns_additional_zone_ids ?? [],
  );
  // Issue #25 — split-horizon publishing toggle.
  const [dnsSplitHorizon, setDnsSplitHorizon] = useState(
    subnet.dns_split_horizon ?? false,
  );
  // DHCP state — initialized from subnet
  const [dhcpInherit, setDhcpInherit] = useState(
    subnet.dhcp_inherit_settings ?? true,
  );
  const [dhcpServerGroupId, setDhcpServerGroupId] = useState<string | null>(
    subnet.dhcp_server_group_id ?? null,
  );
  // DDNS state — initialised from subnet
  const [ddnsEnabled, setDdnsEnabled] = useState(subnet.ddns_enabled ?? false);
  const [ddnsPolicy, setDdnsPolicy] = useState<DdnsPolicy>(
    subnet.ddns_hostname_policy ?? "client_or_generated",
  );
  const [ddnsDomainOverride, setDdnsDomainOverride] = useState<string | null>(
    subnet.ddns_domain_override ?? null,
  );
  const [ddnsTtl, setDdnsTtl] = useState<number | null>(
    subnet.ddns_ttl ?? null,
  );
  // Device profiling state — initialised from subnet (Phase 1).
  const [autoProfileEnabled, setAutoProfileEnabled] = useState(
    subnet.auto_profile_on_dhcp_lease ?? false,
  );
  const [autoProfilePreset, setAutoProfilePreset] = useState<AutoProfilePreset>(
    subnet.auto_profile_preset ?? "service_and_os",
  );
  const [autoProfileRefreshDays, setAutoProfileRefreshDays] = useState(
    subnet.auto_profile_refresh_days ?? 30,
  );
  // Compliance / classification flags (issue #75). First-class
  // booleans rather than freeform tags so the Compliance dashboard
  // queries hit indexed predicates.
  const [pciScope, setPciScope] = useState(subnet.pci_scope ?? false);
  const [hipaaScope, setHipaaScope] = useState(subnet.hipaa_scope ?? false);
  const [internetFacing, setInternetFacing] = useState(
    subnet.internet_facing ?? false,
  );

  // Detect whether network/broadcast records currently exist
  const { data: addresses } = useQuery({
    queryKey: ["addresses", subnet.id],
    queryFn: () => ipamApi.listAddresses(subnet.id),
  });
  const hasAutoAddresses =
    addresses?.some(
      (a) => a.status === "network" || a.status === "broadcast",
    ) ?? true;
  const [autoAddresses, setAutoAddresses] = useState<boolean | null>(null);
  // Use detected value as the default; allow override
  const effectiveAutoAddresses =
    autoAddresses !== null ? autoAddresses : hasAutoAddresses;

  const { data: cfDefs = [] } = useQuery({
    queryKey: ["custom-fields", "subnet"],
    queryFn: () => customFieldsApi.list("subnet"),
  });

  // Effective (inherited) tags + custom_fields from the Block/Space ancestor
  // chain — used to render inherited values as placeholders on any local
  // fields the subnet hasn't overridden.
  const { data: effectiveFields } = useQuery({
    queryKey: ["effective-fields", subnet.id],
    queryFn: () => ipamApi.effectiveFields(subnet.id),
    staleTime: 30_000,
  });
  const { data: allSpaces = [] } = useQuery({
    queryKey: ["spaces"],
    queryFn: ipamApi.listSpaces,
    staleTime: 60_000,
  });
  const { data: allBlocksForLabels = [] } = useQuery({
    queryKey: ["blocks"],
    queryFn: () => ipamApi.listBlocks(),
    staleTime: 60_000,
  });
  // Build human labels for each inherited custom-field key. Only include
  // keys whose source is NOT the subnet itself — those are the ones we want
  // to surface as inherited placeholders.
  const cfInheritedLabels: Record<string, string> = {};
  const cfInheritedValues: Record<string, unknown> = {};
  if (effectiveFields) {
    for (const [key, src] of Object.entries(
      effectiveFields.custom_field_sources,
    )) {
      if (src === "subnet") continue;
      cfInheritedValues[key] = effectiveFields.custom_fields[key];
      if (src.startsWith("block:")) {
        const bid = src.slice("block:".length);
        const b = allBlocksForLabels.find((x) => x.id === bid);
        cfInheritedLabels[key] = b?.name
          ? `block ${b.name}`
          : b?.network
            ? `block ${b.network}`
            : "parent block";
      } else if (src.startsWith("space:")) {
        const sid = src.slice("space:".length);
        const s = allSpaces.find((x) => x.id === sid);
        cfInheritedLabels[key] = s?.name ? `IP Space ${s.name}` : "IP Space";
      } else {
        cfInheritedLabels[key] = src;
      }
    }
  }

  const mutation = useMutation({
    mutationFn: () => {
      const manageAuto =
        autoAddresses !== null && autoAddresses !== hasAutoAddresses
          ? !autoAddresses // True = remove, False = add
          : undefined;
      return ipamApi.updateSubnet(subnet.id, {
        name: name || undefined,
        description,
        gateway: gateway || undefined,
        vlan_ref_id: vlanRefId,
        vxlan_id: vxlanId.trim() ? Number(vxlanId.trim()) : null,
        status,
        custom_fields: customFields,
        dns_inherit_settings: dnsInherit,
        dns_group_ids: dnsInherit ? null : dnsGroupIds,
        dns_zone_id: dnsInherit ? null : dnsZoneId,
        dns_additional_zone_ids: dnsInherit ? null : dnsAdditionalZoneIds,
        dns_split_horizon: dnsSplitHorizon,
        dhcp_inherit_settings: dhcpInherit,
        dhcp_server_group_id: dhcpInherit ? null : dhcpServerGroupId,
        ddns_enabled: ddnsEnabled,
        ddns_hostname_policy: ddnsPolicy,
        ddns_domain_override: ddnsDomainOverride,
        ddns_ttl: ddnsTtl,
        auto_profile_on_dhcp_lease: autoProfileEnabled,
        auto_profile_preset: autoProfilePreset,
        auto_profile_refresh_days: autoProfileRefreshDays,
        pci_scope: pciScope,
        hipaa_scope: hipaaScope,
        internet_facing: internetFacing,
        ...(manageAuto !== undefined
          ? { manage_auto_addresses: manageAuto }
          : {}),
      });
    },
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ["subnets", subnet.space_id] });
      // Invalidate addresses so network/broadcast changes are reflected immediately
      qc.invalidateQueries({ queryKey: ["addresses", subnet.id] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      qc.invalidateQueries({ queryKey: ["dns-group-records"] });
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
      // Refresh "Subnets using this VLAN" lists on the VLANs page
      qc.invalidateQueries({ queryKey: ["subnets-by-vlan"] });
      onClose(updated);
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to save";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  const [deleteError, setDeleteError] = useState<string | null>(null);
  const deleteMutation = useMutation({
    // EditSubnetModal's two-step delete already requires the
    // operator to tick "…and all its contents will be permanently
    // deleted" — cascade is the right semantics. force=true.
    mutationFn: () => ipamApi.deleteSubnet(subnet.id, true),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnets", subnet.space_id] });
      qc.invalidateQueries({ queryKey: ["blocks", subnet.space_id] });
      onDeleted?.();
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      setDeleteError(
        typeof detail === "string"
          ? detail
          : detail
            ? JSON.stringify(detail)
            : "Failed to delete subnet.",
      );
    },
  });

  function resetDelete() {
    setDeleteStep(0);
    setDeleteError(null);
    setDeleteChecked(false);
  }

  // ── Delete step 1 ──
  if (deleteStep === 1) {
    return (
      <Modal title="Delete Subnet" onClose={resetDelete}>
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Are you sure you want to delete subnet{" "}
            <strong className="font-mono text-foreground">
              {subnet.network}
            </strong>
            {subnet.name ? ` (${subnet.name})` : ""}?
          </p>
          {deleteError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {deleteError}
            </div>
          )}
          <div className="flex justify-end gap-2">
            <button
              onClick={resetDelete}
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

  // ── Delete step 2 ──
  if (deleteStep === 2) {
    return (
      <Modal title="Confirm Permanent Deletion" onClose={resetDelete}>
        <div className="space-y-4">
          <p className="text-sm font-medium text-destructive">
            This action cannot be undone.
          </p>
          <p className="text-sm text-muted-foreground">
            <strong className="font-mono text-foreground">
              {subnet.network}
            </strong>{" "}
            and all its contents will be permanently removed.
          </p>
          <label className="flex cursor-pointer items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={deleteChecked}
              onChange={(e) => setDeleteChecked(e.target.checked)}
            />
            I understand {subnet.network} and all its contents will be
            permanently deleted.
          </label>
          {deleteError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {deleteError}
            </div>
          )}
          <div className="flex justify-end gap-2">
            <button
              onClick={resetDelete}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              Cancel
            </button>
            <button
              onClick={() => {
                setDeleteError(null);
                deleteMutation.mutate();
              }}
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
        <VlanPicker vlanRefId={vlanRefId} onChange={setVlanRefId} />
        {subnet.vlan_id != null && !vlanRefId && (
          <p className="text-[11px] text-muted-foreground italic">
            Current VLAN tag: {subnet.vlan_id} (unassigned — create a Router /
            VLAN to manage).
          </p>
        )}
        <Field label="VXLAN ID (optional)">
          <input
            type="number"
            min={1}
            max={16777214}
            placeholder="1 – 16777214"
            value={vxlanId}
            onChange={(e) => setVxlanId(e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field label="Status">
          <select
            className={inputCls}
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            {SUBNET_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </Field>
        <div className="border-t pt-3">
          <label className="flex items-start gap-3">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 rounded"
              checked={effectiveAutoAddresses}
              onChange={(e) => setAutoAddresses(e.target.checked)}
            />
            <div>
              <span className="text-sm font-medium">
                Network / Broadcast records
              </span>
              <p className="text-xs text-muted-foreground">
                {effectiveAutoAddresses
                  ? "Network and broadcast addresses are present. Uncheck to remove them (e.g. for loopbacks or P2P links)."
                  : "Network and broadcast addresses are not present. Check to add them."}
              </p>
            </div>
          </label>
        </div>
        <CustomFieldsSection
          definitions={cfDefs}
          values={customFields}
          onChange={(k, v) => setCustomFields((prev) => ({ ...prev, [k]: v }))}
          inherited={cfInheritedValues}
          inheritedLabels={cfInheritedLabels}
        />
        <div className="border-t pt-3">
          <DnsSettingsSection
            inherit={dnsInherit}
            groupIds={dnsGroupIds}
            zoneId={dnsZoneId}
            additionalZoneIds={dnsAdditionalZoneIds}
            onInheritChange={setDnsInherit}
            onGroupIdsChange={setDnsGroupIds}
            onZoneIdChange={setDnsZoneId}
            onAdditionalZoneIdsChange={setDnsAdditionalZoneIds}
            parentBlockId={subnet.block_id}
          />
        </div>
        <div className="border-t pt-3">
          <DhcpSettingsSection
            inherit={dhcpInherit}
            serverGroupId={dhcpServerGroupId}
            onInheritChange={setDhcpInherit}
            onServerGroupIdChange={setDhcpServerGroupId}
            parentBlockId={subnet.block_id}
            fallbackSpaceId={subnet.space_id}
          />
        </div>
        <div className="border-t pt-3">
          <label className="flex items-start gap-2 text-xs cursor-pointer select-none">
            <input
              type="checkbox"
              className="mt-0.5 h-3.5 w-3.5"
              checked={dnsSplitHorizon}
              onChange={(e) => setDnsSplitHorizon(e.target.checked)}
            />
            <span>
              <span className="font-medium">DNS split-horizon publishing</span>
              <span className="ml-1 text-muted-foreground">
                — when on, the IP create / edit modal lets operators publish the
                same name into additional zones (typically an internal +
                external pair).
              </span>
            </span>
          </label>
        </div>
        <div className="border-t pt-3">
          <DdnsSettingsSection
            enabled={ddnsEnabled}
            policy={ddnsPolicy}
            domainOverride={ddnsDomainOverride}
            ttl={ddnsTtl}
            subnetNetwork={subnet.network}
            onEnabledChange={setDdnsEnabled}
            onPolicyChange={setDdnsPolicy}
            onDomainOverrideChange={setDdnsDomainOverride}
            onTtlChange={setDdnsTtl}
          />
        </div>
        <div className="border-t pt-3">
          <ProfilingSettingsSection
            enabled={autoProfileEnabled}
            preset={autoProfilePreset}
            refreshDays={autoProfileRefreshDays}
            onEnabledChange={setAutoProfileEnabled}
            onPresetChange={setAutoProfilePreset}
            onRefreshDaysChange={setAutoProfileRefreshDays}
          />
        </div>
        <div className="border-t pt-3">
          <ClassificationSection
            pciScope={pciScope}
            hipaaScope={hipaaScope}
            internetFacing={internetFacing}
            onPciChange={setPciScope}
            onHipaaChange={setHipaaScope}
            onInternetFacingChange={setInternetFacing}
          />
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={() => onClose()}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              setError(null);
              mutation.mutate();
            }}
            disabled={mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Saving…" : "Save"}
          </button>
        </div>
        {onDeleted && (
          <div className="mt-2 border-t pt-3">
            <button
              onClick={() => setDeleteStep(1)}
              className="text-xs text-destructive hover:underline"
            >
              Delete this subnet…
            </button>
          </div>
        )}
      </div>
    </Modal>
  );
}

// ─── DHCP Sync Modal ─────────────────────────────────────────────────────────
//
// Fires ``POST /dhcp/servers/{id}/sync-leases`` against every unique DHCP
// server that backs a scope in this subnet. Shows per-server counters as
// each result lands. The backend now deletes leases that vanished from
// the wire (``removed`` / ``ipam_revoked``) so a prominent row surfaces
// how many stale rows the sync cleaned up — the common reason a user
// clicks the button manually.

type DhcpServerSyncState =
  | { status: "pending" }
  | { status: "done"; result: DHCPLeaseSyncResult }
  | { status: "error"; error: string };

function useDhcpSync(subnetId: string, enabled: boolean) {
  const qc = useQueryClient();
  const { data: scopes = [] } = useQuery({
    queryKey: ["dhcp-scopes-subnet", subnetId],
    queryFn: () => dhcpApi.listScopesBySubnet(subnetId),
    enabled,
  });
  const { data: servers = [] } = useQuery({
    queryKey: ["dhcp-servers"],
    queryFn: () => dhcpApi.listServers(),
    enabled,
  });
  // Under the group-centric model, a scope targets a group, and every
  // member of that group serves the subnet. Fan the sync out to every
  // DHCP server whose group is hosting any scope for this subnet.
  const scopeGroupIds = new Set(scopes.map((sc) => sc.group_id));
  const serverIds = Array.from(
    new Set(
      servers
        .filter(
          (s) =>
            s.server_group_id != null && scopeGroupIds.has(s.server_group_id),
        )
        .map((s) => s.id),
    ),
  );
  const serverNames = new Map(servers.map((s) => [s.id, s.name]));
  const [state, setState] = useState<Map<string, DhcpServerSyncState>>(
    new Map(),
  );
  const kickedOff = useRef(false);

  useEffect(() => {
    if (!enabled || kickedOff.current || serverIds.length === 0) return;
    kickedOff.current = true;
    setState(
      new Map(serverIds.map((id) => [id, { status: "pending" as const }])),
    );
    serverIds.forEach((serverId) => {
      dhcpApi
        .syncLeasesNow(serverId)
        .then((result) => {
          setState((prev) => {
            const next = new Map(prev);
            next.set(serverId, { status: "done", result });
            return next;
          });
        })
        .catch((err: Error) => {
          setState((prev) => {
            const next = new Map(prev);
            next.set(serverId, {
              status: "error",
              error: err?.message ?? String(err),
            });
            return next;
          });
        });
    });
  }, [enabled, serverIds.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const allDone =
    state.size > 0 &&
    Array.from(state.values()).every((s) => s.status !== "pending");

  // When every server has reported, bust the caches that depend on
  // lease state so stale rows drop out of the address + lease views.
  useEffect(() => {
    if (!allDone) return;
    qc.invalidateQueries({ queryKey: ["addresses", subnetId] });
    qc.invalidateQueries({ queryKey: ["dhcp-leases"] });
    qc.invalidateQueries({ queryKey: ["dhcp-scopes-subnet", subnetId] });
    qc.invalidateQueries({ queryKey: ["dns-sync-summary", subnetId] });
  }, [allDone, qc, subnetId]);

  return { serverIds, serverNames, state, allDone };
}

function DhcpSyncSummaryBody({
  serverIds,
  serverNames,
  state,
}: {
  serverIds: string[];
  serverNames: Map<string, string>;
  state: Map<string, DhcpServerSyncState>;
}) {
  if (serverIds.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No DHCP scopes are attached to this subnet — nothing to sync.
      </p>
    );
  }
  return (
    <div className="space-y-2">
      {serverIds.map((serverId) => {
        const s = state.get(serverId) ?? { status: "pending" as const };
        const name = serverNames.get(serverId) ?? serverId.slice(0, 8);
        return (
          <div key={serverId} className="rounded-md border p-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">{name}</span>
              {s.status === "pending" && (
                <span className="flex items-center gap-1 text-xs text-muted-foreground">
                  <RefreshCw className="h-3 w-3 animate-spin" /> Syncing…
                </span>
              )}
              {s.status === "done" && (
                <span className="text-xs text-emerald-600 dark:text-emerald-400">
                  Done
                </span>
              )}
              {s.status === "error" && (
                <span className="text-xs text-destructive">Failed</span>
              )}
            </div>
            {s.status === "done" && (
              <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                <CounterRow
                  label="Active leases"
                  value={s.result.server_leases}
                />
                <CounterRow label="Refreshed" value={s.result.refreshed} />
                <CounterRow label="New" value={s.result.imported} />
                <CounterRow
                  label="Removed (deleted on server)"
                  value={s.result.removed}
                  emphasis={s.result.removed > 0}
                />
                <CounterRow
                  label="IPAM created"
                  value={s.result.ipam_created}
                />
                <CounterRow
                  label="IPAM revoked"
                  value={s.result.ipam_revoked}
                  emphasis={s.result.ipam_revoked > 0}
                />
                {s.result.errors.length > 0 && (
                  <div className="col-span-2 mt-1 rounded border border-destructive/40 bg-destructive/10 p-2 text-[11px] text-destructive">
                    {s.result.errors.length} error(s):
                    <ul className="ml-3 list-disc">
                      {s.result.errors.slice(0, 3).map((e, i) => (
                        <li key={i} className="truncate">
                          {e}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </dl>
            )}
            {s.status === "error" && (
              <p className="mt-2 rounded border border-destructive/40 bg-destructive/10 p-2 text-[11px] text-destructive">
                {s.error}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}

function CounterRow({
  label,
  value,
  emphasis,
}: {
  label: string;
  value: number;
  emphasis?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between">
      <dt className="text-muted-foreground">{label}</dt>
      <dd
        className={cn(
          "font-mono tabular-nums",
          emphasis &&
            value > 0 &&
            "font-semibold text-amber-600 dark:text-amber-400",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

function DhcpSyncModal({
  subnetId,
  onClose,
}: {
  subnetId: string;
  onClose: () => void;
}) {
  const { serverIds, serverNames, state, allDone } = useDhcpSync(
    subnetId,
    true,
  );
  return (
    <Modal title="DHCP Sync" onClose={onClose}>
      <DhcpSyncSummaryBody
        serverIds={serverIds}
        serverNames={serverNames}
        state={state}
      />
      <div className="mt-4 flex justify-end">
        <button
          onClick={onClose}
          disabled={!allDone && serverIds.length > 0}
          className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
        >
          {allDone || serverIds.length === 0 ? "Close" : "Syncing…"}
        </button>
      </div>
    </Modal>
  );
}

// ─── Sync All Modal ──────────────────────────────────────────────────────────
//
// One modal that covers both surfaces. DHCP sync runs inline (it's a pure
// pull — no user decisions to make). DNS sync is preview-and-apply with
// per-row selection, so the modal just shows a drift summary and chains
// into the full ``DnsSyncModal`` when the user clicks "Review DNS
// changes…".
function SyncAllModal({
  subnet,
  onOpenDnsDetails,
  onClose,
}: {
  subnet: Subnet;
  onOpenDnsDetails: () => void;
  onClose: () => void;
}) {
  const { serverIds, serverNames, state, allDone } = useDhcpSync(
    subnet.id,
    true,
  );
  const { data: dnsSummary, isLoading: dnsLoading } = useQuery({
    queryKey: ["dns-sync-summary", subnet.id],
    queryFn: () => ipamApi.dnsSyncSummary(subnet.id),
    refetchOnMount: "always",
  });

  const dhcpBusy = !allDone && serverIds.length > 0;

  return (
    <Modal title={`Sync All — ${subnet.network}`} onClose={onClose} wide>
      <div className="space-y-4">
        <section>
          <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <Server className="h-4 w-4" /> DHCP
          </h3>
          <DhcpSyncSummaryBody
            serverIds={serverIds}
            serverNames={serverNames}
            state={state}
          />
        </section>

        <section className="border-t pt-4">
          <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <Globe2 className="h-4 w-4" /> DNS
          </h3>
          {dnsLoading && (
            <p className="text-xs text-muted-foreground">Checking drift…</p>
          )}
          {dnsSummary && !dnsSummary.has_drift && (
            <p className="text-sm text-emerald-600 dark:text-emerald-400">
              In sync — no DNS drift detected.
            </p>
          )}
          {dnsSummary && dnsSummary.has_drift && (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs">
              <p className="font-medium text-amber-700 dark:text-amber-400">
                {dnsSummary.total} record
                {dnsSummary.total === 1 ? "" : "s"} out of sync
              </p>
              <p className="mt-1 text-muted-foreground">
                {dnsSummary.missing} missing · {dnsSummary.mismatched}{" "}
                mismatched · {dnsSummary.stale} stale
              </p>
              <p className="mt-2 text-muted-foreground">
                DNS sync needs per-record confirmation — open the detail view to
                review and apply.
              </p>
            </div>
          )}
        </section>

        <div className="flex items-center justify-between gap-2 border-t pt-3">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Close
          </button>
          <button
            onClick={onOpenDnsDetails}
            disabled={dhcpBusy || !dnsSummary?.has_drift}
            title={
              dhcpBusy
                ? "Wait for DHCP sync to finish"
                : !dnsSummary?.has_drift
                  ? "No DNS drift to apply"
                  : "Review and apply DNS changes"
            }
            className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            <Globe2 className="h-3.5 w-3.5" /> Review DNS changes…
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Edit Address Modal ───────────────────────────────────────────────────────

const ADDRESS_STATUSES = [
  "allocated",
  "reserved",
  "deprecated",
  "static_dhcp",
  "dhcp",
] as const;

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
  const [role, setRole] = useState<string>(address.role ?? "");
  // datetime-local needs ``YYYY-MM-DDTHH:MM`` (no seconds, no TZ).
  // Trim trailing seconds + drop the trailing ``Z`` if present.
  const [reservedUntil, setReservedUntil] = useState<string>(
    address.reserved_until
      ? new Date(address.reserved_until).toISOString().slice(0, 16)
      : "",
  );
  const [customFields, setCustomFields] = useState<Record<string, unknown>>(
    (address.custom_fields as Record<string, unknown>) ?? {},
  );
  const [dnsZoneId, setDnsZoneId] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [showMacHistory, setShowMacHistory] = useState(false);
  const [pendingWarnings, setPendingWarnings] = useState<
    CollisionWarning[] | null
  >(null);

  const { data: cfDefs = [] } = useQuery({
    queryKey: ["custom-fields", "ip_address"],
    queryFn: () => customFieldsApi.list("ip_address"),
  });

  // Fetch effective DNS for the subnet this IP belongs to
  const { data: effectiveDns } = useQuery({
    queryKey: ["effective-dns-subnet", address.subnet_id],
    queryFn: () => ipamApi.getEffectiveSubnetDns(address.subnet_id),
    staleTime: 30_000,
  });

  // Aliases live in the DNS zone but are owned by this IP (auto-deleted on purge).
  const { data: existingAliases = [] } = useQuery({
    queryKey: ["ip-aliases", address.id],
    queryFn: () => ipamApi.listAliases(address.id),
  });
  const [newAliasName, setNewAliasName] = useState("");
  const [newAliasType, setNewAliasType] = useState<"CNAME" | "A">("CNAME");
  const addAliasMut = useMutation({
    mutationFn: () =>
      ipamApi.addAlias(address.id, {
        name: newAliasName.trim(),
        record_type: newAliasType,
      }),
    onSuccess: () => {
      setNewAliasName("");
      qc.invalidateQueries({ queryKey: ["ip-aliases", address.id] });
      qc.invalidateQueries({ queryKey: ["addresses", address.subnet_id] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      qc.invalidateQueries({ queryKey: ["dns-group-records"] });
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
      qc.invalidateQueries({ queryKey: ["subnet-aliases", address.subnet_id] });
    },
    onError: (e: unknown) => {
      const err = e as {
        response?: { data?: { detail?: unknown } };
      };
      const d = err?.response?.data?.detail;
      setError(typeof d === "string" ? d : "Failed to add alias");
    },
  });
  const delAliasMut = useMutation({
    mutationFn: (rid: string) => ipamApi.deleteAlias(address.id, rid),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ip-aliases", address.id] });
      qc.invalidateQueries({ queryKey: ["subnet-aliases", address.subnet_id] });
    },
  });

  const zoneGroupIds: string[] = effectiveDns?.dns_group_ids ?? [];
  const zoneQueries = useQueries({
    queries: (zoneGroupIds as string[]).map((gId: string) => ({
      queryKey: ["dns-zones", gId],
      queryFn: () => dnsApi.listZones(gId),
      staleTime: 60_000,
    })),
  });
  const allGroupZones: DNSZone[] = zoneQueries
    .flatMap((q: { data?: DNSZone[] }) => q.data ?? [])
    .filter((z: DNSZone) => !z.name.toLowerCase().includes("arpa"));

  // When the block/subnet has an explicit primary zone and/or additional
  // zones, restrict the picker to just those. Falling back to every zone in
  // the group only happens when the admin picked a group without pinning
  // specific zones.
  const explicitZoneIds = [
    ...(effectiveDns?.dns_zone_id ? [effectiveDns.dns_zone_id] : []),
    ...(effectiveDns?.dns_additional_zone_ids ?? []),
  ];
  const availableZones: DNSZone[] =
    explicitZoneIds.length > 0
      ? allGroupZones.filter((z: DNSZone) => explicitZoneIds.includes(z.id))
      : allGroupZones;

  // Pre-select zone from current FQDN or primary zone
  useEffect(() => {
    if (!dnsZoneId && availableZones.length > 0) {
      const primary = effectiveDns?.dns_zone_id;
      setDnsZoneId(
        primary && availableZones.some((z: DNSZone) => z.id === primary)
          ? primary
          : availableZones[0].id,
      );
    }
  }, [availableZones.length, effectiveDns?.dns_zone_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedZone = availableZones.find((z: DNSZone) => z.id === dnsZoneId);
  const fqdnPreview =
    hostname && selectedZone
      ? `${hostname}.${selectedZone.name.replace(/\.$/, "")}`
      : null;

  const mutation = useMutation({
    mutationFn: (force: boolean) => {
      // Empty string on role → null (clears the field).
      // Empty string on reservedUntil → null when status=reserved
      // (= no TTL); when status moved off reserved we still send
      // null so the backend's safety guard can clear it explicitly.
      const reservedIso =
        status === "reserved" && reservedUntil
          ? new Date(reservedUntil).toISOString()
          : null;
      return ipamApi.updateAddress(address.id, {
        hostname: hostname || undefined,
        description: description || undefined,
        mac_address: macAddress || undefined,
        status,
        custom_fields: customFields,
        dns_zone_id: dnsZoneId || undefined,
        role: (role || null) as IPRole | null,
        reserved_until: reservedIso,
        force,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["addresses", address.subnet_id] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      qc.invalidateQueries({ queryKey: ["dns-group-records"] });
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
      onClose();
    },
    onError: (err: unknown) => {
      const warnings = parseCollisionWarnings(err);
      if (warnings && warnings.length > 0) {
        setPendingWarnings(warnings);
        setError(null);
        return;
      }
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to save";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  // Clear the warning when the user changes a collision-relevant field.
  useEffect(() => {
    setPendingWarnings(null);
  }, [hostname, macAddress, dnsZoneId]);

  // ── Tabs ────────────────────────────────────────────────────────────
  // EditAddressModal is also the "IP detail panel" surface — the
  // network-discovery feature mounts a second tab here that surfaces
  // switch-port info from the FDB join. Keeping the form on the
  // "Details" tab means the modal is unchanged for users who never
  // open Network.
  const [activeIpTab, setActiveIpTab] = useState<"details" | "network">(
    "details",
  );
  const { data: networkRows } = useNetworkContext(address.id);
  const networkCount = networkRows?.length ?? 0;
  const [nmapOpen, setNmapOpen] = useState(false);

  return (
    <Modal title={`Edit ${address.address}`} onClose={onClose}>
      <div className="-mt-1 mb-3 flex gap-1 border-b">
        <button
          type="button"
          onClick={() => setActiveIpTab("details")}
          className={cn(
            "-mb-px border-b-2 px-3 py-1.5 text-sm",
            activeIpTab === "details"
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
          )}
        >
          Details
        </button>
        <button
          type="button"
          onClick={() => setActiveIpTab("network")}
          className={cn(
            "-mb-px border-b-2 px-3 py-1.5 text-sm",
            activeIpTab === "network"
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
          )}
        >
          Network
          <NetworkTabBadge count={networkCount} />
        </button>
        <button
          type="button"
          onClick={() => setNmapOpen(true)}
          className="ml-auto -mb-px inline-flex items-center gap-1 border-b-2 border-transparent px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
          title="Run an nmap scan against this IP"
        >
          <Radar className="h-3.5 w-3.5" />
          Scan with Nmap
        </button>
      </div>
      {nmapOpen && (
        <NmapScanModal
          ip={address.address}
          ipAddressId={address.id}
          onClose={() => setNmapOpen(false)}
        />
      )}
      {activeIpTab === "network" && (
        <div className="pb-2">
          <IPNetworkTab addressId={address.id} />
        </div>
      )}
      <div className={cn("space-y-3", activeIpTab !== "details" && "hidden")}>
        <Field label="Hostname">
          <input
            className={inputCls}
            value={hostname}
            onChange={(e) => setHostname(e.target.value)}
            placeholder="Optional"
            autoFocus
          />
        </Field>
        {/* DNS zone — only shown when zones are available */}
        {availableZones.length > 0 && (
          <Field label="DNS Zone">
            {availableZones.length === 1 ? (
              <p className="text-xs text-muted-foreground py-1">
                <Globe2 className="inline h-3 w-3 mr-1" />
                {availableZones[0].name.replace(/\.$/, "")}
                {fqdnPreview && (
                  <span className="ml-2 font-mono text-emerald-600 dark:text-emerald-400">
                    → {fqdnPreview}
                  </span>
                )}
              </p>
            ) : (
              <div className="space-y-1">
                <select
                  className={inputCls}
                  value={dnsZoneId}
                  onChange={(e) => setDnsZoneId(e.target.value)}
                >
                  <ZoneOptions
                    zones={availableZones}
                    primaryId={effectiveDns?.dns_zone_id}
                    additionalIds={effectiveDns?.dns_additional_zone_ids ?? []}
                    noneOption="None (remove DNS record)"
                  />
                </select>
                {fqdnPreview && (
                  <p className="text-xs font-mono text-emerald-600 dark:text-emerald-400">
                    → {fqdnPreview}
                  </p>
                )}
              </div>
            )}
          </Field>
        )}
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
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </Field>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Role">
            <select
              className={inputCls}
              value={role}
              onChange={(e) => setRole(e.target.value)}
            >
              <option value="">— None —</option>
              {IP_ROLE_OPTIONS.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
            {role === "vrrp" || role === "vip" || role === "anycast" ? (
              <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-400">
                Shared-by-design role — MAC collision warnings are suppressed.
              </p>
            ) : null}
          </Field>
          {status === "reserved" ? (
            <Field label="Reserved until">
              <input
                type="datetime-local"
                className={inputCls}
                value={reservedUntil}
                onChange={(e) => setReservedUntil(e.target.value)}
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                Optional TTL — auto-released after this time.
              </p>
            </Field>
          ) : (
            <div />
          )}
        </div>
        {address.mac_address ? (
          <div className="-mt-1 mb-1">
            <button
              type="button"
              onClick={() => setShowMacHistory(true)}
              className="rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-muted"
            >
              MAC history…
            </button>
          </div>
        ) : null}

        <Field label="DNS Aliases">
          <div className="space-y-1.5">
            <p className="text-[11px] text-muted-foreground -mt-0.5">
              Extra records pointing to this IP. Added records are removed
              automatically when the IP is purged.
            </p>
            {existingAliases.length === 0 ? (
              <p className="text-xs text-muted-foreground/60 italic">
                No aliases.
              </p>
            ) : (
              <div className="space-y-1">
                {existingAliases.map((a) => (
                  <div
                    key={a.id}
                    className="flex items-center gap-2 rounded-md border bg-muted/30 px-2 py-1"
                  >
                    <span className="rounded bg-background px-1.5 py-0.5 text-[10px] font-medium">
                      {a.record_type}
                    </span>
                    <span className="flex-1 truncate font-mono text-xs">
                      {a.fqdn}
                    </span>
                    <span className="text-[11px] text-muted-foreground truncate">
                      → {a.value}
                    </span>
                    <button
                      type="button"
                      onClick={() => delAliasMut.mutate(a.id)}
                      className="flex-shrink-0 rounded p-0.5 text-muted-foreground hover:text-destructive"
                      title="Delete alias"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="flex items-center gap-2 pt-1">
              <select
                className={cn(inputCls, "w-24")}
                value={newAliasType}
                onChange={(e) =>
                  setNewAliasType(e.target.value as "CNAME" | "A")
                }
              >
                <option value="CNAME">CNAME</option>
                <option value="A">A</option>
              </select>
              <input
                className={cn(inputCls, "flex-1 min-w-0")}
                placeholder="alias name (e.g. www, mail)"
                value={newAliasName}
                onChange={(e) => setNewAliasName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && newAliasName.trim()) {
                    e.preventDefault();
                    addAliasMut.mutate();
                  }
                }}
              />
              <button
                type="button"
                onClick={() => addAliasMut.mutate()}
                disabled={
                  !newAliasName.trim() ||
                  addAliasMut.isPending ||
                  !(address.forward_zone_id || effectiveDns?.dns_zone_id)
                }
                className="flex-shrink-0 rounded-md border px-2 py-1 text-xs hover:bg-accent disabled:opacity-40"
              >
                <Plus className="h-3 w-3 inline" /> Add
              </button>
            </div>
          </div>
        </Field>

        <CustomFieldsSection
          definitions={cfDefs}
          values={customFields}
          onChange={(k, v) => setCustomFields((prev) => ({ ...prev, [k]: v }))}
        />
        {error && <p className="text-xs text-destructive">{error}</p>}
        {pendingWarnings && (
          <CollisionWarningBanner warnings={pendingWarnings} />
        )}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              setError(null);
              mutation.mutate(pendingWarnings != null);
            }}
            disabled={mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending
              ? "Saving…"
              : pendingWarnings
                ? "Save anyway"
                : "Save"}
          </button>
        </div>
      </div>
      {showMacHistory && (
        <MacHistoryModal
          address={address}
          onClose={() => setShowMacHistory(false)}
        />
      )}
    </Modal>
  );
}

function MacHistoryModal({
  address,
  onClose,
}: {
  address: IPAddress;
  onClose: () => void;
}) {
  const { data: history = [], isLoading } = useQuery<MacHistoryEntry[]>({
    queryKey: ["mac-history", address.id],
    queryFn: () => ipamApi.listMacHistory(address.id),
  });
  return (
    <Modal title={`MAC history — ${address.address}`} onClose={onClose} wide>
      <div className="space-y-3">
        <p className="text-xs text-muted-foreground">
          Every distinct MAC ever assigned to this IP, newest activity first.
          ``last_seen`` bumps on every IP write that carries the same MAC; a MAC
          change appends a new row instead of overwriting the previous one.
        </p>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : history.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No MAC observations recorded for this IP yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[480px] text-sm">
              <thead>
                <tr className="border-b bg-muted/40 text-xs">
                  <th className="px-3 py-1.5 text-left font-medium">MAC</th>
                  <th className="px-3 py-1.5 text-left font-medium">Vendor</th>
                  <th className="px-3 py-1.5 text-left font-medium">
                    First seen
                  </th>
                  <th className="px-3 py-1.5 text-left font-medium">
                    Last seen
                  </th>
                </tr>
              </thead>
              <tbody className={zebraBodyCls}>
                {history.map((row) => (
                  <tr key={row.id} className="border-b last:border-0">
                    <td className="px-3 py-1.5 font-mono text-xs">
                      {row.mac_address}
                    </td>
                    <td className="px-3 py-1.5 text-xs text-muted-foreground">
                      {row.vendor ?? (
                        <span className="text-muted-foreground/40">—</span>
                      )}
                    </td>
                    <td className="px-3 py-1.5 text-xs text-muted-foreground">
                      {new Date(row.first_seen).toLocaleString()}
                    </td>
                    <td className="px-3 py-1.5 text-xs text-muted-foreground">
                      {new Date(row.last_seen).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Modal>
  );
}

// ─── Aliases Subnet Panel ────────────────────────────────────────────────────

function _formatNatPorts(start: number | null, end: number | null): string {
  if (start == null && end == null) return "";
  if (end == null || end === start) return `:${start}`;
  return `:${start}–${end}`;
}

function _formatNatLine(m: NATMapping): {
  from: string;
  to: string;
  proto: string;
} {
  // hide-NAT mappings have a subnet on the internal side (no single IP);
  // render the subnet's CIDR, falling back to its name, then to a UUID
  // prefix only as a last-resort label.
  const subnetLabel =
    m.internal_subnet_cidr ??
    m.internal_subnet_name ??
    (m.internal_subnet_id
      ? `subnet ${m.internal_subnet_id.slice(0, 8)}…`
      : "—");
  const internal =
    (m.internal_ip ?? (m.internal_subnet_id ? subnetLabel : "—")) +
    _formatNatPorts(m.internal_port_start, m.internal_port_end);
  const external =
    (m.external_ip ?? "—") +
    _formatNatPorts(m.external_port_start, m.external_port_end);
  return { from: internal, to: external, proto: m.protocol };
}

function NatRowsTable({
  rows,
  emptyText,
}: {
  rows: NATMapping[];
  emptyText: string;
}) {
  if (rows.length === 0) {
    return (
      <p className="px-6 py-4 text-sm text-muted-foreground">{emptyText}</p>
    );
  }
  return (
    <table className="w-full text-sm">
      <thead className="bg-muted/40 text-xs">
        <tr>
          <th className="px-3 py-2 text-left font-medium">Name</th>
          <th className="px-3 py-2 text-left font-medium">Kind</th>
          <th className="px-3 py-2 text-left font-medium">
            Internal → External
          </th>
          <th className="px-3 py-2 text-left font-medium">Proto</th>
          <th className="px-3 py-2 text-left font-medium">Device</th>
          <th className="px-3 py-2 text-left font-medium">Description</th>
        </tr>
      </thead>
      <tbody className={zebraBodyCls}>
        {rows.map((m) => {
          const f = _formatNatLine(m);
          return (
            <tr key={m.id}>
              <td className="px-3 py-1.5 font-medium">{m.name}</td>
              <td className="px-3 py-1.5">
                <span className="inline-flex rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase">
                  {m.kind}
                </span>
              </td>
              <td className="px-3 py-1.5 font-mono text-xs">
                {f.from}
                <span className="mx-1 text-muted-foreground">→</span>
                {f.to}
              </td>
              <td className="px-3 py-1.5 text-xs uppercase text-muted-foreground">
                {f.proto}
              </td>
              <td className="px-3 py-1.5 text-xs text-muted-foreground">
                {m.device_label ?? "—"}
              </td>
              <td className="px-3 py-1.5 text-xs text-muted-foreground">
                {m.description ?? "—"}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function NatSubnetPanel({ subnetId }: { subnetId: string }) {
  const { data = [], isLoading } = useQuery({
    queryKey: ["subnet-nat-mappings", subnetId],
    queryFn: () => natApi.bySubnet(subnetId),
  });
  if (isLoading) {
    return <p className="px-6 py-4 text-sm text-muted-foreground">Loading…</p>;
  }
  return (
    <div className="space-y-2 p-2">
      <p className="px-3 pt-1 text-xs text-muted-foreground">
        Every NAT mapping whose internal IP falls inside this subnet's CIDR, or
        that's pinned to it as a hide-NAT source.
      </p>
      <NatRowsTable
        rows={data}
        emptyText="No NAT mappings reference any IP in this subnet."
      />
    </div>
  );
}

function NatMappingsForIpModal({
  ip,
  onClose,
}: {
  ip: IPAddress;
  onClose: () => void;
}) {
  const { data = [], isLoading } = useQuery({
    queryKey: ["nat-by-ip", ip.id],
    queryFn: () => natApi.byIp(ip.id),
  });
  return (
    <Modal title={`NAT mappings for ${ip.address}`} onClose={onClose}>
      <div className="space-y-3">
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : (
          <NatRowsTable
            rows={data}
            emptyText="No NAT mappings reference this IP."
          />
        )}
        <div className="flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
          >
            Close
          </button>
        </div>
      </div>
    </Modal>
  );
}

function AliasesSubnetPanel({ subnetId }: { subnetId: string }) {
  const qc = useQueryClient();
  const { data: aliases = [], isLoading } = useQuery({
    queryKey: ["subnet-aliases", subnetId],
    queryFn: () => ipamApi.listSubnetAliases(subnetId),
  });
  const [confirmDel, setConfirmDel] = useState<{
    ip_address_id: string;
    id: string;
    fqdn: string;
  } | null>(null);

  const delAlias = useMutation({
    mutationFn: (a: { ip_address_id: string; id: string }) =>
      ipamApi.deleteAlias(a.ip_address_id, a.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnet-aliases", subnetId] });
      qc.invalidateQueries({ queryKey: ["addresses", subnetId] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      qc.invalidateQueries({ queryKey: ["dns-group-records"] });
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
      setConfirmDel(null);
    },
  });

  if (isLoading) {
    return (
      <p className="p-6 text-sm text-muted-foreground">Loading aliases…</p>
    );
  }
  if (aliases.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <Globe2 className="mb-3 h-10 w-10 text-muted-foreground/30" />
        <p className="text-sm text-muted-foreground">
          No aliases in this subnet.
        </p>
        <p className="mt-1 text-xs text-muted-foreground/70">
          Add aliases from the IP address edit or allocate modal.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <thead>
          <tr className="border-b bg-muted/40 text-xs">
            <th className="px-4 py-2 text-left font-medium">Alias</th>
            <th className="px-4 py-2 text-left font-medium">Type</th>
            <th className="px-4 py-2 text-left font-medium">Target</th>
            <th className="px-4 py-2 text-left font-medium">IP</th>
            <th className="px-4 py-2 text-left font-medium">Host</th>
            <th className="px-4 py-2" />
          </tr>
        </thead>
        <tbody className={zebraBodyCls}>
          {aliases.map((a) => (
            <tr key={a.id} className="border-b last:border-0 hover:bg-muted/20">
              <td className="px-4 py-2 font-mono text-xs">{a.fqdn}</td>
              <td className="px-4 py-2">
                <span className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium">
                  {a.record_type}
                </span>
              </td>
              <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                {a.value}
              </td>
              <td className="px-4 py-2 font-mono text-xs">{a.ip_address}</td>
              <td className="px-4 py-2 text-muted-foreground">
                {a.ip_hostname ?? (
                  <span className="text-muted-foreground/40">—</span>
                )}
              </td>
              <td className="px-4 py-2 text-right">
                <button
                  onClick={() =>
                    setConfirmDel({
                      ip_address_id: a.ip_address_id,
                      id: a.id,
                      fqdn: a.fqdn,
                    })
                  }
                  className="rounded p-1 text-muted-foreground hover:text-destructive"
                  title="Delete alias"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {confirmDel && (
        <ConfirmDeleteModal
          title="Delete alias"
          message={`Delete alias ${confirmDel.fqdn}? The DNS record will be removed.`}
          onConfirm={() =>
            delAlias.mutate({
              ip_address_id: confirmDel.ip_address_id,
              id: confirmDel.id,
            })
          }
          onClose={() => setConfirmDel(null)}
          isPending={delAlias.isPending}
        />
      )}
    </div>
  );
}

// ─── Bulk Edit / Bulk Delete modals ──────────────────────────────────────────

function BulkEditAddressesModal({
  ipIds,
  subnetId,
  onClose,
  onDone,
}: {
  ipIds: string[];
  subnetId: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const qc = useQueryClient();
  const [editStatus, setEditStatus] = useState(false);
  const [status, setStatus] = useState<string>("allocated");
  const [editDescription, setEditDescription] = useState(false);
  const [description, setDescription] = useState("");
  const [editTags, setEditTags] = useState(false);
  const [replaceAllTags, setReplaceAllTags] = useState(false);
  // Each row: k = key, v = value, remove = true means delete that key on all IPs.
  const [tagRows, setTagRows] = useState<
    { k: string; v: string; remove: boolean }[]
  >([]);
  const [customFields, setCustomFields] = useState<Record<string, unknown>>({});
  // Per-custom-field opt-in: only keys set here are sent in the merge payload.
  const [cfOptIn, setCfOptIn] = useState<Record<string, boolean>>({});
  const [editCustomFields, setEditCustomFields] = useState(false);
  const [editDnsZone, setEditDnsZone] = useState(false);
  const [dnsZoneId, setDnsZoneId] = useState<string>("");
  const [editRole, setEditRole] = useState(false);
  const [role, setRole] = useState<string>("");
  const [editReservedUntil, setEditReservedUntil] = useState(false);
  const [reservedUntil, setReservedUntil] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  const { data: cfDefs = [] } = useQuery({
    queryKey: ["custom-fields", "ip_address"],
    queryFn: () => customFieldsApi.list("ip_address"),
  });

  // Same shape the single-address modal uses: load the effective DNS config
  // for the subnet, then restrict the picker to explicit primary + additional
  // zones. Falling back to all group zones only when nothing is pinned.
  const { data: effectiveDns } = useQuery({
    queryKey: ["effective-dns-subnet", subnetId],
    queryFn: () => ipamApi.getEffectiveSubnetDns(subnetId),
    staleTime: 30_000,
  });
  const zoneGroupIds: string[] = effectiveDns?.dns_group_ids ?? [];
  const zoneQueries = useQueries({
    queries: zoneGroupIds.map((gId: string) => ({
      queryKey: ["dns-zones", gId],
      queryFn: () => dnsApi.listZones(gId),
      staleTime: 60_000,
    })),
  });
  const allGroupZones: DNSZone[] = zoneQueries
    .flatMap((q: { data?: DNSZone[] }) => q.data ?? [])
    .filter((z: DNSZone) => !z.name.toLowerCase().includes("arpa"));
  const explicitZoneIds = [
    ...(effectiveDns?.dns_zone_id ? [effectiveDns.dns_zone_id] : []),
    ...(effectiveDns?.dns_additional_zone_ids ?? []),
  ];
  const availableZones: DNSZone[] =
    explicitZoneIds.length > 0
      ? allGroupZones.filter((z: DNSZone) => explicitZoneIds.includes(z.id))
      : allGroupZones;

  useEffect(() => {
    if (editDnsZone && !dnsZoneId && availableZones.length > 0) {
      const primary = effectiveDns?.dns_zone_id;
      setDnsZoneId(
        primary && availableZones.some((z: DNSZone) => z.id === primary)
          ? primary
          : availableZones[0].id,
      );
    }
  }, [editDnsZone, availableZones.length, effectiveDns?.dns_zone_id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Needed for "replace all tags" mode: we need the union of existing keys
  // across the selected IPs so we can null them out server-side.
  const { data: subnetAddresses = [] } = useQuery({
    queryKey: ["addresses", subnetId],
    queryFn: () => ipamApi.listAddresses(subnetId),
  });
  const selectedIpSet = new Set(ipIds);
  const existingTagKeys = new Set<string>();
  for (const ip of subnetAddresses) {
    if (!selectedIpSet.has(ip.id)) continue;
    const t = (ip.tags as Record<string, unknown> | null) ?? {};
    for (const k of Object.keys(t)) existingTagKeys.add(k);
  }

  const mutation = useMutation({
    mutationFn: () => {
      const changes: {
        status?: string;
        description?: string;
        tags?: Record<string, unknown>;
        custom_fields?: Record<string, unknown>;
        dns_zone_id?: string;
        role?: IPRole | "" | null;
        reserved_until?: string | null;
      } = {};
      if (editStatus) changes.status = status;
      if (editDescription) changes.description = description;
      if (editRole) {
        // Empty string clears the role on every selected IP.
        changes.role = (role || "") as IPRole | "";
      }
      if (editReservedUntil) {
        changes.reserved_until = reservedUntil
          ? new Date(reservedUntil).toISOString()
          : null;
      }

      if (editTags) {
        const tagsObj: Record<string, unknown> = {};
        const keptKeys = new Set<string>();
        for (const row of tagRows) {
          const k = row.k.trim();
          if (!k) continue;
          if (row.remove) {
            tagsObj[k] = null;
          } else {
            tagsObj[k] = row.v;
            keptKeys.add(k);
          }
        }
        if (replaceAllTags) {
          // Null-out every pre-existing key the user didn't explicitly keep —
          // turns the default "merge" semantic into "replace" via the
          // backend's null-removes rule, without needing a new backend flag.
          for (const k of existingTagKeys) {
            if (!keptKeys.has(k)) tagsObj[k] = null;
          }
        }
        if (Object.keys(tagsObj).length > 0) changes.tags = tagsObj;
      }

      if (editCustomFields) {
        const cfObj: Record<string, unknown> = {};
        for (const [key, optedIn] of Object.entries(cfOptIn)) {
          if (!optedIn) continue;
          const v = customFields[key];
          // Treat explicitly empty as a removal (null). Without this the
          // backend would store "" and the field would still look "set".
          cfObj[key] = v === "" || v === undefined ? null : v;
        }
        if (Object.keys(cfObj).length > 0) changes.custom_fields = cfObj;
      }
      if (editDnsZone) changes.dns_zone_id = dnsZoneId; // "" clears the zone
      return ipamApi.bulkEditAddresses({ ip_ids: ipIds, changes });
    },
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["addresses", subnetId] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      qc.invalidateQueries({ queryKey: ["dns-group-records"] });
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
      qc.invalidateQueries({ queryKey: ["subnet-aliases", subnetId] });
      if (res.skipped.length > 0) {
        setError(
          `${res.updated_count} updated; ${res.skipped.length} skipped (system/orphan rows).`,
        );
        setTimeout(onDone, 1200);
      } else {
        onDone();
      }
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to apply bulk edit";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  const hasTagChanges =
    editTags &&
    (tagRows.some((r) => r.k.trim() !== "") ||
      (replaceAllTags && existingTagKeys.size > 0));
  const hasCfChanges = editCustomFields && Object.values(cfOptIn).some(Boolean);
  const hasChanges =
    editStatus ||
    editDescription ||
    hasTagChanges ||
    hasCfChanges ||
    editDnsZone ||
    editRole ||
    editReservedUntil;

  return (
    <Modal title={`Bulk edit ${ipIds.length} IP addresses`} onClose={onClose}>
      <div className="space-y-3">
        <p className="text-xs text-muted-foreground">
          Only fields you tick will be touched. Tags and custom fields are
          merged by default — enable <em>Replace all tags</em> to overwrite
          everything, or mark a tag row as <em>remove</em> to delete just that
          key.
        </p>

        <div className="rounded-md border p-3 space-y-2">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              checked={editStatus}
              onChange={(e) => setEditStatus(e.target.checked)}
            />
            Status
          </label>
          {editStatus && (
            <select
              className={inputCls}
              value={status}
              onChange={(e) => setStatus(e.target.value)}
            >
              {[
                "available",
                "allocated",
                "reserved",
                "static_dhcp",
                "deprecated",
              ].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          )}
        </div>

        <div className="rounded-md border p-3 space-y-2">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              checked={editDescription}
              onChange={(e) => setEditDescription(e.target.checked)}
            />
            Description
          </label>
          {editDescription && (
            <input
              className={inputCls}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Replace description with…"
            />
          )}
        </div>

        <div className="rounded-md border p-3 space-y-2">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              checked={editRole}
              onChange={(e) => setEditRole(e.target.checked)}
            />
            Role
          </label>
          {editRole && (
            <select
              className={inputCls}
              value={role}
              onChange={(e) => setRole(e.target.value)}
            >
              <option value="">— None (clear) —</option>
              {IP_ROLE_OPTIONS.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          )}
        </div>

        <div className="rounded-md border p-3 space-y-2">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              checked={editReservedUntil}
              onChange={(e) => setEditReservedUntil(e.target.checked)}
            />
            Reserved until
          </label>
          {editReservedUntil && (
            <>
              <input
                type="datetime-local"
                className={inputCls}
                value={reservedUntil}
                onChange={(e) => setReservedUntil(e.target.value)}
              />
              <p className="text-[11px] text-muted-foreground">
                Sets the TTL on every selected IP. Leave blank to clear the TTL
                (= indefinite reservation). The reservation sweep flips expired
                rows back to
                <em> available</em>.
              </p>
            </>
          )}
        </div>

        {availableZones.length > 0 && (
          <div className="rounded-md border p-3 space-y-2">
            <label className="flex items-center gap-2 text-sm font-medium">
              <input
                type="checkbox"
                checked={editDnsZone}
                onChange={(e) => setEditDnsZone(e.target.checked)}
              />
              DNS Zone
            </label>
            {editDnsZone && (
              <div className="space-y-1">
                <select
                  className={inputCls}
                  value={dnsZoneId}
                  onChange={(e) => setDnsZoneId(e.target.value)}
                >
                  <ZoneOptions
                    zones={availableZones}
                    primaryId={effectiveDns?.dns_zone_id}
                    additionalIds={effectiveDns?.dns_additional_zone_ids ?? []}
                    noneOption="None (remove DNS records)"
                  />
                </select>
                <p className="text-[11px] text-muted-foreground">
                  Moves every selected IP's forward record to this zone (and
                  deletes the old record if present). Picking &ldquo;None&rdquo;
                  removes the DNS record entirely.
                </p>
              </div>
            )}
          </div>
        )}

        <div className="rounded-md border p-3 space-y-2">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              checked={editTags}
              onChange={(e) => setEditTags(e.target.checked)}
            />
            Tags
          </label>
          {editTags && (
            <>
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={replaceAllTags}
                  onChange={(e) => setReplaceAllTags(e.target.checked)}
                />
                Replace all tags (clear existing keys that aren't listed below)
              </label>
              <p className="text-[11px] text-muted-foreground">
                {replaceAllTags ? (
                  <>
                    Selected IPs currently have {existingTagKeys.size} distinct
                    tag keys — any key not listed below will be removed.
                  </>
                ) : (
                  <>
                    Each row adds or updates one key on every selected IP.
                    Toggle <em>remove</em> to delete that key instead.
                  </>
                )}
              </p>
              {tagRows.map((row, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input
                    className={cn(inputCls, "flex-1")}
                    value={row.k}
                    onChange={(e) =>
                      setTagRows((p) =>
                        p.map((r, j) =>
                          i === j ? { ...r, k: e.target.value } : r,
                        ),
                      )
                    }
                    placeholder="key"
                  />
                  <input
                    className={cn(
                      inputCls,
                      "flex-1",
                      row.remove && "opacity-40",
                    )}
                    value={row.v}
                    disabled={row.remove}
                    onChange={(e) =>
                      setTagRows((p) =>
                        p.map((r, j) =>
                          i === j ? { ...r, v: e.target.value } : r,
                        ),
                      )
                    }
                    placeholder={row.remove ? "(will remove key)" : "value"}
                  />
                  <label
                    className="flex flex-shrink-0 items-center gap-1 text-[11px] text-muted-foreground"
                    title="If checked, this key is removed from all selected IPs."
                  >
                    <input
                      type="checkbox"
                      checked={row.remove}
                      onChange={(e) =>
                        setTagRows((p) =>
                          p.map((r, j) =>
                            i === j ? { ...r, remove: e.target.checked } : r,
                          ),
                        )
                      }
                    />
                    remove
                  </label>
                  <button
                    type="button"
                    onClick={() =>
                      setTagRows((p) => p.filter((_, j) => j !== i))
                    }
                    className="rounded p-1 text-muted-foreground hover:text-destructive"
                    title="Discard row"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={() =>
                  setTagRows((p) => [...p, { k: "", v: "", remove: false }])
                }
                className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
              >
                <Plus className="h-3 w-3" /> Add tag
              </button>
            </>
          )}
        </div>

        {cfDefs.length > 0 && (
          <div className="rounded-md border p-3 space-y-2">
            <label className="flex items-center gap-2 text-sm font-medium">
              <input
                type="checkbox"
                checked={editCustomFields}
                onChange={(e) => setEditCustomFields(e.target.checked)}
              />
              Custom fields (merge)
            </label>
            {editCustomFields && (
              <>
                <p className="text-[11px] text-muted-foreground -mt-1">
                  Tick the box next to each field you want to change — only
                  ticked fields are written to selected IPs. Leaving a ticked
                  field empty clears it on every selected IP.
                </p>
                <div className="space-y-3">
                  {cfDefs.map((def) => {
                    const checked = !!cfOptIn[def.name];
                    const val = customFields[def.name] ?? "";
                    return (
                      <div key={def.name} className="space-y-1">
                        <label className="flex items-center gap-2 text-xs font-medium">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={(e) =>
                              setCfOptIn((prev) => ({
                                ...prev,
                                [def.name]: e.target.checked,
                              }))
                            }
                          />
                          {def.label}
                        </label>
                        {checked && (
                          <div className="pl-5">
                            {def.description && (
                              <p className="mb-1 text-xs text-muted-foreground">
                                {def.description}
                              </p>
                            )}
                            {def.field_type === "boolean" ? (
                              <input
                                type="checkbox"
                                className="rounded"
                                checked={!!val}
                                onChange={(e) =>
                                  setCustomFields((prev) => ({
                                    ...prev,
                                    [def.name]: e.target.checked,
                                  }))
                                }
                              />
                            ) : def.field_type === "select" && def.options ? (
                              <select
                                className={inputCls}
                                value={String(val)}
                                onChange={(e) =>
                                  setCustomFields((prev) => ({
                                    ...prev,
                                    [def.name]: e.target.value,
                                  }))
                                }
                              >
                                <option value="">— Clear value —</option>
                                {def.options.map((opt) => (
                                  <option key={opt} value={opt}>
                                    {opt}
                                  </option>
                                ))}
                              </select>
                            ) : (
                              <input
                                className={inputCls}
                                type={
                                  def.field_type === "number"
                                    ? "number"
                                    : def.field_type === "email"
                                      ? "email"
                                      : def.field_type === "url"
                                        ? "url"
                                        : "text"
                                }
                                value={String(val)}
                                onChange={(e) =>
                                  setCustomFields((prev) => ({
                                    ...prev,
                                    [def.name]: e.target.value,
                                  }))
                                }
                                placeholder="(empty clears field)"
                              />
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </div>
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
            onClick={() => {
              setError(null);
              mutation.mutate();
            }}
            disabled={!hasChanges || mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Applying…" : "Apply to all"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function BulkDeleteAddressesModal({
  ipIds,
  subnetId,
  onClose,
  onDone,
}: {
  ipIds: string[];
  subnetId: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const qc = useQueryClient();
  const [permanent, setPermanent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => ipamApi.bulkDeleteAddresses({ ip_ids: ipIds, permanent }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["addresses", subnetId] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      qc.invalidateQueries({ queryKey: ["dns-group-records"] });
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
      qc.invalidateQueries({ queryKey: ["subnet-aliases", subnetId] });
      if (res.skipped.length > 0) {
        setError(
          `${res.deleted_count} deleted; ${res.skipped.length} skipped (system rows).`,
        );
        setTimeout(onDone, 1200);
      } else {
        onDone();
      }
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to delete";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  return (
    <Modal title={`Delete ${ipIds.length} IP addresses`} onClose={onClose}>
      <div className="space-y-3">
        <p className="text-sm">
          {permanent ? (
            <span className="text-destructive">
              Permanently delete {ipIds.length} IP
              {ipIds.length === 1 ? "" : "s"}? This cannot be undone.
            </span>
          ) : (
            <>
              Mark {ipIds.length} IP{ipIds.length === 1 ? "" : "s"} as{" "}
              <span className="font-medium">orphan</span>. They can be restored
              later.
            </>
          )}
        </p>
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={permanent}
            onChange={(e) => setPermanent(e.target.checked)}
          />
          Permanently delete instead of soft-delete
        </label>
        <p className="text-[11px] text-muted-foreground">
          System rows (network, broadcast) are always skipped.
        </p>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              setError(null);
              mutation.mutate();
            }}
            disabled={mutation.isPending}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50",
              permanent
                ? "bg-destructive hover:bg-destructive/90"
                : "bg-primary hover:bg-primary/90",
            )}
          >
            {mutation.isPending
              ? "Deleting…"
              : permanent
                ? "Delete forever"
                : "Mark as orphan"}
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
  onAllocateIp,
}: {
  subnet: Subnet;
  isSelected: boolean;
  onSelect: () => void;
  onDelete: () => void;
  onEdited: (updated: Subnet) => void;
  onAllocateIp?: (s: Subnet) => void;
}) {
  const [showEdit, setShowEdit] = useState(false);
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `subnet:${subnet.id}`,
    data: { kind: "subnet", subnet },
  });

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          ref={setNodeRef}
          {...attributes}
          {...listeners}
          onClick={onSelect}
          className={cn(
            "group flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1.5 text-sm",
            isSelected
              ? "bg-primary/10 text-primary font-medium"
              : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
            isDragging && "opacity-40",
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
              <span className="truncate text-xs text-muted-foreground/60">
                {subnet.name}
              </span>
            )}
          </div>
          <UtilizationDot percent={subnet.utilization_percent} />
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuLabel>{subnet.network}</ContextMenuLabel>
        <ContextMenuSeparator />
        {onAllocateIp && (
          <ContextMenuItem onSelect={() => onAllocateIp(subnet)}>
            Allocate IP
          </ContextMenuItem>
        )}
        <ContextMenuItem onSelect={() => setShowEdit(true)}>
          Edit…
        </ContextMenuItem>
        <ContextMenuItem destructive onSelect={() => onDelete()}>
          Delete…
        </ContextMenuItem>
      </ContextMenuContent>
      {showEdit && (
        <EditSubnetModal
          subnet={subnet}
          onClose={(updated) => {
            setShowEdit(false);
            if (updated) onEdited(updated);
          }}
        />
      )}
    </ContextMenu>
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

/** Delete-an-allocated-IP flow: two choices in one modal — mark as orphan
 * (amber, reversible; DNS / DHCP cascades still happen but the IPAM row
 * stays so the address can be restored) or permanently delete (red, irreversible).
 * The two colors distinguish the tradeoff at a glance. */
function DeleteOrOrphanModal({
  address,
  onOrphan,
  onPurge,
  onClose,
  isOrphanPending,
  isPurgePending,
}: {
  address: IPAddress;
  onOrphan: () => void;
  onPurge: () => void;
  onClose: () => void;
  isOrphanPending?: boolean;
  isPurgePending?: boolean;
}) {
  return (
    <Modal title="Delete IP Address" onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-muted-foreground">
          What should happen to{" "}
          <span className="font-mono font-medium">{address.address}</span>?
        </p>
        <div className="rounded-md border border-amber-500/40 bg-amber-500/5 p-3">
          <div className="text-xs font-medium text-amber-700 dark:text-amber-400">
            Mark as Orphan (reversible)
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            The row is kept but marked <code>orphan</code>, greyed out in the
            list, and excluded from next-free allocation. DNS and DHCP cascades
            still run. You can restore or permanently delete it later from the
            orphans view.
          </p>
        </div>
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3">
          <div className="text-xs font-medium text-destructive">
            Delete Permanently (irreversible)
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            The IPAM row is removed immediately. DNS records and DHCP static
            assignments tied to this IP are also cascaded. There's no undo —
            skip the orphan state entirely.
          </p>
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={onPurge}
            disabled={isOrphanPending || isPurgePending}
            className="rounded-md bg-destructive px-3 py-1.5 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
          >
            {isPurgePending ? "…" : "Delete Permanently"}
          </button>
          <button
            onClick={onOrphan}
            disabled={isOrphanPending || isPurgePending}
            className="rounded-md bg-amber-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-600 disabled:opacity-50 dark:bg-amber-600 dark:hover:bg-amber-700"
          >
            {isOrphanPending ? "…" : "Mark as Orphan"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** Two-step destruction modal: step 1 confirms intent, step 2 requires checkbox. */
function ConfirmDestroyModal({
  title,
  description,
  checkLabel,
  onConfirm,
  onClose,
  isPending,
  error,
}: {
  title: string;
  description: string;
  checkLabel: string;
  onConfirm: () => void;
  onClose: () => void;
  isPending?: boolean;
  error?: string | null;
}) {
  const [step, setStep] = useState<1 | 2>(1);
  const [checked, setChecked] = useState(false);

  if (step === 1) {
    return (
      <Modal title={title} onClose={onClose}>
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">{description}</p>
          {error && (
            <div className="max-h-48 overflow-auto whitespace-pre-line rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          <div className="flex justify-end gap-2">
            <button
              onClick={onClose}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              Cancel
            </button>
            <button
              onClick={() => setStep(2)}
              className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90"
            >
              Continue
            </button>
          </div>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="Confirm Permanent Deletion" onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm font-medium text-destructive">
          This action cannot be undone.
        </p>
        <p className="text-sm text-muted-foreground">{description}</p>
        <label className="flex cursor-pointer items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={checked}
            onChange={() => setChecked(!checked)}
          />
          {checkLabel}
        </label>
        {error && (
          <div className="max-h-48 overflow-auto whitespace-pre-line rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
            {error}
          </div>
        )}
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={!checked || isPending}
            className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
          >
            {isPending ? "Deleting…" : "Delete permanently"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── VRF / BGP badges in the space detail header ────────────────────────────

/** Resolves ``space.vrf_id`` against the VRF list (cached) and renders the
 * linked VRF's name + RD + RTs as badges. Falls back to the legacy freeform
 * ``vrf_name`` / ``route_distinguisher`` / ``route_targets`` columns for
 * backward-compat with rows created before issue #86 phase 1. */
function SpaceVrfBadges({
  space,
  onEdit,
}: {
  space: IPSpace;
  onEdit: () => void;
}) {
  const { data: vrfs } = useQuery({
    queryKey: ["vrfs-picker"],
    queryFn: () => vrfsApi.list(),
    staleTime: 60_000,
  });
  const linkedVrf = space.vrf_id
    ? (vrfs ?? []).find((v) => v.id === space.vrf_id)
    : null;

  const vrfName = linkedVrf?.name ?? space.vrf_name ?? null;
  const rd =
    linkedVrf?.route_distinguisher ?? space.route_distinguisher ?? null;
  const importTargets = linkedVrf?.import_targets ?? null;
  const exportTargets = linkedVrf?.export_targets ?? null;
  const legacyTargets = !linkedVrf ? (space.route_targets ?? null) : null;

  const hasAny =
    vrfName ||
    rd ||
    space.asn_id ||
    (importTargets && importTargets.length > 0) ||
    (exportTargets && exportTargets.length > 0) ||
    (legacyTargets && legacyTargets.length > 0);

  if (!hasAny) {
    return (
      <p className="mt-1 text-xs text-muted-foreground/50">
        VRF / BGP — not configured{" "}
        <button
          type="button"
          onClick={onEdit}
          className="underline hover:text-muted-foreground"
        >
          (Edit Space to add)
        </button>
      </p>
    );
  }

  return (
    <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      {vrfName && (
        <span className="rounded border px-1.5 py-0.5 font-mono">
          VRF: {vrfName}
          {!linkedVrf && space.vrf_name ? " (legacy)" : ""}
        </span>
      )}
      {rd && (
        <span className="rounded border px-1.5 py-0.5 font-mono">RD: {rd}</span>
      )}
      {importTargets && importTargets.length > 0 && (
        <span className="rounded border px-1.5 py-0.5 font-mono">
          import: {importTargets.join(", ")}
        </span>
      )}
      {exportTargets && exportTargets.length > 0 && (
        <span className="rounded border px-1.5 py-0.5 font-mono">
          export: {exportTargets.join(", ")}
        </span>
      )}
      {legacyTargets && legacyTargets.length > 0 && (
        <span className="rounded border border-amber-500/40 bg-amber-500/5 px-1.5 py-0.5 font-mono text-amber-700 dark:text-amber-400">
          RT (legacy): {legacyTargets.join(", ")}
        </span>
      )}
      {space.asn_id && <SpaceAsnBadge asnId={space.asn_id} />}
    </div>
  );
}

function SpaceAsnBadge({ asnId }: { asnId: string }) {
  const { data } = useQuery({
    queryKey: ["asns-picker"],
    queryFn: () => asnsApi.list({ limit: 500 }),
    staleTime: 60_000,
  });
  const asn = (data?.items ?? []).find((a) => a.id === asnId);
  if (!asn) return null;
  return (
    <span className="rounded border px-1.5 py-0.5 font-mono">
      AS{asn.number}
      {asn.name ? ` — ${asn.name}` : ""}
    </span>
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
  const [color, setColor] = useState<string | null>(space.color ?? null);
  const [deleteStep, setDeleteStep] = useState<0 | 1 | 2>(0);
  const [deleteChecked, setDeleteChecked] = useState(false);

  // DNS state — space is the top-level source; no inherit toggle needed
  const [dnsGroupIds, setDnsGroupIds] = useState<string[]>(
    space.dns_group_ids ?? [],
  );
  const [dnsZoneId, setDnsZoneId] = useState<string | null>(
    space.dns_zone_id ?? null,
  );
  const [dnsAdditionalZoneIds, setDnsAdditionalZoneIds] = useState<string[]>(
    space.dns_additional_zone_ids ?? [],
  );
  const [dhcpServerGroupId, setDhcpServerGroupId] = useState<string | null>(
    space.dhcp_server_group_id ?? null,
  );
  // VRF / BGP annotation — pure metadata, no semantic effect on
  // address allocation. The collapsible section keeps the modal tidy
  // for operators who don't run multiple VRFs. Legacy freeform values
  // (vrf_name / RD / RTs) are migrated forward to first-class VRF
  // entities — see issue #86 phase 1; the picker below points at one.
  const [showVrf, setShowVrf] = useState<boolean>(true);
  const [vrfId, setVrfId] = useState<string | null>(space.vrf_id ?? null);
  const [asnId, setAsnId] = useState<string | null>(space.asn_id ?? null);

  const saveMutation = useMutation({
    mutationFn: () => {
      return ipamApi.updateSpace(space.id, {
        name,
        description,
        color,
        dns_group_ids: dnsGroupIds,
        dns_zone_id: dnsZoneId,
        dns_additional_zone_ids: dnsAdditionalZoneIds,
        dhcp_server_group_id: dhcpServerGroupId,
        vrf_id: vrfId,
        asn_id: asnId,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["spaces"] });
      onClose();
    },
  });

  const [deleteError, setDeleteError] = useState<string | null>(null);
  const deleteMutation = useMutation({
    mutationFn: () => ipamApi.deleteSpace(space.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["spaces"] });
      onDeleted();
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      setDeleteError(
        typeof detail === "string"
          ? detail
          : detail
            ? JSON.stringify(detail)
            : "Failed to delete IP space.",
      );
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
      <Modal
        title="Confirm Permanent Deletion"
        onClose={() => setDeleteStep(0)}
      >
        <div className="space-y-4">
          <p className="text-sm font-medium text-destructive">
            This action cannot be undone.
          </p>
          <p className="text-sm text-muted-foreground">
            All subnets and IP address records in{" "}
            <strong className="text-foreground">{space.name}</strong> will be
            permanently removed from the database.
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
          {deleteError && (
            <p className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {deleteError}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setDeleteStep(0)}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              Cancel
            </button>
            <button
              onClick={() => {
                setDeleteError(null);
                deleteMutation.mutate();
              }}
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
    <Modal title="Edit IP Space" onClose={onClose} wide>
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
        <Field label="Color">
          <SwatchPicker value={color} onChange={setColor} />
        </Field>

        {/* DNS defaults — propagate down to blocks/subnets that inherit */}
        <div className="border-t pt-3">
          <p className="mb-2 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
            DNS Defaults (inherited by child blocks and subnets)
          </p>
          <DnsSettingsSection
            inherit={false}
            hideInheritToggle
            groupIds={dnsGroupIds}
            zoneId={dnsZoneId}
            additionalZoneIds={dnsAdditionalZoneIds}
            onInheritChange={() => {}}
            onGroupIdsChange={setDnsGroupIds}
            onZoneIdChange={setDnsZoneId}
            onAdditionalZoneIdsChange={setDnsAdditionalZoneIds}
          />
        </div>

        {/* DHCP defaults — inherited by child blocks / subnets */}
        <div className="border-t pt-3">
          <p className="mb-2 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
            DHCP Defaults (inherited by child blocks and subnets)
          </p>
          <DhcpSettingsSection
            inherit={false}
            hideInheritToggle
            serverGroupId={dhcpServerGroupId}
            onInheritChange={() => {}}
            onServerGroupIdChange={setDhcpServerGroupId}
          />
        </div>

        {/* VRF / BGP annotation — collapsible because most homelab and
            small deployments don't run a multi-VRF fabric. */}
        <div className="border-t pt-3">
          <button
            type="button"
            onClick={() => setShowVrf((s) => !s)}
            className="flex items-center gap-1 text-xs font-semibold text-muted-foreground uppercase tracking-wide hover:text-foreground"
          >
            {showVrf ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            VRF / BGP (optional)
          </button>
          {showVrf && (
            <div className="mt-2 space-y-2">
              <Field label="VRF">
                <VrfPicker
                  className={inputCls}
                  value={vrfId}
                  onChange={setVrfId}
                />
                <p className="mt-1 text-[11px] text-muted-foreground">
                  Manage VRFs (RD + import / export RTs) under{" "}
                  <a
                    href="/network/vrfs"
                    className="underline hover:text-foreground"
                  >
                    Network → VRFs
                  </a>
                  .
                </p>
                {space.vrf_name && !vrfId && (
                  <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
                    Legacy freeform VRF: <code>{space.vrf_name}</code>
                    {space.route_distinguisher
                      ? ` (RD ${space.route_distinguisher})`
                      : ""}{" "}
                    — pick a first-class VRF above to migrate.
                  </p>
                )}
              </Field>
              <Field label="Origin ASN (BGP)">
                <AsnPicker
                  className={inputCls}
                  value={asnId}
                  onChange={setAsnId}
                />
              </Field>
              <p className="text-xs text-muted-foreground">
                Pure annotation — address allocation does not consult these
                fields. Different VRFs with overlapping IPs already work via
                separate IPSpace rows.
              </p>
            </div>
          )}
        </div>

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

function buildBlockTree(
  blocks: IPBlock[],
  subnets: Subnet[],
  parentId: string | null,
): BlockNode[] {
  return blocks
    .filter((b) => b.parent_block_id === parentId)
    .map((b) => ({
      block: b,
      children: buildBlockTree(blocks, subnets, b.id),
      subnets: subnets
        .filter((s) => s.block_id === b.id)
        .slice()
        .sort((x, y) => compareNetwork(String(x.network), String(y.network))),
    }))
    .sort((a, b) =>
      compareNetwork(String(a.block.network), String(b.block.network)),
    );
}

/**
 * Merge a node's child blocks and direct subnets into a single list
 * sorted by network address. Keeps the tree reading in sequential
 * order — a supernet block with subnets nested inside it lands next
 * to its IP-adjacent peers rather than being bucketed to the top or
 * bottom of the sibling list.
 */
type TreeItem =
  | { kind: "block"; node: BlockNode; key: string }
  | { kind: "subnet"; subnet: Subnet; key: string };

function sortedTreeItems(node: BlockNode): TreeItem[] {
  const items: TreeItem[] = [
    ...node.children.map(
      (c): TreeItem => ({
        kind: "block",
        node: c,
        key: String(c.block.network),
      }),
    ),
    ...node.subnets.map(
      (s): TreeItem => ({
        kind: "subnet",
        subnet: s,
        key: String(s.network),
      }),
    ),
  ];
  items.sort((a, b) => compareNetwork(a.key, b.key));
  return items;
}

// Flatten blocks into an indented label list for dropdowns
function flattenBlocks(
  nodes: BlockNode[],
  depth = 0,
): { id: string; label: string }[] {
  return nodes.flatMap(({ block, children }) => [
    {
      id: block.id,
      label: `${"  ".repeat(depth)}${block.network}${block.name ? ` — ${block.name}` : ""}`,
    },
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
  const [description, setDescription] = useState("");
  const [parentBlockId, setParentBlockId] = useState(
    defaultParentBlockId ?? "",
  );
  const [customFields, setCustomFields] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);

  // DNS state
  const [dnsInherit, setDnsInherit] = useState(true);
  const [dnsGroupIds, setDnsGroupIds] = useState<string[]>([]);
  const [dnsZoneId, setDnsZoneId] = useState<string | null>(null);
  const [dnsAdditionalZoneIds, setDnsAdditionalZoneIds] = useState<string[]>(
    [],
  );
  // DHCP state
  const [dhcpInherit, setDhcpInherit] = useState(true);
  const [dhcpServerGroupId, setDhcpServerGroupId] = useState<string | null>(
    null,
  );
  const [asnId, setAsnId] = useState<string | null>(null);
  const [vrfId, setVrfId] = useState<string | null>(null);
  // Optional template pre-fill (issue #26).
  const [templateId, setTemplateId] = useState<string>("");

  const { data: existingBlocks } = useQuery({
    queryKey: ["blocks", spaceId],
    queryFn: () => ipamApi.listBlocks(spaceId),
  });

  const { data: cfDefs = [] } = useQuery({
    queryKey: ["custom-fields", "ip_block"],
    queryFn: () => customFieldsApi.list("ip_block"),
  });

  const { data: blockTemplates } = useQuery({
    queryKey: ["ipam-templates", "block"],
    queryFn: () => ipamApi.listTemplates({ applies_to: "block" }),
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
        description: description || undefined,
        parent_block_id: parentBlockId || undefined,
        custom_fields: customFields,
        dns_inherit_settings: dnsInherit,
        ...(dnsInherit
          ? {}
          : {
              dns_group_ids: dnsGroupIds,
              dns_zone_id: dnsZoneId,
              dns_additional_zone_ids: dnsAdditionalZoneIds,
            }),
        dhcp_inherit_settings: dhcpInherit,
        ...(dhcpInherit ? {} : { dhcp_server_group_id: dhcpServerGroupId }),
        asn_id: asnId,
        vrf_id: vrfId,
        ...(templateId ? { template_id: templateId } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["blocks", spaceId] });
      qc.invalidateQueries({ queryKey: ["blocks"] });
      onClose();
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to create block";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  return (
    <Modal title="New IP Block" onClose={onClose}>
      <div className="space-y-3">
        {(blockTemplates ?? []).length > 0 && (
          <Field label="Apply template (optional)">
            <select
              className={inputCls}
              value={templateId}
              onChange={(e) => setTemplateId(e.target.value)}
            >
              <option value="">— none —</option>
              {(blockTemplates ?? []).map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                  {t.description ? ` — ${t.description}` : ""}
                </option>
              ))}
            </select>
            {templateId && (
              <p className="mt-1 text-xs text-muted-foreground">
                Operator-supplied fields below override the template's defaults.
                Children defined in the template are carved automatically.
              </p>
            )}
          </Field>
        )}
        <Field label="Network (CIDR)">
          <input
            className={inputCls}
            value={network}
            onChange={(e) => {
              setNetwork(e.target.value);
              setError(null);
            }}
            placeholder="e.g. 10.0.0.0/8 or 2001:db8::/32"
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
        <Field label="Description">
          <input
            className={inputCls}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional"
          />
        </Field>
        {flatBlocks.length > 0 && (
          <Field label="Parent Block (optional)">
            <select
              className={inputCls}
              value={parentBlockId}
              onChange={(e) => setParentBlockId(e.target.value)}
            >
              <option value="">— None (top-level) —</option>
              {flatBlocks.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.label}
                </option>
              ))}
            </select>
          </Field>
        )}
        <CustomFieldsSection
          definitions={cfDefs}
          values={customFields}
          onChange={(k, v) => setCustomFields((prev) => ({ ...prev, [k]: v }))}
        />
        <div className="border-t pt-3">
          <DnsSettingsSection
            inherit={dnsInherit}
            groupIds={dnsGroupIds}
            zoneId={dnsZoneId}
            additionalZoneIds={dnsAdditionalZoneIds}
            onInheritChange={setDnsInherit}
            onGroupIdsChange={setDnsGroupIds}
            onZoneIdChange={setDnsZoneId}
            onAdditionalZoneIdsChange={setDnsAdditionalZoneIds}
            parentBlockId={parentBlockId || null}
            fallbackSpaceId={!parentBlockId ? spaceId : null}
          />
        </div>
        <div className="border-t pt-3">
          <DhcpSettingsSection
            inherit={dhcpInherit}
            serverGroupId={dhcpServerGroupId}
            onInheritChange={setDhcpInherit}
            onServerGroupIdChange={setDhcpServerGroupId}
            parentBlockId={parentBlockId || null}
            fallbackSpaceId={!parentBlockId ? spaceId : null}
          />
        </div>
        <div className="border-t pt-3 space-y-3">
          <Field label="VRF (optional)">
            <VrfPicker className={inputCls} value={vrfId} onChange={setVrfId} />
            <p className="mt-1 text-[11px] text-muted-foreground">
              Pin a different VRF than the parent space when this block lives in
              a separate routing context (e.g. hub-and-spoke fabrics). Leave
              blank to inherit from the space.
            </p>
          </Field>
          <Field label="Origin ASN (BGP, optional)">
            <AsnPicker className={inputCls} value={asnId} onChange={setAsnId} />
          </Field>
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              setError(null);
              mutation.mutate();
            }}
            disabled={!network || mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Creating…" : "Create Block"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Edit Block Modal ─────────────────────────────────────────────────────────

function EditBlockModal({
  block,
  onClose,
  onDeleted,
}: {
  block: IPBlock;
  onClose: (updated?: IPBlock) => void;
  onDeleted?: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(block.name ?? "");
  const [description, setDescription] = useState(block.description ?? "");
  const [customFields, setCustomFields] = useState<Record<string, unknown>>(
    (block.custom_fields as Record<string, unknown>) ?? {},
  );
  const [error, setError] = useState<string | null>(null);
  const [deleteStep, setDeleteStep] = useState<0 | 1 | 2>(0);
  const [deleteChecked, setDeleteChecked] = useState(false);

  // DNS state — initialized from block
  const [dnsInherit, setDnsInherit] = useState(
    block.dns_inherit_settings ?? true,
  );
  const [dnsGroupIds, setDnsGroupIds] = useState<string[]>(
    block.dns_group_ids ?? [],
  );
  const [dnsZoneId, setDnsZoneId] = useState<string | null>(
    block.dns_zone_id ?? null,
  );
  const [dnsAdditionalZoneIds, setDnsAdditionalZoneIds] = useState<string[]>(
    block.dns_additional_zone_ids ?? [],
  );
  // DHCP state — initialized from block
  const [dhcpInherit, setDhcpInherit] = useState(
    block.dhcp_inherit_settings ?? true,
  );
  const [dhcpServerGroupId, setDhcpServerGroupId] = useState<string | null>(
    block.dhcp_server_group_id ?? null,
  );
  const [asnId, setAsnId] = useState<string | null>(block.asn_id ?? null);
  const [vrfId, setVrfId] = useState<string | null>(block.vrf_id ?? null);

  const { data: cfDefs = [] } = useQuery({
    queryKey: ["custom-fields", "ip_block"],
    queryFn: () => customFieldsApi.list("ip_block"),
  });

  // Effective (inherited) custom_fields from parent block(s) + space.
  const { data: effectiveFields } = useQuery({
    queryKey: ["block-effective-fields", block.id],
    queryFn: () => ipamApi.effectiveBlockFields(block.id),
    staleTime: 30_000,
  });
  const { data: allSpaces = [] } = useQuery({
    queryKey: ["spaces"],
    queryFn: ipamApi.listSpaces,
    staleTime: 60_000,
  });
  const { data: allBlocksForLabels = [] } = useQuery({
    queryKey: ["blocks"],
    queryFn: () => ipamApi.listBlocks(),
    staleTime: 60_000,
  });
  const cfInheritedLabels: Record<string, string> = {};
  const cfInheritedValues: Record<string, unknown> = {};
  if (effectiveFields) {
    for (const [key, src] of Object.entries(
      effectiveFields.custom_field_sources,
    )) {
      // The endpoint includes the block itself in the chain; skip those —
      // we only want ancestor-sourced values to surface as placeholders.
      if (src === `block:${block.id}`) continue;
      cfInheritedValues[key] = effectiveFields.custom_fields[key];
      if (src.startsWith("block:")) {
        const bid = src.slice("block:".length);
        const b = allBlocksForLabels.find((x) => x.id === bid);
        cfInheritedLabels[key] = b?.name
          ? `block ${b.name}`
          : b?.network
            ? `block ${b.network}`
            : "parent block";
      } else if (src.startsWith("space:")) {
        const sid = src.slice("space:".length);
        const s = allSpaces.find((x) => x.id === sid);
        cfInheritedLabels[key] = s?.name ? `IP Space ${s.name}` : "IP Space";
      } else {
        cfInheritedLabels[key] = src;
      }
    }
  }

  const mutation = useMutation({
    mutationFn: () =>
      ipamApi.updateBlock(block.id, {
        name: name || undefined,
        description,
        custom_fields: customFields,
        dns_inherit_settings: dnsInherit,
        dns_group_ids: dnsInherit ? null : dnsGroupIds,
        dns_zone_id: dnsInherit ? null : dnsZoneId,
        dns_additional_zone_ids: dnsInherit ? null : dnsAdditionalZoneIds,
        dhcp_inherit_settings: dhcpInherit,
        dhcp_server_group_id: dhcpInherit ? null : dhcpServerGroupId,
        asn_id: asnId,
        vrf_id: vrfId,
      }),
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ["blocks", block.space_id] });
      qc.invalidateQueries({ queryKey: ["blocks"] });
      onClose(updated);
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to save";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  const [deleteError, setDeleteError] = useState<string | null>(null);
  const deleteMutation = useMutation({
    mutationFn: () => ipamApi.deleteBlock(block.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["blocks", block.space_id] });
      qc.invalidateQueries({ queryKey: ["blocks"] });
      onDeleted?.();
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      setDeleteError(
        typeof detail === "string"
          ? detail
          : detail
            ? JSON.stringify(detail)
            : "Failed to delete block.",
      );
    },
  });

  // ── Delete step 1 ──
  if (deleteStep === 1) {
    return (
      <Modal title="Delete Block" onClose={() => setDeleteStep(0)}>
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Are you sure you want to delete block{" "}
            <strong className="text-foreground font-mono">
              {block.network}
            </strong>
            {block.name ? ` (${block.name})` : ""}? This will permanently delete
            all subnets and IP addresses within it.
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

  // ── Delete step 2 ──
  if (deleteStep === 2) {
    return (
      <Modal
        title="Confirm Permanent Deletion"
        onClose={() => setDeleteStep(0)}
      >
        <div className="space-y-4">
          <p className="text-sm font-medium text-destructive">
            This action cannot be undone.
          </p>
          <p className="text-sm text-muted-foreground">
            All subnets and IP address records within{" "}
            <strong className="text-foreground font-mono">
              {block.network}
            </strong>{" "}
            will be permanently removed from the database.
          </p>
          <label className="flex cursor-pointer items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={deleteChecked}
              onChange={(e) => setDeleteChecked(e.target.checked)}
            />
            I understand this will permanently delete all data in this block.
          </label>
          {deleteError && (
            <p className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {deleteError}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setDeleteStep(0)}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              Cancel
            </button>
            <button
              onClick={() => {
                setDeleteError(null);
                deleteMutation.mutate();
              }}
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
    <Modal title={`Edit ${block.network}`} onClose={() => onClose()}>
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
        <CustomFieldsSection
          definitions={cfDefs}
          values={customFields}
          onChange={(k, v) => setCustomFields((prev) => ({ ...prev, [k]: v }))}
          inherited={cfInheritedValues}
          inheritedLabels={cfInheritedLabels}
        />
        <div className="border-t pt-3">
          <DnsSettingsSection
            inherit={dnsInherit}
            groupIds={dnsGroupIds}
            zoneId={dnsZoneId}
            additionalZoneIds={dnsAdditionalZoneIds}
            onInheritChange={setDnsInherit}
            onGroupIdsChange={setDnsGroupIds}
            onZoneIdChange={setDnsZoneId}
            onAdditionalZoneIdsChange={setDnsAdditionalZoneIds}
            parentBlockId={block.parent_block_id}
          />
        </div>
        <div className="border-t pt-3">
          <DhcpSettingsSection
            inherit={dhcpInherit}
            serverGroupId={dhcpServerGroupId}
            onInheritChange={setDhcpInherit}
            onServerGroupIdChange={setDhcpServerGroupId}
            parentBlockId={block.parent_block_id}
            fallbackSpaceId={
              !block.parent_block_id ? block.space_id : undefined
            }
          />
        </div>
        <div className="border-t pt-3 space-y-3">
          <Field label="VRF (optional)">
            <VrfPicker className={inputCls} value={vrfId} onChange={setVrfId} />
            <p className="mt-1 text-[11px] text-muted-foreground">
              Override the parent space's VRF when this block lives in a
              different routing context. Leave blank to inherit.
            </p>
          </Field>
          <Field label="Origin ASN (BGP, optional)">
            <AsnPicker className={inputCls} value={asnId} onChange={setAsnId} />
          </Field>
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={() => onClose()}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              setError(null);
              mutation.mutate();
            }}
            disabled={mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Saving…" : "Save"}
          </button>
        </div>
        <div className="mt-2 border-t pt-3">
          <button
            onClick={() => setDeleteStep(1)}
            className="text-xs text-destructive hover:underline"
          >
            Delete this block…
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
  onDeleteBlock,
  onEditBlock,
  onCreateSubnet,
  onCreateChildBlock,
  onAllocateIp,
  depth,
}: {
  node: BlockNode;
  selectedSubnetId: string | null;
  selectedBlockId: string | null;
  onSelectBlock: (b: IPBlock) => void;
  onSelectSubnet: (s: Subnet) => void;
  onDeleteSubnet: (s: Subnet) => void;
  onDeleteBlock?: (b: IPBlock) => void;
  onEditBlock?: (b: IPBlock) => void;
  onCreateSubnet: (blockId: string) => void;
  onCreateChildBlock: (parentBlockId: string) => void;
  onAllocateIp?: (s: Subnet) => void;
  depth: number;
}) {
  const [expanded, setExpanded] = useState(true);
  const hasContent = node.children.length > 0 || node.subnets.length > 0;
  const isSelected = selectedBlockId === node.block.id;
  const {
    attributes: dragAttrs,
    listeners: dragListeners,
    setNodeRef: setDragRef,
    isDragging,
  } = useDraggable({
    id: `block:${node.block.id}`,
    data: { kind: "block", block: node.block },
  });
  const { setNodeRef: setDropRef, isOver } = useDroppable({
    id: `block-drop:${node.block.id}`,
    data: { kind: "block", block: node.block },
  });

  const setRefs = (el: HTMLDivElement | null) => {
    setDragRef(el);
    setDropRef(el);
  };

  return (
    <div>
      {/* Block header row */}
      <ContextMenu>
        <ContextMenuTrigger asChild>
          <div
            ref={setRefs}
            {...dragAttrs}
            {...dragListeners}
            className={cn(
              "group flex items-center gap-1 rounded-md px-2 py-1 text-xs hover:bg-muted/30 cursor-pointer",
              isSelected && "bg-primary/10",
              isOver && "ring-1 ring-primary/60 bg-primary/5",
              isDragging && "opacity-40",
            )}
          >
            {/* [+] / [-] toggle box */}
            {hasContent ? (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  setExpanded((v) => !v);
                }}
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
                isSelected
                  ? "text-primary"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <Layers className="h-3 w-3 flex-shrink-0" />
              <span className="font-mono font-medium flex-1 truncate">
                {node.block.network}
              </span>
              {node.block.name && (
                <span className="truncate text-[10px] opacity-60 mr-1">
                  {node.block.name}
                </span>
              )}
            </button>
          </div>
        </ContextMenuTrigger>
        <ContextMenuContent>
          <ContextMenuLabel>{node.block.network}</ContextMenuLabel>
          <ContextMenuSeparator />
          <ContextMenuItem onSelect={() => onCreateChildBlock(node.block.id)}>
            New child block…
          </ContextMenuItem>
          <ContextMenuItem onSelect={() => onCreateSubnet(node.block.id)}>
            New subnet…
          </ContextMenuItem>
          <ContextMenuSeparator />
          {onEditBlock && (
            <ContextMenuItem onSelect={() => onEditBlock(node.block)}>
              Edit…
            </ContextMenuItem>
          )}
          {onDeleteBlock && (
            <ContextMenuItem
              destructive
              onSelect={() => onDeleteBlock(node.block)}
            >
              Delete…
            </ContextMenuItem>
          )}
        </ContextMenuContent>
      </ContextMenu>

      {/* Children with vertical tree line. Blocks and subnets are
          interleaved in a single sort by network address so the tree
          reads sequentially — a supernet block (e.g. 10.255.0.0/24)
          with subnets inside it lands alongside its IP-adjacent peers
          rather than being bucketed to the top or bottom. */}
      {expanded && hasContent && (
        <div className="ml-[9px] pl-3 border-l border-border/40 space-y-0.5">
          {sortedTreeItems(node).map((item) =>
            item.kind === "block" ? (
              <BlockTreeRow
                key={`b:${item.node.block.id}`}
                node={item.node}
                selectedSubnetId={selectedSubnetId}
                selectedBlockId={selectedBlockId}
                onSelectBlock={onSelectBlock}
                onSelectSubnet={onSelectSubnet}
                onDeleteSubnet={onDeleteSubnet}
                onDeleteBlock={onDeleteBlock}
                onEditBlock={onEditBlock}
                onCreateSubnet={onCreateSubnet}
                onCreateChildBlock={onCreateChildBlock}
                onAllocateIp={onAllocateIp}
                depth={depth + 1}
              />
            ) : (
              <SubnetRow
                key={`s:${item.subnet.id}`}
                subnet={item.subnet}
                isSelected={selectedSubnetId === item.subnet.id}
                onSelect={() => onSelectSubnet(item.subnet)}
                onDelete={() => onDeleteSubnet(item.subnet)}
                onEdited={(updated) => onSelectSubnet(updated)}
                onAllocateIp={onAllocateIp}
              />
            ),
          )}
          {node.children.length === 0 && node.subnets.length === 0 && (
            <p className="py-0.5 pl-2 text-xs text-muted-foreground/40">
              Empty
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Block Detail View (right panel when block is selected) ──────────────────

function BlockDetailView({
  block: initialBlock,
  spaceName,
  ancestors,
  allBlocks,
  allSubnets,
  space,
  onSelectSpace,
  onSelectBlock,
  onSelectSubnet,
}: {
  block: IPBlock;
  spaceName: string;
  ancestors: IPBlock[];
  allBlocks: IPBlock[];
  allSubnets: Subnet[];
  space?: IPSpace;
  onSelectSpace: () => void;
  onSelectBlock: (b: IPBlock) => void;
  onSelectSubnet: (s: Subnet) => void;
}) {
  const [block, setBlock] = useState(initialBlock);
  const [showEdit, setShowEdit] = useState(false);
  const [showResizeBlock, setShowResizeBlock] = useState(false);
  const [showMoveBlock, setShowMoveBlock] = useState(false);
  const [showCreateSubnet, setShowCreateSubnet] = useState(false);
  const [showCreateChildBlock, setShowCreateChildBlock] = useState(false);
  const [showPlanAllocation, setShowPlanAllocation] = useState(false);
  const [allocationView, setAllocationView] = useSessionState<
    "band" | "treemap"
  >(`block-${initialBlock.id}-alloc-view`, "band");
  const [blockFilter, setBlockFilter] = useState({
    network: "",
    name: "",
    router: "",
    vlan: "",
    status: "",
  });
  const [showBlockFilters, setShowBlockFilters] = useState(false);
  // Unified selection set with ``subnet:<id>`` / ``block:<id>`` keys —
  // mirrors the space-level view so a single bulk action can delete a
  // mixed set of subnets + empty leaf blocks.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [showBulkEdit, setShowBulkEdit] = useState(false);
  const [showBulkDelete, setShowBulkDelete] = useState(false);
  const [showDnsSync, setShowDnsSync] = useState(false);
  const [showFindFree, setShowFindFree] = useState(false);
  const [showSplitSubnet, setShowSplitSubnet] = useState(false);
  const [showMergeSubnet, setShowMergeSubnet] = useState(false);

  const qc = useQueryClient();

  const [blockBulkDeleteError, setBlockBulkDeleteError] = useState<
    string | null
  >(null);
  const blockBulkDeleteMut = useMutation({
    // allSettled on both phases so a single 409 (non-empty subnet/block)
    // doesn't hide the rest. Subnets first — a subnet hanging off a leaf
    // block would otherwise trip the block's RESTRICT FK on cascade.
    mutationFn: async () => {
      const subnetIds: string[] = [];
      const blockIds: string[] = [];
      for (const key of selected) {
        if (key.startsWith("subnet:"))
          subnetIds.push(key.slice("subnet:".length));
        else if (key.startsWith("block:"))
          blockIds.push(key.slice("block:".length));
      }
      const subnetResults = await Promise.allSettled(
        subnetIds.map((id) => ipamApi.deleteSubnet(id, true)),
      );
      const blockResults = await Promise.allSettled(
        blockIds.map((id) => ipamApi.deleteBlock(id)),
      );
      type Fail = { id: string; kind: "subnet" | "block"; message: string };
      const failures: Fail[] = [];
      const collect = (
        ids: string[],
        results: PromiseSettledResult<unknown>[],
        kind: "subnet" | "block",
      ) => {
        ids.forEach((id, i) => {
          const r = results[i];
          if (r.status === "rejected") {
            const reason = r.reason as
              | { response?: { data?: { detail?: unknown } } }
              | undefined;
            const detail = reason?.response?.data?.detail;
            failures.push({
              id,
              kind,
              message:
                typeof detail === "string"
                  ? detail
                  : detail
                    ? JSON.stringify(detail)
                    : "Unknown error",
            });
          }
        });
      };
      collect(subnetIds, subnetResults, "subnet");
      collect(blockIds, blockResults, "block");
      return { failures, total: subnetIds.length + blockIds.length };
    },
    onSuccess: ({ failures, total }) => {
      qc.invalidateQueries({ queryKey: ["subnets", block.space_id] });
      qc.invalidateQueries({ queryKey: ["blocks", block.space_id] });
      if (failures.length === 0) {
        setSelected(new Set());
        setShowBulkDelete(false);
        setBlockBulkDeleteError(null);
        return;
      }
      const subnetLookup = new Map(allSubnets.map((s) => [s.id, s.network]));
      const blockLookup = new Map(allBlocks.map((b) => [b.id, b.network]));
      const detail = failures
        .map((f) => {
          const lookup = f.kind === "subnet" ? subnetLookup : blockLookup;
          return `• ${f.kind} ${lookup.get(f.id) ?? f.id}: ${f.message}`;
        })
        .join("\n");
      setBlockBulkDeleteError(
        `${failures.length} of ${total} items could not be deleted:\n${detail}`,
      );
    },
  });

  // Sync if parent passes a new block object (e.g. after deep-link navigation)
  useEffect(() => {
    setBlock(initialBlock);
  }, [initialBlock.id]);

  const { data: subnetCfDefs = [] } = useQuery({
    queryKey: ["custom-fields", "subnet"],
    queryFn: () => customFieldsApi.list("subnet"),
  });

  const directSubnets = allSubnets.filter((s) => s.block_id === block.id);
  const directChildBlocks = allBlocks.filter(
    (b) => b.parent_block_id === block.id,
  );

  // Leaf-empty blocks (no child blocks AND no child subnets) get a checkbox.
  // Anything else is hidden behind a placeholder cell — deleting a non-leaf
  // would either cascade unpredictably or hit the FK RESTRICT.
  const leafBlockIds = new Set<string>();
  {
    const subnetBlockParents = new Set(allSubnets.map((s) => s.block_id));
    const blockParents = new Set(
      allBlocks.map((b) => b.parent_block_id).filter((p): p is string => !!p),
    );
    for (const b of allBlocks) {
      if (!subnetBlockParents.has(b.id) && !blockParents.has(b.id)) {
        leafBlockIds.add(b.id);
      }
    }
  }

  const selectedSubnetIds = [...selected]
    .filter((k) => k.startsWith("subnet:"))
    .map((k) => k.slice("subnet:".length));
  const selectedBlockKeys = [...selected].filter((k) => k.startsWith("block:"));
  const hasBlocksSelected = selectedBlockKeys.length > 0;
  const selectedCount = selected.size;
  const toggleOne = (key: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const [freeRangePreset, setFreeRangePreset] = useState<FreeCidrRange | null>(
    null,
  );

  const crumbs: BreadcrumbItem[] = [
    { label: spaceName, variant: "space", onClick: onSelectSpace },
    ...ancestors.map(
      (a): BreadcrumbItem => ({
        label: a.network + (a.name ? ` (${a.name})` : ""),
        variant: "block",
        onClick: () => onSelectBlock(a),
      }),
    ),
    {
      label: block.network + (block.name ? ` (${block.name})` : ""),
      variant: "block",
    },
  ];

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="border-b">
        {/* Top bar */}
        <div className="flex items-center justify-between gap-4 px-6 pt-3 pb-2">
          <BreadcrumbPills items={crumbs} />
          <div className="flex flex-shrink-0 items-center gap-2">
            {selectedCount > 0 ? (
              <>
                {!hasBlocksSelected && (
                  <HeaderButton
                    icon={Pencil}
                    onClick={() => setShowBulkEdit(true)}
                  >
                    Bulk Edit ({selectedSubnetIds.length})
                  </HeaderButton>
                )}
                {!hasBlocksSelected && selectedSubnetIds.length === 1 && (
                  <HeaderButton onClick={() => setShowSplitSubnet(true)}>
                    Split…
                  </HeaderButton>
                )}
                {!hasBlocksSelected && selectedSubnetIds.length >= 2 && (
                  <HeaderButton onClick={() => setShowMergeSubnet(true)}>
                    Merge…
                  </HeaderButton>
                )}
                <HeaderButton
                  variant="destructive"
                  icon={Trash2}
                  onClick={() => setShowBulkDelete(true)}
                >
                  Delete ({selectedCount})
                </HeaderButton>
                <HeaderButton onClick={() => setSelected(new Set())}>
                  Clear
                </HeaderButton>
              </>
            ) : (
              <>
                <HeaderButton
                  icon={Globe2}
                  onClick={() => setShowDnsSync(true)}
                  title="Reconcile IPAM-managed DNS records across every subnet under this block"
                >
                  Sync DNS
                </HeaderButton>
                <ExportButton scope={{ block_id: block.id }} label="Export" />
                {space && (
                  <HeaderButton
                    icon={Search}
                    onClick={() => setShowFindFree(true)}
                    title="Find unused CIDRs in this block"
                  >
                    Find Free…
                  </HeaderButton>
                )}
                <HeaderButton icon={Pencil} onClick={() => setShowEdit(true)}>
                  Edit
                </HeaderButton>
                <HeaderButton
                  onClick={() => setShowResizeBlock(true)}
                  title="Grow this block to a larger CIDR (e.g. /16 → /15). Shrinking is not supported."
                >
                  Resize…
                </HeaderButton>
                <HeaderButton
                  onClick={() => setShowMoveBlock(true)}
                  title="Move this block (and everything under it) to a different IP space."
                >
                  Move…
                </HeaderButton>
                <HeaderButton
                  icon={Layers}
                  onClick={() => setShowCreateChildBlock(true)}
                >
                  Add Block
                </HeaderButton>
                <HeaderButton
                  variant="primary"
                  icon={Plus}
                  onClick={() => setShowCreateSubnet(true)}
                >
                  New Subnet
                </HeaderButton>
              </>
            )}
          </div>
        </div>
        {/* Identity row */}
        <div className="flex items-center gap-3 px-6 pb-2">
          <Layers className="h-4 w-4 text-violet-500" />
          <span className="font-mono text-xl font-bold tracking-tight">
            {block.network}
          </span>
          {block.name && (
            <span className="text-sm text-muted-foreground">{block.name}</span>
          )}
          {block.description && (
            <span className="text-xs text-muted-foreground/70">
              · {block.description}
            </span>
          )}
        </div>
        {/* Custom field values */}
        {Object.keys(block.custom_fields ?? {}).length > 0 && (
          <div className="flex flex-wrap gap-x-6 gap-y-1 border-t bg-muted/20 px-6 py-2">
            {Object.entries(block.custom_fields).map(([k, v]) => (
              <div key={k} className="flex items-center gap-1.5">
                <span className="text-xs text-muted-foreground">{k}</span>
                <span className="text-xs font-medium">{String(v)}</span>
              </div>
            ))}
          </div>
        )}
        {/* Allocation map */}
        <div className="border-t px-6 py-2">
          <div className="mb-1 flex items-center justify-between">
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
              Allocation map
            </p>
            <div className="flex items-center gap-3">
              <div className="inline-flex overflow-hidden rounded border text-[10px]">
                <button
                  type="button"
                  onClick={() => setAllocationView("band")}
                  className={
                    allocationView === "band"
                      ? "bg-primary px-2 py-0.5 text-primary-foreground"
                      : "px-2 py-0.5 text-muted-foreground hover:bg-muted/50"
                  }
                >
                  Band
                </button>
                <button
                  type="button"
                  onClick={() => setAllocationView("treemap")}
                  className={
                    allocationView === "treemap"
                      ? "bg-primary px-2 py-0.5 text-primary-foreground"
                      : "px-2 py-0.5 text-muted-foreground hover:bg-muted/50"
                  }
                >
                  Treemap
                </button>
              </div>
              <button
                type="button"
                onClick={() => setShowPlanAllocation(true)}
                className="text-[10px] font-medium text-primary hover:underline"
              >
                Plan allocation…
              </button>
            </div>
          </div>
          {allocationView === "band" ? (
            <FreeSpaceBand
              block={block}
              directSubnets={directSubnets}
              childBlocks={directChildBlocks}
              onSelectFree={(range) => {
                setFreeRangePreset(range);
                setShowCreateSubnet(true);
              }}
            />
          ) : (
            <FreeSpaceTreemap
              block={block}
              directSubnets={directSubnets}
              childBlocks={directChildBlocks}
            />
          )}
        </div>
        <AggregationSuggestions blockId={block.id} />
      </div>
      {showPlanAllocation && (
        <PlanAllocationModal
          block={block}
          onClose={() => setShowPlanAllocation(false)}
        />
      )}
      {showEdit && (
        <EditBlockModal
          block={block}
          onClose={(updated) => {
            if (updated) setBlock(updated);
            setShowEdit(false);
          }}
          onDeleted={onSelectSpace}
        />
      )}
      {showResizeBlock && (
        <ResizeBlockModal
          block={block}
          onClose={() => setShowResizeBlock(false)}
          onCommitted={(result) => setBlock(result.block)}
        />
      )}
      {showMoveBlock && (
        <MoveBlockModal
          block={block}
          onClose={() => setShowMoveBlock(false)}
          onCommitted={(result) => {
            setBlock(result.block);
            setShowMoveBlock(false);
          }}
        />
      )}
      {showCreateSubnet && (
        <CreateSubnetModal
          spaceId={block.space_id}
          defaultBlockId={block.id}
          defaultNetwork={freeRangePreset?.network}
          onClose={() => {
            setShowCreateSubnet(false);
            setFreeRangePreset(null);
          }}
        />
      )}
      {showCreateChildBlock && (
        <CreateBlockModal
          spaceId={block.space_id}
          defaultParentBlockId={block.id}
          onClose={() => setShowCreateChildBlock(false)}
        />
      )}
      {showBulkEdit && (
        <BulkEditSubnetsModal
          subnetIds={selectedSubnetIds}
          onClose={() => setShowBulkEdit(false)}
          onDone={() => {
            setShowBulkEdit(false);
            setSelected(new Set());
          }}
        />
      )}
      {showDnsSync && (
        <DnsSyncModal
          scope={{
            kind: "block",
            id: block.id,
            label: block.network + (block.name ? ` (${block.name})` : ""),
          }}
          onClose={() => setShowDnsSync(false)}
        />
      )}
      {showBulkDelete &&
        (() => {
          const sCount = selectedSubnetIds.length;
          const bCount = selectedBlockKeys.length;
          const noun =
            sCount && bCount
              ? `${sCount} subnet${sCount === 1 ? "" : "s"} + ${bCount} block${bCount === 1 ? "" : "s"}`
              : sCount
                ? `${sCount} Subnet${sCount === 1 ? "" : "s"}`
                : `${bCount} empty Block${bCount === 1 ? "" : "s"}`;
          return (
            <ConfirmDestroyModal
              title={`Delete ${noun}`}
              description={
                sCount > 0
                  ? `This will move ${sCount} subnet${sCount === 1 ? "" : "s"} to Trash` +
                    (bCount > 0
                      ? ` and permanently delete ${bCount} empty block${bCount === 1 ? "" : "s"}.`
                      : ". You can restore from Admin → Trash within 30 days.")
                  : `This will permanently delete ${bCount} empty block${bCount === 1 ? "" : "s"}. Blocks are not restorable from Trash.`
              }
              checkLabel={`I understand ${noun} will be deleted.`}
              isPending={blockBulkDeleteMut.isPending}
              error={blockBulkDeleteError}
              onClose={() => {
                setShowBulkDelete(false);
                setBlockBulkDeleteError(null);
              }}
              onConfirm={() => {
                setBlockBulkDeleteError(null);
                blockBulkDeleteMut.mutate();
              }}
            />
          );
        })()}
      {showFindFree && space && (
        <FindFreeModal
          space={space}
          defaultBlockId={block.id}
          onClose={() => setShowFindFree(false)}
          onPickCidr={(cidr) => {
            setFreeRangePreset({
              network: cidr,
              first: "",
              last: "",
              size: 0,
              prefix_len: 0,
            });
            setShowFindFree(false);
            setShowCreateSubnet(true);
          }}
        />
      )}
      {showSplitSubnet &&
        (() => {
          const subnetId = selectedSubnetIds[0];
          const sn = allSubnets.find((s) => s.id === subnetId);
          return sn ? (
            <SplitSubnetModal
              subnet={sn}
              onClose={() => setShowSplitSubnet(false)}
              onCommitted={() => {
                setShowSplitSubnet(false);
                setSelected(new Set());
                qc.invalidateQueries({ queryKey: ["subnets"] });
              }}
            />
          ) : null;
        })()}
      {showMergeSubnet &&
        (() => {
          const subnetId = selectedSubnetIds[0];
          const sn = allSubnets.find((s) => s.id === subnetId);
          return sn ? (
            <MergeSubnetSiblingPicker
              subnet={sn}
              onClose={() => setShowMergeSubnet(false)}
              onCommitted={() => {
                setShowMergeSubnet(false);
                setSelected(new Set());
                qc.invalidateQueries({ queryKey: ["subnets"] });
              }}
            />
          ) : null;
        })()}
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
          let allRows = rawRows
            .slice(1)
            .map((r) => ({ ...r, depth: Math.max(0, r.depth) }));

          // Apply filters
          if (Object.values(blockFilter).some(Boolean)) {
            allRows = allRows.filter((r) => {
              if (r.type === "block" && r.block) {
                const b = r.block;
                if (
                  blockFilter.network &&
                  !b.network.includes(blockFilter.network)
                )
                  return false;
                if (
                  blockFilter.name &&
                  !(b.name ?? "")
                    .toLowerCase()
                    .includes(blockFilter.name.toLowerCase())
                )
                  return false;
                return true;
              }
              if (r.type === "subnet" && r.subnet) {
                const s = r.subnet;
                if (
                  blockFilter.network &&
                  !s.network.includes(blockFilter.network)
                )
                  return false;
                if (
                  blockFilter.name &&
                  !(s.name ?? "")
                    .toLowerCase()
                    .includes(blockFilter.name.toLowerCase())
                )
                  return false;
                if (
                  blockFilter.router &&
                  !(s.vlan?.router_name ?? "")
                    .toLowerCase()
                    .includes(blockFilter.router.toLowerCase())
                )
                  return false;
                if (
                  blockFilter.vlan &&
                  !(
                    String(s.vlan_id ?? "").includes(blockFilter.vlan) ||
                    (s.vlan?.name ?? "")
                      .toLowerCase()
                      .includes(blockFilter.vlan.toLowerCase())
                  )
                )
                  return false;
                if (blockFilter.status && s.status !== blockFilter.status)
                  return false;
                return true;
              }
              return true;
            });
          }

          if (allRows.length === 0) {
            return (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <Layers className="mb-3 h-10 w-10 text-muted-foreground/20" />
                <p className="text-sm text-muted-foreground">
                  {Object.values(blockFilter).some(Boolean)
                    ? "No results match the active filters."
                    : "This block has no child blocks or subnets yet."}
                </p>
              </div>
            );
          }

          return (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-sm">
                <thead>
                  <tr className="border-b bg-muted/40 text-xs">
                    <th className="w-8 px-2 py-2 text-left">
                      {(() => {
                        const selectableKeys = [
                          ...allRows
                            .filter((r) => r.type === "subnet" && r.subnet)
                            .map((r) => `subnet:${r.subnet!.id}`),
                          ...allRows
                            .filter(
                              (r) =>
                                r.type === "block" &&
                                r.block &&
                                leafBlockIds.has(r.block.id),
                            )
                            .map((r) => `block:${r.block!.id}`),
                        ];
                        const allSelected =
                          selectableKeys.length > 0 &&
                          selectableKeys.every((k) => selected.has(k));
                        return (
                          <input
                            type="checkbox"
                            aria-label="Select all selectable rows"
                            checked={allSelected}
                            disabled={selectableKeys.length === 0}
                            onChange={() =>
                              setSelected(
                                allSelected
                                  ? new Set()
                                  : new Set(selectableKeys),
                              )
                            }
                          />
                        );
                      })()}
                    </th>
                    {(
                      [
                        "Network",
                        "Name",
                        "Router",
                        "VLAN",
                        "Used IPs",
                        "Utilization",
                        "Size",
                        "Status",
                      ] as const
                    ).map((col) => {
                      const filterKey =
                        col === "Network"
                          ? "network"
                          : col === "Name"
                            ? "name"
                            : col === "Router"
                              ? "router"
                              : col === "VLAN"
                                ? "vlan"
                                : col === "Status"
                                  ? "status"
                                  : null;
                      const hasFilter = filterKey
                        ? !!blockFilter[filterKey as keyof typeof blockFilter]
                        : false;
                      const isFilterable = filterKey !== null;
                      return (
                        <th
                          key={col}
                          className={cn(
                            "px-4 py-2 font-medium text-muted-foreground",
                            col === "Size" ? "text-right" : "text-left",
                          )}
                        >
                          <span className="inline-flex items-center gap-1">
                            {col}
                            {isFilterable && (
                              <button
                                onClick={() => setShowBlockFilters((v) => !v)}
                                title={`Filter by ${col}`}
                                className={cn(
                                  "rounded p-0.5 hover:bg-accent",
                                  hasFilter
                                    ? "text-primary"
                                    : showBlockFilters ||
                                        Object.values(blockFilter).some(Boolean)
                                      ? "text-primary/50"
                                      : "text-muted-foreground/40 hover:text-muted-foreground",
                                )}
                              >
                                <Filter className="h-2.5 w-2.5" />
                              </button>
                            )}
                          </span>
                        </th>
                      );
                    })}
                    {subnetCfDefs.map((def) => (
                      <th
                        key={def.name}
                        className="px-4 py-2 text-left font-medium text-muted-foreground"
                      >
                        {def.label}
                      </th>
                    ))}
                    <th className="px-4 py-2 text-right">
                      {Object.values(blockFilter).some(Boolean) && (
                        <button
                          onClick={() =>
                            setBlockFilter({
                              network: "",
                              name: "",
                              router: "",
                              vlan: "",
                              status: "",
                            })
                          }
                          title="Clear all filters"
                          className="rounded p-0.5 text-primary hover:text-destructive"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      )}
                    </th>
                  </tr>
                  {showBlockFilters && (
                    <tr className="border-b bg-muted/10 text-xs">
                      <td />
                      {(
                        [
                          "Network",
                          "Name",
                          "Router",
                          "VLAN",
                          "Used IPs",
                          "Utilization",
                          "Size",
                          "Status",
                        ] as const
                      ).map((col) => {
                        const filterKey =
                          col === "Network"
                            ? "network"
                            : col === "Name"
                              ? "name"
                              : col === "Router"
                                ? "router"
                                : col === "VLAN"
                                  ? "vlan"
                                  : col === "Status"
                                    ? "status"
                                    : null;
                        if (!filterKey) return <td key={col} />;
                        if (filterKey === "status") {
                          return (
                            <td key={col} className="px-2 py-1">
                              <select
                                value={blockFilter.status}
                                onChange={(e) =>
                                  setBlockFilter((p) => ({
                                    ...p,
                                    status: e.target.value,
                                  }))
                                }
                                className="w-full rounded border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                              >
                                <option value="">All</option>
                                {[
                                  "active",
                                  "reserved",
                                  "deprecated",
                                  "quarantine",
                                ].map((s) => (
                                  <option key={s} value={s}>
                                    {s}
                                  </option>
                                ))}
                              </select>
                            </td>
                          );
                        }
                        return (
                          <td key={col} className="px-2 py-1">
                            <input
                              type="text"
                              value={
                                blockFilter[
                                  filterKey as keyof typeof blockFilter
                                ]
                              }
                              onChange={(e) =>
                                setBlockFilter((p) => ({
                                  ...p,
                                  [filterKey]: e.target.value,
                                }))
                              }
                              placeholder="Filter…"
                              className="w-full rounded border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                            />
                          </td>
                        );
                      })}
                      {subnetCfDefs.map((def) => (
                        <td key={def.name} />
                      ))}
                      <td />
                    </tr>
                  )}
                </thead>
                <tbody className={zebraBodyCls}>
                  {allRows.map((item) => {
                    const indent = item.depth * 20;
                    if (item.type === "block" && item.block) {
                      const b = item.block;
                      const isLeaf = leafBlockIds.has(b.id);
                      const blockKey = `block:${b.id}`;
                      return (
                        <tr
                          key={item.key}
                          onClick={() => onSelectBlock(b)}
                          className="border-b last:border-0 cursor-pointer hover:bg-muted/30 bg-muted/10"
                        >
                          <td
                            className="w-8 px-2 py-2"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {isLeaf ? (
                              <input
                                type="checkbox"
                                aria-label={`Select block ${b.network}`}
                                checked={selected.has(blockKey)}
                                onChange={() => toggleOne(blockKey)}
                              />
                            ) : (
                              <span
                                className="inline-block h-3.5 w-3.5"
                                title="Block has child blocks or subnets — delete those first"
                              />
                            )}
                          </td>
                          <td
                            className="py-2 pr-4"
                            style={{ paddingLeft: `${indent + 16}px` }}
                          >
                            <span className="inline-flex items-center gap-1.5 font-mono font-semibold text-foreground">
                              <Layers className="h-3.5 w-3.5 flex-shrink-0 text-violet-500" />
                              {b.network}
                            </span>
                          </td>
                          <td className="px-4 py-2 text-muted-foreground">
                            {b.name || (
                              <span className="text-muted-foreground/40">
                                —
                              </span>
                            )}
                          </td>
                          <td className="px-4 py-2 text-muted-foreground/40">
                            —
                          </td>
                          <td className="px-4 py-2 text-muted-foreground/40">
                            —
                          </td>
                          <td className="px-4 py-2 text-muted-foreground/40">
                            —
                          </td>
                          <td className="px-4 py-2">
                            {b.utilization_percent > 0 ? (
                              <UtilizationBar percent={b.utilization_percent} />
                            ) : (
                              <span className="text-muted-foreground/40">
                                —
                              </span>
                            )}
                          </td>
                          <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
                            {cidrSize(b.network).toLocaleString()}
                          </td>
                          <td className="px-4 py-2 text-muted-foreground/40">
                            —
                          </td>
                          {subnetCfDefs.map((def) => (
                            <td
                              key={def.name}
                              className="px-4 py-2 text-muted-foreground/40"
                            >
                              —
                            </td>
                          ))}
                          <td />
                        </tr>
                      );
                    }
                    if (item.type === "subnet" && item.subnet) {
                      const s = item.subnet;
                      return (
                        <tr
                          key={item.key}
                          onClick={() => onSelectSubnet(s)}
                          className={cn(
                            "border-b last:border-0 cursor-pointer hover:bg-muted/30",
                            selected.has(`subnet:${s.id}`) && "bg-primary/5",
                          )}
                        >
                          <td
                            className="w-8 px-2 py-2"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <input
                              type="checkbox"
                              aria-label={`Select ${s.network}`}
                              checked={selected.has(`subnet:${s.id}`)}
                              onChange={() => toggleOne(`subnet:${s.id}`)}
                            />
                          </td>
                          <td
                            className="py-2 pr-4"
                            style={{ paddingLeft: `${indent + 16}px` }}
                          >
                            <span className="inline-flex items-center gap-1.5 font-mono font-medium">
                              <Network className="h-3.5 w-3.5 flex-shrink-0 text-blue-500" />
                              {s.network}
                            </span>
                          </td>
                          <td className="px-4 py-2 text-muted-foreground">
                            {s.name || (
                              <span className="text-muted-foreground/40">
                                —
                              </span>
                            )}
                          </td>
                          <td className="px-4 py-2 text-muted-foreground">
                            {s.vlan?.router_name ?? (
                              <span className="text-muted-foreground/40">
                                —
                              </span>
                            )}
                          </td>
                          <td className="px-4 py-2 text-muted-foreground">
                            {s.vlan ? (
                              <span>
                                {s.vlan.vlan_id}
                                {s.vlan.name && (
                                  <span className="ml-1 text-muted-foreground/70">
                                    ({s.vlan.name})
                                  </span>
                                )}
                              </span>
                            ) : s.vlan_id != null ? (
                              <span
                                className="text-muted-foreground/70"
                                title="Legacy tag — assign a Router/VLAN from the Edit modal"
                              >
                                {s.vlan_id}
                              </span>
                            ) : (
                              <span className="text-muted-foreground/40">
                                —
                              </span>
                            )}
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
                          {subnetCfDefs.map((def) => (
                            <td
                              key={def.name}
                              className="px-4 py-2 text-muted-foreground"
                            >
                              {s.custom_fields?.[def.name] != null ? (
                                String(s.custom_fields[def.name])
                              ) : (
                                <span className="text-muted-foreground/40">
                                  —
                                </span>
                              )}
                            </td>
                          ))}
                          <td />
                        </tr>
                      );
                    }
                    return null;
                  })}
                </tbody>
              </table>
            </div>
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
    {
      type: "block" as const,
      depth,
      block: node.block,
      key: `b-${node.block.id}`,
    },
    ...flattenToTableRows(node.children, depth + 1),
    ...node.subnets.map(
      (s): TreeTableItem => ({
        type: "subnet",
        depth: depth + 1,
        subnet: s,
        key: `s-${s.id}`,
      }),
    ),
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
  onSpaceDeleted,
}: {
  space: IPSpace;
  onSelectSubnet: (subnet: Subnet) => void;
  onSelectBlock: (block: IPBlock) => void;
  onSpaceDeleted?: () => void;
}) {
  const qc = useQueryClient();
  const { data: blocks, isLoading: blocksLoading } = useQuery({
    queryKey: ["blocks", space.id],
    queryFn: () => ipamApi.listBlocks(space.id),
  });

  const { data: subnets, isLoading: subnetsLoading } = useQuery({
    queryKey: ["subnets", space.id],
    queryFn: () => ipamApi.listSubnets({ space_id: space.id }),
  });

  const { data: subnetCfDefs = [] } = useQuery({
    queryKey: ["custom-fields", "subnet"],
    queryFn: () => customFieldsApi.list("subnet"),
  });

  // Selection holds either ``subnet:<id>`` or ``block:<id>`` so a single
  // bulk action can delete a mixed set. Only *leaf* blocks (no child blocks
  // or subnets) get a checkbox — deleting a non-leaf block would cascade
  // unpredictably or hit the FK RESTRICT on subnet.block_id.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkOpen, setBulkOpen] = useState(false);
  const [showBulkDelete, setShowBulkDelete] = useState(false);
  const [showEditSpace, setShowEditSpace] = useState(false);
  const [showCreateBlock, setShowCreateBlock] = useState(false);
  const [showCreateSubnet, setShowCreateSubnet] = useState(false);
  const [showFindFree, setShowFindFree] = useState(false);
  const [findFreePrefill, setFindFreePrefill] = useState<{
    network: string;
    blockId: string;
  } | null>(null);
  const [showSplitSubnet, setShowSplitSubnet] = useState(false);
  const [showMergeSubnet, setShowMergeSubnet] = useState(false);
  const [showSpaceFilters, setShowSpaceFilters] = useState(false);
  const [spaceFilter, setSpaceFilter] = useState({
    network: "",
    name: "",
    router: "",
    vlan: "",
    status: "",
  });
  const [showDnsSync, setShowDnsSync] = useState(false);

  const [spaceBulkDeleteError, setSpaceBulkDeleteError] = useState<
    string | null
  >(null);
  const bulkDeleteMut = useMutation({
    // allSettled on both phases so a single 409 (non-empty subnet/block)
    // doesn't hide the rest. Subnets first — a subnet hanging off a leaf
    // block would otherwise trip the block's RESTRICT FK.
    mutationFn: async () => {
      const subnetIds: string[] = [];
      const blockIds: string[] = [];
      for (const key of selected) {
        if (key.startsWith("subnet:"))
          subnetIds.push(key.slice("subnet:".length));
        else if (key.startsWith("block:"))
          blockIds.push(key.slice("block:".length));
      }
      const subnetResults = await Promise.allSettled(
        // The bulk-delete confirmation modal already made the
        // cascade explicit; pass force=true so we cascade through
        // any non-empty subnets the operator has acknowledged.
        subnetIds.map((id) => ipamApi.deleteSubnet(id, true)),
      );
      const blockResults = await Promise.allSettled(
        blockIds.map((id) => ipamApi.deleteBlock(id)),
      );
      type Fail = { id: string; kind: "subnet" | "block"; message: string };
      const failures: Fail[] = [];
      const collect = (
        ids: string[],
        results: PromiseSettledResult<unknown>[],
        kind: "subnet" | "block",
      ) => {
        ids.forEach((id, i) => {
          const r = results[i];
          if (r.status === "rejected") {
            const reason = r.reason as
              | { response?: { data?: { detail?: unknown } } }
              | undefined;
            const detail = reason?.response?.data?.detail;
            failures.push({
              id,
              kind,
              message:
                typeof detail === "string"
                  ? detail
                  : detail
                    ? JSON.stringify(detail)
                    : "Unknown error",
            });
          }
        });
      };
      collect(subnetIds, subnetResults, "subnet");
      collect(blockIds, blockResults, "block");
      return { failures, total: subnetIds.length + blockIds.length };
    },
    onSuccess: ({ failures, total }) => {
      qc.invalidateQueries({ queryKey: ["subnets", space.id] });
      qc.invalidateQueries({ queryKey: ["blocks", space.id] });
      if (failures.length === 0) {
        setSelected(new Set());
        setShowBulkDelete(false);
        setSpaceBulkDeleteError(null);
        return;
      }
      const lookup = new Map<string, string>();
      (subnets ?? []).forEach((s) => lookup.set(s.id, s.network));
      (blocks ?? []).forEach((b) => lookup.set(b.id, b.network));
      const detail = failures
        .map((f) => `• ${f.kind} ${lookup.get(f.id) ?? f.id}: ${f.message}`)
        .join("\n");
      setSpaceBulkDeleteError(
        `${failures.length} of ${total} items could not be deleted:\n${detail}`,
      );
    },
  });

  const toggleOne = (key: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const isLoading = blocksLoading || subnetsLoading;
  const rows =
    blocks && subnets
      ? flattenToTableRows(buildBlockTree(blocks, subnets, null))
      : [];

  // A block is a "leaf" when nothing else is anchored to it. Safe to delete
  // directly; the FK RESTRICT on child subnets and the CASCADE on child
  // blocks both stay dormant when the set is empty.
  const leafBlockIds = new Set<string>();
  if (blocks && subnets) {
    const subnetBlockParents = new Set(subnets.map((s) => s.block_id));
    const blockParents = new Set(
      blocks.map((b) => b.parent_block_id).filter((p): p is string => !!p),
    );
    for (const b of blocks) {
      if (!subnetBlockParents.has(b.id) && !blockParents.has(b.id)) {
        leafBlockIds.add(b.id);
      }
    }
  }
  const selectedCount = selected.size;
  const hasBlocksSelected = [...selected].some((k) => k.startsWith("block:"));
  const selectedSubnetIds = [...selected]
    .filter((k) => k.startsWith("subnet:"))
    .map((k) => k.slice("subnet:".length));

  const hasSpaceFilter = Object.values(spaceFilter).some(Boolean);
  const filteredSpaceRows = hasSpaceFilter
    ? rows.filter((item) => {
        if (item.type === "block" && item.block) {
          const b = item.block;
          if (spaceFilter.network && !b.network.includes(spaceFilter.network))
            return false;
          if (
            spaceFilter.name &&
            b.name &&
            !b.name.toLowerCase().includes(spaceFilter.name.toLowerCase())
          )
            return false;
          return true;
        }
        if (item.type === "subnet" && item.subnet) {
          const s = item.subnet;
          if (spaceFilter.network && !s.network.includes(spaceFilter.network))
            return false;
          if (
            spaceFilter.name &&
            s.name &&
            !s.name.toLowerCase().includes(spaceFilter.name.toLowerCase())
          )
            return false;
          if (
            spaceFilter.router &&
            !(s.vlan?.router_name ?? "")
              .toLowerCase()
              .includes(spaceFilter.router.toLowerCase())
          )
            return false;
          if (
            spaceFilter.vlan &&
            !(
              String(s.vlan_id ?? "").includes(spaceFilter.vlan) ||
              (s.vlan?.name ?? "")
                .toLowerCase()
                .includes(spaceFilter.vlan.toLowerCase())
            )
          )
            return false;
          if (spaceFilter.status && s.status !== spaceFilter.status)
            return false;
          return true;
        }
        return true;
      })
    : rows;

  const isEmpty = !isLoading && filteredSpaceRows.length === 0;

  const selectableKeysInView = [
    ...filteredSpaceRows
      .filter((r) => r.type === "subnet" && r.subnet)
      .map((r) => `subnet:${r.subnet!.id}`),
    ...filteredSpaceRows
      .filter(
        (r) => r.type === "block" && r.block && leafBlockIds.has(r.block.id),
      )
      .map((r) => `block:${r.block!.id}`),
  ];
  const allSelected =
    selectableKeysInView.length > 0 &&
    selectableKeysInView.every((k) => selected.has(k));
  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(selectableKeysInView));

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="border-b px-6 py-3">
        <div className="flex items-center justify-between gap-4 pb-2">
          <BreadcrumbPills items={[{ label: space.name, variant: "space" }]} />
          <div className="flex flex-shrink-0 items-center gap-2">
            {selected.size > 0 && (
              <>
                {!hasBlocksSelected && (
                  <HeaderButton
                    icon={Pencil}
                    onClick={() => setBulkOpen(true)}
                    title="Bulk-edit applies to subnets only"
                  >
                    Bulk Edit ({selectedCount})
                  </HeaderButton>
                )}
                {!hasBlocksSelected && selectedSubnetIds.length === 1 && (
                  <HeaderButton
                    onClick={() => setShowSplitSubnet(true)}
                    title="Split this subnet into 2^k aligned children"
                  >
                    Split…
                  </HeaderButton>
                )}
                {!hasBlocksSelected && selectedSubnetIds.length >= 2 && (
                  <HeaderButton
                    onClick={() => setShowMergeSubnet(true)}
                    title="Merge contiguous sibling subnets"
                  >
                    Merge…
                  </HeaderButton>
                )}
                <HeaderButton
                  variant="destructive"
                  icon={Trash2}
                  onClick={() => setShowBulkDelete(true)}
                >
                  Delete ({selectedCount})
                </HeaderButton>
              </>
            )}
            <HeaderButton
              icon={Globe2}
              onClick={() => setShowDnsSync(true)}
              title="Reconcile IPAM-managed DNS records across every subnet in this space"
            >
              Sync DNS
            </HeaderButton>
            <ExportButton scope={{ space_id: space.id }} label="Export" />
            <HeaderButton
              icon={Search}
              onClick={() => setShowFindFree(true)}
              title="Find unused CIDRs in this space"
            >
              Find Free…
            </HeaderButton>
            <HeaderButton icon={Pencil} onClick={() => setShowEditSpace(true)}>
              Edit Space
            </HeaderButton>
            <HeaderButton
              icon={Layers}
              onClick={() => setShowCreateBlock(true)}
            >
              Add Block
            </HeaderButton>
            <HeaderButton
              variant="primary"
              icon={Plus}
              onClick={() => setShowCreateSubnet(true)}
            >
              Add Subnet
            </HeaderButton>
          </div>
        </div>
        <div>
          <h2 className="text-base font-semibold">{space.name}</h2>
          {space.description && (
            <p className="text-xs text-muted-foreground">{space.description}</p>
          )}
          <SpaceVrfBadges space={space} onEdit={() => setShowEditSpace(true)} />
        </div>
      </div>
      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <p className="px-6 py-4 text-sm text-muted-foreground">Loading…</p>
        ) : isEmpty ? (
          <p className="px-6 py-4 text-sm text-muted-foreground">
            No blocks or subnets in this space yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b bg-muted/40 text-xs">
                  <th className="w-8 px-2 py-2 text-left">
                    <input
                      type="checkbox"
                      aria-label="Select all selectable rows"
                      checked={allSelected}
                      onChange={toggleAll}
                      disabled={selectableKeysInView.length === 0}
                    />
                  </th>
                  {(
                    [
                      "Network",
                      "Name",
                      "Router",
                      "VLAN",
                      "Used IPs",
                      "Utilization",
                      "Size",
                      "Status",
                    ] as const
                  ).map((col) => {
                    const filterKey =
                      col === "Network"
                        ? "network"
                        : col === "Name"
                          ? "name"
                          : col === "Router"
                            ? "router"
                            : col === "VLAN"
                              ? "vlan"
                              : col === "Status"
                                ? "status"
                                : null;
                    const hasFilter = filterKey
                      ? !!spaceFilter[filterKey as keyof typeof spaceFilter]
                      : false;
                    const isFilterable = filterKey !== null;
                    return (
                      <th
                        key={col}
                        className={cn(
                          "px-4 py-2 font-medium text-muted-foreground",
                          col === "Size" ? "text-right" : "text-left",
                        )}
                      >
                        <span className="inline-flex items-center gap-1">
                          {col}
                          {isFilterable && (
                            <button
                              onClick={() => setShowSpaceFilters((v) => !v)}
                              title={`Filter by ${col}`}
                              className={cn(
                                "rounded p-0.5 hover:bg-accent",
                                hasFilter
                                  ? "text-primary"
                                  : showSpaceFilters || hasSpaceFilter
                                    ? "text-primary/50"
                                    : "text-muted-foreground/40 hover:text-muted-foreground",
                              )}
                            >
                              <Filter className="h-2.5 w-2.5" />
                            </button>
                          )}
                        </span>
                      </th>
                    );
                  })}
                  {subnetCfDefs.map((def) => (
                    <th
                      key={def.name}
                      className="px-4 py-2 text-left font-medium text-muted-foreground"
                    >
                      {def.label}
                    </th>
                  ))}
                  <th className="px-4 py-2 text-right">
                    {hasSpaceFilter && (
                      <button
                        onClick={() =>
                          setSpaceFilter({
                            network: "",
                            name: "",
                            router: "",
                            vlan: "",
                            status: "",
                          })
                        }
                        title="Clear all filters"
                        className="rounded p-0.5 text-primary hover:text-destructive"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    )}
                  </th>
                </tr>
                {showSpaceFilters && (
                  <tr className="border-b bg-muted/10 text-xs">
                    <td />
                    {(
                      [
                        "Network",
                        "Name",
                        "Router",
                        "VLAN",
                        "Used IPs",
                        "Utilization",
                        "Size",
                        "Status",
                      ] as const
                    ).map((col) => {
                      const filterKey =
                        col === "Network"
                          ? "network"
                          : col === "Name"
                            ? "name"
                            : col === "Router"
                              ? "router"
                              : col === "VLAN"
                                ? "vlan"
                                : col === "Status"
                                  ? "status"
                                  : null;
                      if (!filterKey) return <td key={col} />;
                      if (filterKey === "status") {
                        return (
                          <td key={col} className="px-2 py-1">
                            <select
                              value={spaceFilter.status}
                              onChange={(e) =>
                                setSpaceFilter((f) => ({
                                  ...f,
                                  status: e.target.value,
                                }))
                              }
                              className="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                            >
                              <option value="">All</option>
                              {["active", "reserved", "deprecated"].map((s) => (
                                <option key={s} value={s}>
                                  {s}
                                </option>
                              ))}
                            </select>
                          </td>
                        );
                      }
                      return (
                        <td key={col} className="px-2 py-1">
                          <input
                            type="text"
                            value={
                              spaceFilter[filterKey as keyof typeof spaceFilter]
                            }
                            onChange={(e) =>
                              setSpaceFilter((f) => ({
                                ...f,
                                [filterKey]: e.target.value,
                              }))
                            }
                            placeholder="Filter…"
                            className="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                          />
                        </td>
                      );
                    })}
                    {subnetCfDefs.map((def) => (
                      <td key={def.name} />
                    ))}
                    <td />
                  </tr>
                )}
              </thead>
              <tbody className={zebraBodyCls}>
                {filteredSpaceRows.map((item) => {
                  const indent = item.depth * 20;
                  if (item.type === "block" && item.block) {
                    const b = item.block;
                    const size = cidrSize(b.network);
                    const isLeaf = leafBlockIds.has(b.id);
                    const key = `block:${b.id}`;
                    return (
                      <tr
                        key={item.key}
                        onClick={() => onSelectBlock(b)}
                        className="border-b last:border-0 cursor-pointer hover:bg-muted/30 bg-muted/10"
                      >
                        <td
                          className="w-8 px-2 py-2"
                          onClick={(e) => e.stopPropagation()}
                        >
                          {isLeaf ? (
                            <input
                              type="checkbox"
                              aria-label={`Select block ${b.network}`}
                              checked={selected.has(key)}
                              onChange={() => toggleOne(key)}
                            />
                          ) : (
                            <span
                              className="inline-block h-3.5 w-3.5"
                              title="Block has child blocks or subnets — delete those first"
                            />
                          )}
                        </td>
                        <td
                          className="py-2 pr-4"
                          style={{ paddingLeft: `${indent + 16}px` }}
                        >
                          <span className="inline-flex items-center gap-1.5 font-mono font-semibold text-foreground">
                            <Layers className="h-3.5 w-3.5 flex-shrink-0 text-violet-500" />
                            {b.network}
                          </span>
                        </td>
                        <td className="px-4 py-2 text-muted-foreground">
                          {b.name || (
                            <span className="text-muted-foreground/40">—</span>
                          )}
                        </td>
                        <td className="px-4 py-2 text-muted-foreground/40">
                          —
                        </td>
                        <td className="px-4 py-2 text-muted-foreground/40">
                          —
                        </td>
                        <td className="px-4 py-2 text-muted-foreground/40">
                          —
                        </td>
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
                        <td className="px-4 py-2 text-muted-foreground/40">
                          —
                        </td>
                        {subnetCfDefs.map((def) => (
                          <td
                            key={def.name}
                            className="px-4 py-2 text-muted-foreground/40"
                          >
                            —
                          </td>
                        ))}
                      </tr>
                    );
                  }
                  if (item.type === "subnet" && item.subnet) {
                    const s = item.subnet;
                    const key = `subnet:${s.id}`;
                    return (
                      <tr
                        key={item.key}
                        onClick={() => onSelectSubnet(s)}
                        className="border-b last:border-0 cursor-pointer hover:bg-muted/30"
                      >
                        <td
                          className="w-8 px-2 py-2"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <input
                            type="checkbox"
                            aria-label={`Select ${s.network}`}
                            checked={selected.has(key)}
                            onChange={() => toggleOne(key)}
                          />
                        </td>
                        <td
                          className="py-2 pr-4"
                          style={{ paddingLeft: `${indent + 16}px` }}
                        >
                          <span className="inline-flex items-center gap-1.5 font-mono font-medium">
                            <Network className="h-3.5 w-3.5 flex-shrink-0 text-blue-500" />
                            {s.network}
                          </span>
                        </td>
                        <td className="px-4 py-2 text-muted-foreground">
                          {s.name || (
                            <span className="text-muted-foreground/40">—</span>
                          )}
                        </td>
                        <td className="px-4 py-2 text-muted-foreground">
                          {s.vlan?.router_name ?? (
                            <span className="text-muted-foreground/40">—</span>
                          )}
                        </td>
                        <td className="px-4 py-2 text-muted-foreground">
                          {s.vlan ? (
                            <span>
                              {s.vlan.vlan_id}
                              {s.vlan.name && (
                                <span className="ml-1 text-muted-foreground/70">
                                  ({s.vlan.name})
                                </span>
                              )}
                            </span>
                          ) : s.vlan_id != null ? (
                            <span
                              className="text-muted-foreground/70"
                              title="Legacy tag — assign a Router/VLAN from the Edit modal"
                            >
                              {s.vlan_id}
                            </span>
                          ) : (
                            <span className="text-muted-foreground/40">—</span>
                          )}
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
                        {subnetCfDefs.map((def) => (
                          <td
                            key={def.name}
                            className="px-4 py-2 text-muted-foreground"
                          >
                            {s.custom_fields?.[def.name] != null ? (
                              String(s.custom_fields[def.name])
                            ) : (
                              <span className="text-muted-foreground/40">
                                —
                              </span>
                            )}
                          </td>
                        ))}
                      </tr>
                    );
                  }
                  return null;
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {bulkOpen && (
        <BulkEditSubnetsModal
          subnetIds={Array.from(selected)
            .filter((k) => k.startsWith("subnet:"))
            .map((k) => k.slice("subnet:".length))}
          onClose={() => setBulkOpen(false)}
          onDone={() => {
            setBulkOpen(false);
            setSelected(new Set());
          }}
        />
      )}
      {showEditSpace && (
        <EditSpaceModal
          space={space}
          onClose={() => {
            setShowEditSpace(false);
            qc.invalidateQueries({ queryKey: ["spaces"] });
          }}
          onDeleted={() => {
            setShowEditSpace(false);
            onSpaceDeleted?.();
          }}
        />
      )}
      {showCreateBlock && (
        <CreateBlockModal
          spaceId={space.id}
          onClose={() => setShowCreateBlock(false)}
        />
      )}
      {showCreateSubnet && (
        <CreateSubnetModal
          spaceId={space.id}
          defaultBlockId={findFreePrefill?.blockId}
          defaultNetwork={findFreePrefill?.network}
          onClose={() => {
            setShowCreateSubnet(false);
            setFindFreePrefill(null);
          }}
        />
      )}
      {showFindFree && (
        <FindFreeModal
          space={space}
          onClose={() => setShowFindFree(false)}
          onPickCidr={(cidr, blockId) => {
            setFindFreePrefill({ network: cidr, blockId });
            setShowFindFree(false);
            setShowCreateSubnet(true);
          }}
        />
      )}
      {showSplitSubnet &&
        (() => {
          const sn = subnets?.find((s) => s.id === selectedSubnetIds[0]);
          return sn ? (
            <SplitSubnetModal
              subnet={sn}
              onClose={() => setShowSplitSubnet(false)}
              onCommitted={() => {
                setShowSplitSubnet(false);
                setSelected(new Set());
                qc.invalidateQueries({ queryKey: ["subnets", space.id] });
              }}
            />
          ) : null;
        })()}
      {showMergeSubnet &&
        (() => {
          const sn = subnets?.find((s) => s.id === selectedSubnetIds[0]);
          return sn ? (
            <MergeSubnetSiblingPicker
              subnet={sn}
              onClose={() => setShowMergeSubnet(false)}
              onCommitted={() => {
                setShowMergeSubnet(false);
                setSelected(new Set());
                qc.invalidateQueries({ queryKey: ["subnets", space.id] });
              }}
            />
          ) : null;
        })()}
      {showBulkDelete &&
        (() => {
          const subnetCount = [...selected].filter((k) =>
            k.startsWith("subnet:"),
          ).length;
          const blockCount = [...selected].filter((k) =>
            k.startsWith("block:"),
          ).length;
          const parts: string[] = [];
          if (subnetCount)
            parts.push(`${subnetCount} subnet${subnetCount === 1 ? "" : "s"}`);
          if (blockCount)
            parts.push(`${blockCount} block${blockCount === 1 ? "" : "s"}`);
          const summary = parts.join(" + ");
          return (
            <ConfirmDestroyModal
              title={`Delete ${summary}`}
              description={`This will move ${summary} to Trash. You can restore from Admin → Trash within 30 days. Only leaf blocks (no child blocks or subnets) are selectable.`}
              checkLabel={`I understand ${summary} will be moved to Trash.`}
              isPending={bulkDeleteMut.isPending}
              error={spaceBulkDeleteError}
              onClose={() => {
                setShowBulkDelete(false);
                setSpaceBulkDeleteError(null);
              }}
              onConfirm={() => {
                setSpaceBulkDeleteError(null);
                bulkDeleteMut.mutate();
              }}
            />
          );
        })()}
      {showDnsSync && (
        <DnsSyncModal
          scope={{ kind: "space", id: space.id, label: space.name }}
          onClose={() => setShowDnsSync(false)}
        />
      )}
    </div>
  );
}

// ─── Bulk-edit Modal ─────────────────────────────────────────────────────────

function BulkEditSubnetsModal({
  subnetIds,
  onClose,
  onDone,
}: {
  subnetIds: string[];
  onClose: () => void;
  onDone: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [statusVal, setStatusVal] = useState("");
  const [vlanId, setVlanId] = useState("");
  const [error, setError] = useState<string | null>(null);

  const mut = useMutation({
    mutationFn: () => {
      const changes: Record<string, unknown> = {};
      if (name.trim()) changes.name = name.trim();
      if (description.trim()) changes.description = description.trim();
      if (statusVal) changes.status = statusVal;
      if (vlanId.trim()) {
        const n = Number(vlanId);
        if (Number.isNaN(n)) throw new Error("VLAN ID must be a number");
        changes.vlan_id = n;
      }
      if (Object.keys(changes).length === 0) {
        throw new Error("Set at least one field to apply");
      }
      return ipamApi.bulkEditSubnets(subnetIds, changes);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnets"] });
      qc.invalidateQueries({ queryKey: ["blocks"] });
      onDone();
    },
    onError: (e: Error) => setError(e.message),
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 p-2 sm:p-4">
      <div className="w-full max-w-[95vw] sm:max-w-md rounded-lg border bg-card p-4 sm:p-6 shadow-lg">
        <h3 className="mb-3 text-base font-semibold">
          Bulk edit {subnetIds.length} subnet{subnetIds.length === 1 ? "" : "s"}
        </h3>
        <p className="mb-3 text-xs text-muted-foreground">
          Leave a field blank to keep it unchanged.
        </p>
        <div className="space-y-3 text-sm">
          <label className="block">
            <span className="mb-1 block text-xs text-muted-foreground">
              Name
            </span>
            <input
              className="w-full rounded border bg-background px-2 py-1"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-muted-foreground">
              Description
            </span>
            <input
              className="w-full rounded border bg-background px-2 py-1"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-muted-foreground">
              Status
            </span>
            <select
              className="w-full rounded border bg-background px-2 py-1"
              value={statusVal}
              onChange={(e) => setStatusVal(e.target.value)}
            >
              <option value="">—</option>
              <option value="active">active</option>
              <option value="deprecated">deprecated</option>
              <option value="reserved">reserved</option>
              <option value="quarantine">quarantine</option>
            </select>
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-muted-foreground">
              VLAN ID
            </span>
            <input
              className="w-full rounded border bg-background px-2 py-1"
              value={vlanId}
              onChange={(e) => setVlanId(e.target.value)}
              inputMode="numeric"
            />
          </label>
        </div>
        {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded border px-3 py-1 text-xs hover:bg-muted"
          >
            Cancel
          </button>
          <button
            disabled={mut.isPending}
            onClick={() => {
              setError(null);
              mut.mutate();
            }}
            className="rounded bg-primary px-3 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50"
          >
            {mut.isPending ? "Applying…" : "Apply"}
          </button>
        </div>
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
  const [expanded, setExpanded] = useSessionState<boolean>(
    `spatium.ipam.expandedSpace.${space.id}`,
    true,
  );
  const [showCreateSubnet, setShowCreateSubnet] = useState<
    string | true | false
  >(false); // string = default block_id
  const [showCreateBlock, setShowCreateBlock] = useState<string | true | false>(
    false,
  ); // string = parent block_id
  const [showEditSpace, setShowEditSpace] = useState(false);
  const [editBlock, setEditBlock] = useState<IPBlock | null>(null);
  const [subnetToDelete, setSubnetToDelete] = useState<Subnet | null>(null);
  const [blockToDelete, setBlockToDelete] = useState<IPBlock | null>(null);
  const [dndError, setDndError] = useState<string | null>(null);
  const qc = useQueryClient();
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
  );

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

  const [subnetDeleteError, setSubnetDeleteError] = useState<string | null>(
    null,
  );
  const deleteSubnet = useMutation({
    // The single-subnet ConfirmDestroyModal already requires the
    // operator to tick "…and all its contents will be permanently
    // deleted" before this fires, so cascade is the right semantics.
    mutationFn: (id: string) => ipamApi.deleteSubnet(id, true),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnets", space.id] });
      setSubnetToDelete(null);
      setSubnetDeleteError(null);
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      setSubnetDeleteError(
        typeof detail === "string"
          ? detail
          : detail
            ? JSON.stringify(detail)
            : "Failed to delete subnet.",
      );
    },
  });

  const [blockDeleteError, setBlockDeleteError] = useState<string | null>(null);
  const deleteBlockMut = useMutation({
    mutationFn: (id: string) => ipamApi.deleteBlock(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["blocks", space.id] });
      qc.invalidateQueries({ queryKey: ["subnets", space.id] });
      setBlockToDelete(null);
      setBlockDeleteError(null);
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      setBlockDeleteError(
        typeof detail === "string"
          ? detail
          : detail
            ? JSON.stringify(detail)
            : "Failed to delete block.",
      );
    },
  });

  const moveSubnet = useMutation({
    mutationFn: ({ id, block_id }: { id: string; block_id: string }) =>
      ipamApi.updateSubnet(id, { block_id } as Partial<Subnet>),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnets", space.id] });
      qc.invalidateQueries({ queryKey: ["blocks", space.id] });
    },
    onError: (err: unknown) => {
      const msg = (err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      setDndError(typeof msg === "string" ? msg : "Move failed");
    },
  });

  const moveBlock = useMutation({
    mutationFn: ({
      id,
      parent_block_id,
    }: {
      id: string;
      parent_block_id: string | null;
    }) => ipamApi.updateBlock(id, { parent_block_id }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["blocks", space.id] });
    },
    onError: (err: unknown) => {
      const msg = (err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      setDndError(typeof msg === "string" ? msg : "Move failed");
    },
  });

  function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over) return;
    const srcData = active.data.current as
      | { kind: "block" | "subnet"; block?: IPBlock; subnet?: Subnet }
      | undefined;
    const dstData = over.data.current as
      | { kind: "block"; block: IPBlock }
      | undefined;
    if (!srcData || !dstData) return;
    const targetBlock = dstData.block;

    if (srcData.kind === "subnet" && srcData.subnet) {
      const sn = srcData.subnet;
      if (sn.block_id === targetBlock.id) return;
      if (sn.space_id !== targetBlock.space_id) {
        setDndError("Cannot move subnet across IP spaces");
        return;
      }
      if (!cidrContains(targetBlock.network, sn.network)) {
        setDndError(`${sn.network} does not fit inside ${targetBlock.network}`);
        return;
      }
      moveSubnet.mutate({ id: sn.id, block_id: targetBlock.id });
      return;
    }

    if (srcData.kind === "block" && srcData.block) {
      const b = srcData.block;
      if (b.id === targetBlock.id) return;
      if (b.parent_block_id === targetBlock.id) return;
      if (b.space_id !== targetBlock.space_id) {
        setDndError("Cannot move block across IP spaces");
        return;
      }
      if (!cidrContains(targetBlock.network, b.network)) {
        setDndError(`${b.network} does not fit inside ${targetBlock.network}`);
        return;
      }
      // Prevent making a block a descendant of itself (client-side; backend also checks)
      if (blocks) {
        let cursor: IPBlock | undefined = targetBlock;
        while (cursor) {
          if (cursor.id === b.id) {
            setDndError("Cannot move a block into its own descendant");
            return;
          }
          cursor = cursor.parent_block_id
            ? blocks.find((bl) => bl.id === cursor!.parent_block_id)
            : undefined;
        }
      }
      moveBlock.mutate({ id: b.id, parent_block_id: targetBlock.id });
    }
  }

  // (block_id is now required; all subnets appear under their block)

  return (
    <div>
      {/* Space header */}
      <ContextMenu>
        <ContextMenuTrigger asChild>
          <div
            className={cn(
              "group flex items-center gap-1 rounded-md px-1 py-1.5 hover:bg-muted/50",
              swatchTintCls(space.color),
              // Ring lets the color tint stay visible while still marking the
              // row as selected; fall back to bg-primary/5 only when no
              // color is set so an uncolored space still has a selected look.
              isSpaceSelected && !space.color && "bg-primary/5",
              isSpaceSelected && "ring-1 ring-primary/60",
            )}
            title={space.color ? `color: ${space.color}` : undefined}
          >
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
              <span
                className={cn(
                  "flex-1 truncate text-left text-sm font-medium",
                  isSpaceSelected && "text-primary",
                )}
              >
                {space.name}
              </span>
            </button>
          </div>
        </ContextMenuTrigger>
        <ContextMenuContent>
          <ContextMenuLabel>{space.name}</ContextMenuLabel>
          <ContextMenuSeparator />
          <ContextMenuItem onSelect={() => setShowCreateBlock(true)}>
            New Block…
          </ContextMenuItem>
          <ContextMenuItem onSelect={() => setShowCreateSubnet(true)}>
            New Subnet…
          </ContextMenuItem>
          <ContextMenuSeparator />
          <ContextMenuItem onSelect={() => setExpanded((v) => !v)}>
            {expanded ? "Collapse" : "Expand"}
          </ContextMenuItem>
          <ContextMenuItem onSelect={() => setShowEditSpace(true)}>
            Edit Space…
          </ContextMenuItem>
        </ContextMenuContent>
      </ContextMenu>

      {/* Tree with vertical connecting line */}
      {expanded && (
        <DndContext sensors={sensors} onDragEnd={handleDragEnd}>
          <div className="ml-[9px] pl-2 border-l border-border/40 space-y-0.5">
            {isLoading && (
              <p className="py-1 pl-2 text-xs text-muted-foreground">
                Loading…
              </p>
            )}

            {/* Block tree (recursive) */}
            {blocks &&
              subnets &&
              buildBlockTree(blocks, subnets, null).map((node) => (
                <BlockTreeRow
                  key={node.block.id}
                  node={node}
                  selectedSubnetId={selectedSubnetId}
                  selectedBlockId={selectedBlockId}
                  onSelectBlock={onSelectBlock}
                  onSelectSubnet={onSelectSubnet}
                  onDeleteSubnet={(s) => setSubnetToDelete(s)}
                  onDeleteBlock={(b) => setBlockToDelete(b)}
                  onEditBlock={(b) => setEditBlock(b)}
                  onCreateSubnet={(blockId) => setShowCreateSubnet(blockId)}
                  onCreateChildBlock={(parentId) =>
                    setShowCreateBlock(parentId)
                  }
                  onAllocateIp={(s) => onSelectSubnet(s)}
                  depth={0}
                />
              ))}

            {!isLoading && !subnets?.length && !blocks?.length && (
              <p className="py-1 pl-2 text-xs text-muted-foreground">
                No blocks yet.
              </p>
            )}
          </div>
        </DndContext>
      )}

      {dndError && (
        <div className="mx-2 mt-2 flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-2 py-1.5 text-xs text-destructive">
          <span className="flex-1">{dndError}</span>
          <button
            onClick={() => setDndError(null)}
            className="text-destructive hover:opacity-70"
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      )}

      {editBlock && (
        <EditBlockModal
          block={editBlock}
          onClose={(updated) => {
            setEditBlock(null);
            if (updated)
              qc.invalidateQueries({ queryKey: ["blocks", space.id] });
          }}
          onDeleted={() => setEditBlock(null)}
        />
      )}

      {blockToDelete && (
        <ConfirmDestroyModal
          title="Delete Block"
          description={`Delete block ${blockToDelete.network}${blockToDelete.name ? ` (${blockToDelete.name})` : ""}?`}
          checkLabel={`I understand everything inside ${blockToDelete.network} will be permanently deleted.`}
          isPending={deleteBlockMut.isPending}
          error={blockDeleteError}
          onClose={() => {
            setBlockToDelete(null);
            setBlockDeleteError(null);
          }}
          onConfirm={() => {
            setBlockDeleteError(null);
            deleteBlockMut.mutate(blockToDelete.id);
          }}
        />
      )}

      {showCreateSubnet && (
        <CreateSubnetModal
          spaceId={space.id}
          defaultBlockId={
            typeof showCreateSubnet === "string" ? showCreateSubnet : undefined
          }
          onClose={() => setShowCreateSubnet(false)}
        />
      )}
      {showCreateBlock && (
        <CreateBlockModal
          spaceId={space.id}
          defaultParentBlockId={
            typeof showCreateBlock === "string" ? showCreateBlock : undefined
          }
          onClose={() => setShowCreateBlock(false)}
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
        <ConfirmDestroyModal
          title="Delete Subnet"
          description={`Delete subnet ${subnetToDelete.network}${subnetToDelete.name ? ` (${subnetToDelete.name})` : ""}?`}
          checkLabel={`I understand ${subnetToDelete.network} and all its contents will be permanently deleted.`}
          isPending={deleteSubnet.isPending}
          error={subnetDeleteError}
          onClose={() => {
            setSubnetToDelete(null);
            setSubnetDeleteError(null);
          }}
          onConfirm={() => {
            if (selectedSubnetId === subnetToDelete.id) onSelectSubnet(null);
            setSubnetDeleteError(null);
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
  useStickyLocation("spatium.lastUrl.ipam");
  const [selectedSubnet, setSelectedSubnet] = useState<Subnet | null>(null);
  const [selectedSpace, setSelectedSpace] = useState<IPSpace | null>(null);
  const [selectedBlock, setSelectedBlock] = useState<IPBlock | null>(null);
  const [showCreateSpace, setShowCreateSpace] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const qc = useQueryClient();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const deepLinkHandled = useRef(false);
  const urlRestored = useRef(false);
  // Captured from ``location.state`` on the first deep-link read and
  // passed down to ``SubnetDetail``. Can't be read lazily by
  // ``SubnetDetail`` itself because ``selectSubnet`` calls
  // ``setSearchParams(..., { replace: true })`` which drops
  // ``location.state`` before ``SubnetDetail`` ever mounts.
  const [pendingHighlightAddress, setPendingHighlightAddress] = useState<
    string | null
  >(null);
  // Subnet id the deep-link targeted — used below to clear the
  // highlight as soon as the operator navigates to a different subnet,
  // so coming back to the original subnet doesn't re-flash the row
  // (one-shot semantics).
  const highlightTargetSubnetRef = useRef<string | null>(null);

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
      if (sp) {
        selectSpace(sp);
        deepLinkHandled.current = true;
        urlRestored.current = true;
      }
    } else if (state.selectBlock && allBlocks) {
      const bl = allBlocks.find((b) => b.id === state.selectBlock);
      if (bl) {
        selectBlock(bl);
        deepLinkHandled.current = true;
        urlRestored.current = true;
      }
    } else if (state.selectSubnet && allSubnets) {
      const sn = allSubnets.find((s) => s.id === state.selectSubnet);
      if (sn) {
        // Capture the highlight BEFORE selectSubnet fires — the setter
        // replaces the history entry via setSearchParams and drops
        // ``location.state``, so SubnetDetail can't read it later.
        if (state.highlightAddress) {
          setPendingHighlightAddress(state.highlightAddress);
          highlightTargetSubnetRef.current = sn.id;
        }
        selectSubnet(sn);
        deepLinkHandled.current = true;
        urlRestored.current = true;
      }
    }
  }, [location.state, spaces, allBlocks, allSubnets]);

  // One-shot semantics: as soon as the operator navigates to a subnet
  // other than the deep-link target (or to a block / space), clear the
  // pending highlight. Re-visiting the original subnet later should
  // not re-flash the row.
  useEffect(() => {
    if (!pendingHighlightAddress) return;
    if (selectedSubnet?.id !== highlightTargetSubnetRef.current) {
      setPendingHighlightAddress(null);
    }
  }, [selectedSubnet, pendingHighlightAddress]);

  // URL-state restore: reopen last-visited space/block/subnet on back-navigation
  // Depends on searchParams so that when `useStickyLocation` navigates from
  // bare `/ipam` → `/ipam?subnet=…` after mount, this effect re-runs and picks
  // up the now-populated params. The `urlRestored` guard is only set once
  // we've actually matched a param, so an early run with empty searchParams
  // doesn't latch us into "nothing to restore".
  useEffect(() => {
    if (urlRestored.current) return;
    if (!spaces || !allBlocks || !allSubnets) return;
    const subnetId = searchParams.get("subnet");
    const blockId = searchParams.get("block");
    const spaceId = searchParams.get("space");
    if (!subnetId && !blockId && !spaceId) return;
    urlRestored.current = true;
    if (subnetId) {
      const sn = allSubnets.find((s: Subnet) => s.id === subnetId);
      if (sn) {
        setSelectedSubnet(sn);
        setSelectedBlock(null);
        setSelectedSpace(null);
        return;
      }
    }
    if (blockId) {
      const bl = allBlocks.find((b) => b.id === blockId);
      if (bl) {
        setSelectedBlock(bl);
        setSelectedSubnet(null);
        setSelectedSpace(null);
        return;
      }
    }
    if (spaceId) {
      const sp = spaces.find((s) => s.id === spaceId);
      if (sp) {
        setSelectedSpace(sp);
        setSelectedSubnet(null);
        setSelectedBlock(null);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spaces, allBlocks, allSubnets, searchParams]);

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
    setSearchParams(subnet ? { subnet: subnet.id } : {}, { replace: true });
  }

  function selectSpace(space: IPSpace) {
    setSelectedSpace(space);
    setSelectedSubnet(null);
    setSelectedBlock(null);
    setSearchParams({ space: space.id }, { replace: true });
  }

  function selectBlock(block: IPBlock) {
    setSelectedBlock(block);
    setSelectedSubnet(null);
    setSelectedSpace(null);
    setSearchParams({ block: block.id }, { replace: true });
  }

  const selectedSubnetBlock = detailBlocks?.find(
    (b) => b.id === selectedSubnet?.block_id,
  );
  const selectedSubnetBlockAncestors =
    selectedSubnetBlock && detailBlocks
      ? getBlockAncestors(selectedSubnetBlock, detailBlocks)
      : [];
  const selectedBlockAncestors =
    selectedBlock && detailBlocks
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
              onClick={() => {
                // Force refetch — bare invalidate only marks queries stale,
                // which isn't enough when the user pressed Refresh after
                // creating resources via the API directly.
                qc.refetchQueries({ queryKey: ["spaces"] });
                qc.refetchQueries({ queryKey: ["blocks"] });
                qc.refetchQueries({ queryKey: ["subnets"] });
              }}
              className="rounded p-1 text-muted-foreground hover:text-foreground"
              title="Refresh"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setShowImport(true)}
              disabled={!spaces || spaces.length === 0}
              className="rounded p-1 text-muted-foreground hover:text-foreground disabled:opacity-40"
              title="Import subnets"
            >
              <Upload className="h-3.5 w-3.5" />
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
            spaceName={
              spaces?.find((s: IPSpace) => s.id === selectedSubnet.space_id)
                ?.name
            }
            block={selectedSubnetBlock}
            blockAncestors={selectedSubnetBlockAncestors}
            highlightAddressId={pendingHighlightAddress}
            onSelectSpace={() => {
              const sp = spaces?.find(
                (s: IPSpace) => s.id === selectedSubnet.space_id,
              );
              if (sp) selectSpace(sp);
            }}
            onSelectBlock={selectBlock}
            onSubnetEdited={(updated) => setSelectedSubnet(updated)}
            onSubnetDeleted={() => selectSubnet(null)}
          />
        ) : selectedBlock ? (
          <BlockDetailView
            block={selectedBlock}
            spaceName={
              spaces?.find((s: IPSpace) => s.id === selectedBlock.space_id)
                ?.name ?? ""
            }
            space={spaces?.find(
              (s: IPSpace) => s.id === selectedBlock.space_id,
            )}
            ancestors={selectedBlockAncestors}
            allBlocks={detailBlocks ?? []}
            allSubnets={detailSubnets ?? []}
            onSelectSpace={() => {
              const sp = spaces?.find(
                (s: IPSpace) => s.id === selectedBlock.space_id,
              );
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
            onSpaceDeleted={() => {
              setSelectedSpace(null);
              setSearchParams({}, { replace: true });
            }}
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
      {showImport && spaces && (
        <ImportModal
          spaces={spaces}
          defaultSpaceId={
            selectedSpace?.id ??
            selectedBlock?.space_id ??
            selectedSubnet?.space_id
          }
          onClose={() => setShowImport(false)}
          onCommitted={() => {
            qc.invalidateQueries({ queryKey: ["spaces"] });
            qc.invalidateQueries({ queryKey: ["blocks"] });
            qc.invalidateQueries({ queryKey: ["subnets"] });
          }}
        />
      )}
    </div>
  );
}
