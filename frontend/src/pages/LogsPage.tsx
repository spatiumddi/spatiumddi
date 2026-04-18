import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  Ban,
  Calendar,
  ChevronDown,
  ChevronRight,
  FileClock,
  Info,
  RefreshCw,
  Search,
  ScrollText,
  XCircle,
} from "lucide-react";
import {
  logsApi,
  type DhcpAuditDay,
  type DhcpAuditRow,
  type LogEventRow,
  type LogSource,
} from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Logs page — central log viewer.
 *
 * Two tabs, two different data sources:
 *
 *   • **Event Log** — Windows Event Log (admin / operational events
 *     from DNS + DHCP server roles). Pulled via
 *     ``Get-WinEvent -FilterHashtable`` over WinRM. Applies to both
 *     DNS and DHCP servers.
 *
 *   • **DHCP Audit** — per-lease events (grants, renewals, releases,
 *     conflicts, DNS update results) parsed from
 *     ``C:\Windows\System32\dhcp\DhcpSrvLog-<Day>.log``. DHCP only.
 *     This is the actual "DHCP client requests" view; the Event Log
 *     tab covers service-level events like scope activation.
 *
 * Both tabs auto-fetch on mount and on filter change; staleTime is
 * Infinity so tab-switching doesn't re-hit the DC.
 */

type Tab = "events" | "audit";

export function LogsPage() {
  const [tab, setTab] = useState<Tab>("events");

  // Discovery — who can we pull logs from? Shared across tabs.
  const {
    data: sources,
    isLoading: sourcesLoading,
    refetch: refetchSources,
  } = useQuery({
    queryKey: ["logs-sources"],
    queryFn: logsApi.listSources,
    staleTime: 60_000,
  });

  // Filter by tab — audit tab is DHCP-only.
  const tabSources = useMemo(() => {
    if (!sources) return [];
    if (tab === "audit") return sources.filter((s) => s.server_kind === "dhcp");
    return sources;
  }, [sources, tab]);

  if (sourcesLoading) {
    return (
      <div className="p-6 text-sm text-muted-foreground">Loading sources…</div>
    );
  }

  if (!sources || sources.length === 0) {
    return <EmptyLogsState />;
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header + tab switcher */}
      <div className="border-b px-6 py-4">
        <div className="flex items-end justify-between gap-4">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-bold tracking-tight">
              <ScrollText className="h-5 w-5" />
              Logs
            </h1>
            <p className="mt-1 text-xs text-muted-foreground">
              Read-only. Windows DNS / DHCP today; agent + control-plane logs
              coming.
            </p>
          </div>
          <button
            onClick={() => refetchSources()}
            title="Refresh source list"
            className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-accent"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Sources
          </button>
        </div>
        <div className="mt-3 flex gap-1 border-b -mb-4">
          <TabButton
            active={tab === "events"}
            onClick={() => setTab("events")}
            icon={ScrollText}
            label="Event Log"
            hint="Service events (scope load, zone transfer, auth)"
          />
          <TabButton
            active={tab === "audit"}
            onClick={() => setTab("audit")}
            icon={FileClock}
            label="DHCP Audit"
            hint="Per-lease events (grants, renewals, releases)"
            count={
              sources.filter((s) => s.server_kind === "dhcp").length ||
              undefined
            }
          />
        </div>
      </div>

      {tab === "events" && <EventLogTab sources={tabSources} />}
      {tab === "audit" && <DhcpAuditTab sources={tabSources} />}
    </div>
  );
}

function TabButton({
  active,
  onClick,
  icon: Icon,
  label,
  hint,
  count,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ElementType;
  label: string;
  hint?: string;
  count?: number;
}) {
  return (
    <button
      onClick={onClick}
      title={hint}
      className={cn(
        "flex items-center gap-1.5 border-b-2 px-3 pb-2 pt-1.5 text-xs font-medium transition-colors",
        active
          ? "border-primary text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground",
      )}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
      {count !== undefined && (
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] tabular-nums">
          {count}
        </span>
      )}
    </button>
  );
}

