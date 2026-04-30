import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Copy,
  ExternalLink,
  Loader2,
  Pencil,
  Radar,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";

import {
  ipamApi,
  nmapApi,
  type DHCPFingerprintResponse,
  type IPAddress,
  type NmapScanRead,
  type Subnet,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  MODAL_BACKDROP_CLS,
  useDraggableModal,
} from "@/components/ui/use-draggable-modal";
import { IPNetworkTab } from "./IPNetworkTab";
import { SeenDot } from "./SeenDot";

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
  // ``discovered`` — passive observation only, no operator intent yet.
  // Sky tone keeps it visually distinct from ``available`` (green) and
  // ``allocated`` (purple); the orthogonal alive dot in the IPAM table
  // tells the operator whether the discovered row is currently up.
  discovered: "bg-sky-100 text-sky-800 dark:bg-sky-900/30 dark:text-sky-400",
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
              <SeenDot
                lastSeenAt={addr.last_seen_at}
                lastSeenMethod={addr.last_seen_method}
                size="md"
              />
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

          {/* Device profile (active layer — Phase 1 nmap auto-profile) */}
          <DeviceProfileSection addr={addr} canEdit={canEdit} />
        </div>
      </div>
    </div>
  );
}

// ── Device profile ─────────────────────────────────────────────────────
//
// Shows the most recent successful nmap profile scan (OS guess + top
// open services) and surfaces a "Re-profile now" button. The button
// dispatches an ad-hoc scan via /ipam/addresses/{id}/profile — same
// pipeline as the lease-driven auto-profile, but with the refresh-window
// dedupe bypassed so the operator can force a fresh result on demand.
// Per-subnet concurrency cap still applies (returns 429 when full).
//
// Phase 2 (passive DHCP fingerprinting) will surface a sibling
// "Passive fingerprint" panel inside this section once shipped — the
// passive layer reads option-55/option-60 from incoming DHCP traffic
// and looks up the device class via fingerbank. For now Phase 1 owns
// the whole block.

