import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  Pencil,
  Plus,
  Server,
  Trash2,
} from "lucide-react";
import {
  dhcpApi,
  type DHCPPool,
  type DHCPScope,
  type DHCPStaticAssignment,
} from "@/lib/api";
import { CreateScopeModal } from "./CreateScopeModal";
import { CreatePoolModal } from "./CreatePoolModal";
import { CreateStaticAssignmentModal } from "./CreateStaticAssignmentModal";
import { DeleteConfirmModal } from "./_shared";

function PoolRow({ pool, scope }: { pool: DHCPPool; scope: DHCPScope }) {
  const qc = useQueryClient();
  const [edit, setEdit] = useState(false);
  const [del, setDel] = useState(false);
  const mut = useMutation({
    mutationFn: () => dhcpApi.deletePool(scope.id, pool.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-pools", scope.id] });
      setDel(false);
    },
  });
  return (
    <tr className="border-b last:border-0 text-sm">
      <td className="px-3 py-1.5">{pool.name || "—"}</td>
      <td className="px-3 py-1.5 font-mono text-xs">{pool.start_ip}</td>
      <td className="px-3 py-1.5 font-mono text-xs">{pool.end_ip}</td>
      <td className="px-3 py-1.5">
        <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
          {pool.pool_type}
        </span>
      </td>
      <td className="px-3 py-1.5 text-right">
        <button
          onClick={() => setEdit(true)}
          className="rounded p-1 text-muted-foreground hover:text-foreground"
        >
          <Pencil className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={() => setDel(true)}
          className="rounded p-1 text-muted-foreground hover:text-destructive"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </td>
      {edit && (
        <CreatePoolModal pool={pool} scope={scope} onClose={() => setEdit(false)} />
      )}
      {del && (
        <DeleteConfirmModal
          title="Delete Pool"
          description={`Delete pool ${pool.start_ip} – ${pool.end_ip}?`}
          onConfirm={() => mut.mutate()}
          onClose={() => setDel(false)}
          isPending={mut.isPending}
        />
      )}
    </tr>
  );
}

function StaticRow({
  row,
  scope,
}: {
  row: DHCPStaticAssignment;
  scope: DHCPScope;
}) {
  const qc = useQueryClient();
  const [edit, setEdit] = useState(false);
  const [del, setDel] = useState(false);
  const mut = useMutation({
    mutationFn: () => dhcpApi.deleteStatic(scope.id, row.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-statics", scope.id] });
      setDel(false);
    },
  });
  return (
    <tr className="border-b last:border-0 text-sm">
      <td className="px-3 py-1.5 font-mono text-xs">{row.mac_address}</td>
      <td className="px-3 py-1.5 font-mono text-xs">{row.ip_address}</td>
      <td className="px-3 py-1.5">{row.hostname || "—"}</td>
      <td className="px-3 py-1.5 text-muted-foreground truncate max-w-xs">
        {row.description}
      </td>
      <td className="px-3 py-1.5 text-right">
        <button
          onClick={() => setEdit(true)}
          className="rounded p-1 text-muted-foreground hover:text-foreground"
        >
          <Pencil className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={() => setDel(true)}
          className="rounded p-1 text-muted-foreground hover:text-destructive"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </td>
      {edit && (
        <CreateStaticAssignmentModal
          staticAssignment={row}
          scope={scope}
          onClose={() => setEdit(false)}
        />
      )}
      {del && (
        <DeleteConfirmModal
          title="Delete Static Assignment"
          description={`Delete static for ${row.mac_address} → ${row.ip_address}?`}
          onConfirm={() => mut.mutate()}
          onClose={() => setDel(false)}
          isPending={mut.isPending}
        />
      )}
    </tr>
  );
}

