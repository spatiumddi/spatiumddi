import { useState, useEffect } from "react";
import { useLocation } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Globe, Plus, Trash2, Pencil, ChevronDown, ChevronRight,
  Settings2, Shield, Eye, FileText, Layers, RefreshCw, X, Cpu,
  FolderOpen, Folder,
} from "lucide-react";
import {
  dnsApi,
  type DNSServerGroup, type DNSServer, type DNSZone, type DNSView, type DNSRecord,
} from "@/lib/api";

// ── Shared primitives ─────────────────────────────────────────────────────────

const inputCls =
  "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <label className="block text-xs font-medium text-muted-foreground">{label}</label>
      {children}
    </div>
  );
}

function Modal({
  title, onClose, children, wide,
}: { title: string; onClose: () => void; children: React.ReactNode; wide?: boolean }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className={`w-full ${wide ? "max-w-2xl" : "max-w-md"} rounded-lg border bg-card p-6 shadow-lg max-h-[90vh] overflow-y-auto`}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold">{title}</h2>
          <button onClick={onClose} className="rounded p-1 text-muted-foreground hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

type ApiError = { response?: { data?: { detail?: string } } };

function Btns({ onClose, pending, label }: { onClose: () => void; pending: boolean; label?: string }) {
  return (
    <div className="flex justify-end gap-2 pt-2">
      <button type="button" onClick={onClose} className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent">Cancel</button>
      <button type="submit" disabled={pending} className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
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
            <button onClick={onClose} className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted">Cancel</button>
            <button onClick={() => setStep(2)} className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90">
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
        <p className="text-sm font-medium text-destructive">This action cannot be undone.</p>
        <p className="text-sm text-muted-foreground">{description}</p>
        <label className="flex cursor-pointer items-start gap-2 text-sm">
          <input type="checkbox" className="mt-0.5" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
          {checkLabel}
        </label>
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted">Cancel</button>
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

// ── Zone TLD tree helper ──────────────────────────────────────────────────────

interface TldGroup { tld: string; zones: DNSZone[] }

function buildTldTree(zones: DNSZone[]): TldGroup[] {
  const map = new Map<string, DNSZone[]>();
  for (const z of zones) {
    const name = z.name.replace(/\.$/, "");
    const parts = name.split(".");
    const tld = parts[parts.length - 1];
    if (!map.has(tld)) map.set(tld, []);
    map.get(tld)!.push(z);
  }
  return Array.from(map.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([tld, zs]) => ({ tld, zones: zs.sort((a, b) => a.name.localeCompare(b.name)) }));
}

// ── Group Modal (create / edit) ───────────────────────────────────────────────

function GroupModal({ group, onClose }: { group?: DNSServerGroup; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(group?.name ?? "");
  const [description, setDescription] = useState(group?.description ?? "");
  const [groupType, setGroupType] = useState(group?.group_type ?? "internal");
  const [isRecursive, setIsRecursive] = useState(group?.is_recursive ?? true);
  const [error, setError] = useState("");

  const mut = useMutation({
    mutationFn: (d: Partial<DNSServerGroup>) =>
      group ? dnsApi.updateGroup(group.id, d) : dnsApi.createGroup(d),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["dns-groups"] }); onClose(); },
    onError: (e: ApiError) => setError(e?.response?.data?.detail ?? "Error"),
  });

  return (
    <Modal title={group ? "Edit Server Group" : "New Server Group"} onClose={onClose}>
      <form onSubmit={(e) => { e.preventDefault(); setError(""); mut.mutate({ name, description, group_type: groupType, is_recursive: isRecursive }); }} className="space-y-3">
        <Field label="Name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. internal-resolvers" required autoFocus /></Field>
        <Field label="Description"><input className={inputCls} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Optional" /></Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Type">
            <select className={inputCls} value={groupType} onChange={(e) => setGroupType(e.target.value)}>
              <option value="internal">Internal</option>
              <option value="external">External</option>
              <option value="dmz">DMZ</option>
              <option value="custom">Custom</option>
            </select>
          </Field>
          <Field label="Recursion">
            <label className="flex items-center gap-2 mt-2 cursor-pointer">
              <input type="checkbox" checked={isRecursive} onChange={(e) => setIsRecursive(e.target.checked)} className="h-4 w-4" />
              <span className="text-sm">Allow recursion</span>
            </label>
          </Field>
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <Btns onClose={onClose} pending={mut.isPending} label={group ? "Save" : "Create"} />
      </form>
    </Modal>
  );
}

// ── Server Modal (add / edit) ─────────────────────────────────────────────────

function ServerModal({ groupId, server, onClose }: { groupId: string; server?: DNSServer; onClose: () => void }) {
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
      server ? dnsApi.updateServer(groupId, server.id, d) : dnsApi.createServer(groupId, d),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["dns-servers", groupId] }); onClose(); },
    onError: (e: ApiError) => setError(e?.response?.data?.detail ?? "Error"),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault(); setError("");
    const roleList = roles.split(/[,\s]+/).map((r) => r.trim()).filter(Boolean);
    mut.mutate({
      name, driver, host,
      port: parseInt(port, 10),
      api_port: apiPort ? parseInt(apiPort, 10) : null,
      roles: roleList,
      notes,
      ...(apiKey ? { api_key: apiKey } : {}),
    });
  }

  return (
    <Modal title={server ? `Edit ${server.name}` : "Add Server"} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="ns1" required autoFocus /></Field>
          <Field label="Driver">
            <select className={inputCls} value={driver} onChange={(e) => setDriver(e.target.value)}>
              <option value="bind9">BIND9</option>
              <option value="powerdns">PowerDNS</option>
            </select>
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Host / IP"><input className={inputCls} value={host} onChange={(e) => setHost(e.target.value)} placeholder="10.0.0.53" required /></Field>
          <Field label="DNS Port"><input className={inputCls} value={port} onChange={(e) => setPort(e.target.value)} placeholder="53" /></Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="API Port (rndc / REST)"><input className={inputCls} value={apiPort} onChange={(e) => setApiPort(e.target.value)} placeholder="953 / 8081" /></Field>
          <Field label={server ? "New API Key (leave blank to keep)" : "API Key"}>
            <input type="password" className={inputCls} value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder={server ? "unchanged" : "optional"} />
          </Field>
        </div>
        <Field label="Roles (comma-separated)">
          <input className={inputCls} value={roles} onChange={(e) => setRoles(e.target.value)} placeholder="authoritative, recursive" />
        </Field>
        <Field label="Notes"><input className={inputCls} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Optional notes" /></Field>
        {server && (
          <p className="text-xs text-muted-foreground">
            Servers can also be auto-registered by the DNS agent container — see <code>DNS_AGENT_KEY</code> in deployment docs.
          </p>
        )}
        {error && <p className="text-sm text-destructive">{error}</p>}
        <Btns onClose={onClose} pending={mut.isPending} label={server ? "Save" : "Add Server"} />
      </form>
    </Modal>
  );
}

