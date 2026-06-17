// Shared TLS-cert state helpers (issue #118). Kept out of the page
// component file so React Fast Refresh doesn't warn on mixed
// component/value exports (same split as ``use-draggable-modal.ts``).

import type { TLSCertState } from "@/lib/api";

export const TLS_CERT_STATES: TLSCertState[] = [
  "unknown",
  "ok",
  "expiring",
  "expired",
  "mismatch",
  "unreachable",
];

const STATE_CLS: Record<TLSCertState, string> = {
  ok: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-400",
  expiring:
    "bg-amber-100 text-amber-700 dark:bg-amber-950/30 dark:text-amber-400",
  expired: "bg-red-100 text-red-700 dark:bg-red-950/30 dark:text-red-400",
  mismatch: "bg-red-100 text-red-700 dark:bg-red-950/30 dark:text-red-400",
  unreachable: "bg-red-100 text-red-700 dark:bg-red-950/30 dark:text-red-400",
  unknown: "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
};

export function tlsStateCls(state: TLSCertState): string {
  return STATE_CLS[state] ?? STATE_CLS.unknown;
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}
