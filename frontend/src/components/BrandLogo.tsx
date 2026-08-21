import { useEffect, useState } from "react";

import logoIcon from "@/assets/logo-icon.svg";
import {
  usePublicSettings,
  DEFAULT_APP_TITLE,
} from "@/hooks/usePublicSettings";
import { publicSettingsApi } from "@/lib/api";

/**
 * The product mark — an operator-uploaded logo when one exists (issue
 * #886), the bundled asset otherwise. Shared by the sidebar and the login
 * page so a custom logo can never appear in one and not the other.
 *
 * The custom logo is served from our own API rather than a CDN or a data
 * URI, which keeps it inside the ``img-src 'self'`` CSP without widening
 * the policy.
 */
export function BrandLogo({ className }: { className?: string }) {
  const { settings } = usePublicSettings();
  const custom = settings.logo_sha256
    ? publicSettingsApi.logoUrl(settings.logo_sha256)
    : null;

  // Fall back to the bundled mark if the custom logo fails to load. This
  // is not hypothetical: another operator deleting or replacing the logo
  // leaves every other open tab requesting the old sha until its cached
  // ``public-settings`` query goes stale (5 min), and without this the
  // sidebar shows a broken-image glyph for that whole window.
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [custom]);

  return (
    <img
      src={custom && !failed ? custom : logoIcon}
      onError={() => setFailed(true)}
      alt={settings.app_title.trim() || DEFAULT_APP_TITLE}
      className={className}
    />
  );
}
