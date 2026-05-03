import { useState, useEffect, useRef } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useStickyLocation } from "@/lib/stickyLocation";
import { useSessionState } from "@/lib/useSessionState";
import { useRowHighlight } from "@/lib/useRowHighlight";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Globe,
  Plus,
  Trash2,
  Pencil,
  ChevronRight,
  Settings2,
  Shield,
  Eye,
  FileText,
  Layers,
  RefreshCw,
  X,
  Cpu,
  FolderOpen,
  Folder,
  Upload,
  Download,
  Ban,
  Lock,
  Info,
  Filter,
  Search,
  ListTree,
  Radar,
  Sparkles,
  Workflow,
  KeyRound,
  Copy,
} from "lucide-react";
import { PropagationCheckModal } from "./PropagationCheckModal";
import { BlocklistCatalogModal } from "./BlocklistCatalogModal";
import { DelegationModal } from "./DelegationModal";
import { ZoneTemplateModal } from "./ZoneTemplateModal";
import { ServerDetailModal } from "./ServerDetailModal";
import { PoolsView } from "./PoolsView";
import { Modal } from "@/components/ui/modal";
import { HeaderButton } from "@/components/ui/header-button";
import {
  dnsApi,
  dnsBlocklistApi,
  domainsApi,
  formatApiError,
  type DNSServerGroup,
  type DNSServer,
  type DNSZone,
  type ZoneServerState,
  type DNSView,
  type DNSRecord,
  type DNSGroupRecord,
  type DNSImportPreview,
  type DNSRecordChange,
  type DNSBlockList,
  type DNSBlockListEntry,
  type DNSBlockListException,
  type DNSTSIGKey,
  type WindowsDNSCredentials,
  type DNSGroupSyncResult,
} from "@/lib/api";
import { copyToClipboard } from "@/lib/clipboard";
import { useTableSort, SortableTh } from "@/lib/useTableSort";
import { cn, swatchCls, zebraBodyCls } from "@/lib/utils";
import { SwatchPicker } from "@/components/ui/swatch-picker";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuLabel,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";

// ── Shared primitives ─────────────────────────────────────────────────────────

const inputCls =
  "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label className="block text-xs font-medium text-muted-foreground">
        {label}
      </label>
      {children}
    </div>
  );
}

type ApiError = { response?: { data?: { detail?: string } } };

function Btns({
  onClose,
  pending,
  label,
}: {
  onClose: () => void;
  pending: boolean;
  label?: string;
}) {
  return (
    <div className="flex justify-end gap-2 pt-2">
      <button
        type="button"
        onClick={onClose}
        className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
      >
        Cancel
      </button>
      <button
        type="submit"
        disabled={pending}
        className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        {pending ? "Saving…" : (label ?? "Save")}
      </button>
    </div>
  );
}

