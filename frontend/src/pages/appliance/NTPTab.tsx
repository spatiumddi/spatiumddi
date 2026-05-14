import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { authApi, settingsApi, versionApi } from "@/lib/api";
import { NTPSection } from "@/components/NTPSection";

/**
 * Appliance → NTP tab (issue #154).
 *
 * Thin wrapper around the shared ``NTPSection`` component. Same shape
 * as ``SNMPTab``: settings + auth + version queries here, the form
 * itself in the component so it could be reused elsewhere if needed.
 */
export function NTPTab() {
  const { data: me } = useQuery({
    queryKey: ["me"],
    queryFn: authApi.me,
    staleTime: 60_000,
  });
  const { data: settings, isLoading } = useQuery({
    queryKey: ["settings"],
    queryFn: settingsApi.get,
  });
  const { data: versionInfo } = useQuery({
    queryKey: ["version"],
    queryFn: versionApi.get,
    staleTime: 60 * 60 * 1000,
  });

  if (isLoading || !settings) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading NTP settings…
      </div>
    );
  }

  const isSuperadmin = me?.is_superadmin ?? false;
  const applianceMode = versionInfo?.appliance_mode ?? true;
  const inputCls =
    "rounded-md border bg-background px-3 py-1.5 text-sm disabled:opacity-60";

  return (
    <div className="mx-auto max-w-4xl">
      <div className="mb-4">
        <h2 className="text-base font-semibold">NTP (chrony)</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          chrony runs at the OS level on every SpatiumDDI appliance host — local
          + every registered remote agent. The rendered ``chrony.conf`` ships
          through the ConfigBundle long-poll, validated host-side via ``chronyd
          -t`` before activation. Pool mode uses resolver-expanded NTP pools
          (internet-connected default); servers mode is for air-gapped sites
          pointing at internal time sources; mixed runs both. Optionally serve
          NTP to clients (opens UDP 123 in the host firewall).
        </p>
      </div>
      <NTPSection
        values={settings}
        isSuperadmin={isSuperadmin}
        applianceMode={applianceMode}
        inputCls={inputCls}
      />
    </div>
  );
}
