import { usePublicSettings } from "@/hooks/usePublicSettings";

/**
 * Operator-defined environment strip — "you are on the DEV box" (issue
 * #887). Palo Alto and F5 both ship this, and for the same reason: the
 * expensive mistake is making a real change on a box you thought was a
 * lab.
 *
 * Colours are operator-picked hex, so they land in an inline ``style``
 * rather than Tailwind classes — the palette isn't known at build time.
 *
 * Rendered above the maintenance banners (identity first, then state) and
 * on the login page, where "which box is this" matters most.
 */
export function EnvironmentBanner({ edge }: { edge: "top" | "bottom" }) {
  const { settings } = usePublicSettings();
  const banner = settings.env_banner;

  if (!banner.enabled) return null;
  const text = banner.text.trim();
  if (!text) return null;
  if (banner.position !== "both" && banner.position !== edge) return null;

  return (
    <div
      // Not aria-live: the text never changes while the page is open, and
      // announcing it on every render would be noise. It reads in normal
      // document order instead.
      className={
        edge === "top"
          ? "flex-shrink-0 border-b px-4 py-1 text-center text-xs font-semibold tracking-wide"
          : "flex-shrink-0 border-t px-4 py-1 text-center text-xs font-semibold tracking-wide"
      }
      style={{
        backgroundColor: banner.bg,
        color: banner.fg,
        borderColor: banner.bg,
      }}
    >
      {text}
    </div>
  );
}
