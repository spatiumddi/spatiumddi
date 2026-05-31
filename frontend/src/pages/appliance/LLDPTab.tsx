import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { authApi, settingsApi, versionApi } from "@/lib/api";
import { LLDPSection } from "@/components/LLDPSection";

/**
 * Appliance → LLDP tab (issue #343).
 *
 * Thin wrapper around the shared ``LLDPSection`` form, same shape as the
 * SNMP / NTP tabs. lldpd runs at the OS level on every appliance host; the
 * rendered config ships through the ConfigBundle long-poll the same way
 * SNMP / chrony do. Live neighbour discovery (the supervisor shipping
 * ``lldpcli show neighbors`` back to IPAM) is Phase 2.
 */
export function LLDPTab() {
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
        Loading LLDP settings…
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
        <h2 className="text-base font-semibold">LLDP (lldpd)</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          lldpd runs at the OS level on every SpatiumDDI appliance host. It
          advertises this node to upstream switches (chassis-id, system name,
          management IP, capabilities) and learns its L2 neighbours. LLDP is raw
          Layer-2 (ethertype 0x88cc) — unlike SNMP / NTP it opens no firewall
          port. The interface allowlist excludes container / k3s vNICs by
          default so the appliance never advertises into the overlay network.
        </p>
      </div>
      <LLDPSection
        values={settings}
        isSuperadmin={isSuperadmin}
        applianceMode={applianceMode}
        inputCls={inputCls}
      />
    </div>
  );
}
