import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Box,
  CheckCircle2,
  ChevronRight,
  Download,
  ExternalLink,
  Loader2,
  RefreshCw,
} from "lucide-react";

import { applianceReleasesApi, type ApplianceRelease } from "@/lib/api";
import { Modal } from "@/components/ui/modal";

// Number of most-recent releases rendered as full cards. Anything older
// collapses behind a "Show N older releases" disclosure so the tab
// stays scannable even after dozens of releases have shipped.
const FULL_CARDS = 3;

/**
 * Phase 4c — Release management.
 *
 * Lists recent GitHub releases (top 25, 60 s cached server-side),
 * shows the currently-installed version, and lets the operator
 * one-click an upgrade. The actual `docker-compose pull && up -d`
 * runs on the host via a systemd Path unit so the api container can
 * recreate itself cleanly mid-upgrade.
 *
 * While an upgrade is in flight, the apply buttons are disabled and
 * a "Update log" card auto-tails /var/log/spatiumddi/update.log via
 * a 3-second poll. Once /api/v1/version reports a different version
 * the upgrade is complete (poll interval bumps back down).
 *
 * Apply is gated on ``applianceMode`` — the host-side systemd Path
 * unit (``spatiumddi-update.path``) only exists on a SpatiumDDI OS
 * appliance. On docker / k8s control planes, Apply is replaced with a
 * "manual upgrade" command modal carrying the appropriate
 * ``docker compose pull`` / ``helm upgrade`` invocation.
 */
export function ReleasesTab({
  applianceMode = false,
}: {
  applianceMode?: boolean;
}) {
  const qc = useQueryClient();
  const [confirmTarget, setConfirmTarget] = useState<ApplianceRelease | null>(
    null,
  );

  const { data, isLoading, error } = useQuery({
    queryKey: ["appliance", "releases"],
    queryFn: applianceReleasesApi.list,
    // Poll faster while an apply is in flight so the log + the
    // is_installed marker on each card update without manual refresh.
    refetchInterval: (q) => (q.state.data?.apply_in_flight ? 3_000 : 60_000),
  });

  const apply = useMutation({
    mutationFn: applianceReleasesApi.apply,
    onSuccess: () => {
      setConfirmTarget(null);
      qc.invalidateQueries({ queryKey: ["appliance", "releases"] });
    },
  });

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <Box className="h-4 w-4 text-muted-foreground" />
            SpatiumDDI Releases
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Updates pull new container images from{" "}
            <code className="rounded bg-muted px-1">ghcr.io/spatiumddi</code>{" "}
            and recycle the stack via a host-side systemd unit so the api can
            replace itself cleanly. The web UI reconnects automatically once the
            new version is healthy.
          </p>
        </div>
        <div className="shrink-0 rounded-md border bg-muted px-2 py-1.5 text-xs">
          Running:{" "}
          <span className="ml-1 font-mono text-foreground">
            {data?.installed_version ?? "—"}
          </span>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          Failed to load releases: {(error as Error).message}
        </div>
      )}

      {data?.apply_in_flight && (
        <div className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3">
          <div className="flex items-start gap-2">
            <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-amber-600 dark:text-amber-400" />
            <div className="flex-1 text-xs">
              <p className="font-medium text-amber-700 dark:text-amber-400">
                Upgrade in flight — pulling images + recycling the stack
              </p>
              <p className="mt-0.5 text-amber-700/80 dark:text-amber-400/80">
                The api container will recycle itself; this page may go blank
                for ~30 s and come back on the new version. Don't close the
                browser tab.
              </p>
            </div>
          </div>
        </div>
      )}

      {(data?.update_log_tail ?? "").length > 0 && (
        <details
          className="rounded-md border bg-card"
          open={data?.apply_in_flight}
        >
          <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-muted-foreground hover:bg-muted/50">
            Update log (tail) — last apply
          </summary>
          <pre className="overflow-auto border-t bg-muted/30 px-3 py-2 font-mono text-[11px] leading-tight">
            {data?.update_log_tail}
          </pre>
        </details>
      )}

      {isLoading ? (
        <div className="py-12 text-center text-sm text-muted-foreground">
          Loading…
        </div>
      ) : !data || data.releases.length === 0 ? (
        <div className="rounded-lg border border-dashed bg-muted/30 px-6 py-12 text-center text-sm text-muted-foreground">
          <RefreshCw className="mx-auto h-6 w-6 text-muted-foreground/50" />
          <p className="mt-3 font-medium">No releases found</p>
          <p className="mt-1 text-xs">
            GitHub API may be rate-limiting unauthenticated requests, or no
            releases have been published for this repo yet. Refresh in a minute.
          </p>
        </div>
      ) : (
        <ReleasesList
          releases={data.releases}
          applyInFlight={data.apply_in_flight}
          applianceMode={applianceMode}
          onApply={(rel) => setConfirmTarget(rel)}
        />
      )}

      {confirmTarget && (
        <ApplyConfirmModal
          release={confirmTarget}
          installed={data?.installed_version ?? "—"}
          onClose={() => !apply.isPending && setConfirmTarget(null)}
          onConfirm={() => apply.mutate(confirmTarget.tag)}
          submitting={apply.isPending}
          error={apply.isError ? (apply.error as Error).message : null}
        />
      )}
    </div>
  );
}