// ── Double-confirm destroy modal (matches IPAM pattern) ──────────────────────

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
  description: React.ReactNode;
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
            <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
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
            onChange={(e) => setChecked(e.target.checked)}
          />
          {checkLabel}
        </label>
        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
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
            {isPending ? "Deleting…" : "Delete"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** Single-step destructive confirm (no checkbox). Used for low-stakes deletes
 * like individual DNS records — the two-step modal above stays for group /
 * blocklist deletes where the blast radius is much larger. */
function ConfirmSingleModal({
  title,
  description,
  onConfirm,
  onClose,
  isPending,
  confirmLabel = "Delete",
}: {
  title: string;
  description: React.ReactNode;
  onConfirm: () => void;
  onClose: () => void;
  isPending?: boolean;
  confirmLabel?: string;
}) {
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
            onClick={onConfirm}
            disabled={isPending}
            className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
          >
            {isPending ? "Deleting…" : confirmLabel}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Download helper ──────────────────────────────────────────────────────────

function downloadBlob(
  data: Blob | string,
  filename: string,
  mime = "text/plain",
) {
  const blob = data instanceof Blob ? data : new Blob([data], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// UTC timestamp suffix used when the backend didn't send a Content-Disposition
// filename (rare — every export endpoint sets it). Matches the backend's
// "%Y%m%d-%H%M%S" format so fallback filenames sort alongside real ones.
function _utcTimestampSuffix(): string {
  return new Date()
    .toISOString()
    .slice(0, 19)
    .replace(/[-:]/g, "")
    .replace("T", "-");
}

// ── Import Zone Modal ────────────────────────────────────────────────────────

function ImportZoneModal({
  groupId,
  zone,
  onClose,
}: {
  groupId: string;
  zone: DNSZone;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [zoneFile, setZoneFile] = useState("");
  const [strategy, setStrategy] = useState<"merge" | "replace" | "append">(
    "merge",
  );
  const [preview, setPreview] = useState<DNSImportPreview | null>(null);
  const [error, setError] = useState<string | null>(null);

  const previewMut = useMutation({
    mutationFn: () =>
      dnsApi.importZonePreview(groupId, zone.id, {
        zone_file: zoneFile,
        zone_name: zone.name,
      }),
    onSuccess: (data) => {
      setPreview(data);
      setError(null);
    },
    onError: (err: ApiError) => {
      setPreview(null);
      setError(formatApiError(err, "Failed to parse zone file"));
    },
  });

  const commitMut = useMutation({
    mutationFn: () =>
      dnsApi.importZoneCommit(groupId, zone.id, {
        zone_file: zoneFile,
        zone_name: zone.name,
        conflict_strategy: strategy,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-records", zone.id] });
      qc.invalidateQueries({ queryKey: ["dns-zones", groupId] });
      onClose();
    },
    onError: (err: ApiError) => {
      setError(formatApiError(err, "Import failed"));
    },
  });

  const onFileChosen = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const text = await file.text();
    setZoneFile(text);
    setPreview(null);
  };

  const renderChanges = (
    label: string,
    items: DNSRecordChange[],
    color: string,
  ) =>
    items.length > 0 && (
      <details className="rounded border" open={items.length <= 10}>
        <summary
          className={`cursor-pointer px-2 py-1 text-xs font-medium ${color}`}
        >
          {label} ({items.length})
        </summary>
        <div className="max-h-40 overflow-auto">
          <table className="w-full text-xs">
            <tbody>
              {items.map((c, i) => (
                <tr key={i} className="border-t">
                  <td className="px-2 py-0.5 font-mono">{c.name}</td>
                  <td className="px-2 py-0.5">{c.record_type}</td>
                  <td className="px-2 py-0.5 font-mono text-muted-foreground truncate max-w-xs">
                    {c.value}
                  </td>
                  <td className="px-2 py-0.5 text-muted-foreground">
                    {c.ttl ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    );

  return (
    <Modal title={`Import Zone File — ${zone.name}`} onClose={onClose} wide>
      <div className="space-y-3">
        <Field label="Zone file (RFC 1035 format)">
          <input
            type="file"
            accept=".zone,.db,.txt,text/plain,text/dns"
            onChange={onFileChosen}
            className="text-xs"
          />
        </Field>
        <Field label="…or paste contents">
          <textarea
            className={`${inputCls} font-mono text-xs`}
            rows={8}
            value={zoneFile}
            onChange={(e) => {
              setZoneFile(e.target.value);
              setPreview(null);
            }}
            placeholder="$ORIGIN example.com.&#10;$TTL 3600&#10;@ IN SOA ns1 hostmaster ( 1 86400 7200 3600000 3600 )"
          />
        </Field>

        <Field label="Conflict strategy">
          <select
            className={inputCls}
            value={strategy}
            onChange={(e) =>
              setStrategy(e.target.value as "merge" | "replace" | "append")
            }
          >
            <option value="merge">
              Merge — add new, update changed, keep existing
            </option>
            <option value="replace">
              Replace — make the zone match the file exactly
            </option>
            <option value="append">
              Append — only add records that do not exist
            </option>
          </select>
        </Field>

        {error && (
          <div className="rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {error}
          </div>
        )}

        {preview && (
          <div className="space-y-2">
            <div className="text-xs text-muted-foreground">
              Parsed {preview.record_count} record
              {preview.record_count !== 1 ? "s" : ""}
              {preview.soa_detected &&
                " (SOA detected — zone SOA will not be changed)"}
            </div>
            {renderChanges("Create", preview.to_create, "text-emerald-600")}
            {renderChanges("Update", preview.to_update, "text-amber-600")}
            {renderChanges(
              "Delete (only with Replace)",
              preview.to_delete,
              "text-destructive",
            )}
            {renderChanges(
              "Unchanged",
              preview.unchanged,
              "text-muted-foreground",
            )}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
          >
            Cancel
          </button>
          <button
            onClick={() => previewMut.mutate()}
            disabled={!zoneFile || previewMut.isPending}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
          >
            {previewMut.isPending ? "Parsing…" : "Preview"}
          </button>
          <button
            onClick={() => commitMut.mutate()}
            disabled={!preview || commitMut.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {commitMut.isPending ? "Importing…" : "Import"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── DNS zone tree builder (recursive: com → test.com → sub.test.com) ─────────

interface DnsTreeNode {
  domain: string; // full domain name at this level, e.g. "test.com"
  zone?: DNSZone; // set if this node corresponds to a registered zone
  children: DnsTreeNode[];
}

function buildDnsTree(zones: DNSZone[]): DnsTreeNode[] {
  const nodeMap = new Map<string, DnsTreeNode>();

  function getOrCreate(domain: string): DnsTreeNode {
    if (!nodeMap.has(domain)) nodeMap.set(domain, { domain, children: [] });
    return nodeMap.get(domain)!;
  }

  const tldSet = new Set<string>();

  for (const z of zones) {
    const name = z.name.replace(/\.$/, ""); // strip trailing dot
    const parts = name.split("."); // ["sub", "test", "com"]

    tldSet.add(parts[parts.length - 1]);

    // Build ancestor chain from TLD down to zone
    for (let level = 0; level < parts.length; level++) {
      const startIdx = parts.length - 1 - level;
      const domain = parts.slice(startIdx).join(".");
      getOrCreate(domain);

      if (level > 0) {
        const parentDomain = parts.slice(startIdx + 1).join(".");
        const parent = getOrCreate(parentDomain);
        const child = getOrCreate(domain);
        if (!parent.children.find((c) => c.domain === domain)) {
          parent.children.push(child);
        }
      }
    }

    getOrCreate(name).zone = z;
  }

  function sortNode(n: DnsTreeNode) {
    n.children.sort((a, b) => a.domain.localeCompare(b.domain));
    n.children.forEach(sortNode);
  }

  const roots = [...tldSet]
    .sort()
    .map((tld) => nodeMap.get(tld)!)
    .filter(Boolean);
  roots.forEach(sortNode);
  return roots;
}

// ── Group Modal (create / edit) ───────────────────────────────────────────────

function GroupModal({
  group,
  onClose,
}: {
  group?: DNSServerGroup;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(group?.name ?? "");
  const [description, setDescription] = useState(group?.description ?? "");
  const [groupType, setGroupType] = useState(group?.group_type ?? "internal");
  const [isRecursive, setIsRecursive] = useState(group?.is_recursive ?? true);
  // BIND9 catalog zones (RFC 9432). Off by default — only meaningful in
  // ≥2-server BIND9 groups, and BIND 9.18+ is required.
  const [catalogZonesEnabled, setCatalogZonesEnabled] = useState(
    group?.catalog_zones_enabled ?? false,
  );
  const [catalogZoneName, setCatalogZoneName] = useState(
    group?.catalog_zone_name ?? "catalog.spatium.invalid.",
  );
  const [error, setError] = useState("");

  const mut = useMutation({
    mutationFn: (d: Partial<DNSServerGroup>) =>
      group ? dnsApi.updateGroup(group.id, d) : dnsApi.createGroup(d),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-groups"] });
      onClose();
    },
    onError: (e: ApiError) => setError(formatApiError(e)),
  });

  return (
    <Modal
      title={group ? "Edit Server Group" : "New Server Group"}
      onClose={onClose}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setError("");
          mut.mutate({
            name,
            description,
            group_type: groupType,
            is_recursive: isRecursive,
            catalog_zones_enabled: catalogZonesEnabled,
            catalog_zone_name: catalogZoneName.trim(),
          });
        }}
        className="space-y-3"
      >
        <Field label="Name">
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. internal-resolvers"
            required
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
        <div className="grid grid-cols-2 gap-3">
          <Field label="Type">
            <select
              className={inputCls}
              value={groupType}
              onChange={(e) => setGroupType(e.target.value)}
            >
              <option value="internal">Internal</option>
              <option value="external">External</option>
              <option value="dmz">DMZ</option>
              <option value="custom">Custom</option>
            </select>
          </Field>
          <Field label="Recursion">
            <label className="flex items-center gap-2 mt-2 cursor-pointer">
              <input
                type="checkbox"
                checked={isRecursive}
                onChange={(e) => setIsRecursive(e.target.checked)}
                className="h-4 w-4"
              />
              <span className="text-sm">Allow recursion</span>
            </label>
          </Field>
        </div>

        {/* BIND9 catalog zones (RFC 9432). The producer is the group's
            is_primary=True bind9 server; every other bind9 member joins
            as a consumer and pulls members from the catalog instead of
            getting per-zone config push. Pointless on a single-server
            group; the toggle is kept available so adding a second server
            later just works. */}
        <div className="rounded border bg-muted/20 p-3">
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={catalogZonesEnabled}
              onChange={(e) => setCatalogZonesEnabled(e.target.checked)}
              className="h-4 w-4"
            />
            <span className="text-sm font-medium">Use BIND9 catalog zones</span>
          </label>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Distribute zones via one catalog instead of per-server config push.
            Requires BIND 9.18+. Skip on single-server groups.
          </p>
          {catalogZonesEnabled && (
            <div className="mt-2">
              <label className="mb-0.5 block text-xs font-medium">
                Catalog zone name
              </label>
              <input
                className={inputCls}
                value={catalogZoneName}
                onChange={(e) => setCatalogZoneName(e.target.value)}
                placeholder="catalog.spatium.invalid."
              />
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                Synthetic FQDN — pick something inside an unroutable label (e.g.{" "}
                <code>.invalid.</code>) so it doesn't collide with a real zone.
              </p>
            </div>
          )}
        </div>

        {error && <p className="text-sm text-destructive">{error}</p>}
        <Btns
          onClose={onClose}
          pending={mut.isPending}
          label={group ? "Save" : "Create"}
        />
      </form>
    </Modal>
  );
}

// ── Server Modal (add / edit) ─────────────────────────────────────────────────

function ServerModal({
  groupId,
  server,
  onClose,
}: {
  groupId: string;
  server?: DNSServer;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = !!server;
  const [name, setName] = useState(server?.name ?? "");
  const [driver, setDriver] = useState(server?.driver ?? "bind9");
  const [host, setHost] = useState(server?.host ?? "");
  const [port, setPort] = useState(String(server?.port ?? 53));
  const [apiPort, setApiPort] = useState(String(server?.api_port ?? ""));
  const [roles, setRoles] = useState((server?.roles ?? []).join(", "));
  const [notes, setNotes] = useState(server?.notes ?? "");
  const [apiKey, setApiKey] = useState("");
  const [isEnabled, setIsEnabled] = useState(server?.is_enabled ?? true);
  const [error, setError] = useState("");

  // Windows credential state — same contract as the DHCP modal:
  //   * On edit with creds: leave blank to keep, type to replace.
  //   * Always send the creds block on windows_dns so transport / port /
  //     TLS toggles reach the backend (backend merges with stored blob).
  const [winUsername, setWinUsername] = useState("");
  const [winPassword, setWinPassword] = useState("");
  const [winPort, setWinPort] = useState("5985");
  const [winTransport, setWinTransport] =
    useState<WindowsDNSCredentials["transport"]>("ntlm");
  const [winUseTLS, setWinUseTLS] = useState(false);
  const [winVerifyTLS, setWinVerifyTLS] = useState(false);
  const [winClearCreds, setWinClearCreds] = useState(false);
  const [testResult, setTestResult] = useState<{
    ok: boolean;
    message: string;
  } | null>(null);

  const hasExistingCreds = !!server?.has_credentials;

  const testMut = useMutation({
    mutationFn: () => {
      const useStored =
        editing && hasExistingCreds && !winPassword && !winUsername;
      if (useStored) {
        return dnsApi.testWindowsCredentials({
          host,
          server_id: server!.id,
        });
      }
      return dnsApi.testWindowsCredentials({
        host,
        credentials: {
          username: winUsername,
          password: winPassword,
          winrm_port: parseInt(winPort, 10) || 5985,
          transport: winTransport,
          use_tls: winUseTLS,
          verify_tls: winVerifyTLS,
        },
      });
    },
    onSuccess: setTestResult,
    onError: (e: ApiError) =>
      setTestResult({ ok: false, message: formatApiError(e, "Test failed") }),
  });

  const mut = useMutation({
    mutationFn: (d: Record<string, unknown>) =>
      server
        ? dnsApi.updateServer(groupId, server.id, d)
        : dnsApi.createServer(groupId, d),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-servers", groupId] });
      onClose();
    },
    onError: (e: ApiError) => setError(formatApiError(e)),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const roleList = roles
      .split(/[,\s]+/)
      .map((r) => r.trim())
      .filter(Boolean);

    const payload: Record<string, unknown> = {
      name,
      driver,
      host,
      port: parseInt(port, 10),
      api_port: apiPort ? parseInt(apiPort, 10) : null,
      roles: roleList,
      notes,
      is_enabled: isEnabled,
      ...(apiKey ? { api_key: apiKey } : {}),
    };

    if (driver === "windows_dns") {
      if (winClearCreds) {
        payload.windows_credentials = {};
      } else if (winUsername || winPassword || editing) {
        // Path B is opt-in: only send a creds block if the user entered
        // something, or if we're editing a server that may already have
        // creds (lets them flip transport / port without re-typing).
        const creds: Partial<WindowsDNSCredentials> = {
          winrm_port: parseInt(winPort, 10) || 5985,
          transport: winTransport,
          use_tls: winUseTLS,
          verify_tls: winVerifyTLS,
        };
        if (winUsername) creds.username = winUsername;
        if (winPassword) creds.password = winPassword;
        // First-time set requires both. Edit path is merge — backend
        // checks "have stored creds" before accepting partials.
        if (
          !editing &&
          (winUsername || winPassword) &&
          (!winUsername || !winPassword)
        ) {
          setError(
            "Windows DNS Path B requires both username and password to enable WinRM. Leave both blank for Path A only (RFC 2136).",
          );
          return;
        }
        if (winUsername || winPassword || (editing && hasExistingCreds)) {
          payload.windows_credentials = creds;
        }
      }
    }

    mut.mutate(payload);
  }

  return (
    <Modal
      title={server ? `Edit ${server.name}` : "Add Server"}
      onClose={onClose}
    >
      <form onSubmit={submit} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name">
            <input
              className={inputCls}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="ns1"
              required
              autoFocus
            />
          </Field>
          <Field label="Driver">
            <select
              className={inputCls}
              value={driver}
              onChange={(e) => setDriver(e.target.value)}
            >
              <option value="bind9">BIND9 (agent-managed)</option>
              <option value="windows_dns">
                Windows DNS (agentless, RFC 2136 + optional WinRM)
              </option>
            </select>
          </Field>
        </div>
        {driver === "windows_dns" && (
          <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
            <strong>Windows DNS:</strong>{" "}
            <span className="font-medium">Path A</span> (always on): record CRUD
            via RFC 2136 — zones must exist in Windows DNS Manager with
            <em> Nonsecure and secure</em> dynamic updates enabled.{" "}
            <span className="font-medium">Path B</span> (optional, configure
            credentials below): adds WinRM-backed zone topology reads so you can
            import existing zones into SpatiumDDI. No agent container required
            either way.
          </div>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Host / IP">
            <input
              className={inputCls}
              value={host}
              onChange={(e) => setHost(e.target.value)}
              placeholder="10.0.0.53"
              required
            />
          </Field>
          <Field label="DNS Port">
            <input
              className={inputCls}
              value={port}
              onChange={(e) => setPort(e.target.value)}
              placeholder="53"
            />
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="API Port (rndc / REST)">
            <input
              className={inputCls}
              value={apiPort}
              onChange={(e) => setApiPort(e.target.value)}
              placeholder="953 / 8081"
            />
          </Field>
          <Field
            label={server ? "New API Key (leave blank to keep)" : "API Key"}
          >
            <input
              type="password"
              className={inputCls}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={server ? "unchanged" : "optional"}
            />
          </Field>
        </div>
        <Field label="Roles (comma-separated)">
          <input
            className={inputCls}
            value={roles}
            onChange={(e) => setRoles(e.target.value)}
            placeholder="authoritative, recursive"
          />
        </Field>
        <Field label="Notes">
          <input
            className={inputCls}
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Optional notes"
          />
        </Field>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={isEnabled}
            onChange={(e) => setIsEnabled(e.target.checked)}
          />
          <span>
            <span className="font-medium">Enabled</span>
            <span className="block text-xs text-muted-foreground">
              Uncheck to pause this server — SpatiumDDI will skip it in the
              health probe, the bi-directional sync task, and record-op writes.
              Useful during Windows DNS / DC maintenance. Status will read{" "}
              <code>disabled</code> until re-enabled.
            </span>
          </span>
        </label>

        {driver === "windows_dns" && (
          <div className="rounded-md border border-sky-500/40 bg-sky-500/5 p-3 space-y-3">
            <div className="text-xs">
              <div className="font-medium text-sky-600 dark:text-sky-400">
                Path B — WinRM / PowerShell (optional)
              </div>
              <p className="mt-1 text-muted-foreground">
                Fill the fields below to unlock zone-topology reads (import
                existing Windows DNS zones into SpatiumDDI). Credentials are
                stored Fernet-encrypted and never returned by the API. Leave
                blank to stay on Path A only (record CRUD via RFC 2136).
              </p>
            </div>

            <details className="rounded border bg-background/40 text-xs">
              <summary className="cursor-pointer px-3 py-2 font-medium select-none">
                Windows setup checklist — click to expand
              </summary>
              <div className="space-y-3 border-t px-3 py-2.5 text-muted-foreground">
                <div>
                  <div className="font-medium text-foreground">
                    1. Enable WinRM on the DNS server
                  </div>
                  <pre className="mt-1 rounded bg-muted p-2 font-mono text-[11px] whitespace-pre-wrap">
                    Enable-PSRemoting -Force
                  </pre>
                </div>
                <div>
                  <div className="font-medium text-foreground">
                    2. Grant the service account access
                  </div>
                  <p>
                    Add the account to{" "}
                    <code className="font-mono">Remote Management Users</code>{" "}
                    (transport) and to{" "}
                    <code className="font-mono">DnsAdmins</code> for zone CRUD
                    (read-only needs only the first). DCs have the same domain
                    group quirks as Windows DHCP — see the DHCP server checklist
                    if you hit <code>0x80080005</code>.
                  </p>
                </div>
                <div>
                  <div className="font-medium text-foreground">
                    3. Verify from another host
                  </div>
                  <pre className="mt-1 rounded bg-muted p-2 font-mono text-[11px] whitespace-pre-wrap">
                    {
                      "Invoke-Command <host> { Get-DnsServerZone } -Credential (Get-Credential)"
                    }
                  </pre>
                </div>
              </div>
            </details>

            {hasExistingCreds && !winClearCreds && (
              <div className="flex items-center justify-between rounded border bg-background/50 px-3 py-2 text-xs">
                <span>
                  <span className="font-medium">Credentials set.</span> Leave
                  fields blank to keep them, or enter new values to replace.
                </span>
                <button
                  type="button"
                  onClick={() => setWinClearCreds(true)}
                  className="rounded border px-2 py-0.5 text-[11px] hover:bg-muted"
                >
                  Clear
                </button>
              </div>
            )}
            {winClearCreds && (
              <div className="flex items-center justify-between rounded border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs">
                <span className="text-destructive">
                  Credentials will be removed on save (Path A only afterward).
                </span>
                <button
                  type="button"
                  onClick={() => setWinClearCreds(false)}
                  className="rounded border px-2 py-0.5 text-[11px] hover:bg-muted"
                >
                  Undo
                </button>
              </div>
            )}

            <div
              className={`grid grid-cols-2 gap-3 ${winClearCreds ? "opacity-40 pointer-events-none" : ""}`}
            >
              <Field label="Username">
                <input
                  className={inputCls}
                  value={winUsername}
                  onChange={(e) => setWinUsername(e.target.value)}
                  placeholder={"CORP\\dnsreader"}
                  autoComplete="off"
                />
              </Field>
              <Field label="Password">
                <input
                  type="password"
                  className={inputCls}
                  value={winPassword}
                  onChange={(e) => setWinPassword(e.target.value)}
                  placeholder={hasExistingCreds ? "(unchanged)" : "optional"}
                  autoComplete="off"
                />
              </Field>
              <Field label="WinRM Port">
                <input
                  type="number"
                  className={inputCls}
                  value={winPort}
                  onChange={(e) => setWinPort(e.target.value)}
                />
              </Field>
              <Field label="Auth Transport">
                <select
                  className={inputCls}
                  value={winTransport}
                  onChange={(e) =>
                    setWinTransport(
                      e.target.value as WindowsDNSCredentials["transport"],
                    )
                  }
                >
                  <option value="ntlm">NTLM</option>
                  <option value="kerberos">Kerberos</option>
                  <option value="basic">Basic</option>
                  <option value="credssp">CredSSP</option>
                </select>
              </Field>
              <Field label="Use HTTPS (port 5986)">
                <input
                  type="checkbox"
                  checked={winUseTLS}
                  onChange={(e) => {
                    setWinUseTLS(e.target.checked);
                    if (e.target.checked && winPort === "5985")
                      setWinPort("5986");
                    if (!e.target.checked && winPort === "5986")
                      setWinPort("5985");
                  }}
                />
              </Field>
              <Field label="Verify TLS certificate">
                <input
                  type="checkbox"
                  checked={winVerifyTLS}
                  disabled={!winUseTLS}
                  onChange={(e) => setWinVerifyTLS(e.target.checked)}
                />
              </Field>
            </div>

            <div
              className={`flex items-center gap-3 ${winClearCreds ? "opacity-40 pointer-events-none" : ""}`}
            >
              <button
                type="button"
                onClick={() => {
                  setTestResult(null);
                  testMut.mutate();
                }}
                disabled={
                  testMut.isPending ||
                  !host ||
                  (!winUsername &&
                    !(editing && hasExistingCreds && !winPassword))
                }
                className="rounded-md border px-3 py-1.5 text-xs hover:bg-accent disabled:opacity-50"
              >
                {testMut.isPending ? "Testing…" : "Test Connection"}
              </button>
              {editing && hasExistingCreds && !winUsername && !winPassword && (
                <span className="text-[11px] text-muted-foreground">
                  will use stored credentials
                </span>
              )}
              {testResult && (
                <span
                  className={`text-xs ${testResult.ok ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"}`}
                >
                  {testResult.ok ? "✓ " : "✗ "}
                  {testResult.message}
                </span>
              )}
            </div>
          </div>
        )}

        {server && (
          <p className="text-xs text-muted-foreground">
            Servers can also be auto-registered by the DNS agent container — see{" "}
            <code>DNS_AGENT_KEY</code> in deployment docs.
          </p>
        )}
        {error && <p className="text-sm text-destructive">{error}</p>}
        <Btns
          onClose={onClose}
          pending={mut.isPending}
          label={server ? "Save" : "Add Server"}
        />
      </form>
    </Modal>
  );
}

// ── Zone Modal (add / edit) ───────────────────────────────────────────────────

function ZoneModal({
  groupId,
  views,
  zone,
  initialName,
  onClose,
}: {
  groupId: string;
  views: DNSView[];
  zone?: DNSZone;
  initialName?: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(
    zone?.name?.replace(/\.$/, "") ?? initialName ?? "",
  );
  const [zoneType, setZoneType] = useState(zone?.zone_type ?? "primary");
  const [kind, setKind] = useState(zone?.kind ?? "forward");
  const [viewId, setViewId] = useState(zone?.view_id ?? "");
  const [primaryNs, setPrimaryNs] = useState(zone?.primary_ns ?? "");
  const [adminEmail, setAdminEmail] = useState(zone?.admin_email ?? "");
  const [ttl, setTtl] = useState(String(zone?.ttl ?? 3600));
  const [dnssec, setDnssec] = useState(zone?.dnssec_enabled ?? false);
  const [color, setColor] = useState<string | null>(zone?.color ?? null);
  // Forward-zone config — only shown / submitted when zoneType === "forward".
  const [forwardersText, setForwardersText] = useState(
    (zone?.forwarders ?? []).join(", "),
  );
  const [forwardOnly, setForwardOnly] = useState(zone?.forward_only ?? true);
  const [domainId, setDomainId] = useState<string | null>(
    zone?.domain_id ?? null,
  );
  const { data: domainList } = useQuery({
    queryKey: ["domains-picker"],
    queryFn: () => domainsApi.list({ page_size: 500 }),
    staleTime: 60_000,
  });
  const [error, setError] = useState("");

  const mut = useMutation({
    mutationFn: (d: Record<string, unknown>) =>
      zone
        ? dnsApi.updateZone(groupId, zone.id, d)
        : dnsApi.createZone(groupId, d),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-zones", groupId] });
      onClose();
    },
    onError: (e: ApiError) => setError(formatApiError(e)),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const payload: Record<string, unknown> = {
      name,
      zone_type: zoneType,
      kind,
      view_id: viewId || null,
      primary_ns: primaryNs,
      admin_email: adminEmail,
      ttl: parseInt(ttl, 10),
      dnssec_enabled: dnssec,
      color,
      domain_id: domainId,
    };
    if (zoneType === "forward") {
      const fwds = forwardersText
        .split(/[,\s]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      if (fwds.length === 0) {
        setError("Forward zones need at least one forwarder IP");
        return;
      }
      payload.forwarders = fwds;
      payload.forward_only = forwardOnly;
    }
    mut.mutate(payload);
  }

  return (
    <Modal title={zone ? `Edit ${zone.name}` : "Add Zone"} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Zone Name (FQDN)">
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="example.com"
            required
            autoFocus
            disabled={!!zone}
          />
          {!zone && (
            <p className="text-xs text-muted-foreground mt-0.5">
              Trailing dot added automatically.
            </p>
          )}
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Type">
            <select
              className={inputCls}
              value={zoneType}
              onChange={(e) => setZoneType(e.target.value)}
            >
              <option value="primary">Primary</option>
              <option value="secondary">Secondary</option>
              <option value="stub">Stub</option>
              <option value="forward">Forward</option>
            </select>
          </Field>
          <Field label="Kind">
            <select
              className={inputCls}
              value={kind}
              onChange={(e) => setKind(e.target.value)}
            >
              <option value="forward">Forward lookup</option>
              <option value="reverse">Reverse lookup</option>
            </select>
          </Field>
        </div>
        {zoneType === "forward" && (
          <div className="space-y-3 rounded border border-dashed bg-muted/20 p-3">
            <p className="text-[11px] text-muted-foreground">
              <strong>Conditional forwarder.</strong> Queries for{" "}
              <span className="font-mono">{name || "this zone"}</span> are
              relayed to the upstream resolvers below — typical use is "forward{" "}
              <code>corp.local</code> to the AD DNS at 10.0.0.5". Records on the
              zone are ignored when the type is forward.
            </p>
            <Field label="Forwarder IPs (comma- or space-separated)">
              <input
                className={inputCls}
                value={forwardersText}
                onChange={(e) => setForwardersText(e.target.value)}
                placeholder="10.0.0.5, 10.0.0.6"
              />
            </Field>
            <Field label="Fallback policy">
              <select
                className={inputCls}
                value={forwardOnly ? "only" : "first"}
                onChange={(e) => setForwardOnly(e.target.value === "only")}
              >
                <option value="only">
                  forward only — never fall back to recursion
                </option>
                <option value="first">
                  forward first — fall back if all forwarders fail
                </option>
              </select>
            </Field>
          </div>
        )}
        {views.length > 0 && (
          <Field label="View (optional)">
            <select
              className={inputCls}
              value={viewId}
              onChange={(e) => setViewId(e.target.value)}
            >
              <option value="">— No view —</option>
              {views.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.name}
                </option>
              ))}
            </select>
          </Field>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Primary NS">
            <input
              className={inputCls}
              value={primaryNs}
              onChange={(e) => setPrimaryNs(e.target.value)}
              placeholder="ns1.example.com."
            />
          </Field>
          <Field label="Admin Email">
            <input
              className={inputCls}
              value={adminEmail}
              onChange={(e) => setAdminEmail(e.target.value)}
              placeholder="hostmaster.example.com."
            />
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Default TTL (seconds)">
            <input
              className={inputCls}
              value={ttl}
              onChange={(e) => setTtl(e.target.value)}
              placeholder="3600"
            />
          </Field>
          <Field label="DNSSEC">
            <label className="flex items-center gap-2 mt-2 cursor-pointer">
              <input
                type="checkbox"
                checked={dnssec}
                onChange={(e) => setDnssec(e.target.checked)}
                className="h-4 w-4"
              />
              <span className="text-sm">Enable DNSSEC</span>
            </label>
          </Field>
        </div>
        <Field label="Color">
          <SwatchPicker value={color} onChange={setColor} />
        </Field>
        <Field label="Linked Domain (optional)">
          <select
            className={inputCls}
            value={domainId ?? ""}
            onChange={(e) => setDomainId(e.target.value || null)}
          >
            <option value="">— Auto-match by zone name —</option>
            {(domainList?.items ?? []).map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
                {d.registrar ? ` — ${d.registrar}` : ""}
              </option>
            ))}
          </select>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Pin to a tracked domain registration so the Domain detail page
            surfaces it under "Linked DNS Zones". Auto-matches by name when left
            blank.
          </p>
        </Field>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <Btns
          onClose={onClose}
          pending={mut.isPending}
          label={zone ? "Save" : "Add Zone"}
        />
      </form>
    </Modal>
  );
}

// ── Record Modal (add / edit) ─────────────────────────────────────────────────

const RECORD_TYPES = [
  "A",
  "AAAA",
  "CNAME",
  "MX",
  "TXT",
  "NS",
  "PTR",
  "SRV",
  "CAA",
  "TLSA",
  "SSHFP",
  "NAPTR",
  "LOC",
];

function RecordModal({
  groupId,
  zoneId,
  zoneName,
  record,
  onClose,
}: {
  groupId: string;
  zoneId: string;
  zoneName?: string;
  record?: DNSRecord;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const isReverseZone =
    !!zoneName && /\.(in-addr|ip6)\.arpa\.?$/i.test(zoneName);
  const [name, setName] = useState(record?.name ?? "");
  const [type, setType] = useState(
    record?.record_type ?? (isReverseZone ? "PTR" : "A"),
  );
  const [value, setValue] = useState(record?.value ?? "");
  const [ttl, setTtl] = useState(String(record?.ttl ?? ""));
  const [priority, setPriority] = useState(String(record?.priority ?? ""));
  const [viewId, setViewId] = useState<string>(record?.view_id ?? "");
  const [error, setError] = useState("");

  const { data: views = [] } = useQuery({
    queryKey: ["dns-views", groupId],
    queryFn: () => dnsApi.listViews(groupId),
  });

  const showPriority = ["MX", "SRV"].includes(type);

  const mut = useMutation({
    mutationFn: (d: Record<string, unknown>) =>
      record
        ? dnsApi.updateRecord(groupId, zoneId, record.id, d)
        : dnsApi.createRecord(groupId, zoneId, d),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-records", zoneId] });
      onClose();
    },
    onError: (e: ApiError) => setError(formatApiError(e)),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    mut.mutate({
      name,
      record_type: type,
      value,
      ttl: ttl ? parseInt(ttl, 10) : null,
      priority: priority ? parseInt(priority, 10) : null,
      view_id: viewId || null,
    });
  }

  return (
    <Modal title={record ? "Edit Record" : "Add Record"} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name (relative to zone)">
            <input
              className={inputCls}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder='@ for apex, "www", "mail"'
              required
              autoFocus
            />
          </Field>
          <Field label="Type">
            <select
              className={inputCls}
              value={type}
              onChange={(e) => setType(e.target.value)}
            >
              {RECORD_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Value">
          <input
            className={inputCls}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={
              type === "A"
                ? "10.0.0.1"
                : type === "CNAME"
                  ? "other.example.com."
                  : type === "PTR"
                    ? "host.example.com."
                    : "record value"
            }
            required
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="TTL (leave blank for zone default)">
            <input
              className={inputCls}
              value={ttl}
              onChange={(e) => setTtl(e.target.value)}
              placeholder="zone default"
            />
          </Field>
          {showPriority && (
            <Field label="Priority">
              <input
                className={inputCls}
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
                placeholder="10"
              />
            </Field>
          )}
        </div>
        <Field label="View (optional — scope record to a split-horizon view)">
          <select
            className={inputCls}
            value={viewId}
            onChange={(e) => setViewId(e.target.value)}
          >
            <option value="">All views (default)</option>
            {views.map((v) => (
              <option key={v.id} value={v.id}>
                {v.name}
              </option>
            ))}
          </select>
        </Field>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <Btns
          onClose={onClose}
          pending={mut.isPending}
          label={record ? "Save" : "Add Record"}
        />
      </form>
    </Modal>
  );
}

// ── Zone Detail View (records panel) ─────────────────────────────────────────

/**
 * Per-server serial-drift pill. Three display states:
 *  - All servers on the target serial → emerald "N/N synced · serial X"
 *  - Some servers behind / ahead      → amber "1/3 drift · target X"
 *  - No server has reported yet       → muted "— not reported"
 *
 * Hover tooltip lists every server with its own serial for quick drift
 * diagnosis (e.g. "ns2: 41 (target 42, reported 3m ago)").
 */
function ZoneSyncPill({ state }: { state: ZoneServerState }) {
  const total = state.servers.length;
  if (total === 0) return null;
  const reported = state.servers.filter((s) => s.current_serial !== null);
  const inSync = state.servers.filter(
    (s) => s.current_serial === state.target_serial,
  );
  const tooltip = state.servers
    .map((s) =>
      s.current_serial === null
        ? `${s.server_name}: not reported`
        : `${s.server_name}: serial ${s.current_serial}` +
          (s.current_serial !== state.target_serial ? " (drift)" : ""),
    )
    .join("\n");

  let cls = "bg-muted/40 text-muted-foreground";
  let label = "not reported";
  if (reported.length === 0) {
    // noop — keep muted
  } else if (state.in_sync) {
    cls =
      "bg-emerald-500/15 text-emerald-600 dark:bg-emerald-500/20 dark:text-emerald-400";
    label = `${inSync.length}/${total} synced · serial ${state.target_serial}`;
  } else {
    cls =
      "bg-amber-500/15 text-amber-700 dark:bg-amber-500/20 dark:text-amber-400";
    label = `${total - inSync.length}/${total} drift · target ${state.target_serial}`;
  }
  return (
    <span
      className={cn(
        "ml-2 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium",
        cls,
      )}
      title={tooltip}
    >
      {label}
    </span>
  );
}

function ZoneDetailView({
  group,
  zone,
  highlightRecordId,
  onDeleted,
}: {
  group: DNSServerGroup;
  zone: DNSZone;
  highlightRecordId?: string | null;
  onDeleted: () => void;
}) {
  const qc = useQueryClient();
  const [showAddRecord, setShowAddRecord] = useState(false);
  const [editRecord, setEditRecord] = useState<DNSRecord | null>(null);
  const [propagationRecord, setPropagationRecord] = useState<DNSRecord | null>(
    null,
  );
  const [showEditZone, setShowEditZone] = useState(false);
  const [showDelegate, setShowDelegate] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showRecFilters, setShowRecFilters] = useState(false);
  const [recFilter, setRecFilter] = useState({ name: "", type: "", value: "" });
  // Search-landing highlight — ``highlightRecordId`` is passed down
  // by DNSPage, which captured it from ``location.state`` before
  // ``setSelection`` fired its ``setSearchParams(..., { replace: true })``
  // that drops state.
  const { register: registerHighlightRow, isActive: isHighlightedRow } =
    useRowHighlight(highlightRecordId ?? null);

  const handleExport = async () => {
    const { data, filename } = await dnsApi.exportZone(group.id, zone.id);
    const name =
      filename ??
      `${zone.name.replace(/\.$/, "")}-${_utcTimestampSuffix()}.zone`;
    downloadBlob(data, name, "text/dns");
  };

  // Per-server zone-serial drift pill — agents POST their loaded serial
  // to /dns/agents/zone-state after each structural apply; this endpoint
  // joins those reports with the group's server list.
  const { data: serverState } = useQuery({
    queryKey: ["zone-server-state", group.id, zone.id],
    queryFn: () => dnsApi.getZoneServerState(group.id, zone.id),
    refetchInterval: 30_000,
  });

  // Delegation wizard surface only appears when this zone has an eligible
  // parent zone in the same group — preview the parent up front so the
  // header button can hide cleanly otherwise.
  const { data: delegationPreview } = useQuery({
    queryKey: ["dns-delegation-preview", group.id, zone.id],
    queryFn: () => dnsApi.getDelegationPreview(group.id, zone.id),
    // Forward zones don't host records, so no delegation work to do.
    enabled: zone.zone_type !== "forward" && !zone.tailscale_tenant_id,
  });
  const showDelegateButton =
    delegationPreview?.has_parent === true &&
    (delegationPreview.ns_records_to_create.length > 0 ||
      delegationPreview.glue_records_to_create.length > 0);

  // "Sync with server" — bi-directional additive sync against the zone's
  // primary authoritative server (today: Windows DNS). AXFR imports missing
  // records into our DB, then every DB record not already on the wire is
  // pushed back via RFC 2136. Never deletes. Result shown in <SyncResultModal/>.
  const [syncResult, setSyncResult] = useState<SyncResultPayload | null>(null);
  const syncMut = useMutation({
    mutationFn: () => dnsApi.syncZoneWithServer(group.id, zone.id, true),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["dns-records", zone.id] });
      setSyncResult({ ok: true, ...res });
    },
    onError: (err) => {
      setSyncResult({
        ok: false,
        error: formatApiError(err, "Sync with server failed"),
      });
    },
  });

  const { data: views = [] } = useQuery({
    queryKey: ["dns-views", group.id],
    queryFn: () => dnsApi.listViews(group.id),
  });
  const { data: records = [], isFetching } = useQuery({
    queryKey: ["dns-records", zone.id],
    queryFn: () => dnsApi.listRecords(group.id, zone.id),
  });

  const deleteZone = useMutation({
    mutationFn: () => dnsApi.deleteZone(group.id, zone.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-zones", group.id] });
      onDeleted();
    },
  });

  const deleteRecord = useMutation({
    mutationFn: (r: DNSRecord) => dnsApi.deleteRecord(group.id, zone.id, r.id),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["dns-records", zone.id] }),
  });
  const [confirmDeleteRecord, setConfirmDeleteRecord] =
    useState<DNSRecord | null>(null);
  const [selectedRecords, setSelectedRecords] = useState<Set<string>>(
    new Set(),
  );
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);
  const bulkDeleteRecords = useMutation({
    // Server-side bulk endpoint: one WinRM round trip for agentless
    // drivers + a single zone-serial bump instead of N. Replaces the
    // old Promise.allSettled fan-out.
    mutationFn: (ids: string[]) =>
      dnsApi.bulkDeleteRecords(group.id, zone.id, ids),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-records", zone.id] });
      setSelectedRecords(new Set());
      setConfirmBulkDelete(false);
    },
  });

  // Forward zones have no records — they just hand queries off to the
  // listed forwarders. The detail surface below swaps the records table
  // for a forwarders/policy panel and hides the record-management buttons.
  const isForward = zone.zone_type === "forward";

  // Records / Pools sub-tab toggle. Pools live under the same zone but
  // need their own management surface — health-check config, member
  // states, manual enable toggles. Forward zones don't host records,
  // so the tab strip is hidden there.
  const [zoneView, setZoneView] = useState<"records" | "pools">("records");
  const { data: poolsForCount = [] } = useQuery({
    queryKey: ["dns-pools", group.id, zone.id],
    queryFn: () => dnsApi.listPools(group.id, zone.id),
    enabled: !isForward && !zone.tailscale_tenant_id,
  });

  const recordTypes = [...new Set(records.map((r) => r.record_type))].sort();
  const hasRecFilter = Object.values(recFilter).some(Boolean);
  const filtered = records.filter((r) => {
    if (
      recFilter.name &&
      !r.name.toLowerCase().includes(recFilter.name.toLowerCase())
    )
      return false;
    if (recFilter.type && r.record_type !== recFilter.type) return false;
    if (
      recFilter.value &&
      !r.value.toLowerCase().includes(recFilter.value.toLowerCase())
    )
      return false;
    return true;
  });

  const typeBadge: Record<string, string> = {
    A: "bg-blue-500/15 text-blue-600",
    AAAA: "bg-violet-500/15 text-violet-600",
    CNAME: "bg-amber-500/15 text-amber-600",
    MX: "bg-emerald-500/15 text-emerald-600",
    TXT: "bg-muted text-muted-foreground",
    NS: "bg-orange-500/15 text-orange-600",
    PTR: "bg-cyan-500/15 text-cyan-600",
  };

  return (
    <div className="flex flex-col h-full">
      {/* Zone header */}
      <div className="flex items-center justify-between border-b px-5 py-3">
        <div>
          <div className="flex items-center gap-2">
            {swatchCls(zone.color) ? (
              <span
                className={cn(
                  "h-3 w-3 rounded-full flex-shrink-0",
                  swatchCls(zone.color)!,
                )}
                title={`color: ${zone.color}`}
              />
            ) : (
              <FileText className="h-4 w-4 text-muted-foreground" />
            )}
            <h2 className="font-semibold text-base font-mono">
              {zone.name.replace(/\.$/, "")}
            </h2>
            <span className="inline-flex items-center rounded border px-1.5 py-0.5 text-xs">
              {zone.zone_type}
            </span>
            <span className="text-xs text-muted-foreground">{zone.kind}</span>
            {zone.dnssec_enabled && (
              <span className="inline-flex items-center rounded px-1.5 py-0.5 text-xs bg-emerald-500/15 text-emerald-600">
                DNSSEC
              </span>
            )}
            {zone.tailscale_tenant_id && (
              <span
                className="inline-flex items-center rounded px-1.5 py-0.5 text-xs bg-cyan-500/15 text-cyan-700 dark:text-cyan-300"
                title="Synthesised by the Tailscale integration. Records are derived from the device list on every sync; manual edits are blocked."
              >
                Tailscale (read-only)
              </span>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            TTL {zone.ttl}s · serial {zone.last_serial || "—"}
            {zone.primary_ns && ` · ${zone.primary_ns}`}
            {serverState && <ZoneSyncPill state={serverState} />}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {!isForward && (
            <HeaderButton
              icon={RefreshCw}
              iconClassName={isFetching ? "animate-spin" : ""}
              onClick={() => {
                // Refresh everything the zone view can show — records,
                // pools, and per-server convergence — so the button
                // works on whichever tab the operator is on. Each
                // invalidation only re-fetches if the corresponding
                // query is currently mounted, so this stays cheap.
                qc.invalidateQueries({ queryKey: ["dns-records", zone.id] });
                qc.invalidateQueries({
                  queryKey: ["dns-pools", group.id, zone.id],
                });
                qc.invalidateQueries({ queryKey: ["dns-pools"] });
                qc.invalidateQueries({
                  queryKey: ["dns-zone-server-state", zone.id],
                });
              }}
              disabled={isFetching}
              title="Reload the data shown in this view (records, pools, sync state) from SpatiumDDI — does not re-query the DNS server"
            >
              Refresh
            </HeaderButton>
          )}
          {!isForward && (
            <>
              <HeaderButton
                icon={RefreshCw}
                iconClassName={syncMut.isPending ? "animate-spin" : ""}
                onClick={() => syncMut.mutate()}
                disabled={syncMut.isPending}
                title="Two-way additive sync with the zone's authoritative server: AXFR missing records into SpatiumDDI, then push anything in our DB that isn't on the wire. Never deletes."
              >
                {syncMut.isPending ? "Syncing…" : "Sync with server"}
              </HeaderButton>
              <HeaderButton icon={Upload} onClick={() => setShowImport(true)}>
                Import
              </HeaderButton>
              <HeaderButton icon={Download} onClick={handleExport}>
                Export
              </HeaderButton>
            </>
          )}
          {showDelegateButton && (
            <HeaderButton
              icon={Workflow}
              onClick={() => setShowDelegate(true)}
              title="The parent zone is missing NS / glue records for this zone. Click to review and create them."
            >
              Delegate
            </HeaderButton>
          )}
          <HeaderButton
            icon={Pencil}
            onClick={() => setShowEditZone(true)}
            disabled={!!zone.tailscale_tenant_id}
            title={
              zone.tailscale_tenant_id
                ? "This zone is synthesised by the Tailscale integration; edits would be overwritten on the next sync."
                : undefined
            }
          >
            Edit Zone
          </HeaderButton>
          <HeaderButton
            variant="destructive"
            icon={Trash2}
            onClick={() => setConfirmDelete(true)}
            disabled={!!zone.tailscale_tenant_id}
            title={
              zone.tailscale_tenant_id
                ? "Delete the Tailscale tenant or unbind its DNS group to release this zone."
                : undefined
            }
          >
            Delete Zone
          </HeaderButton>
          {!isForward && (
            <HeaderButton
              variant="primary"
              icon={Plus}
              onClick={() => setShowAddRecord(true)}
              disabled={!!zone.tailscale_tenant_id}
              title={
                zone.tailscale_tenant_id
                  ? "Records are managed by the Tailscale reconciler."
                  : undefined
              }
            >
              Add Record
            </HeaderButton>
          )}
        </div>
      </div>

      {/* Forward-zone detail — no records, just forwarders + policy. */}
      {isForward && (
        <div className="flex-1 overflow-auto px-5 py-4">
          <div className="max-w-2xl space-y-4">
            <div className="rounded border bg-card p-4 text-sm">
              <p className="text-xs text-muted-foreground">
                <strong className="text-foreground">
                  Conditional forwarder.
                </strong>{" "}
                Queries for{" "}
                <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
                  {zone.name.replace(/\.$/, "")}
                </code>{" "}
                are sent to the upstream resolvers below — no records are stored
                in SpatiumDDI for this zone.
              </p>
            </div>

            <div className="rounded border bg-card">
              <div className="border-b px-4 py-2 text-xs font-medium text-muted-foreground">
                Forwarders
              </div>
              <div className="divide-y">
                {zone.forwarders.length === 0 ? (
                  <p className="px-4 py-3 text-sm italic text-muted-foreground">
                    No forwarders configured. Click <em>Edit Zone</em> to add
                    upstream resolvers.
                  </p>
                ) : (
                  zone.forwarders.map((ip, i) => (
                    <div
                      key={`${ip}-${i}`}
                      className="px-4 py-2 font-mono text-sm"
                    >
                      {ip}
                    </div>
                  ))
                )}
              </div>
            </div>

            <div className="rounded border bg-card px-4 py-3 text-sm">
              <div className="text-xs font-medium text-muted-foreground">
                Policy
              </div>
              <div className="mt-1">
                {zone.forward_only ? (
                  <span className="font-mono">forward only</span>
                ) : (
                  <span className="font-mono">forward first</span>
                )}
                <p className="mt-1 text-xs text-muted-foreground">
                  {zone.forward_only
                    ? "Queries are sent only to the forwarders. If they all fail, BIND returns SERVFAIL — it never falls back to recursion."
                    : "Queries are sent to the forwarders first. If they all fail, BIND falls back to normal recursion via the root servers."}
                </p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Records / Pools tab strip — primary zones only. */}
      {!isForward && !zone.tailscale_tenant_id && (
        <div className="flex gap-1 border-b px-5">
          <button
            type="button"
            onClick={() => setZoneView("records")}
            className={
              "border-b-2 px-3 py-2 text-xs font-medium transition-colors " +
              (zoneView === "records"
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground")
            }
          >
            Records ({records.length})
          </button>
          <button
            type="button"
            onClick={() => setZoneView("pools")}
            className={
              "border-b-2 px-3 py-2 text-xs font-medium transition-colors " +
              (zoneView === "pools"
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground")
            }
          >
            Pools ({poolsForCount.length})
          </button>
        </div>
      )}

      {/* Pools sub-view */}
      {!isForward && !zone.tailscale_tenant_id && zoneView === "pools" && (
        <PoolsView group={group} zone={zone} />
      )}

      {/* Bulk actions — shown when any manual records are selected. */}
      {!isForward && zoneView === "records" && selectedRecords.size > 0 && (
        <div className="flex items-center justify-between border-b bg-amber-50 px-5 py-1.5 text-xs dark:bg-amber-900/10">
          <span>
            {selectedRecords.size} record
            {selectedRecords.size !== 1 ? "s" : ""} selected
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setSelectedRecords(new Set())}
              className="rounded-md border px-2 py-1 hover:bg-muted"
            >
              Clear
            </button>
            <button
              onClick={() => setConfirmBulkDelete(true)}
              className="flex items-center gap-1 rounded-md border border-destructive/40 px-2 py-1 text-destructive hover:bg-destructive/10"
            >
              <Trash2 className="h-3 w-3" /> Delete selected
            </button>
          </div>
        </div>
      )}

      {/* Records table */}
      {!isForward && zoneView === "records" && (
        <div className="flex-1 overflow-auto">
          {isFetching && records.length === 0 && (
            <p className="px-5 py-4 text-sm text-muted-foreground">Loading…</p>
          )}
          {filtered.length === 0 && !isFetching && (
            <div className="flex flex-col items-center justify-center h-40">
              <p className="text-sm text-muted-foreground italic">
                {hasRecFilter
                  ? "No records match the current filter."
                  : 'No records yet. Click "Add Record" to create one.'}
              </p>
            </div>
          )}
          {(filtered.length > 0 || showRecFilters) && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-sm">
                <thead className="sticky top-0 bg-card">
                  <tr className="border-b text-xs text-muted-foreground">
                    <th className="w-8 py-2 pl-3">
                      {(() => {
                        const manualIds = filtered
                          .filter((r) => !r.auto_generated && !r.pool_member_id)
                          .map((r) => r.id);
                        const allSel =
                          manualIds.length > 0 &&
                          manualIds.every((id) => selectedRecords.has(id));
                        return (
                          <input
                            type="checkbox"
                            disabled={manualIds.length === 0}
                            checked={allSel}
                            onChange={() => {
                              setSelectedRecords((prev) => {
                                const next = new Set(prev);
                                if (allSel)
                                  manualIds.forEach((id) => next.delete(id));
                                else manualIds.forEach((id) => next.add(id));
                                return next;
                              });
                            }}
                            title="Select all manual records (IPAM-managed records are skipped)"
                          />
                        );
                      })()}
                    </th>
                    {(["Name", "Type", "Value", "TTL", "Pri"] as const).map(
                      (col) => {
                        const filterKey =
                          col === "Name"
                            ? "name"
                            : col === "Type"
                              ? "type"
                              : col === "Value"
                                ? "value"
                                : null;
                        const hasFilter = filterKey
                          ? !!recFilter[filterKey as keyof typeof recFilter]
                          : false;
                        return (
                          <th
                            key={col}
                            className={
                              col === "Name"
                                ? "py-2 pl-5 text-left font-medium"
                                : "py-2 text-left font-medium"
                            }
                          >
                            <span className="inline-flex items-center gap-1">
                              {col}
                              {filterKey && (
                                <button
                                  onClick={() => setShowRecFilters((v) => !v)}
                                  title={`Filter by ${col}`}
                                  className={`rounded p-0.5 hover:bg-accent ${hasFilter ? "text-primary" : showRecFilters || hasRecFilter ? "text-primary/50" : "text-muted-foreground/40 hover:text-muted-foreground"}`}
                                >
                                  <Filter className="h-2.5 w-2.5" />
                                </button>
                              )}
                            </span>
                          </th>
                        );
                      },
                    )}
                    <th className="py-2 pr-3 text-right">
                      {hasRecFilter && (
                        <button
                          onClick={() =>
                            setRecFilter({ name: "", type: "", value: "" })
                          }
                          title="Clear filters"
                          className="rounded p-0.5 text-primary hover:text-destructive"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      )}
                    </th>
                  </tr>
                  {showRecFilters && (
                    <tr className="border-b bg-muted/10 text-xs">
                      <td />
                      <td className="px-2 py-1 pl-5">
                        <input
                          type="text"
                          value={recFilter.name}
                          onChange={(e) =>
                            setRecFilter((f) => ({
                              ...f,
                              name: e.target.value,
                            }))
                          }
                          placeholder="Filter…"
                          className="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                        />
                      </td>
                      <td className="px-2 py-1">
                        <select
                          value={recFilter.type}
                          onChange={(e) =>
                            setRecFilter((f) => ({
                              ...f,
                              type: e.target.value,
                            }))
                          }
                          className="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                        >
                          <option value="">All</option>
                          {recordTypes.map((t) => (
                            <option key={t} value={t}>
                              {t}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="px-2 py-1">
                        <input
                          type="text"
                          value={recFilter.value}
                          onChange={(e) =>
                            setRecFilter((f) => ({
                              ...f,
                              value: e.target.value,
                            }))
                          }
                          placeholder="Filter…"
                          className="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                        />
                      </td>
                      <td />
                      <td />
                      <td />
                    </tr>
                  )}
                </thead>
                <tbody className={zebraBodyCls}>
                  {filtered.map((r) => (
                    <ContextMenu key={r.id}>
                      <ContextMenuTrigger asChild>
                        <tr
                          ref={registerHighlightRow(r.id)}
                          className={cn(
                            "border-b last:border-0 hover:bg-muted/40 group",
                            isHighlightedRow(r.id) && "spatium-row-highlight",
                          )}
                        >
                          <td className="w-8 py-1.5 pl-3">
                            {!r.auto_generated && !r.pool_member_id && (
                              <input
                                type="checkbox"
                                checked={selectedRecords.has(r.id)}
                                onChange={() =>
                                  setSelectedRecords((prev) => {
                                    const next = new Set(prev);
                                    if (next.has(r.id)) next.delete(r.id);
                                    else next.add(r.id);
                                    return next;
                                  })
                                }
                              />
                            )}
                          </td>
                          <td className="py-1.5 pl-5 font-mono text-xs font-medium">
                            {r.auto_generated || r.pool_member_id ? (
                              r.name
                            ) : (
                              <button
                                onClick={() => setEditRecord(r)}
                                className="hover:text-primary hover:underline"
                                title="Edit record"
                              >
                                {r.name}
                              </button>
                            )}
                          </td>
                          <td className="py-1.5">
                            <span
                              className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${typeBadge[r.record_type] ?? "bg-muted text-muted-foreground"}`}
                            >
                              {r.record_type}
                            </span>
                          </td>
                          <td className="py-1.5 font-mono text-xs text-muted-foreground max-w-xs truncate">
                            {r.value}
                          </td>
                          <td className="py-1.5 text-xs text-muted-foreground">
                            {r.ttl ?? "—"}
                          </td>
                          <td className="py-1.5 text-xs text-muted-foreground">
                            {r.priority ?? "—"}
                          </td>
                          <td className="py-1.5 pr-3">
                            {r.pool_member_id ? (
                              <div className="flex items-center justify-end gap-1">
                                <span
                                  title="This record is rendered by a DNS pool's health-check pipeline. Manage it from the Pools tab — direct edits are blocked."
                                  className="flex items-center gap-1 rounded border border-violet-300/60 bg-violet-50 px-1.5 py-0.5 text-xs text-violet-700 dark:border-violet-700/40 dark:bg-violet-900/20 dark:text-violet-300"
                                >
                                  <Lock className="h-2.5 w-2.5" />
                                  Pool
                                </span>
                                <button
                                  className="h-6 w-6 flex items-center justify-center rounded text-muted-foreground hover:text-foreground"
                                  onClick={() => setZoneView("pools")}
                                  title="Manage in Pools tab"
                                >
                                  <Info className="h-3 w-3" />
                                </button>
                              </div>
                            ) : r.auto_generated ? (
                              <div className="flex items-center justify-end gap-1">
                                {r.tailscale_tenant_id ? (
                                  <span
                                    title="Synthesised by the Tailscale integration. Records are derived from the device list on every sync; manual edits are blocked."
                                    className="flex items-center gap-1 rounded border border-cyan-300/60 bg-cyan-50 px-1.5 py-0.5 text-xs text-cyan-700 dark:border-cyan-700/40 dark:bg-cyan-900/20 dark:text-cyan-300"
                                  >
                                    <Lock className="h-2.5 w-2.5" />
                                    Tailscale
                                  </span>
                                ) : (
                                  <span
                                    title="This record was created automatically by IPAM. Edit the IP address in IPAM to change it."
                                    className="flex items-center gap-1 rounded border border-amber-300/60 bg-amber-50 px-1.5 py-0.5 text-xs text-amber-700 dark:border-amber-700/40 dark:bg-amber-900/20 dark:text-amber-400"
                                  >
                                    <Lock className="h-2.5 w-2.5" />
                                    IPAM
                                  </span>
                                )}
                                <span
                                  title="Managed externally — changes made here will be overwritten on the next sync."
                                  className="flex h-5 w-5 cursor-help items-center justify-center rounded text-muted-foreground/60 hover:text-muted-foreground"
                                >
                                  <Info className="h-3 w-3" />
                                </span>
                              </div>
                            ) : (
                              <div className="flex items-center justify-end gap-1">
                                <button
                                  className="h-6 w-6 flex items-center justify-center rounded text-muted-foreground hover:text-foreground"
                                  onClick={() => setPropagationRecord(r)}
                                  title="Check propagation across public resolvers"
                                >
                                  <Radar className="h-3 w-3" />
                                </button>
                                <button
                                  className="h-6 w-6 flex items-center justify-center rounded text-muted-foreground hover:text-foreground"
                                  onClick={() => setEditRecord(r)}
                                  title="Edit record"
                                >
                                  <Pencil className="h-3 w-3" />
                                </button>
                                <button
                                  className="h-6 w-6 flex items-center justify-center rounded text-muted-foreground hover:text-destructive"
                                  onClick={() => setConfirmDeleteRecord(r)}
                                  title="Delete record"
                                >
                                  <Trash2 className="h-3 w-3" />
                                </button>
                              </div>
                            )}
                          </td>
                        </tr>
                      </ContextMenuTrigger>
                      <ContextMenuContent>
                        <ContextMenuLabel>
                          {r.name} {r.record_type}
                        </ContextMenuLabel>
                        <ContextMenuSeparator />
                        <ContextMenuItem
                          onSelect={() => copyToClipboard(r.name)}
                        >
                          Copy Name
                        </ContextMenuItem>
                        <ContextMenuItem
                          onSelect={() => copyToClipboard(r.value)}
                        >
                          Copy Value
                        </ContextMenuItem>
                        {r.auto_generated ? (
                          <>
                            <ContextMenuSeparator />
                            <ContextMenuItem disabled>
                              Managed by IPAM — read-only
                            </ContextMenuItem>
                          </>
                        ) : (
                          <>
                            <ContextMenuSeparator />
                            <ContextMenuItem onSelect={() => setEditRecord(r)}>
                              Edit…
                            </ContextMenuItem>
                            <ContextMenuItem
                              destructive
                              onSelect={() => setConfirmDeleteRecord(r)}
                            >
                              Delete…
                            </ContextMenuItem>
                          </>
                        )}
                      </ContextMenuContent>
                    </ContextMenu>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {showAddRecord && (
        <RecordModal
          groupId={group.id}
          zoneId={zone.id}
          zoneName={zone.name}
          onClose={() => setShowAddRecord(false)}
        />
      )}
      {editRecord && (
        <RecordModal
          groupId={group.id}
          zoneId={zone.id}
          zoneName={zone.name}
          record={editRecord}
          onClose={() => setEditRecord(null)}
        />
      )}
      {propagationRecord && (
        <PropagationCheckModal
          fqdn={propagationRecord.fqdn}
          recordType={propagationRecord.record_type}
          onClose={() => setPropagationRecord(null)}
        />
      )}
      {confirmDeleteRecord && (
        <ConfirmSingleModal
          title="Delete Record"
          description={
            <>
              Delete{" "}
              <span className="font-mono">
                {confirmDeleteRecord.name} {confirmDeleteRecord.record_type}
              </span>
              ? This will remove the record from the zone and fire an RFC 2136
              update.
            </>
          }
          isPending={deleteRecord.isPending}
          onConfirm={() =>
            deleteRecord.mutate(confirmDeleteRecord, {
              onSuccess: () => setConfirmDeleteRecord(null),
            })
          }
          onClose={() => setConfirmDeleteRecord(null)}
        />
      )}
      {confirmBulkDelete && (
        <ConfirmSingleModal
          title={`Delete ${selectedRecords.size} records`}
          description={
            <>
              Delete the{" "}
              <span className="font-medium">{selectedRecords.size}</span>{" "}
              selected records? IPAM-managed records are excluded automatically.
            </>
          }
          isPending={bulkDeleteRecords.isPending}
          onConfirm={() =>
            bulkDeleteRecords.mutate(Array.from(selectedRecords))
          }
          onClose={() => setConfirmBulkDelete(false)}
        />
      )}
      {showEditZone && (
        <ZoneModal
          groupId={group.id}
          views={views}
          zone={zone}
          onClose={() => setShowEditZone(false)}
        />
      )}
      {showDelegate && (
        <DelegationModal
          groupId={group.id}
          zoneId={zone.id}
          zoneName={zone.name}
          onClose={() => setShowDelegate(false)}
        />
      )}
      {showImport && (
        <ImportZoneModal
          groupId={group.id}
          zone={zone}
          onClose={() => setShowImport(false)}
        />
      )}
      {confirmDelete && (
        <ConfirmDestroyModal
          title="Delete DNS Zone"
          description={`Permanently delete zone "${zone.name}" and all its records from SpatiumDDI?`}
          checkLabel={`I understand all records in "${zone.name}" will be permanently deleted.`}
          onConfirm={() => deleteZone.mutate()}
          onClose={() => setConfirmDelete(false)}
          isPending={deleteZone.isPending}
        />
      )}
      {syncResult && (
        <SyncResultModal
          zoneName={zone.name}
          result={syncResult}
          onClose={() => setSyncResult(null)}
        />
      )}
    </div>
  );
}

// ── Sync-with-server result modal ─────────────────────────────────────────────
//
// Surfaces the per-run summary from POST /dns/groups/{id}/zones/{id}/sync-with-server
// — counts for both directions (pulled from server → DB, pushed from DB → server)
// plus per-record lists and any push errors that came back.

type SyncRecord = {
  name: string;
  fqdn: string;
  record_type: string;
  value: string;
  ttl: number | null;
};

type SyncResultPayload =
  | {
      ok: true;
      // pull direction
      server_records: number;
      existing_in_db: number;
      imported: number;
      skipped_unsupported: number;
      imported_records: SyncRecord[];
      // push direction
      push_candidates: number;
      pushed: number;
      pushed_records: SyncRecord[];
      push_errors: string[];
    }
  | { ok: false; error: string };

function SyncResultModal({
  zoneName,
  result,
  onClose,
}: {
  zoneName: string;
  result: SyncResultPayload;
  onClose: () => void;
}) {
  if (!result.ok) {
    return (
      <Modal title="Sync with server — failed" onClose={onClose}>
        <div className="space-y-3">
          <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
            {result.error}
          </div>
          <p className="text-xs text-muted-foreground">
            Common causes: zone transfers not allowed from this host (DNS
            Manager → zone → Properties → Zone Transfers), dynamic updates not
            permitted, primary server unreachable, or the driver does not
            support AXFR pull (only Windows DNS does today).
          </p>
          <div className="flex justify-end pt-1">
            <button
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

  const {
    server_records,
    existing_in_db,
    imported,
    skipped_unsupported,
    imported_records,
    pushed,
    pushed_records,
    push_errors,
  } = result;
  const somethingHappened =
    imported > 0 || pushed > 0 || push_errors.length > 0;
  const wide = imported_records.length > 0 || pushed_records.length > 0;

  return (
    <Modal
      title={`Sync with server — ${zoneName.replace(/\.$/, "")}`}
      onClose={onClose}
      wide={wide}
    >
      <div className="space-y-4">
        {/* Pull direction */}
        <div>
          <div className="mb-2 text-xs font-medium text-muted-foreground">
            ⬇ Server → SpatiumDDI (pull)
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="On server" value={server_records} />
            <Stat label="Already in DB" value={existing_in_db} />
            <Stat
              label="Imported"
              value={imported}
              highlight={imported > 0 ? "good" : undefined}
            />
            <Stat
              label="Skipped"
              value={skipped_unsupported}
              hint="unsupported record type"
            />
          </div>
          {imported > 0 && (
            <RecordTable
              records={imported_records}
              heading="New records added to SpatiumDDI"
            />
          )}
        </div>

        {/* Push direction */}
        <div>
          <div className="mb-2 text-xs font-medium text-muted-foreground">
            ⬆ SpatiumDDI → Server (push)
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <Stat
              label="Pushed"
              value={pushed}
              highlight={
                pushed > 0 && push_errors.length === 0 ? "good" : undefined
              }
            />
            <Stat
              label="Errors"
              value={push_errors.length}
              highlight={push_errors.length > 0 ? "bad" : undefined}
            />
            <Stat label="Skipped" value={0} hint="DB already matches server" />
          </div>
          {pushed > 0 && (
            <RecordTable
              records={pushed_records}
              heading="Records applied to the server"
            />
          )}
          {push_errors.length > 0 && (
            <div className="mt-2 rounded-md border border-destructive/30 bg-destructive/10 p-3">
              <div className="mb-1 text-xs font-medium text-destructive">
                Push errors ({push_errors.length})
              </div>
              <ul className="list-disc space-y-0.5 pl-4 text-xs text-destructive">
                {push_errors.map((e, i) => (
                  <li key={i} className="font-mono break-all">
                    {e}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        {!somethingHappened && (
          <p className="text-xs text-muted-foreground">
            Already in sync — the DB and the authoritative server hold the same
            records.
          </p>
        )}

        <div className="flex justify-end pt-1">
          <button
            onClick={onClose}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
          >
            Done
          </button>
        </div>
      </div>
    </Modal>
  );
}

function RecordTable({
  records,
  heading,
}: {
  records: SyncRecord[];
  heading: string;
}) {
  return (
    <div className="mt-2 rounded-md border">
      <div className="border-b px-3 py-2 text-xs font-medium text-muted-foreground">
        {heading}
      </div>
      <div className="max-h-60 overflow-auto">
        <table className="w-full text-xs">
          <thead className="bg-muted/40">
            <tr className="text-left">
              <th className="px-3 py-1.5 font-medium">Name</th>
              <th className="px-3 py-1.5 font-medium">Type</th>
              <th className="px-3 py-1.5 font-medium">Value</th>
              <th className="px-3 py-1.5 font-medium">TTL</th>
            </tr>
          </thead>
          <tbody className={zebraBodyCls}>
            {records.map((r, i) => (
              <tr key={i} className="border-t">
                <td className="px-3 py-1 font-mono">{r.name}</td>
                <td className="px-3 py-1">
                  <span className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium">
                    {r.record_type}
                  </span>
                </td>
                <td className="px-3 py-1 font-mono break-all">{r.value}</td>
                <td className="px-3 py-1 text-muted-foreground">
                  {r.ttl ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  highlight,
  hint,
}: {
  label: string;
  value: number;
  highlight?: "good" | "bad";
  hint?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-md border p-3",
        highlight === "good" &&
          "border-emerald-500/30 bg-emerald-500/5 dark:bg-emerald-500/10",
        highlight === "bad" &&
          "border-destructive/30 bg-destructive/5 dark:bg-destructive/10",
      )}
    >
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-xl font-semibold tabular-nums">{value}</div>
      {hint && (
        <div className="mt-0.5 text-[10px] text-muted-foreground/70">
          {hint}
        </div>
      )}
    </div>
  );
}

// ── Servers Tab ────────────────────────────────────────────────────────────────

function ServersTab({ group }: { group: DNSServerGroup }) {
  const qc = useQueryClient();
  const [showAdd, setShowAdd] = useState(false);
  const [editServer, setEditServer] = useState<DNSServer | null>(null);
  const [detailServer, setDetailServer] = useState<DNSServer | null>(null);
  const [confirmDeleteServer, setConfirmDeleteServer] =
    useState<DNSServer | null>(null);

  const { data: servers = [], isFetching } = useQuery({
    queryKey: ["dns-servers", group.id],
    queryFn: () => dnsApi.listServers(group.id),
    // Refetch frequently so the health dot stays fresh as the Celery
    // Beat-scheduled dns-health-sweep task updates server rows.
    refetchInterval: 30_000,
  });
  const { data: zones = [] } = useQuery({
    queryKey: ["dns-zones", group.id],
    queryFn: () => dnsApi.listZones(group.id),
  });

  const healthCounts = servers.reduce(
    (acc, s) => {
      acc[s.status] = (acc[s.status] ?? 0) + 1;
      return acc;
    },
    {} as Record<string, number>,
  );

  const del = useMutation({
    mutationFn: (s: DNSServer) => dnsApi.deleteServer(group.id, s.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-servers", group.id] });
      setConfirmDeleteServer(null);
    },
  });

  const statusCls: Record<string, string> = {
    active: "bg-emerald-500/15 text-emerald-600",
    unreachable: "bg-red-500/15 text-red-600",
    syncing: "bg-blue-500/15 text-blue-600",
    error: "bg-red-500/15 text-red-600",
    disabled: "bg-muted text-muted-foreground",
  };
  const dotCls: Record<string, string> = {
    active: "bg-emerald-500",
    unreachable: "bg-red-500",
    syncing: "bg-blue-500",
    error: "bg-red-500",
    disabled: "bg-muted-foreground/40",
  };

  return (
    <div>
      {servers.length > 0 && (
        <div className="mb-4 rounded-md border bg-card p-3">
          <div className="flex items-center gap-4 flex-wrap text-xs">
            <span className="font-medium text-muted-foreground uppercase tracking-wider">
              Health
            </span>
            {(["active", "unreachable", "syncing", "error"] as const).map(
              (s) =>
                healthCounts[s] ? (
                  <span key={s} className="flex items-center gap-1.5">
                    <span
                      className={`inline-block h-2 w-2 rounded-full ${dotCls[s]}`}
                    />
                    {healthCounts[s]} {s}
                  </span>
                ) : null,
            )}
            <span className="ml-auto text-muted-foreground">
              Zone serials:{" "}
              {zones.length === 0
                ? "no zones"
                : `${zones.length} zone${zones.length === 1 ? "" : "s"} · all servers assumed consistent (per-server serials arrive in Wave 3)`}
            </span>
          </div>
        </div>
      )}
      <div className="flex items-center justify-between mb-4">
        <div>
          <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
            DNS Servers
          </span>
          <p className="text-xs text-muted-foreground mt-0.5">
            Servers can also be auto-registered by BIND9 agent containers using
            the <code className="font-mono">DNS_AGENT_KEY</code> env var.
          </p>
        </div>
        <button
          className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
          onClick={() => setShowAdd(true)}
        >
          <Plus className="h-3 w-3" /> Add Server
        </button>
      </div>
      {isFetching && servers.length === 0 && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}
      {servers.length === 0 && !isFetching && (
        <p className="text-sm text-muted-foreground italic">
          No servers. Add one manually or start a DNS agent container.
        </p>
      )}
      <div className="space-y-2">
        {servers.map((s) => (
          <div
            key={s.id}
            className="flex items-center justify-between rounded-md border bg-card px-3 py-2.5 group cursor-pointer hover:bg-accent/40"
            onClick={() => setDetailServer(s)}
            title="Click to view details"
          >
            <div className="flex items-center gap-3">
              <Cpu className="h-4 w-4 text-muted-foreground flex-shrink-0" />
              <div>
                <div className="flex items-center gap-2">
                  <span
                    className={`inline-block h-2 w-2 rounded-full ${dotCls[s.status] ?? "bg-muted"}`}
                    title={`status: ${s.status}${s.last_health_check_at ? ` · last check: ${new Date(s.last_health_check_at).toLocaleString()}` : " · never checked"}`}
                  />
                  <span className="text-sm font-medium">{s.name}</span>
                  <span
                    className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${statusCls[s.status] ?? "bg-muted text-muted-foreground"}`}
                  >
                    {s.status}
                  </span>
                  <span className="inline-flex items-center rounded border px-1.5 py-0.5 text-xs">
                    {s.driver}
                  </span>
                </div>
                <p className="text-xs text-muted-foreground">
                  {s.host}:{s.port}
                  {s.roles.length > 0 && ` · ${s.roles.join(", ")}`}
                  {s.last_sync_at &&
                    ` · synced ${new Date(s.last_sync_at).toLocaleDateString()}`}
                  {s.last_health_check_at &&
                    ` · health ${new Date(s.last_health_check_at).toLocaleTimeString()}`}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-1">
              <button
                className="h-7 w-7 flex items-center justify-center rounded text-muted-foreground hover:text-foreground"
                onClick={(e) => {
                  e.stopPropagation();
                  setEditServer(s);
                }}
              >
                <Pencil className="h-3.5 w-3.5" />
              </button>
              <button
                className="h-7 w-7 flex items-center justify-center rounded text-muted-foreground hover:text-destructive"
                onClick={(e) => {
                  e.stopPropagation();
                  setConfirmDeleteServer(s);
                }}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        ))}
      </div>
      {showAdd && (
        <ServerModal groupId={group.id} onClose={() => setShowAdd(false)} />
      )}
      {editServer && (
        <ServerModal
          groupId={group.id}
          server={editServer}
          onClose={() => setEditServer(null)}
        />
      )}
      {detailServer && (
        <ServerDetailModal
          server={detailServer}
          onClose={() => setDetailServer(null)}
        />
      )}
      {confirmDeleteServer && (
        <ConfirmDestroyModal
          title="Delete DNS Server"
          description={`Remove "${confirmDeleteServer.name}" (${confirmDeleteServer.host}:${confirmDeleteServer.port}) from this group?`}
          checkLabel="I understand this server will be removed from SpatiumDDI management."
          onConfirm={() => del.mutate(confirmDeleteServer)}
          onClose={() => setConfirmDeleteServer(null)}
          isPending={del.isPending}
        />
      )}
    </div>
  );
}

function SyncStat({
  label,
  value,
  accent,
}: {
  label: string;
  value: number | string;
  accent?: "good" | "bad";
}) {
  const color =
    accent === "good"
      ? "text-emerald-600 dark:text-emerald-400"
      : accent === "bad"
        ? "text-destructive"
        : "text-foreground";
  return (
    <div className="rounded-md border bg-card px-2.5 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className={`text-lg font-semibold tabular-nums ${color}`}>
        {value}
      </div>
    </div>
  );
}

// ── Views Tab ─────────────────────────────────────────────────────────────────

function ViewsTab({ group }: { group: DNSServerGroup }) {
  const { data: views = [] } = useQuery({
    queryKey: ["dns-views", group.id],
    queryFn: () => dnsApi.listViews(group.id),
  });

  return (
    <div>
      {views.length === 0 ? (
        <p className="text-sm text-muted-foreground italic">
          No views defined. Views enable split-horizon DNS.
        </p>
      ) : (
        <div className="space-y-2">
          {views.map((v) => (
            <div key={v.id} className="rounded-md border bg-card px-3 py-2.5">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{v.name}</span>
                <span className="text-xs text-muted-foreground">
                  order: {v.order}
                </span>
              </div>
              {v.description && (
                <p className="text-xs text-muted-foreground mt-0.5">
                  {v.description}
                </p>
              )}
              <div className="mt-1.5 flex flex-wrap gap-1">
                {v.match_clients.map((c) => (
                  <span
                    key={c}
                    className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-xs font-mono"
                  >
                    {c}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── ACLs Tab ──────────────────────────────────────────────────────────────────

function AclsTab({ groupId }: { groupId: string }) {
  const qc = useQueryClient();
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [newEntries, setNewEntries] = useState("");
  const [error, setError] = useState("");

  const { data: acls = [] } = useQuery({
    queryKey: ["dns-acls", groupId],
    queryFn: () => dnsApi.listAcls(groupId),
  });

  const createMut = useMutation({
    mutationFn: (d: Record<string, unknown>) => dnsApi.createAcl(groupId, d),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-acls", groupId] });
      setShowCreate(false);
      setNewName("");
      setNewDesc("");
      setNewEntries("");
    },
    onError: (e: ApiError) => setError(formatApiError(e)),
  });
  const delMut = useMutation({
    mutationFn: (id: string) => dnsApi.deleteAcl(groupId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dns-acls", groupId] }),
  });

  function createAcl(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const entries = newEntries
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean)
      .map((val, i) => ({
        value: val.startsWith("!") ? val.slice(1) : val,
        negate: val.startsWith("!"),
        order: i,
      }));
    createMut.mutate({ name: newName, description: newDesc, entries });
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
          Named ACLs
        </span>
        <button
          className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
          onClick={() => setShowCreate(true)}
        >
          <Plus className="h-3 w-3" /> New ACL
        </button>
      </div>
      {showCreate && (
        <form
          onSubmit={createAcl}
          className="mb-4 rounded-md border bg-muted/30 p-3 space-y-3"
        >
          <div className="grid grid-cols-2 gap-3">
            <Field label="ACL Name">
              <input
                className={inputCls}
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="internal-clients"
                required
              />
            </Field>
            <Field label="Description">
              <input
                className={inputCls}
                value={newDesc}
                onChange={(e) => setNewDesc(e.target.value)}
                placeholder="Optional"
              />
            </Field>
          </div>
          <Field label="Entries (one per line; prefix ! to negate)">
            <textarea
              value={newEntries}
              onChange={(e) => setNewEntries(e.target.value)}
              className="w-full rounded border bg-background px-2 py-1 font-mono text-xs resize-none h-20 focus:outline-none focus:ring-1 focus:ring-ring"
              placeholder={"10.0.0.0/8\n192.168.0.0/16\n!198.51.100.0/24"}
            />
          </Field>
          {error && <p className="text-xs text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="rounded-md border px-2 py-1 text-xs hover:bg-accent"
              onClick={() => setShowCreate(false)}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMut.isPending}
              className="rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground disabled:opacity-50"
            >
              Create
            </button>
          </div>
        </form>
      )}
      {acls.length === 0 && !showCreate && (
        <p className="text-sm text-muted-foreground italic">
          No named ACLs defined.
        </p>
      )}
      <div className="space-y-2">
        {acls.map((acl) => (
          <div
            key={acl.id}
            className="rounded-md border bg-card px-3 py-2.5 group"
          >
            <div className="flex items-center justify-between">
              <div>
                <span className="text-sm font-medium font-mono">
                  {acl.name}
                </span>
                {acl.description && (
                  <span className="ml-2 text-xs text-muted-foreground">
                    {acl.description}
                  </span>
                )}
              </div>
              <button
                className="h-7 w-7 flex items-center justify-center rounded opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive"
                onClick={() => {
                  if (confirm(`Delete ACL "${acl.name}"?`))
                    delMut.mutate(acl.id);
                }}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
            {acl.entries.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {acl.entries.map((entry) => (
                  <span
                    key={entry.id}
                    className={`inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-xs font-mono ${entry.negate ? "line-through opacity-60" : ""}`}
                  >
                    {entry.negate ? "!" : ""}
                    {entry.value}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── TSIG Keys Tab ─────────────────────────────────────────────────────────────

function TSIGKeysTab({ group }: { group: DNSServerGroup }) {
  const qc = useQueryClient();
  const { data: keys = [], isFetching } = useQuery({
    queryKey: ["dns-tsig-keys", group.id],
    queryFn: () => dnsApi.listTSIGKeys(group.id),
  });
  const [showAdd, setShowAdd] = useState(false);
  const [editKey, setEditKey] = useState<DNSTSIGKey | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<DNSTSIGKey | null>(null);
  const [confirmRotate, setConfirmRotate] = useState<DNSTSIGKey | null>(null);
  // Lives across modals — surfaces the freshly-issued plaintext secret one
  // last time after create / rotate so the operator can copy it.
  const [revealedSecret, setRevealedSecret] = useState<{
    name: string;
    algorithm: string;
    secret: string;
    action: "created" | "rotated";
  } | null>(null);

  const deleteMut = useMutation({
    mutationFn: (id: string) => dnsApi.deleteTSIGKey(group.id, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-tsig-keys", group.id] });
      setConfirmDelete(null);
    },
  });
  const rotateMut = useMutation({
    mutationFn: (id: string) => dnsApi.rotateTSIGKey(group.id, id),
    onSuccess: (key) => {
      qc.invalidateQueries({ queryKey: ["dns-tsig-keys", group.id] });
      setConfirmRotate(null);
      if (key.secret) {
        setRevealedSecret({
          name: key.name,
          algorithm: key.algorithm,
          secret: key.secret,
          action: "rotated",
        });
      }
    },
  });

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
          {keys.length} TSIG key{keys.length !== 1 ? "s" : ""}
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={() =>
              qc.invalidateQueries({ queryKey: ["dns-tsig-keys", group.id] })
            }
            className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted/50"
            disabled={isFetching}
          >
            <RefreshCw
              className={cn("h-3 w-3", isFetching && "animate-spin")}
            />
            Refresh
          </button>
          <button
            onClick={() => setShowAdd(true)}
            className="flex items-center gap-1 rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90"
          >
            <Plus className="h-3 w-3" /> New TSIG Key
          </button>
        </div>
      </div>

      <p className="rounded border bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
        Named TSIG keys distributed to every BIND9 agent in this group. Use them
        to authenticate external <code>nsupdate</code> clients (allow-update) or
        downstream secondaries pulling AXFR (allow-transfer). Reference a key
        from a zone's allow-update / allow-transfer field as{" "}
        <code>key {"<name>;"}</code>.
      </p>

      {keys.length === 0 && (
        <p className="text-xs italic text-muted-foreground">
          No TSIG keys yet. Click <em>New TSIG Key</em> to add one.
        </p>
      )}

      <div className="overflow-x-auto rounded border">
        <table className="w-full min-w-[640px] text-sm">
          <thead className="bg-muted/30 text-xs text-muted-foreground">
            <tr className="border-b">
              <th className="px-3 py-1.5 text-left font-medium">Name</th>
              <th className="px-2 py-1.5 text-left font-medium">Algorithm</th>
              <th className="px-2 py-1.5 text-left font-medium">Purpose</th>
              <th className="px-2 py-1.5 text-left font-medium">
                Last rotated
              </th>
              <th className="px-3 py-1.5 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {keys.map((k) => (
              <tr
                key={k.id}
                className="border-b last:border-0 hover:bg-muted/20"
              >
                <td className="px-3 py-1.5 font-mono text-xs">{k.name}</td>
                <td className="px-2 py-1.5 font-mono text-xs">{k.algorithm}</td>
                <td className="px-2 py-1.5 text-xs text-muted-foreground">
                  {k.purpose ?? "—"}
                </td>
                <td className="px-2 py-1.5 text-xs text-muted-foreground">
                  {k.last_rotated_at
                    ? new Date(k.last_rotated_at).toLocaleString()
                    : "never"}
                </td>
                <td className="px-3 py-1.5 text-right">
                  <div className="inline-flex items-center gap-1">
                    <button
                      onClick={() => setConfirmRotate(k)}
                      className="rounded border px-2 py-0.5 text-xs hover:bg-muted/50"
                      title="Generate a fresh secret of the same algorithm and replace the stored one"
                    >
                      Rotate
                    </button>
                    <button
                      onClick={() => setEditKey(k)}
                      className="h-6 w-6 inline-flex items-center justify-center rounded hover:bg-muted/50"
                      title="Edit metadata"
                    >
                      <Pencil className="h-3 w-3" />
                    </button>
                    <button
                      onClick={() => setConfirmDelete(k)}
                      className="h-6 w-6 inline-flex items-center justify-center rounded text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                      title="Delete key"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {showAdd && (
        <TSIGKeyModal
          groupId={group.id}
          onClose={() => setShowAdd(false)}
          onCreated={(secret) => {
            setShowAdd(false);
            setRevealedSecret({ ...secret, action: "created" });
          }}
        />
      )}
      {editKey && (
        <TSIGKeyModal
          groupId={group.id}
          existing={editKey}
          onClose={() => setEditKey(null)}
        />
      )}
      {confirmDelete && (
        <ConfirmDestroyModal
          title="Delete TSIG Key"
          description={`Permanently delete "${confirmDelete.name}"? Any zones referencing this key in their allow-update / allow-transfer fields will start failing auth on the next BIND reload.`}
          checkLabel={`I understand "${confirmDelete.name}" will be deleted from every BIND9 server in this group on the next config push.`}
          onConfirm={() => deleteMut.mutate(confirmDelete.id)}
          onClose={() => setConfirmDelete(null)}
          isPending={deleteMut.isPending}
        />
      )}
      {confirmRotate && (
        <ConfirmDestroyModal
          title="Rotate TSIG Key"
          description={`Generate a new secret for "${confirmRotate.name}"? The old secret stops working immediately on the next BIND9 push — every consuming client must be updated with the new value.`}
          checkLabel={`I understand consumers of "${confirmRotate.name}" will break until reconfigured.`}
          onConfirm={() => rotateMut.mutate(confirmRotate.id)}
          onClose={() => setConfirmRotate(null)}
          isPending={rotateMut.isPending}
        />
      )}
      {revealedSecret && (
        <RevealedSecretModal
          name={revealedSecret.name}
          algorithm={revealedSecret.algorithm}
          secret={revealedSecret.secret}
          action={revealedSecret.action}
          onClose={() => setRevealedSecret(null)}
        />
      )}
    </div>
  );
}

function TSIGKeyModal({
  groupId,
  existing,
  onClose,
  onCreated,
}: {
  groupId: string;
  existing?: DNSTSIGKey;
  onClose: () => void;
  onCreated?: (s: { name: string; algorithm: string; secret: string }) => void;
}) {
  const qc = useQueryClient();
  const isEdit = !!existing;
  const [name, setName] = useState(existing?.name ?? "");
  const [algorithm, setAlgorithm] = useState(
    existing?.algorithm ?? "hmac-sha256",
  );
  const [purpose, setPurpose] = useState<string>(existing?.purpose ?? "");
  const [notes, setNotes] = useState<string>(existing?.notes ?? "");
  // Optional operator-supplied secret — empty string means "let the server
  // generate one." Only used on the create path.
  const [secret, setSecret] = useState<string>("");

  const generateMut = useMutation({
    mutationFn: () => dnsApi.generateTSIGSecret(groupId, algorithm),
    onSuccess: (r) => setSecret(r.secret),
  });

  const saveMut = useMutation({
    mutationFn: () => {
      if (isEdit && existing) {
        return dnsApi.updateTSIGKey(groupId, existing.id, {
          name,
          algorithm,
          purpose: purpose || null,
          notes,
        });
      }
      return dnsApi.createTSIGKey(groupId, {
        name,
        algorithm,
        secret: secret.trim() || null,
        purpose: purpose || null,
        notes,
      });
    },
    onSuccess: (key) => {
      qc.invalidateQueries({ queryKey: ["dns-tsig-keys", groupId] });
      if (!isEdit && key.secret && onCreated) {
        onCreated({
          name: key.name,
          algorithm: key.algorithm,
          secret: key.secret,
        });
      } else {
        onClose();
      }
    },
  });

  return (
    <Modal
      title={isEdit ? `Edit ${existing!.name}` : "New TSIG Key"}
      onClose={onClose}
    >
      <div className="space-y-3 text-sm">
        <div>
          <label className="mb-0.5 block text-xs font-medium">Name</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. tsig-update.spatium.local."
            className="w-full rounded border bg-background px-2 py-1 text-xs font-mono"
          />
          <p className="mt-0.5 text-[11px] text-muted-foreground">
            RFC 1035 dotted ASCII label. Lower-case letters, digits, dots,
            dashes. The convention is to end with a trailing dot (FQDN).
          </p>
        </div>
        <div>
          <label className="mb-0.5 block text-xs font-medium">Algorithm</label>
          <select
            value={algorithm}
            onChange={(e) => setAlgorithm(e.target.value)}
            className="w-full rounded border bg-background px-2 py-1 text-xs"
          >
            {[
              "hmac-sha1",
              "hmac-sha224",
              "hmac-sha256",
              "hmac-sha384",
              "hmac-sha512",
            ].map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
          <p className="mt-0.5 text-[11px] text-muted-foreground">
            BIND9 + dnspython both default to <code>hmac-sha256</code>. Older
            clients may still need <code>hmac-sha1</code>.
          </p>
        </div>
        {!isEdit && (
          <div>
            <label className="mb-0.5 block text-xs font-medium">
              Secret (optional)
            </label>
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder="Leave blank to generate"
                className="flex-1 rounded border bg-background px-2 py-1 text-xs font-mono"
              />
              <button
                type="button"
                onClick={() => generateMut.mutate()}
                disabled={generateMut.isPending}
                className="rounded border px-2 py-1 text-xs hover:bg-muted/50 disabled:opacity-50"
              >
                Generate
              </button>
            </div>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              Base64-encoded random bytes. Leave blank to have the server
              generate one of the right size for the chosen algorithm.
            </p>
          </div>
        )}
        <div>
          <label className="mb-0.5 block text-xs font-medium">
            Purpose (optional)
          </label>
          <input
            type="text"
            value={purpose}
            onChange={(e) => setPurpose(e.target.value)}
            placeholder="e.g. nsupdate, axfr-pull"
            className="w-full rounded border bg-background px-2 py-1 text-xs"
          />
        </div>
        <div>
          <label className="mb-0.5 block text-xs font-medium">Notes</label>
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
            className="w-full rounded border bg-background px-2 py-1 text-xs"
            placeholder="What is this key for?"
          />
        </div>
        {saveMut.isError && (
          <p className="text-xs text-destructive">
            {formatApiError(saveMut.error, "Save failed")}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            className="rounded border px-3 py-1.5 text-xs hover:bg-muted/50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => saveMut.mutate()}
            disabled={saveMut.isPending || !name.trim()}
            className="rounded bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {saveMut.isPending ? "Saving…" : isEdit ? "Save" : "Create"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function RevealedSecretModal({
  name,
  algorithm,
  secret,
  action,
  onClose,
}: {
  name: string;
  algorithm: string;
  secret: string;
  action: "created" | "rotated";
  onClose: () => void;
}) {
  return (
    <Modal title="Copy this secret now" onClose={onClose}>
      <div className="space-y-3 text-sm">
        <p className="text-xs text-muted-foreground">
          This is the only time the plaintext secret for{" "}
          <code className="font-mono">{name}</code> will be shown. Copy it into
          your <code>nsupdate</code> client / secondary-server config before
          closing — it is hashed at rest and can't be recovered.
        </p>
        <div className="rounded border bg-muted/30 p-3 font-mono text-xs">
          <div className="mb-1 text-muted-foreground">{algorithm}</div>
          <div className="break-all">{secret}</div>
        </div>
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard.writeText(secret);
          }}
          className="inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs hover:bg-muted/50"
        >
          <Copy className="h-3 w-3" /> Copy secret
        </button>
        <p className="text-[11px] text-muted-foreground">
          Key {action} • The new value will reach BIND9 servers on the next
          ConfigBundle long-poll (typically within seconds).
        </p>
        <div className="flex justify-end pt-1">
          <button
            type="button"
            onClick={onClose}
            className="rounded bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
          >
            I have copied it
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Options Tab ───────────────────────────────────────────────────────────────

function OptionsTab({ groupId }: { groupId: string }) {
  const qc = useQueryClient();
  const { data: opts, isLoading } = useQuery({
    queryKey: ["dns-options", groupId],
    queryFn: () => dnsApi.getOptions(groupId),
  });

  const [forwardersEnabled, setForwardersEnabled] = useState(false);
  const [forwarders, setForwarders] = useState("");
  const [forwardPolicy, setForwardPolicy] = useState("first");
  const [recursionEnabled, setRecursionEnabled] = useState(true);
  const [allowRecursion, setAllowRecursion] = useState("any");
  const [dnssecValidation, setDnssecValidation] = useState("auto");
  const [notifyEnabled, setNotifyEnabled] = useState("yes");
  const [allowQuery, setAllowQuery] = useState("any");
  const [allowTransfer, setAllowTransfer] = useState("none");
  const [queryLogEnabled, setQueryLogEnabled] = useState(false);
  const [queryLogChannel, setQueryLogChannel] = useState("file");
  const [queryLogFile, setQueryLogFile] = useState(
    "/var/log/named/queries.log",
  );
  const [queryLogSeverity, setQueryLogSeverity] = useState("info");
  const [dirty, setDirty] = useState(false);
  const [saved, setSaved] = useState(false);
  const [initialized, setInitialized] = useState(false);

  if (opts && !initialized) {
    setForwardersEnabled(opts.forwarders.length > 0);
    setForwarders(opts.forwarders.join("\n"));
    setForwardPolicy(opts.forward_policy);
    setRecursionEnabled(opts.recursion_enabled);
    setAllowRecursion(opts.allow_recursion.join(", "));
    setDnssecValidation(opts.dnssec_validation);
    setNotifyEnabled(opts.notify_enabled);
    setAllowQuery(opts.allow_query.join(", "));
    setAllowTransfer(opts.allow_transfer.join(", "));
    setQueryLogEnabled(opts.query_log_enabled);
    setQueryLogChannel(opts.query_log_channel);
    setQueryLogFile(opts.query_log_file);
    setQueryLogSeverity(opts.query_log_severity);
    setInitialized(true);
  }

  const saveMut = useMutation({
    mutationFn: (d: Record<string, unknown>) =>
      dnsApi.updateOptions(groupId, d),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-options", groupId] });
      setDirty(false);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    },
  });

  function list(s: string) {
    return s
      .split(/[,\n]+/)
      .map((x) => x.trim())
      .filter(Boolean);
  }

  function save() {
    saveMut.mutate({
      forwarders: forwardersEnabled ? list(forwarders) : [],
      forward_policy: forwardPolicy,
      recursion_enabled: recursionEnabled,
      allow_recursion: list(allowRecursion),
      dnssec_validation: dnssecValidation,
      notify_enabled: notifyEnabled,
      allow_query: list(allowQuery),
      allow_transfer: list(allowTransfer),
      query_log_enabled: queryLogEnabled,
      query_log_channel: queryLogChannel,
      query_log_file: queryLogFile,
      query_log_severity: queryLogSeverity,
    });
  }

  if (isLoading)
    return <p className="text-sm text-muted-foreground">Loading…</p>;

  // Full-width select to prevent text/arrow overlap
  const selCls =
    "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring appearance-none";

  const card = "rounded-md border p-4 space-y-3";
  const cardTitle = "text-sm font-medium flex items-center gap-2";

  return (
    <div className="space-y-5 max-w-xl">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">
          Zone/view overrides take precedence over server defaults.
        </p>
        <button
          disabled={!dirty || saveMut.isPending}
          onClick={save}
          className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm disabled:opacity-50 ${saved ? "border text-emerald-600" : "bg-primary text-primary-foreground hover:bg-primary/90"}`}
        >
          {saved ? (
            <>
              <RefreshCw className="h-3.5 w-3.5" /> Saved
            </>
          ) : saveMut.isPending ? (
            "Saving…"
          ) : (
            "Save Changes"
          )}
        </button>
      </div>

      <div className={card}>
        <div className="flex items-center justify-between">
          <h4 className={cardTitle}>
            <Layers className="h-4 w-4 text-muted-foreground" /> Forwarders
          </h4>
          <label className="flex items-center gap-2 cursor-pointer text-sm">
            <input
              type="checkbox"
              checked={forwardersEnabled}
              onChange={(e) => {
                setForwardersEnabled(e.target.checked);
                setDirty(true);
              }}
              className="h-4 w-4"
            />
            Enable
          </label>
        </div>
        {!forwardersEnabled && (
          <p className="text-xs text-muted-foreground">
            Forwarders disabled — suitable for authoritative-only or air-gapped
            servers.
          </p>
        )}
        {forwardersEnabled && (
          <>
            <Field label="Upstream resolvers (one per line)">
              <textarea
                value={forwarders}
                onChange={(e) => {
                  setForwarders(e.target.value);
                  setDirty(true);
                }}
                className="w-full rounded border bg-background px-2 py-1 font-mono text-xs resize-none h-16 focus:outline-none focus:ring-1 focus:ring-ring"
                placeholder={"1.1.1.1\n8.8.8.8"}
              />
            </Field>
            <Field label="Forward policy">
              <select
                className={selCls}
                value={forwardPolicy}
                onChange={(e) => {
                  setForwardPolicy(e.target.value);
                  setDirty(true);
                }}
              >
                <option value="first">
                  first — try forwarders first, fall back to recursion
                </option>
                <option value="only">
                  only — always send to forwarders, never recurse
                </option>
              </select>
            </Field>
          </>
        )}
      </div>

      <div className={card}>
        <h4 className={cardTitle}>Recursion</h4>
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={recursionEnabled}
            onChange={(e) => {
              setRecursionEnabled(e.target.checked);
              setDirty(true);
            }}
            className="h-4 w-4"
          />
          <span className="text-sm">Enable recursion</span>
        </label>
        <Field label="allow-recursion (comma-separated CIDRs / ACL names)">
          <input
            className={inputCls}
            value={allowRecursion}
            onChange={(e) => {
              setAllowRecursion(e.target.value);
              setDirty(true);
            }}
            placeholder="any"
          />
        </Field>
      </div>

      <div className={card}>
        <h4 className={cardTitle}>
          <Shield className="h-4 w-4 text-muted-foreground" /> DNSSEC Validation
        </h4>
        <Field label="Validation mode">
          <select
            className={selCls}
            value={dnssecValidation}
            onChange={(e) => {
              setDnssecValidation(e.target.value);
              setDirty(true);
            }}
          >
            <option value="auto">
              auto — validate using built-in managed keys (recommended)
            </option>
            <option value="yes">
              yes — validate; trust anchors must be configured manually
            </option>
            <option value="no">no — do not validate DNSSEC signatures</option>
          </select>
        </Field>
      </div>

      <div className={card}>
        <h4 className={cardTitle}>Notify</h4>
        <Field label="Notify mode">
          <select
            className={selCls}
            value={notifyEnabled}
            onChange={(e) => {
              setNotifyEnabled(e.target.value);
              setDirty(true);
            }}
          >
            <option value="yes">
              yes — notify all servers listed in NS records
            </option>
            <option value="explicit">
              explicit — only notify servers in also-notify list
            </option>
            <option value="master-only">
              master-only — only send notifies from primary
            </option>
            <option value="no">no — disable zone change notifications</option>
          </select>
        </Field>
      </div>

      <div className={card}>
        <h4 className={cardTitle}>Query &amp; Transfer ACLs</h4>
        <Field label="allow-query (comma-separated CIDRs / ACL names)">
          <input
            className={inputCls}
            value={allowQuery}
            onChange={(e) => {
              setAllowQuery(e.target.value);
              setDirty(true);
            }}
            placeholder="any"
          />
        </Field>
        <Field label="allow-transfer (comma-separated CIDRs / ACL names)">
          <input
            className={inputCls}
            value={allowTransfer}
            onChange={(e) => {
              setAllowTransfer(e.target.value);
              setDirty(true);
            }}
            placeholder="none"
          />
        </Field>
      </div>

      <div className={card}>
        <div className="flex items-center justify-between">
          <h4 className={cardTitle}>
            <FileText className="h-4 w-4 text-muted-foreground" /> Query Logging
          </h4>
          <label className="flex items-center gap-2 cursor-pointer text-sm">
            <input
              type="checkbox"
              checked={queryLogEnabled}
              onChange={(e) => {
                setQueryLogEnabled(e.target.checked);
                setDirty(true);
              }}
              className="h-4 w-4"
            />
            Enable
          </label>
        </div>
        {!queryLogEnabled && (
          <p className="text-xs text-muted-foreground">
            DNS query logs are disabled. Enable to record every query received
            by BIND for debugging or audit purposes (high volume — large file
            growth).
          </p>
        )}
        {queryLogEnabled && (
          <>
            <Field label="Log channel">
              <select
                className={selCls}
                value={queryLogChannel}
                onChange={(e) => {
                  setQueryLogChannel(e.target.value);
                  setDirty(true);
                }}
              >
                <option value="file">
                  file — write to a log file (rotated by BIND)
                </option>
                <option value="syslog">
                  syslog — send to local syslog (daemon facility)
                </option>
                <option value="stderr">
                  stderr — write to container stderr (visible via docker logs)
                </option>
              </select>
            </Field>
            {queryLogChannel === "file" && (
              <Field label="Log file path (inside container)">
                <input
                  className={inputCls}
                  value={queryLogFile}
                  onChange={(e) => {
                    setQueryLogFile(e.target.value);
                    setDirty(true);
                  }}
                  placeholder="/var/log/named/queries.log"
                />
              </Field>
            )}
            <Field label="Severity">
              <select
                className={selCls}
                value={queryLogSeverity}
                onChange={(e) => {
                  setQueryLogSeverity(e.target.value);
                  setDirty(true);
                }}
              >
                <option value="info">
                  info — normal queries (recommended)
                </option>
                <option value="debug">
                  debug — very verbose; for troubleshooting only
                </option>
                <option value="notice">notice — only notable events</option>
                <option value="warning">warning — warnings and above</option>
                <option value="error">error — errors only</option>
              </select>
            </Field>
            <p className="text-xs text-muted-foreground">
              Logs the <code>queries</code> and <code>query-errors</code>{" "}
              categories. View with{" "}
              <code className="font-mono">docker logs</code> (stderr) or{" "}
              <code className="font-mono">
                docker exec &lt;dns-container&gt; tail -f {queryLogFile}
              </code>{" "}
              (file).
            </p>
          </>
        )}
      </div>
    </div>
  );
}

// ── Records Tab ───────────────────────────────────────────────────────────────
// Group-wide view of every record across every zone. Mirrors the IPAM subnet
// address-table filter pattern: per-column inputs with a contains/begins/ends
// /regex mode picker for text columns, dropdowns for Type / Zone / View /
// Source, click-to-sort on every header.

type RecordFilterMode = "contains" | "begins" | "ends" | "regex";

function applyTextFilter(
  value: string | null | undefined,
  filter: string,
  mode: RecordFilterMode,
): boolean {
  if (!filter) return true;
  const v = (value ?? "").toLowerCase();
  const f = filter.toLowerCase();
  if (mode === "begins") return v.startsWith(f);
  if (mode === "ends") return v.endsWith(f);
  if (mode === "regex") {
    try {
      return new RegExp(filter, "i").test(value ?? "");
    } catch {
      return true;
    }
  }
  return v.includes(f);
}

function RecordsTab({
  group,
  onSelectZone,
}: {
  group: DNSServerGroup;
  onSelectZone: (z: DNSZone) => void;
}) {
  const qc = useQueryClient();
  const { data: records = [], isLoading } = useQuery({
    queryKey: ["dns-group-records", group.id],
    queryFn: () => dnsApi.listGroupRecords(group.id),
  });
  const { data: zones = [] } = useQuery({
    queryKey: ["dns-zones", group.id],
    queryFn: () => dnsApi.listZones(group.id),
  });
  const { data: views = [] } = useQuery({
    queryKey: ["dns-views", group.id],
    queryFn: () => dnsApi.listViews(group.id),
  });

  type ColKey = "name" | "type" | "zone" | "value" | "ttl" | "view" | "source";

  const [colFilters, setColFilters] = useState<Record<ColKey, string>>({
    name: "",
    type: "",
    zone: "",
    value: "",
    ttl: "",
    view: "",
    source: "",
  });
  const [filterModes, setFilterModes] = useState<
    Partial<Record<ColKey, RecordFilterMode>>
  >({});
  const [openFilterMenu, setOpenFilterMenu] = useState<ColKey | null>(null);
  const [editing, setEditing] = useState<DNSGroupRecord | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<DNSGroupRecord | null>(
    null,
  );

  const uniqueTypes = Array.from(
    new Set(records.map((r) => r.record_type)),
  ).sort();

  const filtered = records.filter((r) => {
    if (
      !applyTextFilter(
        r.name || "@",
        colFilters.name,
        filterModes.name ?? "contains",
      )
    )
      return false;
    if (colFilters.type && r.record_type !== colFilters.type) return false;
    if (
      !applyTextFilter(
        r.zone_name,
        colFilters.zone,
        filterModes.zone ?? "contains",
      )
    )
      return false;
    if (
      !applyTextFilter(
        r.value,
        colFilters.value,
        filterModes.value ?? "contains",
      )
    )
      return false;
    if (colFilters.ttl) {
      const ttlStr = r.ttl === null ? "" : String(r.ttl);
      if (!ttlStr.includes(colFilters.ttl)) return false;
    }
    if (colFilters.view) {
      if (colFilters.view === "__none__") {
        if (r.view_id) return false;
      } else if (r.view_id !== colFilters.view) {
        return false;
      }
    }
    if (colFilters.source) {
      const src = r.auto_generated ? "auto" : "user";
      if (src !== colFilters.source) return false;
    }
    return true;
  });

  const { sorted, sort, toggle } = useTableSort<DNSGroupRecord, ColKey>(
    filtered,
    { key: "name", dir: "asc" },
    (row, key) => {
      if (key === "name") return row.fqdn;
      if (key === "type") return row.record_type;
      if (key === "zone") return row.zone_name;
      if (key === "value") return row.value;
      if (key === "ttl") return row.ttl ?? -1;
      if (key === "view") return row.view_name ?? "";
      if (key === "source") return row.auto_generated ? "auto" : "user";
      return "";
    },
  );

  const deleteMut = useMutation({
    mutationFn: (rec: DNSGroupRecord) =>
      dnsApi.deleteRecord(group.id, rec.zone_id, rec.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-group-records", group.id] });
      qc.invalidateQueries({ queryKey: ["dns-records"] });
      setConfirmDelete(null);
    },
  });

  const hasActiveFilter = Object.values(colFilters).some(Boolean);
  function clearFilters() {
    setColFilters({
      name: "",
      type: "",
      zone: "",
      value: "",
      ttl: "",
      view: "",
      source: "",
    });
    setFilterModes({});
  }

  const TEXT_COLS: ColKey[] = ["name", "zone", "value", "ttl"];

  function renderFilterCell(col: ColKey) {
    if (col === "type") {
      return (
        <select
          value={colFilters.type}
          onChange={(e) =>
            setColFilters((p) => ({ ...p, type: e.target.value }))
          }
          className="w-full rounded border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
        >
          <option value="">All</option>
          {uniqueTypes.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      );
    }
    if (col === "view") {
      return (
        <select
          value={colFilters.view}
          onChange={(e) =>
            setColFilters((p) => ({ ...p, view: e.target.value }))
          }
          className="w-full rounded border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
        >
          <option value="">All</option>
          <option value="__none__">— none —</option>
          {views.map((v) => (
            <option key={v.id} value={v.id}>
              {v.name}
            </option>
          ))}
        </select>
      );
    }
    if (col === "source") {
      return (
        <select
          value={colFilters.source}
          onChange={(e) =>
            setColFilters((p) => ({ ...p, source: e.target.value }))
          }
          className="w-full rounded border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
        >
          <option value="">All</option>
          <option value="user">User</option>
          <option value="auto">Auto (IPAM/DHCP)</option>
        </select>
      );
    }
    if (!TEXT_COLS.includes(col)) return null;
    const mode = filterModes[col] ?? "contains";
    return (
      <div className="flex items-center">
        <input
          type="text"
          value={colFilters[col]}
          onChange={(e) =>
            setColFilters((p) => ({ ...p, [col]: e.target.value }))
          }
          placeholder="Filter…"
          className="w-full min-w-0 rounded-l border border-r-0 bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <div className="relative">
          <button
            type="button"
            onClick={() =>
              setOpenFilterMenu(openFilterMenu === col ? null : col)
            }
            className="rounded-r border bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent"
            title="Filter mode"
          >
            {mode === "begins"
              ? "^"
              : mode === "ends"
                ? "$"
                : mode === "regex"
                  ? ".*"
                  : "⊂"}
          </button>
          {openFilterMenu === col && (
            <div className="absolute left-0 top-full z-30 mt-0.5 w-32 rounded-md border bg-popover shadow-md">
              {(
                [
                  ["contains", "⊂ Contains"],
                  ["begins", "^ Begins"],
                  ["ends", "$ Ends"],
                  ["regex", ".* Regex"],
                ] as const
              ).map(([m, label]) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => {
                    setFilterModes((p) => ({ ...p, [col]: m }));
                    setOpenFilterMenu(null);
                  }}
                  className={cn(
                    "w-full px-3 py-1.5 text-left text-xs hover:bg-accent",
                    mode === m && "font-semibold text-primary",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading records…</p>;
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">
          {filtered.length.toLocaleString()} of{" "}
          {records.length.toLocaleString()}{" "}
          {records.length === 1 ? "record" : "records"}
        </p>
        {hasActiveFilter && (
          <button
            onClick={clearFilters}
            className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
          >
            <X className="h-3 w-3" />
            Clear filters
          </button>
        )}
      </div>

      <div className="overflow-hidden rounded-lg border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/40 text-xs">
              <SortableTh sortKey="name" sort={sort} onSort={toggle}>
                Name
              </SortableTh>
              <SortableTh sortKey="type" sort={sort} onSort={toggle}>
                Type
              </SortableTh>
              <SortableTh sortKey="zone" sort={sort} onSort={toggle}>
                Zone
              </SortableTh>
              <SortableTh sortKey="value" sort={sort} onSort={toggle}>
                Value
              </SortableTh>
              <SortableTh
                sortKey="ttl"
                sort={sort}
                onSort={toggle}
                align="right"
              >
                TTL
              </SortableTh>
              <SortableTh sortKey="view" sort={sort} onSort={toggle}>
                View
              </SortableTh>
              <SortableTh sortKey="source" sort={sort} onSort={toggle}>
                Source
              </SortableTh>
              <th className="px-2 py-2 text-right" />
            </tr>
            <tr className="border-b bg-muted/10 text-xs">
              {(
                [
                  "name",
                  "type",
                  "zone",
                  "value",
                  "ttl",
                  "view",
                  "source",
                ] as ColKey[]
              ).map((col) => (
                <td key={col} className="px-2 py-1">
                  {renderFilterCell(col)}
                </td>
              ))}
              <td />
            </tr>
          </thead>
          <tbody className={zebraBodyCls}>
            {sorted.length === 0 ? (
              <tr>
                <td
                  colSpan={8}
                  className="px-4 py-8 text-center text-sm text-muted-foreground"
                >
                  {records.length === 0
                    ? "No records in this group yet."
                    : "No records match the active filters."}
                </td>
              </tr>
            ) : (
              sorted.map((rec) => {
                const zone = zones.find((z) => z.id === rec.zone_id);
                return (
                  <tr
                    key={rec.id}
                    className="border-b last:border-0 hover:bg-muted/20"
                  >
                    <td className="px-4 py-2 font-mono text-xs">
                      <button
                        onClick={() => zone && onSelectZone(zone)}
                        className="hover:underline"
                        title="Open zone"
                      >
                        {rec.fqdn}
                      </button>
                    </td>
                    <td className="px-4 py-2">
                      <span className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium">
                        {rec.record_type}
                      </span>
                    </td>
                    <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                      {rec.zone_name}
                    </td>
                    <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                      {rec.value}
                    </td>
                    <td className="px-4 py-2 text-right text-xs tabular-nums text-muted-foreground">
                      {rec.ttl ?? (
                        <span className="text-muted-foreground/40">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-xs text-muted-foreground">
                      {rec.view_name ?? (
                        <span className="text-muted-foreground/40">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      {rec.auto_generated ? (
                        <span
                          className="inline-flex items-center rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-400"
                          title="Auto-managed by IPAM or DHCP"
                        >
                          auto
                        </span>
                      ) : (
                        <span className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                          user
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-2 text-right">
                      <div className="flex items-center justify-end gap-1">
                        <button
                          onClick={() => setEditing(rec)}
                          disabled={rec.auto_generated}
                          className="rounded p-1 text-muted-foreground hover:text-foreground disabled:opacity-30 disabled:pointer-events-none"
                          title={
                            rec.auto_generated
                              ? "Auto-managed — edit the source IP/lease"
                              : "Edit"
                          }
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </button>
                        <button
                          onClick={() => setConfirmDelete(rec)}
                          disabled={rec.auto_generated}
                          className="rounded p-1 text-muted-foreground hover:text-destructive disabled:opacity-30 disabled:pointer-events-none"
                          title={
                            rec.auto_generated
                              ? "Auto-managed — delete the source IP/lease"
                              : "Delete"
                          }
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {editing && (
        <RecordModal
          groupId={group.id}
          zoneId={editing.zone_id}
          zoneName={editing.zone_name}
          record={{
            id: editing.id,
            zone_id: editing.zone_id,
            view_id: editing.view_id,
            name: editing.name,
            fqdn: editing.fqdn,
            record_type: editing.record_type,
            value: editing.value,
            ttl: editing.ttl,
            priority: editing.priority,
            weight: editing.weight,
            port: editing.port,
            auto_generated: editing.auto_generated,
            tailscale_tenant_id: editing.tailscale_tenant_id ?? null,
            pool_member_id: editing.pool_member_id ?? null,
            created_at: editing.created_at,
            modified_at: editing.modified_at,
          }}
          onClose={() => {
            setEditing(null);
            qc.invalidateQueries({
              queryKey: ["dns-group-records", group.id],
            });
          }}
        />
      )}

      {confirmDelete && (
        <Modal title="Delete DNS record" onClose={() => setConfirmDelete(null)}>
          <div className="space-y-3">
            <p className="text-sm">
              Delete{" "}
              <span className="font-mono font-medium">
                {confirmDelete.fqdn}
              </span>{" "}
              <span className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium">
                {confirmDelete.record_type}
              </span>
              ? This cannot be undone.
            </p>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setConfirmDelete(null)}
                className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
              >
                Cancel
              </button>
              <button
                onClick={() => deleteMut.mutate(confirmDelete)}
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

// ── Zones Tab ─────────────────────────────────────────────────────────────────

function ZonesTab({
  group,
  onSelectZone,
}: {
  group: DNSServerGroup;
  onSelectZone: (z: DNSZone) => void;
}) {
  const qc = useQueryClient();
  const [showAdd, setShowAdd] = useState(false);
  const [showFromTemplate, setShowFromTemplate] = useState(false);
  const [showZoneFilters, setShowZoneFilters] = useState(false);
  const [zoneNameFilter, setZoneNameFilter] = useState("");
  const [zoneTypeFilter, setZoneTypeFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);

  const { data: zones = [], isFetching } = useQuery({
    queryKey: ["dns-zones", group.id],
    queryFn: () => dnsApi.listZones(group.id),
  });

  const { data: views = [] } = useQuery({
    queryKey: ["dns-views", group.id],
    queryFn: () => dnsApi.listViews(group.id),
  });

  const hasZoneFilter = !!(zoneNameFilter || zoneTypeFilter);
  const filteredZones = hasZoneFilter
    ? zones.filter((z) => {
        if (
          zoneNameFilter &&
          !z.name.toLowerCase().includes(zoneNameFilter.toLowerCase())
        )
          return false;
        if (zoneTypeFilter && z.zone_type !== zoneTypeFilter) return false;
        return true;
      })
    : zones;
  const tree = buildDnsTree(filteredZones);

  const typeBadge: Record<string, string> = {
    primary: "bg-blue-500/15 text-blue-600",
    secondary: "bg-violet-500/15 text-violet-600",
    stub: "bg-amber-500/15 text-amber-600",
    forward: "bg-muted text-muted-foreground",
  };

  const filteredIds = filteredZones.map((z) => z.id);
  const allFilteredSelected =
    filteredIds.length > 0 && filteredIds.every((id) => selected.has(id));
  const someFilteredSelected =
    !allFilteredSelected && filteredIds.some((id) => selected.has(id));

  function toggleAll() {
    if (allFilteredSelected) {
      setSelected((prev) => {
        const next = new Set(prev);
        for (const id of filteredIds) next.delete(id);
        return next;
      });
    } else {
      setSelected((prev) => {
        const next = new Set(prev);
        for (const id of filteredIds) next.add(id);
        return next;
      });
    }
  }
  function toggleOne(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const bulkDeleteZones = useMutation({
    mutationFn: async (ids: string[]) => {
      await Promise.allSettled(
        ids.map((id) => dnsApi.deleteZone(group.id, id)),
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-zones", group.id] });
      setSelected(new Set());
      setConfirmBulkDelete(false);
    },
  });

  function renderZoneRows(node: DnsTreeNode, depth: number): React.ReactNode[] {
    const rows: React.ReactNode[] = [];
    const indent = depth * 14;
    if (node.zone) {
      const z = node.zone;
      const sel = selected.has(z.id);
      rows.push(
        <ContextMenu key={z.id}>
          <ContextMenuTrigger asChild>
            <tr
              className={cn(
                "border-b last:border-0 hover:bg-muted/30",
                sel && "bg-primary/5",
              )}
            >
              <td className="w-8 px-2 py-1">
                <input
                  type="checkbox"
                  checked={sel}
                  onChange={() => toggleOne(z.id)}
                  onClick={(e) => e.stopPropagation()}
                />
              </td>
              <td
                className="py-1 pr-2 cursor-pointer"
                onClick={() => onSelectZone(z)}
              >
                <span
                  className="inline-flex items-center gap-1.5"
                  style={{ paddingLeft: indent }}
                >
                  {swatchCls(z.color) ? (
                    <span
                      className={cn(
                        "h-2 w-2 rounded-full flex-shrink-0",
                        swatchCls(z.color)!,
                      )}
                    />
                  ) : (
                    <FileText className="h-3 w-3 text-muted-foreground flex-shrink-0" />
                  )}
                  <span className="font-mono text-xs">
                    {z.name.replace(/\.$/, "")}
                  </span>
                </span>
              </td>
              <td className="py-1">
                <span
                  className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${typeBadge[z.zone_type] ?? "bg-muted text-muted-foreground"}`}
                >
                  {z.zone_type}
                </span>
              </td>
              <td className="py-1 tabular-nums text-xs text-muted-foreground">
                {z.ttl}
              </td>
              <td className="py-1 text-xs">
                {z.dnssec_enabled ? (
                  <span className="inline-flex items-center gap-1 text-emerald-600">
                    <Shield className="h-3 w-3" /> on
                  </span>
                ) : (
                  <span className="text-muted-foreground/50">—</span>
                )}
              </td>
              <td className="py-1 text-xs text-muted-foreground">
                {z.last_pushed_at
                  ? new Date(z.last_pushed_at).toLocaleString()
                  : "—"}
              </td>
              <td className="py-1 pr-2 text-right">
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelectZone(z);
                  }}
                  className="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground hover:bg-muted hover:text-foreground"
                  title="Open zone"
                >
                  <Pencil className="h-3 w-3" />
                </button>
              </td>
            </tr>
          </ContextMenuTrigger>
          <ContextMenuContent>
            <ContextMenuLabel>{z.name.replace(/\.$/, "")}</ContextMenuLabel>
            <ContextMenuSeparator />
            <ContextMenuItem onSelect={() => onSelectZone(z)}>
              Open Zone
            </ContextMenuItem>
            <ContextMenuItem
              onSelect={() => copyToClipboard(z.name.replace(/\.$/, ""))}
            >
              Copy Name
            </ContextMenuItem>
            <ContextMenuItem
              onSelect={async () => {
                const { data, filename } = await dnsApi.exportZone(
                  group.id,
                  z.id,
                );
                const name =
                  filename ??
                  `${z.name.replace(/\.$/, "")}-${_utcTimestampSuffix()}.zone`;
                downloadBlob(data, name, "text/dns");
              }}
            >
              Export Zone File
            </ContextMenuItem>
          </ContextMenuContent>
        </ContextMenu>,
      );
    } else {
      rows.push(
        <tr
          key={`folder:${node.domain}`}
          className="bg-muted/10 border-b last:border-0"
        >
          <td />
          <td colSpan={6} className="py-0.5">
            <span
              className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground/70"
              style={{ paddingLeft: indent }}
            >
              <Folder className="h-3 w-3" />.{node.domain}
            </span>
          </td>
        </tr>,
      );
    }
    for (const child of node.children) {
      rows.push(...renderZoneRows(child, depth + 1));
    }
    return rows;
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-2 gap-2 flex-wrap">
        <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
          {hasZoneFilter
            ? `${filteredZones.length} / ${zones.length}`
            : zones.length}{" "}
          zone{zones.length !== 1 ? "s" : ""}
          {selected.size > 0 && (
            <span className="ml-2 text-primary normal-case tracking-normal">
              {selected.size} selected
            </span>
          )}
        </span>
        <div className="flex items-center gap-2 flex-wrap">
          {selected.size > 0 && (
            <>
              <button
                onClick={() => setConfirmBulkDelete(true)}
                className="flex items-center gap-1 rounded-md bg-destructive px-2 py-1 text-xs text-destructive-foreground hover:bg-destructive/90"
              >
                <Trash2 className="h-3 w-3" /> Delete {selected.size}
              </button>
              <button
                onClick={() => setSelected(new Set())}
                className="rounded-md border px-2 py-1 text-xs hover:bg-accent"
              >
                Clear
              </button>
              <span className="h-4 w-px bg-border" />
            </>
          )}
          <button
            onClick={() => setShowZoneFilters((v) => !v)}
            title="Toggle filters"
            className={`flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent ${showZoneFilters ? "bg-muted" : ""}`}
          >
            <Filter className="h-3 w-3" />
            {hasZoneFilter && (
              <span className="h-1.5 w-1.5 rounded-full bg-primary" />
            )}
          </button>
          <button
            className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted disabled:opacity-50"
            disabled={zones.length === 0}
            onClick={async () => {
              const { data, filename } = await dnsApi.exportAllZones(group.id);
              const name =
                filename ??
                `dns-zones-${group.id}-${_utcTimestampSuffix()}.zip`;
              downloadBlob(data, name, "application/zip");
            }}
          >
            <Download className="h-3.5 w-3.5" /> Export All
          </button>
          <button
            className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted/50"
            onClick={() => setShowFromTemplate(true)}
            title="Stamp a starter zone from a curated template (mail, AD, web, k8s)"
          >
            <Sparkles className="h-3 w-3" /> From Template
          </button>
          <button
            className="flex items-center gap-1 rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90"
            onClick={() => setShowAdd(true)}
          >
            <Plus className="h-3 w-3" /> Add Zone
          </button>
        </div>
      </div>

      {showZoneFilters && (
        <div className="mb-3 flex items-center gap-2 rounded-md border bg-muted/10 px-3 py-2">
          <input
            type="text"
            value={zoneNameFilter}
            onChange={(e) => setZoneNameFilter(e.target.value)}
            placeholder="Filter by name…"
            className="flex-1 rounded border border-border bg-background px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
          />
          <select
            value={zoneTypeFilter}
            onChange={(e) => setZoneTypeFilter(e.target.value)}
            className="rounded border border-border bg-background px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
          >
            <option value="">All types</option>
            {["primary", "secondary", "stub", "forward"].map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          {hasZoneFilter && (
            <button
              onClick={() => {
                setZoneNameFilter("");
                setZoneTypeFilter("");
              }}
              className="text-xs text-muted-foreground hover:text-foreground"
              title="Clear filters"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      )}

      {isFetching && zones.length === 0 && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}
      {zones.length === 0 && !isFetching && (
        <p className="text-sm text-muted-foreground italic">
          No zones yet. Click "Add Zone" to create one.
        </p>
      )}
      {hasZoneFilter && filteredZones.length === 0 && zones.length > 0 && (
        <p className="text-sm text-muted-foreground italic">
          No zones match the current filter.
        </p>
      )}

      {zones.length > 0 && (
        <div className="rounded-md border overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/30 text-left text-[10px] uppercase tracking-wider text-muted-foreground">
                <th className="w-8 px-2 py-1.5">
                  <input
                    type="checkbox"
                    checked={allFilteredSelected}
                    ref={(el) => {
                      if (el) el.indeterminate = someFilteredSelected;
                    }}
                    onChange={toggleAll}
                    aria-label="Select all filtered zones"
                  />
                </th>
                <th className="py-1.5 font-medium">Name</th>
                <th className="py-1.5 font-medium">Type</th>
                <th className="py-1.5 font-medium">TTL</th>
                <th className="py-1.5 font-medium">DNSSEC</th>
                <th className="py-1.5 font-medium">Last Push</th>
                <th className="py-1.5" />
              </tr>
            </thead>
            <tbody className={zebraBodyCls}>
              {tree.flatMap((root) => renderZoneRows(root, 0))}
            </tbody>
          </table>
        </div>
      )}

      {showAdd && (
        <ZoneModal
          groupId={group.id}
          views={views}
          onClose={() => setShowAdd(false)}
        />
      )}
      {showFromTemplate && (
        <ZoneTemplateModal
          groupId={group.id}
          onClose={() => setShowFromTemplate(false)}
          onCreated={(zone) => {
            setShowFromTemplate(false);
            // Navigate the operator straight into the freshly-created zone.
            onSelectZone(zone);
          }}
        />
      )}

      {confirmBulkDelete && (
        <ConfirmDestroyModal
          title={`Delete ${selected.size} zone${selected.size === 1 ? "" : "s"}`}
          description={
            <>
              Permanently delete the{" "}
              <span className="font-medium">{selected.size}</span> selected zone
              {selected.size === 1 ? "" : "s"} and all their records from
              SpatiumDDI? This cannot be undone.
            </>
          }
          checkLabel={`I understand ${selected.size} zone${selected.size === 1 ? "" : "s"} and all their records will be permanently deleted.`}
          isPending={bulkDeleteZones.isPending}
          onClose={() => setConfirmBulkDelete(false)}
          onConfirm={() => bulkDeleteZones.mutate(Array.from(selected))}
        />
      )}
    </div>
  );
}

// ── Blocklists Tab ────────────────────────────────────────────────────────────

function BlocklistsTab({ group }: { group: DNSServerGroup }) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<DNSBlockList | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showCatalog, setShowCatalog] = useState(false);
  const [editList, setEditList] = useState<DNSBlockList | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<DNSBlockList | null>(null);
  // Bulk-select state. Keyed by blocklist id; spans both sections so the
  // operator can apply / detach / refresh / delete a mixed selection.
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);

  // Track baselines so we can detect when an in-flight refresh has actually
  // finished writing rows. Stamping `last_synced_at` is the proxy.
  const refreshBaselineRef = useRef<Map<string, string | null>>(new Map());
  // Per-row in-flight set; the refresh task takes 10–60s, far longer than
  // the API call to enqueue it. Driven by onMutate / onError plus an
  // effect that clears entries once the row's last_synced_at advances.
  const [refreshing, setRefreshing] = useState<Set<string>>(new Set());

  const { data: lists = [], isFetching } = useQuery({
    queryKey: ["dns-blocklists"],
    queryFn: () => dnsBlocklistApi.list(),
    // While any row is mid-refresh, poll every 3s so the entry_count
    // updates without the operator having to refresh the page. The
    // task itself takes 10–60s; the refetch interval clears once
    // every pending row's last_synced_at has advanced past its baseline.
    refetchInterval: refreshing.size > 0 ? 3000 : false,
  });

  // Clear the per-row pending flag as soon as the row's last_synced_at
  // advances past the value it had when the refresh was kicked off.
  useEffect(() => {
    if (refreshing.size === 0) return;
    setRefreshing((prev) => {
      const next = new Set(prev);
      for (const id of prev) {
        const baseline = refreshBaselineRef.current.get(id) ?? null;
        const row = lists.find((l) => l.id === id);
        if (row && row.last_synced_at && row.last_synced_at !== baseline) {
          next.delete(id);
          refreshBaselineRef.current.delete(id);
        }
      }
      return next.size === prev.size ? prev : next;
    });
  }, [lists, refreshing]);

  // Filter by lists applied to this group (or not yet applied anywhere)
  const applied = lists.filter((l) => l.applied_group_ids.includes(group.id));
  const other = lists.filter((l) => !l.applied_group_ids.includes(group.id));

  // Selection helpers — both per-row and per-section.
  function toggleOne(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function toggleSection(rows: DNSBlockList[]) {
    const ids = rows.map((r) => r.id);
    const allSel = ids.length > 0 && ids.every((id) => selectedIds.has(id));
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (allSel) ids.forEach((id) => next.delete(id));
      else ids.forEach((id) => next.add(id));
      return next;
    });
  }
  const selectedRows = lists.filter((l) => selectedIds.has(l.id));
  const selCount = selectedRows.length;
  const selAppliedCount = selectedRows.filter((l) =>
    l.applied_group_ids.includes(group.id),
  ).length;
  const selDetachedCount = selCount - selAppliedCount;
  const selRefreshableCount = selectedRows.filter(
    (l) => l.source_type === "url" && l.feed_url,
  ).length;

  const deleteMut = useMutation({
    mutationFn: (id: string) => dnsBlocklistApi.delete(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-blocklists"] });
      setConfirmDelete(null);
      if (selected && confirmDelete && selected.id === confirmDelete.id)
        setSelected(null);
    },
  });

  const toggleAssignment = useMutation({
    mutationFn: async ({
      list,
      assign,
    }: {
      list: DNSBlockList;
      assign: boolean;
    }) => {
      const ids = new Set(list.applied_group_ids);
      if (assign) ids.add(group.id);
      else ids.delete(group.id);
      return dnsBlocklistApi.updateAssignments(list.id, {
        server_group_ids: Array.from(ids),
      });
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dns-blocklists"] }),
  });

  const refreshMut = useMutation({
    mutationFn: (id: string) => dnsBlocklistApi.refresh(id),
    onMutate: (id) => {
      const row = lists.find((l) => l.id === id);
      refreshBaselineRef.current.set(id, row?.last_synced_at ?? null);
      setRefreshing((prev) => new Set(prev).add(id));
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dns-blocklists"] }),
    onError: (_e, id) => {
      setRefreshing((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
      refreshBaselineRef.current.delete(id);
    },
  });

  // Bulk mutations — fan out via Promise.allSettled. Per-list scale is
  // small (a few dozen at most), so client-side fan-out beats adding a
  // bulk endpoint for now.
  const bulkApply = useMutation({
    mutationFn: async (ids: string[]) => {
      const targets = lists.filter(
        (l) => ids.includes(l.id) && !l.applied_group_ids.includes(group.id),
      );
      await Promise.allSettled(
        targets.map((l) =>
          dnsBlocklistApi.updateAssignments(l.id, {
            server_group_ids: [...l.applied_group_ids, group.id],
          }),
        ),
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-blocklists"] });
      setSelectedIds(new Set());
    },
  });
  const bulkDetach = useMutation({
    mutationFn: async (ids: string[]) => {
      const targets = lists.filter(
        (l) => ids.includes(l.id) && l.applied_group_ids.includes(group.id),
      );
      await Promise.allSettled(
        targets.map((l) =>
          dnsBlocklistApi.updateAssignments(l.id, {
            server_group_ids: l.applied_group_ids.filter((g) => g !== group.id),
          }),
        ),
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-blocklists"] });
      setSelectedIds(new Set());
    },
  });
  const bulkRefresh = useMutation({
    mutationFn: async (ids: string[]) => {
      const targets = lists.filter(
        (l) => ids.includes(l.id) && l.source_type === "url" && l.feed_url,
      );
      // Stamp baselines + flip per-row spinners up-front so the existing
      // single-row polling logic clears them once last_synced_at advances.
      for (const l of targets) {
        refreshBaselineRef.current.set(l.id, l.last_synced_at ?? null);
      }
      setRefreshing((prev) => {
        const next = new Set(prev);
        for (const l of targets) next.add(l.id);
        return next;
      });
      const results = await Promise.allSettled(
        targets.map((l) => dnsBlocklistApi.refresh(l.id)),
      );
      // Roll back spinners for any failed enqueue. Successes stay spinning
      // until last_synced_at moves.
      const failedIds = targets
        .filter((_, i) => results[i].status === "rejected")
        .map((l) => l.id);
      if (failedIds.length > 0) {
        setRefreshing((prev) => {
          const next = new Set(prev);
          for (const id of failedIds) next.delete(id);
          return next;
        });
        for (const id of failedIds) refreshBaselineRef.current.delete(id);
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-blocklists"] });
      setSelectedIds(new Set());
    },
  });
  const bulkDelete = useMutation({
    mutationFn: async (ids: string[]) => {
      await Promise.allSettled(ids.map((id) => dnsBlocklistApi.delete(id)));
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-blocklists"] });
      setSelectedIds(new Set());
      setConfirmBulkDelete(false);
    },
  });

  if (selected) {
    return (
      <BlocklistDetail
        list={selected}
        onBack={() => {
          setSelected(null);
          qc.invalidateQueries({ queryKey: ["dns-blocklists"] });
        }}
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
          {lists.length} blocking list{lists.length !== 1 ? "s" : ""}
        </span>
        <div className="flex items-center gap-2">
          <button
            className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted/50"
            onClick={() => setShowCatalog(true)}
            title="Subscribe to a curated public blocklist (StevenBlack / Hagezi / OISD / AdGuard / …)"
          >
            <Sparkles className="h-3 w-3" /> Browse Catalog
          </button>
          <button
            className="flex items-center gap-1 rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90"
            onClick={() => setShowCreate(true)}
          >
            <Plus className="h-3 w-3" /> New Blocking List
          </button>
        </div>
      </div>
      {showCatalog && (
        <BlocklistCatalogModal onClose={() => setShowCatalog(false)} />
      )}

      {/* Bulk-action toolbar — only visible when at least one row is
          selected. Each button operates on the subset of selected rows
          where it's meaningful (Apply skips already-applied; Detach
          skips not-applied; Refresh skips manual / file_upload lists). */}
      {selCount > 0 && (
        <div className="flex items-center justify-between rounded-md border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs dark:border-amber-900 dark:bg-amber-900/20">
          <span>
            {selCount} list{selCount !== 1 ? "s" : ""} selected
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setSelectedIds(new Set())}
              className="rounded border px-2 py-1 hover:bg-muted/30"
            >
              Clear
            </button>
            <button
              onClick={() => bulkApply.mutate(Array.from(selectedIds))}
              disabled={bulkApply.isPending || selDetachedCount === 0}
              className="inline-flex items-center gap-1 rounded border border-emerald-400 px-2 py-1 text-emerald-600 hover:bg-emerald-500/10 disabled:opacity-40"
              title={
                selDetachedCount === 0
                  ? "Every selected list is already applied to this group"
                  : `Apply ${selDetachedCount} list${selDetachedCount !== 1 ? "s" : ""} to ${group.name}`
              }
            >
              Apply ({selDetachedCount})
            </button>
            <button
              onClick={() => bulkDetach.mutate(Array.from(selectedIds))}
              disabled={bulkDetach.isPending || selAppliedCount === 0}
              className="inline-flex items-center gap-1 rounded border border-amber-400 px-2 py-1 text-amber-600 hover:bg-amber-500/10 disabled:opacity-40"
              title={
                selAppliedCount === 0
                  ? "None of the selected lists are applied to this group"
                  : `Detach ${selAppliedCount} list${selAppliedCount !== 1 ? "s" : ""} from ${group.name}`
              }
            >
              Detach ({selAppliedCount})
            </button>
            <button
              onClick={() => bulkRefresh.mutate(Array.from(selectedIds))}
              disabled={bulkRefresh.isPending || selRefreshableCount === 0}
              className="inline-flex items-center gap-1 rounded border px-2 py-1 hover:bg-muted/30 disabled:opacity-40"
              title={
                selRefreshableCount === 0
                  ? "Only URL-sourced lists can be refreshed"
                  : `Refresh ${selRefreshableCount} URL-sourced list${selRefreshableCount !== 1 ? "s" : ""}`
              }
            >
              <RefreshCw className="h-3 w-3" /> Refresh ({selRefreshableCount})
            </button>
            <button
              onClick={() => setConfirmBulkDelete(true)}
              disabled={bulkDelete.isPending}
              className="inline-flex items-center gap-1 rounded border border-destructive/40 px-2 py-1 text-destructive hover:bg-destructive/10 disabled:opacity-40"
            >
              <Trash2 className="h-3 w-3" /> Delete ({selCount})
            </button>
          </div>
        </div>
      )}

      {isFetching && lists.length === 0 && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}

      {[
        { label: "Applied to this group", rows: applied, assigned: true },
        { label: "Available (not applied)", rows: other, assigned: false },
      ].map((section) => {
        const sectionIds = section.rows.map((r) => r.id);
        const sectionAllSel =
          sectionIds.length > 0 &&
          sectionIds.every((id) => selectedIds.has(id));
        const sectionSomeSel =
          !sectionAllSel && sectionIds.some((id) => selectedIds.has(id));
        return (
          <div key={section.label}>
            <div className="mb-2 flex items-center gap-2">
              {section.rows.length > 0 && (
                <input
                  type="checkbox"
                  checked={sectionAllSel}
                  ref={(el) => {
                    if (el) el.indeterminate = sectionSomeSel;
                  }}
                  onChange={() => toggleSection(section.rows)}
                  title={`Select all in "${section.label}"`}
                />
              )}
              <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                {section.label}
              </h4>
            </div>
            {section.rows.length === 0 && (
              <p className="text-xs text-muted-foreground italic">None.</p>
            )}
            <div className="space-y-1">
              {section.rows.map((l) => {
                const isSel = selectedIds.has(l.id);
                return (
                  <div
                    key={l.id}
                    className={cn(
                      "flex items-center gap-2 rounded-md border bg-card px-3 py-2 group hover:bg-accent/30 cursor-pointer",
                      isSel && "ring-1 ring-primary/40 bg-primary/5",
                    )}
                    onClick={() => setSelected(l)}
                  >
                    <input
                      type="checkbox"
                      checked={isSel}
                      onChange={() => toggleOne(l.id)}
                      onClick={(e) => e.stopPropagation()}
                      className="flex-shrink-0"
                    />
                    <Ban className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
                    <span className="font-mono text-sm truncate">{l.name}</span>
                    <span className="inline-flex items-center rounded px-1.5 py-0.5 text-xs bg-muted text-muted-foreground">
                      {l.category}
                    </span>
                    <span className="inline-flex items-center rounded px-1.5 py-0.5 text-xs bg-muted text-muted-foreground">
                      {l.block_mode}
                    </span>
                    {l.source_type === "url" && l.feed_url && (
                      <span className="inline-flex items-center rounded px-1.5 py-0.5 text-xs bg-blue-500/15 text-blue-600">
                        feed
                      </span>
                    )}
                    {!l.enabled && (
                      <span className="inline-flex items-center rounded px-1.5 py-0.5 text-xs bg-amber-500/15 text-amber-600">
                        disabled
                      </span>
                    )}
                    <span className="ml-auto text-xs text-muted-foreground">
                      {l.entry_count} entries
                    </span>
                    <div
                      className="flex items-center gap-1"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <button
                        title={
                          section.assigned
                            ? "Detach from this group"
                            : "Apply to this group"
                        }
                        className={`rounded border px-2 py-0.5 text-xs ${section.assigned ? "text-amber-600 border-amber-400" : "text-emerald-600 border-emerald-400"}`}
                        onClick={() =>
                          toggleAssignment.mutate({
                            list: l,
                            assign: !section.assigned,
                          })
                        }
                      >
                        {section.assigned ? "Detach" : "Apply"}
                      </button>
                      {l.source_type === "url" && l.feed_url && (
                        <button
                          title={
                            refreshing.has(l.id)
                              ? "Refresh in progress…"
                              : "Refresh from feed"
                          }
                          disabled={refreshing.has(l.id)}
                          className="h-6 w-6 flex items-center justify-center rounded text-muted-foreground hover:text-foreground disabled:opacity-50"
                          onClick={() => refreshMut.mutate(l.id)}
                        >
                          <RefreshCw
                            className={cn(
                              "h-3 w-3",
                              refreshing.has(l.id) && "animate-spin",
                            )}
                          />
                        </button>
                      )}
                      <button
                        className="h-6 w-6 flex items-center justify-center rounded text-muted-foreground hover:text-foreground"
                        onClick={() => setEditList(l)}
                      >
                        <Pencil className="h-3 w-3" />
                      </button>
                      <button
                        className="h-6 w-6 flex items-center justify-center rounded text-muted-foreground hover:text-destructive"
                        onClick={() => setConfirmDelete(l)}
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}

      {showCreate && <BlocklistModal onClose={() => setShowCreate(false)} />}
      {editList && (
        <BlocklistModal list={editList} onClose={() => setEditList(null)} />
      )}
      {confirmDelete && (
        <ConfirmDestroyModal
          title="Delete Blocking List"
          description={`Permanently delete "${confirmDelete.name}" and all its entries/exceptions?`}
          checkLabel={`I understand all entries in "${confirmDelete.name}" will be permanently deleted.`}
          onConfirm={() => deleteMut.mutate(confirmDelete.id)}
          onClose={() => setConfirmDelete(null)}
          isPending={deleteMut.isPending}
        />
      )}
      {confirmBulkDelete && (
        <ConfirmDestroyModal
          title={`Delete ${selCount} blocklist${selCount === 1 ? "" : "s"}`}
          description={`Permanently delete the ${selCount} selected blocking list${selCount === 1 ? "" : "s"} and all their entries / exceptions? This cannot be undone.`}
          checkLabel={`I understand the ${selCount} selected blocking list${selCount === 1 ? " will be" : "s will be"} permanently deleted.`}
          onConfirm={() => bulkDelete.mutate(Array.from(selectedIds))}
          onClose={() => setConfirmBulkDelete(false)}
          isPending={bulkDelete.isPending}
        />
      )}
    </div>
  );
}

function BlocklistModal({
  list,
  onClose,
}: {
  list?: DNSBlockList;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(list?.name ?? "");
  const [description, setDescription] = useState(list?.description ?? "");
  const [category, setCategory] = useState(list?.category ?? "custom");
  const [sourceType, setSourceType] = useState(list?.source_type ?? "manual");
  const [feedUrl, setFeedUrl] = useState(list?.feed_url ?? "");
  const [feedFormat, setFeedFormat] = useState(list?.feed_format ?? "hosts");
  const [blockMode, setBlockMode] = useState(list?.block_mode ?? "nxdomain");
  const [sinkholeIp, setSinkholeIp] = useState(list?.sinkhole_ip ?? "");
  const [updateHours, setUpdateHours] = useState(
    list?.update_interval_hours ?? 24,
  );
  const [enabled, setEnabled] = useState(list?.enabled ?? true);
  const [error, setError] = useState("");

  const mut = useMutation({
    mutationFn: (d: Partial<DNSBlockList>) =>
      list ? dnsBlocklistApi.update(list.id, d) : dnsBlocklistApi.create(d),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-blocklists"] });
      onClose();
    },
    onError: (e: ApiError) => setError(e.response?.data?.detail ?? "Failed"),
  });

  return (
    <Modal
      title={list ? "Edit Blocking List" : "New Blocking List"}
      onClose={onClose}
      wide
    >
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          mut.mutate({
            name,
            description,
            category,
            source_type: sourceType,
            feed_url: sourceType === "url" ? feedUrl || null : null,
            feed_format: feedFormat,
            block_mode: blockMode,
            sinkhole_ip: blockMode === "sinkhole" ? sinkholeIp || null : null,
            update_interval_hours: updateHours,
            enabled,
          });
        }}
      >
        <Field label="Name">
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
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
          <Field label="Category">
            <input
              className={inputCls}
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              placeholder="ads | malware | tracking | ..."
            />
          </Field>
          <Field label="Block mode">
            <select
              className={inputCls}
              value={blockMode}
              onChange={(e) => setBlockMode(e.target.value)}
            >
              <option value="nxdomain">nxdomain</option>
              <option value="sinkhole">sinkhole</option>
              <option value="refused">refused</option>
            </select>
          </Field>
        </div>
        {blockMode === "sinkhole" && (
          <Field label="Sinkhole IP">
            <input
              className={inputCls}
              value={sinkholeIp}
              onChange={(e) => setSinkholeIp(e.target.value)}
              placeholder="0.0.0.0"
            />
          </Field>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Source type">
            <select
              className={inputCls}
              value={sourceType}
              onChange={(e) => setSourceType(e.target.value)}
            >
              <option value="manual">manual</option>
              <option value="url">url (feed)</option>
              <option value="file_upload">file_upload</option>
            </select>
          </Field>
          <Field label="Feed format">
            <select
              className={inputCls}
              value={feedFormat}
              onChange={(e) => setFeedFormat(e.target.value)}
            >
              <option value="hosts">hosts</option>
              <option value="domains">domains</option>
              <option value="adblock">adblock</option>
            </select>
          </Field>
        </div>
        {sourceType === "url" && (
          <>
            <Field label="Feed URL">
              <input
                className={inputCls}
                value={feedUrl}
                onChange={(e) => setFeedUrl(e.target.value)}
                placeholder="https://example.com/list.txt"
              />
            </Field>
            <Field label="Update interval (hours, 0 = manual)">
              <input
                type="number"
                min={0}
                className={inputCls}
                value={updateHours}
                onChange={(e) => setUpdateHours(Number(e.target.value))}
              />
            </Field>
          </>
        )}
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          Enabled
        </label>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <Btns onClose={onClose} pending={mut.isPending} />
      </form>
    </Modal>
  );
}

function BlocklistDetail({
  list,
  onBack,
}: {
  list: DNSBlockList;
  onBack: () => void;
}) {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [limit] = useState(50);
  const [offset, setOffset] = useState(0);
  const [newDomain, setNewDomain] = useState("");
  const [newReason, setNewReason] = useState("");
  const [bulkText, setBulkText] = useState("");
  const [showBulk, setShowBulk] = useState(false);
  const [excDomain, setExcDomain] = useState("");
  const [excReason, setExcReason] = useState("");

  const { data: page } = useQuery({
    queryKey: ["dns-blocklist-entries", list.id, q, limit, offset],
    queryFn: () =>
      dnsBlocklistApi.listEntries(list.id, {
        q: q || undefined,
        limit,
        offset,
      }),
  });
  const { data: exceptions = [] } = useQuery({
    queryKey: ["dns-blocklist-exceptions", list.id],
    queryFn: () => dnsBlocklistApi.listExceptions(list.id),
  });

  const addEntry = useMutation({
    // is_wildcard defaults to true server-side (Pi-hole semantics); toggle
    // per-entry via the Subdomains column after adding.
    mutationFn: () =>
      dnsBlocklistApi.addEntry(list.id, {
        domain: newDomain,
        reason: newReason || undefined,
      }),
    onSuccess: () => {
      setNewDomain("");
      setNewReason("");
      qc.invalidateQueries({ queryKey: ["dns-blocklist-entries", list.id] });
    },
  });
  const bulkAdd = useMutation({
    mutationFn: () =>
      dnsBlocklistApi.bulkAddEntries(
        list.id,
        bulkText
          .split(/\r?\n/)
          .map((s) => s.trim())
          .filter(Boolean),
      ),
    onSuccess: () => {
      setBulkText("");
      setShowBulk(false);
      qc.invalidateQueries({ queryKey: ["dns-blocklist-entries", list.id] });
    },
  });
  const deleteEntry = useMutation({
    mutationFn: (id: string) => dnsBlocklistApi.deleteEntry(list.id, id),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["dns-blocklist-entries", list.id] }),
  });
  const [editEntry, setEditEntry] = useState<DNSBlockListEntry | null>(null);
  const [editDomain, setEditDomain] = useState("");
  const [editEntryReason, setEditEntryReason] = useState("");
  const [editEntryWildcard, setEditEntryWildcard] = useState(true);
  // Inline toggle — fires immediately from the row checkbox, no Save button.
  const toggleEntryWildcard = useMutation({
    mutationFn: ({ id, value }: { id: string; value: boolean }) =>
      dnsBlocklistApi.updateEntry(list.id, id, { is_wildcard: value }),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["dns-blocklist-entries", list.id] }),
  });
  const updateEntry = useMutation({
    mutationFn: () =>
      dnsBlocklistApi.updateEntry(list.id, editEntry!.id, {
        domain: editDomain,
        reason: editEntryReason,
        is_wildcard: editEntryWildcard,
      }),
    onSuccess: () => {
      setEditEntry(null);
      qc.invalidateQueries({ queryKey: ["dns-blocklist-entries", list.id] });
    },
  });
  const addException = useMutation({
    mutationFn: () =>
      dnsBlocklistApi.addException(list.id, {
        domain: excDomain,
        reason: excReason,
      }),
    onSuccess: () => {
      setExcDomain("");
      setExcReason("");
      qc.invalidateQueries({ queryKey: ["dns-blocklist-exceptions", list.id] });
    },
  });
  const deleteException = useMutation({
    mutationFn: (id: string) => dnsBlocklistApi.deleteException(list.id, id),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["dns-blocklist-exceptions", list.id] }),
  });
  const [editException, setEditException] =
    useState<DNSBlockListException | null>(null);
  const [editExcDomain, setEditExcDomain] = useState("");
  const [editExcReason, setEditExcReason] = useState("");
  const updateException = useMutation({
    mutationFn: () =>
      dnsBlocklistApi.updateException(list.id, editException!.id, {
        domain: editExcDomain,
        reason: editExcReason,
      }),
    onSuccess: () => {
      setEditException(null);
      qc.invalidateQueries({ queryKey: ["dns-blocklist-exceptions", list.id] });
    },
  });
  const refresh = useMutation({
    mutationFn: () => dnsBlocklistApi.refresh(list.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dns-blocklists"] }),
  });

  const total = page?.total ?? 0;
  const items = page?.items ?? [];

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <button
          className="text-xs text-muted-foreground hover:text-foreground"
          onClick={onBack}
        >
          <ChevronRight className="h-3 w-3 rotate-180 inline mr-1" />
          Back
        </button>
        <h3 className="font-semibold text-sm">{list.name}</h3>
        <span className="text-xs text-muted-foreground">
          {total} entries · {exceptions.length} exceptions
        </span>
        {list.feed_url && (
          <button
            className="ml-auto flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
            onClick={() => refresh.mutate()}
            disabled={refresh.isPending}
          >
            <RefreshCw className="h-3 w-3" />
            {refresh.isPending ? "Queuing…" : "Refresh from feed"}
          </button>
        )}
      </div>

      {list.last_synced_at && (
        <p className="text-xs text-muted-foreground">
          Last synced: {new Date(list.last_synced_at).toLocaleString()}
          {list.last_sync_status && <> — {list.last_sync_status}</>}
          {list.last_sync_error && (
            <span className="text-destructive"> ({list.last_sync_error})</span>
          )}
        </p>
      )}

      {/* Blocked domains (the list itself) */}
      <div className="rounded-md border-2 border-destructive/30">
        <div className="flex items-center justify-between border-b border-destructive/30 bg-destructive/5 px-3 py-1.5">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase text-destructive">
            <Ban className="h-3.5 w-3.5" /> Blocked Domains
          </div>
          <span className="text-xs text-muted-foreground">
            Domains added here are blocked by the DNS server.
          </span>
        </div>
        <div className="space-y-2 border-b p-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              className={`${inputCls} w-full pl-8`}
              placeholder="Search entries…"
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setOffset(0);
              }}
            />
          </div>
          <div className="flex items-center gap-2">
            <input
              className={`${inputCls} flex-1 min-w-0`}
              placeholder="Domain to block"
              value={newDomain}
              onChange={(e) => setNewDomain(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && newDomain) {
                  e.preventDefault();
                  addEntry.mutate();
                }
              }}
            />
            <input
              className={`${inputCls} flex-1 min-w-0`}
              placeholder="Reason (optional)"
              value={newReason}
              onChange={(e) => setNewReason(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && newDomain) {
                  e.preventDefault();
                  addEntry.mutate();
                }
              }}
            />
            <button
              className="flex-shrink-0 rounded-md border px-2 py-1 text-xs hover:bg-accent"
              onClick={() => addEntry.mutate()}
              disabled={!newDomain}
            >
              Add
            </button>
            <button
              className="flex-shrink-0 rounded-md border px-2 py-1 text-xs hover:bg-accent"
              onClick={() => setShowBulk(true)}
            >
              Bulk add
            </button>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="text-left text-xs text-muted-foreground">
                <th className="px-3 py-1.5">Domain</th>
                <th className="px-3 py-1.5">Type</th>
                <th className="px-3 py-1.5">Subdomains</th>
                <th className="px-3 py-1.5">Reason</th>
                <th className="px-3 py-1.5">Source</th>
                <th className="px-3 py-1.5 w-8"></th>
              </tr>
            </thead>
            <tbody className={zebraBodyCls}>
              {items.length === 0 && (
                <tr>
                  <td
                    colSpan={6}
                    className="px-3 py-4 text-center text-xs text-muted-foreground italic"
                  >
                    No entries
                  </td>
                </tr>
              )}
              {items.map((e: DNSBlockListEntry) => (
                <tr key={e.id} className="border-t hover:bg-accent/30">
                  <td className="px-3 py-1 font-mono text-xs">{e.domain}</td>
                  <td className="px-3 py-1 text-xs">{e.entry_type}</td>
                  <td className="px-3 py-1">
                    <input
                      type="checkbox"
                      checked={e.is_wildcard}
                      disabled={
                        e.source !== "manual" ||
                        (toggleEntryWildcard.isPending &&
                          toggleEntryWildcard.variables?.id === e.id)
                      }
                      onChange={(ev) =>
                        toggleEntryWildcard.mutate({
                          id: e.id,
                          value: ev.target.checked,
                        })
                      }
                      title={
                        e.source === "manual"
                          ? "Also block *.<domain>. Saves immediately."
                          : "Feed-sourced entries can't be toggled."
                      }
                    />
                  </td>
                  <td className="px-3 py-1 text-xs text-muted-foreground">
                    {e.reason || (
                      <span className="text-muted-foreground/40">—</span>
                    )}
                  </td>
                  <td className="px-3 py-1 text-xs">{e.source}</td>
                  <td className="px-3 py-1 text-right">
                    <div className="flex items-center justify-end gap-1">
                      {e.source === "manual" && (
                        <button
                          className="text-muted-foreground hover:text-foreground"
                          onClick={() => {
                            setEditEntry(e);
                            setEditDomain(e.domain);
                            setEditEntryReason(e.reason ?? "");
                            setEditEntryWildcard(e.is_wildcard);
                          }}
                          title="Edit domain"
                        >
                          <Pencil className="h-3 w-3" />
                        </button>
                      )}
                      <button
                        className="text-muted-foreground hover:text-destructive"
                        onClick={() => deleteEntry.mutate(e.id)}
                        title={
                          e.source === "manual"
                            ? "Remove entry"
                            : "Remove (will return on next feed refresh)"
                        }
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {total > limit && (
          <div className="flex items-center justify-between border-t px-3 py-1.5 text-xs">
            <span className="text-muted-foreground">
              {offset + 1}–{Math.min(offset + limit, total)} of {total}
            </span>
            <div className="flex gap-1">
              <button
                className="rounded border px-2 py-0.5 disabled:opacity-40"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - limit))}
              >
                Prev
              </button>
              <button
                className="rounded border px-2 py-0.5 disabled:opacity-40"
                disabled={offset + limit >= total}
                onClick={() => setOffset(offset + limit)}
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Exceptions (allow-list) */}
      <div className="rounded-md border-2 border-emerald-500/30">
        <div className="flex items-center justify-between border-b border-emerald-500/30 bg-emerald-500/5 px-3 py-1.5">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase text-emerald-700 dark:text-emerald-400">
            <Shield className="h-3.5 w-3.5" /> Allow-list (Exceptions)
          </div>
          <span className="text-xs text-muted-foreground">
            Domains added here are never blocked, even if they match a blocked
            entry.
          </span>
        </div>
        <div className="flex items-center gap-2 border-b p-2">
          <input
            className={`${inputCls} flex-1 min-w-0`}
            placeholder="Domain to allow"
            value={excDomain}
            onChange={(e) => setExcDomain(e.target.value)}
          />
          <input
            className={`${inputCls} flex-1 min-w-0`}
            placeholder="Reason (optional)"
            value={excReason}
            onChange={(e) => setExcReason(e.target.value)}
          />
          <button
            className="flex-shrink-0 rounded-md border px-2 py-1 text-xs hover:bg-accent"
            onClick={() => addException.mutate()}
            disabled={!excDomain}
          >
            Add
          </button>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[480px] text-sm">
            <thead>
              <tr className="text-left text-xs text-muted-foreground">
                <th className="px-3 py-1.5">Domain</th>
                <th className="px-3 py-1.5">Reason</th>
                <th className="px-3 py-1.5 w-8"></th>
              </tr>
            </thead>
            <tbody className={zebraBodyCls}>
              {exceptions.length === 0 && (
                <tr>
                  <td
                    colSpan={3}
                    className="px-3 py-4 text-center text-xs text-muted-foreground italic"
                  >
                    No exceptions
                  </td>
                </tr>
              )}
              {exceptions.map((ex: DNSBlockListException) => (
                <tr key={ex.id} className="border-t hover:bg-accent/30">
                  <td className="px-3 py-1 font-mono text-xs">{ex.domain}</td>
                  <td className="px-3 py-1 text-xs">{ex.reason}</td>
                  <td className="px-3 py-1 text-right">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        className="text-muted-foreground hover:text-foreground"
                        onClick={() => {
                          setEditException(ex);
                          setEditExcDomain(ex.domain);
                          setEditExcReason(ex.reason ?? "");
                        }}
                        title="Edit exception"
                      >
                        <Pencil className="h-3 w-3" />
                      </button>
                      <button
                        className="text-muted-foreground hover:text-destructive"
                        onClick={() => deleteException.mutate(ex.id)}
                        title="Remove exception"
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {editException && (
        <Modal title="Edit Exception" onClose={() => setEditException(null)}>
          <div className="space-y-3">
            <Field label="Domain">
              <input
                className={inputCls}
                value={editExcDomain}
                onChange={(ev) => setEditExcDomain(ev.target.value)}
                autoFocus
              />
            </Field>
            <Field label="Reason">
              <input
                className={inputCls}
                value={editExcReason}
                onChange={(ev) => setEditExcReason(ev.target.value)}
                placeholder="Optional"
              />
            </Field>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setEditException(null)}
                className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
              >
                Cancel
              </button>
              <button
                onClick={() => updateException.mutate()}
                disabled={!editExcDomain.trim() || updateException.isPending}
                className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {updateException.isPending ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </Modal>
      )}
      {editEntry && (
        <Modal title="Edit Blocked Domain" onClose={() => setEditEntry(null)}>
          <div className="space-y-3">
            <Field label="Domain">
              <input
                className={inputCls}
                value={editDomain}
                onChange={(ev) => setEditDomain(ev.target.value)}
                autoFocus
              />
            </Field>
            <Field label="Reason">
              <input
                className={inputCls}
                value={editEntryReason}
                onChange={(ev) => setEditEntryReason(ev.target.value)}
                placeholder="Optional"
              />
            </Field>
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                checked={editEntryWildcard}
                onChange={(ev) => setEditEntryWildcard(ev.target.checked)}
              />
              Block subdomains too (
              <code className="font-mono text-xs">
                *.{editDomain || "domain"}
              </code>
              )
            </label>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setEditEntry(null)}
                className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
              >
                Cancel
              </button>
              <button
                onClick={() => updateEntry.mutate()}
                disabled={!editDomain.trim() || updateEntry.isPending}
                className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {updateEntry.isPending ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </Modal>
      )}
      {showBulk && (
        <Modal title="Bulk Add Domains" onClose={() => setShowBulk(false)} wide>
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              bulkAdd.mutate();
            }}
          >
            <p className="text-xs text-muted-foreground">
              One domain per line. Duplicates and invalid entries are skipped.
            </p>
            <textarea
              className={`${inputCls} font-mono text-xs`}
              rows={12}
              value={bulkText}
              onChange={(e) => setBulkText(e.target.value)}
            />
            <Btns
              onClose={() => setShowBulk(false)}
              pending={bulkAdd.isPending}
              label="Add Domains"
            />
          </form>
        </Modal>
      )}
    </div>
  );
}

// ── Group Detail View ─────────────────────────────────────────────────────────

type GroupTab =
  | "zones"
  | "records"
  | "servers"
  | "views"
  | "acls"
  | "blocklists"
  | "tsig"
  | "options";

function GroupDetailView({
  group,
  onSelectZone,
  onEdit,
  onDelete,
}: {
  group: DNSServerGroup;
  onSelectZone: (z: DNSZone) => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const qc = useQueryClient();
  const [syncing, setSyncing] = useState(false);
  const [groupSyncResult, setGroupSyncResult] = useState<{
    result: DNSGroupSyncResult | null;
    error: string | null;
  } | null>(null);

  async function runGroupSync() {
    setSyncing(true);
    setGroupSyncResult({ result: null, error: null });
    try {
      const result = await dnsApi.syncGroupWithServers(group.id);
      setGroupSyncResult({ result, error: null });
      qc.invalidateQueries({ queryKey: ["dns-zones", group.id] });
      qc.invalidateQueries({ queryKey: ["dns-group-records", group.id] });
      qc.invalidateQueries({ queryKey: ["dns-servers", group.id] });
    } catch (e) {
      setGroupSyncResult({
        result: null,
        error: formatApiError(e as ApiError, "Sync with servers failed"),
      });
    } finally {
      setSyncing(false);
    }
  }

  const [searchParams, setSearchParams] = useSearchParams();
  const tab = (searchParams.get("tab") as GroupTab) || "zones";
  const setTab = (t: GroupTab) =>
    setSearchParams(
      (prev: URLSearchParams) => {
        const next = new URLSearchParams(prev);
        next.set("tab", t);
        return next;
      },
      { replace: true },
    );

  const tabs: { id: GroupTab; label: string; icon: React.ElementType }[] = [
    { id: "zones", label: "Zones", icon: FileText },
    { id: "records", label: "Records", icon: ListTree },
    { id: "servers", label: "Servers", icon: Cpu },
    { id: "views", label: "Views", icon: Eye },
    { id: "acls", label: "ACLs", icon: Shield },
    { id: "blocklists", label: "Blocking Lists", icon: Ban },
    { id: "tsig", label: "TSIG Keys", icon: KeyRound },
    { id: "options", label: "Options", icon: Settings2 },
  ];

  const typeBadge: Record<string, string> = {
    internal: "bg-blue-500/15 text-blue-600",
    external: "bg-violet-500/15 text-violet-600",
    dmz: "bg-amber-500/15 text-amber-600",
    custom: "bg-muted text-muted-foreground",
  };

  return (
    <div className="flex flex-col h-full">
      <div className="border-b px-5 py-3">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 min-w-0">
            <Globe className="h-4 w-4 text-muted-foreground flex-shrink-0" />
            <h2 className="font-semibold text-base truncate">{group.name}</h2>
            <span
              className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium flex-shrink-0 ${typeBadge[group.group_type] ?? "bg-muted text-muted-foreground"}`}
            >
              {group.group_type}
            </span>
            {group.is_recursive && (
              <span className="inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium bg-emerald-500/15 text-emerald-600 flex-shrink-0">
                recursive
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            <button
              className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
              onClick={() => {
                qc.invalidateQueries({ queryKey: ["dns-zones", group.id] });
                qc.invalidateQueries({
                  queryKey: ["dns-group-records", group.id],
                });
                qc.invalidateQueries({
                  queryKey: ["dns-servers", group.id],
                });
                qc.invalidateQueries({ queryKey: ["dns-views", group.id] });
                qc.invalidateQueries({ queryKey: ["dns-acls", group.id] });
                qc.invalidateQueries({ queryKey: ["dns-blocklists"] });
                qc.invalidateQueries({ queryKey: ["dns-options", group.id] });
              }}
              title="Reload all data for this group (zones, records, servers, views, ACLs, blocklists, options) from the control plane."
            >
              <RefreshCw className="h-3 w-3" />
              Refresh
            </button>
            <button
              className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
              onClick={runGroupSync}
              disabled={syncing}
              title="Bi-directional additive sync against every enabled server in this group. Pulls missing zones and records from the servers (imports into SpatiumDDI), and pushes any SpatiumDDI zones / records not yet on the servers."
            >
              <RefreshCw className={cn("h-3 w-3", syncing && "animate-spin")} />
              {syncing ? "Syncing…" : "Sync with Servers"}
            </button>
            <button
              className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
              onClick={onEdit}
            >
              <Pencil className="h-3 w-3" /> Edit Group
            </button>
            <button
              className="flex items-center gap-1 rounded-md border border-destructive/40 px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
              onClick={onDelete}
            >
              <Trash2 className="h-3 w-3" /> Delete Group
            </button>
          </div>
        </div>
        {group.description && (
          <p className="text-xs text-muted-foreground mt-0.5">
            {group.description}
          </p>
        )}
      </div>

      <div className="flex border-b">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`flex items-center gap-1.5 px-4 py-2.5 text-xs font-medium border-b-2 transition-colors ${tab === t.id ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"}`}
          >
            <t.icon className="h-3.5 w-3.5" />
            {t.label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-auto p-5">
        {tab === "zones" && (
          <ZonesTab group={group} onSelectZone={onSelectZone} />
        )}
        {tab === "records" && (
          <RecordsTab group={group} onSelectZone={onSelectZone} />
        )}
        {tab === "servers" && <ServersTab group={group} />}
        {tab === "views" && <ViewsTab group={group} />}
        {tab === "acls" && <AclsTab groupId={group.id} />}
        {tab === "blocklists" && <BlocklistsTab group={group} />}
        {tab === "tsig" && <TSIGKeysTab group={group} />}
        {tab === "options" && <OptionsTab groupId={group.id} />}
      </div>

      {groupSyncResult && (
        <SyncWithServersResultModal
          group={group}
          result={groupSyncResult.result}
          error={groupSyncResult.error}
          onClose={() => setGroupSyncResult(null)}
        />
      )}
    </div>
  );
}

function SyncWithServersResultModal({
  group,
  result,
  error,
  onClose,
}: {
  group: DNSServerGroup;
  result: DNSGroupSyncResult | null;
  error: string | null;
  onClose: () => void;
}) {
  return (
    <Modal title={`Sync result — ${group.name}`} onClose={onClose} wide>
      <div className="space-y-3 text-sm">
        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
            {error}
          </div>
        )}
        {result && (
          <>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 text-xs">
              <SyncStat
                label="Servers"
                value={`${result.servers_succeeded}/${result.servers_attempted}`}
              />
              <SyncStat
                label="Zones imported"
                value={result.total_zones_imported}
                accent={result.total_zones_imported ? "good" : undefined}
              />
              <SyncStat
                label="Zones pushed"
                value={result.total_zones_pushed_to_server}
                accent={
                  result.total_zones_pushed_to_server ? "good" : undefined
                }
              />
              <SyncStat
                label="Records imported"
                value={result.total_imported}
                accent={result.total_imported ? "good" : undefined}
              />
            </div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-2 text-xs">
              <SyncStat
                label="Records pushed"
                value={result.total_pushed}
                accent={result.total_pushed ? "good" : undefined}
              />
              <SyncStat
                label="Push errors"
                value={result.total_push_errors}
                accent={result.total_push_errors ? "bad" : undefined}
              />
            </div>

            {result.items.length === 0 && (
              <p className="text-sm text-muted-foreground italic">
                No enabled servers in this group.
              </p>
            )}

            {result.items.map((srv) => (
              <details
                key={srv.server_id}
                className="rounded-md border"
                open={
                  !!srv.error ||
                  (srv.result?.zones_pushed_to_server?.length ?? 0) > 0 ||
                  (srv.result?.zones_imported?.length ?? 0) > 0 ||
                  (srv.result?.zones_push_to_server_errors?.length ?? 0) > 0
                }
              >
                <summary className="cursor-pointer select-none border-b bg-muted/20 px-3 py-1.5 text-xs font-medium flex items-center justify-between">
                  <span className="flex items-center gap-2">
                    <Cpu className="h-3 w-3" />
                    {srv.server_name}
                    <span className="rounded bg-muted px-1 py-0 text-[10px] text-muted-foreground">
                      {srv.driver}
                    </span>
                  </span>
                  <span
                    className={
                      srv.error ? "text-destructive" : "text-muted-foreground"
                    }
                  >
                    {srv.error
                      ? "failed"
                      : srv.result
                        ? `${srv.result.zones_attempted} zone${srv.result.zones_attempted === 1 ? "" : "s"}`
                        : "—"}
                  </span>
                </summary>
                <div className="px-3 py-2 space-y-2">
                  {srv.error && (
                    <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
                      {srv.error}
                    </div>
                  )}
                  {srv.result && (
                    <>
                      {srv.result.zones_pushed_to_server.length > 0 && (
                        <div className="rounded-md border border-sky-500/40 bg-sky-500/5 px-3 py-2 text-xs">
                          <div className="font-medium text-sky-700 dark:text-sky-300">
                            Pushed {srv.result.zones_pushed_to_server.length}{" "}
                            zone
                            {srv.result.zones_pushed_to_server.length === 1
                              ? ""
                              : "s"}{" "}
                            to the server
                          </div>
                          <ul className="mt-1 ml-4 list-disc text-muted-foreground">
                            {srv.result.zones_pushed_to_server.map((z) => (
                              <li key={z} className="font-mono">
                                {z}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {srv.result.zones_imported.length > 0 && (
                        <div className="rounded-md border border-emerald-500/40 bg-emerald-500/5 px-3 py-2 text-xs">
                          <div className="font-medium text-emerald-700 dark:text-emerald-400">
                            Imported {srv.result.zones_imported.length} new zone
                            {srv.result.zones_imported.length === 1 ? "" : "s"}
                          </div>
                          <ul className="mt-1 ml-4 list-disc text-muted-foreground">
                            {srv.result.zones_imported.map((z) => (
                              <li key={z} className="font-mono">
                                {z}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {srv.result.zones_push_to_server_errors.length > 0 && (
                        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs">
                          <div className="font-medium text-destructive">
                            Zone push errors
                          </div>
                          <ul className="mt-1 ml-4 list-disc text-muted-foreground">
                            {srv.result.zones_push_to_server_errors.map(
                              (e, i) => (
                                <li key={i} className="font-mono text-[11px]">
                                  {e}
                                </li>
                              ),
                            )}
                          </ul>
                        </div>
                      )}
                      {srv.result.zones_skipped_system.length > 0 && (
                        <div className="text-[11px] text-muted-foreground">
                          Skipped Windows system zones:{" "}
                          <span className="font-mono">
                            {srv.result.zones_skipped_system.join(", ")}
                          </span>
                        </div>
                      )}
                      {srv.result.items.length > 0 && (
                        <div className="rounded border">
                          <table className="w-full text-xs">
                            <thead>
                              <tr className="border-b bg-muted/10 text-left">
                                <th className="px-3 py-1 font-medium">Zone</th>
                                <th className="px-3 py-1 font-medium tabular-nums">
                                  On server
                                </th>
                                <th className="px-3 py-1 font-medium tabular-nums">
                                  Imported
                                </th>
                                <th className="px-3 py-1 font-medium tabular-nums">
                                  Pushed
                                </th>
                                <th className="px-3 py-1 font-medium">
                                  Status
                                </th>
                              </tr>
                            </thead>
                            <tbody className={zebraBodyCls}>
                              {srv.result.items.map((item) => (
                                <tr
                                  key={item.zone}
                                  className="border-b last:border-0"
                                >
                                  <td className="px-3 py-1 font-mono">
                                    {item.zone}
                                  </td>
                                  <td className="px-3 py-1 tabular-nums">
                                    {item.server_records}
                                  </td>
                                  <td className="px-3 py-1 tabular-nums text-emerald-600">
                                    {item.imported || "—"}
                                  </td>
                                  <td className="px-3 py-1 tabular-nums text-emerald-600">
                                    {item.pushed || "—"}
                                  </td>
                                  <td className="px-3 py-1">
                                    {item.error ? (
                                      <span className="text-destructive">
                                        {item.error}
                                      </span>
                                    ) : item.push_errors.length > 0 ? (
                                      <span className="text-amber-600">
                                        {item.push_errors.length} push err
                                      </span>
                                    ) : (
                                      <span className="text-muted-foreground">
                                        ok
                                      </span>
                                    )}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </>
                  )}
                </div>
              </details>
            ))}
          </>
        )}
        <div className="flex justify-end">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Close
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Sidebar zone tree rows ────────────────────────────────────────────────────

function ZoneTreeRows({
  groupId,
  selectedZoneId,
  onSelectZone,
}: {
  groupId: string;
  selectedZoneId: string | null;
  onSelectZone: (z: DNSZone) => void;
}) {
  const [expandedNodes, setExpandedNodes] = useSessionState<Set<string>>(
    `spatium.dns.expandedZones.${groupId}`,
    new Set(),
  );
  const [createZoneName, setCreateZoneName] = useState<string | null>(null);

  const { data: zones = [] } = useQuery({
    queryKey: ["dns-zones", groupId],
    queryFn: () => dnsApi.listZones(groupId),
  });
  const { data: views = [] } = useQuery({
    queryKey: ["dns-views", groupId],
    queryFn: () => dnsApi.listViews(groupId),
    staleTime: 30_000,
  });

  const tree = buildDnsTree(zones);

  if (tree.length === 0)
    return (
      <p className="px-3 py-1.5 text-xs text-muted-foreground italic">
        No zones
      </p>
    );

  function toggleNode(domain: string) {
    setExpandedNodes((prev: Set<string>) => {
      const next = new Set(prev);
      if (next.has(domain)) next.delete(domain);
      else next.add(domain);
      return next;
    });
  }

  function renderNode(node: DnsTreeNode, depth: number): React.ReactNode {
    const paddingLeft = 12 + depth * 14;
    const hasChildren = node.children.length > 0;
    const expanded = expandedNodes.has(node.domain);

    return (
      <div key={node.domain}>
        {hasChildren ? (
          /* Node with children — split expand toggle from zone select */
          <div className="flex items-center" style={{ paddingLeft }}>
            <button
              className="flex items-center justify-center w-5 h-6 flex-shrink-0 text-muted-foreground hover:text-foreground"
              onClick={() => toggleNode(node.domain)}
              title={expanded ? "Collapse" : "Expand"}
            >
              {expanded ? (
                <FolderOpen className="h-3 w-3" />
              ) : (
                <Folder className="h-3 w-3" />
              )}
            </button>
            {node.zone ? (
              /* This node is also a registered zone — make label clickable */
              <button
                className={`flex flex-1 items-center gap-1.5 rounded py-1 pr-2 text-xs ${
                  selectedZoneId === node.zone.id
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-foreground"
                }`}
                onClick={() => onSelectZone(node.zone!)}
              >
                {swatchCls(node.zone.color) ? (
                  <span
                    className={cn(
                      "h-2 w-2 rounded-full flex-shrink-0",
                      swatchCls(node.zone.color)!,
                    )}
                  />
                ) : (
                  <FileText className="h-3 w-3 flex-shrink-0" />
                )}
                <span className="font-mono truncate">
                  {node.zone.name.replace(/\.$/, "")}
                </span>
                {node.zone.dnssec_enabled && (
                  <Shield className="h-2.5 w-2.5 ml-auto flex-shrink-0 text-emerald-500" />
                )}
              </button>
            ) : (
              /* Intermediate folder with no zone. TLD-level nodes (no dot,
                 like "org" or "com") just toggle; you never create a zone
                 literally at the TLD. Deeper folders (e.g. "example.com")
                 open the Create Zone modal on click. */
              <button
                className="flex flex-1 items-center gap-1 rounded py-1 pr-2 text-xs font-medium font-mono text-muted-foreground hover:bg-accent hover:text-foreground"
                onClick={() =>
                  node.domain.includes(".")
                    ? setCreateZoneName(node.domain)
                    : toggleNode(node.domain)
                }
                title={
                  node.domain.includes(".")
                    ? `Create zone "${node.domain}" here`
                    : "Expand / collapse"
                }
              >
                {node.domain}
              </button>
            )}
          </div>
        ) : node.zone ? (
          /* Leaf zone node */
          <button
            className={`flex w-full items-center gap-1.5 rounded py-1 pr-2 text-xs ${
              selectedZoneId === node.zone.id
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-accent hover:text-foreground"
            }`}
            style={{ paddingLeft }}
            onClick={() => onSelectZone(node.zone!)}
          >
            {swatchCls(node.zone.color) ? (
              <span
                className={cn(
                  "h-2 w-2 rounded-full flex-shrink-0",
                  swatchCls(node.zone.color)!,
                )}
              />
            ) : (
              <FileText className="h-3 w-3 flex-shrink-0" />
            )}
            <span className="font-mono truncate">
              {node.zone.name.replace(/\.$/, "")}
            </span>
            {node.zone.dnssec_enabled && (
              <Shield className="h-2.5 w-2.5 ml-auto flex-shrink-0 text-emerald-500" />
            )}
          </button>
        ) : (
          /* Intermediate domain with no zone */
          <div
            className="flex w-full items-center gap-1.5 rounded px-3 py-1 text-xs text-muted-foreground"
            style={{ paddingLeft }}
          >
            <Folder className="h-3 w-3 flex-shrink-0" />
            <span className="font-medium font-mono">{node.domain}</span>
          </div>
        )}
        {/* Children */}
        {hasChildren && expanded && (
          <div>
            {node.children.map((child) => renderNode(child, depth + 1))}
          </div>
        )}
      </div>
    );
  }

  return (
    <>
      <div>{tree.map((root) => renderNode(root, 0))}</div>
      {createZoneName && (
        <ZoneModal
          groupId={groupId}
          views={views}
          initialName={createZoneName}
          onClose={() => setCreateZoneName(null)}
        />
      )}
    </>
  );
}

// ── Main DNS Page ─────────────────────────────────────────────────────────────

type Selection =
  | { type: "group"; group: DNSServerGroup }
  | { type: "zone"; group: DNSServerGroup; zone: DNSZone };

export function DNSPage() {
  useStickyLocation("spatium.lastUrl.dns");
  const qc = useQueryClient();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [selection, setSelectionState] = useState<Selection | null>(null);
  const [showCreateGroup, setShowCreateGroup] = useState(false);
  const [editGroup, setEditGroup] = useState<DNSServerGroup | null>(null);
  const [confirmDeleteGroup, setConfirmDeleteGroup] =
    useState<DNSServerGroup | null>(null);
  const [expandedGroups, setExpandedGroups] = useSessionState<Set<string>>(
    "spatium.dns.expandedGroups",
    new Set(),
  );
  const urlRestored = useRef(false);
  // Captured from ``location.state.highlightRecord`` before
  // ``setSelection`` fires its ``setSearchParams(..., { replace: true })``
  // — that replace drops ``location.state`` so we can't lazy-read it
  // inside ``ZoneDetailView``.
  const [pendingHighlightRecord, setPendingHighlightRecord] = useState<
    string | null
  >(null);
  // Zone the deep-link targeted — used below to clear the highlight as
  // soon as the operator switches to a different zone (one-shot).
  const highlightTargetZoneRef = useRef<string | null>(null);

  // Update selection state + URL search params together. Preserves `tab`
  // when staying within the same group; clears it when switching groups.
  function setSelection(sel: Selection | null) {
    setSelectionState(sel);
    setSearchParams(
      (prev: URLSearchParams) => {
        const next = new URLSearchParams(prev);
        const prevGroupId = next.get("group");
        if (!sel) {
          next.delete("group");
          next.delete("zone");
          next.delete("tab");
        } else if (sel.type === "group") {
          next.set("group", sel.group.id);
          next.delete("zone");
          if (prevGroupId !== sel.group.id) next.delete("tab");
        } else {
          next.set("group", sel.group.id);
          next.set("zone", sel.zone.id);
          if (prevGroupId !== sel.group.id) next.delete("tab");
        }
        return next;
      },
      { replace: true },
    );
  }

  const { data: groups = [], isLoading } = useQuery({
    queryKey: ["dns-groups"],
    queryFn: () => dnsApi.listGroups(),
  });

  // Deep-link from global search: navigate("/dns", { state: { selectGroup, selectZone, highlightRecord? } })
  useEffect(() => {
    const state = location.state as {
      selectGroup?: string;
      selectZone?: string;
      highlightRecord?: string;
    } | null;
    if (!state?.selectGroup || groups.length === 0) return;
    const group = groups.find(
      (g: DNSServerGroup) => g.id === state.selectGroup,
    );
    if (!group) return;
    setExpandedGroups((prev) => new Set([...prev, group.id]));
    // Capture the highlight BEFORE setSelection fires — the setter
    // calls setSearchParams(..., { replace: true }) which drops
    // location.state, so ZoneDetailView can't read it later.
    if (state.highlightRecord && state.selectZone) {
      setPendingHighlightRecord(state.highlightRecord);
      highlightTargetZoneRef.current = state.selectZone;
    }
    if (state.selectZone) {
      // Zone selection: load zones for the group, then select
      dnsApi.listZones(group.id).then((zones: DNSZone[]) => {
        const zone = zones.find((z: DNSZone) => z.id === state.selectZone);
        if (zone) setSelection({ type: "zone", group, zone });
        else setSelection({ type: "group", group });
      });
    } else {
      setSelection({ type: "group", group });
    }
    // Clear state so re-renders don't re-trigger
    window.history.replaceState({}, "");
    urlRestored.current = true;
  }, [location.state, groups]);

  // One-shot: clear the pending record highlight as soon as the
  // operator switches to a different zone (or back to group view).
  useEffect(() => {
    if (!pendingHighlightRecord) return;
    const currentZoneId = selection?.type === "zone" ? selection.zone.id : null;
    if (currentZoneId !== highlightTargetZoneRef.current) {
      setPendingHighlightRecord(null);
    }
  }, [selection, pendingHighlightRecord]);

  // URL-state restore: reopen last-visited group/zone on back-navigation.
  // Depends on searchParams so that when `useStickyLocation` navigates from
  // bare `/dns` → `/dns?group=…` after mount, this effect re-runs and picks
  // up the now-populated params. The `urlRestored` guard is only set once
  // we've actually matched a param, so an early run with empty searchParams
  // doesn't latch us into "nothing to restore".
  useEffect(() => {
    if (urlRestored.current) return;
    if (groups.length === 0) return;
    const groupId = searchParams.get("group");
    const zoneId = searchParams.get("zone");
    if (!groupId) return;
    urlRestored.current = true;
    const group = groups.find((g: DNSServerGroup) => g.id === groupId);
    if (!group) return;
    setExpandedGroups((prev) => new Set([...prev, group.id]));
    if (zoneId) {
      dnsApi.listZones(group.id).then((zones: DNSZone[]) => {
        const zone = zones.find((z: DNSZone) => z.id === zoneId);
        setSelectionState(
          zone ? { type: "zone", group, zone } : { type: "group", group },
        );
      });
    } else {
      setSelectionState({ type: "group", group });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groups, searchParams]);

  const deleteGroup = useMutation({
    mutationFn: (id: string) => dnsApi.deleteGroup(id),
    onSuccess: (_, id) => {
      qc.invalidateQueries({ queryKey: ["dns-groups"] });
      if (selection && "group" in selection && selection.group.id === id)
        setSelection(null);
      setConfirmDeleteGroup(null);
    },
  });
  const deleteGroupError =
    deleteGroup.error &&
    (((deleteGroup.error as { response?: { data?: { detail?: string } } })
      ?.response?.data?.detail as string | undefined) ??
      (deleteGroup.error as Error).message);

  function toggleGroup(id: string) {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const groupTypeDot: Record<string, string> = {
    internal: "bg-blue-500",
    external: "bg-violet-500",
    dmz: "bg-amber-500",
    custom: "bg-muted-foreground",
  };

  return (
    <div className="flex h-full overflow-hidden">
      {/* ── Sidebar ── */}
      <div className="w-72 flex-shrink-0 flex flex-col border-r bg-card">
        <div className="flex items-center justify-between px-4 py-3 border-b">
          <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            DNS Server Groups
          </span>
          <div className="flex gap-1">
            <button
              className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-accent hover:text-foreground"
              onClick={() => {
                // Force refetch — bare invalidate only marks queries
                // stale, which isn't enough when the user pressed
                // Refresh after external changes (API, another tab).
                qc.refetchQueries({ queryKey: ["dns-groups"] });
                qc.refetchQueries({ queryKey: ["dns-servers"] });
                qc.refetchQueries({ queryKey: ["dns-zones"] });
              }}
              title="Refresh"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </button>
            <button
              className="flex h-6 w-6 items-center justify-center rounded hover:bg-accent"
              onClick={() => setShowCreateGroup(true)}
              title="New server group"
            >
              <Plus className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto py-1">
          {isLoading && (
            <p className="px-4 py-2 text-xs text-muted-foreground">Loading…</p>
          )}
          {groups.length === 0 && !isLoading && (
            <div className="px-4 pt-6 text-center">
              <Globe className="h-8 w-8 text-muted-foreground/30 mx-auto mb-2" />
              <p className="text-xs text-muted-foreground mb-3">
                No server groups yet.
              </p>
              <button
                className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs mx-auto hover:bg-accent"
                onClick={() => setShowCreateGroup(true)}
              >
                <Plus className="h-3 w-3" /> Create Group
              </button>
            </div>
          )}

          {groups.map((g) => {
            const expanded = expandedGroups.has(g.id);
            const groupSelected =
              selection?.type === "group" && selection.group.id === g.id;

            return (
              <div key={g.id}>
                {/* Group row */}
                <div
                  className={`flex items-center rounded-md mx-1 ${groupSelected ? "bg-primary text-primary-foreground" : ""}`}
                >
                  {/* Expand toggle */}
                  <button
                    className={`ml-1 flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-sm border text-[10px] font-bold ${
                      groupSelected
                        ? "border-primary-foreground/60 bg-primary text-primary-foreground"
                        : "border-border bg-background text-muted-foreground hover:border-primary hover:text-primary"
                    }`}
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleGroup(g.id);
                    }}
                    title={expanded ? "Collapse" : "Expand"}
                  >
                    {expanded ? "−" : "+"}
                  </button>
                  {/* Group name — click to select. Auto-expand on first
                      click but NEVER auto-collapse: clicking the name to
                      navigate back to the group view from a child zone
                      shouldn't lose the tree context. The chevron is the
                      dedicated way to collapse. */}
                  <button
                    className="flex flex-1 items-center gap-2 py-1.5 pl-2 pr-1 min-w-0"
                    onClick={() => {
                      setSelection({ type: "group", group: g });
                      if (!expanded) toggleGroup(g.id);
                    }}
                  >
                    <span
                      className={`h-2 w-2 rounded-full flex-shrink-0 ${groupTypeDot[g.group_type] ?? "bg-muted-foreground"}`}
                    />
                    <span className="text-sm font-medium truncate">
                      {g.name}
                    </span>
                  </button>
                </div>

                {/* Zone tree (when group expanded). `expandedGroups` is the
                    sole source of truth — URL/location restore effects add
                    the group to the set when navigating to a zone, so a
                    zone can't be "selected inside a collapsed group" in
                    practice. Overriding with a zone-in-group check made
                    the [+]/[−] toggle appear broken when a zone was selected:
                    the state flipped but the tree stayed visible. */}
                {expanded && (
                  <div className="ml-4 mb-1">
                    <ZoneTreeRows
                      groupId={g.id}
                      selectedZoneId={
                        selection?.type === "zone" ? selection.zone.id : null
                      }
                      onSelectZone={(z) =>
                        setSelection({ type: "zone", group: g, zone: z })
                      }
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* ── Main panel ── */}
      <div className="flex-1 overflow-hidden">
        {!selection && (
          <div className="flex h-full items-center justify-center">
            <div className="text-center">
              <Globe className="h-12 w-12 text-muted-foreground/20 mx-auto mb-3" />
              <p className="text-sm text-muted-foreground">
                {groups.length === 0
                  ? "Create a server group to start managing DNS."
                  : "Select a server group or zone from the tree."}
              </p>
            </div>
          </div>
        )}
        {selection?.type === "group" && (
          <GroupDetailView
            group={selection.group}
            onSelectZone={(z) =>
              setSelection({ type: "zone", group: selection.group, zone: z })
            }
            onEdit={() => setEditGroup(selection.group)}
            onDelete={() => setConfirmDeleteGroup(selection.group)}
          />
        )}
        {selection?.type === "zone" && (
          <ZoneDetailView
            group={selection.group}
            zone={selection.zone}
            highlightRecordId={pendingHighlightRecord}
            onDeleted={() =>
              setSelection({ type: "group", group: selection.group })
            }
          />
        )}
      </div>

      {showCreateGroup && (
        <GroupModal onClose={() => setShowCreateGroup(false)} />
      )}
      {editGroup && (
        <GroupModal group={editGroup} onClose={() => setEditGroup(null)} />
      )}
      {confirmDeleteGroup && (
        <ConfirmDestroyModal
          title="Delete Server Group"
          description={`Permanently delete group "${confirmDeleteGroup.name}"? The group must be empty — move or delete its servers and zones first.`}
          checkLabel={`I understand the group "${confirmDeleteGroup.name}" will be deleted.`}
          onConfirm={() => deleteGroup.mutate(confirmDeleteGroup.id)}
          onClose={() => {
            setConfirmDeleteGroup(null);
            deleteGroup.reset();
          }}
          isPending={deleteGroup.isPending}
          error={deleteGroupError || null}
        />
      )}
    </div>
  );
}
