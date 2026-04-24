import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Brief landing-flash for search navigation.
 *
 * Usage:
 *   const { register, isActive } = useRowHighlight(highlightId);
 *   <tr ref={register(addr.id)} className={cn(..., isActive(addr.id) && "spatium-row-highlight")}>
 *
 * The matching row auto-scrolls into view as soon as it mounts, even
 * if that mount happens well after navigation (common case: the table
 * is populated by a pending query). The CSS keyframe
 * ``.spatium-row-highlight`` uses ``animation-fill-mode: forwards`` so
 * it plays once and fades to the baseline — we don't need to actively
 * clear the active-id state.
 */
export function useRowHighlight(targetId: string | null | undefined) {
  const [activeId, setActiveId] = useState<string | null>(targetId ?? null);
  // Track which ids we've already scrolled into view so a re-render
  // doesn't re-scroll (jarring on every state change).
  const scrolledRef = useRef<Set<string>>(new Set());

  // Sync to the caller's target whenever it changes — including to
  // ``null``, so the parent can clear the highlight by setting the
  // prop back to null / undefined.
  useEffect(() => {
    const next = targetId ?? null;
    setActiveId(next);
    if (next) scrolledRef.current.delete(next);
  }, [targetId]);

  const register = useCallback(
    (id: string) => (el: HTMLElement | null) => {
      if (!el) return;
      if (id !== activeId) return;
      if (scrolledRef.current.has(id)) return;
      scrolledRef.current.add(id);
      // Defer one frame so the row has painted before we scroll.
      requestAnimationFrame(() => {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    },
    [activeId],
  );

  const isActive = useCallback((id: string) => id === activeId, [activeId]);

  return { register, isActive };
}