function DeviceProfileSection({
  addr,
  canEdit,
}: {
  addr: IPAddress;
  canEdit: boolean;
}) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [showRawSig, setShowRawSig] = useState(false);
  // Track the scan dispatched by clicking "Re-profile now" so we can
  // keep the button in a "Scanning…" state until the worker finishes.
  // The mutation's own ``isPending`` only covers the dispatch HTTP
  // call (~ms), but the actual nmap run takes 30 s–2 min server-side.
  const [activeScanId, setActiveScanId] = useState<string | null>(null);
  // The most recent scan completed in *this* modal session. The parent
  // (IPAMPage) holds ``viewingAddress`` as a snapshot, so even after we
  // invalidate the addresses query the ``addr`` prop here doesn't see
  // the new ``last_profile_scan_id`` until the operator reopens the
  // modal. Caching the completed scan locally lets the panel refresh
  // immediately on terminal status without that round-trip.
  const [sessionScan, setSessionScan] = useState<NmapScanRead | null>(null);

  const scanQuery = useQuery({
    enabled: !!addr.last_profile_scan_id,
    queryKey: ["nmap-scan", addr.last_profile_scan_id],
    queryFn: () => nmapApi.getScan(addr.last_profile_scan_id as string),
    staleTime: 30_000,
  });

  // Poll the freshly-dispatched scan until it reaches a terminal
  // state. ``refetchInterval`` returns ``false`` to stop the poll
  // automatically; the effect below clears local state + invalidates
  // the parent queries so the device-profile panel refreshes.
  const activeScanQuery = useQuery({
    enabled: !!activeScanId,
    queryKey: ["nmap-scan-active", activeScanId],
    queryFn: () => nmapApi.getScan(activeScanId as string),
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      return s === "queued" || s === "running" ? 2000 : false;
    },
    refetchIntervalInBackground: false,
  });

  useEffect(() => {
    if (!activeScanId) return;
    const data = activeScanQuery.data;
    const status = data?.status;
    if (
      status === "completed" ||
      status === "failed" ||
      status === "cancelled"
    ) {
      // Cache the completed scan so the panel re-renders immediately
      // (the ``addr`` prop is parent-held + lags the DB).
      setSessionScan(data ?? null);
      qc.invalidateQueries({ queryKey: ["addresses", addr.subnet_id] });
      qc.invalidateQueries({
        queryKey: ["nmap-scan", addr.last_profile_scan_id],
      });
      qc.invalidateQueries({ queryKey: ["dhcp-fingerprint", addr.id] });
      if (status === "failed") {
        const msg = data?.error_message;
        setError(typeof msg === "string" && msg ? msg : "Scan failed");
      }
      setActiveScanId(null);
    }
  }, [
    activeScanId,
    activeScanQuery.data,
    addr.id,
    addr.subnet_id,
    addr.last_profile_scan_id,
    qc,
  ]);

  // Passive fingerprint — Phase 2. The endpoint 404s when no MAC or
  // no fingerprint has been observed; treat 404 as "no data" rather
  // than a hard error.
  const fingerprintQuery = useQuery({
    enabled: !!addr.mac_address,
    queryKey: ["dhcp-fingerprint", addr.id],
    queryFn: async () => {
      try {
        return await ipamApi.getDhcpFingerprint(addr.id);
      } catch (err) {
        const status = (err as { response?: { status?: number } })?.response
          ?.status;
        if (status === 404) return null;
        throw err;
      }
    },
    staleTime: 30_000,
  });

  const reprofile = useMutation({
    mutationFn: () => ipamApi.profileAddress(addr.id),
    onSuccess: (data) => {
      setError(null);
      // Hand the dispatched scan to the polling query so the spinner
      // tracks the actual worker run, not just the dispatch ack.
      setActiveScanId(data.scan_id);
      qc.invalidateQueries({ queryKey: ["nmap-scans"] });
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      setError(typeof detail === "string" ? detail : "Failed to dispatch scan");
    },
  });

  // Effective "scan in flight" state: covers the dispatch HTTP call
  // (mutation pending) AND the worker-side scan window (active scan
  // status is queued or running). ``activeScanLabel`` reflects what
  // the user sees alongside the spinner.
  const activeStatus = activeScanQuery.data?.status;
  const isScanning =
    reprofile.isPending ||
    (!!activeScanId &&
      (activeStatus === "queued" ||
        activeStatus === "running" ||
        activeStatus === undefined));
  const scanLabel = reprofile.isPending
    ? "Dispatching…"
    : activeStatus === "running"
      ? "Scanning…"
      : activeStatus === "queued"
        ? "Queued…"
        : isScanning
          ? "Scanning…"
          : "Re-profile now";

  const passiveType =
    addr.device_type ?? fingerprintQuery.data?.fingerbank_device_name ?? null;
  const passiveClass =
    addr.device_class ?? fingerprintQuery.data?.fingerbank_device_class ?? null;
  const passiveManufacturer =
    addr.device_manufacturer ??
    fingerprintQuery.data?.fingerbank_manufacturer ??
    null;
  const hasPassive =
    !!passiveType ||
    !!passiveClass ||
    !!passiveManufacturer ||
    !!fingerprintQuery.data;

  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <div className="flex items-center gap-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
          <Radar className="h-3 w-3" /> Device profile
        </div>
        {canEdit && (
          <button
            type="button"
            onClick={() => reprofile.mutate()}
            disabled={isScanning}
            className="inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] hover:bg-accent disabled:opacity-60"
            title={
              isScanning
                ? "A profile scan is currently running for this IP"
                : "Run a fresh nmap profile scan now"
            }
          >
            {isScanning ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <RefreshCw className="h-3 w-3" />
            )}
            {scanLabel}
          </button>
        )}
      </div>

      {error && (
        <div className="mb-2 rounded-md border border-destructive/40 bg-destructive/5 px-2 py-1 text-[11px] text-destructive">
          {error}
        </div>
      )}

      {/* Passive layer — DHCP fingerprint via fingerbank. */}
      {hasPassive && (
        <div className="mb-2 rounded-md border bg-muted/30 p-2.5">
          <div className="mb-1 flex items-center justify-between text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            <span>Passive fingerprint</span>
            {fingerprintQuery.data && (
              <button
                type="button"
                onClick={() => setShowRawSig((v) => !v)}
                className="text-[10px] font-normal normal-case tracking-normal text-muted-foreground/80 hover:text-foreground"
              >
                {showRawSig ? "hide" : "show"} raw signature
              </button>
            )}
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                Type
              </div>
              <div className="mt-0.5 text-sm">
                {passiveType ?? (
                  <span className="text-muted-foreground/50">—</span>
                )}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                Class
              </div>
              <div className="mt-0.5 text-sm">
                {passiveClass ?? (
                  <span className="text-muted-foreground/50">—</span>
                )}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                Manufacturer
              </div>
              <div className="mt-0.5 text-sm">
                {passiveManufacturer ?? (
                  <span className="text-muted-foreground/50">—</span>
                )}
              </div>
            </div>
          </div>
          {fingerprintQuery.data?.fingerbank_score != null && (
            <div className="mt-1 text-[11px] text-muted-foreground">
              fingerbank score {fingerprintQuery.data.fingerbank_score}/100
              {fingerprintQuery.data.fingerbank_last_lookup_at && (
                <>
                  {" · "}
                  looked up{" "}
                  {new Date(
                    fingerprintQuery.data.fingerbank_last_lookup_at,
                  ).toLocaleString()}
                </>
              )}
            </div>
          )}
          {fingerprintQuery.data?.fingerbank_last_error && (
            <div className="mt-1 text-[11px] text-destructive">
              fingerbank: {fingerprintQuery.data.fingerbank_last_error}
            </div>
          )}
          {showRawSig && fingerprintQuery.data && (
            <RawSignaturePanel fp={fingerprintQuery.data} />
          )}
        </div>
      )}

      {/* Active layer — nmap profile scan. ``sessionScan`` (the
          just-completed scan from this modal session) wins over the
          parent-held ``addr.last_profile_scan_id`` snapshot so the
          panel refreshes immediately on terminal status. */}
      {(() => {
        const displayScan = sessionScan ?? scanQuery.data ?? null;
        const displayProfiledAt =
          sessionScan?.finished_at ?? addr.last_profiled_at ?? null;
        const havePrior = !!addr.last_profile_scan_id || !!sessionScan;
        if (!havePrior && !isScanning) {
          return (
            <p className="text-xs text-muted-foreground italic">
              No active profile yet. Auto-profiling triggers on a fresh DHCP
              lease when the subnet's "Device profiling" toggle is enabled, or
              run one ad-hoc via the button above.
            </p>
          );
        }
        if (!havePrior) return null;
        return (
          <DeviceProfileScanPanel
            lastProfiledAt={displayProfiledAt}
            scan={displayScan}
            loading={scanQuery.isLoading && !sessionScan}
          />
        );
      })()}
    </div>
  );
}

