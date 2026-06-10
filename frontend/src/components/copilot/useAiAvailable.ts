import { useQuery } from "@tanstack/react-query";
import { aiApi } from "@/lib/api";
import { useFeatureModules } from "@/hooks/useFeatureModules";

/**
 * Returns ``true`` when at least one AI provider is configured + enabled,
 * or while we don't yet know (loading / 403 for non-superadmins). Returns
 * ``false`` only when we're sure the provider list is empty or all
 * providers are disabled — that's the case where every "Ask AI" affordance
 * should hide because clicking it would land the operator in a chat
 * drawer with nothing to talk to.
 *
 * Mirrors ``CopilotButton``'s gate exactly + shares its query key so
 * React Query dedupes the fetch across every consumer (one HTTP call,
 * many subscribers).
 */
export function useAiAvailable(): boolean {
  const { enabled, ready } = useFeatureModules();
  // Gate on ``ready`` so the query waits for the real module state — without
  // it, ``enabled`` returns true while the module set is still loading and
  // the gated /ai/providers endpoint 404s once on every hard page load.
  const moduleOn = ready && enabled("ai.copilot");
  const providersQ = useQuery({
    queryKey: ["ai-providers", "any-enabled"],
    queryFn: aiApi.listProviders,
    staleTime: 5 * 60 * 1000,
    retry: false,
    enabled: moduleOn,
  });
  // When the Operator Copilot module is off, the gated /ai/providers
  // endpoint 404s — never fire it and never advertise AI as available.
  if (!moduleOn) return false;
  const enabledCount = providersQ.data?.filter((p) => p.is_enabled).length;
  return enabledCount !== 0;
}