function EmptyLogsState() {
  return (
    <div className="flex h-full flex-col">
      <div className="border-b px-6 py-4">
        <h1 className="flex items-center gap-2 text-xl font-bold tracking-tight">
          <ScrollText className="h-5 w-5" />
          Logs
        </h1>
      </div>
      <div className="flex flex-1 items-center justify-center p-10">
        <div className="max-w-md rounded-lg border border-dashed p-10 text-center">
          <ScrollText className="mx-auto mb-3 h-10 w-10 text-muted-foreground/30" />
          <p className="text-sm font-medium">No log sources yet</p>
          <p className="mt-2 text-xs text-muted-foreground">
            Register a Windows DNS or DHCP server with WinRM credentials
            configured and its logs will appear here.
          </p>
        </div>
      </div>
    </div>
  );
}

// ── Event Log tab ───────────────────────────────────────────────────────────

function EventLogTab({ sources }: { sources: LogSource[] }) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [logName, setLogName] = useState<string | null>(null);
  const [maxEvents, setMaxEvents] = useState(100);
  const [level, setLevel] = useState<number | "">("");
  const [since, setSince] = useState<string>("");
  const [search, setSearch] = useState("");

  useEffect(() => {
    if (sources.length === 0) {
      setSelectedKey(null);
      setLogName(null);
      return;
    }
    if (selectedKey) return;
    const first = sources[0];
    setSelectedKey(`${first.server_kind}:${first.server_id}`);
    if (first.logs.length > 0) setLogName(first.logs[0].name);
  }, [sources, selectedKey]);

  const selected = useMemo(
    () =>
      sources.find((s) => `${s.server_kind}:${s.server_id}` === selectedKey),
    [selectedKey, sources],
  );

  useEffect(() => {
    if (!selected) return;
    if (!logName || !selected.logs.some((l) => l.name === logName)) {
      setLogName(selected.logs[0]?.name ?? null);
    }
  }, [selected, logName]);

  const sinceIso = since ? new Date(since).toISOString() : null;
  const enabled = !!selected && !!logName;

  const eventsQuery = useQuery({
    queryKey: [
      "logs-query",
      selected?.server_id,
      selected?.server_kind,
      logName,
      level,
      maxEvents,
      sinceIso,
    ],
    queryFn: () =>
      logsApi.query({
        server_id: selected!.server_id,
        server_kind: selected!.server_kind,
        log_name: logName!,
        max_events: maxEvents,
        level: level === "" ? null : level,
        since: sinceIso,
        event_id: null,
      }),
    enabled,
    staleTime: Infinity,
    gcTime: 5 * 60_000,
    retry: false,
  });

  const events = eventsQuery.data?.events ?? [];
  const filteredEvents = useMemo(() => {
    if (!search.trim()) return events;
    const q = search.trim().toLowerCase();
    return events.filter(
      (e) =>
        e.message.toLowerCase().includes(q) ||
        e.provider.toLowerCase().includes(q) ||
        String(e.id).includes(q),
    );
  }, [events, search]);

  return (
    <>
      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-3 border-b bg-muted/20 px-6 py-3">
        <ServerPicker
          sources={sources}
          value={selectedKey}
          onChange={(key) => {
            setSelectedKey(key);
            const s = sources.find(
              (src) => `${src.server_kind}:${src.server_id}` === key,
            );
            setLogName(s?.logs?.[0]?.name ?? null);
          }}
        />
        {selected && (
          <label className="flex items-center gap-1.5 text-xs">
            <span className="text-muted-foreground">Log</span>
            <select
              value={logName ?? ""}
              onChange={(e) => setLogName(e.target.value)}
              className="min-w-[240px] rounded-md border bg-background px-2 py-1 text-sm"
            >
              {selected.logs.map((l) => (
                <option key={l.name} value={l.name}>
                  {l.display}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="flex items-center gap-1.5 text-xs">
          <span className="text-muted-foreground">Level</span>
          <select
            value={level}
            onChange={(e) =>
              setLevel(
                e.target.value === "" ? "" : parseInt(e.target.value, 10),
              )
            }
            className="rounded-md border bg-background px-2 py-1 text-sm"
          >
            <option value="">All</option>
            <option value="1">Critical</option>
            <option value="2">Error</option>
            <option value="3">Warning</option>
            <option value="4">Information</option>
            <option value="5">Verbose</option>
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-xs">
          <span className="inline-flex items-center gap-1 text-muted-foreground">
            <Calendar className="h-3 w-3" />
            Since
          </span>
          <input
            type="datetime-local"
            value={since}
            onChange={(e) => setSince(e.target.value)}
            className="rounded-md border bg-background px-2 py-1 text-sm"
          />
          {since && (
            <button
              type="button"
              onClick={() => setSince("")}
              title="Clear since filter"
              className="rounded text-[11px] text-muted-foreground hover:text-foreground"
            >
              ✕
            </button>
          )}
        </label>
        <MaxEventsPicker value={maxEvents} onChange={setMaxEvents} />
        <FilterSearch value={search} onChange={setSearch} />
        <RefreshButton query={eventsQuery} enabled={enabled} />
      </div>

      {/* Results */}
      <div className="flex-1 overflow-auto">
        {eventsQuery.isError && <QueryErrorBanner query={eventsQuery} />}
        {enabled && eventsQuery.isLoading && (
          <QueryingIndicator host={selected?.host} />
        )}
        {eventsQuery.data && events.length === 0 && !eventsQuery.isError && (
          <EmptyResults hasSince={!!since} />
        )}
        {events.length > 0 && (
          <div className="divide-y">
            <div className="sticky top-0 z-10 grid grid-cols-[140px_52px_60px_140px_1fr] gap-3 border-b bg-muted/40 px-6 py-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              <span>Time</span>
              <span>Level</span>
              <span className="text-right">Event</span>
              <span>Provider</span>
              <span>Message</span>
            </div>
            {filteredEvents.map((ev, idx) => (
              <EventRow key={`${ev.time}-${ev.id}-${idx}`} ev={ev} />
            ))}
            {eventsQuery.data?.truncated && <TruncatedNotice max={maxEvents} />}
          </div>
        )}
      </div>
    </>
  );
}

// ── DHCP Audit tab ──────────────────────────────────────────────────────────

const WEEKDAYS: DhcpAuditDay[] = [
  "Mon",
  "Tue",
  "Wed",
  "Thu",
  "Fri",
  "Sat",
  "Sun",
];

function DhcpAuditTab({ sources }: { sources: LogSource[] }) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [day, setDay] = useState<DhcpAuditDay | "today">("today");
  const [maxEvents, setMaxEvents] = useState(500);
  const [codeFilter, setCodeFilter] = useState<number | "">("");
  const [search, setSearch] = useState("");

  useEffect(() => {
    if (sources.length === 0) {
      setSelectedKey(null);
      return;
    }
    if (selectedKey) return;
    const first = sources[0];
    setSelectedKey(`${first.server_kind}:${first.server_id}`);
  }, [sources, selectedKey]);

  const selected = useMemo(
    () =>
      sources.find((s) => `${s.server_kind}:${s.server_id}` === selectedKey),
    [selectedKey, sources],
  );

  const enabled = !!selected;
  const auditQuery = useQuery({
    queryKey: ["dhcp-audit", selected?.server_id, day, maxEvents],
    queryFn: () =>
      logsApi.dhcpAudit({
        server_id: selected!.server_id,
        day: day === "today" ? null : day,
        max_events: maxEvents,
      }),
    enabled,
    staleTime: Infinity,
    gcTime: 5 * 60_000,
    retry: false,
  });

  const allRows = auditQuery.data?.events ?? [];
  // Newest first for display — the file is oldest → newest.
  const rows = useMemo(() => [...allRows].reverse(), [allRows]);
  const filtered = useMemo(() => {
    let out = rows;
    if (codeFilter !== "") {
      out = out.filter((r) => r.event_code === codeFilter);
    }
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      out = out.filter(
        (r) =>
          r.ip_address.toLowerCase().includes(q) ||
          r.hostname.toLowerCase().includes(q) ||
          r.mac_address.toLowerCase().includes(q) ||
          r.description.toLowerCase().includes(q) ||
          r.event_label.toLowerCase().includes(q),
      );
    }
    return out;
  }, [rows, codeFilter, search]);

  // Build a code → count map for the picker, so it shows event-code
  // distribution for the current day without needing a second query.
  const codeCounts = useMemo(() => {
    const m = new Map<number, number>();
    for (const r of allRows)
      m.set(r.event_code, (m.get(r.event_code) ?? 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [allRows]);

  if (sources.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center p-10">
        <div className="max-w-md rounded-lg border border-dashed p-10 text-center">
          <FileClock className="mx-auto mb-3 h-10 w-10 text-muted-foreground/30" />
          <p className="text-sm font-medium">No DHCP servers configured</p>
          <p className="mt-2 text-xs text-muted-foreground">
            DHCP audit logs require a Windows DHCP server with WinRM
            credentials. Register one under DHCP → Server Groups first.
          </p>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="flex flex-wrap items-center gap-3 border-b bg-muted/20 px-6 py-3">
        <ServerPicker
          sources={sources}
          value={selectedKey}
          onChange={setSelectedKey}
        />
        <label className="flex items-center gap-1.5 text-xs">
          <span className="inline-flex items-center gap-1 text-muted-foreground">
            <Calendar className="h-3 w-3" />
            Day
          </span>
          <select
            value={day}
            onChange={(e) => setDay(e.target.value as DhcpAuditDay | "today")}
            className="rounded-md border bg-background px-2 py-1 text-sm"
          >
            <option value="today">Today</option>
            {WEEKDAYS.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
          <span className="text-[10px] text-muted-foreground/70">
            // Windows keeps 7 daily logs
          </span>
        </label>
        <label className="flex items-center gap-1.5 text-xs">
          <span className="text-muted-foreground">Event</span>
          <select
            value={codeFilter}
            onChange={(e) =>
              setCodeFilter(
                e.target.value === "" ? "" : parseInt(e.target.value, 10),
              )
            }
            className="min-w-[220px] rounded-md border bg-background px-2 py-1 text-sm"
          >
            <option value="">All ({allRows.length})</option>
            {codeCounts.map(([code, count]) => {
              const label = allRows.find(
                (r) => r.event_code === code,
              )?.event_label;
              return (
                <option key={code} value={code}>
                  {code} — {label} ({count})
                </option>
              );
            })}
          </select>
        </label>
        <MaxEventsPicker
          value={maxEvents}
          onChange={setMaxEvents}
          options={[100, 250, 500, 1000, 2000]}
        />
        <FilterSearch value={search} onChange={setSearch} />
        <RefreshButton query={auditQuery} enabled={enabled} />
      </div>

      <div className="flex-1 overflow-auto">
        {auditQuery.isError && <QueryErrorBanner query={auditQuery} />}
        {enabled && auditQuery.isLoading && (
          <QueryingIndicator host={selected?.host} />
        )}
        {auditQuery.data && allRows.length === 0 && !auditQuery.isError && (
          <EmptyAuditResults day={day === "today" ? "today" : day} />
        )}
        {filtered.length > 0 && (
          <div className="divide-y">
            <div className="sticky top-0 z-10 grid grid-cols-[140px_180px_120px_160px_140px_1fr] gap-3 border-b bg-muted/40 px-6 py-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              <span>Time</span>
              <span>Event</span>
              <span>IP</span>
              <span>Hostname</span>
              <span>MAC</span>
              <span>Description</span>
            </div>
            {filtered.map((r, idx) => (
              <AuditRow key={`${r.time}-${r.transaction_id}-${idx}`} row={r} />
            ))}
            {auditQuery.data?.truncated && <TruncatedNotice max={maxEvents} />}
          </div>
        )}
      </div>
    </>
  );
}

// ── Shared filter-bar + result chrome ───────────────────────────────────────

function ServerPicker({
  sources,
  value,
  onChange,
}: {
  sources: LogSource[];
  value: string | null;
  onChange: (v: string) => void;
}) {
  return (
    <label className="flex items-center gap-1.5 text-xs">
      <span className="text-muted-foreground">Server</span>
      <select
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-md border bg-background px-2 py-1 text-sm"
      >
        {sources.map((s) => (
          <option
            key={`${s.server_kind}:${s.server_id}`}
            value={`${s.server_kind}:${s.server_id}`}
          >
            {s.server_kind.toUpperCase()} · {s.server_name} ({s.host})
          </option>
        ))}
      </select>
    </label>
  );
}

function MaxEventsPicker({
  value,
  onChange,
  options = [50, 100, 200, 500],
}: {
  value: number;
  onChange: (v: number) => void;
  options?: number[];
}) {
  return (
    <label className="flex items-center gap-1.5 text-xs">
      <span className="text-muted-foreground">Max</span>
      <select
        value={value}
        onChange={(e) => onChange(parseInt(e.target.value, 10))}
        className="rounded-md border bg-background px-2 py-1 text-sm"
      >
        {options.map((n) => (
          <option key={n} value={n}>
            {n}
          </option>
        ))}
      </select>
    </label>
  );
}

function FilterSearch({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="relative flex-1 min-w-[180px] max-w-[320px]">
      <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Filter rendered events…"
        className="w-full rounded-md border bg-background pl-7 pr-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
      />
    </div>
  );
}

function RefreshButton({
  query,
  enabled,
}: {
  query: { isFetching: boolean; refetch: () => unknown };
  enabled: boolean;
}) {
  return (
    <button
      onClick={() => query.refetch()}
      disabled={!enabled || query.isFetching}
      title="Re-run the query (bypasses cache)"
      className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
    >
      <RefreshCw
        className={cn("h-3.5 w-3.5", query.isFetching && "animate-spin")}
      />
      {query.isFetching ? "Querying…" : "Refresh"}
    </button>
  );
}

function QueryErrorBanner({ query }: { query: { error: unknown } }) {
  const detail =
    (
      query.error as
        | {
            response?: { data?: { detail?: string } };
          }
        | undefined
    )?.response?.data?.detail ?? String(query.error);
  return (
    <div className="m-6 rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">
      <p className="font-medium">Query failed</p>
      <p className="mt-1 whitespace-pre-wrap break-words text-xs">{detail}</p>
    </div>
  );
}

function QueryingIndicator({ host }: { host: string | undefined }) {
  return (
    <div className="flex h-full items-center justify-center">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <RefreshCw className="h-4 w-4 animate-spin" />
        Querying {host}…
      </div>
    </div>
  );
}

function EmptyResults({ hasSince }: { hasSince: boolean }) {
  return (
    <div className="m-6 rounded-lg border border-dashed p-8 text-center">
      <p className="text-sm font-medium">No events match your filter.</p>
      <p className="mt-1 text-xs text-muted-foreground">
        {hasSince
          ? "Try widening the time window or raising Max."
          : "Try widening the level filter or raising Max."}
      </p>
    </div>
  );
}

function EmptyAuditResults({ day }: { day: string }) {
  return (
    <div className="m-6 rounded-lg border border-dashed p-8 text-center">
      <p className="text-sm font-medium">
        No DHCP audit events for {day === "today" ? "today" : `the ${day} log`}.
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        The log file may be empty, absent (DHCP service inactive on this
        weekday), or the DHCP role may not be enabled on this host. Windows
        keeps one rotating file per weekday in{" "}
        <code className="rounded bg-muted px-1">C:\Windows\System32\dhcp</code>.
      </p>
    </div>
  );
}

function TruncatedNotice({ max }: { max: number }) {
  return (
    <div className="bg-amber-50/40 px-6 py-2 text-xs text-amber-700 dark:bg-amber-950/20 dark:text-amber-400">
      Result limit reached ({max}). Raise Max or narrow the filters to see older
      events.
    </div>
  );
}

// ── Row renderers ───────────────────────────────────────────────────────────

function levelStyling(level: string): {
  icon: React.ElementType;
  color: string;
  bg: string;
} {
  const l = level.toLowerCase();
  if (l.startsWith("crit") || l === "critical") {
    return {
      icon: XCircle,
      color: "text-red-600 dark:text-red-400",
      bg: "bg-red-500",
    };
  }
  if (l === "error") {
    return {
      icon: Ban,
      color: "text-red-600 dark:text-red-400",
      bg: "bg-red-500",
    };
  }
  if (l === "warning") {
    return {
      icon: AlertTriangle,
      color: "text-amber-600 dark:text-amber-400",
      bg: "bg-amber-500",
    };
  }
  if (l === "verbose") {
    return {
      icon: Info,
      color: "text-muted-foreground",
      bg: "bg-muted-foreground/50",
    };
  }
  return {
    icon: Info,
    color: "text-blue-600 dark:text-blue-400",
    bg: "bg-blue-500",
  };
}

function EventRow({ ev }: { ev: LogEventRow }) {
  const [expanded, setExpanded] = useState(false);
  const { icon: LevelIcon, color, bg } = levelStyling(ev.level);
  const firstLine = ev.message.split("\n")[0];
  const hasMore = ev.message.includes("\n") || ev.message.length > 120;
  return (
    <div className="grid grid-cols-[140px_52px_60px_140px_1fr] items-start gap-3 px-6 py-2 text-xs hover:bg-accent/30">
      <span className="font-mono text-muted-foreground">
        {formatTime(ev.time)}
      </span>
      <span className="flex items-center gap-1.5">
        <span className={cn("inline-block h-1.5 w-1.5 rounded-full", bg)} />
        <LevelIcon className={cn("h-3 w-3", color)} />
      </span>
      <span className="text-right tabular-nums text-muted-foreground">
        {ev.id}
      </span>
      <span className="truncate text-muted-foreground" title={ev.provider}>
        {ev.provider.replace(/^Microsoft-Windows-/, "")}
      </span>
      <div className="min-w-0">
        <button
          onClick={() => hasMore && setExpanded((v) => !v)}
          className={cn(
            "flex w-full items-start gap-1.5 text-left",
            hasMore ? "cursor-pointer hover:text-foreground" : "cursor-default",
          )}
        >
          {hasMore &&
            (expanded ? (
              <ChevronDown className="mt-0.5 h-3 w-3 flex-shrink-0 text-muted-foreground" />
            ) : (
              <ChevronRight className="mt-0.5 h-3 w-3 flex-shrink-0 text-muted-foreground" />
            ))}
          <span
            className={cn(
              "block",
              expanded ? "whitespace-pre-wrap" : "truncate",
            )}
          >
            {expanded ? ev.message : firstLine}
          </span>
        </button>
      </div>
    </div>
  );
}

/** Colour family per DHCP audit event code — roughly mirrors the
 *  Windows Event Log level semantics applied to the DHCP audit code
 *  ranges. 10/11/20 (grants/renew) = green; 12/16/17 (release /
 *  expire) = muted; 13/14/15/18/31/33/34 (failures) = red;
 *  51-57 (auth-related) = blue. */
function auditCodeColor(code: number): string {
  if ([10, 11, 20, 32, 51, 55, 57, 61, 64].includes(code))
    return "bg-emerald-500";
  if ([12, 16, 17, 23, 24].includes(code)) return "bg-muted-foreground/50";
  if (
    [13, 14, 15, 18, 25, 31, 33, 34, 35, 50, 52, 54, 56, 58, 59, 60].includes(
      code,
    )
  )
    return "bg-red-500";
  if ([30, 53, 62, 63].includes(code)) return "bg-blue-500";
  return "bg-muted-foreground/40";
}

function AuditRow({ row }: { row: DhcpAuditRow }) {
  const dot = auditCodeColor(row.event_code);
  return (
    <div className="grid grid-cols-[140px_180px_120px_160px_140px_1fr] items-center gap-3 px-6 py-1.5 text-xs hover:bg-accent/30">
      <span className="font-mono text-muted-foreground">
        {formatTime(row.time)}
      </span>
      <span className="flex items-center gap-1.5 truncate">
        <span className={cn("inline-block h-1.5 w-1.5 rounded-full", dot)} />
        <span
          className="tabular-nums text-muted-foreground"
          title={`Event code ${row.event_code}`}
        >
          {row.event_code}
        </span>
        <span className="truncate font-medium" title={row.event_label}>
          {row.event_label}
        </span>
      </span>
      <span className="truncate font-mono" title={row.ip_address}>
        {row.ip_address || <span className="text-muted-foreground/40">—</span>}
      </span>
      <span className="truncate" title={row.hostname}>
        {row.hostname || <span className="text-muted-foreground/40">—</span>}
      </span>
      <span
        className="truncate font-mono text-muted-foreground"
        title={row.mac_address}
      >
        {row.mac_address || <span className="text-muted-foreground/40">—</span>}
      </span>
      <span className="truncate text-muted-foreground" title={row.description}>
        {row.description || <span className="text-muted-foreground/40">—</span>}
      </span>
    </div>
  );
}

function formatTime(iso: string): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return (
      d.toLocaleDateString(undefined, { month: "short", day: "2-digit" }) +
      " " +
      d.toLocaleTimeString(undefined, { hour12: false })
    );
  } catch {
    return iso;
  }
}
