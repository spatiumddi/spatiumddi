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
/**
 * Resolve the enabled set the way the server does (#1068).
 *
 * `entry.enabled` is a module's OWN state. A module is only actually on
 * when its whole `requires` ancestry is on too — `get_enabled_modules` in
 * `app/services/feature_modules.py` applies exactly this rule, and the two
 * MUST agree: if the sidebar reads a child as enabled while the router has
 * resolved it off, the operator gets a nav row whose page 404s.
 *
 * Matches the server's forgiving edges deliberately: an unknown parent id
 * resolves TRUE (a rename must never black out a subtree), and a cycle
 * resolves FALSE for the repeated id rather than recursing forever. The
 * catalog itself is pinned acyclic and dangling-free by
 * `tests/test_feature_module_defaults.py`.
 */
export function resolveEnabled(modules: FeatureModuleEntry[]): Set<string> {
  const byId = new Map(modules.map((m) => [m.id, m]));
  const memo = new Map<string, boolean>();

  function resolve(id: string, seen: Set<string>): boolean {
    const cached = memo.get(id);
    if (cached !== undefined) return cached;
    if (seen.has(id)) return false;
    const entry = byId.get(id);
    if (!entry) return true;
    const ok =
      entry.enabled &&
      entry.requires.every((parent) => resolve(parent, new Set([...seen, id])));
    memo.set(id, ok);
    return ok;
  }

  return new Set(
    modules.filter((m) => resolve(m.id, new Set())).map((m) => m.id),
  );
}

export function useFeatureModules() {
  const query = useQuery({
    queryKey: ["feature-modules"],
    queryFn: featureModulesApi.list,
    staleTime: 5 * 60 * 1000,
  });

  const modules = query.data ?? [];
  const enabledSet = resolveEnabled(modules);

  // ``enabled`` is the hot path — every NavItem in the sidebar calls
  // it. When we're still loading (or errored), default to true so we
  // don't blink the section out of existence on every page load.
  function enabled(id: string): boolean {
    if (!query.data) return true;
    return enabledSet.has(id);
  }

  // ``ready`` is true once the module set has actually loaded. Gate DATA
  // queries that hit feature-module-gated endpoints on ``ready && enabled(id)``:
  // ``enabled`` optimistically returns true while loading (so the sidebar
  // doesn't blink), which would otherwise fire a gated query once on a hard
  // page load and 404 before the real module state is known.
  const ready = !!query.data;

  return {
    modules,
    enabled,
    ready,
    isLoading: query.isLoading,
    isError: query.isError,
    refetch: query.refetch,
  };
}

export type { FeatureModuleEntry };
