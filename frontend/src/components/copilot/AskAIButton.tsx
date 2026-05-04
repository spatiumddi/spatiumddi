import { Sparkles } from "lucide-react";
import { askAI } from "./askAI";

/**
 * Tiny icon-button that opens the Operator Copilot drawer with a
 * context block pre-filled (issue #90 Phase 2). Drop into any list
 * row / detail page header to give the operator a one-click
 * affordance to ask the AI about whatever they're looking at.
 *
 * Pass a human-readable ``context`` string — typically built by
 * stitching together a few interesting columns of the resource
 * ("Subnet 10.0.0.0/24, name 'prod-web', VLAN 100"). The chat
 * drawer appends it to the next new-session's system prompt so the
 * model knows the context without the operator having to restate.
 *
 * The button is intentionally subtle (icon-only by default,
 * primary tint on hover) so it doesn't compete with the row's
 * primary actions (edit, delete).
 */
export function AskAIButton({
  context,
  label = "Ask AI",
  size = "sm",
  className = "",
}: {
  context: string;
  label?: string;
  size?: "xs" | "sm";
  className?: string;
}) {
  const sizing = size === "xs" ? "h-3 w-3" : "h-3.5 w-3.5";
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        askAI({ context });
      }}
      title={label}
      className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:bg-primary/5 hover:text-primary ${className}`}
    >
      <Sparkles className={sizing} />
    </button>
  );
}
