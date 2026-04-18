import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { settingsApi, authApi, type PlatformSettings } from "@/lib/api";
import { ArrowRight, ArrowLeftRight, RotateCcw, Save, Search } from "lucide-react";
import { cn } from "@/lib/utils";

function Field({
  label,
  description,
  children,
}: {
  label: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-8 py-3">
      <div>
        <div className="text-sm font-medium">{label}</div>
        {description && (
          <div className="text-xs text-muted-foreground">{description}</div>
        )}
      </div>
      <div className="flex-shrink-0">{children}</div>
    </div>
  );
}

function Toggle({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      onClick={() => !disabled && onChange(!checked)}
      disabled={disabled}
      className={cn(
        "relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none disabled:opacity-60",
        checked ? "bg-primary" : "bg-muted-foreground/30",
      )}
    >
      <span
        className={cn(
          "pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform",
          checked ? "translate-x-4" : "translate-x-0",
        )}
      />
    </button>
  );
}

type SectionId =
  | "branding"
  | "discovery"
  | "dns"
  | "dns-auto-sync"
  | "dns-pull-from-server"
  | "dhcp"
  | "dhcp-lease-sync"
  | "ip-allocation"
  | "session"
  | "subnet-tree"
  | "updates"
  | "utilization";

interface SectionDef {
  id: SectionId;
  title: string;
  description: string;
  /** Search keywords (lowercased) so the section sidebar filter can match deeper terms. */
  keywords: string[];
}

// Which PlatformSettings keys each section owns — drives the per-section
// "Reset to defaults" button so it only overwrites that section's fields.
const SECTION_FIELDS: Record<SectionId, (keyof PlatformSettings)[]> = {
  branding: ["app_title", "app_base_url"],
  discovery: ["discovery_scan_enabled", "discovery_scan_interval_minutes"],
  dns: [
    "dns_default_ttl",
    "dns_default_zone_type",
    "dns_default_dnssec_validation",
    "dns_recursive_by_default",
  ],
  "dns-auto-sync": [
    "dns_auto_sync_enabled",
    "dns_auto_sync_interval_minutes",
    "dns_auto_sync_delete_stale",
  ],
  "dns-pull-from-server": [
    "dns_pull_from_server_enabled",
    "dns_pull_from_server_interval_minutes",
  ],
  dhcp: [
    "dhcp_default_dns_servers",
    "dhcp_default_domain_name",
    "dhcp_default_domain_search",
    "dhcp_default_ntp_servers",
    "dhcp_default_lease_time",
  ],
  "dhcp-lease-sync": [
    "dhcp_pull_leases_enabled",
    "dhcp_pull_leases_interval_minutes",
  ],
  "ip-allocation": ["ip_allocation_strategy"],
  session: ["session_timeout_minutes", "auto_logout_minutes"],
  "subnet-tree": ["subnet_tree_default_expanded_depth"],
  updates: ["github_release_check_enabled"],
  utilization: [
    "utilization_warn_threshold",
    "utilization_critical_threshold",
  ],
};

/** Three-tier horizontal flow with one arrow highlighted to indicate which
 *  boundary this particular reconciliation job crosses. */
function LayerDiagram({
  highlight,
}: {
  highlight: "ipam-to-db" | "db-to-server";
}) {
  const pill =
    "rounded-md border bg-background px-3 py-1.5 text-xs font-medium whitespace-nowrap";
  const arrowIdle = "h-4 w-4 text-muted-foreground/50";
  const arrowActive = "h-4 w-4 text-primary";
  return (
    <div className="flex items-center gap-2 rounded-md border border-dashed bg-muted/30 px-3 py-2 text-xs">
      <span className={pill}>IPAM</span>
      <ArrowRight
        className={highlight === "ipam-to-db" ? arrowActive : arrowIdle}
      />
      <span className={pill}>SpatiumDDI DNS</span>
      <ArrowLeftRight
        className={highlight === "db-to-server" ? arrowActive : arrowIdle}
      />
      <span className={pill}>Windows / BIND9</span>
    </div>
  );
}

