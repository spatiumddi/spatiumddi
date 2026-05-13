import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Copy,
  HardDrive,
  Loader2,
  RefreshCw,
  Server,
  Settings2,
  Upload,
  X,
} from "lucide-react";

import { ConfirmModal } from "@/components/ui/confirm-modal";
import { Modal } from "@/components/ui/modal";
import {
  applianceApi,
  applianceFleetApi,
  applianceReleasesApi,
  applianceSlotApi,
  type ApplianceDeploymentKind,
  type FleetAgentKind,
  type FleetAgentRow,
} from "@/lib/api";

import { SlotUpgradeCard } from "./SlotUpgradeCard";

/**
 * Phase 8f-5 — fleet OS version management.
 *
 * Unified table covering this appliance + every registered DNS +
 * DHCP agent. The "self" row pinned at the top opens the full
 * SlotUpgradeCard in a modal (slot A/B detail, log tail, rollback)
 * because the local OS upgrade flow is richer than the per-row fleet
 * upgrade. Agent rows keep the existing single-row Upgrade /
 * Manual-upgrade affordances, plus bulk-select checkboxes so the
 * operator can roll a release out to N appliance agents in one
 * action without a full-fleet outage (each agent applies on its own
 * inactive slot independently; reboots are still per-agent).
 */

const RELEASE_BASE =
  "https://github.com/spatiumddi/spatiumddi/releases/download";
const SLOT_IMAGE_URL = (tag: string) =>
  `${RELEASE_BASE}/${tag}/spatiumddi-appliance-slot-amd64.raw.xz`;

// Stable synthetic id for the self row in the React table key + the
// selection Set. Real agent ids are UUIDs, so this string can't collide.
const SELF_ROW_ID = "__self__";

