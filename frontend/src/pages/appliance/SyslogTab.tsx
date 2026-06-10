import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { authApi, settingsApi, versionApi } from "@/lib/api";
import { SyslogSection } from "@/components/SyslogSection";

/**
 * Appliance → Syslog tab (issue #156).
 *
 * Thin wrapper around the shared ``SyslogSection`` component. Same shape
 * as ``NTPTab`` / ``SNMPTab``: settings + auth + version queries here,
 * the form itself in the component.
 */
export function SyslogTab() {
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
        Loading syslog settings…
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
        <h2 className="text-base font-semibold">Syslog forwarding (rsyslog)</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          rsyslog runs at the OS level on every SpatiumDDI appliance host —
          local + every registered remote agent. When forwarding is on, the
          rendered <code>/etc/rsyslog.d/50-spatium-forward.conf</code> ships
          through the ConfigBundle long-poll, validated host-side via{" "}
          <code>rsyslogd -N1</code> before activation. Both the systemd journal
          and file log sources are forwarded. UDP / TCP / TLS targets are
          supported (TLS uses the gtls driver with a per-target CA). Forwarding
          is outbound only — no inbound firewall port is opened.
        </p>
      </div>
      <SyslogSection
        values={settings}
        isSuperadmin={isSuperadmin}
        applianceMode={applianceMode}
        inputCls={inputCls}
      />
    </div>
  );
}
