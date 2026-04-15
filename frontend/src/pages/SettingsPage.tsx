import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { settingsApi, authApi, type PlatformSettings } from "@/lib/api";
import { Save, Search } from "lucide-react";
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

// Alphabetically sorted by `title`.
const SECTIONS: SectionDef[] = [
  {
    id: "branding",
    title: "Branding",
    description: "Application title and visual identity.",
    keywords: ["title", "name", "logo", "header"],
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
              <button
                onClick={handleSave}
                disabled={!dirty || mutation.isPending}
                className="flex flex-shrink-0 items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
              >
                <Save className="h-4 w-4" />
                {saved ? "Saved!" : mutation.isPending ? "Saving…" : "Save"}
              </button>
            )}
          </div>

          {mutation.isError && (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-2 text-sm text-destructive">
              Failed to save settings. Please try again.
            </div>
          )}

          <div className="rounded-lg border bg-card divide-y px-5">
            {activeId === "branding" && (
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
