import { Sparkles } from "lucide-react";
import { askAI } from "./askAI";

/**
 * Button that opens the Operator Copilot drawer with a context block
 * pre-filled (issue #90 Phase 2). Drop into any list row / detail
 * page header to give the operator a one-click affordance to ask
 * the AI about whatever they're looking at.
 *
 * Pass a human-readable ``context`` string — typically built by
 * stitching together a few interesting columns of the resource
 * ("Subnet 10.0.0.0/24, name 'prod-web', VLAN 100"). The chat
 * drawer appends it to the next new-session's system prompt so the
 * model knows the context without the operator having to restate.
 *
 * Renders icon + "Ask AI" label by default so it's discoverable.
 * Pass ``iconOnly`` for tight rows where there's no room for text;
 * ``tooltip`` carries the longer "Ask AI about this <thing>" form
 * regardless of which mode is rendered.
 */
export function AskAIButton({
  context,
  prompt,
  tooltip = "Ask AI about this",
  size = "sm",
  iconOnly = false,
  className = "",
}: {
  context: string;
  /** Optional default question to prefill the chat composer
   *  textarea. Operator can edit before sending — never auto-sent.
   *  Use this to point the operator at the obvious first question
   *  for the resource type ("Summarise this subnet", "Explain this
   *  alert"). When omitted, the textarea opens empty and the
   *  operator types whatever they like. */
  prompt?: string;
  tooltip?: string;
  size?: "xs" | "sm";
  iconOnly?: boolean;
  className?: string;
}) {
  const sizing = size === "xs" ? "h-3 w-3" : "h-3.5 w-3.5";
  // Mirrors ``HeaderButton`` secondary variant exactly so the button
  // sits at the same visual weight as Edit / Refresh / etc. in a
  // header bar — anything more muted reads as a tertiary utility
  // and operators miss it. Primary tint kicks in on hover to signal
  // "this opens the AI surface".
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        askAI({ context, prompt });
      }}
      title={tooltip}
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-md border px-3 py-1.5 text-sm transition-colors hover:border-primary/40 hover:bg-primary/5 hover:text-primary ${className}`}
    >
      <Sparkles className={`${sizing} text-primary`} />
      {!iconOnly && <span>Ask AI</span>}
    </button>
  );
}
