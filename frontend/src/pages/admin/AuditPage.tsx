import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ClipboardList,
  ChevronLeft,
  ChevronRight,
  Search,
  X,
} from "lucide-react";
import { auditApi, type AuditLogEntry } from "@/lib/api";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 50;

const ACTION_COLORS: Record<string, string> = {
  create: "bg-green-500/15 text-green-600 dark:text-green-400",
  update: "bg-blue-500/15 text-blue-600 dark:text-blue-400",
  delete: "bg-red-500/15 text-red-600 dark:text-red-400",
  reset_password: "bg-yellow-500/15 text-yellow-700 dark:text-yellow-400",
  login: "bg-purple-500/15 text-purple-600 dark:text-purple-400",
  logout: "bg-zinc-500/15 text-zinc-600 dark:text-zinc-400",
};

function ActionBadge({ action }: { action: string }) {
  const cls =
    ACTION_COLORS[action] ?? "bg-zinc-500/15 text-zinc-600 dark:text-zinc-400";
  return (
    <span
      className={cn(
        "inline-flex rounded px-1.5 py-0.5 text-xs font-medium font-mono",
        cls,
      )}
    >
      {action}
    </span>
  );
}

function ResultBadge({ result }: { result: string }) {
  const ok = result === "success";
  return (
    <span
      className={cn(
        "inline-flex rounded px-1.5 py-0.5 text-xs font-medium",
        ok
          ? "bg-green-500/15 text-green-600 dark:text-green-400"
          : "bg-red-500/15 text-red-600 dark:text-red-400",
      )}
    >
      {result}
    </span>
  );
}

function formatTs(ts: string) {
  try {
    const d = new Date(ts);
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

export function AuditPage() {
  const [page, setPage] = useState(0);
  const [actionFilter, setActionFilter] = useState("");
  const [resourceTypeFilter, setResourceTypeFilter] = useState("");
  const [userFilter, setUserFilter] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["audit", page, actionFilter, resourceTypeFilter, userFilter],
    queryFn: () =>
      auditApi.list({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        action: actionFilter || undefined,
        resource_type: resourceTypeFilter || undefined,
        user_display_name: userFilter || undefined,
      }),
  });

  const total = data?.total ?? 0;
  const items = data?.items ?? [];
  const totalPages = Math.ceil(total / PAGE_SIZE);

  function resetFilters() {
    setActionFilter("");
    setResourceTypeFilter("");
    setUserFilter("");
    setPage(0);
  }

  const hasFilters = actionFilter || resourceTypeFilter || userFilter;

  return (
    <div className="h-full overflow-auto p-6">
      <div className="mx-auto max-w-7xl space-y-6">
        {/* Header */}
        <div className="flex items-center gap-3">
          <ClipboardList className="h-6 w-6 text-muted-foreground" />
          <div>
            <h1 className="text-xl font-semibold">Audit Log</h1>
            <p className="text-sm text-muted-foreground">
              All administrative actions recorded by the system
            </p>
          </div>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              value={userFilter}
              onChange={(e) => {
                setUserFilter(e.target.value);
                setPage(0);
              }}
              placeholder="Filter by user…"
              className="h-8 rounded-md border bg-background pl-8 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring w-44"
            />
            {userFilter && (
              <button
                onClick={() => {
                  setUserFilter("");
                  setPage(0);
                }}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>

          <select
            value={actionFilter}
            onChange={(e) => {
              setActionFilter(e.target.value);
              setPage(0);
            }}
            className="h-8 rounded-md border bg-background px-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All actions</option>
            <option value="create">create</option>
            <option value="update">update</option>
            <option value="delete">delete</option>
            <option value="reset_password">reset_password</option>
            <option value="login">login</option>
            <option value="logout">logout</option>
          </select>

          <select
            value={resourceTypeFilter}
            onChange={(e) => {
              setResourceTypeFilter(e.target.value);
              setPage(0);
            }}
            className="h-8 rounded-md border bg-background px-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All resource types</option>
            <option value="user">user</option>
            <option value="ip_space">ip_space</option>
            <option value="ip_block">ip_block</option>
            <option value="subnet">subnet</option>
            <option value="ip_address">ip_address</option>
          </select>

          {hasFilters && (
            <button
              onClick={resetFilters}
              className="flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            >
              <X className="h-3.5 w-3.5" />
              Clear
            </button>
          )}

          <span className="ml-auto text-sm text-muted-foreground">
            {total.toLocaleString()} {total === 1 ? "event" : "events"}
          </span>
        </div>

        {/* Table */}
        <div className="rounded-lg border overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/40">
                <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                  Timestamp
                </th>
                <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                  User
                </th>
                <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                  Action
                </th>
                <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                  Resource
                </th>
                <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                  Summary
                </th>
                <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                  Result
                </th>
                <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                  IP
                </th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <tr>
                  <td
                    colSpan={7}
                    className="px-3 py-8 text-center text-muted-foreground"
                  >
                    Loading…
                  </td>
                </tr>
              ) : items.length === 0 ? (
                <tr>
                  <td
                    colSpan={7}
                    className="px-3 py-8 text-center text-muted-foreground"
                  >
                    No audit events found
                  </td>
                </tr>
              ) : (
                items.map((entry: AuditLogEntry) => (
                  <tr
                    key={entry.id}
                    className="border-b last:border-0 hover:bg-muted/30"
                  >
                    <td className="px-3 py-2 font-mono text-xs text-muted-foreground whitespace-nowrap">
                      {formatTs(entry.timestamp)}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      <span className="font-medium">
                        {entry.user_display_name}
                      </span>
                      {entry.auth_source !== "local" && (
                        <span className="ml-1.5 text-xs text-muted-foreground">
                          ({entry.auth_source})
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      <ActionBadge action={entry.action} />
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      <span className="text-xs text-muted-foreground">
                        {entry.resource_type}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-muted-foreground max-w-xs truncate">
                      {entry.resource_display}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      <ResultBadge result={entry.result} />
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-muted-foreground whitespace-nowrap">
                      {entry.source_ip ?? "—"}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between">
            <span className="text-sm text-muted-foreground">
              Page {page + 1} of {totalPages}
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="flex h-8 w-8 items-center justify-center rounded-md border text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none disabled:opacity-40"
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
              <button
                onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                disabled={page >= totalPages - 1}
                className="flex h-8 w-8 items-center justify-center rounded-md border text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none disabled:opacity-40"
              >
                <ChevronRight className="h-4 w-4" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
