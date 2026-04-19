import { useEffect, useRef, useState } from "react";

/** CSS for the outer flex-center backdrop. Same across every modal. */
export const MODAL_BACKDROP_CLS =
  "fixed inset-0 z-50 flex items-center justify-center bg-black/20 p-2 sm:p-4";

/**
 * Drag hook for modal dialogs. Returns a style to apply to the dialog card
 * (``transform: translate(x, y)``) and a set of props to spread on whatever
 * element should act as the drag handle (typically the title bar).
 *
 * Esc is bound here too so any modal using the hook gets close-on-Escape
 * "for free" — the safety net when a user drags the dialog off-screen.
 *
 * Lives in its own file (not ``modal.tsx``) so Vite's fast-refresh plugin
 * doesn't warn about mixed component + utility exports.
 */
export function useDraggableModal(onClose: () => void) {
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const dragRef = useRef<{
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const d = dragRef.current;
      if (!d) return;
      setOffset({
        x: d.originX + (e.clientX - d.startX),
        y: d.originY + (e.clientY - d.startY),
      });
    };
    const onUp = () => {
      dragRef.current = null;
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const startDrag = (e: React.MouseEvent) => {
    // Don't start dragging when the press landed on an interactive control
    // (close button, form input, etc.) — the hit-test walks up to the
    // nearest <button> or <input>/<select>/<textarea> via ``closest``.
    const target = e.target as HTMLElement;
    if (target.closest("button, input, select, textarea, a")) return;
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      originX: offset.x,
      originY: offset.y,
    };
    e.preventDefault();
  };

  return {
    dialogStyle: { transform: `translate(${offset.x}px, ${offset.y}px)` },
    dragHandleProps: {
      onMouseDown: startDrag,
      className: "cursor-grab active:cursor-grabbing select-none",
      title: "Drag to move",
    },
  };
}