function ScopeCard({ scope }: { scope: DHCPScope }) {
  const qc = useQueryClient();
  const [showPools, setShowPools] = useState(true);
  const [showStatics, setShowStatics] = useState(false);
  const [showAddPool, setShowAddPool] = useState(false);
  const [showAddStatic, setShowAddStatic] = useState(false);
  const [editScope, setEditScope] = useState(false);
  const [deleteScope, setDeleteScope] = useState(false);

  const { data: pools = [] } = useQuery({
    queryKey: ["dhcp-pools", scope.id],
    queryFn: () => dhcpApi.listPools(scope.id),
  });
  const { data: statics = [] } = useQuery({
    queryKey: ["dhcp-statics", scope.id],
    queryFn: () => dhcpApi.listStatics(scope.id),
  });

  const toggleEnabled = useMutation({
    mutationFn: (enabled: boolean) =>
      dhcpApi.updateScope(scope.id, { enabled }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-scopes-subnet", scope.subnet_id] });
    },
  });

  const delMut = useMutation({
    mutationFn: () => dhcpApi.deleteScope(scope.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-scopes-subnet", scope.subnet_id] });
      setDeleteScope(false);
    },
  });

  return (
    <div className="rounded-lg border bg-card">
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="flex items-center gap-3 min-w-0">
          <Server className="h-4 w-4 text-muted-foreground flex-shrink-0" />
          <div className="min-w-0">
            <p className="text-sm font-semibold truncate">
              {scope.name || `Scope ${scope.id.slice(0, 8)}`}
            </p>
            <p className="text-xs text-muted-foreground">
              Lease {scope.lease_time}s · {pools.length} pool{pools.length !== 1 ? "s" : ""} ·{" "}
              {statics.length} static{statics.length !== 1 ? "s" : ""}
              {scope.ddns_enabled && " · DDNS"}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs cursor-pointer">
            <input
              type="checkbox"
              checked={scope.enabled}
              onChange={(e) => toggleEnabled.mutate(e.target.checked)}
            />
            {scope.enabled ? "Enabled" : "Disabled"}
          </label>
          <button
            onClick={() => setEditScope(true)}
            className="rounded p-1 text-muted-foreground hover:text-foreground"
            title="Edit scope"
          >
            <Pencil className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={() => setDeleteScope(true)}
            className="rounded p-1 text-muted-foreground hover:text-destructive"
            title="Delete scope"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <div>
        <div className="flex items-center justify-between px-4 py-2 border-b bg-muted/30">
          <button
            onClick={() => setShowPools((v) => !v)}
            className="flex items-center gap-1 text-xs font-semibold"
          >
            {showPools ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
            Pools ({pools.length})
          </button>
          <button
            onClick={() => setShowAddPool(true)}
            className="flex items-center gap-1 text-xs text-primary hover:underline"
          >
            <Plus className="h-3 w-3" /> Add Pool
          </button>
        </div>
        {showPools && pools.length > 0 && (
          <table className="w-full">
            <thead>
              <tr className="border-b bg-muted/20 text-xs">
                <th className="px-3 py-1.5 text-left font-medium">Name</th>
                <th className="px-3 py-1.5 text-left font-medium">Start</th>
                <th className="px-3 py-1.5 text-left font-medium">End</th>
                <th className="px-3 py-1.5 text-left font-medium">Type</th>
                <th className="px-3 py-1.5"></th>
              </tr>
            </thead>
            <tbody>
              {pools.map((p) => (
                <PoolRow key={p.id} pool={p} scope={scope} />
              ))}
            </tbody>
          </table>
        )}

        <div className="flex items-center justify-between px-4 py-2 border-b bg-muted/30">
          <button
            onClick={() => setShowStatics((v) => !v)}
            className="flex items-center gap-1 text-xs font-semibold"
          >
            {showStatics ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
            Static Assignments ({statics.length})
          </button>
          <button
            onClick={() => setShowAddStatic(true)}
            className="flex items-center gap-1 text-xs text-primary hover:underline"
          >
            <Plus className="h-3 w-3" /> Add Static
          </button>
        </div>
        {showStatics && statics.length > 0 && (
          <table className="w-full">
            <thead>
              <tr className="border-b bg-muted/20 text-xs">
                <th className="px-3 py-1.5 text-left font-medium">MAC</th>
                <th className="px-3 py-1.5 text-left font-medium">IP</th>
                <th className="px-3 py-1.5 text-left font-medium">Hostname</th>
                <th className="px-3 py-1.5 text-left font-medium">Description</th>
                <th className="px-3 py-1.5"></th>
              </tr>
            </thead>
            <tbody>
              {statics.map((s) => (
                <StaticRow key={s.id} row={s} scope={scope} />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showAddPool && (
        <CreatePoolModal scope={scope} onClose={() => setShowAddPool(false)} />
      )}
      {showAddStatic && (
        <CreateStaticAssignmentModal
          scope={scope}
          onClose={() => setShowAddStatic(false)}
        />
      )}
      {editScope && (
        <CreateScopeModal scope={scope} onClose={() => setEditScope(false)} />
      )}
      {deleteScope && (
        <DeleteConfirmModal
          title="Delete DHCP Scope"
          description={`Delete scope "${scope.name}" and all its pools and static assignments?`}
          references={[
            `${pools.length} pool${pools.length !== 1 ? "s" : ""}`,
            `${statics.length} static assignment${statics.length !== 1 ? "s" : ""}`,
          ]}
          onConfirm={() => delMut.mutate()}
          onClose={() => setDeleteScope(false)}
          isPending={delMut.isPending}
        />
      )}
    </div>
  );
}

/**
 * Panel shown inside the IPAM SubnetDetail "DHCP" tab. Lists all DHCP scopes
 * defined against the given subnet, lets the user create a new scope, and
 * exposes inline management of pools and static assignments.
 */
export function DHCPSubnetPanel({ subnetId }: { subnetId: string }) {
  const [showCreate, setShowCreate] = useState(false);
  const { data: scopes = [], isLoading } = useQuery({
    queryKey: ["dhcp-scopes-subnet", subnetId],
    queryFn: () => dhcpApi.listScopesBySubnet(subnetId),
  });

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold">DHCP Scopes</h2>
          <p className="text-xs text-muted-foreground mt-0.5">
            One scope per server — use multiple scopes for HA pairs.
          </p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
        >
          <Plus className="h-3.5 w-3.5" /> Create Scope
        </button>
      </div>

      {isLoading && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}

      {!isLoading && scopes.length === 0 && (
        <div className="rounded-lg border border-dashed p-10 text-center">
          <Server className="mx-auto mb-3 h-10 w-10 text-muted-foreground/30" />
          <p className="text-sm font-medium">No DHCP scopes on this subnet</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Create a scope to start serving leases from a DHCP server.
          </p>
        </div>
      )}

      <div className="space-y-4">
        {scopes.map((s) => (
          <ScopeCard key={s.id} scope={s} />
        ))}
      </div>

      {showCreate && (
        <CreateScopeModal
          subnetId={subnetId}
          onClose={() => setShowCreate(false)}
        />
      )}
    </div>
  );
}
