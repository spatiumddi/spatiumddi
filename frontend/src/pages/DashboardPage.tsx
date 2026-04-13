import { useQuery } from "@tanstack/react-query";
import { ipamApi } from "@/lib/api";
import { Network } from "lucide-react";

export function DashboardPage() {
  const { data: spaces } = useQuery({
    queryKey: ["spaces"],
    queryFn: ipamApi.listSpaces,
  });

  const { data: subnets } = useQuery({
    queryKey: ["subnets"],
    queryFn: () => ipamApi.listSubnets(),
  });

  return (
    <div className="h-full overflow-auto p-6">
      <div className="space-y-6">
        <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard label="IP Spaces" value={spaces?.length ?? "—"} icon={Network} />
          <StatCard label="Subnets" value={subnets?.length ?? "—"} icon={Network} />
        </div>
      </div>
    </div>
  );
}

function StatCard({
  label,
  value,
  icon: Icon,
}: {
  label: string;
  value: string | number;
  icon: React.ElementType;
}) {
  return (
    <div className="rounded-lg border bg-card p-5">
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-muted-foreground">{label}</p>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </div>
      <p className="mt-2 text-3xl font-bold">{value}</p>
    </div>
  );
}
