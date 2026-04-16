import { useState, useEffect, useRef } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useStickyLocation } from "@/lib/stickyLocation";
import { useSessionState } from "@/lib/useSessionState";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Globe,
  Plus,
  Trash2,
  Pencil,
  ChevronDown,
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
} from "lucide-react";
import {
  dnsApi,
  dnsBlocklistApi,
  type DNSServerGroup,
  type DNSServer,
  type DNSZone,
  type DNSView,
  type DNSRecord,
  type DNSGroupRecord,
  type DNSImportPreview,
  type DNSRecordChange,
  type DNSBlockList,
  type DNSBlockListEntry,
  type DNSBlockListException,
} from "@/lib/api";
import { useTableSort, SortableTh } from "@/lib/useTableSort";
import { cn } from "@/lib/utils";

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
        className={`w-full max-w-[95vw] ${wide ? "sm:max-w-2xl" : "sm:max-w-md"} rounded-lg border bg-card p-4 sm:p-6 shadow-lg max-h-[90vh] overflow-y-auto`}
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
            onChange={(e) => setChecked(e.target.checked)}
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
      setError(err.response?.data?.detail ?? "Failed to parse zone file");
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
      setError(err.response?.data?.detail ?? "Import failed");
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
  const [error, setError] = useState("");

  const mut = useMutation({
    mutationFn: (d: Partial<DNSServerGroup>) =>
      group ? dnsApi.updateGroup(group.id, d) : dnsApi.createGroup(d),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-groups"] });
      onClose();
    },
    onError: (e: ApiError) => setError(e?.response?.data?.detail ?? "Error"),
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
  const [name, setName] = useState(server?.name ?? "");
  const [driver, setDriver] = useState(server?.driver ?? "bind9");
  const [host, setHost] = useState(server?.host ?? "");
  const [port, setPort] = useState(String(server?.port ?? 53));
  const [apiPort, setApiPort] = useState(String(server?.api_port ?? ""));
  const [roles, setRoles] = useState((server?.roles ?? []).join(", "));
  const [notes, setNotes] = useState(server?.notes ?? "");
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState("");

  const mut = useMutation({
    mutationFn: (d: Record<string, unknown>) =>
      server
        ? dnsApi.updateServer(groupId, server.id, d)
        : dnsApi.createServer(groupId, d),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-servers", groupId] });
      onClose();
    },
    onError: (e: ApiError) => setError(e?.response?.data?.detail ?? "Error"),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const roleList = roles
      .split(/[,\s]+/)
      .map((r) => r.trim())
      .filter(Boolean);
    mut.mutate({
      name,
      driver,
      host,
      port: parseInt(port, 10),
      api_port: apiPort ? parseInt(apiPort, 10) : null,
      roles: roleList,
      notes,
      ...(apiKey ? { api_key: apiKey } : {}),
    });
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
              <option value="bind9">BIND9</option>
            </select>
          </Field>
        </div>
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
    onError: (e: ApiError) => setError(e?.response?.data?.detail ?? "Error"),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    mut.mutate({
      name,
      zone_type: zoneType,
      kind,
      view_id: viewId || null,
      primary_ns: primaryNs,
      admin_email: adminEmail,
      ttl: parseInt(ttl, 10),
      dnssec_enabled: dnssec,
    });
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
    onError: (e: ApiError) => setError(e?.response?.data?.detail ?? "Error"),
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

function ZoneDetailView({
  group,
  zone,
  onDeleted,
}: {
  group: DNSServerGroup;
  zone: DNSZone;
  onDeleted: () => void;
}) {
  const qc = useQueryClient();
  const [showAddRecord, setShowAddRecord] = useState(false);
  const [editRecord, setEditRecord] = useState<DNSRecord | null>(null);
  const [showEditZone, setShowEditZone] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showRecFilters, setShowRecFilters] = useState(false);
  const [recFilter, setRecFilter] = useState({ name: "", type: "", value: "" });

  const handleExport = async () => {
    const text = await dnsApi.exportZone(group.id, zone.id);
    downloadBlob(text, `${zone.name.replace(/\.$/, "")}.zone`, "text/dns");
  };

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
    mutationFn: async (ids: string[]) => {
      // No server-side bulk endpoint yet — fan out. Ignore individual failures
      // so one bad row doesn't strand the rest (each has its own DDNS op).
      await Promise.allSettled(
        ids.map((id) => dnsApi.deleteRecord(group.id, zone.id, id)),
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-records", zone.id] });
      setSelectedRecords(new Set());
      setConfirmBulkDelete(false);
    },
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
            <FileText className="h-4 w-4 text-muted-foreground" />
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
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            TTL {zone.ttl}s · serial {zone.last_serial || "—"}
            {zone.primary_ns && ` · ${zone.primary_ns}`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
            onClick={() => setShowImport(true)}
          >
            <Upload className="h-3 w-3" /> Import
          </button>
          <button
            className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
            onClick={handleExport}
          >
            <Download className="h-3 w-3" /> Export
          </button>
          <button
            className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
            onClick={() => setShowEditZone(true)}
          >
            <Pencil className="h-3 w-3" /> Edit Zone
          </button>
          <button
            className="flex items-center gap-1 rounded-md border border-destructive/40 px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
            onClick={() => setConfirmDelete(true)}
          >
            <Trash2 className="h-3 w-3" /> Delete Zone
          </button>
          <button
            className="flex items-center gap-1 rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90"
            onClick={() => setShowAddRecord(true)}
          >
            <Plus className="h-3 w-3" /> Add Record
          </button>
        </div>
      </div>

      {/* Bulk actions — shown when any manual records are selected. */}
      {selectedRecords.size > 0 && (
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
                      .filter((r) => !r.auto_generated)
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
                        setRecFilter((f) => ({ ...f, name: e.target.value }))
                      }
                      placeholder="Filter…"
                      className="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                    />
                  </td>
                  <td className="px-2 py-1">
                    <select
                      value={recFilter.type}
                      onChange={(e) =>
                        setRecFilter((f) => ({ ...f, type: e.target.value }))
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
                        setRecFilter((f) => ({ ...f, value: e.target.value }))
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
            <tbody>
              {filtered.map((r) => (
                <tr
                  key={r.id}
                  className="border-b last:border-0 hover:bg-muted/40 group"
                >
                  <td className="w-8 py-1.5 pl-3">
                    {!r.auto_generated && (
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
                    {r.auto_generated ? (
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
                    {r.auto_generated ? (
                      <div className="flex items-center justify-end gap-1">
                        <span
                          title="This record was created automatically by IPAM. Edit the IP address in IPAM to change it."
                          className="flex items-center gap-1 rounded border border-amber-300/60 bg-amber-50 px-1.5 py-0.5 text-xs text-amber-700 dark:border-amber-700/40 dark:bg-amber-900/20 dark:text-amber-400"
                        >
                          <Lock className="h-2.5 w-2.5" />
                          IPAM
                        </span>
                        <span
                          title="Managed by IPAM — changes made here will be overwritten. To edit, update the IP address record in IPAM."
                          className="flex h-5 w-5 cursor-help items-center justify-center rounded text-muted-foreground/60 hover:text-muted-foreground"
                        >
                          <Info className="h-3 w-3" />
                        </span>
                      </div>
                    ) : (
                      <div className="flex items-center justify-end gap-1">
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
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>

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
    </div>
  );
}

// ── Servers Tab ────────────────────────────────────────────────────────────────

function ServersTab({ group }: { group: DNSServerGroup }) {
  const qc = useQueryClient();
  const [showAdd, setShowAdd] = useState(false);
  const [editServer, setEditServer] = useState<DNSServer | null>(null);
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
  };
  const dotCls: Record<string, string> = {
    active: "bg-emerald-500",
    unreachable: "bg-red-500",
    syncing: "bg-blue-500",
    error: "bg-red-500",
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
            className="flex items-center justify-between rounded-md border bg-card px-3 py-2.5 group"
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
            <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100">
              <button
                className="h-7 w-7 flex items-center justify-center rounded text-muted-foreground hover:text-foreground"
                onClick={() => setEditServer(s)}
              >
                <Pencil className="h-3.5 w-3.5" />
              </button>
              <button
                className="h-7 w-7 flex items-center justify-center rounded text-muted-foreground hover:text-destructive"
                onClick={() => setConfirmDeleteServer(s)}
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
    onError: (e: ApiError) => setError(e?.response?.data?.detail ?? "Error"),
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
          <tbody>
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
  const [showAdd, setShowAdd] = useState(false);
  const [showZoneFilters, setShowZoneFilters] = useState(false);
  const [zoneNameFilter, setZoneNameFilter] = useState("");
  const [zoneTypeFilter, setZoneTypeFilter] = useState("");

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

  function renderZoneNode(node: DnsTreeNode, depth: number): React.ReactNode {
    const indent = depth * 16;
    return (
      <div key={node.domain}>
        {/* If this node has a zone, render it as a clickable card */}
        {node.zone ? (
          <div
            className="flex items-center justify-between rounded-md border bg-card px-3 py-2 group hover:bg-accent/30 cursor-pointer mb-1"
            style={{ marginLeft: indent }}
            onClick={() => onSelectZone(node.zone!)}
          >
            <div className="flex items-center gap-2 min-w-0">
              <FileText className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
              <span className="font-mono text-sm truncate">
                {node.zone.name.replace(/\.$/, "")}
              </span>
              <span
                className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium flex-shrink-0 ${typeBadge[node.zone.zone_type] ?? "bg-muted text-muted-foreground"}`}
              >
                {node.zone.zone_type}
              </span>
              {node.zone.dnssec_enabled && (
                <Shield className="h-3 w-3 text-emerald-500 flex-shrink-0" />
              )}
            </div>
          </div>
        ) : (
          /* Folder-only node (intermediate domain level with no zone registered) */
          <div
            className="flex items-center gap-1.5 mb-1"
            style={{ marginLeft: indent }}
          >
            <Folder className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
            <span className="text-xs font-semibold text-muted-foreground">
              .{node.domain}
            </span>
          </div>
        )}
        {/* Recurse into children */}
        {node.children.length > 0 && (
          <div>
            {node.children.map((child) => renderZoneNode(child, depth + 1))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
          {hasZoneFilter
            ? `${filteredZones.length} / ${zones.length}`
            : zones.length}{" "}
          zone{zones.length !== 1 ? "s" : ""}
        </span>
        <div className="flex items-center gap-2">
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
            className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
            disabled={zones.length === 0}
            onClick={async () => {
              const blob = await dnsApi.exportAllZones(group.id);
              downloadBlob(
                blob,
                `dns-zones-${group.id}.zip`,
                "application/zip",
              );
            }}
          >
            <Download className="h-3 w-3" /> Export All
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

      {tree.map((root) => renderZoneNode(root, 0))}

      {showAdd && (
        <ZoneModal
          groupId={group.id}
          views={views}
          onClose={() => setShowAdd(false)}
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
  const [editList, setEditList] = useState<DNSBlockList | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<DNSBlockList | null>(null);

  const { data: lists = [], isFetching } = useQuery({
    queryKey: ["dns-blocklists"],
    queryFn: () => dnsBlocklistApi.list(),
  });

  // Filter by lists applied to this group (or not yet applied anywhere)
  const applied = lists.filter((l) => l.applied_group_ids.includes(group.id));
  const other = lists.filter((l) => !l.applied_group_ids.includes(group.id));

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
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dns-blocklists"] }),
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
        <button
          className="flex items-center gap-1 rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90"
          onClick={() => setShowCreate(true)}
        >
          <Plus className="h-3 w-3" /> New Blocking List
        </button>
      </div>

      {isFetching && lists.length === 0 && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}

      {[
        { label: "Applied to this group", rows: applied, assigned: true },
        { label: "Available (not applied)", rows: other, assigned: false },
      ].map((section) => (
        <div key={section.label}>
          <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
            {section.label}
          </h4>
          {section.rows.length === 0 && (
            <p className="text-xs text-muted-foreground italic">None.</p>
          )}
          <div className="space-y-1">
            {section.rows.map((l) => (
              <div
                key={l.id}
                className="flex items-center gap-2 rounded-md border bg-card px-3 py-2 group hover:bg-accent/30 cursor-pointer"
                onClick={() => setSelected(l)}
              >
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
                  className="flex items-center gap-1 opacity-0 group-hover:opacity-100"
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
                      title="Refresh from feed"
                      className="h-6 w-6 flex items-center justify-center rounded text-muted-foreground hover:text-foreground"
                      onClick={() => refreshMut.mutate(l.id)}
                    >
                      <RefreshCw className="h-3 w-3" />
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
            ))}
          </div>
        </div>
      ))}

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
          <tbody>
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
          <tbody>
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
        {tab === "options" && <OptionsTab groupId={group.id} />}
      </div>
    </div>
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
                <FileText className="h-3 w-3 flex-shrink-0" />
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
            <FileText className="h-3 w-3 flex-shrink-0" />
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

  // Deep-link from global search: navigate("/dns", { state: { selectGroup, selectZone } })
  useEffect(() => {
    const state = location.state as {
      selectGroup?: string;
      selectZone?: string;
    } | null;
    if (!state?.selectGroup || groups.length === 0) return;
    const group = groups.find(
      (g: DNSServerGroup) => g.id === state.selectGroup,
    );
    if (!group) return;
    setExpandedGroups((prev) => new Set([...prev, group.id]));
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

  // URL-state restore: reopen last-visited group/zone on back-navigation
  useEffect(() => {
    if (urlRestored.current) return;
    if (groups.length === 0) return;
    urlRestored.current = true;
    const groupId = searchParams.get("group");
    const zoneId = searchParams.get("zone");
    if (!groupId) return;
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
  }, [groups]);

  const deleteGroup = useMutation({
    mutationFn: (id: string) => dnsApi.deleteGroup(id),
    onSuccess: (_, id) => {
      qc.invalidateQueries({ queryKey: ["dns-groups"] });
      if (selection && "group" in selection && selection.group.id === id)
        setSelection(null);
      setConfirmDeleteGroup(null);
    },
  });

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
          <button
            className="flex h-6 w-6 items-center justify-center rounded hover:bg-accent"
            onClick={() => setShowCreateGroup(true)}
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
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
            const zoneInGroup =
              selection?.type === "zone" && selection.group.id === g.id;

            return (
              <div key={g.id}>
                {/* Group row */}
                <div
                  className={`flex items-center rounded-md mx-1 ${groupSelected ? "bg-primary text-primary-foreground" : ""}`}
                >
                  {/* Expand toggle */}
                  <button
                    className={`flex h-7 w-6 items-center justify-center flex-shrink-0 ${groupSelected ? "text-primary-foreground" : "text-muted-foreground hover:text-foreground"}`}
                    onClick={() => toggleGroup(g.id)}
                  >
                    {expanded ? (
                      <ChevronDown className="h-3.5 w-3.5" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5" />
                    )}
                  </button>
                  {/* Group name — click to select */}
                  <button
                    className="flex flex-1 items-center gap-2 py-1.5 pr-1 min-w-0"
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

                {/* Zone tree (when group expanded) */}
                {(expanded || zoneInGroup) && (
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
          description={`Permanently delete group "${confirmDeleteGroup.name}" and all its servers, zones, ACLs, and options?`}
          checkLabel={`I understand all data in "${confirmDeleteGroup.name}" will be permanently deleted.`}
          onConfirm={() => deleteGroup.mutate(confirmDeleteGroup.id)}
          onClose={() => setConfirmDeleteGroup(null)}
          isPending={deleteGroup.isPending}
        />
      )}
    </div>
  );
}
