import { useMemo, useState } from "react";
import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  Pencil,
  Plus,
  RefreshCw,
  Server,
  Trash2,
  Wifi,
} from "lucide-react";
import {
  dhcpApi,
  ipamApi,
  type DHCPPool,
  type DHCPScope,
  type DHCPServer,
  type DHCPServerGroup,
  type DHCPStaticAssignment,
  type DHCPClientClass,
  type DHCPLease,
} from "@/lib/api";
import { useSessionState } from "@/lib/useSessionState";
import { cn } from "@/lib/utils";
import { CreateServerGroupModal } from "./CreateServerGroupModal";
import { CreateServerModal } from "./CreateServerModal";
import { CreateScopeModal } from "./CreateScopeModal";
import { CreateClientClassModal } from "./CreateClientClassModal";
import { DeleteConfirmModal, StatusDot } from "./_shared";

type Selection =
  | { type: "group"; group: DHCPServerGroup }
  | { type: "server"; group: DHCPServerGroup | null; server: DHCPServer }
  | null;

type Tab =
  | "scopes"
  | "pools"
  | "statics"
  | "classes"
  | "leases"
  | "options";

// ─────────────────────────────────────────────────────────────────────────────
// Sidebar
// ─────────────────────────────────────────────────────────────────────────────

function GroupSidebar({
  selection,
  onSelect,
  onCreateGroup,
}: {
  selection: Selection;
  onSelect: (s: Selection) => void;
  onCreateGroup: () => void;
}) {
  const [expanded, setExpanded] = useSessionState<Set<string>>(
    "spatium.dhcp.expandedGroups",
    new Set(),
  );
  const { data: groups = [], isLoading } = useQuery({
    queryKey: ["dhcp-groups"],
    queryFn: dhcpApi.listGroups,
  });
  const { data: ungrouped = [] } = useQuery({
    queryKey: ["dhcp-servers", "all"],
    queryFn: () => dhcpApi.listServers(),
  });

  function toggle(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="w-72 flex-shrink-0 flex flex-col border-r bg-card">
      <div className="flex items-center justify-between px-4 py-3 border-b">
        <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          DHCP Server Groups
        </span>
        <button
          className="flex h-6 w-6 items-center justify-center rounded hover:bg-accent"
          onClick={onCreateGroup}
          title="New group"
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
            <Wifi className="h-8 w-8 text-muted-foreground/30 mx-auto mb-2" />
            <p className="text-xs text-muted-foreground mb-3">
              No server groups yet.
            </p>
            <button
              className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs mx-auto hover:bg-accent"
              onClick={onCreateGroup}
            >
              <Plus className="h-3 w-3" /> Create Group
            </button>
          </div>
        )}

        {groups.map((g) => {
          const isExpanded = expanded.has(g.id);
          const selected =
            selection?.type === "group" && selection.group.id === g.id;
          const serversInGroup = ungrouped.filter((s) => s.server_group_id === g.id);

          return (
            <div key={g.id}>
              <div
                className={cn(
                  "flex items-center rounded-md mx-1",
                  selected && "bg-primary text-primary-foreground",
                )}
              >
                <button
                  className={cn(
                    "flex h-7 w-6 items-center justify-center flex-shrink-0",
                    selected
                      ? "text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                  onClick={() => toggle(g.id)}
                >
                  {isExpanded ? (
                    <ChevronDown className="h-3.5 w-3.5" />
                  ) : (
                    <ChevronRight className="h-3.5 w-3.5" />
                  )}
                </button>
                <button
                  className="flex flex-1 items-center gap-2 py-1.5 pr-1 min-w-0"
                  onClick={() => {
                    onSelect({ type: "group", group: g });
                    if (!isExpanded) toggle(g.id);
                  }}
                >
                  <Wifi className="h-3.5 w-3.5 flex-shrink-0" />
                  <span className="text-sm font-medium truncate">{g.name}</span>
                  <span className="ml-auto text-xs text-muted-foreground">
                    {serversInGroup.length}
                  </span>
                </button>
              </div>

              {isExpanded && (
                <div className="ml-6 mb-1">
                  {serversInGroup.length === 0 && (
                    <p className="py-1 text-xs text-muted-foreground/70">
                      No servers in this group.
                    </p>
                  )}
                  {serversInGroup.map((s) => {
                    const active =
                      selection?.type === "server" &&
                      selection.server.id === s.id;
                    return (
                      <button
                        key={s.id}
                        onClick={() =>
                          onSelect({ type: "server", group: g, server: s })
                        }
                        className={cn(
                          "flex w-full items-center gap-2 rounded-md px-2 py-1 text-xs",
                          active
                            ? "bg-primary/10 text-primary font-medium"
                            : "hover:bg-accent",
                        )}
                      >
                        <StatusDot status={s.status} />
                        <span className="truncate">{s.name}</span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}

        {/* Ungrouped servers */}
        {ungrouped.some((s) => !s.server_group_id) && (
          <div className="mt-3 border-t pt-2">
            <p className="px-4 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60">
              Ungrouped Servers
            </p>
            {ungrouped
              .filter((s) => !s.server_group_id)
              .map((s) => {
                const active =
                  selection?.type === "server" &&
                  selection.server.id === s.id;
                return (
                  <button
                    key={s.id}
                    onClick={() =>
                      onSelect({ type: "server", group: null, server: s })
                    }
                    className={cn(
                      "flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-xs mx-1",
                      active
                        ? "bg-primary/10 text-primary font-medium"
                        : "hover:bg-accent",
                    )}
                  >
                    <StatusDot status={s.status} />
                    <span className="truncate">{s.name}</span>
                  </button>
                );
              })}
          </div>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Group detail view
// ─────────────────────────────────────────────────────────────────────────────

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors",
        active
          ? "border-primary text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function GroupDetailView({
  group,
  onEdit,
  onDelete,
  onAddServer,
}: {
  group: DHCPServerGroup;
  onEdit: () => void;
  onDelete: () => void;
  onAddServer: () => void;
}) {
  const { data: servers = [] } = useQuery({
    queryKey: ["dhcp-servers", group.id],
    queryFn: () => dhcpApi.listServers(group.id),
    refetchInterval: 30_000,
  });

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="border-b px-6 py-4 bg-card">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-lg font-semibold">{group.name}</h1>
              <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                {group.mode}
              </span>
              <span className="text-xs text-muted-foreground">
                {servers.length} server{servers.length !== 1 ? "s" : ""}
              </span>
            </div>
            {group.description && (
              <p className="mt-1 text-xs text-muted-foreground">
                {group.description}
              </p>
            )}
          </div>
          <div className="flex items-center gap-3 text-xs">
            <button
              onClick={onAddServer}
              className="flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-primary-foreground hover:bg-primary/90"
            >
              <Plus className="h-3.5 w-3.5" /> Add Server
            </button>
            <button
              onClick={onEdit}
              className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
            >
              <Pencil className="h-3 w-3" /> Edit Group
            </button>
            <button
              onClick={onDelete}
              className="flex items-center gap-1 rounded-md border border-destructive/40 px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
            >
              <Trash2 className="h-3 w-3" /> Delete Group
            </button>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-auto p-6">
        <div className="rounded-lg border">
          <div className="border-b px-4 py-2 bg-muted/30">
            <h2 className="text-sm font-semibold">Servers</h2>
          </div>
          {servers.length === 0 ? (
            <div className="p-8 text-center">
              <p className="text-sm text-muted-foreground">
                No servers in this group yet.
              </p>
              <button
                onClick={onAddServer}
                className="mt-3 inline-flex items-center gap-1 rounded-md border px-3 py-1.5 text-xs hover:bg-accent"
              >
                <Plus className="h-3 w-3" /> Add Server
              </button>
            </div>
          ) : (
            <div className="divide-y">
              {servers.map((s) => (
                <div
                  key={s.id}
                  className="flex items-center gap-3 px-4 py-2.5"
                >
                  <StatusDot status={s.status} />
                  <span className="w-48 truncate text-sm font-medium">
                    {s.name}
                  </span>
                  <span className="w-48 truncate font-mono text-xs text-muted-foreground">
                    {s.host}:{s.port}
                  </span>
                  <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                    {s.driver}
                  </span>
                  <span className="ml-auto text-xs text-muted-foreground">
                    {s.last_sync_at
                      ? `synced ${new Date(s.last_sync_at).toLocaleString()}`
                      : "never synced"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Server detail view
// ─────────────────────────────────────────────────────────────────────────────

function ServerScopesTab({ server }: { server: DHCPServer }) {
  const qc = useQueryClient();
  const { data: subnets = [] } = useQuery({
    queryKey: ["subnets"],
    queryFn: () => ipamApi.listSubnets(),
  });

  // For a server, we list scopes across all subnets. We fetch per-subnet then
  // keep only scopes where server_id matches (or is null — covering "any in
  // group" scopes when the server is in a group).
  const scopeQueries = useQueries({
    queries: subnets.map((s) => ({
      queryKey: ["dhcp-scopes-subnet", s.id],
      queryFn: () => dhcpApi.listScopesBySubnet(s.id),
    })),
  });
  const allScopes: (DHCPScope & { subnet_network?: string })[] = scopeQueries
    .flatMap((q, i) =>
      (q.data ?? []).map((sc) => ({
        ...sc,
        subnet_network: subnets[i]?.network,
      })),
    )
    .filter(
      (sc) =>
        sc.server_id === server.id ||
        (sc.server_id === null && server.server_group_id),
    );

  const [createForSubnet, setCreateForSubnet] = useState<string | null>(null);
  const [editScope, setEditScope] = useState<DHCPScope | null>(null);
  const [delScope, setDelScope] = useState<DHCPScope | null>(null);

  const delMut = useMutation({
    mutationFn: (id: string) => dhcpApi.deleteScope(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-scopes-subnet"] });
      setDelScope(null);
    },
  });

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">
          {allScopes.length} scope{allScopes.length !== 1 ? "s" : ""} served by this server.
        </p>
        <div className="flex items-center gap-2">
          <select
            className="rounded-md border bg-background px-2 py-1 text-xs"
            defaultValue=""
            onChange={(e) => {
              if (e.target.value) setCreateForSubnet(e.target.value);
              e.target.value = "";
            }}
          >
            <option value="">+ New Scope on subnet…</option>
            {subnets.map((s) => (
              <option key={s.id} value={s.id}>
                {s.network}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="rounded-lg border">
        {allScopes.length === 0 ? (
          <p className="p-6 text-center text-sm text-muted-foreground">
            No scopes on this server.
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/30 text-xs">
                <th className="px-3 py-2 text-left font-medium">Subnet</th>
                <th className="px-3 py-2 text-left font-medium">Name</th>
                <th className="px-3 py-2 text-left font-medium">Enabled</th>
                <th className="px-3 py-2 text-left font-medium">Lease (s)</th>
                <th className="px-3 py-2 text-left font-medium">DDNS</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {allScopes.map((sc) => (
                <tr key={sc.id} className="border-b last:border-0">
                  <td className="px-3 py-2 font-mono text-xs">
                    {sc.subnet_network ?? "—"}
                  </td>
                  <td className="px-3 py-2">{sc.name}</td>
                  <td className="px-3 py-2">{sc.enabled ? "yes" : "no"}</td>
                  <td className="px-3 py-2 tabular-nums">{sc.lease_time}</td>
                  <td className="px-3 py-2">{sc.ddns_enabled ? "on" : "off"}</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      onClick={() => setEditScope(sc)}
                      className="rounded p-1 text-muted-foreground hover:text-foreground"
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                    <button
                      onClick={() => setDelScope(sc)}
                      className="rounded p-1 text-muted-foreground hover:text-destructive"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {createForSubnet && (
        <CreateScopeModal
          subnetId={createForSubnet}
          onClose={() => setCreateForSubnet(null)}
        />
      )}
      {editScope && (
        <CreateScopeModal
          scope={editScope}
          onClose={() => setEditScope(null)}
        />
      )}
      {delScope && (
        <DeleteConfirmModal
          title="Delete DHCP Scope"
          description={`Delete scope "${delScope.name}"?`}
          onConfirm={() => delMut.mutate(delScope.id)}
          onClose={() => setDelScope(null)}
          isPending={delMut.isPending}
        />
      )}
    </div>
  );
}

function ServerPoolsOrStaticsTab({
  server,
  kind,
}: {
  server: DHCPServer;
  kind: "pools" | "statics";
}) {
  const { data: subnets = [] } = useQuery({
    queryKey: ["subnets"],
    queryFn: () => ipamApi.listSubnets(),
  });
  const scopeQueries = useQueries({
    queries: subnets.map((s) => ({
      queryKey: ["dhcp-scopes-subnet", s.id],
      queryFn: () => dhcpApi.listScopesBySubnet(s.id),
    })),
  });
  const allScopes = scopeQueries
    .flatMap((q) => q.data ?? [])
    .filter((sc) => sc.server_id === server.id || sc.server_id === null);

  const nestedQueries = useQueries({
    queries: allScopes.map((sc) => ({
      queryKey: [kind === "pools" ? "dhcp-pools" : "dhcp-statics", sc.id],
      queryFn: () =>
        kind === "pools" ? dhcpApi.listPools(sc.id) : dhcpApi.listStatics(sc.id),
    })),
  });

  const rows: Array<{
    scope: DHCPScope;
    item: DHCPPool | DHCPStaticAssignment;
  }> = nestedQueries.flatMap((q, i) =>
    (q.data ?? []).map((item) => ({ scope: allScopes[i]!, item })),
  );

  return (
    <div className="rounded-lg border">
      {rows.length === 0 ? (
        <p className="p-6 text-center text-sm text-muted-foreground">
          No {kind === "pools" ? "pools" : "static assignments"} yet.
        </p>
      ) : kind === "pools" ? (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/30 text-xs">
              <th className="px-3 py-2 text-left font-medium">Scope</th>
              <th className="px-3 py-2 text-left font-medium">Name</th>
              <th className="px-3 py-2 text-left font-medium">Start</th>
              <th className="px-3 py-2 text-left font-medium">End</th>
              <th className="px-3 py-2 text-left font-medium">Type</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ scope, item }) => {
              const p = item as DHCPPool;
              return (
                <tr key={p.id} className="border-b last:border-0">
                  <td className="px-3 py-2 text-xs">{scope.name}</td>
                  <td className="px-3 py-2">{p.name || "—"}</td>
                  <td className="px-3 py-2 font-mono text-xs">{p.start_ip}</td>
                  <td className="px-3 py-2 font-mono text-xs">{p.end_ip}</td>
                  <td className="px-3 py-2">
                    <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                      {p.pool_type}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/30 text-xs">
              <th className="px-3 py-2 text-left font-medium">Scope</th>
              <th className="px-3 py-2 text-left font-medium">MAC</th>
              <th className="px-3 py-2 text-left font-medium">IP</th>
              <th className="px-3 py-2 text-left font-medium">Hostname</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ scope, item }) => {
              const s = item as DHCPStaticAssignment;
              return (
                <tr key={s.id} className="border-b last:border-0">
                  <td className="px-3 py-2 text-xs">{scope.name}</td>
                  <td className="px-3 py-2 font-mono text-xs">{s.mac}</td>
                  <td className="px-3 py-2 font-mono text-xs">{s.ip}</td>
                  <td className="px-3 py-2">{s.hostname || "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ClientClassesTab({ server }: { server: DHCPServer }) {
  const qc = useQueryClient();
  const { data: classes = [] } = useQuery({
    queryKey: ["dhcp-client-classes", server.id],
    queryFn: () => dhcpApi.listClientClasses(server.id),
  });
  const [showCreate, setShowCreate] = useState(false);
  const [edit, setEdit] = useState<DHCPClientClass | null>(null);
  const [del, setDel] = useState<DHCPClientClass | null>(null);
  const delMut = useMutation({
    mutationFn: (id: string) => dhcpApi.deleteClientClass(server.id, id),
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: ["dhcp-client-classes", server.id],
      });
      setDel(null);
    },
  });

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <button
          onClick={() => setShowCreate(true)}
          className="flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground hover:bg-primary/90"
        >
          <Plus className="h-3 w-3" /> New Client Class
        </button>
      </div>
      <div className="rounded-lg border">
        {classes.length === 0 ? (
          <p className="p-6 text-center text-sm text-muted-foreground">
            No client classes defined.
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/30 text-xs">
                <th className="px-3 py-2 text-left font-medium">Name</th>
                <th className="px-3 py-2 text-left font-medium">Description</th>
                <th className="px-3 py-2 text-left font-medium">Match</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {classes.map((c) => (
                <tr key={c.id} className="border-b last:border-0">
                  <td className="px-3 py-2 font-medium">{c.name}</td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {c.description}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs truncate max-w-md">
                    {c.match_expression}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      onClick={() => setEdit(c)}
                      className="rounded p-1 text-muted-foreground hover:text-foreground"
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                    <button
                      onClick={() => setDel(c)}
                      className="rounded p-1 text-muted-foreground hover:text-destructive"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showCreate && (
        <CreateClientClassModal
          serverId={server.id}
          onClose={() => setShowCreate(false)}
        />
      )}
      {edit && (
        <CreateClientClassModal
          klass={edit}
          serverId={server.id}
          onClose={() => setEdit(null)}
        />
      )}
      {del && (
        <DeleteConfirmModal
          title="Delete Client Class"
          description={`Delete class "${del.name}"?`}
          onConfirm={() => delMut.mutate(del.id)}
          onClose={() => setDel(null)}
          isPending={delMut.isPending}
        />
      )}
    </div>
  );
}

function LeasesTab({ server }: { server: DHCPServer }) {
  const [state, setState] = useState<string>("");
  const [subnetId, setSubnetId] = useState<string>("");
  const [offset, setOffset] = useState(0);
  const limit = 50;

  const { data: subnets = [] } = useQuery({
    queryKey: ["subnets"],
    queryFn: () => ipamApi.listSubnets(),
  });

  const { data, isFetching, refetch } = useQuery({
    queryKey: ["dhcp-leases", server.id, state, subnetId, offset],
    queryFn: () =>
      dhcpApi.getLeases(server.id, {
        state: state || undefined,
        subnet_id: subnetId || undefined,
        limit,
        offset,
      }),
  });

  const leases = data?.items ?? [];
  const total = data?.total ?? 0;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <select
          className="rounded-md border bg-background px-2 py-1 text-xs"
          value={state}
          onChange={(e) => {
            setOffset(0);
            setState(e.target.value);
          }}
        >
          <option value="">All states</option>
          <option value="active">Active</option>
          <option value="expired">Expired</option>
          <option value="released">Released</option>
          <option value="declined">Declined</option>
        </select>
        <select
          className="rounded-md border bg-background px-2 py-1 text-xs"
          value={subnetId}
          onChange={(e) => {
            setOffset(0);
            setSubnetId(e.target.value);
          }}
        >
          <option value="">All subnets</option>
          {subnets.map((s) => (
            <option key={s.id} value={s.id}>
              {s.network}
            </option>
          ))}
        </select>
        <button
          onClick={() => refetch()}
          className="ml-auto flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
          disabled={isFetching}
        >
          <RefreshCw
            className={cn("h-3 w-3", isFetching && "animate-spin")}
          />
          Refresh
        </button>
      </div>
      <div className="rounded-lg border overflow-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/30 text-xs">
              <th className="px-3 py-2 text-left font-medium">IP</th>
              <th className="px-3 py-2 text-left font-medium">MAC</th>
              <th className="px-3 py-2 text-left font-medium">Hostname</th>
              <th className="px-3 py-2 text-left font-medium">State</th>
              <th className="px-3 py-2 text-left font-medium">Expires</th>
              <th className="px-3 py-2 text-left font-medium">Last Seen</th>
            </tr>
          </thead>
          <tbody>
            {leases.length === 0 && (
              <tr>
                <td
                  colSpan={6}
                  className="p-6 text-center text-sm text-muted-foreground"
                >
                  {isFetching ? "Loading…" : "No leases."}
                </td>
              </tr>
            )}
            {leases.map((l: DHCPLease) => (
              <tr key={`${l.ip}-${l.mac}`} className="border-b last:border-0">
                <td className="px-3 py-1.5 font-mono text-xs">{l.ip}</td>
                <td className="px-3 py-1.5 font-mono text-xs">{l.mac}</td>
                <td className="px-3 py-1.5">{l.hostname || "—"}</td>
                <td className="px-3 py-1.5">
                  <span
                    className={cn(
                      "rounded-full px-2 py-0.5 text-xs",
                      l.state === "active"
                        ? "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"
                        : "bg-muted text-muted-foreground",
                    )}
                  >
                    {l.state}
                  </span>
                </td>
                <td className="px-3 py-1.5 text-xs text-muted-foreground">
                  {l.expires_at
                    ? new Date(l.expires_at).toLocaleString()
                    : "—"}
                </td>
                <td className="px-3 py-1.5 text-xs text-muted-foreground">
                  {l.last_seen
                    ? new Date(l.last_seen).toLocaleString()
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {total > limit && (
        <div className="flex items-center justify-between text-xs">
          <span className="text-muted-foreground">
            {offset + 1}–{Math.min(offset + limit, total)} of {total}
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setOffset(Math.max(0, offset - limit))}
              disabled={offset === 0}
              className="rounded-md border px-2 py-1 hover:bg-accent disabled:opacity-50"
            >
              Prev
            </button>
            <button
              onClick={() => setOffset(offset + limit)}
              disabled={offset + limit >= total}
              className="rounded-md border px-2 py-1 hover:bg-accent disabled:opacity-50"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function ServerDetailView({
  server,
  group,
  onEdit,
  onDelete,
}: {
  server: DHCPServer;
  group: DHCPServerGroup | null;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("scopes");
  const syncMut = useMutation({
    mutationFn: () => dhcpApi.syncServer(server.id),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["dhcp-servers"] }),
  });
  const approveMut = useMutation({
    mutationFn: () => dhcpApi.approveServer(server.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dhcp-servers"] }),
  });

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="border-b px-6 py-4 bg-card">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <StatusDot status={server.status} />
              <h1 className="text-lg font-semibold truncate">{server.name}</h1>
              <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                {server.driver}
              </span>
              {!server.agent_approved && (
                <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-800 dark:bg-amber-900/30 dark:text-amber-400">
                  pending approval
                </span>
              )}
            </div>
            <div className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
              <span className="font-mono">
                {server.host}:{server.port}
              </span>
              {group && <span>Group: {group.name}</span>}
              <span>
                {server.last_sync_at
                  ? `Last sync ${new Date(server.last_sync_at).toLocaleString()}`
                  : "Never synced"}
              </span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {!server.agent_approved && (
              <button
                onClick={() => approveMut.mutate()}
                className="rounded-md bg-emerald-600 px-3 py-1.5 text-xs text-white hover:bg-emerald-700"
                disabled={approveMut.isPending}
              >
                Approve
              </button>
            )}
            <button
              onClick={() => syncMut.mutate()}
              className="flex items-center gap-1 rounded-md border px-3 py-1.5 text-xs hover:bg-accent"
              disabled={syncMut.isPending}
            >
              <RefreshCw
                className={cn(
                  "h-3 w-3",
                  syncMut.isPending && "animate-spin",
                )}
              />
              Force Sync
            </button>
            <button
              onClick={onEdit}
              className="rounded-md border px-3 py-1.5 text-xs hover:bg-accent"
            >
              Edit
            </button>
            <button
              onClick={onDelete}
              className="rounded-md border border-destructive/40 px-3 py-1.5 text-xs text-destructive hover:bg-destructive/10"
            >
              Delete
            </button>
          </div>
        </div>
      </div>

      <div className="border-b px-6 bg-card">
        <div className="flex gap-1">
          <TabButton active={tab === "scopes"} onClick={() => setTab("scopes")}>
            Scopes
          </TabButton>
          <TabButton active={tab === "pools"} onClick={() => setTab("pools")}>
            Pools
          </TabButton>
          <TabButton
            active={tab === "statics"}
            onClick={() => setTab("statics")}
          >
            Static Assignments
          </TabButton>
          <TabButton
            active={tab === "classes"}
            onClick={() => setTab("classes")}
          >
            Client Classes
          </TabButton>
          <TabButton active={tab === "leases"} onClick={() => setTab("leases")}>
            Leases
          </TabButton>
          <TabButton
            active={tab === "options"}
            onClick={() => setTab("options")}
          >
            Server Options
          </TabButton>
        </div>
      </div>

      <div className="flex-1 overflow-auto p-6">
        {tab === "scopes" && <ServerScopesTab server={server} />}
        {tab === "pools" && (
          <ServerPoolsOrStaticsTab server={server} kind="pools" />
        )}
        {tab === "statics" && (
          <ServerPoolsOrStaticsTab server={server} kind="statics" />
        )}
        {tab === "classes" && <ClientClassesTab server={server} />}
        {tab === "leases" && <LeasesTab server={server} />}
        {tab === "options" && (
          <div className="rounded-lg border p-6 text-sm text-muted-foreground">
            Server-level default options (global pool, renew times, reservation
            defaults) are managed via the driver. Push changes with Force Sync.
          </div>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Page shell
// ─────────────────────────────────────────────────────────────────────────────

export function DHCPPage() {
  const qc = useQueryClient();
  const [selection, setSelection] = useState<Selection>(null);
  const [showCreateGroup, setShowCreateGroup] = useState(false);
  const [editGroup, setEditGroup] = useState<DHCPServerGroup | null>(null);
  const [delGroup, setDelGroup] = useState<DHCPServerGroup | null>(null);
  const [addServerFor, setAddServerFor] = useState<string | null>(null);
  const [editServer, setEditServer] = useState<DHCPServer | null>(null);
  const [delServer, setDelServer] = useState<DHCPServer | null>(null);

  const deleteGroupMut = useMutation({
    mutationFn: (id: string) => dhcpApi.deleteGroup(id),
    onSuccess: (_, id) => {
      qc.invalidateQueries({ queryKey: ["dhcp-groups"] });
      if (selection && "group" in selection && selection.group?.id === id)
        setSelection(null);
      setDelGroup(null);
    },
  });
  const deleteServerMut = useMutation({
    mutationFn: (id: string) => dhcpApi.deleteServer(id),
    onSuccess: (_, id) => {
      qc.invalidateQueries({ queryKey: ["dhcp-servers"] });
      if (selection?.type === "server" && selection.server.id === id)
        setSelection(null);
      setDelServer(null);
    },
  });

  // Refresh selected server on server-list invalidations so status badges
  // stay current.
  const selectedServerId =
    selection?.type === "server" ? selection.server.id : null;
  const { data: freshServer } = useQuery({
    queryKey: ["dhcp-server", selectedServerId],
    queryFn: () => dhcpApi.getServer(selectedServerId as string),
    enabled: !!selectedServerId,
    refetchInterval: 30_000,
  });
  const effectiveServer = useMemo(() => {
    if (selection?.type !== "server") return null;
    return freshServer ?? selection.server;
  }, [selection, freshServer]);

  return (
    <div className="flex h-full overflow-hidden">
      <GroupSidebar
        selection={selection}
        onSelect={setSelection}
        onCreateGroup={() => setShowCreateGroup(true)}
      />

      <div className="flex-1 overflow-hidden">
        {!selection && (
          <div className="flex h-full items-center justify-center">
            <div className="text-center">
              <Server className="h-12 w-12 text-muted-foreground/20 mx-auto mb-3" />
              <p className="text-sm text-muted-foreground">
                Select a server group or server from the sidebar.
              </p>
            </div>
          </div>
        )}
        {selection?.type === "group" && (
          <GroupDetailView
            group={selection.group}
            onEdit={() => setEditGroup(selection.group)}
            onDelete={() => setDelGroup(selection.group)}
            onAddServer={() => setAddServerFor(selection.group.id)}
          />
        )}
        {selection?.type === "server" && effectiveServer && (
          <ServerDetailView
            server={effectiveServer}
            group={selection.group}
            onEdit={() => setEditServer(effectiveServer)}
            onDelete={() => setDelServer(effectiveServer)}
          />
        )}
      </div>

      {showCreateGroup && (
        <CreateServerGroupModal onClose={() => setShowCreateGroup(false)} />
      )}
      {editGroup && (
        <CreateServerGroupModal
          group={editGroup}
          onClose={() => setEditGroup(null)}
        />
      )}
      {delGroup && (
        <DeleteConfirmModal
          title="Delete Server Group"
          description={`Permanently delete group "${delGroup.name}"?`}
          onConfirm={() => deleteGroupMut.mutate(delGroup.id)}
          onClose={() => setDelGroup(null)}
          isPending={deleteGroupMut.isPending}
        />
      )}
      {addServerFor && (
        <CreateServerModal
          defaultGroupId={addServerFor}
          onClose={() => setAddServerFor(null)}
        />
      )}
      {editServer && (
        <CreateServerModal
          server={editServer}
          onClose={() => setEditServer(null)}
        />
      )}
      {delServer && (
        <DeleteConfirmModal
          title="Delete DHCP Server"
          description={`Remove server "${delServer.name}"? Its scopes remain but will be unassigned.`}
          onConfirm={() => deleteServerMut.mutate(delServer.id)}
          onClose={() => setDelServer(null)}
          isPending={deleteServerMut.isPending}
        />
      )}
    </div>
  );
}
