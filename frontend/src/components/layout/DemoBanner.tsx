import { useQuery } from "@tanstack/react-query";

import { platformHealthApi } from "@/lib/api";

/**
 * Persistent banner shown across every page when the API reports
 * ``demo_mode: true`` from /health/platform. Backend gating is the
 * real defence — this is just an honest-broker notice so visitors
 * know mutations are locked and admin/admin will keep working.
 */
export function DemoBanner() {
  const { data } = useQuery({
    queryKey: ["platform-health"],
    queryFn: () => platformHealthApi.get(),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  if (!data?.demo_mode) return null;

  return (
    <div className="border-b border-amber-300 bg-amber-100 px-4 py-1.5 text-center text-xs text-amber-900 dark:border-amber-500/50 dark:bg-amber-500/15 dark:text-amber-200">
      Demo mode — nmap, AI, integrations, webhooks, and outbound mail are
      disabled. Sign-in is locked to <strong>admin / admin</strong>; data resets
      on Codespace rebuild.
    </div>
  );
}
