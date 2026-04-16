import { useState } from "react";
import { X } from "lucide-react";

export const inputCls =
  "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label className="block text-xs font-medium text-muted-foreground">
        {label}
      </label>
      {children}
      {hint && <p className="text-[11px] text-muted-foreground/70">{hint}</p>}
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-2 sm:p-4">
      <div
        className={`w-full max-w-[95vw] ${wide ? "sm:max-w-2xl" : "sm:max-w-md"} rounded-lg border bg-card p-4 sm:p-6 shadow-lg max-h-[90vh] overflow-y-auto`}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold">{title}</h2>
          <button
            onClick={onClose}
            className="rounded p-1 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Btns({
  onClose,
  pending,
  label,
}: {
  onClose: () => void;
  pending: boolean;
  label?: string;
}) {
  return (
    <div className="flex justify-end gap-2 pt-2">
      <button
        type="button"
        onClick={onClose}
        className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
      >
        Cancel
      </button>
      <button
        type="submit"
        disabled={pending}
        className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        {pending ? "Saving…" : (label ?? "Save")}
      </button>
    </div>
  );
}

export type ApiError = { response?: { data?: { detail?: unknown } } };

// eslint-disable-next-line react-refresh/only-export-components
export function errMsg(e: unknown, fallback = "Request failed"): string {
  const ae = e as ApiError;
  const d = ae?.response?.data?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    // Pydantic 422 — array of { type, loc, msg, input }.
    return (
      (d as Array<{ loc?: (string | number)[]; msg?: string }>)
        .map((err) => {
          const field = (err.loc ?? []).filter((p) => p !== "body").join(".");
          return field ? `${field}: ${err.msg}` : err.msg;
        })
        .filter(Boolean)
        .join("; ") || fallback
    );
  }
  return fallback;
}

/** Shared destructive-confirm modal (single-step with optional references block). */
export function DeleteConfirmModal({
  title,
  description,
  referencesTitle,
  references,
  onConfirm,
  onClose,
  isPending,
}: {
  title: string;
  description: string;
  referencesTitle?: string;
  references?: string[];
  onConfirm: () => void;
  onClose: () => void;
  isPending?: boolean;
}) {
  const [checked, setChecked] = useState(false);
  return (
    <Modal title={title} onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-muted-foreground">{description}</p>
        {references && references.length > 0 && (
          <div className="rounded-md border bg-muted/40 p-3">
            <p className="text-xs font-medium mb-1.5">
              {referencesTitle ?? "Referenced objects:"}
            </p>
            <ul className="text-xs text-muted-foreground list-disc pl-5 space-y-0.5">
              {references.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </div>
        )}
        <label className="flex cursor-pointer items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={checked}
            onChange={(e) => setChecked(e.target.checked)}
            className="mt-0.5"
          />
          <span>I understand this action cannot be undone.</span>
        </label>
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            disabled={!checked || isPending}
            onClick={onConfirm}
            className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
          >
            {isPending ? "Deleting…" : "Delete"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** Status dot shared across DHCP UI (matches DNS color scheme). */
export function StatusDot({
  status,
  className = "",
}: {
  status: string;
  className?: string;
}) {
  const cls =
    {
      active: "bg-emerald-500",
      syncing: "bg-blue-500",
      unreachable: "bg-red-500",
      error: "bg-red-500",
      pending: "bg-amber-500",
    }[status] ?? "bg-muted";
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full flex-shrink-0 ${cls} ${className}`}
      title={status}
    />
  );
}
