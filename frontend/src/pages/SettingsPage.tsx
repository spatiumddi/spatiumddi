import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  settingsApi,
  authApi,
  type OUITaskStatus,
  type PlatformSettings,
} from "@/lib/api";
import {
  AlertCircle,
  ArrowRight,
  ArrowLeftRight,
  CheckCircle2,
  Loader2,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Modal } from "@/components/ui/modal";
import { AuditForwardTargets } from "@/components/AuditForwardTargets";

const OUI_SOURCE_URL = "https://standards-oui.ieee.org/oui/oui.csv";

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
  | "dns"
  | "dns-auto-sync"
  | "dns-pull-from-server"
  | "dhcp"
  | "dhcp-lease-sync"
  | "audit-forward"
  | "integrations-kubernetes"
  | "integrations-docker"
  | "integrations-proxmox"
  | "integrations-tailscale"
  | "ip-allocation"
  | "oui-lookup"
  | "device-profiling"
  | "network-asn"
  | "network-domains"
  | "network-vrf"
  | "ai-digest"
  | "password-policy"
  | "session"
  | "subnet-tree"
  | "updates"
  | "utilization";

type SectionGroup =
  | "Application"
  | "Security"
  | "IPAM"
  | "DNS"
  | "DHCP"
  | "Network"
  | "Integrations"
  | "AI";

interface SectionDef {
  id: SectionId;
  title: string;
  description: string;
  /** Sidebar grouping — separator + label rendered between groups. */
  group: SectionGroup;
  /** Search keywords (lowercased) so the section sidebar filter can match deeper terms. */
  keywords: string[];
}

// Group display order. Within each group, entries render in the order
// they appear in SECTIONS below (alphabetical by title).
const GROUP_ORDER: SectionGroup[] = [
  "Application",
  "Security",
  "IPAM",
  "DNS",
  "DHCP",
  "Network",
  "Integrations",
];

