import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Clock, Loader2 } from "lucide-react";

import { settingsApi, formatApiError } from "@/lib/api";

/**
 * Appliance timezone control (issue #165).
 *
 * Settings → NTP tab uses this. The picker is a text input backed by
 * a ``<datalist>`` of every IANA tz name the browser reports via
 * ``Intl.supportedValuesOf("timeZone")`` — gives auto-complete
 * without committing to a long ``<select>`` UX. Empty string clears
 * the override (host falls back to install-time default).
 *
 * Apply flow: PUT ``/api/v1/settings`` → ``platform_settings.timezone``
 * → supervisor heartbeat ships ``desired_timezone`` → supervisor's
 * ``maybe_fire_timezone`` writes ``/var/lib/spatiumddi-host/release-
 * state/tz-pending`` → host ``spatiumddi-tz-reload.path`` fires →
 * runner calls ``timedatectl set-timezone``.
 *
 * The change typically lands within a heartbeat cycle (~5-10 s on
 * the default cadence). The current value field reflects the
 * operator-set DB value, NOT the host's current tz — those can drift
 * during the apply window but converge after the runner reports
 * back. (A future commit could surface the host's last-applied tz
 * via the heartbeat's ``host_state``; deferred to keep this PR
 * focused on the operator-set path.)
 */
interface Props {
  currentValue: string;
  isSuperadmin: boolean;
  applianceMode: boolean;
}

// Fallback IANA list for browsers that don't expose
// ``Intl.supportedValuesOf`` (pre-2022). The full set is ~600 names;
// this trims to common regions to keep bundle weight reasonable on
// the unlikely-old-browser path. Modern browsers ignore this.
const TZ_FALLBACK = [
  "UTC",
  "Africa/Cairo",
  "Africa/Johannesburg",
  "America/Anchorage",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/New_York",
  "America/Sao_Paulo",
  "America/Toronto",
  "America/Vancouver",
  "Asia/Dubai",
  "Asia/Hong_Kong",
  "Asia/Kolkata",
  "Asia/Seoul",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Australia/Sydney",
  "Europe/Amsterdam",
  "Europe/Berlin",
  "Europe/London",
  "Europe/Madrid",
  "Europe/Paris",
  "Europe/Rome",
  "Europe/Stockholm",
  "Europe/Warsaw",
  "Europe/Zurich",
  "Pacific/Auckland",
  "Pacific/Honolulu",
] as const;

export function TimezoneSection({
  currentValue,
  isSuperadmin,
  applianceMode,
}: Props) {
  const qc = useQueryClient();
  const [tz, setTz] = useState(currentValue);
  const [savedNote, setSavedNote] = useState<string | null>(null);

  // Resolve the IANA list once per mount. Modern browsers return the
  // full set; older fallback to the curated subset above.
  const ianaList = useMemo(() => {
    type IntlWithList = typeof Intl & {
      supportedValuesOf?: (key: "timeZone") => string[];
    };
    const I = Intl as IntlWithList;
    if (typeof I.supportedValuesOf === "function") {
      try {
        return I.supportedValuesOf("timeZone");
      } catch {
        return [...TZ_FALLBACK];
      }
    }
    return [...TZ_FALLBACK];
  }, []);

  const save = useMutation({
    mutationFn: (value: string) => settingsApi.update({ timezone: value }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings"] });
      setSavedNote(
        tz === ""
          ? "Timezone override cleared — host falls back to install-time default."
          : `Timezone set to ${tz}. Applies on the host within one heartbeat cycle.`,
      );
    },
    onError: () => setSavedNote(null),
  });

  const disabled = !isSuperadmin || !applianceMode || save.isPending;
  const trimmed = tz.trim();
  const isValid = trimmed === "" || ianaList.includes(trimmed);
  const dirty = trimmed !== currentValue.trim();

  return (
    <section className="rounded-lg border bg-card p-4">
      <div className="mb-3 flex items-center gap-2">
        <Clock className="h-4 w-4" />
        <h3 className="text-sm font-semibold">Host timezone</h3>
      </div>
      <p className="mb-3 text-xs text-muted-foreground">
        IANA timezone name applied to the appliance host (e.g.{" "}
        <code>America/Toronto</code>, <code>Europe/Berlin</code>,{" "}
        <code>UTC</code>). The installer wizard captures the initial value; this
        control changes it post-install. Leave empty to clear the override and
        fall back to the install-time default.
      </p>
      {!applianceMode && (
        <p className="mb-3 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
          This control only applies on SpatiumDDI OS appliances. Docker and
          Kubernetes deployments inherit timezone from the host / container
          runtime; set it there instead.
        </p>
      )}
      <div className="flex items-center gap-2">
        <input
          list="iana-timezones"
          value={tz}
          onChange={(e) => {
            setTz(e.target.value);
            setSavedNote(null);
          }}
          placeholder="UTC"
          disabled={disabled}
          className="w-72 rounded-md border bg-background px-3 py-1.5 text-sm font-mono disabled:opacity-60"
        />
        <datalist id="iana-timezones">
          {ianaList.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
        <button
          type="button"
          disabled={disabled || !isValid || !dirty}
          onClick={() => save.mutate(trimmed)}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:opacity-90 disabled:opacity-50"
        >
          {save.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          Save
        </button>
      </div>
      {!isValid && (
        <p className="mt-2 text-xs text-rose-700 dark:text-rose-300">
          Not a recognised IANA timezone. Use the autocomplete or pick from{" "}
          <a
            href="https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
            target="_blank"
            rel="noreferrer"
            className="underline"
          >
            the IANA database list
          </a>
          .
        </p>
      )}
      {savedNote && (
        <p className="mt-2 text-xs text-emerald-700 dark:text-emerald-300">
          {savedNote}
        </p>
      )}
      {save.error && !savedNote && (
        <p className="mt-2 text-xs text-rose-700 dark:text-rose-300">
          {formatApiError(save.error)}
        </p>
      )}
    </section>
  );
}
