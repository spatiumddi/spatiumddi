import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  Pencil,
  Play,
  Plus,
  Power,
  PowerOff,
  Trash2,
} from "lucide-react";
import {
  alertsApi,
  type AlertRule,
  type AlertRuleType,
  type AlertServerType,
  type AlertSeverity,
} from "@/lib/api";
import { cn, zebraBodyCls } from "@/lib/utils";
import { Modal } from "@/components/ui/modal";

const inputCls =
  "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs font-medium text-muted-foreground">
        {label}
      </label>
      {children}
      {hint && <p className="text-[11px] text-muted-foreground/80">{hint}</p>}
    </div>
  );
}

// ── Rule editor ────────────────────────────────────────────────────────────

function RuleEditorModal({
  existing,
  onClose,
}: {
  existing: AlertRule | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(existing?.name ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [ruleType, setRuleType] = useState<AlertRuleType>(
    existing?.rule_type ?? "subnet_utilization",
  );
  const [threshold, setThreshold] = useState<number>(
    existing?.threshold_percent ?? 90,
  );
  const [serverType, setServerType] = useState<AlertServerType>(
    (existing?.server_type as AlertServerType | null) ?? "any",
  );
  const [severity, setSeverity] = useState<AlertSeverity>(
    existing?.severity ?? "warning",
  );
  const [notifySyslog, setNotifySyslog] = useState(
    existing?.notify_syslog ?? true,
  );
  const [notifyWebhook, setNotifyWebhook] = useState(
    existing?.notify_webhook ?? true,
  );
  const [notifySmtp, setNotifySmtp] = useState(existing?.notify_smtp ?? false);
  const [error, setError] = useState<string | null>(null);

  const mut = useMutation({
    mutationFn: async () => {
      const body = {
        name,
        description,
        enabled,
        severity,
        notify_syslog: notifySyslog,
        notify_webhook: notifyWebhook,
        notify_smtp: notifySmtp,
        threshold_percent: ruleType === "subnet_utilization" ? threshold : null,
        server_type: ruleType === "server_unreachable" ? serverType : null,
      };
      if (existing) {
        return alertsApi.updateRule(existing.id, body);
      }
      return alertsApi.createRule({ ...body, rule_type: ruleType });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alert-rules"] });
      onClose();
    },
    onError: (e: unknown) => {
      const err = e as { response?: { data?: { detail?: string } } };
      setError(err?.response?.data?.detail ?? "Save failed");
    },
  });

  return (
    <Modal
      onClose={onClose}
      title={existing ? "Edit alert rule" : "New alert rule"}
    >
      <div className="space-y-4">
        <Field label="Name">
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Subnet nearing capacity"
          />
        </Field>
        <Field label="Description">
          <textarea
            className={cn(inputCls, "min-h-[60px]")}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
        {!existing && (
          <Field
            label="Rule type"
            hint="Cannot be changed after creation — delete + recreate to switch."
          >
            <select
              className={inputCls}
              value={ruleType}
              onChange={(e) => setRuleType(e.target.value as AlertRuleType)}
            >
              <option value="subnet_utilization">Subnet utilization</option>
              <option value="server_unreachable">Server unreachable</option>
              <option value="asn_holder_drift">ASN holder drift</option>
              <option value="asn_whois_unreachable">
                ASN WHOIS unreachable
              </option>
              <option value="rpki_roa_expiring">RPKI ROA expiring</option>
              <option value="rpki_roa_expired">RPKI ROA expired</option>
            </select>
          </Field>
        )}
        {ruleType === "subnet_utilization" && (
          <Field
            label="Threshold (%)"
            hint="Fires when subnet utilization ≥ this. PTP / loopback subnets (prefix > utilization_max_prefix_*) are excluded."
          >
            <input
              type="number"
              min={0}
              max={100}
              className={inputCls}
              value={threshold}
              onChange={(e) => setThreshold(Number(e.target.value))}
            />
          </Field>
        )}
        {ruleType === "server_unreachable" && (
          <Field label="Server type">
            <select
              className={inputCls}
              value={serverType}
              onChange={(e) => setServerType(e.target.value as AlertServerType)}
            >
              <option value="any">DNS + DHCP</option>
              <option value="dns">DNS only</option>
              <option value="dhcp">DHCP only</option>
            </select>
          </Field>
        )}
        <Field label="Severity">
          <select
            className={inputCls}
            value={severity}
            onChange={(e) => setSeverity(e.target.value as AlertSeverity)}
          >
            <option value="info">Info</option>
            <option value="warning">Warning</option>
            <option value="critical">Critical</option>
          </select>
        </Field>
        <div className="rounded-md border bg-muted/20 p-3 space-y-2">
          <p className="text-xs font-medium text-muted-foreground">
            Delivery channels
          </p>
          <p className="text-[11px] text-muted-foreground/80">
            Fans out to every enabled target of the matching kind in Settings →
            Audit Event Forwarding (webhook covers Slack / Teams / Discord chat
            flavors automatically).
          </p>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={notifySyslog}
              onChange={(e) => setNotifySyslog(e.target.checked)}
            />
            Syslog
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={notifyWebhook}
              onChange={(e) => setNotifyWebhook(e.target.checked)}
            />
            Webhook (incl. Slack / Teams / Discord)
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={notifySmtp}
              onChange={(e) => setNotifySmtp(e.target.checked)}
            />
            Email (SMTP)
          </label>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          Rule enabled
        </label>
        {error && <p className="text-xs text-red-600">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!name.trim() || mut.isPending}
            onClick={() => mut.mutate()}
            className={cn(
              "rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground",
              "hover:bg-primary/90 disabled:opacity-50",
            )}
          >
            {mut.isPending ? "Saving…" : existing ? "Save" : "Create"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Page ───────────────────────────────────────────────────────────────────

export function AlertsPage() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<AlertRule | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [openOnly, setOpenOnly] = useState(true);

  const { data: rules = [], isLoading: rulesLoading } = useQuery({
    queryKey: ["alert-rules"],
    queryFn: alertsApi.listRules,
  });

  const { data: events = [] } = useQuery({
    queryKey: ["alert-events", { openOnly }],
    queryFn: () => alertsApi.listEvents({ open_only: openOnly, limit: 100 }),
    refetchInterval: 15_000,
  });

  const toggle = useMutation({
    mutationFn: (r: AlertRule) =>
      alertsApi.updateRule(r.id, { enabled: !r.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alert-rules"] }),
  });

  const del = useMutation({
    mutationFn: (id: string) => alertsApi.deleteRule(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alert-rules"] }),
  });

  const evaluate = useMutation({
    mutationFn: () => alertsApi.evaluateNow(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alert-events"] });
    },
  });

  const resolve = useMutation({
    mutationFn: (id: string) => alertsApi.resolveEvent(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alert-events"] }),
  });

  return (
    <div className="h-full overflow-auto p-6">
      <div className="mx-auto max-w-[1200px] space-y-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Alerts</h1>
            <p className="mt-1 text-xs text-muted-foreground">
              Rule-based notifications on subnet utilization + server health.
              Fires via syslog + webhook to the audit-forward targets.
            </p>
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => evaluate.mutate()}
              className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
              disabled={evaluate.isPending}
            >
              <Play className="h-3.5 w-3.5" />
              {evaluate.isPending ? "Evaluating…" : "Evaluate now"}
            </button>
            <button
              onClick={() => setShowCreate(true)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground",
                "hover:bg-primary/90",
              )}
            >
              <Plus className="h-3.5 w-3.5" />
              New rule
            </button>
          </div>
        </div>

        {/* ── Rules ───────────────────────────────────────────────────── */}
        <div className="rounded-lg border bg-card">
          <div className="flex items-center justify-between border-b px-4 py-2.5">
            <h2 className="text-sm font-semibold">Rules</h2>
            <span className="text-xs text-muted-foreground">
              {rules.length} total
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs uppercase tracking-wider text-muted-foreground">
                <tr className="border-b">
                  <th className="px-4 py-2 text-left">Name</th>
                  <th className="px-4 py-2 text-left">Type</th>
                  <th className="px-4 py-2 text-left">Parameters</th>
                  <th className="px-4 py-2 text-left">Severity</th>
                  <th className="px-4 py-2 text-left">Channels</th>
                  <th className="px-4 py-2 text-left">Status</th>
                  <th className="px-4 py-2" />
                </tr>
              </thead>
              <tbody className={zebraBodyCls}>
                {rulesLoading && (
                  <tr>
                    <td
                      colSpan={7}
                      className="px-4 py-8 text-center text-xs text-muted-foreground"
                    >
                      Loading…
                    </td>
                  </tr>
                )}
                {!rulesLoading && rules.length === 0 && (
                  <tr>
                    <td
                      colSpan={7}
                      className="px-4 py-8 text-center text-xs text-muted-foreground"
                    >
                      No rules yet — click “New rule” to create one.
                    </td>
                  </tr>
                )}
                {rules.map((r) => (
                  <tr key={r.id} className="border-b">
                    <td className="px-4 py-2 font-medium">{r.name}</td>
                    <td className="px-4 py-2 text-muted-foreground">
                      {r.rule_type}
                    </td>
                    <td className="px-4 py-2 text-muted-foreground tabular-nums">
                      {r.rule_type === "subnet_utilization"
                        ? `≥ ${r.threshold_percent}%`
                        : r.rule_type === "server_unreachable"
                          ? `type=${r.server_type ?? "any"}`
                          : "—"}
                    </td>
                    <td className="px-4 py-2">
                      <SeverityBadge severity={r.severity} />
                    </td>
                    <td className="px-4 py-2 text-[11px] text-muted-foreground">
                      {[
                        r.notify_syslog && "syslog",
                        r.notify_webhook && "webhook",
                        r.notify_smtp && "smtp",
                      ]
                        .filter(Boolean)
                        .join(" · ") || "(none)"}
                    </td>
                    <td className="px-4 py-2">
                      {r.enabled ? (
                        <span className="inline-flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
                          <Check className="h-3.5 w-3.5" /> enabled
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                          <PowerOff className="h-3.5 w-3.5" /> disabled
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      <div className="flex justify-end gap-1">
                        <button
                          onClick={() => toggle.mutate(r)}
                          title={r.enabled ? "Disable" : "Enable"}
                          className="rounded p-1.5 hover:bg-accent"
                        >
                          <Power className="h-3.5 w-3.5" />
                        </button>
                        <button
                          onClick={() => setEditing(r)}
                          title="Edit"
                          className="rounded p-1.5 hover:bg-accent"
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </button>
                        <button
                          onClick={() => {
                            if (confirm(`Delete rule "${r.name}"?`)) {
                              del.mutate(r.id);
                            }
                          }}
                          title="Delete"
                          className="rounded p-1.5 text-red-600 hover:bg-red-50 dark:hover:bg-red-950/30"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* ── Events ─────────────────────────────────────────────────── */}
        <div className="rounded-lg border bg-card">
          <div className="flex items-center justify-between border-b px-4 py-2.5">
            <div className="flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 text-amber-500" />
              <h2 className="text-sm font-semibold">Events</h2>
              <span className="text-xs text-muted-foreground">
                {events.length} shown
              </span>
            </div>
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={openOnly}
                onChange={(e) => setOpenOnly(e.target.checked)}
              />
              Open only
            </label>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs uppercase tracking-wider text-muted-foreground">
                <tr className="border-b">
                  <th className="px-4 py-2 text-left">Fired</th>
                  <th className="px-4 py-2 text-left">Severity</th>
                  <th className="px-4 py-2 text-left">Subject</th>
                  <th className="px-4 py-2 text-left">Message</th>
                  <th className="px-4 py-2 text-left">State</th>
                  <th className="px-4 py-2" />
                </tr>
              </thead>
              <tbody className={zebraBodyCls}>
                {events.length === 0 && (
                  <tr>
                    <td
                      colSpan={6}
                      className="px-4 py-8 text-center text-xs text-muted-foreground"
                    >
                      {openOnly
                        ? "No open events — all clear."
                        : "No events yet."}
                    </td>
                  </tr>
                )}
                {events.map((ev) => (
                  <tr key={ev.id} className="border-b">
                    <td className="px-4 py-2 text-xs text-muted-foreground tabular-nums whitespace-nowrap">
                      {new Date(ev.fired_at).toLocaleString()}
                    </td>
                    <td className="px-4 py-2">
                      <SeverityBadge severity={ev.severity} />
                    </td>
                    <td className="px-4 py-2 text-xs">
                      <span className="text-muted-foreground">
                        {ev.subject_type}:
                      </span>{" "}
                      <span className="font-medium">{ev.subject_display}</span>
                    </td>
                    <td className="px-4 py-2 text-xs text-muted-foreground">
                      {ev.message}
                    </td>
                    <td className="px-4 py-2">
                      {ev.resolved_at ? (
                        <span className="text-xs text-emerald-600 dark:text-emerald-400">
                          resolved {new Date(ev.resolved_at).toLocaleString()}
                        </span>
                      ) : (
                        <span className="text-xs text-amber-600 dark:text-amber-400">
                          open
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      {!ev.resolved_at && (
                        <button
                          onClick={() => resolve.mutate(ev.id)}
                          className="rounded border px-2 py-0.5 text-[11px] hover:bg-accent"
                        >
                          Resolve
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {showCreate && (
          <RuleEditorModal
            existing={null}
            onClose={() => setShowCreate(false)}
          />
        )}
        {editing && (
          <RuleEditorModal
            existing={editing}
            onClose={() => setEditing(null)}
          />
        )}
      </div>
    </div>
  );
}

function SeverityBadge({ severity }: { severity: AlertSeverity }) {
  const cls =
    severity === "critical"
      ? "bg-red-100 text-red-700 dark:bg-red-950/30 dark:text-red-400"
      : severity === "warning"
        ? "bg-amber-100 text-amber-700 dark:bg-amber-950/30 dark:text-amber-400"
        : "bg-blue-100 text-blue-700 dark:bg-blue-950/30 dark:text-blue-400";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded px-2 py-0.5 text-[11px] font-medium uppercase tracking-wider",
        cls,
      )}
    >
      {severity}
    </span>
  );
}
