/**
 * Cross-tree communication helpers for the "Ask AI about this"
 * affordances (issue #90 Phase 2).
 *
 * Any component can fire ``askAI({ context })`` to:
 *   1. open the chat drawer
 *   2. seed the next new-session's system prompt with ``context``
 *
 * The :class:`CopilotButton` is the always-mounted listener; it
 * stores the context in component state and passes it to the drawer
 * as a prop. We use a window event rather than a React Context so
 * the trigger doesn't need to be inside any provider — affordances
 * scattered across IPAM / DNS / DHCP pages just import this file
 * and call ``askAI()`` without any setup.
 */

const COPILOT_OPEN_EVENT = "copilot:open";

export interface AskAIDetail {
  /** Human-readable context block injected into the new session's
   *  system prompt. Keep concise — caps at 4000 chars on the backend. */
  context?: string;
  /** Prefill text for the chat composer textarea. Used by the
   *  Cmd-K "Ask AI: <query>" entry — the operator's typed query
   *  becomes the next message but they can still edit before
   *  sending. Cleared once the drawer reads it. */
  prompt?: string;
}

export function askAI(detail: AskAIDetail = {}): void {
  window.dispatchEvent(
    new CustomEvent<AskAIDetail>(COPILOT_OPEN_EVENT, { detail }),
  );
}

export function onAskAIRequested(
  handler: (detail: AskAIDetail) => void,
): () => void {
  const fn = (e: Event) => {
    const evt = e as CustomEvent<AskAIDetail>;
    handler(evt.detail ?? {});
  };
  window.addEventListener(COPILOT_OPEN_EVENT, fn);
  return () => window.removeEventListener(COPILOT_OPEN_EVENT, fn);
}
