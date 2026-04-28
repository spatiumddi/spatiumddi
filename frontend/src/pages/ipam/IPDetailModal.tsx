import { Copy, ExternalLink, Pencil, Radar, Trash2, X } from "lucide-react";

import { type IPAddress, type Subnet } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  MODAL_BACKDROP_CLS,
  useDraggableModal,
} from "@/components/ui/use-draggable-modal";
import { IPNetworkTab } from "./IPNetworkTab";

// ── Helpers ──────────────────────────────────────────────────────────

const STATUS_COLORS: Record<string, string> = {
  active:
    "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  reserved: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
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
  broadcast: "bg-zinc-100 text-zinc-500 dark:bg-zinc-800/50 dark:text-zinc-400",
  orphan:
    "bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400",
};

function copy(text: string) {
  void navigator.clipboard.writeText(text);
}

function fmtTs(ts?: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function Field({
  label,
  children,
  mono,
}: {
  label: string;
  children: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div>
      <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className={cn("mt-0.5 text-sm", mono && "font-mono", "break-words")}>
        {children}
      </div>
    </div>
  );
}

function dash(v: unknown) {
  if (v === null || v === undefined || v === "") {
    return <span className="text-muted-foreground/50">—</span>;
  }
  return v as React.ReactNode;
}

// ── Modal ────────────────────────────────────────────────────────────

export interface IPDetailModalProps {
  address: IPAddress;
  subnet?: Subnet | null;
  zoneNameById?: Record<string, string>;
  canEdit: boolean;
  onClose: () => void;
  onEdit: () => void;
  onScan: () => void;
  onDelete?: () => void;
}

/**
 * Read-only detail surface for an IP. Opens on row-click; from here the
 * operator can launch a scan or hop into the editor. Tries hard to keep
 * useful details visible at a glance — the form is one click away via
 * the Edit button if anything needs changing.
 */
export function IPDetailModal({
  address: addr,
  subnet,
  zoneNameById,
  canEdit,
  onClose,
  onEdit,
  onScan,
  onDelete,
}: IPDetailModalProps) {
  const { dialogStyle, dragHandleProps } = useDraggableModal(onClose);
  const zoneNames = zoneNameById ?? {};

  const tagEntries = Object.entries(addr.tags ?? {});
  const cfEntries = Object.entries(addr.custom_fields ?? {});

  return (
    <div className={MODAL_BACKDROP_CLS}>
      <div
        className="w-full rounded-lg border bg-card shadow-lg max-h-[90vh] overflow-y-auto max-w-[95vw] sm:max-w-3xl"
        style={dialogStyle}
      >
        {/* Header */}
        <div
          {...dragHandleProps}
          className={cn(
            "flex items-start justify-between gap-3 border-b p-4 sm:p-5",
            dragHandleProps.className,
          )}
        >
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="font-mono text-xl font-semibold">
                {addr.address}
              </h2>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  copy(addr.address);
                }}
                className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                title="Copy IP"
              >
                <Copy className="h-3.5 w-3.5" />
              </button>
              <span
                className={cn(
                  "rounded-full px-2 py-0.5 text-xs font-medium",
                  STATUS_COLORS[addr.status] ??
                    "bg-muted text-muted-foreground",
                )}
              >
                {addr.status}
              </span>
              {addr.role && (
                <span className="inline-flex items-center rounded bg-indigo-100 px-1.5 py-0.5 text-[11px] font-medium text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400">
                  {addr.role}
                </span>
              )}
              {addr.auto_from_lease && (
                <span className="inline-flex items-center rounded bg-cyan-100 px-1.5 py-0.5 text-[11px] font-medium text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-400">
                  DHCP-mirror
                </span>
              )}
            </div>
            {addr.fqdn ? (
              <div className="mt-1 font-mono text-xs text-muted-foreground">
                {addr.fqdn}
              </div>
            ) : addr.hostname ? (
              <div className="mt-1 text-xs text-muted-foreground">
                {addr.hostname}
              </div>
            ) : null}
          </div>
          <div
            className="flex flex-shrink-0 items-center gap-2"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              type="button"
              onClick={onScan}
              className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs hover:bg-accent"
              title="Run an nmap scan against this IP"
            >
              <Radar className="h-3.5 w-3.5" /> Scan with Nmap
            </button>
            {canEdit && (
              <button
                type="button"
                onClick={onEdit}
                className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs hover:bg-accent"
              >
                <Pencil className="h-3.5 w-3.5" /> Edit
              </button>
            )}
            {canEdit && onDelete && (
              <button
                type="button"
                onClick={onDelete}
                className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-destructive hover:bg-destructive/10"
              >
                <Trash2 className="h-3.5 w-3.5" /> Delete
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="rounded p-1 text-muted-foreground hover:text-foreground"
              title="Close"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="space-y-5 p-4 sm:p-5">
          {/* Identity grid */}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field label="Hostname">{dash(addr.hostname)}</Field>
            <Field label="FQDN" mono>
              {dash(addr.fqdn)}
            </Field>
            <Field label="MAC address" mono>
              {addr.mac_address ? (
                <span className="inline-flex items-center gap-1.5">
                  {addr.mac_address}
                  {addr.vendor && (
                    <span className="font-sans text-[11px] text-muted-foreground">
                      {addr.vendor}
                    </span>
                  )}
                </span>
              ) : (
                dash(null)
              )}
            </Field>
            <Field label="Subnet" mono>
              {subnet ? (
                <span>
                  {subnet.network}
                  {subnet.name && (
                    <span className="ml-2 font-sans text-xs text-muted-foreground">
                      {subnet.name}
                    </span>
                  )}
                </span>
              ) : (
                dash(null)
              )}
            </Field>
            <Field label="Description">{dash(addr.description)}</Field>
            <Field label="Reserved until">
              {addr.reserved_until ? fmtTs(addr.reserved_until) : dash(null)}
            </Field>
            <Field label="Last seen">
              <span title={addr.last_seen_at ?? ""}>
                {addr.last_seen_at ? (
                  <>
                    {fmtTs(addr.last_seen_at)}
                    {addr.last_seen_method && (
                      <span className="ml-2 text-[11px] text-muted-foreground">
                        via {addr.last_seen_method}
                      </span>
                    )}
                  </>
                ) : (
                  dash(null)
                )}
              </span>
            </Field>
            <Field label="Forward DNS zone">
              {addr.forward_zone_id ? (
                <span className="font-mono text-xs">
                  {zoneNames[addr.forward_zone_id] ?? addr.forward_zone_id}
                </span>
              ) : (
                dash(null)
              )}
            </Field>
            <Field label="Reverse DNS zone">
              {addr.reverse_zone_id ? (
                <span className="font-mono text-xs">
                  {zoneNames[addr.reverse_zone_id] ?? addr.reverse_zone_id}
                </span>
              ) : (
                dash(null)
              )}
            </Field>
            <Field label="DNS / DHCP linkage">
              <span className="space-x-2 text-[11px] text-muted-foreground">
                {addr.dns_record_id && <span>A-record</span>}
                {addr.dhcp_lease_id && <span>· DHCP lease</span>}
                {addr.static_assignment_id && <span>· DHCP static</span>}
                {!addr.dns_record_id &&
                  !addr.dhcp_lease_id &&
                  !addr.static_assignment_id &&
                  dash(null)}
              </span>
            </Field>
          </div>

          {/* Counters row */}
          {(addr.alias_count || addr.nat_mapping_count) && (
            <div className="flex flex-wrap items-center gap-2 text-[11px]">
              {(addr.alias_count ?? 0) > 0 && (
                <span className="inline-flex items-center rounded bg-indigo-100 px-1.5 py-0.5 font-medium text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400">
                  +{addr.alias_count} alias
                  {addr.alias_count === 1 ? "" : "es"}
                </span>
              )}
              {(addr.nat_mapping_count ?? 0) > 0 && (
                <span className="inline-flex items-center rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
                  NAT {addr.nat_mapping_count}
                </span>
              )}
            </div>
          )}

          {/* Tags */}
          {tagEntries.length > 0 && (
            <div>
              <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                Tags
              </div>
              <div className="flex flex-wrap gap-1">
                {tagEntries.map(([k, v]) => (
                  <span
                    key={k}
                    className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-[11px]"
                  >
                    <span className="font-medium">{k}</span>
                    {v !== true && v !== "" && (
                      <span className="ml-1 text-muted-foreground">
                        {String(v)}
                      </span>
                    )}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Custom fields */}
          {cfEntries.length > 0 && (
            <div>
              <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                Custom fields
              </div>
              <div className="rounded-md border">
                <table className="w-full text-xs">
                  <tbody>
                    {cfEntries.map(([k, v]) => (
                      <tr key={k} className="border-b last:border-0 align-top">
                        <td className="w-1/3 px-2 py-1 text-muted-foreground">
                          {k}
                        </td>
                        <td className="px-2 py-1 break-words">
                          {v === null || v === undefined || v === ""
                            ? dash(null)
                            : typeof v === "object"
                              ? JSON.stringify(v)
                              : String(v)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Network discovery (FDB) */}
          <div>
            <div className="mb-1 flex items-center gap-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              <ExternalLink className="h-3 w-3" /> Network discovery
            </div>
            <IPNetworkTab addressId={addr.id} />
          </div>
        </div>
      </div>
    </div>
  );
}