// Alphabetically sorted by `title`.
const SECTIONS: SectionDef[] = [
  {
    id: "branding",
    title: "Branding & URL",
    description: "Application title, external URL, and visual identity.",
    keywords: [
      "title",
      "name",
      "logo",
      "header",
      "url",
      "base",
      "saml",
      "oidc",
    ],
  },
  {
    id: "discovery",
    title: "Discovery",
    description: "Periodic ping/scan jobs to detect active hosts.",
    keywords: ["scan", "ping", "interval", "discover"],
  },
  {
    id: "dns",
    title: "DNS Defaults",
    description: "Default values applied to new zones and server groups.",
    keywords: ["zone", "ttl", "dnssec", "recursion", "agent", "key"],
  },
  {
    id: "dns-auto-sync",
    title: "IPAM → DNS Reconciliation",
    description:
      "Catches drift between IPAM's expected records (hostname + IP) and SpatiumDDI's DNS DB. Fills in missing A/AAAA/PTR when the live sync missed one — e.g. bulk imports or a previously failed push.",
    keywords: [
      "dns",
      "ipam",
      "sync",
      "reconcile",
      "drift",
      "auto",
      "job",
      "auto-sync",
    ],
  },
  {
    id: "dns-pull-from-server",
    title: "Zone ↔ Server Reconciliation",
    description:
      "Catches drift between SpatiumDDI's DNS DB and the authoritative server's wire (Windows DNS today). AXFR imports out-of-band edits; any DB-only records get pushed back via RFC 2136. Additive only — never deletes.",
    keywords: [
      "dns",
      "server",
      "sync",
      "pull",
      "push",
      "axfr",
      "rfc 2136",
      "windows",
      "import",
      "export",
      "additive",
      "bidirectional",
      "auto",
    ],
  },
  {
    id: "dhcp",
    title: "DHCP Defaults",
    description:
      "Default DNS servers, domain, NTP, and lease time pre-filled when creating a new scope.",
    keywords: ["dns", "domain", "search", "ntp", "lease", "option 42", "scope"],
  },
  {
    id: "dhcp-lease-sync",
    title: "DHCP Lease Sync",
    description:
      "Poll agentless DHCP servers (Windows DHCP today) for active leases and mirror them into DHCP + IPAM. Additive only; expiry is handled by the existing lease cleanup sweep.",
    keywords: [
      "dhcp",
      "lease",
      "windows",
      "winrm",
      "poll",
      "ipam",
      "mirror",
      "additive",
      "auto",
    ],
  },
  {
    id: "ip-allocation",
    title: "IP Allocation",
    description: "How the next IP is chosen during auto-allocation.",
    keywords: ["sequential", "random", "next ip", "strategy"],
  },
  {
    id: "session",
    title: "Session & Security",
    description: "Login session lifetime and idle behavior.",
    keywords: ["timeout", "logout", "expiry", "auth"],
  },
  {
    id: "subnet-tree",
    title: "Subnet Tree UI",
    description: "Tree view defaults in the IPAM browser.",
    keywords: ["expand", "collapse", "depth", "tree"],
  },
  {
    id: "updates",
    title: "Updates",
    description: "Release-check behavior.",
    keywords: ["github", "release", "version", "check"],
  },
  {
    id: "utilization",
    title: "Utilization Thresholds",
    description: "Warning and critical percentages for utilization indicators.",
    keywords: ["warn", "critical", "threshold", "color"],
  },
];