// ── Zone Modal (add / edit) ───────────────────────────────────────────────────

function ZoneModal({ groupId, views, zone, onClose }: { groupId: string; views: DNSView[]; zone?: DNSZone; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(zone?.name?.replace(/\.$/, "") ?? "");
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
      zone ? dnsApi.updateZone(groupId, zone.id, d) : dnsApi.createZone(groupId, d),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["dns-zones", groupId] }); onClose(); },
    onError: (e: ApiError) => setError(e?.response?.data?.detail ?? "Error"),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault(); setError("");
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
          <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="example.com" required autoFocus disabled={!!zone} />
          {!zone && <p className="text-xs text-muted-foreground mt-0.5">Trailing dot added automatically.</p>}
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Type">
            <select className={inputCls} value={zoneType} onChange={(e) => setZoneType(e.target.value)}>
              <option value="primary">Primary</option>
              <option value="secondary">Secondary</option>
              <option value="stub">Stub</option>
              <option value="forward">Forward</option>
            </select>
          </Field>
          <Field label="Kind">
            <select className={inputCls} value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="forward">Forward lookup</option>
              <option value="reverse">Reverse lookup</option>
            </select>
          </Field>
        </div>
        {views.length > 0 && (
          <Field label="View (optional)">
            <select className={inputCls} value={viewId} onChange={(e) => setViewId(e.target.value)}>
              <option value="">— No view —</option>
              {views.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
            </select>
          </Field>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Primary NS"><input className={inputCls} value={primaryNs} onChange={(e) => setPrimaryNs(e.target.value)} placeholder="ns1.example.com." /></Field>
          <Field label="Admin Email"><input className={inputCls} value={adminEmail} onChange={(e) => setAdminEmail(e.target.value)} placeholder="hostmaster.example.com." /></Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Default TTL (seconds)"><input className={inputCls} value={ttl} onChange={(e) => setTtl(e.target.value)} placeholder="3600" /></Field>
          <Field label="DNSSEC">
            <label className="flex items-center gap-2 mt-2 cursor-pointer">
              <input type="checkbox" checked={dnssec} onChange={(e) => setDnssec(e.target.checked)} className="h-4 w-4" />
              <span className="text-sm">Enable DNSSEC</span>
            </label>
          </Field>
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <Btns onClose={onClose} pending={mut.isPending} label={zone ? "Save" : "Add Zone"} />
      </form>
    </Modal>
  );
}

// ── Record Modal (add / edit) ─────────────────────────────────────────────────

const RECORD_TYPES = ["A", "AAAA", "CNAME", "MX", "TXT", "NS", "PTR", "SRV", "CAA", "TLSA", "SSHFP", "NAPTR", "LOC"];

function RecordModal({ groupId, zoneId, record, onClose }: { groupId: string; zoneId: string; record?: DNSRecord; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(record?.name ?? "");
  const [type, setType] = useState(record?.record_type ?? "A");
  const [value, setValue] = useState(record?.value ?? "");
  const [ttl, setTtl] = useState(String(record?.ttl ?? ""));
  const [priority, setPriority] = useState(String(record?.priority ?? ""));
  const [error, setError] = useState("");

  const showPriority = ["MX", "SRV"].includes(type);

  const mut = useMutation({
    mutationFn: (d: Record<string, unknown>) =>
      record
        ? dnsApi.updateRecord(groupId, zoneId, record.id, d)
        : dnsApi.createRecord(groupId, zoneId, d),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["dns-records", zoneId] }); onClose(); },
    onError: (e: ApiError) => setError(e?.response?.data?.detail ?? "Error"),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault(); setError("");
    mut.mutate({
      name,
      record_type: type,
      value,
      ttl: ttl ? parseInt(ttl, 10) : null,
      priority: priority ? parseInt(priority, 10) : null,
    });
  }

  return (
    <Modal title={record ? "Edit Record" : "Add Record"} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name (relative to zone)">
            <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder='@ for apex, "www", "mail"' required autoFocus />
          </Field>
          <Field label="Type">
            <select className={inputCls} value={type} onChange={(e) => setType(e.target.value)}>
              {RECORD_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </Field>
        </div>
        <Field label="Value">
          <input className={inputCls} value={value} onChange={(e) => setValue(e.target.value)} placeholder={type === "A" ? "10.0.0.1" : type === "CNAME" ? "other.example.com." : "record value"} required />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="TTL (leave blank for zone default)">
            <input className={inputCls} value={ttl} onChange={(e) => setTtl(e.target.value)} placeholder="zone default" />
          </Field>
          {showPriority && (
            <Field label="Priority">
              <input className={inputCls} value={priority} onChange={(e) => setPriority(e.target.value)} placeholder="10" />
            </Field>
          )}
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <Btns onClose={onClose} pending={mut.isPending} label={record ? "Save" : "Add Record"} />
      </form>
    </Modal>
  );
}

// ── Zone Detail View (records panel) ─────────────────────────────────────────

function ZoneDetailView({ group, zone, onDeleted }: { group: DNSServerGroup; zone: DNSZone; onDeleted: () => void }) {
  const qc = useQueryClient();
  const [showAddRecord, setShowAddRecord] = useState(false);
  const [editRecord, setEditRecord] = useState<DNSRecord | null>(null);
  const [showEditZone, setShowEditZone] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [typeFilter, setTypeFilter] = useState("");

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
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["dns-zones", group.id] }); onDeleted(); },
  });

  const deleteRecord = useMutation({
    mutationFn: (r: DNSRecord) => dnsApi.deleteRecord(group.id, zone.id, r.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dns-records", zone.id] }),
  });

  const filtered = typeFilter ? records.filter((r) => r.record_type === typeFilter) : records;
  const recordTypes = [...new Set(records.map((r) => r.record_type))].sort();

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
            <h2 className="font-semibold text-base font-mono">{zone.name}</h2>
            <span className="inline-flex items-center rounded border px-1.5 py-0.5 text-xs">{zone.zone_type}</span>
            <span className="text-xs text-muted-foreground">{zone.kind}</span>
            {zone.dnssec_enabled && (
              <span className="inline-flex items-center rounded px-1.5 py-0.5 text-xs bg-emerald-500/15 text-emerald-600">DNSSEC</span>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            TTL {zone.ttl}s · serial {zone.last_serial || "—"}
            {zone.primary_ns && ` · ${zone.primary_ns}`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent" onClick={() => setShowEditZone(true)}>
            <Pencil className="h-3 w-3" /> Edit Zone
          </button>
          <button className="flex items-center gap-1 rounded-md border border-destructive/40 px-2 py-1 text-xs text-destructive hover:bg-destructive/10" onClick={() => setConfirmDelete(true)}>
            <Trash2 className="h-3 w-3" /> Delete Zone
          </button>
          <button className="flex items-center gap-1 rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90" onClick={() => setShowAddRecord(true)}>
            <Plus className="h-3 w-3" /> Add Record
          </button>
        </div>
      </div>

      {/* Filter bar */}
      <div className="flex items-center gap-2 border-b px-5 py-2">
        <span className="text-xs text-muted-foreground">{records.length} record{records.length !== 1 ? "s" : ""}</span>
        {recordTypes.length > 0 && (
          <div className="flex items-center gap-1 ml-2">
            <button
              onClick={() => setTypeFilter("")}
              className={`rounded px-2 py-0.5 text-xs ${!typeFilter ? "bg-primary text-primary-foreground" : "hover:bg-accent"}`}
            >
              All
            </button>
            {recordTypes.map((t) => (
              <button
                key={t}
                onClick={() => setTypeFilter(typeFilter === t ? "" : t)}
                className={`rounded px-2 py-0.5 text-xs ${typeFilter === t ? "bg-primary text-primary-foreground" : "hover:bg-accent"}`}
              >
                {t}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Records table */}
      <div className="flex-1 overflow-auto">
        {isFetching && records.length === 0 && <p className="px-5 py-4 text-sm text-muted-foreground">Loading…</p>}
        {filtered.length === 0 && !isFetching && (
          <div className="flex flex-col items-center justify-center h-40">
            <p className="text-sm text-muted-foreground italic">
              {typeFilter ? `No ${typeFilter} records.` : "No records yet. Click \"Add Record\" to create one."}
            </p>
          </div>
        )}
        {filtered.length > 0 && (
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-card">
              <tr className="border-b text-xs text-muted-foreground">
                <th className="py-2 pl-5 text-left font-medium">Name</th>
                <th className="py-2 text-left font-medium">Type</th>
                <th className="py-2 text-left font-medium">Value</th>
                <th className="py-2 text-left font-medium">TTL</th>
                <th className="py-2 text-left font-medium">Pri</th>
                <th className="py-2 pr-3" />
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => (
                <tr key={r.id} className="border-b last:border-0 hover:bg-muted/40 group">
                  <td className="py-1.5 pl-5 font-mono text-xs font-medium">{r.name}</td>
                  <td className="py-1.5">
                    <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${typeBadge[r.record_type] ?? "bg-muted text-muted-foreground"}`}>
                      {r.record_type}
                    </span>
                  </td>
                  <td className="py-1.5 font-mono text-xs text-muted-foreground max-w-xs truncate">{r.value}</td>
                  <td className="py-1.5 text-xs text-muted-foreground">{r.ttl ?? "—"}</td>
                  <td className="py-1.5 text-xs text-muted-foreground">{r.priority ?? "—"}</td>
                  <td className="py-1.5 pr-3">
                    <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100">
                      {!r.auto_generated && (
                        <button className="h-6 w-6 flex items-center justify-center rounded text-muted-foreground hover:text-foreground" onClick={() => setEditRecord(r)}>
                          <Pencil className="h-3 w-3" />
                        </button>
                      )}
                      <button
                        className="h-6 w-6 flex items-center justify-center rounded text-muted-foreground hover:text-destructive"
                        onClick={() => { if (confirm(`Delete record ${r.name} ${r.record_type}?`)) deleteRecord.mutate(r); }}
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showAddRecord && <RecordModal groupId={group.id} zoneId={zone.id} onClose={() => setShowAddRecord(false)} />}
      {editRecord    && <RecordModal groupId={group.id} zoneId={zone.id} record={editRecord} onClose={() => setEditRecord(null)} />}
      {showEditZone  && <ZoneModal groupId={group.id} views={views} zone={zone} onClose={() => setShowEditZone(false)} />}
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
  const [confirmDeleteServer, setConfirmDeleteServer] = useState<DNSServer | null>(null);

  const { data: servers = [], isFetching } = useQuery({
    queryKey: ["dns-servers", group.id],
    queryFn: () => dnsApi.listServers(group.id),
  });

  const del = useMutation({
    mutationFn: (s: DNSServer) => dnsApi.deleteServer(group.id, s.id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["dns-servers", group.id] }); setConfirmDeleteServer(null); },
  });

  const statusCls: Record<string, string> = {
    active: "bg-emerald-500/15 text-emerald-600",
    unreachable: "bg-red-500/15 text-red-600",
    syncing: "bg-blue-500/15 text-blue-600",
    error: "bg-red-500/15 text-red-600",
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div>
          <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">DNS Servers</span>
          <p className="text-xs text-muted-foreground mt-0.5">
            Servers can also be auto-registered by BIND9/PowerDNS agent containers using the <code className="font-mono">DNS_AGENT_KEY</code> env var.
          </p>
        </div>
        <button className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent" onClick={() => setShowAdd(true)}>
          <Plus className="h-3 w-3" /> Add Server
        </button>
      </div>
      {isFetching && servers.length === 0 && <p className="text-sm text-muted-foreground">Loading…</p>}
      {servers.length === 0 && !isFetching && (
        <p className="text-sm text-muted-foreground italic">No servers. Add one manually or start a DNS agent container.</p>
      )}
      <div className="space-y-2">
        {servers.map((s) => (
          <div key={s.id} className="flex items-center justify-between rounded-md border bg-card px-3 py-2.5 group">
            <div className="flex items-center gap-3">
              <Cpu className="h-4 w-4 text-muted-foreground flex-shrink-0" />
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">{s.name}</span>
                  <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${statusCls[s.status] ?? "bg-muted text-muted-foreground"}`}>{s.status}</span>
                  <span className="inline-flex items-center rounded border px-1.5 py-0.5 text-xs">{s.driver}</span>
                </div>
                <p className="text-xs text-muted-foreground">
                  {s.host}:{s.port}
                  {s.roles.length > 0 && ` · ${s.roles.join(", ")}`}
                  {s.last_sync_at && ` · synced ${new Date(s.last_sync_at).toLocaleDateString()}`}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100">
              <button className="h-7 w-7 flex items-center justify-center rounded text-muted-foreground hover:text-foreground" onClick={() => setEditServer(s)}>
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
      {showAdd    && <ServerModal groupId={group.id} onClose={() => setShowAdd(false)} />}
      {editServer && <ServerModal groupId={group.id} server={editServer} onClose={() => setEditServer(null)} />}
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
        <p className="text-sm text-muted-foreground italic">No views defined. Views enable split-horizon DNS.</p>
      ) : (
        <div className="space-y-2">
          {views.map((v) => (
            <div key={v.id} className="rounded-md border bg-card px-3 py-2.5">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{v.name}</span>
                <span className="text-xs text-muted-foreground">order: {v.order}</span>
              </div>
              {v.description && <p className="text-xs text-muted-foreground mt-0.5">{v.description}</p>}
              <div className="mt-1.5 flex flex-wrap gap-1">
                {v.match_clients.map((c) => (
                  <span key={c} className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-xs font-mono">{c}</span>
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
      setShowCreate(false); setNewName(""); setNewDesc(""); setNewEntries("");
    },
    onError: (e: ApiError) => setError(e?.response?.data?.detail ?? "Error"),
  });
  const delMut = useMutation({
    mutationFn: (id: string) => dnsApi.deleteAcl(groupId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dns-acls", groupId] }),
  });

  function createAcl(e: React.FormEvent) {
    e.preventDefault(); setError("");
    const entries = newEntries.split("\n").map((l) => l.trim()).filter(Boolean).map((val, i) => ({
      value: val.startsWith("!") ? val.slice(1) : val,
      negate: val.startsWith("!"),
      order: i,
    }));
    createMut.mutate({ name: newName, description: newDesc, entries });
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">Named ACLs</span>
        <button className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent" onClick={() => setShowCreate(true)}>
          <Plus className="h-3 w-3" /> New ACL
        </button>
      </div>
      {showCreate && (
        <form onSubmit={createAcl} className="mb-4 rounded-md border bg-muted/30 p-3 space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="ACL Name"><input className={inputCls} value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="internal-clients" required /></Field>
            <Field label="Description"><input className={inputCls} value={newDesc} onChange={(e) => setNewDesc(e.target.value)} placeholder="Optional" /></Field>
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
            <button type="button" className="rounded-md border px-2 py-1 text-xs hover:bg-accent" onClick={() => setShowCreate(false)}>Cancel</button>
            <button type="submit" disabled={createMut.isPending} className="rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground disabled:opacity-50">Create</button>
          </div>
        </form>
      )}
      {acls.length === 0 && !showCreate && (
        <p className="text-sm text-muted-foreground italic">No named ACLs defined.</p>
      )}
      <div className="space-y-2">
        {acls.map((acl) => (
          <div key={acl.id} className="rounded-md border bg-card px-3 py-2.5 group">
            <div className="flex items-center justify-between">
              <div>
                <span className="text-sm font-medium font-mono">{acl.name}</span>
                {acl.description && <span className="ml-2 text-xs text-muted-foreground">{acl.description}</span>}
              </div>
              <button
                className="h-7 w-7 flex items-center justify-center rounded opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive"
                onClick={() => { if (confirm(`Delete ACL "${acl.name}"?`)) delMut.mutate(acl.id); }}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
            {acl.entries.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {acl.entries.map((entry) => (
                  <span key={entry.id} className={`inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-xs font-mono ${entry.negate ? "line-through opacity-60" : ""}`}>
                    {entry.negate ? "!" : ""}{entry.value}
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
    setInitialized(true);
  }

  const saveMut = useMutation({
    mutationFn: (d: Record<string, unknown>) => dnsApi.updateOptions(groupId, d),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-options", groupId] });
      setDirty(false); setSaved(true); setTimeout(() => setSaved(false), 2000);
    },
  });

  function list(s: string) { return s.split(/[,\n]+/).map((x) => x.trim()).filter(Boolean); }

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
    });
  }

  if (isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>;

  // Full-width select to prevent text/arrow overlap
  const selCls = "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring appearance-none";

  const card = "rounded-md border p-4 space-y-3";
  const cardTitle = "text-sm font-medium flex items-center gap-2";

  return (
    <div className="space-y-5 max-w-xl">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">Zone/view overrides take precedence over server defaults.</p>
        <button
          disabled={!dirty || saveMut.isPending}
          onClick={save}
          className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm disabled:opacity-50 ${saved ? "border text-emerald-600" : "bg-primary text-primary-foreground hover:bg-primary/90"}`}
        >
          {saved ? <><RefreshCw className="h-3.5 w-3.5" /> Saved</> : saveMut.isPending ? "Saving…" : "Save Changes"}
        </button>
      </div>

      <div className={card}>
        <div className="flex items-center justify-between">
          <h4 className={cardTitle}><Layers className="h-4 w-4 text-muted-foreground" /> Forwarders</h4>
          <label className="flex items-center gap-2 cursor-pointer text-sm">
            <input
              type="checkbox"
              checked={forwardersEnabled}
              onChange={(e) => { setForwardersEnabled(e.target.checked); setDirty(true); }}
              className="h-4 w-4"
            />
            Enable
          </label>
        </div>
        {!forwardersEnabled && (
          <p className="text-xs text-muted-foreground">
            Forwarders disabled — suitable for authoritative-only or air-gapped servers.
          </p>
        )}
        {forwardersEnabled && (
          <>
            <Field label="Upstream resolvers (one per line)">
              <textarea
                value={forwarders}
                onChange={(e) => { setForwarders(e.target.value); setDirty(true); }}
                className="w-full rounded border bg-background px-2 py-1 font-mono text-xs resize-none h-16 focus:outline-none focus:ring-1 focus:ring-ring"
                placeholder={"1.1.1.1\n8.8.8.8"}
              />
            </Field>
            <Field label="Forward policy">
              <select className={selCls} value={forwardPolicy} onChange={(e) => { setForwardPolicy(e.target.value); setDirty(true); }}>
                <option value="first">first — try forwarders first, fall back to recursion</option>
                <option value="only">only — always send to forwarders, never recurse</option>
              </select>
            </Field>
          </>
        )}
      </div>

      <div className={card}>
        <h4 className={cardTitle}>Recursion</h4>
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={recursionEnabled} onChange={(e) => { setRecursionEnabled(e.target.checked); setDirty(true); }} className="h-4 w-4" />
          <span className="text-sm">Enable recursion</span>
        </label>
        <Field label="allow-recursion (comma-separated CIDRs / ACL names)">
          <input className={inputCls} value={allowRecursion} onChange={(e) => { setAllowRecursion(e.target.value); setDirty(true); }} placeholder="any" />
        </Field>
      </div>

      <div className={card}>
        <h4 className={cardTitle}><Shield className="h-4 w-4 text-muted-foreground" /> DNSSEC Validation</h4>
        <Field label="Validation mode">
          <select className={selCls} value={dnssecValidation} onChange={(e) => { setDnssecValidation(e.target.value); setDirty(true); }}>
            <option value="auto">auto — validate using built-in managed keys (recommended)</option>
            <option value="yes">yes — validate; trust anchors must be configured manually</option>
            <option value="no">no — do not validate DNSSEC signatures</option>
          </select>
        </Field>
      </div>

      <div className={card}>
        <h4 className={cardTitle}>Notify</h4>
        <Field label="Notify mode">
          <select className={selCls} value={notifyEnabled} onChange={(e) => { setNotifyEnabled(e.target.value); setDirty(true); }}>
            <option value="yes">yes — notify all servers listed in NS records</option>
            <option value="explicit">explicit — only notify servers in also-notify list</option>
            <option value="master-only">master-only — only send notifies from primary</option>
            <option value="no">no — disable zone change notifications</option>
          </select>
        </Field>
      </div>

      <div className={card}>
        <h4 className={cardTitle}>Query &amp; Transfer ACLs</h4>
        <Field label="allow-query (comma-separated CIDRs / ACL names)">
          <input className={inputCls} value={allowQuery} onChange={(e) => { setAllowQuery(e.target.value); setDirty(true); }} placeholder="any" />
        </Field>
        <Field label="allow-transfer (comma-separated CIDRs / ACL names)">
          <input className={inputCls} value={allowTransfer} onChange={(e) => { setAllowTransfer(e.target.value); setDirty(true); }} placeholder="none" />
        </Field>
      </div>
    </div>
  );
}

// ── Zones Tab ─────────────────────────────────────────────────────────────────

function ZonesTab({ group, onSelectZone }: { group: DNSServerGroup; onSelectZone: (z: DNSZone) => void }) {
  const qc = useQueryClient();
  const [showAdd, setShowAdd] = useState(false);
  const [editZone, setEditZone] = useState<DNSZone | null>(null);
  const [confirmDeleteZone, setConfirmDeleteZone] = useState<DNSZone | null>(null);

  const { data: zones = [], isFetching } = useQuery({
    queryKey: ["dns-zones", group.id],
    queryFn: () => dnsApi.listZones(group.id),
  });

  const { data: views = [] } = useQuery({
    queryKey: ["dns-views", group.id],
    queryFn: () => dnsApi.listViews(group.id),
  });

  const deleteZone = useMutation({
    mutationFn: (z: DNSZone) => dnsApi.deleteZone(group.id, z.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-zones", group.id] });
      setConfirmDeleteZone(null);
    },
  });

  const tree = buildTldTree(zones);

  const typeBadge: Record<string, string> = {
    primary:   "bg-blue-500/15 text-blue-600",
    secondary: "bg-violet-500/15 text-violet-600",
    stub:      "bg-amber-500/15 text-amber-600",
    forward:   "bg-muted text-muted-foreground",
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
          {zones.length} zone{zones.length !== 1 ? "s" : ""}
        </span>
        <button
          className="flex items-center gap-1 rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90"
          onClick={() => setShowAdd(true)}
        >
          <Plus className="h-3 w-3" /> Add Zone
        </button>
      </div>

      {isFetching && zones.length === 0 && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}
      {zones.length === 0 && !isFetching && (
        <p className="text-sm text-muted-foreground italic">
          No zones yet. Click "Add Zone" to create one.
        </p>
      )}

      {tree.map(({ tld, zones: tzones }) => (
        <div key={tld} className="mb-3">
          {/* TLD header */}
          <div className="flex items-center gap-1.5 mb-1">
            <Folder className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
            <span className="text-xs font-semibold text-muted-foreground">.{tld}</span>
          </div>
          {/* Zone rows */}
          <div className="ml-4 space-y-1">
            {tzones.map((z) => (
              <div
                key={z.id}
                className="flex items-center justify-between rounded-md border bg-card px-3 py-2 group hover:bg-accent/30 cursor-pointer"
                onClick={() => onSelectZone(z)}
              >
                <div className="flex items-center gap-2 min-w-0">
                  <FileText className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
                  <span className="font-mono text-sm truncate">{z.name}</span>
                  <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium flex-shrink-0 ${typeBadge[z.zone_type] ?? "bg-muted text-muted-foreground"}`}>
                    {z.zone_type}
                  </span>
                  {z.dnssec_enabled && (
                    <Shield className="h-3 w-3 text-emerald-500 flex-shrink-0" />
                  )}
                </div>
                <div
                  className="flex items-center gap-1 opacity-0 group-hover:opacity-100 flex-shrink-0"
                  onClick={(e) => e.stopPropagation()}
                >
                  <button
                    className="h-6 w-6 flex items-center justify-center rounded text-muted-foreground hover:text-foreground"
                    onClick={() => setEditZone(z)}
                  >
                    <Pencil className="h-3 w-3" />
                  </button>
                  <button
                    className="h-6 w-6 flex items-center justify-center rounded text-muted-foreground hover:text-destructive"
                    onClick={() => setConfirmDeleteZone(z)}
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}

      {showAdd && (
        <ZoneModal groupId={group.id} views={views} onClose={() => setShowAdd(false)} />
      )}
      {editZone && (
        <ZoneModal groupId={group.id} views={views} zone={editZone} onClose={() => setEditZone(null)} />
      )}
      {confirmDeleteZone && (
        <ConfirmDestroyModal
          title="Delete DNS Zone"
          description={`Permanently delete zone "${confirmDeleteZone.name}" and all its records?`}
          checkLabel={`I understand all records in "${confirmDeleteZone.name}" will be permanently deleted.`}
          onConfirm={() => deleteZone.mutate(confirmDeleteZone)}
          onClose={() => setConfirmDeleteZone(null)}
          isPending={deleteZone.isPending}
        />
      )}
    </div>
  );
}

// ── Group Detail View ─────────────────────────────────────────────────────────

type GroupTab = "zones" | "servers" | "views" | "acls" | "options";

function GroupDetailView({ group, onSelectZone }: { group: DNSServerGroup; onSelectZone: (z: DNSZone) => void }) {
  const [tab, setTab] = useState<GroupTab>("zones");

  const tabs: { id: GroupTab; label: string; icon: React.ElementType }[] = [
    { id: "zones",   label: "Zones",   icon: FileText },
    { id: "servers", label: "Servers", icon: Cpu },
    { id: "views",   label: "Views",   icon: Eye },
    { id: "acls",    label: "ACLs",    icon: Shield },
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
        <div className="flex items-center gap-2">
          <Globe className="h-4 w-4 text-muted-foreground" />
          <h2 className="font-semibold text-base">{group.name}</h2>
          <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${typeBadge[group.group_type] ?? "bg-muted text-muted-foreground"}`}>{group.group_type}</span>
          {group.is_recursive && <span className="inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium bg-emerald-500/15 text-emerald-600">recursive</span>}
        </div>
        {group.description && <p className="text-xs text-muted-foreground mt-0.5">{group.description}</p>}
      </div>

      <div className="flex border-b">
        {tabs.map((t) => (
          <button key={t.id} onClick={() => setTab(t.id)}
            className={`flex items-center gap-1.5 px-4 py-2.5 text-xs font-medium border-b-2 transition-colors ${tab === t.id ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"}`}
          >
            <t.icon className="h-3.5 w-3.5" />{t.label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-auto p-5">
        {tab === "zones"   && <ZonesTab group={group} onSelectZone={onSelectZone} />}
        {tab === "servers" && <ServersTab group={group} />}
        {tab === "views"   && <ViewsTab group={group} />}
        {tab === "acls"    && <AclsTab groupId={group.id} />}
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
  const [expandedTlds, setExpandedTlds] = useState<Set<string>>(new Set());

  const { data: zones = [] } = useQuery({
    queryKey: ["dns-zones", groupId],
    queryFn: () => dnsApi.listZones(groupId),
  });

  const tree = buildTldTree(zones);

  if (tree.length === 0) return (
    <p className="px-3 py-1.5 text-xs text-muted-foreground italic">No zones</p>
  );

  function toggleTld(tld: string) {
    setExpandedTlds((prev) => {
      const next = new Set(prev);
      next.has(tld) ? next.delete(tld) : next.add(tld);
      return next;
    });
  }

  return (
    <div>
      {tree.map(({ tld, zones: tzones }) => {
        const expanded = expandedTlds.has(tld);
        return (
          <div key={tld}>
            {/* TLD row */}
            <button
              className="flex w-full items-center gap-1.5 rounded px-3 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
              onClick={() => toggleTld(tld)}
            >
              {expanded
                ? <FolderOpen className="h-3 w-3 flex-shrink-0" />
                : <Folder className="h-3 w-3 flex-shrink-0" />}
              <span className="font-medium">{tld}</span>
              <span className="ml-auto text-muted-foreground/50">{tzones.length}</span>
            </button>
            {/* Zone rows */}
            {expanded && tzones.map((z) => (
              <button
                key={z.id}
                className={`flex w-full items-center gap-1.5 rounded py-1 pl-7 pr-2 text-xs ${
                  selectedZoneId === z.id
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-foreground"
                }`}
                onClick={() => onSelectZone(z)}
              >
                <FileText className="h-3 w-3 flex-shrink-0" />
                <span className="font-mono truncate">{z.name}</span>
                {z.dnssec_enabled && <Shield className="h-2.5 w-2.5 ml-auto flex-shrink-0 text-emerald-500" />}
              </button>
            ))}
          </div>
        );
      })}
    </div>
  );
}

// ── Main DNS Page ─────────────────────────────────────────────────────────────

type Selection =
  | { type: "group"; group: DNSServerGroup }
  | { type: "zone"; group: DNSServerGroup; zone: DNSZone };

export function DNSPage() {
  const qc = useQueryClient();
  const location = useLocation();
  const [selection, setSelection] = useState<Selection | null>(null);
  const [showCreateGroup, setShowCreateGroup] = useState(false);
  const [editGroup, setEditGroup] = useState<DNSServerGroup | null>(null);
  const [confirmDeleteGroup, setConfirmDeleteGroup] = useState<DNSServerGroup | null>(null);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());

  const { data: groups = [], isLoading } = useQuery({
    queryKey: ["dns-groups"],
    queryFn: () => dnsApi.listGroups(),
  });

  // Deep-link from global search: navigate("/dns", { state: { selectGroup, selectZone } })
  useEffect(() => {
    const state = location.state as { selectGroup?: string; selectZone?: string } | null;
    if (!state?.selectGroup || groups.length === 0) return;
    const group = groups.find((g: DNSServerGroup) => g.id === state.selectGroup);
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
  }, [location.state, groups]);

  const deleteGroup = useMutation({
    mutationFn: (id: string) => dnsApi.deleteGroup(id),
    onSuccess: (_, id) => {
      qc.invalidateQueries({ queryKey: ["dns-groups"] });
      if (selection && "group" in selection && selection.group.id === id) setSelection(null);
      setConfirmDeleteGroup(null);
    },
  });

  function toggleGroup(id: string) {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
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
          <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">DNS Server Groups</span>
          <button className="flex h-6 w-6 items-center justify-center rounded hover:bg-accent" onClick={() => setShowCreateGroup(true)}>
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto py-1">
          {isLoading && <p className="px-4 py-2 text-xs text-muted-foreground">Loading…</p>}
          {groups.length === 0 && !isLoading && (
            <div className="px-4 pt-6 text-center">
              <Globe className="h-8 w-8 text-muted-foreground/30 mx-auto mb-2" />
              <p className="text-xs text-muted-foreground mb-3">No server groups yet.</p>
              <button className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs mx-auto hover:bg-accent" onClick={() => setShowCreateGroup(true)}>
                <Plus className="h-3 w-3" /> Create Group
              </button>
            </div>
          )}

          {groups.map((g) => {
            const expanded = expandedGroups.has(g.id);
            const groupSelected = selection?.type === "group" && selection.group.id === g.id;
            const zoneInGroup = selection?.type === "zone" && selection.group.id === g.id;

            return (
              <div key={g.id}>
                {/* Group row */}
                <div className={`flex items-center rounded-md mx-1 ${groupSelected ? "bg-primary text-primary-foreground" : ""}`}>
                  {/* Expand toggle */}
                  <button
                    className={`flex h-7 w-6 items-center justify-center flex-shrink-0 ${groupSelected ? "text-primary-foreground" : "text-muted-foreground hover:text-foreground"}`}
                    onClick={() => toggleGroup(g.id)}
                  >
                    {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                  </button>
                  {/* Group name — click to select */}
                  <button
                    className="flex flex-1 items-center gap-2 py-1.5 pr-1 min-w-0"
                    onClick={() => { setSelection({ type: "group", group: g }); if (!expanded) toggleGroup(g.id); }}
                  >
                    <span className={`h-2 w-2 rounded-full flex-shrink-0 ${groupTypeDot[g.group_type] ?? "bg-muted-foreground"}`} />
                    <span className="text-sm font-medium truncate">{g.name}</span>
                  </button>
                  {/* Edit / delete */}
                  <div className="flex items-center gap-0.5 px-1 opacity-0 group-hover:opacity-100 flex-shrink-0" style={{ opacity: groupSelected ? 1 : undefined }}>
                    <button
                      className={`h-5 w-5 flex items-center justify-center rounded ${groupSelected ? "hover:bg-primary-foreground/20 text-primary-foreground" : "hover:bg-accent text-muted-foreground"}`}
                      onClick={(e) => { e.stopPropagation(); setEditGroup(g); }}
                    >
                      <Pencil className="h-3 w-3" />
                    </button>
                    <button
                      className={`h-5 w-5 flex items-center justify-center rounded ${groupSelected ? "hover:bg-primary-foreground/20 text-primary-foreground" : "hover:bg-accent text-muted-foreground"}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        setConfirmDeleteGroup(g);
                      }}
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                </div>

                {/* Zone tree (when group expanded) */}
                {(expanded || zoneInGroup) && (
                  <div className="ml-4 mb-1">
                    <ZoneTreeRows
                      groupId={g.id}
                      selectedZoneId={selection?.type === "zone" ? selection.zone.id : null}
                      onSelectZone={(z) => setSelection({ type: "zone", group: g, zone: z })}
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
            onSelectZone={(z) => setSelection({ type: "zone", group: selection.group, zone: z })}
          />
        )}
        {selection?.type === "zone"  && (
          <ZoneDetailView
            group={selection.group}
            zone={selection.zone}
            onDeleted={() => setSelection({ type: "group", group: selection.group })}
          />
        )}
      </div>

      {showCreateGroup && <GroupModal onClose={() => setShowCreateGroup(false)} />}
      {editGroup       && <GroupModal group={editGroup} onClose={() => setEditGroup(null)} />}
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
