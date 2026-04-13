import { useQuery } from "@tanstack/react-query";
import { ipamApi, type Subnet } from "@/lib/api";

export function SubnetsPage() {
  const { data: subnets, isLoading } = useQuery({
    queryKey: ["subnets"],
    queryFn: () => ipamApi.listSubnets(),
  });

  if (isLoading) return <p className="text-muted-foreground">Loading…</p>;

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold tracking-tight">Subnets</h1>
      {subnets?.length === 0 && (
        <p className="text-sm text-muted-foreground">No subnets yet.</p>
      )}
      <div className="rounded-lg border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50">
              <th className="px-4 py-3 text-left font-medium">Network</th>
              <th className="px-4 py-3 text-left font-medium">Name</th>
              <th className="px-4 py-3 text-left font-medium">Gateway</th>
              <th className="px-4 py-3 text-left font-medium">VLAN</th>
              <th className="px-4 py-3 text-left font-medium">Status</th>
              <th className="px-4 py-3 text-left font-medium">Utilization</th>
            </tr>
          </thead>
          <tbody>
            {subnets?.map((subnet: Subnet) => (
              <tr key={subnet.id} className="border-b last:border-0 hover:bg-muted/30">
                <td className="px-4 py-3 font-mono font-medium">{subnet.network}</td>
                <td className="px-4 py-3 text-muted-foreground">{subnet.name || "—"}</td>
                <td className="px-4 py-3 font-mono">{subnet.gateway ?? "—"}</td>
                <td className="px-4 py-3">{subnet.vlan_id ?? "—"}</td>
                <td className="px-4 py-3">
                  <StatusBadge status={subnet.status} />
                </td>
                <td className="px-4 py-3">
                  <UtilizationBar percent={subnet.utilization_percent} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    active: "bg-green-100 text-green-800",
    deprecated: "bg-yellow-100 text-yellow-800",
    reserved: "bg-blue-100 text-blue-800",
    quarantine: "bg-red-100 text-red-800",
  };
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${colors[status] ?? "bg-muted text-muted-foreground"}`}>
      {status}
    </span>
  );
}

function UtilizationBar({ percent }: { percent: number }) {
  const color =
    percent >= 95 ? "bg-red-500" : percent >= 80 ? "bg-amber-400" : "bg-green-500";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 rounded-full bg-muted">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${Math.min(percent, 100)}%` }} />
      </div>
      <span className="text-xs text-muted-foreground">{percent.toFixed(0)}%</span>
    </div>
  );
}
