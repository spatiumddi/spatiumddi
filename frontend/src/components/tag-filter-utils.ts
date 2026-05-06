/**
 * Pure helpers shared between the ``<TagFilterChips>`` component and
 * any in-memory list surface that needs to apply chip semantics
 * client-side (e.g. the IPAM tree, where ``allSubnets`` is already
 * loaded so a server round-trip is wasted motion).
 *
 * Kept in their own module — not co-located with the component — so
 * the React-refresh rule (``react-refresh/only-export-components``)
 * stays happy: the component file exports only React components, the
 * helpers live here and are tree-shaken into whichever caller imports
 * them.
 *
 * Match semantics mirror the server's ``apply_tag_filter`` exactly,
 * so the same chip string filters identically whether it round-trips
 * through ``?tag=`` or runs in-browser.
 */

/** Match one chip against a row's ``tags`` dict.
 *
 * * ``key`` — true when ``tags`` has the key, regardless of value.
 * * ``key:value`` — true when ``tags[key]`` stringifies to ``value``
 *   (the same byte-for-byte comparison Postgres' ``@>`` performs).
 *
 * Empty / whitespace-only chips short-circuit to ``true`` so a stale
 * blank entry can't collapse the result set.
 */
export function matchTagChip(
  tags: Record<string, unknown> | null | undefined,
  chip: string,
): boolean {
  const trimmed = chip.trim();
  if (!trimmed) return true;
  if (!tags) return false;
  const colon = trimmed.indexOf(":");
  if (colon < 0) return trimmed in tags;
  const key = trimmed.slice(0, colon).trim();
  const value = trimmed.slice(colon + 1).trim();
  if (!key || !(key in tags)) return false;
  if (!value) return true;
  const stored = tags[key];
  return stored != null && String(stored) === value;
}

/** AND every chip — empty array passes everything. */
export function matchesAllTagChips(
  tags: Record<string, unknown> | null | undefined,
  chips: string[],
): boolean {
  if (chips.length === 0) return true;
  return chips.every((c) => matchTagChip(tags, c));
}
