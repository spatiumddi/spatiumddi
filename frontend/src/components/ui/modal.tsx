import { type ReactNode } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { MODAL_BACKDROP_CLS, useDraggableModal } from "./use-draggable-modal";

/** Tab strip for breaking long modals into themed sub-sections.
 *
 * Single source of truth for the visual style — previously the IPAM
 * Templates editor had a one-off implementation; the IPAM
 * Space / Block / Subnet modals now reuse this so every tabbed
 * modal in the app reads the same. Active tab gets a primary-coloured
 * underline; disabled tabs grey out + reject clicks.
 *
 * Place above the per-tab content blocks; render content with a
 * ``{tab === "..." && <div>…</div>}`` guard. The shared Modal above
 * already gives you ``max-h-[90vh] overflow-y-auto`` on the dialog
 * body, so long tab content scrolls within the modal itself.
 */
export function ModalTabs<T extends string>({
  tabs,
  active,
  onChange,
}: {
  tabs: ReadonlyArray<{ key: T; label: string; disabled?: boolean }>;
  active: T;
  onChange: (key: T) => void;
}) {
  return (
    <div className="mb-4 flex flex-wrap gap-1 border-b">
      {tabs.map(({ key, label, disabled }) => (
        <button
          key={key}
          type="button"
          onClick={() => !disabled && onChange(key)}
          disabled={disabled}
          className={cn(
            "-mb-px border-b-2 px-3 py-1.5 text-sm transition-colors",
            active === key
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
            disabled && "opacity-40 cursor-not-allowed",
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

// Single shared Modal primitive. Previously each page carried a local copy
// with minor CSS drift — this is the consolidated version.
//
// Behavior:
//   * Title bar is a drag handle (``cursor-grab`` / ``active:cursor-grabbing``).
//     Users can pull the modal out of the way to see content behind it.
//   * Backdrop is ``bg-black/20`` so the underlying page is still readable
//     while the modal is open (previous dim was ``/40``).
//   * Esc closes the modal — defense against dragging the dialog off-screen.
//   * Clicking the close button (the X in the title) or anywhere inside the
//     dialog body does NOT start a drag; only the title text / empty areas
//     of the header do.
//
// API is a strict superset of every pre-existing local ``Modal``:
//   { title, onClose, children, wide? }
// so call sites that don't pass ``wide`` keep the narrow default.
//
// Non-standard modal shapes (with a border-b header + a footer slot, or a
// ``bg-background`` card) use ``useDraggableModal()`` + ``MODAL_BACKDROP_CLS``
// directly to pick up the same drag behavior without adopting this API.
export function Modal({
  title,
  onClose,
  children,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  const { dialogStyle, dragHandleProps } = useDraggableModal(onClose);

  return (
    <div className={MODAL_BACKDROP_CLS}>
      <div
        className={cn(
          "w-full rounded-lg border bg-card p-4 sm:p-6 shadow-lg max-h-[90vh] overflow-y-auto max-w-[95vw]",
          wide ? "sm:max-w-2xl" : "sm:max-w-md",
        )}
        style={dialogStyle}
      >
        <div
          {...dragHandleProps}
          className={cn(
            "mb-4 flex items-center justify-between",
            dragHandleProps.className,
          )}
        >
          <h2 className="text-base font-semibold">{title}</h2>
          <button
            onClick={onClose}
            className="cursor-pointer rounded p-1 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
