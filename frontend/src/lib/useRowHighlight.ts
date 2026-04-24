import { useEffect, useRef, useState } from "react";

/**
 * Brief landing-flash for search navigation.
 *
 * Usage:
 *   const { ref, isActive, register } = useRowHighlight(highlightId);
 *   ...
 *   <tr ref={register(addr.id)} className={cn(..., isActive(addr.id) && "spatium-row-highlight")}>
 *
 * - ``register(id)`` returns a ref callback; attach it to every row so the
 *   hook can locate the matching one after mount.
 * - ``isActive(id)`` returns true when the row should carry the flash class.
 * - The hook scrolls the matched row into view once, then clears after
 *   3s so a re-render doesn't re-trigger the animation.
 */
export function useRowHighlight(targetId: string | null | undefined) {
  const [activeId, setActiveId] = useState<string | null>(targetId ?? null);
  const refs = useRef<Map<string, HTMLElement>>(new Map());

  // When the caller's target changes (e.g. new search result), re-arm.
  useEffect(() => {
    if (targetId) setActiveId(targetId);
  }, [targetId]);

  // Scroll + auto-clear once a matching ref shows up.
  useEffect(() => {
    if (!activeId) return;
    // Defer one tick so the ref registration runs after the parent's
    // render commits.
    const raf = requestAnimationFrame(() => {
      const el = refs.current.get(activeId);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
    const timeout = setTimeout(() => setActiveId(null), 3000);
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(timeout);
    };
  }, [activeId]);

  function register(id: string) {
    return (el: HTMLElement | null) => {
      if (el) refs.current.set(id, el);
      else refs.current.delete(id);
    };
  }

  function isActive(id: string) {
    return activeId === id;
  }

  return { register, isActive };
}
