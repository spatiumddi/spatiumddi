import { useQuery } from "@tanstack/react-query";
import { featureModulesApi, type FeatureModuleEntry } from "@/lib/api";

/** Single React Query subscription for the feature-module enabled set.
 *
 * Cached for 5 min — toggles are superadmin-rare; the sidebar /
 * Settings page re-renders fast enough on the optimistic update
 * inside the toggle handler. Components that just need to gate
 * visibility (sidebar, Cmd-K, page bodies) read ``enabled(id)``;
 * components that render the full catalog (Settings → Features)
 * read ``modules`` and key by id.
 *
 * On query failure, falls back to "everything enabled" — never hide
 * the sidebar from an operator because the API hiccupped on a
 * background poll. The toggle write always errors loud.
 */
export function useFeatureModules() {
  const query = useQuery({
    queryKey: ["feature-modules"],
    queryFn: featureModulesApi.list,
    staleTime: 5 * 60 * 1000,
  });

  const modules = query.data ?? [];
  const enabledSet = new Set(modules.filter((m) => m.enabled).map((m) => m.id));

  // ``enabled`` is the hot path — every NavItem in the sidebar calls
  // it. When we're still loading (or errored), default to true so we
  // don't blink the section out of existence on every page load.
  function enabled(id: string): boolean {
    if (!query.data) return true;
    return enabledSet.has(id);
  }

  return {
    modules,
    enabled,
    isLoading: query.isLoading,
    isError: query.isError,
    refetch: query.refetch,
  };
}

export type { FeatureModuleEntry };