function RawSignaturePanel({ fp }: { fp: DHCPFingerprintResponse }) {
  return (
    <div className="mt-2 space-y-1 border-t pt-2 font-mono text-[11px] text-muted-foreground">
      <div>
        <span className="text-muted-foreground/70">option-55</span>{" "}
        {fp.option_55 ?? "—"}
      </div>
      <div>
        <span className="text-muted-foreground/70">option-60</span>{" "}
        {fp.option_60 ?? "—"}
      </div>
      <div>
        <span className="text-muted-foreground/70">option-77</span>{" "}
        {fp.option_77 ?? "—"}
      </div>
      <div>
        <span className="text-muted-foreground/70">client-id</span>{" "}
        {fp.client_id ?? "—"}
      </div>
      <div>
        <span className="text-muted-foreground/70">first seen</span>{" "}
        {new Date(fp.first_seen_at).toLocaleString()}
      </div>
      <div>
        <span className="text-muted-foreground/70">last seen</span>{" "}
        {new Date(fp.last_seen_at).toLocaleString()}
      </div>
    </div>
  );
}

function DeviceProfileScanPanel({
  lastProfiledAt,
  scan,
  loading,
}: {
  lastProfiledAt: string | null;
  scan: NmapScanRead | null;
  loading: boolean;
}) {
  if (loading) {
    return (
      <p className="text-xs text-muted-foreground">
        <Loader2 className="mr-1 inline h-3 w-3 animate-spin" />
        Loading profile…
      </p>
    );
  }
  if (!scan) {
    return (
      <p className="text-xs text-muted-foreground italic">
        Profile scan unavailable (deleted or not yet readable).
      </p>
    );
  }

  // Cap services at 8 to keep the modal readable. Operators who need
  // the full list can deep-link into the nmap surface from the row id.
  const openPorts = (scan.summary?.ports ?? [])
    .filter((p) => p.state === "open")
    .slice(0, 8);
  const os = scan.summary?.os ?? null;

  return (
    <div className="space-y-2 rounded-md border bg-muted/30 p-2.5">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        <span>
          Last scanned{" "}
          {lastProfiledAt
            ? new Date(lastProfiledAt).toLocaleString()
            : new Date(scan.finished_at ?? scan.created_at).toLocaleString()}
        </span>
        <span>·</span>
        <span>preset {scan.preset}</span>
        <span>·</span>
        <span
          className={cn(
            "rounded px-1.5 py-0.5 font-medium",
            scan.status === "completed"
              ? "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"
              : scan.status === "running" || scan.status === "queued"
                ? "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400"
                : "bg-zinc-100 text-zinc-700 dark:bg-zinc-800/50 dark:text-zinc-400",
          )}
        >
          {scan.status}
        </span>
      </div>

      <div>
        <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
          OS guess
        </div>
        <div className="mt-0.5 text-sm">
          {os?.name ? (
            <>
              {os.name}
              {os.accuracy != null && (
                <span className="ml-2 text-[11px] text-muted-foreground">
                  {os.accuracy}% confidence
                </span>
              )}
            </>
          ) : (
            <span className="text-muted-foreground/50">—</span>
          )}
        </div>
      </div>

      <div>
        <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
          Open services
        </div>
        {openPorts.length === 0 ? (
          <div className="mt-0.5 text-sm text-muted-foreground/50">—</div>
        ) : (
          <ul className="mt-0.5 space-y-0.5 text-xs">
            {openPorts.map((p) => (
              <li key={`${p.proto}-${p.port}`} className="font-mono">
                <span>
                  {p.port}/{p.proto}
                </span>
                {p.service && (
                  <span className="ml-2 text-muted-foreground">
                    {p.service}
                  </span>
                )}
                {(p.product || p.version) && (
                  <span className="ml-2 text-[11px] text-muted-foreground/80">
                    {[p.product, p.version].filter(Boolean).join(" ")}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
