import type { AxiosResponse } from "axios";

import type { ChangeRequestQueued } from "@/lib/api";

/**
 * Detect the two-person approval queue envelope (#62).
 *
 * When the ``governance.approvals`` feature module is on and a policy
 * matches, a covered risky mutation (delete / bulk / factory-reset)
 * returns **202 Accepted** with a ``ChangeRequestQueued`` body instead
 * of performing the change. The operation now sits in the approval queue
 * waiting for a *second* eligible operator to approve it.
 *
 * Covered mutation hooks call this in their ``onSuccess`` and branch on a
 * non-null return: show the toast string below instead of the usual
 * "deleted" feedback, and invalidate the change-request queries so the
 * Sidebar pending-count badge + the Change Requests page refresh.
 *
 * ```ts
 * onSuccess: (resp) => {
 *   const queued = handleApprovalQueued(resp);
 *   if (queued) {
 *     toast(APPROVAL_QUEUED_MESSAGE);
 *     queryClient.invalidateQueries({ queryKey: ["change-requests"] });
 *     return; // skip the normal post-delete invalidations
 *   }
 *   // ... normal success path ...
 * }
 * ```
 *
 * IMPORTANT for callers: the api-client method for a covered route must
 * return the **full axios response** (i.e. ``api.delete(...)`` WITHOUT a
 * trailing ``.then((r) => r.data)``) so the 202 status is observable.
 * ``ipamApi.deleteSubnet`` / ``deleteBlock`` / ``deleteSpace`` already do.
 *
 * @returns the queued payload when the response was a 202 approval-queue
 *   envelope, else ``null`` (the mutation executed inline as before).
 */
export function handleApprovalQueued(
  resp: AxiosResponse<unknown> | unknown,
): ChangeRequestQueued | null {
  const r = resp as AxiosResponse<ChangeRequestQueued> | null | undefined;
  if (
    r &&
    typeof r === "object" &&
    "status" in r &&
    r.status === 202 &&
    r.data &&
    typeof r.data === "object" &&
    "change_request_id" in r.data
  ) {
    return r.data;
  }
  return null;
}

/** Operator-facing feedback when a mutation was queued for approval. */
export const APPROVAL_QUEUED_MESSAGE =
  "Submitted for approval — awaiting a second operator.";

/** React-Query keys the approval queue touches; invalidate both on any
 *  state transition (and after a 202-queued mutation). */
export const CHANGE_REQUEST_QUERY_KEY = ["change-requests"] as const;
export const PENDING_CHANGE_COUNT_QUERY_KEY = [
  "change-requests",
  "pending-count",
] as const;