function ReleasesList({
  releases,
  applyInFlight,
  applianceMode,
  onApply,
}: {
  releases: ApplianceRelease[];
  applyInFlight: boolean;
  applianceMode: boolean;
  onApply: (rel: ApplianceRelease) => void;
}) {
  // Always render the top ``FULL_CARDS`` as full-detail cards. The
  // remainder collapses behind a disclosure to keep the page scannable
  // once the project has shipped dozens of releases. Releases are
  // returned newest-first from the backend, so a simple slice is fine.
  const recent = releases.slice(0, FULL_CARDS);
  const older = releases.slice(FULL_CARDS);

  // If the operator's currently-installed version sits inside the
  // older bucket (they're behind on upgrades), flag it on the disclosure
  // so they can find their row without expanding-and-scanning.
  const installedInOlder = older.find((r) => r.is_installed);

  return (
    <div className="space-y-3">
      {recent.map((rel) => (
        <ReleaseCard
          key={rel.tag}
          release={rel}
          disabled={applyInFlight}
          applianceMode={applianceMode}
          onApply={() => onApply(rel)}
        />
      ))}
      {older.length > 0 && (
        <details
          className="group rounded-lg border bg-card"
          open={applyInFlight}
        >
          <summary className="flex cursor-pointer items-center gap-2 px-4 py-2.5 text-sm hover:bg-muted/50">
            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
            <span className="font-medium">
              Show {older.length} older release{older.length === 1 ? "" : "s"}
            </span>
            {installedInOlder && (
              <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-primary">
                <CheckCircle2 className="h-3 w-3" />
                installed: {installedInOlder.tag}
              </span>
            )}
          </summary>
          <div className="divide-y border-t">
            {older.map((rel) => (
              <CompactReleaseRow
                key={rel.tag}
                release={rel}
                disabled={applyInFlight}
                applianceMode={applianceMode}
                onApply={() => onApply(rel)}
              />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

// One-line compact row used inside the "older releases" disclosure.
// Notes are collapsed by default behind a click-to-expand — operators
// scanning for a specific tag shouldn't have to scroll past every
// release's notes preview.
function CompactReleaseRow({
  release,
  disabled,
  applianceMode,
  onApply,
}: {
  release: ApplianceRelease;
  disabled: boolean;
  applianceMode: boolean;
  onApply: () => void;
}) {
  const [manualOpen, setManualOpen] = useState(false);
  return (
    <div className={release.is_installed ? "bg-primary/5" : ""}>
      <div className="flex items-center gap-2 px-4 py-2">
        <span className="min-w-0 flex-1 font-mono text-xs">{release.tag}</span>
        {release.is_installed && (
          <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-primary">
            <CheckCircle2 className="h-3 w-3" />
            Installed
          </span>
        )}
        {release.is_prerelease && (
          <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-700 dark:text-amber-400">
            Pre
          </span>
        )}
        <span className="shrink-0 text-xs text-muted-foreground">
          {fmtDate(release.published_at)}
        </span>
        <a
          href={release.html_url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex shrink-0 items-center text-muted-foreground hover:text-foreground"
          title="Open on GitHub"
        >
          <ExternalLink className="h-3 w-3" />
        </a>
        {release.is_installed ? (
          <span className="shrink-0 text-xs text-muted-foreground">Active</span>
        ) : applianceMode ? (
          <button
            type="button"
            onClick={onApply}
            disabled={disabled}
            className="inline-flex shrink-0 items-center gap-1 rounded-md border bg-background px-2 py-1 text-xs font-medium hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Download className="h-3 w-3" />
            Apply
          </button>
        ) : (
          <button
            type="button"
            onClick={() => setManualOpen(true)}
            className="inline-flex shrink-0 items-center gap-1 rounded-md border bg-background px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
            title="Show the operator-run upgrade command for docker / k8s"
          >
            <Download className="h-3 w-3" />
            Manual…
          </button>
        )}
      </div>
      {release.body && (
        <details className="px-4 pb-2">
          <summary className="cursor-pointer text-[11px] text-muted-foreground hover:text-foreground">
            Release notes
          </summary>
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded border bg-muted/30 px-2 py-1.5 text-[11px] leading-tight">
            {release.body}
          </pre>
        </details>
      )}
      {manualOpen && (
        <ManualApplyModal
          tag={release.tag}
          onClose={() => setManualOpen(false)}
        />
      )}
    </div>
  );
}

function ReleaseCard({
  release,
  disabled,
  applianceMode,
  onApply,
}: {
  release: ApplianceRelease;
  disabled: boolean;
  applianceMode: boolean;
  onApply: () => void;
}) {
  const [manualOpen, setManualOpen] = useState(false);
  return (
    <div
      className={`rounded-lg border bg-card p-4 shadow-sm ${
        release.is_installed ? "ring-1 ring-primary/40" : ""
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold">
              <span className="font-mono">{release.tag}</span>
            </h3>
            {release.is_installed && (
              <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-primary">
                <CheckCircle2 className="h-3 w-3" />
                Installed
              </span>
            )}
            {release.is_prerelease && (
              <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-700 dark:text-amber-400">
                Pre-release
              </span>
            )}
            <span className="text-xs text-muted-foreground">
              {fmtDate(release.published_at)}
            </span>
            <a
              href={release.html_url}
              target="_blank"
              rel="noopener noreferrer"
              className="ml-auto inline-flex shrink-0 items-center gap-0.5 text-xs text-muted-foreground hover:text-foreground"
            >
              <ExternalLink className="h-3 w-3" />
              GitHub
            </a>
          </div>
          {release.body && (
            <details className="mt-2">
              <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
                Release notes
              </summary>
              <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded border bg-muted/30 px-2 py-1.5 text-[11px] leading-tight">
                {release.body}
              </pre>
            </details>
          )}
        </div>
        <div className="shrink-0">
          {release.is_installed ? (
            <span className="text-xs text-muted-foreground">Active</span>
          ) : applianceMode ? (
            <button
              type="button"
              onClick={onApply}
              disabled={disabled}
              className="inline-flex items-center gap-1 rounded-md border bg-background px-2 py-1 text-xs font-medium hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Download className="h-3 w-3" />
              Apply
            </button>
          ) : (
            <button
              type="button"
              onClick={() => setManualOpen(true)}
              className="inline-flex items-center gap-1 rounded-md border bg-background px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
              title="Show the operator-run upgrade command for docker / k8s"
            >
              <Download className="h-3 w-3" />
              Manual…
            </button>
          )}
        </div>
      </div>
      {manualOpen && (
        <ManualApplyModal
          tag={release.tag}
          onClose={() => setManualOpen(false)}
        />
      )}
    </div>
  );
}

// Replaces the Apply button on docker / k8s control planes. The
// host-side systemd Path unit that the appliance flow relies on
// (``spatiumddi-update.path``) doesn't exist here, so a one-click
// apply isn't possible. Operator copies a command + runs it against
// the deployment they manage.
function ManualApplyModal({
  tag,
  onClose,
}: {
  tag: string;
  onClose: () => void;
}) {
  const dockerCmd = [
    "# On the control-plane host, in the directory holding docker-compose.yml:",
    `SPATIUMDDI_VERSION=${tag} docker compose pull && \\`,
    `SPATIUMDDI_VERSION=${tag} docker compose up -d`,
  ].join("\n");
  const k8sCmd = [
    "# On a workstation with kubectl + helm pointed at the cluster:",
    "helm upgrade spatiumddi \\",
    "  oci://ghcr.io/spatiumddi/charts/spatiumddi \\",
    `  --set image.tag=${tag} \\`,
    "  --reuse-values",
  ].join("\n");
  return (
    <Modal title={`Manual upgrade — ${tag}`} onClose={onClose} wide>
      <div className="space-y-3 text-sm">
        <p className="text-muted-foreground">
          This control plane runs on docker / kubernetes — there's no host-side
          update unit to trigger a one-click apply. Pick the deploy shape that
          matches your install, copy the command, and run it on the
          control-plane host.
        </p>
        <div>
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="text-xs font-medium">Docker compose</span>
            <button
              type="button"
              className="text-xs text-muted-foreground hover:text-foreground"
              onClick={() => {
                if (navigator.clipboard) {
                  void navigator.clipboard.writeText(dockerCmd);
                }
              }}
            >
              Copy
            </button>
          </div>
          <pre className="overflow-x-auto rounded-md border bg-muted/40 p-2 font-mono text-[11px] leading-tight">
            {dockerCmd}
          </pre>
        </div>
        <div>
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="text-xs font-medium">Kubernetes (helm)</span>
            <button
              type="button"
              className="text-xs text-muted-foreground hover:text-foreground"
              onClick={() => {
                if (navigator.clipboard) {
                  void navigator.clipboard.writeText(k8sCmd);
                }
              }}
            >
              Copy
            </button>
          </div>
          <pre className="overflow-x-auto rounded-md border bg-muted/40 p-2 font-mono text-[11px] leading-tight">
            {k8sCmd}
          </pre>
        </div>
        <div className="flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Close
          </button>
        </div>
      </div>
    </Modal>
  );
}

function ApplyConfirmModal({
  release,
  installed,
  onClose,
  onConfirm,
  submitting,
  error,
}: {
  release: ApplianceRelease;
  installed: string;
  onClose: () => void;
  onConfirm: () => void;
  submitting: boolean;
  error: string | null;
}) {
  return (
    <Modal title="Apply release" onClose={onClose} wide>
      <div className="space-y-3 text-sm">
        <p>
          Upgrade the appliance stack from{" "}
          <code className="rounded bg-muted px-1 py-0.5 font-mono">
            {installed}
          </code>{" "}
          to{" "}
          <code className="rounded bg-muted px-1 py-0.5 font-mono">
            {release.tag}
          </code>
          ?
        </p>
        <p className="text-xs text-muted-foreground">
          The host will pull the new image set and recycle every container.
          Expect a ~30 second blackout while the api container restarts. DNS and
          DHCP service continue serving from cache during the recycle.
        </p>
        {release.is_prerelease && (
          <div className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-2 text-xs">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
            <span className="text-amber-700 dark:text-amber-400">
              This release is marked as pre-release on GitHub. Not recommended
              for production appliances.
            </span>
          </div>
        )}
        {error && (
          <div className="flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-2 text-xs text-destructive">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}
        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="rounded-md border bg-background px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={submitting}
            className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            <Download className="h-3.5 w-3.5" />
            {submitting ? "Scheduling…" : `Apply ${release.tag}`}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function fmtDate(s: string): string {
  return new Date(s).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
