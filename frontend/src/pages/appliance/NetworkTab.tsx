import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Clock,
  Globe,
  Network,
  RefreshCw,
  Server,
} from "lucide-react";

import {
  applianceApprovalApi,
  applianceSystemApi,
  formatApiError,
  type MetalLBConfig,
} from "@/lib/api";
import { cn } from "@/lib/utils";

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
          {formatApiError(error)}
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

      <MetalLBConfigCard />
    </div>
  );
}

// #272 Phase 7c — cluster-wide MetalLB pool + control-plane VIP picker.
// Host-network config, so it lives here on Network & Host (moved off the
// Fleet tab where it sat awkwardly between the two appliance tables).
// The operator sets an L2 address pool + a floating VIP; the seed
// supervisor picks the saved config up on its next heartbeat (~60 s) and
// patches the HelmCharts so the frontend Service floats on the VIP.
function MetalLBConfigCard() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["appliance", "metallb"],
    queryFn: applianceApprovalApi.getMetalLBConfig,
    staleTime: 30_000,
  });

  const [enabled, setEnabled] = useState(false);
  const [poolText, setPoolText] = useState("");
  const [vip, setVip] = useState("");
  const [dirty, setDirty] = useState(false);

  // Seed the form from the server once it loads (and re-seed after a
  // save) — but never clobber in-progress operator edits.
  useEffect(() => {
    if (data && !dirty) {
      setEnabled(data.enabled);
      setPoolText((data.pool_addresses ?? []).join("\n"));
      setVip(data.control_plane_vip ?? "");
    }
  }, [data, dirty]);

  const save = useMutation({
    mutationFn: (body: MetalLBConfig) =>
      applianceApprovalApi.setMetalLBConfig(body),
    onSuccess: (saved) => {
      qc.setQueryData(["appliance", "metallb"], saved);
      setDirty(false);
    },
  });

  const poolAddresses = poolText
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);

  const onSave = () => {
    save.mutate({
      enabled,
      pool_addresses: poolAddresses,
      control_plane_vip: vip.trim(),
    });
  };

  return (
    <section className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "inline-block h-2 w-2 rounded-full",
              data?.enabled ? "bg-emerald-500" : "bg-zinc-400",
            )}
          />
          <h3 className="text-sm font-semibold">Control-plane VIP (MetalLB)</h3>
          {data?.enabled && data.control_plane_vip && (
            <span className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
              {data.control_plane_vip}
            </span>
          )}
        </div>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        A single floating IP that fronts the Web UI so it stays reachable on one
        address regardless of which control-plane node is up (multi-node HA).
        The VIP must fall inside the address pool. Applied across the cluster by
        the seed within ~60&nbsp;s; the served certificate auto-adds the VIP to
        its SANs.
      </p>

      {isLoading ? (
        <p className="mt-3 text-sm text-muted-foreground">Loading…</p>
      ) : (
        <div className="mt-3 flex flex-col gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => {
                setEnabled(e.target.checked);
                setDirty(true);
              }}
            />
            Enable MetalLB + control-plane VIP
          </label>
          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium text-muted-foreground">
              Address pool — one CIDR or range per line
            </span>
            <textarea
              value={poolText}
              onChange={(e) => {
                setPoolText(e.target.value);
                setDirty(true);
              }}
              rows={2}
              placeholder={"192.168.0.240/29\n192.168.0.240-192.168.0.247"}
              className="w-full rounded-md border bg-background px-2 py-1 font-mono text-xs"
            />
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium text-muted-foreground">
              Control-plane VIP
            </span>
            <input
              value={vip}
              onChange={(e) => {
                setVip(e.target.value);
                setDirty(true);
              }}
              placeholder="192.168.0.240"
              className="w-full rounded-md border bg-background px-2 py-1 font-mono text-xs"
            />
          </div>
          {save.isError && (
            <p className="text-xs text-rose-600">
              {formatApiError(save.error)}
            </p>
          )}
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onSave}
              disabled={!dirty || save.isPending}
              className="rounded-md border bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {save.isPending ? "Saving…" : "Save"}
            </button>
            {save.isSuccess && !dirty && (
              <span className="text-xs text-emerald-600">✓ Saved</span>
            )}
          </div>
        </div>
      )}
    </section>
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
