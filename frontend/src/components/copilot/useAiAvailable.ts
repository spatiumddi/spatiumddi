import { useQuery } from "@tanstack/react-query";
import { aiApi } from "@/lib/api";

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
  const providersQ = useQuery({
    queryKey: ["ai-providers", "any-enabled"],
    queryFn: aiApi.listProviders,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
  const enabledCount = providersQ.data?.filter((p) => p.is_enabled).length;
  return enabledCount !== 0;
}
