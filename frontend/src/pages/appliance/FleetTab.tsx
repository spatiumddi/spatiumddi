import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  HardDrive,
  Loader2,
  RefreshCw,
  Server,
  Upload,
  X,
} from "lucide-react";

import { ConfirmModal } from "@/components/ui/confirm-modal";
import {
  applianceFleetApi,
  applianceReleasesApi,
  type ApplianceDeploymentKind,
  type FleetAgentKind,
  type FleetAgentRow,
} from "@/lib/api";

/**
 * Phase 8f-5 — fleet upgrade orchestration.
 *
 * Single table of every registered DNS + DHCP agent showing slot
 * state + deployment kind + installed version + currently-set
 * desired version. Per-row Upgrade button stamps the operator's
 * picked release tag onto the agent's server row; the agent's
 * ConfigBundle long-poll picks it up on the next cycle and fires
 * the local slot-upgrade trigger. Docker / k8s rows are read-only
 * with a copy-paste hint instead of an Upgrade button.
 */

const RELEASE_BASE =
  "https://github.com/spatiumddi/spatiumddi/releases/download";
const SLOT_IMAGE_URL = (tag: string) =>
  `${RELEASE_BASE}/${tag}/spatiumddi-appliance-slot-amd64.raw.xz`;

function kindLabel(kind: FleetAgentKind): string {
  return kind === "dns" ? "DNS" : "DHCP";
}

function deploymentLabel(kind: ApplianceDeploymentKind): string {
  if (!kind) return "unknown";
  return kind;
}

function slotLabel(slot: string | null): string {
  if (slot === "slot_a") return "A";
  if (slot === "slot_b") return "B";
  return "—";
}

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return "<1m ago";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}

