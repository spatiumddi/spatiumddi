import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";

import { publicSettingsApi, type PublicSettings } from "@/lib/api";

/** The product's own identity, used until the API answers — and as the
 *  permanent answer for installs that never customise anything. */
export const DEFAULT_APP_TITLE = "SpatiumDDI";

const FALLBACK: PublicSettings = {
  app_title: DEFAULT_APP_TITLE,
  login_banner: { enabled: false, title: "", text: "", require_ack: false },
  env_banner: {
    enabled: false,
    text: "",
    bg: "#b91c1c",
    fg: "#ffffff",
    position: "top",
  },
  logo_sha256: null,
};

/** Single React Query subscription for the unauthenticated branding slice
 * (issues #885 / #886 / #887 / #888).
 *
 * One endpoint feeds four surfaces — the login page's banner + logo +
 * title, the environment strip on every authenticated screen, the sidebar
 * brand, and ``document.title``. The alternative was piggybacking the
 * authenticated half on ``/health/platform`` (as maintenance mode does)
 * and fetching the rest separately on the login page, which would have
 * meant two payloads carrying the same fields and two chances to disagree.
 *
 * Cached for 5 min: branding changes are superadmin-rare, and the Settings
 * save handler invalidates this key so the operator sees their own edit
 * immediately rather than waiting out the TTL.
 *
 * On error the fallback above applies — a hiccup on this call must never
 * blank the app's name or strand a banner on screen.
 */
export function usePublicSettings() {
  const query = useQuery({
    queryKey: ["public-settings"],
    queryFn: publicSettingsApi.get,
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

  return {
    settings: query.data ?? FALLBACK,
    /** True once real values have arrived. Gate anything that would look
     *  wrong flashing the default first (the login banner, notably). */
    ready: !!query.data,
    isLoading: query.isLoading,
  };
}

/** Applies ``app_title`` to the browser tab (issue #888 — the setting
 *  existed and was documented as doing this, but reached nothing outside
 *  the OpenAPI page). Called once from the app root so it covers the
 *  login screen as well as the authenticated shell. */
export function useBrandDocumentTitle() {
  const { settings } = usePublicSettings();
  const title = settings.app_title.trim() || DEFAULT_APP_TITLE;

  useEffect(() => {
    document.title = title;
  }, [title]);
}
