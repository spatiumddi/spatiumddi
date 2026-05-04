import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { aiApi } from "@/lib/api";
import { CopilotDrawer } from "./CopilotDrawer";
import { onAskAIRequested } from "./askAI";

/**
 * Floating chat button + drawer (issue #90 Wave 3 + Phase 2).
 *
 * Renders bottom-right of any authenticated page when at least one
 * AI provider is enabled. Click to open the chat drawer. Hidden
 * entirely when no provider is configured — operator goes to
 * /admin/ai/providers first.
 *
 * Three ways to open the drawer:
 *   - Click the floating button.
 *   - Cmd-K → "Ask AI: <query>" entry at the bottom of the global
 *     search palette (the palette owns Cmd-K end-to-end so there's
 *     no shortcut conflict).
 *   - ``askAI({ context })`` from anywhere — the "Ask AI about this"
 *     affordances on subnet / IP / DNS rows fire this event.
 *
 * When opened via ``askAI``, the supplied context string is passed
 * to the drawer as ``pendingContext`` and forwarded into the next
 * new-session's system prompt. Once consumed (first chat turn lands)
 * it clears so subsequent turns don't re-inject it.
 */
export function CopilotButton() {
  const [open, setOpen] = useState(false);
  const [pendingContext, setPendingContext] = useState<string | null>(null);
  const [pendingPrompt, setPendingPrompt] = useState<string | null>(null);

  // Tiny query to gate visibility — fetches the provider list once
  // every 5 minutes and renders the button only if ≥ 1 enabled.
  const providersQ = useQuery({
    queryKey: ["ai-providers", "any-enabled"],
    queryFn: aiApi.listProviders,
    staleTime: 5 * 60 * 1000,
    // Fail silently — non-superadmins can't list providers, but they
    // can still use the chat. Treat 403 as "we just don't know" and
    // show the button anyway.
    retry: false,
  });

  // ``askAI({ context, prompt })`` from any "Ask AI about this"
  // affordance — or the Cmd-K palette's "Ask AI: <query>" entry —
  // bubbles up here as a window event. Open the drawer + remember
  // the context (seeds system prompt) and / or the prompt (pre-fills
  // the textarea) so the drawer can hand them off to its children.
  useEffect(() => {
    return onAskAIRequested((detail) => {
      if (detail.context) {
        setPendingContext(detail.context);
      }
      if (detail.prompt) {
        setPendingPrompt(detail.prompt);
      }
      setOpen(true);
    });
  }, []);

  // Hide when we *know* no providers are enabled. Show optimistically
  // when we couldn't fetch (403) since non-superadmins still get to chat.
  const enabledCount = providersQ.data?.filter((p) => p.is_enabled).length;
  if (enabledCount === 0) return null;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title="Operator Copilot"
        className="fixed bottom-4 right-4 z-40 inline-flex items-center gap-1.5 rounded-full bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow-lg transition-transform hover:scale-105 hover:bg-primary/90"
      >
        <Sparkles className="h-4 w-4" />
        <span className="hidden sm:inline">Ask AI</span>
      </button>
      {open && (
        <CopilotDrawer
          onClose={() => {
            setOpen(false);
            setPendingContext(null);
            setPendingPrompt(null);
          }}
          pendingContext={pendingContext}
          onContextConsumed={() => setPendingContext(null)}
          pendingPrompt={pendingPrompt}
          onPromptConsumed={() => setPendingPrompt(null)}
        />
      )}
    </>
  );
}