export function SettingsPage() {
  const qc = useQueryClient();
  const { data: me } = useQuery({
    queryKey: ["me"],
    queryFn: authApi.me,
    staleTime: 60_000,
  });
  const isSuperadmin = me?.is_superadmin ?? false;

  const { data, isLoading } = useQuery({
    queryKey: ["settings"],
    queryFn: settingsApi.get,
  });

  const { data: defaults } = useQuery({
    queryKey: ["settings-defaults"],
    queryFn: settingsApi.getDefaults,
    staleTime: Infinity,
  });

  const [form, setForm] = useState<Partial<PlatformSettings>>({});
  const [saved, setSaved] = useState(false);
  const [activeId, setActiveId] = useState<SectionId>("branding");
  const [search, setSearch] = useState("");

  const values: PlatformSettings = {
    ...(data ?? ({} as PlatformSettings)),
    ...form,
  };

  const mutation = useMutation({
    mutationFn: (patch: Partial<PlatformSettings>) => settingsApi.update(patch),
    onSuccess: (updated) => {
      qc.setQueryData(["settings"], updated);
      setForm({});
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    },
  });

  function set<K extends keyof PlatformSettings>(
    key: K,
    value: PlatformSettings[K],
  ) {
    setForm((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
  }

  function handleSave() {
    if (Object.keys(form).length > 0) mutation.mutate(form);
  }

  function handleReset() {
    if (!defaults) return;
    const keys = SECTION_FIELDS[activeId];
    const patch: Partial<PlatformSettings> = {};
    for (const k of keys) {
      if (k in defaults) {
        // Copy arrays so the form holds its own reference.
        const v = defaults[k];
        (patch as Record<string, unknown>)[k] = Array.isArray(v) ? [...v] : v;
      }
    }
    setForm((prev) => ({ ...prev, ...patch }));
    setSaved(false);
  }

  function sectionIsDefault(): boolean {
    if (!defaults) return true;
    for (const k of SECTION_FIELDS[activeId]) {
      if (!(k in defaults)) continue;
      const current = values[k];
      const def = defaults[k];
      if (Array.isArray(def) && Array.isArray(current)) {
        if (
          def.length !== current.length ||
          def.some((v, i) => v !== current[i])
        )
          return false;
      } else if (current !== def) {
        return false;
      }
    }
    return true;
  }

  if (isLoading) {
    return (
      <div className="p-8 text-sm text-muted-foreground">Loading settings…</div>
    );
  }

  const dirty = Object.keys(form).length > 0;
  const q = search.trim().toLowerCase();
  const filteredSections = q
    ? SECTIONS.filter(
        (s) =>
          s.title.toLowerCase().includes(q) ||
          s.description.toLowerCase().includes(q) ||
          s.keywords.some((k) => k.includes(q)),
      )
    : SECTIONS;

  const active = SECTIONS.find((s) => s.id === activeId)!;
  const inputCls =
    "rounded-md border bg-background px-3 py-1.5 text-sm disabled:opacity-60";

  return (
    <div className="flex h-full overflow-hidden">
      {/* ── Sidebar ── */}
      <aside className="w-64 flex-shrink-0 border-r bg-card overflow-y-auto">
        <div className="border-b px-4 py-3">
          <h1 className="text-sm font-semibold">Settings</h1>
          <p className="text-xs text-muted-foreground">
            {isSuperadmin
              ? "Configure SpatiumDDI."
              : "View-only — superadmin required to edit."}
          </p>
        </div>
        <div className="border-b p-3">
          <div className="relative">
            <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter…"
              className="w-full rounded-md border bg-background pl-7 pr-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
        </div>
        <nav className="p-2">
          {filteredSections.length === 0 && (
            <p className="px-2 py-3 text-xs text-muted-foreground italic">
              No sections match.
            </p>
          )}
          {filteredSections.map((s) => (
            <button
              key={s.id}
              onClick={() => setActiveId(s.id)}
              className={cn(
                "block w-full rounded-md px-3 py-1.5 text-left text-sm hover:bg-accent",
                activeId === s.id && "bg-accent font-medium",
              )}
            >
              {s.title}
            </button>
          ))}
        </nav>
      </aside>

      {/* ── Main pane ── */}
      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-2xl space-y-5 p-8">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h2 className="text-lg font-semibold">{active.title}</h2>
              <p className="text-sm text-muted-foreground">
                {active.description}
              </p>
            </div>
            {isSuperadmin && (
              <div className="flex flex-shrink-0 items-center gap-2">
                <button
                  onClick={handleReset}
                  disabled={!defaults || sectionIsDefault()}
                  title="Populate this section's fields with their default values — still requires Save to apply."
                  className="flex items-center gap-1.5 rounded-md border px-3 py-2 text-xs font-medium hover:bg-accent disabled:opacity-40"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                  Reset to defaults
                </button>
                <button
                  onClick={handleSave}
                  disabled={!dirty || mutation.isPending}
                  className="flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
                >
                  <Save className="h-4 w-4" />
                  {saved ? "Saved!" : mutation.isPending ? "Saving…" : "Save"}
                </button>
              </div>
            )}
          </div>

          {activeId === "dns-auto-sync" && (
            <LayerDiagram highlight="ipam-to-db" />
          )}
          {activeId === "dns-pull-from-server" && (
            <LayerDiagram highlight="db-to-server" />
          )}

          {mutation.isError && (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-2 text-sm text-destructive">
              Failed to save settings. Please try again.
            </div>
          )}

          <div className="rounded-lg border bg-card divide-y px-5">
            {activeId === "branding" && (
              <>
                <Field
                  label="Application Title"
                  description="Shown in the browser tab and header."
                >
                  <input
                    value={values.app_title ?? ""}
                    onChange={(e) => set("app_title", e.target.value)}
                    disabled={!isSuperadmin}
                    className={cn(inputCls, "w-48")}
                  />
                </Field>
                <Field
                  label="External URL"
                  description="Public-facing URL (no trailing slash) used to build OIDC/SAML redirect + callback URLs. Leave blank to derive from the incoming request."
                >
                  <input
                    value={values.app_base_url ?? ""}
                    onChange={(e) => set("app_base_url", e.target.value)}
                    placeholder="https://ddi.example.com"
                    disabled={!isSuperadmin}
                    className={cn(inputCls, "w-72")}
                  />
                </Field>
              </>
            )}

            {activeId === "discovery" && (
              <>
                <Field
                  label="Enable Discovery Scans"
                  description="Periodically ping subnets to detect active hosts."
                >
                  <Toggle
                    checked={!!values.discovery_scan_enabled}
                    onChange={(v) => set("discovery_scan_enabled", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                <Field
                  label="Scan Interval"
                  description="How often to run discovery scans."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={1}
                      value={values.discovery_scan_interval_minutes ?? 60}
                      onChange={(e) =>
                        set(
                          "discovery_scan_interval_minutes",
                          Number(e.target.value),
                        )
                      }
                      disabled={!isSuperadmin || !values.discovery_scan_enabled}
                      className={cn(inputCls, "w-24")}
                    />
                    <span className="text-xs text-muted-foreground">min</span>
                  </div>
                </Field>
              </>
            )}

            {activeId === "dns" && (
              <>
                <Field
                  label="Default Zone TTL"
                  description="Default TTL (seconds) applied to new zones."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={60}
                      value={values.dns_default_ttl ?? 3600}
                      onChange={(e) =>
                        set("dns_default_ttl", Number(e.target.value))
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-28")}
                    />
                    <span className="text-xs text-muted-foreground">sec</span>
                  </div>
                </Field>
                <Field
                  label="Default Zone Type"
                  description="Pre-selected zone type when creating a new zone."
                >
                  <select
                    value={values.dns_default_zone_type ?? "primary"}
                    onChange={(e) =>
                      set("dns_default_zone_type", e.target.value)
                    }
                    disabled={!isSuperadmin}
                    className={inputCls}
                  >
                    <option value="primary">Primary</option>
                    <option value="secondary">Secondary</option>
                    <option value="stub">Stub</option>
                    <option value="forward">Forward</option>
                  </select>
                </Field>
                <Field
                  label="Default DNSSEC Validation"
                  description="Default DNSSEC validation mode for new server groups."
                >
                  <select
                    value={values.dns_default_dnssec_validation ?? "auto"}
                    onChange={(e) =>
                      set("dns_default_dnssec_validation", e.target.value)
                    }
                    disabled={!isSuperadmin}
                    className={inputCls}
                  >
                    <option value="auto">auto (recommended)</option>
                    <option value="yes">yes — manual trust anchors</option>
                    <option value="no">no — disabled</option>
                  </select>
                </Field>
                <Field
                  label="Recursive by Default"
                  description="Enable recursion when creating new server groups."
                >
                  <Toggle
                    checked={!!values.dns_recursive_by_default}
                    onChange={(v) => set("dns_recursive_by_default", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                <Field
                  label="DNS Agent Key"
                  description="Pre-shared key for DNS agent container auto-registration. Set DNS_AGENT_KEY env var on both the control plane and agent containers."
                >
                  <span className="rounded bg-muted px-2 py-1 text-xs font-mono text-muted-foreground">
                    configured via DNS_AGENT_KEY env var
                  </span>
                </Field>
              </>
            )}

            {activeId === "dns-auto-sync" && (
              <>
                <Field
                  label="Enable Auto-Sync"
                  description="Periodically reconcile IPAM-expected DNS records against what actually exists. Creates missing A/AAAA/PTR records and updates mismatched ones."
                >
                  <Toggle
                    checked={!!values.dns_auto_sync_enabled}
                    onChange={(v) => set("dns_auto_sync_enabled", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                <Field
                  label="Sync Interval"
                  description="How often the auto-sync job runs."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={1}
                      value={values.dns_auto_sync_interval_minutes ?? 5}
                      onChange={(e) =>
                        set(
                          "dns_auto_sync_interval_minutes",
                          Number(e.target.value),
                        )
                      }
                      disabled={!isSuperadmin || !values.dns_auto_sync_enabled}
                      className={cn(inputCls, "w-24")}
                    />
                    <span className="text-xs text-muted-foreground">min</span>
                  </div>
                </Field>
                <Field
                  label="Delete Stale Records"
                  description="Also remove auto-generated DNS records whose IP has been deleted. Off by default — stale records are conservative to keep while you confirm the drift report."
                >
                  <Toggle
                    checked={!!values.dns_auto_sync_delete_stale}
                    onChange={(v) => set("dns_auto_sync_delete_stale", v)}
                    disabled={!isSuperadmin || !values.dns_auto_sync_enabled}
                  />
                </Field>
                <Field
                  label="Last Run"
                  description="Timestamp of the most recent auto-sync pass."
                >
                  <span className="rounded bg-muted px-2 py-1 text-xs font-mono text-muted-foreground">
                    {values.dns_auto_sync_last_run_at
                      ? new Date(
                          values.dns_auto_sync_last_run_at,
                        ).toLocaleString()
                      : "never"}
                  </span>
                </Field>
              </>
            )}

            {activeId === "dns-pull-from-server" && (
              <>
                <Field
                  label="Enable Server Sync"
                  description="Periodically reconcile each zone with its primary authoritative server in both directions. AXFR imports records on the wire but not in SpatiumDDI; any DB record not on the wire is pushed back via RFC 2136. Additive only — never deletes on either side. Only fires against drivers that support AXFR pull (Windows DNS today)."
                >
                  <Toggle
                    checked={!!values.dns_pull_from_server_enabled}
                    onChange={(v) => set("dns_pull_from_server_enabled", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                <Field
                  label="Sync Interval"
                  description="How often the sync job runs. AXFR + potential RFC 2136 updates are heavier than a simple API poll — 30 minutes is a reasonable default for a lab; lower it if you expect frequent out-of-band edits in Windows DNS Manager."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={1}
                      value={values.dns_pull_from_server_interval_minutes ?? 30}
                      onChange={(e) =>
                        set(
                          "dns_pull_from_server_interval_minutes",
                          Number(e.target.value),
                        )
                      }
                      disabled={
                        !isSuperadmin || !values.dns_pull_from_server_enabled
                      }
                      className={cn(inputCls, "w-24")}
                    />
                    <span className="text-xs text-muted-foreground">min</span>
                  </div>
                </Field>
                <Field
                  label="Last Run"
                  description="Timestamp of the most recent pull pass."
                >
                  <span className="rounded bg-muted px-2 py-1 text-xs font-mono text-muted-foreground">
                    {values.dns_pull_from_server_last_run_at
                      ? new Date(
                          values.dns_pull_from_server_last_run_at,
                        ).toLocaleString()
                      : "never"}
                  </span>
                </Field>
              </>
            )}

            {activeId === "dhcp-lease-sync" && (
              <>
                <Field
                  label="Enable Lease Sync"
                  description="Periodically poll agentless DHCP servers (Windows DHCP today) and upsert their active leases into SpatiumDDI. Each lease also mirrors into IPAM as an auto-from-lease row when the IP lives in a known subnet. Expiry is handled by the existing lease-cleanup sweep, not here."
                >
                  <Toggle
                    checked={!!values.dhcp_pull_leases_enabled}
                    onChange={(v) => set("dhcp_pull_leases_enabled", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                <Field
                  label="Poll Interval"
                  description="How often the sync job polls each agentless server. WinRM round-trips are cheap; 5 minutes is a reasonable default. Lower it if your lease TTLs are short and you want fresher IPAM mirroring."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={1}
                      value={values.dhcp_pull_leases_interval_minutes ?? 5}
                      onChange={(e) =>
                        set(
                          "dhcp_pull_leases_interval_minutes",
                          Number(e.target.value),
                        )
                      }
                      disabled={
                        !isSuperadmin || !values.dhcp_pull_leases_enabled
                      }
                      className={cn(inputCls, "w-24")}
                    />
                    <span className="text-xs text-muted-foreground">min</span>
                  </div>
                </Field>
                <Field
                  label="Last Run"
                  description="Timestamp of the most recent poll pass."
                >
                  <span className="rounded bg-muted px-2 py-1 text-xs font-mono text-muted-foreground">
                    {values.dhcp_pull_leases_last_run_at
                      ? new Date(
                          values.dhcp_pull_leases_last_run_at,
                        ).toLocaleString()
                      : "never"}
                  </span>
                </Field>
              </>
            )}

            {activeId === "dhcp" && (
              <>
                <Field
                  label="Default DNS Servers"
                  description="Comma-separated IPs. Pre-filled as option 6 on new scopes."
                >
                  <input
                    type="text"
                    value={(values.dhcp_default_dns_servers ?? []).join(", ")}
                    onChange={(e) =>
                      set(
                        "dhcp_default_dns_servers",
                        e.target.value
                          .split(",")
                          .map((s) => s.trim())
                          .filter(Boolean),
                      )
                    }
                    placeholder="10.0.0.53, 10.0.0.54"
                    disabled={!isSuperadmin}
                    className={inputCls}
                  />
                </Field>
                <Field
                  label="Default Domain Name"
                  description="DHCP option 15 — the DNS domain clients should append."
                >
                  <input
                    type="text"
                    value={values.dhcp_default_domain_name ?? ""}
                    onChange={(e) =>
                      set("dhcp_default_domain_name", e.target.value)
                    }
                    placeholder="corp.example.com"
                    disabled={!isSuperadmin}
                    className={inputCls}
                  />
                </Field>
                <Field
                  label="Default Domain Search List"
                  description="DHCP option 119 — comma-separated search suffixes."
                >
                  <input
                    type="text"
                    value={(values.dhcp_default_domain_search ?? []).join(", ")}
                    onChange={(e) =>
                      set(
                        "dhcp_default_domain_search",
                        e.target.value
                          .split(",")
                          .map((s) => s.trim())
                          .filter(Boolean),
                      )
                    }
                    placeholder="corp.example.com, example.com"
                    disabled={!isSuperadmin}
                    className={inputCls}
                  />
                </Field>
                <Field
                  label="Default NTP Servers"
                  description="DHCP option 42 — comma-separated NTP server IPs."
                >
                  <input
                    type="text"
                    value={(values.dhcp_default_ntp_servers ?? []).join(", ")}
                    onChange={(e) =>
                      set(
                        "dhcp_default_ntp_servers",
                        e.target.value
                          .split(",")
                          .map((s) => s.trim())
                          .filter(Boolean),
                      )
                    }
                    placeholder="10.0.0.10, 10.0.0.11"
                    disabled={!isSuperadmin}
                    className={inputCls}
                  />
                </Field>
                <Field
                  label="Default Lease Time"
                  description="Initial lease time (seconds) for new scopes."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={60}
                      value={values.dhcp_default_lease_time ?? 86400}
                      onChange={(e) =>
                        set("dhcp_default_lease_time", Number(e.target.value))
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-32")}
                    />
                    <span className="text-xs text-muted-foreground">sec</span>
                  </div>
                </Field>
              </>
            )}

            {activeId === "ip-allocation" && (
              <Field
                label="Allocation Strategy"
                description="Strategy used when auto-allocating the next IP."
              >
                <select
                  value={values.ip_allocation_strategy ?? "sequential"}
                  onChange={(e) =>
                    set("ip_allocation_strategy", e.target.value)
                  }
                  disabled={!isSuperadmin}
                  className={inputCls}
                >
                  <option value="sequential">Sequential</option>
                  <option value="random">Random</option>
                </select>
              </Field>
            )}

            {activeId === "session" && (
              <>
                <Field
                  label="Session Timeout"
                  description="Minutes of inactivity before session expires (0 = disabled)."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={0}
                      value={values.session_timeout_minutes ?? 60}
                      onChange={(e) =>
                        set("session_timeout_minutes", Number(e.target.value))
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-24")}
                    />
                    <span className="text-xs text-muted-foreground">min</span>
                  </div>
                </Field>
                <Field
                  label="Auto-Logout Warning"
                  description="Minutes before expiry to show a logout warning (0 = disabled)."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={0}
                      value={values.auto_logout_minutes ?? 0}
                      onChange={(e) =>
                        set("auto_logout_minutes", Number(e.target.value))
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-24")}
                    />
                    <span className="text-xs text-muted-foreground">min</span>
                  </div>
                </Field>
              </>
            )}

            {activeId === "subnet-tree" && (
              <Field
                label="Default Expanded Depth"
                description="How many levels of the tree are expanded by default."
              >
                <input
                  type="number"
                  min={0}
                  max={10}
                  value={values.subnet_tree_default_expanded_depth ?? 2}
                  onChange={(e) =>
                    set(
                      "subnet_tree_default_expanded_depth",
                      Number(e.target.value),
                    )
                  }
                  disabled={!isSuperadmin}
                  className={cn(inputCls, "w-20")}
                />
              </Field>
            )}

            {activeId === "updates" && (
              <Field
                label="Check for GitHub Releases"
                description="Periodically check GitHub for new SpatiumDDI releases."
              >
                <Toggle
                  checked={!!values.github_release_check_enabled}
                  onChange={(v) => set("github_release_check_enabled", v)}
                  disabled={!isSuperadmin}
                />
              </Field>
            )}

            {activeId === "utilization" && (
              <>
                <Field
                  label="Warning Threshold"
                  description="Utilization percentage to show amber warning."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={0}
                      max={100}
                      value={values.utilization_warn_threshold ?? 80}
                      onChange={(e) =>
                        set(
                          "utilization_warn_threshold",
                          Number(e.target.value),
                        )
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-20")}
                    />
                    <span className="text-xs text-muted-foreground">%</span>
                  </div>
                </Field>
                <Field
                  label="Critical Threshold"
                  description="Utilization percentage to show red critical indicator."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={0}
                      max={100}
                      value={values.utilization_critical_threshold ?? 95}
                      onChange={(e) =>
                        set(
                          "utilization_critical_threshold",
                          Number(e.target.value),
                        )
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-20")}
                    />
                    <span className="text-xs text-muted-foreground">%</span>
                  </div>
                </Field>
              </>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
