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
  Upload,
  Globe2,
  Filter,
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
  type IPSpace,
  type IPBlock,
  type Subnet,
  type IPAddress,
  type CustomField,
  type DNSZone,
  type FreeCidrRange,
  type Router as NetworkRouter,
  type VLAN,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { useStickyLocation } from "@/lib/stickyLocation";
import { useSessionState } from "@/lib/useSessionState";
import { ImportModal, ExportButton } from "./ImportExportModals";
import { cidrContains } from "@/lib/cidr";
import { FreeSpaceBand } from "@/components/ipam/FreeSpaceBand";
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
      {copied ? (
        <Check className="h-3 w-3 text-green-500" />
      ) : (
        <Copy className="h-3 w-3" />
      )}
    </button>
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
          "w-full rounded-lg border bg-card p-4 sm:p-6 shadow-lg max-h-[90vh] overflow-y-auto",
          // Desktop caps, but always fit in the viewport on mobile.
          wide ? "sm:max-w-2xl" : "sm:max-w-md",
          "max-w-[95vw]",
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
  const [dnsGroupIds, setDnsGroupIds] = useState<string[]>([]);
  const [dnsZoneId, setDnsZoneId] = useState<string | null>(null);
  const [dnsAdditionalZoneIds, setDnsAdditionalZoneIds] = useState<string[]>(
    [],
  );

  const mutation = useMutation({
    mutationFn: () =>
      ipamApi.createSpace({
        name,
        description,
        is_default: false,
        dns_group_ids: dnsGroupIds,
        dns_zone_id: dnsZoneId,
        dns_additional_zone_ids: dnsAdditionalZoneIds,
      }),
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

  function toggleGroup(gId: string) {
    if (groupIds.includes(gId)) {
      onGroupIdsChange(groupIds.filter((id) => id !== gId));
      // clear zones from this group if needed
      const groupZoneIds = (
        (zoneQueries.find((_: unknown, i: number) => activeGroupIds[i] === gId)
          ?.data as DNSZone[] | undefined) ?? []
      ).map((z: DNSZone) => z.id);
      if (zoneId && groupZoneIds.includes(zoneId)) onZoneIdChange(null);
      onAdditionalZoneIdsChange(
        additionalZoneIds.filter((id) => !groupZoneIds.includes(id)),
      );
    } else {
      onGroupIdsChange([...groupIds, gId]);
    }
  }

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
        {/* DNS Server Groups */}
        <div>
          <p className="text-xs text-muted-foreground mb-1">
            DNS Server Groups
          </p>
          {allGroups.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">
              No groups configured.
            </p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {allGroups.map((g) => {
                const selected = displayGroupIds.includes(g.id);
                return (
                  <button
                    key={g.id}
                    type="button"
                    onClick={() => !inherit && toggleGroup(g.id)}
                    className={`px-2 py-0.5 rounded-full border text-xs transition-colors ${
                      selected
                        ? "bg-primary text-primary-foreground border-primary"
                        : "bg-background text-muted-foreground border-border hover:border-primary/50"
                    }`}
                  >
                    {g.name}
                  </button>
                );
              })}
            </div>
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

        {/* Additional Zones — always shown (even with just a primary picked)
            so the user can push A records into extra zones. */}
        {availableZones.length > 0 && (
          <div>
            <p className="text-xs text-muted-foreground mb-1">
              Additional Zones
            </p>
            <AdditionalZonesPicker
              allZones={availableZones.filter((z) => z.id !== displayZoneId)}
              selectedIds={displayAdditionalIds}
              onChange={(ids) => !inherit && onAdditionalZoneIdsChange(ids)}
              disabled={inherit}
            />
          </div>
        )}
      </fieldset>
    </div>
  );
}

// ─── Backfill Reverse Zones button ─────────────────────────────────────────

function BackfillReverseZonesButton({
  scope,
  id,
}: {
  scope: "space" | "block" | "subnet";
  id: string;
}) {
  const qc = useQueryClient();
  const [result, setResult] = useState<{
    created: { subnet: string; zone: string }[];
    skipped: number;
  } | null>(null);
  const mut = useMutation({
    mutationFn: () => {
      if (scope === "space") return ipamApi.backfillReverseZonesSpace(id);
      if (scope === "block") return ipamApi.backfillReverseZonesBlock(id);
      return ipamApi.backfillReverseZonesSubnet(id);
    },
    onSuccess: (data) => {
      setResult(data);
      qc.invalidateQueries({ queryKey: ["dns-zones"] });
    },
  });
  return (
    <>
      <button
        onClick={() => mut.mutate()}
        disabled={mut.isPending}
        title="Create missing in-addr.arpa / ip6.arpa zones for every subnet with a DNS group (idempotent)"
        className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
      >
        <RefreshCw
          className={cn("h-3.5 w-3.5", mut.isPending && "animate-spin")}
        />
        Backfill Reverse Zones
      </button>
      {result && (
        <Modal
          title="Reverse Zone Backfill"
          onClose={() => setResult(null)}
          wide
        >
          <div className="space-y-3">
            <p className="text-sm">
              Created{" "}
              <span className="font-medium">{result.created.length}</span> new
              reverse zone{result.created.length !== 1 ? "s" : ""};{" "}
              <span className="text-muted-foreground">
                {result.skipped} skipped
              </span>{" "}
              (already existed or no DNS group configured).
            </p>
            {result.created.length > 0 && (
              <div className="max-h-64 overflow-y-auto rounded-md border">
                <table className="w-full text-sm">
                  <thead className="bg-muted/40 text-xs">
                    <tr>
                      <th className="px-3 py-1.5 text-left">Subnet</th>
                      <th className="px-3 py-1.5 text-left">Reverse Zone</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.created.map((c) => (
                      <tr key={c.zone} className="border-t">
                        <td className="px-3 py-1 font-mono text-xs">
                          {c.subnet}
                        </td>
                        <td className="px-3 py-1 font-mono text-xs">
                          {c.zone}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="flex justify-end">
              <button
                onClick={() => setResult(null)}
                className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
              >
                Close
              </button>
            </div>
          </div>
        </Modal>
      )}
    </>
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
        <div className="h-40 overflow-y-auto rounded-b-md border bg-background">
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

  const { data: blocks } = useQuery({
    queryKey: ["blocks", spaceId],
    queryFn: () => ipamApi.listBlocks(spaceId),
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
  const [customFields, setCustomFields] = useState<Record<string, unknown>>({});
  const [dnsZoneId, setDnsZoneId] = useState<string>("");
  const [dhcpScopeId, setDhcpScopeId] = useState<string>("");
  const [aliases, setAliases] = useState<
    { name: string; record_type: "CNAME" | "A" }[]
  >([]);
  const [error, setError] = useState<string | null>(null);
  const needsDhcpScope = ipStatus === "dhcp" || ipStatus === "static_dhcp";

  const { data: dhcpScopes = [] } = useQuery({
    queryKey: ["dhcp-scopes-subnet", subnetId],
    queryFn: () => dhcpApi.listScopesBySubnet(subnetId),
    enabled: needsDhcpScope,
  });
  useEffect(() => {
    if (needsDhcpScope && !dhcpScopeId && dhcpScopes.length > 0) {
      setDhcpScopeId(dhcpScopes[0].id);
    }
  }, [needsDhcpScope, dhcpScopes.length]); // eslint-disable-line react-hooks/exhaustive-deps

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
    mutationFn: async () => {
      const zoneParam = dnsZoneId || undefined;
      const cleanedAliases = aliases
        .map((a) => ({ ...a, name: a.name.trim() }))
        .filter((a) => a.name.length > 0);
      const created =
        mode === "next"
          ? await ipamApi.nextAddress(subnetId, {
              hostname,
              status: ipStatus,
              mac_address: mac || undefined,
              description: description || undefined,
              custom_fields: customFields,
              dns_zone_id: zoneParam,
              aliases: cleanedAliases.length ? cleanedAliases : undefined,
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
              aliases: cleanedAliases.length ? cleanedAliases : undefined,
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
      qc.invalidateQueries({ queryKey: ["subnet-aliases", subnetId] });
      qc.invalidateQueries({ queryKey: ["subnets"] });
      onClose();
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to allocate address";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  const canSubmit = !!hostname.trim() && (mode === "next" || !!address);

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
                    {" — server "}
                    {sc.server_id?.slice(0, 8) ?? "unassigned"}
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

// ─── Subnet Detail Panel (right pane) ────────────────────────────────────────

function SubnetDetail({
  subnet,
  spaceName,
  block,
  blockAncestors,
  onSelectSpace,
  onSelectBlock,
  onSubnetEdited,
  onSubnetDeleted,
}: {
  subnet: Subnet;
  spaceName?: string;
  block?: IPBlock;
  blockAncestors?: IPBlock[];
  onSelectSpace?: () => void;
  onSelectBlock?: (b: IPBlock) => void;
  onSubnetEdited: (updated: Subnet) => void;
  onSubnetDeleted?: () => void;
}) {
  const qc = useQueryClient();
  const [showAddModal, setShowAddModal] = useState(false);
  const [showEditSubnet, setShowEditSubnet] = useState(false);
  const [showDnsSync, setShowDnsSync] = useState(false);
  const [showOrphans, setShowOrphans] = useState(false);
  const [editingAddress, setEditingAddress] = useState<IPAddress | null>(null);
  const [showFilters, setShowFilters] = useState(false);
  const [activeSubnetTab, setActiveSubnetTab] = useState<
    "addresses" | "dhcp" | "aliases"
  >("addresses");
  const [selectedIpIds, setSelectedIpIds] = useState<Set<string>>(new Set());
  const [showBulkEdit, setShowBulkEdit] = useState(false);
  const [showBulkDelete, setShowBulkDelete] = useState(false);

  type FilterMode = "contains" | "begins" | "ends" | "regex";
  const [colFilters, setColFilters] = useState({
    address: "",
    hostname: "",
    mac: "",
    description: "",
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
    const macNorm = (a.mac_address ?? "").replace(/[:\-.]/g, "");
    const macFilter = cf.mac.replace(/[:\-.]/g, "");
    if (!applyFilter(macNorm, macFilter, fm.mac)) return false;
    if (!applyFilter(a.description ?? "", cf.description, fm.description))
      return false;
    if (cf.status && a.status !== cf.status) return false;
    if (cf.dns && ipDnsState(a) !== cf.dns) return false;
    return true;
  });
  const hasActiveFilter = Object.values(colFilters).some(Boolean);

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
      qc.invalidateQueries({ queryKey: ["subnets"] });
    },
  });

  const purgeAddr = useMutation({
    mutationFn: (id: string) => ipamApi.deleteAddress(id, true), // permanent
    onSuccess: () => {
      setConfirmPurgeAddr(null);
      qc.invalidateQueries({ queryKey: ["addresses", subnet.id] });
      qc.invalidateQueries({ queryKey: ["subnets"] });
    },
  });

  const restoreAddr = useMutation({
    mutationFn: (id: string) =>
      ipamApi.updateAddress(id, { status: "allocated" }),
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
            <button
              onClick={() => setShowDnsSync(true)}
              title="Compare IPAM-managed DNS records against the actual DB and reconcile any drift"
              className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              <Globe2 className="h-3.5 w-3.5" />
              Check DNS Sync
            </button>
            <BackfillReverseZonesButton scope="subnet" id={subnet.id} />
            <button
              onClick={() => setShowOrphans(true)}
              title="List orphaned IPs in this subnet and permanently delete selected rows"
              className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Clean Orphans
            </button>
            <ExportButton scope={{ subnet_id: subnet.id }} label="Export" />
            <button
              onClick={() => setShowEditSubnet(true)}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              Edit
            </button>
            <button
              onClick={() => setShowAddModal(true)}
              className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
            >
              <Plus className="h-3.5 w-3.5" />
              Allocate IP
            </button>
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
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-sm">
                  <thead>
                    <tr className="border-b bg-muted/40 text-xs">
                      <th className="w-8 px-2 py-2">
                        {(() => {
                          const selectable = (filteredAddresses ?? []).filter(
                            (a: IPAddress) =>
                              a.status !== "network" &&
                              a.status !== "broadcast",
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
                      <th className="px-4 py-2 text-right">
                        {hasActiveFilter && (
                          <button
                            onClick={() => {
                              setColFilters({
                                address: "",
                                hostname: "",
                                mac: "",
                                description: "",
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
                        <td />
                      </tr>
                    )}
                  </thead>
                  <tbody>
                    {filteredAddresses?.length === 0 && (
                      <tr>
                        <td
                          colSpan={9}
                          className="px-4 py-6 text-center text-sm text-muted-foreground"
                        >
                          No addresses match the active filters.
                        </td>
                      </tr>
                    )}
                    {filteredAddresses?.map((addr: IPAddress) => {
                      const dnsState = ipDnsState(addr);
                      const systemRow =
                        addr.status === "network" ||
                        addr.status === "broadcast";
                      const rowSelected = selectedIpIds.has(addr.id);
                      return (
                        <tr
                          key={addr.id}
                          className={cn(
                            "group/addr border-b last:border-0 hover:bg-muted/20",
                            (addr.status === "network" ||
                              addr.status === "broadcast") &&
                              "opacity-50",
                            addr.status === "orphan" && "opacity-40",
                            rowSelected && "bg-primary/5",
                          )}
                        >
                          <td className="w-8 px-2 py-2">
                            {!systemRow && (
                              <input
                                type="checkbox"
                                checked={rowSelected}
                                aria-label={`Select ${addr.address}`}
                                onChange={(e) => {
                                  setSelectedIpIds((prev) => {
                                    const next = new Set(prev);
                                    if (e.target.checked) next.add(addr.id);
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
                                  {addr.alias_count === 1 ? "alias" : "aliases"}
                                </span>
                              )}
                            </span>
                          </td>
                          <td className="px-4 py-2 font-mono text-xs">
                            {addr.mac_address ?? (
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
                            <StatusBadge status={addr.status} />
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
                                title="DNS records are missing or differ — open Check DNS Sync to reconcile"
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
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}

      {showAddModal && (
        <AddAddressModal
          subnetId={subnet.id}
          onClose={() => setShowAddModal(false)}
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
      {showDnsSync && (
        <DnsSyncModal
          scope={{ kind: "subnet", id: subnet.id, label: subnet.network }}
          onClose={() => setShowDnsSync(false)}
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

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["dns-sync-preview", scope.kind, scope.id],
    queryFn: fetchPreview,
    refetchOnMount: "always",
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
      qc.invalidateQueries({
        queryKey: ["dns-sync-preview", scope.kind, scope.id],
      });
      qc.invalidateQueries({ queryKey: ["addresses"] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
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

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-2 sm:p-4">
      <div className="w-full max-w-[95vw] sm:max-w-3xl rounded-lg border bg-card shadow-lg flex flex-col max-h-[85vh]">
        <div className="flex items-center justify-between border-b px-5 py-3">
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
          {isLoading && (
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
        status,
        custom_fields: customFields,
        dns_inherit_settings: dnsInherit,
        dns_group_ids: dnsInherit ? null : dnsGroupIds,
        dns_zone_id: dnsInherit ? null : dnsZoneId,
        dns_additional_zone_ids: dnsInherit ? null : dnsAdditionalZoneIds,
        ...(manageAuto !== undefined
          ? { manage_auto_addresses: manageAuto }
          : {}),
      });
    },
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ["subnets", subnet.space_id] });
      // Invalidate addresses so network/broadcast changes are reflected immediately
      qc.invalidateQueries({ queryKey: ["addresses", subnet.id] });
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

  const deleteMutation = useMutation({
    mutationFn: () => ipamApi.deleteSubnet(subnet.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnets", subnet.space_id] });
      qc.invalidateQueries({ queryKey: ["blocks", subnet.space_id] });
      onDeleted?.();
    },
  });

  // ── Delete step 1 ──
  if (deleteStep === 1) {
    return (
      <Modal title="Delete Subnet" onClose={() => setDeleteStep(0)}>
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Are you sure you want to delete subnet{" "}
            <strong className="font-mono text-foreground">
              {subnet.network}
            </strong>
            {subnet.name ? ` (${subnet.name})` : ""}? All IP address records
            within it will be permanently deleted.
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
            All IP address records within{" "}
            <strong className="font-mono text-foreground">
              {subnet.network}
            </strong>{" "}
            will be permanently removed.
          </p>
          <label className="flex cursor-pointer items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={deleteChecked}
              onChange={(e) => setDeleteChecked(e.target.checked)}
            />
            I understand all IP addresses in this subnet will be permanently
            deleted.
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
  const [customFields, setCustomFields] = useState<Record<string, unknown>>(
    (address.custom_fields as Record<string, unknown>) ?? {},
  );
  const [dnsZoneId, setDnsZoneId] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

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
    mutationFn: () =>
      ipamApi.updateAddress(address.id, {
        hostname: hostname || undefined,
        description: description || undefined,
        mac_address: macAddress || undefined,
        status,
        custom_fields: customFields,
        dns_zone_id: dnsZoneId || undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["addresses", address.subnet_id] });
      onClose();
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to save";
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
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ─── Aliases Subnet Panel ────────────────────────────────────────────────────

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
        <tbody>
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
      } = {};
      if (editStatus) changes.status = status;
      if (editDescription) changes.description = description;

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
    editDnsZone;

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

/** Two-step destruction modal: step 1 confirms intent, step 2 requires checkbox. */
function ConfirmDestroyModal({
  title,
  description,
  checkLabel,
  onConfirm,
  onClose,
  isPending,
}: {
  title: string;
  description: string;
  checkLabel: string;
  onConfirm: () => void;
  onClose: () => void;
  isPending?: boolean;
}) {
  const [step, setStep] = useState<1 | 2>(1);
  const [checked, setChecked] = useState(false);

  if (step === 1) {
    return (
      <Modal title={title} onClose={onClose}>
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">{description}</p>
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

  const saveMutation = useMutation({
    mutationFn: () =>
      ipamApi.updateSpace(space.id, {
        name,
        description,
        dns_group_ids: dnsGroupIds,
        dns_zone_id: dnsZoneId,
        dns_additional_zone_ids: dnsAdditionalZoneIds,
      }),
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
      subnets: subnets.filter((s) => s.block_id === b.id),
    }));
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

  const { data: existingBlocks } = useQuery({
    queryKey: ["blocks", spaceId],
    queryFn: () => ipamApi.listBlocks(spaceId),
  });

  const { data: cfDefs = [] } = useQuery({
    queryKey: ["custom-fields", "ip_block"],
    queryFn: () => customFieldsApi.list("ip_block"),
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

  const deleteMutation = useMutation({
    mutationFn: () => ipamApi.deleteBlock(block.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["blocks", block.space_id] });
      qc.invalidateQueries({ queryKey: ["blocks"] });
      onDeleted?.();
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
              onDeleteBlock={onDeleteBlock}
              onEditBlock={onEditBlock}
              onCreateSubnet={onCreateSubnet}
              onCreateChildBlock={onCreateChildBlock}
              onAllocateIp={onAllocateIp}
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
              onAllocateIp={onAllocateIp}
            />
          ))}
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
  const [block, setBlock] = useState(initialBlock);
  const [showEdit, setShowEdit] = useState(false);
  const [showCreateSubnet, setShowCreateSubnet] = useState(false);
  const [showCreateChildBlock, setShowCreateChildBlock] = useState(false);
  const [blockFilter, setBlockFilter] = useState({
    network: "",
    name: "",
    router: "",
    vlan: "",
    status: "",
  });
  const [showBlockFilters, setShowBlockFilters] = useState(false);
  const [selectedSubnets, setSelectedSubnets] = useState<Set<string>>(
    new Set(),
  );
  const [showBulkEdit, setShowBulkEdit] = useState(false);
  const [showBulkDelete, setShowBulkDelete] = useState(false);
  const [showDnsSync, setShowDnsSync] = useState(false);

  const qc = useQueryClient();

  const blockBulkDeleteMut = useMutation({
    mutationFn: () =>
      Promise.all(
        Array.from(selectedSubnets).map((id) => ipamApi.deleteSubnet(id)),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnets", block.space_id] });
      qc.invalidateQueries({ queryKey: ["blocks", block.space_id] });
      setSelectedSubnets(new Set());
      setShowBulkDelete(false);
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
            {selectedSubnets.size > 0 ? (
              <>
                <button
                  onClick={() => setShowBulkEdit(true)}
                  className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
                >
                  Bulk Edit ({selectedSubnets.size})
                </button>
                <button
                  onClick={() => setShowBulkDelete(true)}
                  className="rounded-md border border-destructive/50 px-3 py-1.5 text-sm text-destructive hover:bg-destructive/10"
                >
                  Delete ({selectedSubnets.size})
                </button>
                <button
                  onClick={() => setSelectedSubnets(new Set())}
                  className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
                >
                  Clear
                </button>
              </>
            ) : (
              <>
                <button
                  onClick={() => setShowDnsSync(true)}
                  title="Reconcile IPAM-managed DNS records across every subnet under this block"
                  className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
                >
                  <Globe2 className="h-3.5 w-3.5" />
                  Check DNS Sync
                </button>
                <BackfillReverseZonesButton scope="block" id={block.id} />
                <ExportButton scope={{ block_id: block.id }} label="Export" />
                <button
                  onClick={() => setShowEdit(true)}
                  className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
                >
                  Edit
                </button>
                <button
                  onClick={() => setShowCreateChildBlock(true)}
                  className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
                >
                  <span className="flex items-center gap-1.5">
                    <Layers className="h-3.5 w-3.5" />
                    Add Block
                  </span>
                </button>
                <button
                  onClick={() => setShowCreateSubnet(true)}
                  className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90"
                >
                  <Plus className="h-3.5 w-3.5" />
                  New Subnet
                </button>
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
          <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            Allocation map
          </p>
          <FreeSpaceBand
            block={block}
            directSubnets={directSubnets}
            childBlocks={directChildBlocks}
            onSelectFree={(range) => {
              setFreeRangePreset(range);
              setShowCreateSubnet(true);
            }}
          />
        </div>
      </div>
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
          subnetIds={Array.from(selectedSubnets)}
          onClose={() => setShowBulkEdit(false)}
          onDone={() => {
            setShowBulkEdit(false);
            setSelectedSubnets(new Set());
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
      {showBulkDelete && (
        <ConfirmDestroyModal
          title={`Delete ${selectedSubnets.size} Subnet${selectedSubnets.size === 1 ? "" : "s"}`}
          description={`This will permanently delete ${selectedSubnets.size} subnet${selectedSubnets.size === 1 ? "" : "s"} and all IP address records within them.`}
          checkLabel="I understand all IP addresses in these subnets will be permanently deleted."
          isPending={blockBulkDeleteMut.isPending}
          onClose={() => setShowBulkDelete(false)}
          onConfirm={() => blockBulkDeleteMut.mutate()}
        />
      )}
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
                        const subnetIds = allRows
                          .filter((r) => r.type === "subnet" && r.subnet)
                          .map((r) => r.subnet!.id);
                        const allSelected =
                          subnetIds.length > 0 &&
                          subnetIds.every((id) => selectedSubnets.has(id));
                        return (
                          <input
                            type="checkbox"
                            aria-label="Select all subnets"
                            checked={allSelected}
                            onChange={() =>
                              setSelectedSubnets(
                                allSelected ? new Set() : new Set(subnetIds),
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
                <tbody>
                  {allRows.map((item) => {
                    const indent = item.depth * 20;
                    if (item.type === "block" && item.block) {
                      const b = item.block;
                      return (
                        <tr
                          key={item.key}
                          onClick={() => onSelectBlock(b)}
                          className="border-b last:border-0 cursor-pointer hover:bg-muted/30 bg-muted/10"
                        >
                          <td className="w-8 px-2 py-2" />
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
                            selectedSubnets.has(s.id) && "bg-primary/5",
                          )}
                        >
                          <td
                            className="w-8 px-2 py-2"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <input
                              type="checkbox"
                              aria-label={`Select ${s.network}`}
                              checked={selectedSubnets.has(s.id)}
                              onChange={() =>
                                setSelectedSubnets((prev) => {
                                  const next = new Set(prev);
                                  if (next.has(s.id)) next.delete(s.id);
                                  else next.add(s.id);
                                  return next;
                                })
                              }
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

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkOpen, setBulkOpen] = useState(false);
  const [showBulkDelete, setShowBulkDelete] = useState(false);
  const [showEditSpace, setShowEditSpace] = useState(false);
  const [showCreateBlock, setShowCreateBlock] = useState(false);
  const [showCreateSubnet, setShowCreateSubnet] = useState(false);
  const [showSpaceFilters, setShowSpaceFilters] = useState(false);
  const [spaceFilter, setSpaceFilter] = useState({
    network: "",
    name: "",
    router: "",
    vlan: "",
    status: "",
  });
  const [showDnsSync, setShowDnsSync] = useState(false);

  const bulkDeleteMut = useMutation({
    mutationFn: () =>
      Promise.all(Array.from(selected).map((id) => ipamApi.deleteSubnet(id))),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnets", space.id] });
      qc.invalidateQueries({ queryKey: ["blocks", space.id] });
      setSelected(new Set());
      setShowBulkDelete(false);
    },
  });

  const toggleOne = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const isLoading = blocksLoading || subnetsLoading;
  const rows =
    blocks && subnets
      ? flattenToTableRows(buildBlockTree(blocks, subnets, null))
      : [];

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

  const subnetIdsInView = filteredSpaceRows
    .filter((r) => r.type === "subnet" && r.subnet)
    .map((r) => r.subnet!.id);
  const allSelected =
    subnetIdsInView.length > 0 &&
    subnetIdsInView.every((id) => selected.has(id));
  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(subnetIdsInView));

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="border-b px-6 py-3">
        <div className="flex items-center justify-between gap-4 pb-2">
          <BreadcrumbPills items={[{ label: space.name, variant: "space" }]} />
          <div className="flex flex-shrink-0 items-center gap-2">
            {selected.size > 0 && (
              <>
                <button
                  onClick={() => setBulkOpen(true)}
                  className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
                >
                  Bulk Edit ({selected.size})
                </button>
                <button
                  onClick={() => setShowBulkDelete(true)}
                  className="rounded-md border border-destructive/50 px-3 py-1.5 text-sm text-destructive hover:bg-destructive/10"
                >
                  Delete ({selected.size})
                </button>
              </>
            )}
            <button
              onClick={() => setShowDnsSync(true)}
              title="Reconcile IPAM-managed DNS records across every subnet in this space"
              className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              <Globe2 className="h-3.5 w-3.5" />
              Check DNS Sync
            </button>
            <BackfillReverseZonesButton scope="space" id={space.id} />
            <ExportButton scope={{ space_id: space.id }} label="Export" />
            <button
              onClick={() => setShowEditSpace(true)}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              Edit Space
            </button>
            <button
              onClick={() => setShowCreateBlock(true)}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              <span className="flex items-center gap-1.5">
                <Layers className="h-3.5 w-3.5" />
                Add Block
              </span>
            </button>
            <button
              onClick={() => setShowCreateSubnet(true)}
              className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
            >
              <Plus className="h-3.5 w-3.5" />
              Add Subnet
            </button>
          </div>
        </div>
        <div>
          <h2 className="text-base font-semibold">{space.name}</h2>
          {space.description && (
            <p className="text-xs text-muted-foreground">{space.description}</p>
          )}
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
                      aria-label="Select all subnets"
                      checked={allSelected}
                      onChange={toggleAll}
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
              <tbody>
                {filteredSpaceRows.map((item) => {
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
                        <td className="w-8 px-2 py-2" />
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
                            checked={selected.has(s.id)}
                            onChange={() => toggleOne(s.id)}
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
          subnetIds={Array.from(selected)}
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
          onClose={() => setShowCreateSubnet(false)}
        />
      )}
      {showBulkDelete && (
        <ConfirmDestroyModal
          title={`Delete ${selected.size} Subnet${selected.size === 1 ? "" : "s"}`}
          description={`This will permanently delete ${selected.size} subnet${selected.size === 1 ? "" : "s"} and all IP address records within them.`}
          checkLabel={`I understand all IP addresses in these subnets will be permanently deleted.`}
          isPending={bulkDeleteMut.isPending}
          onClose={() => setShowBulkDelete(false)}
          onConfirm={() => bulkDeleteMut.mutate()}
        />
      )}
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

  const deleteSubnet = useMutation({
    mutationFn: (id: string) => ipamApi.deleteSubnet(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subnets", space.id] });
      setSubnetToDelete(null);
    },
  });

  const deleteBlockMut = useMutation({
    mutationFn: (id: string) => ipamApi.deleteBlock(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["blocks", space.id] });
      qc.invalidateQueries({ queryKey: ["subnets", space.id] });
      setBlockToDelete(null);
    },
    onError: (err: unknown) => {
      const msg = (err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      setDndError(typeof msg === "string" ? msg : "Failed to delete block");
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
      <div
        className={cn(
          "group flex items-center gap-1 rounded-md px-1 py-1.5 hover:bg-muted/50",
          isSpaceSelected && "bg-primary/5",
        )}
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
          description={`Delete block ${blockToDelete.network}${blockToDelete.name ? ` (${blockToDelete.name})` : ""}? All nested blocks and subnets inside it will be permanently deleted.`}
          checkLabel={`I understand everything inside ${blockToDelete.network} will be permanently deleted.`}
          isPending={deleteBlockMut.isPending}
          onClose={() => setBlockToDelete(null)}
          onConfirm={() => deleteBlockMut.mutate(blockToDelete.id)}
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
          description={`Delete subnet ${subnetToDelete.network}${subnetToDelete.name ? ` (${subnetToDelete.name})` : ""}? All IP address records within it will be permanently deleted.`}
          checkLabel={`I understand all IP addresses in ${subnetToDelete.network} will be permanently deleted.`}
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
        selectSubnet(sn);
        deepLinkHandled.current = true;
        urlRestored.current = true;
      }
    }
  }, [location.state, spaces, allBlocks, allSubnets]);

  // URL-state restore: reopen last-visited space/block/subnet on back-navigation
  useEffect(() => {
    if (urlRestored.current) return;
    if (!spaces || !allBlocks || !allSubnets) return;
    urlRestored.current = true;
    const subnetId = searchParams.get("subnet");
    const blockId = searchParams.get("block");
    const spaceId = searchParams.get("space");
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
  }, [spaces, allBlocks, allSubnets]);

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
              onClick={() => qc.invalidateQueries({ queryKey: ["spaces"] })}
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
