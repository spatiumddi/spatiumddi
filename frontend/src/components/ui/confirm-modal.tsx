import { useEffect, useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";

import { Modal } from "./modal";

// Lightweight Cancel / Confirm dialog. Replaces `window.confirm` so
// every confirmation across the app gets the shared draggable modal
// shell + theme-aware styling. For destructive ops that need an
// extra "I understand this can't be undone" checkbox plus a list of
// referenced rows, use ``pages/dhcp/_shared.tsx``'s
// ``DeleteConfirmModal`` instead — this one is the routine yes/no.
//
// Pass ``requireCheckboxLabel`` to require an explicit "yes I mean
// it" checkbox before the Confirm button enables. Used for actions
// that are non-destructive but operationally heavy (e.g. fleet
// reboot — won't lose data but will take an agent offline for
// 30-60 s).
export function ConfirmModal({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  tone = "default",
  loading = false,
  requireCheckboxLabel,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "destructive";
  loading?: boolean;
  requireCheckboxLabel?: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const [checked, setChecked] = useState(false);

  // Reset the checkbox when the modal opens — otherwise it stays
  // checked across re-opens, which is the opposite of the safety
  // valve we want it to be.
  useEffect(() => {
    if (open) setChecked(false);
  }, [open]);

  if (!open) return null;
  const disabled = loading || (!!requireCheckboxLabel && !checked);
  return (
    <Modal title={title} onClose={onClose}>
      <div className="space-y-4">
        <div className="text-sm text-muted-foreground">{message}</div>
        {requireCheckboxLabel && (
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-0.5 cursor-pointer"
              checked={checked}
              onChange={(e) => setChecked(e.target.checked)}
              disabled={loading}
            />
            <span>{requireCheckboxLabel}</span>
          </label>
        )}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={loading}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={onConfirm}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm disabled:opacity-50",
              tone === "destructive"
                ? "bg-destructive text-destructive-foreground hover:bg-destructive/90"
                : "bg-primary text-primary-foreground hover:bg-primary/90",
            )}
          >
            {loading ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </Modal>
  );
}
