import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { aiApi } from "@/lib/api";
import { CopilotDrawer } from "./CopilotDrawer";

/**
 * Floating chat button + drawer (issue #90 Wave 3).
 *
 * Renders bottom-right of any authenticated page when at least one
 * AI provider is enabled. Click to open the chat drawer. Hidden
 * entirely when no provider is configured — operator goes to
 * /admin/ai/providers first.
 *
 * Cmd-K / Ctrl-K shortcut opens the drawer too (matches modern
 * command-palette muscle memory).
 */
export function CopilotButton() {
  const [open, setOpen] = useState(false);

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

  // Cmd-K / Ctrl-K opens the drawer.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
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
        title="Operator Copilot — Cmd/Ctrl+K"
        className="fixed bottom-4 right-4 z-40 inline-flex items-center gap-1.5 rounded-full bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow-lg transition-transform hover:scale-105 hover:bg-primary/90"
      >
        <Sparkles className="h-4 w-4" />
        <span className="hidden sm:inline">Ask AI</span>
      </button>
      {open && <CopilotDrawer onClose={() => setOpen(false)} />}
    </>
  );
}
