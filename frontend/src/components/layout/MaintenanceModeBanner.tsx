import { useQuery } from "@tanstack/react-query";

import { platformHealthApi } from "@/lib/api";

/**
 * Persistent banner shown across every page when the API reports
 * ``maintenance_mode: true`` from /health/platform (issue #57). The
 * backend MaintenanceModeMiddleware is the real enforcement — this is
 * just an honest-broker notice so operators know the platform is
 * read-only and that superadmins can still make changes.
 *
 * Distinct from the appliance-host ``MaintenanceBanner`` (different
 * concept — that one is the per-appliance host maintenance window).
 */
export function MaintenanceModeBanner() {
  const { data } = useQuery({
    queryKey: ["platform-health"],
    queryFn: () => platformHealthApi.get(),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  if (!data?.maintenance_mode) return null;

  const message = data.maintenance_message?.trim();

  return (
    <div className="border-b border-red-300 bg-red-100 px-4 py-1.5 text-center text-xs text-red-900 dark:border-red-500/50 dark:bg-red-500/15 dark:text-red-200">
      <strong>Maintenance mode</strong> — the system is read-only. Mutating
      actions are blocked for everyone except superadmins.
      {message ? <span className="ml-1">{message}</span> : null}
    </div>
  );
}
