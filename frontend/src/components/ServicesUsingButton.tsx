// "Show services using this resource" entry point (issue #99).
//
// The backend ships ``GET /api/v1/services/by-resource/{kind}/{id}`` — a
// reverse lookup that returns every service referencing a given VRF /
// Subnet / IPBlock / DNSZone / DHCPScope / Circuit / Site. This component is
// the operator-facing entry point: it deep-links to the ServicesPage in its
// filtered "by resource" mode (``?resource_kind=…&resource_id=…``).
//
// Two shapes:
//   * header (default) — a ``HeaderButton`` that pre-fetches the count so a
//     0-result resource hides the entry point entirely. Lives in a resource's
//     detail header (VRF detail, subnet/block detail, zone/scope detail).
//   * compact — a small always-visible icon button for list-row action
//     columns (circuits, sites, VRF list). No per-row count fetch — that
//     would be an N+1 across the list; the destination page handles the
//     empty case gracefully.
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Package } from "lucide-react";

import { servicesApi, type ServiceResourceKind } from "@/lib/api";
import { HeaderButton } from "@/components/ui/header-button";
import { useFeatureModules } from "@/hooks/useFeatureModules";

function servicesByResourceHref(
  kind: ServiceResourceKind,
  resourceId: string,
  label?: string,
): string {
  const params = new URLSearchParams({
    resource_kind: kind,
    resource_id: resourceId,
  });
  if (label) params.set("resource_label", label);
  return `/network/services?${params.toString()}`;
}

export function ServicesUsingButton({
  kind,
  resourceId,
  label,
  compact = false,
}: {
  kind: ServiceResourceKind;
  resourceId: string | null | undefined;
  /** Friendly name carried into the filtered view's banner. */
  label?: string;
  compact?: boolean;
}) {
  const navigate = useNavigate();
  const { enabled: moduleEnabled, ready } = useFeatureModules();
  // Services module off → the by-resource page is gone; hide the entry
  // point. (The header variant would also self-hide via its count query
  // 404-ing, but the compact variant renders unconditionally, so gate
  // both here. useFeatureModules is a single cached subscription — no
  // per-row fetch.)
  const moduleOff = ready && !moduleEnabled("network.service");
  const enabled = Boolean(resourceId) && !moduleOff;

  // Header mode pre-fetches so the button can hide itself for 0-result
  // resources. Compact mode never fetches (avoids the per-row N+1).
  const countQ = useQuery({
    queryKey: ["services-by-resource", kind, resourceId],
    queryFn: () => servicesApi.byResource(kind, resourceId as string),
    enabled: enabled && !compact,
    staleTime: 30_000,
  });

  if (!enabled) return null;

  const go = () =>
    navigate(servicesByResourceHref(kind, resourceId as string, label));

  if (compact) {
    return (
      <button
        type="button"
        title="Show services using this resource"
        onClick={go}
        className="rounded p-1 hover:bg-muted"
      >
        <Package className="h-3.5 w-3.5" />
      </button>
    );
  }

  // Render nothing until we know the count — avoids a flicker of "(0)" and
  // hides the entry point entirely when nothing references the resource (or
  // the services module is disabled and the lookup 404s).
  if (!countQ.isSuccess) return null;
  const count = countQ.data.length;
  if (count === 0) return null;

  return (
    <HeaderButton icon={Package} onClick={go}>
      Used by services ({count})
    </HeaderButton>
  );
}
