import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  Ban,
  ChevronDown,
  ChevronRight,
  Globe2,
  Info,
  RefreshCw,
  Search,
  Server,
  ScrollText,
  XCircle,
} from "lucide-react";
import {
  logsApi,
  type LogEventRow,
  type LogQueryRequest,
  type LogSource,
} from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Logs page — central log viewer.
 *
 * MVP scope: Windows Event Log reads over WinRM for agentless DNS /
 * DHCP servers. Future sources (agent logs, control-plane service
 * logs, audit streaming) drop into the same source picker.
 *
 * The UI is deliberately read-only and polling-free by default:
 * `Get-WinEvent` is cheap but not free, and bursting it every 15 s
 * against a production DC for every open tab is antisocial. User
 * hits Refresh to re-fetch.
 */
export function LogsPage() {
  // Discovery — who can we pull logs from?
  const {
    data: sources,
    isLoading: sourcesLoading,
    refetch: refetchSources,
  } = useQuery({
    queryKey: ["logs-sources"],
    queryFn: logsApi.listSources,
    staleTime: 60_000,
  });

  // Selection state — which server, which log, what filters.
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [logName, setLogName] = useState<string | null>(null);
  const [maxEvents, setMaxEvents] = useState(100);
  const [level, setLevel] = useState<number | "">("");
  const [search, setSearch] = useState("");

  // Pick an initial selection once sources land.
  const firstSource = sources?.[0];
  if (!selectedKey && firstSource) {
    const key = `${firstSource.server_kind}:${firstSource.server_id}`;
    setSelectedKey(key);
    if (firstSource.logs.length > 0) setLogName(firstSource.logs[0].name);
  }

  const selected: LogSource | undefined = useMemo(() => {
    if (!selectedKey || !sources) return undefined;
    return sources.find(
      (s) => `${s.server_kind}:${s.server_id}` === selectedKey,
    );
  }, [selectedKey, sources]);

  // Keep the log_name picker valid when the server changes.
  const firstLogName = selected?.logs?.[0]?.name;
  if (selected && logName && !selected.logs.some((l) => l.name === logName)) {
    setLogName(firstLogName ?? null);
  }

  // Query mutation — fired on button press, not on auto-poll.
  const queryMut = useMutation({
    mutationFn: (body: LogQueryRequest) => logsApi.query(body),
  });

  function runQuery() {
    if (!selected || !logName) return;
    queryMut.mutate({
      server_id: selected.server_id,
      server_kind: selected.server_kind,
      log_name: logName,
      max_events: maxEvents,
      level: level === "" ? null : level,
      since: null,
      event_id: null,
    });
  }

  const events = queryMut.data?.events ?? [];
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

  if (sourcesLoading) {
    return (
      <div className="p-6 text-sm text-muted-foreground">Loading sources…</div>
    );
  }

  if (!sources || sources.length === 0) {
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
              configured, and its event logs will appear here. Agent logs +
              control-plane logs are on the roadmap.
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="border-b px-6 py-4">
        <div className="flex items-end justify-between gap-4">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-bold tracking-tight">
              <ScrollText className="h-5 w-5" />
              Logs
            </h1>
            <p className="mt-1 text-xs text-muted-foreground">
              Read-only event log viewer. Windows DNS / DHCP over WinRM today;
              agent + control-plane logs coming.
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
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-3 border-b bg-muted/20 px-6 py-3">
        {/* Server picker */}
        <label className="flex items-center gap-1.5 text-xs">
          <span className="text-muted-foreground">Server</span>
          <select
            value={selectedKey ?? ""}
            onChange={(e) => {
              setSelectedKey(e.target.value);
              const s = sources.find(
                (src) =>
                  `${src.server_kind}:${src.server_id}` === e.target.value,
              );
              setLogName(s?.logs?.[0]?.name ?? null);
              queryMut.reset();
            }}
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

        {/* Log picker */}
        {selected && (
          <label className="flex items-center gap-1.5 text-xs">
            <span className="text-muted-foreground">Log</span>
            <select
              value={logName ?? ""}
              onChange={(e) => {
                setLogName(e.target.value);
                queryMut.reset();
              }}
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

        {/* Level filter */}
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

        {/* Max */}
        <label className="flex items-center gap-1.5 text-xs">
          <span className="text-muted-foreground">Max</span>
          <select
            value={maxEvents}
            onChange={(e) => setMaxEvents(parseInt(e.target.value, 10))}
            className="rounded-md border bg-background px-2 py-1 text-sm"
          >
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={200}>200</option>
            <option value={500}>500</option>
          </select>
        </label>

        {/* Client-side filter */}
        <div className="relative flex-1 min-w-[180px] max-w-[320px]">
          <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Filter rendered events…"
            className="w-full rounded-md border bg-background pl-7 pr-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>

        <button
          onClick={runQuery}
          disabled={queryMut.isPending || !logName}
          className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          <RefreshCw
            className={cn("h-3.5 w-3.5", queryMut.isPending && "animate-spin")}
          />
          {queryMut.isPending ? "Querying…" : "Fetch events"}
        </button>
      </div>

      {/* Results */}
      <div className="flex-1 overflow-auto">
        {queryMut.isError && (
          <div className="m-6 rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">
            <p className="font-medium">Query failed</p>
            <p className="mt-1 text-xs">
              {(queryMut.error as any)?.response?.data?.detail ??
                String(queryMut.error)}
            </p>
          </div>
        )}

        {!queryMut.data && !queryMut.isPending && !queryMut.isError && (
          <div className="flex h-full items-center justify-center">
            <div className="max-w-md rounded-lg border border-dashed p-10 text-center">
              <ServerIcon source={selected} />
              <p className="mt-3 text-sm font-medium">
                Ready — pick a log and click Fetch.
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                Events are pulled on demand (no auto-refresh). This keeps
                PowerShell round-trips off the DC when the tab is idle.
              </p>
            </div>
          </div>
        )}

        {queryMut.data && events.length === 0 && (
          <div className="m-6 rounded-lg border border-dashed p-8 text-center">
            <p className="text-sm font-medium">No events match your filter.</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Try widening the level filter or raising Max.
            </p>
          </div>
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
            {queryMut.data?.truncated && (
              <div className="bg-amber-50/40 px-6 py-2 text-xs text-amber-700 dark:bg-amber-950/20 dark:text-amber-400">
                Result limit reached ({maxEvents}). Raise Max or narrow the
                level / time filters to see older events.
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ServerIcon({ source }: { source: LogSource | undefined }) {
  if (!source)
    return (
      <ScrollText className="mx-auto h-10 w-10 text-muted-foreground/30" />
    );
  const Icon = source.server_kind === "dns" ? Globe2 : Server;
  return <Icon className="mx-auto h-10 w-10 text-muted-foreground/40" />;
}

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
  // Information / default
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