export function FleetTab() {
  const qc = useQueryClient();
  const [upgradeTarget, setUpgradeTarget] = useState<FleetAgentRow | null>(
    null,
  );
  const [selectedTag, setSelectedTag] = useState<string>("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["appliance", "fleet"],
    queryFn: applianceFleetApi.list,
    // Fleet view auto-refreshes so the operator sees pending → done
    // transitions land within the next agent long-poll cycle (typical
    // 30 s heartbeat + 30 s long-poll → up to ~60 s).
    refetchInterval: 15_000,
  });

  // Releases list for the per-row Upgrade modal's tag picker.
  const { data: releasesData } = useQuery({
    queryKey: ["appliance", "releases"],
    queryFn: applianceReleasesApi.list,
    staleTime: 60_000,
  });
  const releases = releasesData?.releases ?? [];

  const upgrade = useMutation({
    mutationFn: ({
      kind,
      id,
      tag,
    }: {
      kind: FleetAgentKind;
      id: string;
      tag: string;
    }) => applianceFleetApi.scheduleUpgrade(kind, id, tag, SLOT_IMAGE_URL(tag)),
    onSuccess: () => {
      setUpgradeTarget(null);
      setSelectedTag("");
      qc.invalidateQueries({ queryKey: ["appliance", "fleet"] });
    },
  });

  const clear = useMutation({
    mutationFn: ({ kind, id }: { kind: FleetAgentKind; id: string }) =>
      applianceFleetApi.clearUpgrade(kind, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "fleet"] });
    },
  });

  const agents = data?.agents ?? [];

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <Server className="h-4 w-4 text-muted-foreground" />
            Fleet upgrade
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Drive slot upgrades for every registered DNS + DHCP agent from one
            screen. Stamps the operator's picked release tag onto the agent's
            server row; the agent's ConfigBundle long-poll picks it up on the
            next cycle and fires the local slot-upgrade trigger (the same
            machinery as the per-appliance OS Image card). Docker / k8s rows
            show copy-paste commands instead.
          </p>
        </div>
        <button
          type="button"
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md border bg-background px-2 py-1 text-xs text-muted-foreground hover:bg-muted"
          onClick={() => refetch()}
          disabled={isLoading}
        >
          <RefreshCw className={`h-3 w-3 ${isLoading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          Failed to load fleet: {(error as Error).message}
        </div>
      )}

      <div className="overflow-x-auto rounded-md border bg-card">
        <table className="w-full min-w-[1100px] text-xs">
          <thead className="border-b bg-muted/50 text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-left font-medium">Kind</th>
              <th className="px-3 py-2 text-left font-medium">Name</th>
              <th className="px-3 py-2 text-left font-medium">Deployment</th>
              <th className="px-3 py-2 text-left font-medium">Installed</th>
              <th className="px-3 py-2 text-left font-medium">Slot</th>
              <th className="px-3 py-2 text-left font-medium">State</th>
              <th className="px-3 py-2 text-left font-medium">Last seen</th>
              <th className="px-3 py-2 text-left font-medium">Pending</th>
              <th className="px-3 py-2 text-right font-medium">Action</th>
            </tr>
          </thead>
          <tbody>
            {agents.length === 0 && (
              <tr>
                <td
                  colSpan={9}
                  className="px-3 py-6 text-center text-muted-foreground"
                >
                  {isLoading ? "Loading fleet…" : "No agents registered."}
                </td>
              </tr>
            )}
            {agents.map((a) => {
              const canUpgrade =
                a.deployment_kind === "appliance" || a.deployment_kind === null;
              const slot =
                a.current_slot &&
                a.durable_default &&
                a.current_slot !== a.durable_default
                  ? `${slotLabel(a.current_slot)} (trial)`
                  : slotLabel(a.current_slot);
              return (
                <tr
                  key={`${a.kind}-${a.id}`}
                  className="border-b last:border-b-0"
                >
                  <td className="px-3 py-2">
                    <span className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-[10px] uppercase">
                      {kindLabel(a.kind)}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <div className="font-medium">{a.name}</div>
                    <div className="font-mono text-[10px] text-muted-foreground">
                      {a.host}
                      {a.last_seen_ip ? ` · ${a.last_seen_ip}` : ""}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <span className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-[10px]">
                      {deploymentLabel(a.deployment_kind)}
                    </span>
                  </td>
                  <td className="px-3 py-2 font-mono">
                    {a.installed_appliance_version || "—"}
                  </td>
                  <td className="px-3 py-2 font-mono">{slot}</td>
                  <td className="px-3 py-2">
                    {a.last_upgrade_state ? (
                      <span
                        className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium ${
                          a.last_upgrade_state === "done"
                            ? "bg-green-500/10 text-green-700 dark:text-green-300"
                            : a.last_upgrade_state === "failed"
                              ? "bg-destructive/10 text-destructive"
                              : a.last_upgrade_state === "in-flight"
                                ? "bg-blue-500/10 text-blue-700 dark:text-blue-300"
                                : "bg-muted text-muted-foreground"
                        }`}
                      >
                        {a.last_upgrade_state === "done" && (
                          <CheckCircle2 className="h-3 w-3" />
                        )}
                        {a.last_upgrade_state === "failed" && (
                          <AlertCircle className="h-3 w-3" />
                        )}
                        {a.last_upgrade_state === "in-flight" && (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        )}
                        {a.last_upgrade_state}
                      </span>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {relativeTime(a.last_seen_at)}
                  </td>
                  <td className="px-3 py-2 font-mono">
                    {a.desired_appliance_version ? (
                      <span className="inline-flex items-center gap-1 rounded-md bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-300">
                        → {a.desired_appliance_version}
                        <button
                          type="button"
                          className="text-amber-700/70 hover:text-amber-900 dark:text-amber-300/70 dark:hover:text-amber-100"
                          title="Clear pending upgrade"
                          onClick={() =>
                            clear.mutate({ kind: a.kind, id: a.id })
                          }
                          disabled={clear.isPending}
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </span>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {canUpgrade ? (
                      <button
                        type="button"
                        className="inline-flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1 text-xs font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                        onClick={() => {
                          setUpgradeTarget(a);
                          setSelectedTag("");
                        }}
                        disabled={!!a.desired_appliance_version}
                      >
                        <Upload className="h-3 w-3" />
                        Upgrade
                      </button>
                    ) : (
                      <span
                        className="text-muted-foreground"
                        title={
                          a.deployment_kind === "docker"
                            ? "Run on agent host: docker compose pull && up -d"
                            : a.deployment_kind === "k8s"
                              ? "helm upgrade … --set image.tag=<version>"
                              : "Slot upgrade not supported on this deployment"
                        }
                      >
                        manual
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <p className="text-[11px] text-muted-foreground">
        Fleet view auto-refreshes every 15 s. Pending upgrades typically resolve
        within one agent long-poll cycle (~30–60 s) — the State column flips to{" "}
        <code>in-flight</code> while the slot dd runs, then <code>done</code>{" "}
        when the host-side runner finishes.
      </p>

      <ConfirmModal
        open={!!upgradeTarget}
        title={
          upgradeTarget
            ? `Upgrade ${kindLabel(upgradeTarget.kind)} agent “${upgradeTarget.name}”?`
            : ""
        }
        message={
          upgradeTarget && (
            <div className="space-y-3">
              <p>
                Stamp{" "}
                <code className="rounded bg-muted px-1 font-mono">
                  desired_appliance_version
                </code>{" "}
                on this agent's row. The agent's next ConfigBundle long-poll
                picks it up; if its installed version doesn't match, the agent
                writes the slot-upgrade trigger and the host-side machinery does
                the rest (dd → grub-reboot → /health/live → grub-set-default).
              </p>
              <label className="block text-xs font-medium">
                Release
                <select
                  value={selectedTag}
                  onChange={(e) => setSelectedTag(e.target.value)}
                  className="mt-1 block w-full rounded-md border bg-background px-2 py-1.5 font-mono text-xs"
                  autoFocus
                >
                  <option value="">— pick a release —</option>
                  {releases.map((r) => {
                    const date = new Date(r.published_at).toLocaleDateString();
                    const pre = r.is_prerelease ? " · pre-release" : "";
                    const installed =
                      upgradeTarget.installed_appliance_version === r.tag
                        ? " · installed on this agent"
                        : "";
                    return (
                      <option key={r.tag} value={r.tag}>
                        {r.tag} — {date}
                        {pre}
                        {installed}
                      </option>
                    );
                  })}
                </select>
              </label>
              {selectedTag && (
                <p className="text-[11px] text-muted-foreground">
                  Will fetch{" "}
                  <code className="rounded bg-muted px-1 font-mono break-all">
                    {SLOT_IMAGE_URL(selectedTag)}
                  </code>
                </p>
              )}
              <p className="flex items-start gap-2 rounded-md border border-blue-500/40 bg-blue-500/5 p-2 text-[11px] text-blue-700 dark:text-blue-300">
                <HardDrive className="mt-0.5 h-3 w-3 shrink-0" />
                The active slot stays untouched during apply; if the new slot
                fails <code>/health/live</code>, the next reboot auto-reverts.
              </p>
              {upgrade.isError && (
                <p className="text-destructive">
                  {(upgrade.error as Error).message}
                </p>
              )}
            </div>
          )
        }
        confirmLabel={selectedTag ? `Stamp ${selectedTag}` : "Pick a release"}
        cancelLabel="Cancel"
        loading={upgrade.isPending}
        onConfirm={() => {
          if (upgradeTarget && selectedTag) {
            upgrade.mutate({
              kind: upgradeTarget.kind,
              id: upgradeTarget.id,
              tag: selectedTag,
            });
          }
        }}
        onClose={() => {
          if (!upgrade.isPending) {
            setUpgradeTarget(null);
            setSelectedTag("");
          }
        }}
      />
    </div>
  );
}
