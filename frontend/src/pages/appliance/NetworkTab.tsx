import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Clock,
  Globe,
  Network,
  RefreshCw,
  Server,
} from "lucide-react";

import { applianceSystemApi } from "@/lib/api";

/**
 * Phase 4f — Network & Host info (read-mostly MVP).
 *
 * Today: hostname + detected host IPs + uptime + reboot-pending +
 * appliance version, all read-only. The maintenance toggle and
 * reboot button live on the Maintenance tab.
 *
 * Deferred (post-4g): hostname rename, DHCP/static IP switch,
 * nftables firewall editor, SSH key upload. Those need additional
 * host-side machinery (each writes a different /etc/... file the
 * api can't reach without a privileged writer service).
 */
export function NetworkTab() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["appliance", "system", "info"],
    queryFn: applianceSystemApi.info,
    refetchInterval: 15_000,
  });

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <Network className="h-4 w-4 text-muted-foreground" />
            Network &amp; Host
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Read-only snapshot for now. Hostname rename, static-IP toggle,
            nftables firewall editor, and SSH key upload are deferred to a
            follow-up — each needs a privileged host-side writer.
          </p>
        </div>
        <button
          type="button"
          onClick={() =>
            qc.invalidateQueries({
              queryKey: ["appliance", "system", "info"],
            })
          }
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md border bg-background px-3 py-1.5 text-sm hover:bg-accent"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh
        </button>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          {(error as Error).message}
        </div>
      )}

      {data?.reboot_pending_from_host && <RebootBanner />}

      <section className="rounded-lg border bg-card shadow-sm">
        <div className="grid grid-cols-1 divide-y sm:grid-cols-2 sm:divide-x sm:divide-y-0">
          <InfoCell
            icon={Server}
            label="Hostname"
            value={isLoading ? "…" : data?.hostname || "—"}
          />
          <InfoCell
            icon={Globe}
            label="Host IPs"
            value={
              isLoading
                ? "…"
                : data?.host_ips && data.host_ips.length > 0
                  ? data.host_ips.join(", ")
                  : "(none detected)"
            }
          />
          <InfoCell
            icon={Clock}
            label="Uptime"
            value={isLoading ? "…" : formatUptime(data?.uptime_seconds)}
          />
          <InfoCell
            icon={Server}
            label="Appliance version"
            value={isLoading ? "…" : data?.appliance_version || "dev"}
          />
        </div>
      </section>
    </div>
  );
}

function InfoCell({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Server;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-start gap-2.5 p-4">
      <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80">
          {label}
        </div>
        <div className="mt-0.5 truncate font-mono text-sm">{value}</div>
      </div>
    </div>
  );
}

function RebootBanner() {
  return (
    <div className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-xs">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
      <div className="text-amber-700 dark:text-amber-400">
        <strong>Reboot pending.</strong> An unattended-upgrades run installed a
        kernel or libc update that requires a reboot. Schedule one from the
        Maintenance tab when convenient.
      </div>
    </div>
  );
}

function formatUptime(seconds: number | null | undefined): string {
  if (!seconds || seconds < 0) return "—";
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  if (days > 0) return `${days}d ${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}
