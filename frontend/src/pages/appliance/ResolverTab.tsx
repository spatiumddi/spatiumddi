import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { authApi, settingsApi, versionApi } from "@/lib/api";
import { ResolverSection } from "@/components/ResolverSection";

/**
 * Appliance → DNS Resolver tab (issue #158).
 *
 * Thin wrapper around the shared ``ResolverSection`` component. Same shape as
 * ``NTPTab`` / ``SSHTab`` / ``SyslogTab``: settings + auth + version queries
 * here, the form itself in the component.
 */
export function ResolverTab() {
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
        Loading resolver settings…
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
        <h2 className="text-base font-semibold">DNS resolver</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          systemd-resolved runs at the OS level on every SpatiumDDI appliance
          host. In <strong>override</strong> mode the configured servers replace
          the upstream resolvers that NetworkManager / DHCP would otherwise pick
          per-link — the rendered{" "}
          <code>/etc/systemd/resolved.conf.d/spatiumddi.conf</code> drop-in pins{" "}
          <code>DNS=</code> and emits a route-only <code>~.</code> default
          domain so the pinned servers actually win. Reverting to{" "}
          <strong>automatic</strong> removes that drop-in and hands resolver
          selection back to per-link DHCP / NetworkManager. The drop-in never
          touches the stub listener (BIND9 binds host <code>:53</code>), and the
          config ships through the ConfigBundle long-poll to every appliance
          host (local + every registered supervisor).
        </p>
      </div>
      <ResolverSection
        values={settings}
        isSuperadmin={isSuperadmin}
        applianceMode={applianceMode}
        inputCls={inputCls}
      />
    </div>
  );
}
