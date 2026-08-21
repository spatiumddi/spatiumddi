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
  const src = settings.logo_sha256
    ? publicSettingsApi.logoUrl(settings.logo_sha256)
    : logoIcon;

  return (
    <img
      src={src}
      alt={settings.app_title.trim() || DEFAULT_APP_TITLE}
      className={className}
    />
  );
}
