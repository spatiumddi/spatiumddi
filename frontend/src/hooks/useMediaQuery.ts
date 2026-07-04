import { useEffect, useState } from "react";

/**
 * Subscribe to a CSS media query and re-render on match changes.
 *
 * Used to render either the mobile card list OR the desktop table in
 * IPAM — never both — so a busy subnet doesn't mount two full copies of
 * every row (one CSS-hidden). Returns ``false`` during SSR / before the
 * first effect runs; the app is a client-only SPA so that first paint is
 * effectively immediate.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}
