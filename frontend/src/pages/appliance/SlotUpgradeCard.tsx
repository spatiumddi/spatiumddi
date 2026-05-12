import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  HardDrive,
  Loader2,
  PlayCircle,
  RefreshCw,
  RotateCcw,
  Shield,
} from "lucide-react";

import { ConfirmModal } from "@/components/ui/confirm-modal";
import { applianceSlotApi, type ApplianceSlot } from "@/lib/api";

/**
 * Phase 8b-3 — Appliance OS slot upgrade UI.
 *
 * Distinct from the container-stack release flow above. This writes
 * a whole-rootfs image into the *inactive* A/B slot via dd, arms grub
 * to one-shot try it on next boot, and lets `spatiumddi-firstboot`
 * commit it after /health/live confirms.
 *
 * Operator pastes an image URL + optional sha256 sidecar URL. While
 * the host-side spatiumddi-slot-upgrade.path unit is running the dd
 * + grub.cfg patch + set-next-boot, this card shows the live log.
 * Once complete (state = "done"), it surfaces a Reboot button — the
 * actual swap happens at next boot, then spatiumddi-firstboot commits.
 */

function slotLabel(s: ApplianceSlot | null): string {
  if (s === "slot_a") return "Slot A (root_a)";
  if (s === "slot_b") return "Slot B (root_b)";
  return "—";
}

// Phase 8b-4 — stable GitHub Release URLs the workflow emits per cut.
// The SlotUpgradeCard pre-fills these so the operator's "Apply"
// click against a fresh install just works without typing.
const DEFAULT_IMAGE_URL =
  "https://github.com/spatiumddi/spatiumddi/releases/latest/download/spatiumddi-appliance-slot-amd64.raw.xz";
const DEFAULT_CHECKSUM_URL =
  "https://github.com/spatiumddi/spatiumddi/releases/latest/download/spatiumddi-appliance-slot-amd64.sha256";