// Which PlatformSettings keys each section owns — drives the per-section
// "Reset to defaults" button so it only overwrites that section's fields.
const SECTION_FIELDS: Record<SectionId, (keyof PlatformSettings)[]> = {
  branding: ["app_title", "app_base_url"],
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
    "dhcp_pull_leases_interval_seconds",
  ],
  // Managed through the dedicated AuditForwardTargets component, not
  // the flat settings form. Legacy flat columns stay on
  // PlatformSettings for one release as a fallback (see audit_forward
  // service); they're intentionally not listed here so the singleton
  // Save button doesn't hit them.
  "audit-forward": [],
  "integrations-kubernetes": ["integration_kubernetes_enabled"],
  "integrations-docker": ["integration_docker_enabled"],
  "integrations-proxmox": ["integration_proxmox_enabled"],
  "integrations-tailscale": ["integration_tailscale_enabled"],
  "ip-allocation": ["ip_allocation_strategy"],
  "oui-lookup": ["oui_lookup_enabled", "oui_update_interval_hours"],
  "device-profiling": ["fingerbank_api_key"],
  "network-asn": [
    "asn_whois_interval_hours",
    "rpki_roa_source",
    "rpki_roa_refresh_interval_hours",
  ],
  "network-domains": ["domain_whois_interval_hours"],
  "network-vrf": ["vrf_strict_rd_validation"],
  "ai-digest": ["ai_daily_digest_enabled"],
  "password-policy": [
    "password_min_length",
    "password_require_uppercase",
    "password_require_lowercase",
    "password_require_digit",
    "password_require_symbol",
    "password_history_count",
    "password_max_age_days",
  ],
  session: ["session_timeout_minutes", "auto_logout_minutes"],
  "subnet-tree": ["subnet_tree_default_expanded_depth"],
  updates: ["github_release_check_enabled"],
  utilization: [
    "utilization_warn_threshold",
    "utilization_critical_threshold",
    "utilization_max_prefix_ipv4",
    "utilization_max_prefix_ipv6",
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

function OUIRefreshModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  // ``null`` = we haven't POSTed yet; kicked by the button below.
  const [taskId, setTaskId] = useState<string | null>(null);
  const [startedAt] = useState<number>(() => Date.now());
  const [hardError, setHardError] = useState<string | null>(null);
  // Poll the status endpoint until the task reaches a terminal state.
  // PENDING vs STARTED aren't distinguishable up-front so we keep
  // polling every 2s and stop as soon as ``ready`` flips true.
  const { data: taskStatus } = useQuery<OUITaskStatus>({
    queryKey: ["oui-refresh-task", taskId],
    queryFn: () => settingsApi.getOUIRefreshStatus(taskId!),
    enabled: !!taskId && !hardError,
    refetchInterval: (q) => (q.state.data?.ready || hardError ? false : 2000),
  });
  const terminal =
    taskStatus?.state === "SUCCESS" || taskStatus?.state === "FAILURE";
  // Kick the refresh on mount. Separate effect so we can bail out
  // cleanly if the feature's been disabled behind our back.
  useEffect(() => {
    let cancelled = false;
    settingsApi
      .refreshOUI()
      .then((res) => {
        if (cancelled) return;
        if (res.status === "disabled") {
          setHardError(
            "OUI lookup is disabled. Enable the toggle + Save first, then try again.",
          );
          return;
        }
        if (res.task_id) setTaskId(res.task_id);
      })
      .catch(() =>
        setHardError("Could not queue the refresh — check API logs."),
      );
    return () => {
      cancelled = true;
    };
  }, []);
  // When the task finishes, invalidate the status query so the outer
  // "Last Updated" + "Vendor Count" rows pick up the new numbers.
  useEffect(() => {
    if (terminal) qc.invalidateQueries({ queryKey: ["oui-status"] });
  }, [terminal, qc]);
  const elapsed = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (terminal || hardError) return;
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [terminal, hardError]);
  // Reading ``tick`` so ESLint + the compiler know the interval side-effect
  // matters for re-rendering the elapsed-seconds counter.
  void tick;

  const result = taskStatus?.result ?? null;
  const counters =
    taskStatus?.state === "SUCCESS" && result?.status === "ran"
      ? [
          { label: "Total", value: result.total ?? 0 },
          { label: "Added", value: result.added ?? 0 },
          { label: "Updated", value: result.updated ?? 0 },
          { label: "Removed", value: result.removed ?? 0 },
          { label: "Unchanged", value: result.unchanged ?? 0 },
        ]
      : null;

  let stageIcon: React.ReactNode;
  let stageText: string;
  let stageClass: string;
  if (hardError) {
    stageIcon = <AlertCircle className="h-5 w-5" />;
    stageText = hardError;
    stageClass = "text-destructive";
  } else if (!taskStatus || !taskStatus.ready) {
    stageIcon = <Loader2 className="h-5 w-5 animate-spin" />;
    stageText =
      taskStatus?.state === "STARTED"
        ? "Fetching the IEEE CSV + applying diff…"
        : "Queued — waiting for the worker to pick up the task…";
    stageClass = "text-muted-foreground";
  } else if (taskStatus.state === "SUCCESS") {
    if (result?.status === "ran") {
      stageIcon = <CheckCircle2 className="h-5 w-5" />;
      stageText = `Refresh complete in ${elapsed}s.`;
      stageClass = "text-emerald-600 dark:text-emerald-400";
    } else if (result?.status === "skipped") {
      stageIcon = <AlertCircle className="h-5 w-5" />;
      stageText = `Skipped — interval not elapsed (${Math.ceil((result.wait_seconds as number | undefined) ?? 0)}s remaining).`;
      stageClass = "text-muted-foreground";
    } else if (result?.status === "error") {
      stageIcon = <AlertCircle className="h-5 w-5" />;
      stageText = `Refresh failed: ${result.reason ?? "unknown"}${result.detail ? ` — ${result.detail}` : ""}`;
      stageClass = "text-destructive";
    } else {
      stageIcon = <AlertCircle className="h-5 w-5" />;
      stageText = `Disabled — ${result?.status ?? "unknown state"}`;
      stageClass = "text-muted-foreground";
    }
  } else {
    stageIcon = <AlertCircle className="h-5 w-5" />;
    stageText = taskStatus.error ?? "Refresh failed.";
    stageClass = "text-destructive";
  }

  return (
    <Modal title="Refresh OUI Vendor Database" onClose={onClose}>
      <div className="space-y-4 text-sm">
        <div className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
          Source:{" "}
          <a
            href={OUI_SOURCE_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="font-mono underline decoration-dotted underline-offset-2 hover:text-foreground"
          >
            {OUI_SOURCE_URL}
          </a>
        </div>

        <div className={cn("flex items-start gap-3", stageClass)}>
          <div className="mt-0.5">{stageIcon}</div>
          <div className="flex-1">
            <div className="font-medium">{stageText}</div>
            {!terminal && !hardError && (
              <div className="mt-1 text-xs text-muted-foreground">
                Elapsed: {elapsed}s (cold fetches can take 30–90s)
              </div>
            )}
            {taskId && (
              <div className="mt-1 font-mono text-[10px] text-muted-foreground/70">
                task {taskId}
              </div>
            )}
          </div>
        </div>

        {counters && (
          <div className="grid grid-cols-5 gap-2 rounded-md border bg-muted/20 p-3">
            {counters.map((c) => (
              <div key={c.label} className="text-center">
                <div className="text-lg font-semibold tabular-nums">
                  {c.value.toLocaleString()}
                </div>
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  {c.label}
                </div>
              </div>
            ))}
          </div>
        )}

        <div className="flex justify-end">
          <button
            onClick={onClose}
            disabled={!terminal && !hardError}
            className="rounded-md bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
          >
            {terminal || hardError ? "Close" : "Please wait…"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function OUILookupSection({
  values,
  set,
  isSuperadmin,
  inputCls,
}: {
  values: PlatformSettings;
  set: <K extends keyof PlatformSettings>(
    key: K,
    value: PlatformSettings[K],
  ) => void;
  isSuperadmin: boolean;
  inputCls: string;
}) {
  const [showRefreshModal, setShowRefreshModal] = useState(false);
  const { data: status } = useQuery({
    queryKey: ["oui-status"],
    queryFn: settingsApi.getOUIStatus,
    refetchInterval: 30_000,
  });
  return (
    <>
      <div className="py-3">
        <div className="text-xs text-muted-foreground">
          Source file:{" "}
          <a
            href={OUI_SOURCE_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="font-mono underline decoration-dotted underline-offset-2 hover:text-foreground"
          >
            {OUI_SOURCE_URL}
          </a>
        </div>
        <div className="mt-1 text-xs text-muted-foreground">
          Fetched by the control plane (not your browser). ~5 MB CSV, ~35k
          prefixes. Stored in Postgres as the <code>oui_vendor</code> table and
          updated incrementally — only prefixes that actually changed bump their{" "}
          <code>updated_at</code>.
        </div>
      </div>
      <Field
        label="Enable OUI Lookup"
        description="Turn on to render MAC addresses with their vendor name in IP tables and DHCP leases. Off by default."
      >
        <Toggle
          checked={!!values.oui_lookup_enabled}
          onChange={(v) => set("oui_lookup_enabled", v)}
          disabled={!isSuperadmin}
        />
      </Field>
      <Field
        label="Refresh Interval"
        description="Hours between automatic fetches. The IEEE file updates roughly daily — leave at 24 unless you're debugging loader behaviour."
      >
        <div className="flex items-center gap-2">
          <input
            type="number"
            min={1}
            max={168}
            value={values.oui_update_interval_hours ?? 24}
            onChange={(e) =>
              set("oui_update_interval_hours", Number(e.target.value))
            }
            disabled={!isSuperadmin || !values.oui_lookup_enabled}
            className={cn(inputCls, "w-24")}
          />
          <span className="text-xs text-muted-foreground">hours</span>
        </div>
      </Field>
      <Field
        label="Last Updated"
        description="Timestamp of the most recent successful fetch."
      >
        <span className="rounded bg-muted px-2 py-1 text-xs font-mono text-muted-foreground">
          {status?.last_updated_at
            ? new Date(status.last_updated_at).toLocaleString()
            : "never"}
        </span>
      </Field>
      <Field
        label="Vendor Count"
        description="Number of OUI prefixes currently loaded."
      >
        <span className="rounded bg-muted px-2 py-1 text-xs font-mono text-muted-foreground">
          {(status?.vendor_count ?? 0).toLocaleString()}
        </span>
      </Field>
      <Field
        label="Refresh Now"
        description="Kick off a fetch immediately without waiting for the next scheduled tick. Save the settings first if you just toggled the feature on."
      >
        <button
          onClick={() => setShowRefreshModal(true)}
          disabled={!isSuperadmin || !values.oui_lookup_enabled}
          className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-accent disabled:opacity-40"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh
        </button>
      </Field>
      {showRefreshModal && (
        <OUIRefreshModal onClose={() => setShowRefreshModal(false)} />
      )}
    </>
  );
}

// ── Device Profiling ───────────────────────────────────────────────────
//
// Backend stores ``fingerbank_api_key_encrypted`` (Fernet at rest) and
// surfaces a read-only ``fingerbank_api_key_set`` boolean on the
// settings response. To set or clear the key, the operator submits a
// plaintext value on the update payload — empty string clears, a
// non-empty value encrypts + replaces. We mirror that in this UI: a
// toggle between "Configured ✓ Replace…" and a password-style input,
// and a separate "Clear" button when one is on file.
function DeviceProfilingSection({
  values,
  set,
  isSuperadmin,
  inputCls,
}: {
  values: PlatformSettings;
  set: <K extends keyof PlatformSettings>(
    key: K,
    value: PlatformSettings[K],
  ) => void;
  isSuperadmin: boolean;
  inputCls: string;
}) {
  const isSet = !!values.fingerbank_api_key_set;
  const draft = values.fingerbank_api_key;
  // Tri-state UI: configured + idle, or replacing/setting (input
  // visible), or clear-pending (draft === "").
  // When the operator clicks Replace… we set draft to "" — that
  // *would* clear the key on save. To keep the "replacing" intent
  // distinct from "clear", we rely on the empty-string draft being
  // typed-over before save. If they save with the input still empty,
  // that's a clear, which matches what the Clear button does anyway.
  const [replacing, setReplacing] = useState(false);
  const clearPending = isSet && draft === "";
  const showInput =
    !isSet || replacing || (draft !== undefined && draft !== "");
  return (
    <>
      <Field
        label="fingerbank API key"
        description="Sign up at fingerbank.org for a free key. Stored Fernet-encrypted server-side; the plaintext is never returned. Without a key, raw DHCP signatures still surface in the IP detail modal but no enrichment runs."
      >
        {clearPending ? (
          <div className="flex items-center gap-2">
            <span className="rounded bg-amber-500/10 px-2 py-1 text-xs font-medium text-amber-700 dark:text-amber-400">
              Pending clear — save to apply
            </span>
            <button
              type="button"
              onClick={() => set("fingerbank_api_key", undefined)}
              disabled={!isSuperadmin}
              className="rounded-md border px-2 py-1 text-xs hover:bg-accent disabled:opacity-40"
            >
              Undo
            </button>
          </div>
        ) : showInput ? (
          <div className="flex items-center gap-2">
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={draft ?? ""}
              onChange={(e) => set("fingerbank_api_key", e.target.value)}
              placeholder={isSet ? "(replace existing key)" : "Paste API key"}
              disabled={!isSuperadmin}
              className={cn(inputCls, "w-96 max-w-full font-mono")}
            />
            {isSet && (
              <button
                type="button"
                onClick={() => {
                  set("fingerbank_api_key", undefined);
                  setReplacing(false);
                }}
                disabled={!isSuperadmin}
                className="rounded-md border px-2 py-1 text-xs hover:bg-accent disabled:opacity-40"
                title="Cancel — keep the existing key"
              >
                Cancel
              </button>
            )}
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <span className="rounded bg-emerald-500/10 px-2 py-1 text-xs font-medium text-emerald-700 dark:text-emerald-400">
              Configured ✓
            </span>
            <button
              type="button"
              onClick={() => setReplacing(true)}
              disabled={!isSuperadmin}
              className="rounded-md border px-2 py-1 text-xs hover:bg-accent disabled:opacity-40"
            >
              Replace…
            </button>
            <button
              type="button"
              onClick={() => set("fingerbank_api_key", "")}
              disabled={!isSuperadmin}
              className="rounded-md border border-destructive/40 px-2 py-1 text-xs text-destructive hover:bg-destructive/10 disabled:opacity-40"
            >
              Clear
            </button>
          </div>
        )}
      </Field>
      <Field
        label="How it gets used"
        description="The DHCP agent's scapy sniffer (when enabled with DHCP_FINGERPRINT_ENABLED=1) ships option-55 / option-60 / option-77 / client-id captures to the control plane. A Celery task looks up each fingerprint via the fingerbank API and stamps Type / Class / Manufacturer onto every IPAM row sharing the MAC. 7-day cache; failures are swallowed so collection never breaks."
      >
        <span className="text-xs text-muted-foreground">
          See the IP detail modal's "Device profile" section for live results.
        </span>
      </Field>
    </>
  );
}

// Grouped, then alphabetical by title within each group. The sidebar
// renders a section header + divider at every group boundary.
const SECTIONS: SectionDef[] = [
  // ── Application ──────────────────────────────────────────────────────
  {
    id: "branding",
    title: "Branding & URL",
    group: "Application",
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
    id: "updates",
    title: "Updates",
    group: "Application",
    description: "Release-check behavior.",
    keywords: ["github", "release", "version", "check"],
  },

  // ── Security ─────────────────────────────────────────────────────────
  {
    id: "audit-forward",
    title: "Audit Event Forwarding",
    group: "Security",
    description:
      "Multi-target forwarding for AuditLog events. Add syslog (UDP/TCP/TLS with RFC 5424 JSON, CEF, LEEF, RFC 3164, or raw JSON-lines) or HTTP webhook targets; each filters independently and a dead collector never blocks the audit write.",
    keywords: [
      "syslog",
      "siem",
      "webhook",
      "audit",
      "forward",
      "splunk",
      "elastic",
      "graylog",
      "arcsight",
      "qradar",
      "cef",
      "leef",
      "tls",
      "log",
      "export",
    ],
  },
  {
    id: "password-policy",
    title: "Password Policy",
    group: "Security",
    description:
      "Complexity, history, and rotation rules applied to local-auth users on every password set. Defaults are deliberately permissive — operators tighten as their compliance footprint grows.",
    keywords: [
      "password",
      "policy",
      "complexity",
      "history",
      "rotation",
      "expire",
      "expiry",
      "max age",
      "min length",
      "uppercase",
      "lowercase",
      "digit",
      "symbol",
      "pci",
      "hipaa",
      "soc2",
    ],
  },
  {
    id: "session",
    title: "Session & Security",
    group: "Security",
    description: "Login session lifetime and idle behavior.",
    keywords: ["timeout", "logout", "expiry", "auth"],
  },

  // ── IPAM ─────────────────────────────────────────────────────────────
  {
    id: "ip-allocation",
    title: "IP Allocation",
    group: "IPAM",
    description: "How the next IP is chosen during auto-allocation.",
    keywords: ["sequential", "random", "next ip", "strategy"],
  },
  {
    id: "oui-lookup",
    title: "OUI Vendor Lookup",
    group: "IPAM",
    description:
      "Pull the IEEE OUI database so MAC addresses in IP tables and DHCP leases render with the vendor name alongside. Opt-in — off by default.",
    keywords: [
      "oui",
      "vendor",
      "mac",
      "ieee",
      "manufacturer",
      "lookup",
      "prefix",
    ],
  },
  {
    id: "device-profiling",
    title: "Device Profiling",
    group: "IPAM",
    description:
      "Optional fingerbank API key — turns the DHCP fingerprints captured by the agent's scapy sniffer into Type / Class / Manufacturer on every IP. Without a key, raw signatures still surface in the IP detail modal but no enrichment runs. Free tier exists at fingerbank.org.",
    keywords: [
      "fingerbank",
      "fingerprint",
      "device",
      "profile",
      "profiling",
      "dhcp",
      "passive",
      "nmap",
      "manufacturer",
      "class",
    ],
  },
  {
    id: "subnet-tree",
    title: "Subnet Tree UI",
    group: "IPAM",
    description: "Tree view defaults in the IPAM browser.",
    keywords: ["expand", "collapse", "depth", "tree"],
  },
  {
    id: "utilization",
    title: "Utilization Thresholds",
    group: "IPAM",
    description: "Warning and critical percentages for utilization indicators.",
    keywords: ["warn", "critical", "threshold", "color"],
  },

  // ── DNS ──────────────────────────────────────────────────────────────
  {
    id: "dns",
    title: "DNS Defaults",
    group: "DNS",
    description: "Default values applied to new zones and server groups.",
    keywords: ["zone", "ttl", "dnssec", "recursion", "agent", "key"],
  },
  {
    id: "dns-auto-sync",
    title: "IPAM → DNS Reconciliation",
    group: "DNS",
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
    group: "DNS",
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

  // ── DHCP ─────────────────────────────────────────────────────────────
  {
    id: "dhcp",
    title: "DHCP Defaults",
    group: "DHCP",
    description:
      "Default DNS servers, domain, NTP, and lease time pre-filled when creating a new scope.",
    keywords: ["dns", "domain", "search", "ntp", "lease", "option 42", "scope"],
  },
  {
    id: "dhcp-lease-sync",
    title: "DHCP Lease Sync",
    group: "DHCP",
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

  // ── Network ──────────────────────────────────────────────────────────
  {
    id: "network-asn",
    title: "ASN Refresh",
    group: "Network",
    description:
      "Cadence for the RDAP / WHOIS refresh of tracked ASNs and the RPKI ROA pull. Beat ticks hourly; the tasks gate on per-row next_check_at + the platform-level interval below, so cadence changes take effect on the next tick without restarting beat.",
    keywords: [
      "asn",
      "autonomous system",
      "rdap",
      "whois",
      "rpki",
      "roa",
      "cloudflare",
      "ripe",
      "bgp",
      "interval",
    ],
  },
  {
    id: "network-domains",
    title: "Domain Refresh",
    group: "Network",
    description:
      "Cadence for the RDAP / WHOIS refresh of tracked domain registrations. Per-row next_check_at gates against this knob.",
    keywords: ["domain", "rdap", "whois", "registrar", "refresh", "expiry"],
  },
  {
    id: "network-vrf",
    title: "VRF Validation",
    group: "Network",
    description:
      "Strict mode for VRF route distinguisher / route target validation. When off, ASN-portion mismatches between an RD/RT and the VRF's linked ASN produce a non-blocking warning. When on, the same mismatch is a 422 hard-fail on create / update.",
    keywords: [
      "vrf",
      "route distinguisher",
      "route target",
      "rd",
      "rt",
      "validation",
      "strict",
    ],
  },

  // ── AI ────────────────────────────────────────────────────────────────
  {
    id: "ai-digest",
    title: "Operator Daily Digest",
    group: "AI",
    description:
      "Once-per-day rollup of audit events, alert activity, and DHCP lease churn — summarised by the highest-priority enabled AI provider and pushed through the existing audit-forward targets (Slack / Teams / Discord / SMTP). Default off; flip on once you have at least one target wired in Audit-forward and at least one AI provider enabled.",
    keywords: [
      "ai",
      "copilot",
      "digest",
      "summary",
      "daily",
      "rollup",
      "report",
      "executive",
    ],
  },

  // ── Integrations ─────────────────────────────────────────────────────
  {
    id: "integrations-kubernetes",
    title: "Kubernetes",
    group: "Integrations",
    description:
      "Connect one or more Kubernetes clusters. When enabled, SpatiumDDI adds a Kubernetes menu item to the sidebar where you manage per-cluster connection configs. Read-only — SpatiumDDI polls the cluster's API server with a service-account token and mirrors LoadBalancer VIPs, Node IPs, cluster CIDRs, and Ingress hostnames into the bound IPAM space + DNS group. Never writes to the cluster.",
    keywords: [
      "kubernetes",
      "k8s",
      "cluster",
      "integration",
      "ingress",
      "loadbalancer",
      "ipam",
      "dns",
      "service account",
    ],
  },
  {
    id: "integrations-docker",
    title: "Docker",
    group: "Integrations",
    description:
      "Connect one or more Docker hosts over Unix socket or TCP+TLS. When enabled, SpatiumDDI adds a Docker menu item to the sidebar where you manage per-host connection configs. Read-only — SpatiumDDI polls each daemon and mirrors Docker networks into the bound IPAM space as subnets, with container IPs as opt-in. Never writes to the daemon.",
    keywords: [
      "docker",
      "container",
      "compose",
      "swarm",
      "integration",
      "bridge",
      "network",
      "ipam",
      "socket",
      "tls",
    ],
  },
  {
    id: "integrations-proxmox",
    title: "Proxmox",
    group: "Integrations",
    description:
      "Connect one or more Proxmox VE endpoints via the REST API with an API token. A single endpoint can represent a whole cluster (the PVE API is homogeneous across members). Read-only — SpatiumDDI mirrors bridges + VLAN interfaces as subnets, and VMs + LXC containers as IP rows. Runtime IPs come from the QEMU guest-agent (VMs) or the LXC /interfaces endpoint. Never writes to PVE.",
    keywords: [
      "proxmox",
      "pve",
      "qemu",
      "lxc",
      "hypervisor",
      "vm",
      "container",
      "bridge",
      "cluster",
      "integration",
      "ipam",
      "dns",
      "token",
    ],
  },
  {
    id: "integrations-tailscale",
    title: "Tailscale",
    group: "Integrations",
    description:
      "Connect one or more Tailscale tenants (tailnets) via the Tailscale REST API with a personal-access token. Read-only — SpatiumDDI auto-creates the CGNAT 100.64.0.0/10 IPv4 block + the IPv6 ULA block under the bound IPAM space and mirrors every tailnet device's addresses as IP rows with OS / version / user / tags / routes in custom fields. Never writes to Tailscale.",
    keywords: [
      "tailscale",
      "tailnet",
      "wireguard",
      "vpn",
      "mesh",
      "magicdns",
      "cgnat",
      "integration",
      "ipam",
      "pat",
      "token",
    ],
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
          {/* When filtering we drop the group headers so results read as a
              single flat list; otherwise render grouped with separators. */}
          {q
            ? filteredSections.map((s) => (
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
              ))
            : GROUP_ORDER.map((group, idx) => {
                // Integrations: always alphabetical by title so the
                // order is stable regardless of the source-order that
                // new integration entries are appended in. Other
                // groups keep their declared order (the grouping is
                // intentional — e.g. IPAM Import/Export reads
                // naturally grouped, not alphabetized).
                const entries =
                  group === "Integrations"
                    ? SECTIONS.filter((s) => s.group === group)
                        .slice()
                        .sort((a, b) => a.title.localeCompare(b.title))
                    : SECTIONS.filter((s) => s.group === group);
                if (entries.length === 0) return null;
                return (
                  <div
                    key={group}
                    className={cn(idx > 0 && "mt-3 border-t pt-3")}
                  >
                    <p className="mb-1 px-3 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/70">
                      {group}
                    </p>
                    {entries.map((s) => (
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
                  </div>
                );
              })}
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
                  description="How often the sync job polls each agentless server, in seconds. Beat ticks every 10 s (the floor); 15 s is the default — near-real-time IPAM population without hammering the Windows DC. Raise it (e.g. 60 / 300) if WinRM latency matters more than freshness."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={10}
                      value={values.dhcp_pull_leases_interval_seconds ?? 15}
                      onChange={(e) =>
                        set(
                          "dhcp_pull_leases_interval_seconds",
                          Number(e.target.value),
                        )
                      }
                      disabled={
                        !isSuperadmin || !values.dhcp_pull_leases_enabled
                      }
                      className={cn(inputCls, "w-24")}
                    />
                    <span className="text-xs text-muted-foreground">sec</span>
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

            {activeId === "audit-forward" && (
              <AuditForwardTargets isSuperadmin={!!isSuperadmin} />
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

            {activeId === "oui-lookup" && (
              <OUILookupSection
                values={values}
                set={set}
                isSuperadmin={isSuperadmin}
                inputCls={inputCls}
              />
            )}

            {activeId === "device-profiling" && (
              <DeviceProfilingSection
                values={values}
                set={set}
                isSuperadmin={isSuperadmin}
                inputCls={inputCls}
              />
            )}

            {activeId === "network-asn" && (
              <>
                <Field
                  label="ASN WHOIS / RDAP refresh interval"
                  description="How often each tracked ASN re-queries RDAP for registration data. Per-ASN gating on next_check_at — bumping this knob is the dial for the global cadence; one-off refresh is available from the per-row button on the ASNs page. 1–168 h range."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={1}
                      max={168}
                      value={values.asn_whois_interval_hours ?? 24}
                      onChange={(e) =>
                        set("asn_whois_interval_hours", Number(e.target.value))
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-24")}
                    />
                    <span className="text-xs text-muted-foreground">hours</span>
                  </div>
                </Field>
                <Field
                  label="RPKI ROA source"
                  description="Source for the global RPKI ROA dump that feeds per-ASN ROA tracking. Cloudflare's mirror is the default — it caches well, refreshes every ~20 min, and ships JSON. RIPE NCC is the authoritative source if you'd rather skip the Cloudflare hop."
                >
                  <select
                    value={values.rpki_roa_source ?? "cloudflare"}
                    onChange={(e) => set("rpki_roa_source", e.target.value)}
                    disabled={!isSuperadmin}
                    className={inputCls}
                  >
                    <option value="cloudflare">Cloudflare (default)</option>
                    <option value="ripe">RIPE NCC</option>
                  </select>
                </Field>
                <Field
                  label="RPKI ROA refresh interval"
                  description="How often the global ROA dump is re-pulled and per-ASN rows are reconciled (INSERT / UPDATE / DELETE + state transitions). The source service caches the dump in-memory for 5 min, so values below 1 h still hit cache where possible. 1–24 h range."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={1}
                      max={24}
                      value={values.rpki_roa_refresh_interval_hours ?? 4}
                      onChange={(e) =>
                        set(
                          "rpki_roa_refresh_interval_hours",
                          Number(e.target.value),
                        )
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-24")}
                    />
                    <span className="text-xs text-muted-foreground">hours</span>
                  </div>
                </Field>
              </>
            )}

            {activeId === "network-domains" && (
              <Field
                label="Domain WHOIS / RDAP refresh interval"
                description="How often each tracked domain re-queries RDAP for registrar / expiry / nameserver data. Per-domain gating on next_check_at; one-off refresh is on the per-row button. 1–168 h range."
              >
                <div className="flex items-center gap-2">
                  <input
                    type="number"
                    min={1}
                    max={168}
                    value={values.domain_whois_interval_hours ?? 24}
                    onChange={(e) =>
                      set("domain_whois_interval_hours", Number(e.target.value))
                    }
                    disabled={!isSuperadmin}
                    className={cn(inputCls, "w-24")}
                  />
                  <span className="text-xs text-muted-foreground">hours</span>
                </div>
              </Field>
            )}

            {activeId === "network-vrf" && (
              <Field
                label="Strict RD / RT validation"
                description="When ON, an ASN:N route distinguisher (or any ASN:N entry in import / export route-target lists) whose ASN portion does not match the VRF's linked ASN row returns 422 — the create / update is rejected outright. When OFF (default), the same mismatch is a non-blocking warning on the response and the row still saves. Flip ON for shops that want strict cross-cutting validation; leave OFF if you regularly carry partner / customer RDs that intentionally don't match your local AS."
              >
                <div className="flex items-center gap-3">
                  <Toggle
                    checked={!!values.vrf_strict_rd_validation}
                    onChange={(v) => set("vrf_strict_rd_validation", v)}
                    disabled={!isSuperadmin}
                  />
                  <span className="text-sm text-muted-foreground">
                    {values.vrf_strict_rd_validation ? "Strict" : "Warn only"}
                  </span>
                </div>
              </Field>
            )}

            {activeId === "ai-digest" && (
              <Field
                label="Daily Operator Digest"
                description="Fires daily at 08:00 UTC. Picks the highest-priority enabled AI provider, summarises the last 24 h of activity, and dispatches the result through every audit-forward target as a `kind=digest` payload (filter on `resource_types: ['ai.digest']` on a target to route digests separately from alerts). Default OFF — turn ON once you have an AI provider and at least one audit-forward target wired up."
              >
                <div className="flex items-center gap-3">
                  <Toggle
                    checked={!!values.ai_daily_digest_enabled}
                    onChange={(v) => set("ai_daily_digest_enabled", v)}
                    disabled={!isSuperadmin}
                  />
                  <span className="text-sm text-muted-foreground">
                    {values.ai_daily_digest_enabled ? "Enabled" : "Disabled"}
                  </span>
                </div>
              </Field>
            )}

            {activeId === "password-policy" && (
              <>
                <Field
                  label="Minimum length"
                  description="Hard floor at 6 characters. Bcrypt truncates above 72 bytes — past that the extra characters aren't actually being checked."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={6}
                      max={128}
                      value={values.password_min_length ?? 12}
                      onChange={(e) =>
                        set("password_min_length", Number(e.target.value))
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-24")}
                    />
                    <span className="text-xs text-muted-foreground">chars</span>
                  </div>
                </Field>
                <Field
                  label="Require uppercase letter"
                  description="At least one A–Z."
                >
                  <Toggle
                    checked={!!values.password_require_uppercase}
                    onChange={(v) => set("password_require_uppercase", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                <Field
                  label="Require lowercase letter"
                  description="At least one a–z."
                >
                  <Toggle
                    checked={!!values.password_require_lowercase}
                    onChange={(v) => set("password_require_lowercase", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                <Field label="Require digit" description="At least one 0–9.">
                  <Toggle
                    checked={!!values.password_require_digit}
                    onChange={(v) => set("password_require_digit", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                <Field
                  label="Require symbol"
                  description="At least one ASCII punctuation character (e.g. ! @ # $)."
                >
                  <Toggle
                    checked={!!values.password_require_symbol}
                    onChange={(v) => set("password_require_symbol", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                <Field
                  label="Password history"
                  description="Block reuse of the last N passwords. 0 disables. Capped at 24."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={0}
                      max={24}
                      value={values.password_history_count ?? 5}
                      onChange={(e) =>
                        set("password_history_count", Number(e.target.value))
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-24")}
                    />
                    <span className="text-xs text-muted-foreground">
                      previous
                    </span>
                  </div>
                </Field>
                <Field
                  label="Maximum password age"
                  description="Force a password change on the next login after this many days. 0 disables. Applies to local-auth users only — external-IdP users carry their own rotation policy."
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={0}
                      max={3650}
                      value={values.password_max_age_days ?? 0}
                      onChange={(e) =>
                        set("password_max_age_days", Number(e.target.value))
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-24")}
                    />
                    <span className="text-xs text-muted-foreground">days</span>
                  </div>
                </Field>
              </>
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

            {activeId === "integrations-kubernetes" && (
              <>
                <Field
                  label="Enable Kubernetes integration"
                  description="Adds a Kubernetes menu item to the sidebar. Per-cluster connection configs are managed there."
                >
                  <Toggle
                    checked={!!values.integration_kubernetes_enabled}
                    onChange={(v) => set("integration_kubernetes_enabled", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                {values.integration_kubernetes_enabled && (
                  <Field
                    label="Clusters"
                    description="Manage connected clusters."
                  >
                    <a
                      href="/kubernetes"
                      className="inline-flex items-center gap-1 rounded-md border px-3 py-1.5 text-xs hover:bg-accent"
                    >
                      Open Kubernetes page →
                    </a>
                  </Field>
                )}
              </>
            )}

            {activeId === "integrations-docker" && (
              <>
                <Field
                  label="Enable Docker integration"
                  description="Adds a Docker menu item to the sidebar. Per-host connection configs are managed there."
                >
                  <Toggle
                    checked={!!values.integration_docker_enabled}
                    onChange={(v) => set("integration_docker_enabled", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                {values.integration_docker_enabled && (
                  <Field label="Hosts" description="Manage connected hosts.">
                    <a
                      href="/docker"
                      className="inline-flex items-center gap-1 rounded-md border px-3 py-1.5 text-xs hover:bg-accent"
                    >
                      Open Docker page →
                    </a>
                  </Field>
                )}
              </>
            )}

            {activeId === "integrations-proxmox" && (
              <>
                <Field
                  label="Enable Proxmox integration"
                  description="Adds a Proxmox menu item to the sidebar. Per-endpoint connection configs are managed there."
                >
                  <Toggle
                    checked={!!values.integration_proxmox_enabled}
                    onChange={(v) => set("integration_proxmox_enabled", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                {values.integration_proxmox_enabled && (
                  <Field
                    label="Endpoints"
                    description="Manage connected Proxmox VE endpoints."
                  >
                    <a
                      href="/proxmox"
                      className="inline-flex items-center gap-1 rounded-md border px-3 py-1.5 text-xs hover:bg-accent"
                    >
                      Open Proxmox page →
                    </a>
                  </Field>
                )}
              </>
            )}

            {activeId === "integrations-tailscale" && (
              <>
                <Field
                  label="Enable Tailscale integration"
                  description="Adds a Tailscale menu item to the sidebar. Per-tenant connection configs are managed there."
                >
                  <Toggle
                    checked={!!values.integration_tailscale_enabled}
                    onChange={(v) => set("integration_tailscale_enabled", v)}
                    disabled={!isSuperadmin}
                  />
                </Field>
                {values.integration_tailscale_enabled && (
                  <Field
                    label="Tenants"
                    description="Manage connected Tailscale tenants (tailnets)."
                  >
                    <a
                      href="/tailscale"
                      className="inline-flex items-center gap-1 rounded-md border px-3 py-1.5 text-xs hover:bg-accent"
                    >
                      Open Tailscale page →
                    </a>
                  </Field>
                )}
              </>
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
                <Field
                  label="IPv4 Max Prefix for Reporting"
                  description="Exclude subnets smaller than this from dashboard heatmap + alerts. Default 29 excludes /30, /31, /32 (PTP links, single-host). Set to 32 to disable."
                >
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-muted-foreground">/</span>
                    <input
                      type="number"
                      min={0}
                      max={32}
                      value={values.utilization_max_prefix_ipv4 ?? 29}
                      onChange={(e) =>
                        set(
                          "utilization_max_prefix_ipv4",
                          Number(e.target.value),
                        )
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-20")}
                    />
                  </div>
                </Field>
                <Field
                  label="IPv6 Max Prefix for Reporting"
                  description="Exclude subnets smaller than this from dashboard heatmap + alerts. Default 126 excludes /127 (RFC 6164 PTP) and /128. Set to 128 to disable."
                >
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-muted-foreground">/</span>
                    <input
                      type="number"
                      min={0}
                      max={128}
                      value={values.utilization_max_prefix_ipv6 ?? 126}
                      onChange={(e) =>
                        set(
                          "utilization_max_prefix_ipv6",
                          Number(e.target.value),
                        )
                      }
                      disabled={!isSuperadmin}
                      className={cn(inputCls, "w-20")}
                    />
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
