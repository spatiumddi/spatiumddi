import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { authApi, settingsApi, versionApi } from "@/lib/api";
import { SSHSection } from "@/components/SSHSection";

/**
 * Appliance → SSH tab (issue #157).
 *
 * Thin wrapper around the shared ``SSHSection`` component. Same shape as
 * ``SyslogTab`` / ``NTPTab`` / ``SNMPTab``: settings + auth + version
 * queries here, the form itself in the component.
 */
export function SSHTab() {
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
        Loading SSH settings…
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
        <h2 className="text-base font-semibold">SSH access</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          sshd runs at the OS level on every SpatiumDDI appliance host — local +
          every registered remote agent. Manage the <code>admin</code> user's
          authorized keys and sshd hardening (password auth, root login, port)
          centrally; the rendered <code>authorized_keys</code> +{" "}
          <code>/etc/ssh/sshd_config.d/spatiumddi.conf</code> ship through the
          ConfigBundle long-poll, validated host-side via <code>sshd -t</code>{" "}
          before activation. The SSH port is firewall-scoped to the allowed
          source networks; port 22 always stays open as an escape hatch so a bad
          change can't lock you out.
        </p>
      </div>
      <SSHSection
        values={settings}
        isSuperadmin={isSuperadmin}
        applianceMode={applianceMode}
        inputCls={inputCls}
      />
    </div>
  );
}