export function SlotUpgradeCard() {
  const qc = useQueryClient();
  const [imageUrl, setImageUrl] = useState(DEFAULT_IMAGE_URL);
  const [checksumUrl, setChecksumUrl] = useState(DEFAULT_CHECKSUM_URL);
  const [rollbackConfirm, setRollbackConfirm] = useState(false);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["appliance", "slot-upgrade"],
    queryFn: applianceSlotApi.status,
    // Poll faster while a slot upgrade is running.
    refetchInterval: (q) =>
      q.state.data?.upgrade_state === "in-flight" ? 3_000 : 30_000,
  });

  const apply = useMutation({
    mutationFn: () =>
      applianceSlotApi.apply(imageUrl.trim(), checksumUrl.trim() || null),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "slot-upgrade"] });
    },
  });

  const rollback = useMutation({
    // Phase 8c-3 — flip the durable default to the inactive slot.
    // ``target_slot: null`` lets the host-side runner auto-pick the
    // inactive slot (matches the "go back to the previous slot" intent).
    mutationFn: () => applianceSlotApi.rollback(null),
    onSuccess: () => {
      setRollbackConfirm(false);
      qc.invalidateQueries({ queryKey: ["appliance", "slot-upgrade"] });
    },
  });

  // Hide entirely on non-appliance deploys; SlotUpgradeCard renders
  // nothing rather than a broken status panel.
  if (data && !data.appliance_mode) {
    return null;
  }

  const inFlight = data?.upgrade_state === "in-flight";
  const trial = data?.is_trial_boot;
  const inactiveSlot: ApplianceSlot | null =
    data?.current_slot === "slot_a"
      ? "slot_b"
      : data?.current_slot === "slot_b"
        ? "slot_a"
        : null;

  return (
    <div className="space-y-3 rounded-lg border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <HardDrive className="h-4 w-4 text-muted-foreground" />
            Appliance OS Image (atomic A/B upgrade)
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Writes a slot <code className="rounded bg-muted px-1">.raw.xz</code>{" "}
            image into the inactive A/B partition, arms grub to try it on next
            boot, and rolls back automatically if{" "}
            <code className="rounded bg-muted px-1">/health/live</code> doesn’t
            come up. Active slot is never touched during apply.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {/* Phase 8c-3 — rollback button. Hidden when there's no
              inactive slot to roll back to (fresh install, no upgrade
              ever applied) and during a trial boot — in a trial boot
              the "inactive" slot is the durable one, so a "rollback
              to inactive" would commit the trial, which is the
              opposite of the operator's intent. During a trial boot
              the right action is just reboot (which reverts), handled
              via the Maintenance tab. */}
          {data?.current_slot && inactiveSlot && !trial && (
            <button
              type="button"
              className="inline-flex items-center gap-1.5 rounded-md border bg-background px-2 py-1 text-xs text-muted-foreground hover:bg-muted disabled:opacity-50"
              onClick={() => setRollbackConfirm(true)}
              disabled={inFlight || rollback.isPending}
              title={`Durably switch back to ${slotLabel(inactiveSlot)} (reboot required to take effect).`}
            >
              <RotateCcw className="h-3 w-3" />
              Rollback to {inactiveSlot === "slot_a" ? "A" : "B"}
            </button>
          )}
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-md border bg-background px-2 py-1 text-xs text-muted-foreground hover:bg-muted"
            onClick={() => refetch()}
            disabled={isLoading}
          >
            <RefreshCw
              className={`h-3 w-3 ${isLoading ? "animate-spin" : ""}`}
            />
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          Failed to load slot status: {(error as Error).message}
        </div>
      )}

      {/* Slot status — current / durable / target */}
      <div className="grid grid-cols-3 gap-2 text-xs">
        <div className="rounded-md border bg-muted/40 p-2">
          <div className="text-muted-foreground">Active (booted)</div>
          <div className="mt-0.5 font-mono font-semibold">
            {slotLabel(data?.current_slot ?? null)}
          </div>
        </div>
        <div className="rounded-md border bg-muted/40 p-2">
          <div className="text-muted-foreground">Durable default</div>
          <div className="mt-0.5 font-mono font-semibold">
            {slotLabel(data?.durable_default ?? null)}
          </div>
        </div>
        <div className="rounded-md border bg-muted/40 p-2">
          <div className="text-muted-foreground">Target (inactive)</div>
          <div className="mt-0.5 font-mono font-semibold">
            {slotLabel(inactiveSlot)}
          </div>
        </div>
      </div>

      {trial && (
        <div className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-2.5 text-xs text-amber-700 dark:text-amber-300">
          <Shield className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <div>
            <div className="font-semibold">Trial boot</div>
            <div className="mt-0.5">
              You’re on{" "}
              <code className="rounded bg-amber-500/10 px-1">
                {data?.current_slot}
              </code>{" "}
              but the durable default is still{" "}
              <code className="rounded bg-amber-500/10 px-1">
                {data?.durable_default}
              </code>
              . Once <code>/health/live</code> confirms, the swap commits
              automatically. A reboot before commit reverts.
            </div>
          </div>
        </div>
      )}

      {/* Apply form */}
      <div className="space-y-2">
        <label className="text-xs font-medium">
          Slot image URL or local path
          <input
            type="text"
            value={imageUrl}
            onChange={(e) => setImageUrl(e.target.value)}
            placeholder="https://github.com/spatiumddi/spatiumddi/releases/download/.../spatiumddi-appliance-slot-amd64.raw.xz"
            disabled={inFlight}
            className="mt-1 block w-full rounded-md border bg-background px-2 py-1.5 font-mono text-xs disabled:opacity-50"
          />
        </label>
        <label className="text-xs font-medium">
          SHA-256 sidecar URL (optional)
          <input
            type="text"
            value={checksumUrl}
            onChange={(e) => setChecksumUrl(e.target.value)}
            placeholder="https://.../spatiumddi-appliance-slot-amd64.sha256"
            disabled={inFlight}
            className="mt-1 block w-full rounded-md border bg-background px-2 py-1.5 font-mono text-xs disabled:opacity-50"
          />
        </label>
        <div className="flex items-center justify-between gap-2">
          <div className="text-xs text-muted-foreground">
            Writes to{" "}
            <code className="rounded bg-muted px-1">
              {slotLabel(inactiveSlot)}
            </code>
            . The active slot is untouched until you reboot.
          </div>
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-semibold text-primary-foreground disabled:opacity-50"
            onClick={() => apply.mutate()}
            disabled={inFlight || !imageUrl.trim() || apply.isPending}
          >
            {inFlight || apply.isPending ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Applying…
              </>
            ) : (
              <>
                <PlayCircle className="h-3.5 w-3.5" />
                Apply to inactive slot
              </>
            )}
          </button>
        </div>
      </div>

      {/* In-flight / result banner */}
      {data?.upgrade_state === "in-flight" && (
        <div className="flex items-start gap-2 rounded-md border border-blue-500/50 bg-blue-500/10 p-2.5 text-xs text-blue-700 dark:text-blue-300">
          <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" />
          <div>
            <div className="font-semibold">Apply running</div>
            <div className="mt-0.5">
              Streaming + decompressing → writing to {slotLabel(inactiveSlot)} →
              patching grub.cfg → arming next-boot. Watch the log below.
            </div>
          </div>
        </div>
      )}

      {data?.upgrade_state === "done" && (
        <div className="flex items-start gap-2 rounded-md border border-green-500/50 bg-green-500/10 p-2.5 text-xs text-green-700 dark:text-green-300">
          <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <div>
            <div className="font-semibold">Apply complete</div>
            <div className="mt-0.5">
              {slotLabel(inactiveSlot)} is ready and armed for next boot. Reboot
              to switch — health-check passes will commit it automatically.
            </div>
          </div>
        </div>
      )}

      {data?.upgrade_state === "failed" && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-2.5 text-xs text-destructive">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <div>
            <div className="font-semibold">Apply failed</div>
            <div className="mt-0.5">
              See the log below for details. Active slot is untouched — you can
              re-try with a different image.
            </div>
          </div>
        </div>
      )}

      {/* Log tail */}
      {data?.log_tail && (
        <div className="space-y-1">
          <div className="text-xs font-medium text-muted-foreground">
            slot-upgrade.log (last 120 lines)
          </div>
          <pre className="max-h-72 overflow-auto rounded-md border bg-muted/40 p-2 font-mono text-[11px] leading-tight">
            {data.log_tail}
          </pre>
        </div>
      )}

      <ConfirmModal
        open={rollbackConfirm}
        title={`Rollback to ${slotLabel(inactiveSlot)}?`}
        message={
          <div className="space-y-2">
            <p>
              This durably flips the boot default from{" "}
              <code className="rounded bg-muted px-1 font-mono">
                {data?.current_slot ?? "—"}
              </code>{" "}
              to{" "}
              <code className="rounded bg-muted px-1 font-mono">
                {inactiveSlot ?? "—"}
              </code>
              . The active slot keeps running until you reboot — the swap
              doesn’t take effect until then.
            </p>
            <p>
              Operator state on <code>/var</code> (databases, container images,
              certs, audit log) is shared across slots and is not touched. The
              OS layer (kernel, systemd units, host binaries) reverts to
              whatever shipped on the target slot. If the target slot is
              unstamped or carries a broken image, reboot will fail and the next
              reboot reverts to the current slot automatically (Phase 8c safety
              net).
            </p>
            {rollback.isError && (
              <p className="text-destructive">
                {(rollback.error as Error).message}
              </p>
            )}
          </div>
        }
        confirmLabel="Rollback"
        cancelLabel="Cancel"
        tone="destructive"
        loading={rollback.isPending}
        onConfirm={() => rollback.mutate()}
        onClose={() => !rollback.isPending && setRollbackConfirm(false)}
      />
    </div>
  );
}