function kindLabel(kind: FleetAgentKind | "self"): string {
  if (kind === "self") return "SELF";
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
  const [manualTarget, setManualTarget] = useState<FleetAgentRow | null>(null);
  const [selectedTag, setSelectedTag] = useState<string>("");
  const [manualSelectedTag, setManualSelectedTag] = useState<string>("");
  const [selfModalOpen, setSelfModalOpen] = useState(false);
  // IDs of agent rows the operator has checkbox-selected for bulk apply.
  // Set of strings (agent UUIDs); self isn't bulk-selectable.
  const [selectedAgentIds, setSelectedAgentIds] = useState<Set<string>>(
    new Set(),
  );
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkTag, setBulkTag] = useState<string>("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["appliance", "fleet"],
    queryFn: applianceFleetApi.list,
    // Fleet view auto-refreshes so the operator sees pending → done
    // transitions land within the next agent long-poll cycle (typical
    // 30 s heartbeat + 30 s long-poll → up to ~60 s).
    refetchInterval: 15_000,
  });

  // Self-row data — slot status + appliance info for the pinned row.
  // Both endpoints are appliance-only; on plain docker/k8s API hosts
  // they short-circuit (slot status returns appliance_mode=false,
  // info returns appliance_mode=false) and we render no self row.
  const { data: slotData } = useQuery({
    queryKey: ["appliance", "slot-upgrade"],
    queryFn: applianceSlotApi.status,
    refetchInterval: (q) =>
      q.state.data?.upgrade_state === "in-flight" ? 3_000 : 30_000,
  });
  const { data: info } = useQuery({
    queryKey: ["appliance", "info"],
    queryFn: applianceApi.getInfo,
    staleTime: 5 * 60 * 1000,
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

  const bulk = useMutation({
    // Bulk apply iterates the selected agent rows + fires the same
    // scheduleUpgrade call per row. Each call is independent so a
    // failure on one agent doesn't block the others; Promise.allSettled
    // gives us per-row outcomes for the success/error summary.
    mutationFn: async (input: {
      tag: string;
      targets: { kind: FleetAgentKind; id: string }[];
    }) => {
      const results = await Promise.allSettled(
        input.targets.map((t) =>
          applianceFleetApi.scheduleUpgrade(
            t.kind,
            t.id,
            input.tag,
            SLOT_IMAGE_URL(input.tag),
          ),
        ),
      );
      const ok = results.filter((r) => r.status === "fulfilled").length;
      const failed = results.length - ok;
      return { ok, failed, results };
    },
    onSuccess: () => {
      setBulkOpen(false);
      setBulkTag("");
      setSelectedAgentIds(new Set());
      qc.invalidateQueries({ queryKey: ["appliance", "fleet"] });
    },
  });

  const agents = data?.agents ?? [];

  // Build the synthetic self row. Only rendered when this API host
  // is running the SpatiumDDI OS appliance — for plain docker/k8s
  // installs we drop the row entirely (the table is then just agents).
  const selfRow: FleetAgentRow | null = useMemo(() => {
    if (!slotData?.appliance_mode) return null;
    const installed =
      slotData.current_slot === "slot_a"
        ? slotData.slot_a_version
        : slotData.current_slot === "slot_b"
          ? slotData.slot_b_version
          : null;
    return {
      kind: "dns", // placeholder — never read for self row, action column
      // branches on id === SELF_ROW_ID before touching kind.
      id: SELF_ROW_ID,
      name: info?.appliance_hostname || "this appliance",
      host: info?.appliance_hostname || "—",
      deployment_kind: "appliance",
      installed_appliance_version: installed,
      current_slot: slotData.current_slot,
      durable_default: slotData.durable_default,
      is_trial_boot: slotData.is_trial_boot,
      last_upgrade_state:
        slotData.upgrade_state === "idle" ? null : slotData.upgrade_state,
      last_upgrade_state_at: slotData.upgrade_state_at,
      last_seen_at: new Date().toISOString(),
      last_seen_ip: null,
      desired_appliance_version: null,
      desired_slot_image_url: null,
    };
  }, [slotData, info]);

  // Selectable agents = appliance-kind only (slot upgrade target).
  // Docker/k8s rows aren't bulk-selectable because their upgrade path
  // is the manual copy-paste command modal.
  const bulkEligibleAgents = useMemo(
    () =>
      agents.filter(
        (a) => a.deployment_kind === "appliance" || a.deployment_kind === null,
      ),
    [agents],
  );

  const allBulkSelected =
    bulkEligibleAgents.length > 0 &&
    bulkEligibleAgents.every((a) => selectedAgentIds.has(a.id));

  function toggleAgent(id: string) {
    setSelectedAgentIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function toggleSelectAll() {
    if (allBulkSelected) {
      setSelectedAgentIds(new Set());
    } else {
      setSelectedAgentIds(new Set(bulkEligibleAgents.map((a) => a.id)));
    }
  }

  const selectedCount = selectedAgentIds.size;
  const bulkTargets = bulkEligibleAgents.filter((a) =>
    selectedAgentIds.has(a.id),
  );

  // Default tag for bulk modal — newest non-prerelease.
  const defaultBulkTag =
    bulkTag ||
    releases.find((r) => !r.is_prerelease)?.tag ||
    releases[0]?.tag ||
    "";

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <Server className="h-4 w-4 text-muted-foreground" />
            OS versions — this appliance + fleet
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Manage the OS version on this appliance and every registered DNS +
            DHCP agent from one screen. The pinned <em>self</em> row at the top
            opens the full A/B slot detail (versions per slot, apply log,
            rollback) in a modal — the same machinery the per-row Upgrade button
            uses for remote agents. Roll a release out to multiple agents at
            once by checking rows + clicking
            <em> Apply to selected</em>; each agent applies on its own inactive
            slot independently so there's no fleet-wide outage. Docker / k8s
            rows show copy-paste commands instead of a slot upgrade.
          </p>
        </div>
        <button
          type="button"
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md border bg-background px-2 py-1 text-xs text-muted-foreground hover:bg-muted"
          onClick={() => {
            refetch();
            qc.invalidateQueries({ queryKey: ["appliance", "slot-upgrade"] });
          }}
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

      {/* Bulk-action toolbar — visible when ≥1 agent row is checked.
          Acts on the selected set only; self row is excluded by design
          since its upgrade flow is the richer modal (rollback, log
          tail) that doesn't bulk well. */}
      {selectedCount > 0 && (
        <div className="flex items-center justify-between gap-3 rounded-md border border-primary/40 bg-primary/5 px-3 py-2 text-xs">
          <div>
            <strong>{selectedCount}</strong> agent
            {selectedCount === 1 ? "" : "s"} selected
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="rounded-md border bg-background px-2 py-1 text-xs hover:bg-muted"
              onClick={() => setSelectedAgentIds(new Set())}
            >
              Clear selection
            </button>
            <button
              type="button"
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1 text-xs font-semibold text-primary-foreground hover:bg-primary/90"
              onClick={() => {
                setBulkOpen(true);
                setBulkTag("");
              }}
            >
              <Upload className="h-3 w-3" />
              Apply to selected…
            </button>
          </div>
        </div>
      )}

      <div className="overflow-x-auto rounded-md border bg-card">
        <table className="w-full min-w-[1200px] text-xs">
          <thead className="border-b bg-muted/50 text-muted-foreground">
            <tr>
              <th className="w-8 px-3 py-2 text-left font-medium">
                <input
                  type="checkbox"
                  className="cursor-pointer"
                  checked={allBulkSelected}
                  onChange={toggleSelectAll}
                  disabled={bulkEligibleAgents.length === 0}
                  title="Select all upgradeable agents"
                />
              </th>
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
            {/* Self row — pinned at top with a subtle accent border so
                the operator clocks it as "this appliance" without
                reading the Kind chip. */}
            {selfRow && (
              <FleetRow
                row={selfRow}
                isSelf
                selected={false}
                onToggle={() => {}}
                onUpgrade={() => setSelfModalOpen(true)}
                onManual={() => setSelfModalOpen(true)}
                onClearPending={() => {}}
                clearPending={false}
              />
            )}

            {!selfRow && agents.length === 0 && (
              <tr>
                <td
                  colSpan={10}
                  className="px-3 py-6 text-center text-muted-foreground"
                >
                  {isLoading ? "Loading fleet…" : "No agents registered."}
                </td>
              </tr>
            )}

            {agents.map((a) => (
              <FleetRow
                key={`${a.kind}-${a.id}`}
                row={a}
                isSelf={false}
                selected={selectedAgentIds.has(a.id)}
                onToggle={() => toggleAgent(a.id)}
                onUpgrade={() => {
                  setUpgradeTarget(a);
                  setSelectedTag("");
                }}
                onManual={() => {
                  setManualTarget(a);
                  setManualSelectedTag("");
                }}
                onClearPending={() => clear.mutate({ kind: a.kind, id: a.id })}
                clearPending={clear.isPending}
              />
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-[11px] text-muted-foreground">
        Fleet view auto-refreshes every 15 s. Pending upgrades typically resolve
        within one agent long-poll cycle (~30–60 s) — the State column flips to{" "}
        <code>in-flight</code> while the slot dd runs, then <code>done</code>{" "}
        when the host-side runner finishes.
      </p>

      {/* Self-row OS Image modal — wraps the existing SlotUpgradeCard
          so its slot A/B detail, apply log, and rollback all stay
          reachable through one entrypoint. */}
      {selfModalOpen && (
        <Modal
          title="OS Image — this appliance"
          onClose={() => setSelfModalOpen(false)}
          wide
        >
          <SlotUpgradeCard />
        </Modal>
      )}

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

      {/* Bulk apply modal — one release picker → N stamps (one per
          selected agent row). Failures are reported per-row in the
          summary; partial-success leaves the rows that DID get
          stamped with their pending chip so the operator can see
          what's in flight. */}
      <ConfirmModal
        open={bulkOpen}
        title={`Apply a release to ${selectedCount} agent${selectedCount === 1 ? "" : "s"}?`}
        message={
          <div className="space-y-3">
            <p>
              Stamps the picked release on every selected agent's row. Each
              agent applies on its own inactive slot independently — no
              fleet-wide downtime, and a failed apply on one agent leaves the
              others unaffected. Reboots remain per-agent (the agents arm grub
              one-shot but don't reboot themselves).
            </p>
            <div className="rounded-md border bg-muted/30 p-2 text-[11px]">
              <div className="mb-1 font-semibold text-muted-foreground">
                Targets
              </div>
              <ul className="space-y-0.5">
                {bulkTargets.map((t) => (
                  <li
                    key={`${t.kind}-${t.id}`}
                    className="flex items-center justify-between gap-2 font-mono"
                  >
                    <span>
                      {kindLabel(t.kind)} · {t.name}
                    </span>
                    <span className="text-muted-foreground">
                      {t.installed_appliance_version || "—"}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
            <label className="block text-xs font-medium">
              Release
              <select
                value={defaultBulkTag}
                onChange={(e) => setBulkTag(e.target.value)}
                className="mt-1 block w-full rounded-md border bg-background px-2 py-1.5 font-mono text-xs"
                autoFocus
              >
                <option value="">— pick a release —</option>
                {releases.map((r) => {
                  const date = new Date(r.published_at).toLocaleDateString();
                  const pre = r.is_prerelease ? " · pre-release" : "";
                  return (
                    <option key={r.tag} value={r.tag}>
                      {r.tag} — {date}
                      {pre}
                    </option>
                  );
                })}
              </select>
            </label>
            {defaultBulkTag && (
              <p className="text-[11px] text-muted-foreground">
                Will fetch{" "}
                <code className="rounded bg-muted px-1 font-mono break-all">
                  {SLOT_IMAGE_URL(defaultBulkTag)}
                </code>{" "}
                on each target.
              </p>
            )}
            {bulk.isError && (
              <p className="text-destructive">
                {(bulk.error as Error).message}
              </p>
            )}
          </div>
        }
        confirmLabel={
          defaultBulkTag
            ? `Stamp ${defaultBulkTag} on ${selectedCount} agent${selectedCount === 1 ? "" : "s"}`
            : "Pick a release"
        }
        cancelLabel="Cancel"
        loading={bulk.isPending}
        onConfirm={() => {
          if (defaultBulkTag && bulkTargets.length > 0) {
            bulk.mutate({
              tag: defaultBulkTag,
              targets: bulkTargets.map((t) => ({ kind: t.kind, id: t.id })),
            });
          }
        }}
        onClose={() => {
          if (!bulk.isPending) {
            setBulkOpen(false);
            setBulkTag("");
          }
        }}
      />

      {manualTarget && (
        <ManualUpgradeModal
          target={manualTarget}
          selectedTag={manualSelectedTag}
          onSelectTag={setManualSelectedTag}
          releases={releases}
          onClose={() => {
            setManualTarget(null);
            setManualSelectedTag("");
          }}
        />
      )}
    </div>
  );
}

// One <tr> per fleet row. Extracted so the self row + agent rows
// share styling (column widths, chip rendering) without duplicating
// half the table. Self-row affordances differ:
//   - no checkbox column (excluded from bulk select)
//   - action column shows "Manage…" (opens SlotUpgradeCard) instead
//     of Upgrade / Manual upgrade
//   - subtle left-border accent so it reads as the pinned "this box"
//     row without needing to spell it out
function FleetRow({
  row,
  isSelf,
  selected,
  onToggle,
  onUpgrade,
  onManual,
  onClearPending,
  clearPending,
}: {
  row: FleetAgentRow;
  isSelf: boolean;
  selected: boolean;
  onToggle: () => void;
  onUpgrade: () => void;
  onManual: () => void;
  onClearPending: () => void;
  clearPending: boolean;
}) {
  const canUpgrade =
    row.deployment_kind === "appliance" || row.deployment_kind === null;
  const slot =
    row.current_slot &&
    row.durable_default &&
    row.current_slot !== row.durable_default
      ? `${slotLabel(row.current_slot)} (trial)`
      : slotLabel(row.current_slot);
  const rowClass = isSelf
    ? "border-b border-l-2 border-l-primary bg-primary/5 last:border-b-0"
    : "border-b last:border-b-0";

  return (
    <tr className={rowClass}>
      <td className="px-3 py-2">
        {isSelf ? (
          <span className="text-[10px] text-muted-foreground" title="Self row">
            —
          </span>
        ) : (
          <input
            type="checkbox"
            className="cursor-pointer"
            checked={selected}
            onChange={onToggle}
            disabled={!canUpgrade}
            title={
              canUpgrade
                ? "Select for bulk apply"
                : "Bulk apply doesn't support docker/k8s rows"
            }
          />
        )}
      </td>
      <td className="px-3 py-2">
        <span className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-[10px] uppercase">
          {kindLabel(isSelf ? "self" : row.kind)}
        </span>
      </td>
      <td className="px-3 py-2">
        <div className="font-medium">
          {isSelf ? <em>{row.name}</em> : row.name}
        </div>
        <div className="font-mono text-[10px] text-muted-foreground">
          {row.host}
          {row.last_seen_ip ? ` · ${row.last_seen_ip}` : ""}
        </div>
      </td>
      <td className="px-3 py-2">
        <span className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-[10px]">
          {deploymentLabel(row.deployment_kind)}
        </span>
      </td>
      <td className="px-3 py-2 font-mono">
        {row.installed_appliance_version || "—"}
      </td>
      <td className="px-3 py-2 font-mono">{slot}</td>
      <td className="px-3 py-2">
        {row.last_upgrade_state ? (
          <span
            className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium ${
              row.last_upgrade_state === "done"
                ? "bg-green-500/10 text-green-700 dark:text-green-300"
                : row.last_upgrade_state === "failed"
                  ? "bg-destructive/10 text-destructive"
                  : row.last_upgrade_state === "in-flight"
                    ? "bg-blue-500/10 text-blue-700 dark:text-blue-300"
                    : "bg-muted text-muted-foreground"
            }`}
          >
            {row.last_upgrade_state === "done" && (
              <CheckCircle2 className="h-3 w-3" />
            )}
            {row.last_upgrade_state === "failed" && (
              <AlertCircle className="h-3 w-3" />
            )}
            {row.last_upgrade_state === "in-flight" && (
              <Loader2 className="h-3 w-3 animate-spin" />
            )}
            {row.last_upgrade_state}
          </span>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </td>
      <td className="px-3 py-2 text-muted-foreground">
        {isSelf ? "now" : relativeTime(row.last_seen_at)}
      </td>
      <td className="px-3 py-2 font-mono">
        {row.desired_appliance_version ? (
          <span className="inline-flex items-center gap-1 rounded-md bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-300">
            → {row.desired_appliance_version}
            {!isSelf && (
              <button
                type="button"
                className="text-amber-700/70 hover:text-amber-900 dark:text-amber-300/70 dark:hover:text-amber-100"
                title="Clear pending upgrade"
                onClick={onClearPending}
                disabled={clearPending}
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </span>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </td>
      <td className="px-3 py-2 text-right">
        {isSelf ? (
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-background px-2.5 py-1 text-xs font-semibold text-primary hover:bg-primary/5"
            onClick={onUpgrade}
            title="Open the full A/B slot detail (per-slot versions, log tail, rollback)"
          >
            <Settings2 className="h-3 w-3" />
            Manage…
          </button>
        ) : canUpgrade ? (
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1 text-xs font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            onClick={onUpgrade}
            disabled={!!row.desired_appliance_version}
          >
            <Upload className="h-3 w-3" />
            Upgrade
          </button>
        ) : (
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-md border bg-background px-2.5 py-1 text-xs text-muted-foreground hover:bg-muted"
            onClick={onManual}
            title="Show the operator-run upgrade command for this deployment kind"
          >
            Manual upgrade…
          </button>
        )}
      </td>
    </tr>
  );
}

function ManualUpgradeModal({
  target,
  selectedTag,
  onSelectTag,
  releases,
  onClose,
}: {
  target: FleetAgentRow;
  selectedTag: string;
  onSelectTag: (tag: string) => void;
  releases: { tag: string; published_at: string; is_prerelease: boolean }[];
  onClose: () => void;
}) {
  // Default tag: newest non-prerelease (releases come back newest-first).
  // Selecting "" keeps the placeholder option so operators see commands
  // with ``<release-tag>`` rather than a stale tag.
  const effectiveTag =
    selectedTag ||
    releases.find((r) => !r.is_prerelease)?.tag ||
    releases[0]?.tag ||
    "<release-tag>";

  const dockerCmd = [
    "# On the agent host, in the directory holding docker-compose.yml:",
    `SPATIUMDDI_VERSION=${effectiveTag} docker compose pull && \\`,
    `SPATIUMDDI_VERSION=${effectiveTag} docker compose up -d`,
  ].join("\n");

  // Service name varies by helm release; ``spatiumddi-${kind}`` is the
  // chart convention. Operator can rename in their command if their
  // release uses a different name.
  const helmServiceName =
    target.kind === "dns" ? "spatiumddi-dns-bind9" : "spatiumddi-dhcp-kea";
  const k8sCmd = [
    "# On a workstation with kubectl + helm pointed at the agent's cluster:",
    `helm upgrade ${helmServiceName} \\`,
    "  oci://ghcr.io/spatiumddi/charts/spatiumddi \\",
    `  --set image.tag=${effectiveTag} \\`,
    "  --reuse-values",
  ].join("\n");

  const cmd = target.deployment_kind === "k8s" ? k8sCmd : dockerCmd;
  const kindLabelText =
    target.deployment_kind === "k8s" ? "Kubernetes" : "Docker";

  return (
    <Modal
      title={`Manual upgrade — ${target.name} (${kindLabelText})`}
      onClose={onClose}
      wide
    >
      <div className="space-y-3 text-sm">
        <p className="text-muted-foreground">
          Agents running outside the SpatiumDDI OS appliance can't be
          slot-upgraded from this UI — there's no A/B partition to dd into. Roll
          the agent's container image instead. Pick the release tag, copy the
          command, and run it on the agent's host.
        </p>
        <label className="block text-xs font-medium">
          Release
          <select
            value={selectedTag}
            onChange={(e) => onSelectTag(e.target.value)}
            className="mt-1 block w-full rounded-md border bg-background px-2 py-1.5 font-mono text-xs"
            autoFocus
          >
            <option value="">— pick a release —</option>
            {releases.map((r) => {
              const date = new Date(r.published_at).toLocaleDateString();
              const pre = r.is_prerelease ? " · pre-release" : "";
              return (
                <option key={r.tag} value={r.tag}>
                  {r.tag} — {date}
                  {pre}
                </option>
              );
            })}
          </select>
        </label>
        <div>
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="text-xs font-medium">{kindLabelText} command</span>
            <button
              type="button"
              className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
              onClick={() => {
                if (navigator.clipboard) {
                  void navigator.clipboard.writeText(cmd);
                }
              }}
              title="Copy command to clipboard"
            >
              <Copy className="h-3 w-3" />
              Copy
            </button>
          </div>
          <pre className="overflow-x-auto rounded-md border bg-muted/40 p-2 font-mono text-[11px] leading-tight">
            {cmd}
          </pre>
        </div>
        <p className="text-[11px] text-muted-foreground">
          The agent reports back the new ``installed_appliance_version`` on its
          next heartbeat once the container restarts. The Fleet table's
          ``Installed`` column updates within ~30 s.
        </p>
        <div className="flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Close
          </button>
        </div>
      </div>
    </Modal>
  );
}
