import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Loader2,
  RefreshCw,
  BellOff,
  Search,
  ShieldCheck,
  ShieldQuestion,
} from "lucide-react";

import { Modal } from "@/components/ui/modal";
import {
  dnsThreatApi,
  type DNSClientWindow,
  type DNSTunnelSignal,
} from "@/lib/api";

/**
 * Logs → DNS Threat (issue #699).
 *
 * Scored per-client windows, worst first. DNS tunneling hides payload
 * in the names a host looks up, under a domain the attacker's own
 * server answers for — it is the exfiltration path a firewall never
 * inspects.
 *
 * Two framing decisions worth keeping:
 *
 * 1. **An empty list is not an all-clear.** The rollup only has rows
 *    when the default-off module is on AND a DNS group has query
 *    logging enabled, so "nothing here" has two very different
 *    meanings. The empty state says which one it is rather than
 *    rendering a reassuring blank.
 * 2. **The score always shows its working.** Every signal is listed
 *    with its contribution, including the ones that scored zero, so an
 *    operator can see what was considered — "your entropy is fine,
 *    it's the fan-out that's odd" beats a bare 62.
 *
 * #699 asks for a deep link from a finding to the IP detail modal.
 * Not wired yet: the IPAM page accepts ``?subnet=`` / ``?block=`` /
 * ``?space=`` but has no parameter that opens a specific address, and
 * a button that silently does nothing is worse than no button. Needs
 * an ``?ip=`` entry point on IPAMPage first.
 */

const SCORE_BANDS = [
  { min: 60, label: "Likely tunnel", cls: "text-rose-600 dark:text-rose-400" },
  { min: 40, label: "Suspicious", cls: "text-amber-600 dark:text-amber-400" },
  {
    min: 20,
    label: "Worth a look",
    cls: "text-yellow-600 dark:text-yellow-500",
  },
] as const;

function band(score: number) {
  return (
    SCORE_BANDS.find((b) => score >= b.min) ?? {
      label: "Low",
      cls: "text-muted-foreground",
    }
  );
}

function ScoreCell({ score }: { score: number }) {
  const b = band(score);
  return (
    <div className="flex items-center gap-2">
      <span className={`font-mono text-sm font-semibold ${b.cls}`}>
        {score.toFixed(0)}
      </span>
      <span className={`text-xs ${b.cls}`}>{b.label}</span>
    </div>
  );
}

function SignalBar({ signal }: { signal: DNSTunnelSignal }) {
  // Against the signal's OWN ceiling, which the backend reports:
  // weights differ (30/25/30/15), so scaling every bar by the same
  // factor made a fully-saturated payload-qtype signal render at 45%
  // and nothing ever reach 100%.
  const ceiling = signal.max_contribution || 0;
  const pct = ceiling
    ? Math.max(0, Math.min(100, (signal.contribution / ceiling) * 100))
    : 0;
  return (
    <li className="py-1">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-xs">{signal.name}</span>
        <span className="shrink-0 text-xs text-muted-foreground">
          +{signal.contribution.toFixed(1)}
        </span>
      </div>
      <div className="mt-0.5 h-1 w-full overflow-hidden rounded bg-muted">
        <div
          className={
            signal.contribution > 0 ? "h-full bg-amber-500" : "h-full bg-muted"
          }
          style={{ width: `${pct}%` }}
        />
      </div>
      <p className="mt-0.5 break-words text-xs text-muted-foreground">
        {signal.detail}
      </p>
    </li>
  );
}

