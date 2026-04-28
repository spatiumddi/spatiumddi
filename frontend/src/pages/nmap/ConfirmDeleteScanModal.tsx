import { Modal } from "@/components/ui/modal";
import type { NmapScanRead } from "@/lib/api";

export interface ConfirmDeleteScanModalProps {
  scan: NmapScanRead;
  onConfirm: () => void;
  onClose: () => void;
  pending?: boolean;
}

export function ConfirmDeleteScanModal({
  scan,
  onConfirm,
  onClose,
  pending,
}: ConfirmDeleteScanModalProps) {
  const inFlight = scan.status === "queued" || scan.status === "running";
  const title = inFlight ? "Cancel scan" : "Delete scan";
  const verb = inFlight ? "Cancel" : "Delete";

  return (
    <Modal title={title} onClose={onClose}>
      <div className="space-y-3 text-sm">
        <p>
          {inFlight ? (
            <>
              Cancel the running scan against{" "}
              <span className="font-mono">{scan.target_ip}</span>? The runner
              will self-terminate on its next status check.
            </>
          ) : (
            <>
              Permanently delete the scan record for{" "}
              <span className="font-mono">{scan.target_ip}</span> ({scan.preset}
              , {scan.status})? This cannot be undone.
            </>
          )}
        </p>
        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            disabled={pending}
            className="rounded-md border px-3 py-1.5 text-xs hover:bg-accent"
          >
            Keep
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={pending}
            className="rounded-md bg-destructive px-3 py-1.5 text-xs text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
          >
            {pending ? "Working…" : verb}
          </button>
        </div>
      </div>
    </Modal>
  );
}