export function DNSThreatTab() {
  const [hours, setHours] = useState(24);
  const [minScore, setMinScore] = useState(20);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Allowlisted windows are hidden by default but reachable: the
  // rollup keeps them so an operator can confirm "we looked at this
  // client and cleared it" rather than wondering why a chatty host
  // never appears at all.
  const [showAllowlisted, setShowAllowlisted] = useState(false);
  const [inspecting, setInspecting] = useState<string | null>(null);
  const [muting, setMuting] = useState<string | null>(null);
  const qc = useQueryClient();
  const refreshAll = () => {
    void qc.invalidateQueries({ queryKey: ["dns-threat-windows"] });
    void qc.invalidateQueries({ queryKey: ["dns-threat-summary"] });
  };
  const unmute = useMutation({
    mutationFn: (ip: string) => dnsThreatApi.unmute(ip),
    onSuccess: refreshAll,
  });

  const q = useQuery({
    queryKey: ["dns-threat-windows", hours, minScore, showAllowlisted],
    queryFn: () =>
      dnsThreatApi.listWindows({
        hours,
        min_score: minScore,
        include_allowlisted: showAllowlisted,
        limit: 200,
      }),
    retry: false,
  });
  const summaryQ = useQuery({
    queryKey: ["dns-threat-summary", hours],
    queryFn: () => dnsThreatApi.summary({ hours }),
    retry: false,
  });

  // A 404 here means the feature module is off, not that something
  // broke — the router include is module-gated.
  const moduleOff =
    (q.error as { response?: { status?: number } } | null)?.response?.status ===
    404;

  const rows = q.data ?? [];
  const summary = summaryQ.data;

  if (moduleOff) {
    return (
      <div className="rounded-lg border bg-card p-6">
        <div className="flex items-start gap-3">
          <ShieldQuestion className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" />
          <div>
            <h3 className="text-sm font-semibold">
              DNS threat analytics is off
            </h3>
            <p className="mt-1 max-w-2xl text-xs text-muted-foreground">
              Scoring reads the <strong>names your clients look up</strong>, so
              it is disabled by default rather than switched on by an upgrade.
              Enable the <strong>Security → DNS threat analytics</strong> module
              in Settings to turn it on. It also needs query logging enabled on
              at least one DNS server group before there is anything to score.
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-3 rounded-lg border bg-card px-4 py-3">
        <div>
          <label className="mb-1 block text-xs font-medium">Window</label>
          <select
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
            className="rounded-md border bg-background px-2 py-1.5 text-sm"
          >
            <option value={6}>Last 6 hours</option>
            <option value={24}>Last 24 hours</option>
            <option value={72}>Last 3 days</option>
            <option value={168}>Last 7 days</option>
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium">
            Minimum score
          </label>
          <select
            value={minScore}
            onChange={(e) => setMinScore(Number(e.target.value))}
            className="rounded-md border bg-background px-2 py-1.5 text-sm"
          >
            <option value={0}>Everything (noisy)</option>
            <option value={20}>20 — worth a look</option>
            <option value={40}>40 — suspicious</option>
            <option value={60}>60 — likely tunnel</option>
          </select>
        </div>
        <label className="flex items-center gap-1.5 text-xs">
          <input
            type="checkbox"
            checked={showAllowlisted}
            onChange={(e) => setShowAllowlisted(e.target.checked)}
          />
          Show cleared (allowlisted)
        </label>
        <button
          type="button"
          onClick={() => {
            void q.refetch();
            void summaryQ.refetch();
          }}
          className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
        >
          <RefreshCw className="h-4 w-4" />
          Refresh
        </button>

        {summary && (
          <div className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
            {summary.has_data ? (
              <>
                <ShieldCheck className="h-4 w-4" />
                {summary.windows_scored} windows · {summary.clients_seen}{" "}
                clients scored
              </>
            ) : (
              <>
                <AlertTriangle className="h-4 w-4 text-amber-500" />
                No windows scored yet
              </>
            )}
          </div>
        )}
      </div>

      {q.isLoading ? (
        <div className="flex items-center gap-2 rounded-lg border bg-card px-4 py-6 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Scoring&hellip;
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-lg border bg-card p-6">
          <div className="flex items-start gap-3">
            {summary?.has_data ? (
              <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-emerald-600 dark:text-emerald-400" />
            ) : (
              <ShieldQuestion className="mt-0.5 h-5 w-5 shrink-0 text-amber-500" />
            )}
            <div>
              <h3 className="text-sm font-semibold">
                {summary?.has_data
                  ? "Nothing scoring above the threshold"
                  : "No data to score yet"}
              </h3>
              <p className="mt-1 max-w-2xl text-xs text-muted-foreground">
                {summary?.has_data ? (
                  <>
                    {summary.clients_seen} client(s) were scored over this
                    window and none crossed {minScore}. Lower the minimum score
                    to see the full picture.
                  </>
                ) : (
                  <>
                    The rollup has scored nothing in this period, which is{" "}
                    <strong>not the same as an all-clear</strong>. Check that a
                    DNS server group has query logging enabled — without it
                    there are no queries to analyse.
                  </>
                )}
              </p>
            </div>
          </div>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border bg-card">
          <table className="w-full min-w-[820px] text-sm">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="px-4 py-2 font-medium">Score</th>
                <th className="px-4 py-2 font-medium">Client</th>
                <th className="px-4 py-2 font-medium">Window</th>
                <th className="px-4 py-2 font-medium">Queries</th>
                <th className="px-4 py-2 font-medium">Concentrated on</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {rows.map((w) => (
                <Row
                  key={w.id}
                  w={w}
                  open={expanded.has(w.id)}
                  onToggle={() =>
                    setExpanded((prev) => {
                      const next = new Set(prev);
                      if (next.has(w.id)) next.delete(w.id);
                      else next.add(w.id);
                      return next;
                    })
                  }
                  onInspect={() => setInspecting(w.client_ip)}
                  onMute={() => setMuting(w.client_ip)}
                  onUnmute={() => unmute.mutate(w.client_ip)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {muting && (
        <MuteModal
          clientIp={muting}
          onClose={() => setMuting(null)}
          onDone={refreshAll}
        />
      )}

      {inspecting && (
        <ClientDetailModal
          clientIp={inspecting}
          hours={hours}
          onClose={() => setInspecting(null)}
        />
      )}
    </div>
  );
}

function Row({
  w,
  open,
  onToggle,
  onInspect,
  onMute,
  onUnmute,
}: {
  w: DNSClientWindow;
  open: boolean;
  onToggle: () => void;
  onInspect: () => void;
  onMute: () => void;
  onUnmute: () => void;
}) {
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <>
      {/* The whole row toggles the evidence panel — scanning a list of
          findings means opening and closing several in a row, and
          hunting for a small button each time is friction. The button
          stays as the visible affordance that the row does something. */}
      <tr
        onClick={onToggle}
        className={`cursor-pointer border-b last:border-0 hover:bg-accent/40 ${
          w.muted ? "opacity-50" : ""
        }`}
      >
        <td className="px-4 py-2">
          <ScoreCell score={w.tunnel_score} />
        </td>
        <td className="px-4 py-2 font-mono text-xs">
          {w.client_ip}
          {w.muted && (
            <span
              className="ml-1.5 inline-flex items-center gap-1 rounded bg-muted px-1 py-0.5 font-sans text-[10px] text-muted-foreground"
              title={w.mute_reason ?? undefined}
            >
              <BellOff className="h-2.5 w-2.5" />
              muted
            </span>
          )}
        </td>
        <td className="px-4 py-2 text-xs text-muted-foreground">
          {new Date(w.window_start).toLocaleString()}
        </td>
        <td className="px-4 py-2 text-xs">
          {w.query_count.toLocaleString()}
          <span className="ml-1 text-muted-foreground">
            ({w.distinct_qnames.toLocaleString()} unique)
          </span>
        </td>
        <td className="px-4 py-2 text-xs">
          {w.top_parent ? (
            <>
              <span className="font-mono">{w.top_parent}</span>
              <span className="ml-1 text-muted-foreground">
                ({w.top_parent_subdomains} subdomains)
              </span>
            </>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </td>
        {/* Clicks inside the action cell must not also fire the row's
            toggle, or opening the modal would leave the panel flapping. */}
        <td className="px-4 py-2" onClick={(e) => e.stopPropagation()}>
          <div className="flex justify-end gap-1.5">
            {w.muted ? (
              <button
                type="button"
                onClick={onUnmute}
                className="inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs hover:bg-accent"
              >
                Unmute
              </button>
            ) : (
              <button
                type="button"
                onClick={onMute}
                className="inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs hover:bg-accent"
              >
                <BellOff className="h-3 w-3" />
                Mute
              </button>
            )}
            <button
              type="button"
              onClick={onInspect}
              className="inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs hover:bg-accent"
            >
              <Search className="h-3 w-3" />
              Details
            </button>
            <button
              type="button"
              onClick={onToggle}
              className="inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs hover:bg-accent"
            >
              <Chevron className="h-3 w-3" />
              Why
            </button>
          </div>
        </td>
      </tr>
      {open && (
        <tr className="border-b last:border-0">
          <td colSpan={6} className="bg-muted/30 px-4 py-3">
            {w.muted && w.mute_reason && (
              <p className="mb-2 rounded border border-dashed px-2 py-1 text-xs text-muted-foreground">
                <strong>Muted:</strong> {w.mute_reason}
                {w.mute_until &&
                  ` (until ${new Date(w.mute_until).toLocaleDateString()})`}
              </p>
            )}
            <p className="mb-2 text-xs text-muted-foreground">
              Every signal is listed, including those contributing nothing —
              what was <em>ruled out</em> matters as much as what fired.
            </p>
            <ul className="max-w-3xl">
              {w.tunnel_signals.map((s) => (
                <SignalBar key={s.name} signal={s} />
              ))}
            </ul>
            <p className="mt-2 text-xs text-muted-foreground">
              Longest label {w.max_label_length} chars · mean entropy{" "}
              {w.mean_label_entropy.toFixed(2)} bits/char ·{" "}
              {w.payload_qtype_count} payload-bearing qtypes · seen by{" "}
              {w.server_count} server(s)
            </p>
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * Per-client deep dive.
 *
 * Exists for the one question the inline evidence panel structurally
 * cannot answer: **is this sustained?** A single hour at 70 is a
 * curiosity; six consecutive hours at 70 is someone's data leaving the
 * building. The row shows one window; this shows the client's whole
 * retained history, newest first, so the shape is visible at a glance.
 */
function ClientDetailModal({
  clientIp,
  hours,
  onClose,
}: {
  clientIp: string;
  hours: number;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ["dns-threat-client", clientIp],
    // Per-client fetch returns that client's full history regardless of
    // score — a window that dropped back to 0 is part of the story.
    queryFn: () =>
      dnsThreatApi.listWindows({
        client_ip: clientIp,
        hours: Math.max(hours, 24 * 7),
        include_allowlisted: true,
        limit: 200,
      }),
  });
  const rows = q.data ?? [];
  const peak = rows.reduce((m, r) => Math.max(m, r.tunnel_score), 0);
  const totalQueries = rows.reduce((n, r) => n + r.query_count, 0);
  const scored = rows.filter((r) => r.tunnel_score > 0);

  return (
    <Modal title={`DNS behaviour — ${clientIp}`} onClose={onClose} wide>
      {q.isLoading ? (
        <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading history&hellip;
        </div>
      ) : rows.length === 0 ? (
        <p className="py-6 text-sm text-muted-foreground">
          No retained windows for this client.
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Peak score" value={peak.toFixed(0)} />
            <Stat label="Windows retained" value={String(rows.length)} />
            <Stat label="Windows scoring" value={String(scored.length)} />
            <Stat label="Queries seen" value={totalQueries.toLocaleString()} />
          </div>

          <div>
            <h4 className="mb-1 text-xs font-semibold">Score over time</h4>
            <p className="mb-2 text-xs text-muted-foreground">
              Newest first. A run of consecutive scoring hours is the signal
              that matters — one-off spikes are usually noise.
            </p>
            <ul className="max-h-52 overflow-y-auto rounded border">
              {rows.map((r) => (
                <li
                  key={r.id}
                  className="flex items-center gap-2 border-b px-2 py-1 text-xs last:border-0"
                >
                  <span className="w-32 shrink-0 text-muted-foreground">
                    {new Date(r.window_start).toLocaleString()}
                  </span>
                  <span className="h-1.5 flex-1 overflow-hidden rounded bg-muted">
                    <span
                      className={
                        r.tunnel_score >= 60
                          ? "block h-full bg-rose-500"
                          : r.tunnel_score >= 20
                            ? "block h-full bg-amber-500"
                            : "block h-full bg-emerald-500/50"
                      }
                      style={{ width: `${Math.max(2, r.tunnel_score)}%` }}
                    />
                  </span>
                  <span className="w-8 shrink-0 text-right font-mono">
                    {r.tunnel_score.toFixed(0)}
                  </span>
                  <span className="w-40 shrink-0 truncate text-muted-foreground">
                    {r.allowlisted ? "cleared (allowlisted)" : r.top_parent}
                  </span>
                </li>
              ))}
            </ul>
          </div>

          <div>
            <h4 className="mb-1 text-xs font-semibold">
              Signals — most recent window
            </h4>
            <ul>
              {(rows[0]?.tunnel_signals ?? []).map((sig) => (
                <SignalBar key={sig.name} signal={sig} />
              ))}
            </ul>
          </div>

          <p className="text-xs text-muted-foreground">
            To see the actual names this client asked for, open the{" "}
            <strong>DNS Queries</strong> tab and filter on{" "}
            <span className="font-mono">{clientIp}</span>.
          </p>
        </div>
      )}
    </Modal>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border px-2 py-1.5">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
    </div>
  );
}

/**
 * Clear a reviewed finding.
 *
 * A mute on the CLIENT, not a delete of the rows — deleting wouldn't
 * stick (the aggregator recomputes recent hours and upserts) and would
 * destroy evidence of what may be an exfiltration event. The reason is
 * required, because an unexplained mute is how a real incident gets
 * buried by someone tidying a dashboard.
 */
function MuteModal({
  clientIp,
  onClose,
  onDone,
}: {
  clientIp: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [reason, setReason] = useState("");
  // Default to an expiring mute: a snap "this is fine" should age out
  // and get re-examined rather than silently outlive whoever made it.
  const [days, setDays] = useState<number | "forever">(30);

  const save = useMutation({
    mutationFn: () =>
      dnsThreatApi.mute({
        client_ip: clientIp,
        reason,
        muted_until:
          days === "forever"
            ? null
            : new Date(Date.now() + days * 86400_000).toISOString(),
      }),
    onSuccess: () => {
      onDone();
      onClose();
    },
  });

  return (
    <Modal title={`Mute findings for ${clientIp}`} onClose={onClose}>
      <div className="flex flex-col gap-3">
        <p className="text-xs text-muted-foreground">
          Hides this client from the Threat tab and stops the{" "}
          <strong>DNS tunneling suspected</strong> alert firing for it. The
          scored windows and their evidence are kept — muting records a
          decision, it doesn&rsquo;t delete anything.
        </p>

        <div>
          <label className="mb-1 block text-xs font-medium">
            Why is this expected? <span className="text-rose-500">*</span>
          </label>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={3}
            placeholder="e.g. Veeam backup agent — chunks payload into subdomains by design"
            className="w-full rounded-md border bg-background px-2 py-1.5 text-sm"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Required. The next person to look at this host reads this instead of
            re-investigating it.
          </p>
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium">Mute for</label>
          <select
            value={String(days)}
            onChange={(e) =>
              setDays(
                e.target.value === "forever"
                  ? "forever"
                  : Number(e.target.value),
              )
            }
            className="w-full rounded-md border bg-background px-2 py-1.5 text-sm"
          >
            <option value={7}>7 days</option>
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
            <option value="forever">Indefinitely</option>
          </select>
        </div>

        {save.isError && (
          <p className="text-xs text-rose-600 dark:text-rose-400">
            {(save.error as Error).message}
          </p>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={reason.trim().length < 3 || save.isPending}
            onClick={() => save.mutate()}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {save.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            Mute client
          </button>
        </div>
      </div>
    </Modal>
  );
}
