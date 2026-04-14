import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { settingsApi, authApi, type PlatformSettings } from "@/lib/api";
import { Save } from "lucide-react";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border bg-card">
      <div className="border-b px-5 py-3">
        <h2 className="text-sm font-semibold">{title}</h2>
      </div>
      <div className="divide-y px-5">{children}</div>
    </div>
  );
}

function Field({
  label,
  description,
  children,
}: {
  label: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-8 py-3">
      <div>
        <div className="text-sm font-medium">{label}</div>
        {description && <div className="text-xs text-muted-foreground">{description}</div>}
      </div>
      <div className="flex-shrink-0">{children}</div>
    </div>
  );
}

export function SettingsPage() {
  const qc = useQueryClient();
  const { data: me } = useQuery({ queryKey: ["me"], queryFn: authApi.me, staleTime: 60_000 });
  const isSuperadmin = me?.is_superadmin ?? false;

  const { data, isLoading } = useQuery({
    queryKey: ["settings"],
    queryFn: settingsApi.get,
  });

  const [form, setForm] = useState<Partial<PlatformSettings>>({});
  const [saved, setSaved] = useState(false);

  // Merge loaded data with local edits
  const values: PlatformSettings = { ...(data ?? ({} as PlatformSettings)), ...form };

  const mutation = useMutation({
    mutationFn: (patch: Partial<PlatformSettings>) => settingsApi.update(patch),
    onSuccess: (updated) => {
      qc.setQueryData(["settings"], updated);
      setForm({});
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    },
  });

  function set<K extends keyof PlatformSettings>(key: K, value: PlatformSettings[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
  }

  function handleSave() {
    if (Object.keys(form).length > 0) {
      mutation.mutate(form);
    }
  }

  if (isLoading) {
    return <div className="p-8 text-sm text-muted-foreground">Loading settings…</div>;
  }

  const dirty = Object.keys(form).length > 0;

  return (
    <div className="h-full overflow-auto">
    <div className="mx-auto max-w-2xl space-y-6 p-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Platform Settings</h1>
          <p className="text-sm text-muted-foreground">
            {isSuperadmin ? "Configure SpatiumDDI platform behavior." : "View-only — superadmin access required to change settings."}
          </p>
        </div>
        {isSuperadmin && (
          <button
            onClick={handleSave}
            disabled={!dirty || mutation.isPending}
            className="flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
          >
            <Save className="h-4 w-4" />
            {saved ? "Saved!" : mutation.isPending ? "Saving…" : "Save"}
          </button>
        )}
      </div>

      {mutation.isError && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-2 text-sm text-destructive">
          Failed to save settings. Please try again.
        </div>
      )}

      <Section title="Branding">
        <Field label="Application Title" description="Shown in the browser tab and header.">
          <input
            value={values.app_title ?? ""}
            onChange={(e) => set("app_title", e.target.value)}
            disabled={!isSuperadmin}
            className="rounded-md border bg-background px-3 py-1.5 text-sm w-48 disabled:opacity-60"
          />
        </Field>
      </Section>

      <Section title="IP Allocation">
        <Field label="Allocation Strategy" description="Strategy used when auto-allocating the next IP.">
          <select
            value={values.ip_allocation_strategy ?? "sequential"}
            onChange={(e) => set("ip_allocation_strategy", e.target.value)}
            disabled={!isSuperadmin}
            className="rounded-md border bg-background px-3 py-1.5 text-sm disabled:opacity-60"
          >
            <option value="sequential">Sequential</option>
            <option value="random">Random</option>
          </select>
        </Field>
      </Section>

      <Section title="Session & Security">
        <Field label="Session Timeout" description="Minutes of inactivity before session expires (0 = disabled).">
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={0}
              value={values.session_timeout_minutes ?? 60}
              onChange={(e) => set("session_timeout_minutes", Number(e.target.value))}
              disabled={!isSuperadmin}
              className="rounded-md border bg-background px-3 py-1.5 text-sm w-24 disabled:opacity-60"
            />
            <span className="text-xs text-muted-foreground">min</span>
          </div>
        </Field>
        <Field label="Auto-Logout Warning" description="Minutes before expiry to show a logout warning (0 = disabled).">
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={0}
              value={values.auto_logout_minutes ?? 0}
              onChange={(e) => set("auto_logout_minutes", Number(e.target.value))}
              disabled={!isSuperadmin}
              className="rounded-md border bg-background px-3 py-1.5 text-sm w-24 disabled:opacity-60"
            />
            <span className="text-xs text-muted-foreground">min</span>
          </div>
        </Field>
      </Section>

      <Section title="Utilization Thresholds">
        <Field label="Warning Threshold" description="Utilization percentage to show amber warning.">
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={0}
              max={100}
              value={values.utilization_warn_threshold ?? 80}
              onChange={(e) => set("utilization_warn_threshold", Number(e.target.value))}
              disabled={!isSuperadmin}
              className="rounded-md border bg-background px-3 py-1.5 text-sm w-20 disabled:opacity-60"
            />
            <span className="text-xs text-muted-foreground">%</span>
          </div>
        </Field>
        <Field label="Critical Threshold" description="Utilization percentage to show red critical indicator.">
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={0}
              max={100}
              value={values.utilization_critical_threshold ?? 95}
              onChange={(e) => set("utilization_critical_threshold", Number(e.target.value))}
              disabled={!isSuperadmin}
              className="rounded-md border bg-background px-3 py-1.5 text-sm w-20 disabled:opacity-60"
            />
            <span className="text-xs text-muted-foreground">%</span>
          </div>
        </Field>
      </Section>

      <Section title="Subnet Tree UI">
        <Field label="Default Expanded Depth" description="How many levels of the tree are expanded by default.">
          <input
            type="number"
            min={0}
            max={10}
            value={values.subnet_tree_default_expanded_depth ?? 2}
            onChange={(e) => set("subnet_tree_default_expanded_depth", Number(e.target.value))}
            disabled={!isSuperadmin}
            className="rounded-md border bg-background px-3 py-1.5 text-sm w-20 disabled:opacity-60"
          />
        </Field>
      </Section>

      <Section title="Discovery">
        <Field label="Enable Discovery Scans" description="Periodically ping subnets to detect active hosts.">
          <button
            role="switch"
            aria-checked={values.discovery_scan_enabled}
            onClick={() => isSuperadmin && set("discovery_scan_enabled", !values.discovery_scan_enabled)}
            disabled={!isSuperadmin}
            className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none disabled:opacity-60 ${
              values.discovery_scan_enabled ? "bg-primary" : "bg-muted-foreground/30"
            }`}
          >
            <span
              className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                values.discovery_scan_enabled ? "translate-x-4" : "translate-x-0"
              }`}
            />
          </button>
        </Field>
        <Field label="Scan Interval" description="How often to run discovery scans.">
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={1}
              value={values.discovery_scan_interval_minutes ?? 60}
              onChange={(e) => set("discovery_scan_interval_minutes", Number(e.target.value))}
              disabled={!isSuperadmin || !values.discovery_scan_enabled}
              className="rounded-md border bg-background px-3 py-1.5 text-sm w-24 disabled:opacity-60"
            />
            <span className="text-xs text-muted-foreground">min</span>
          </div>
        </Field>
      </Section>

      <Section title="Updates">
        <Field label="Check for GitHub Releases" description="Periodically check GitHub for new SpatiumDDI releases.">
          <button
            role="switch"
            aria-checked={values.github_release_check_enabled}
            onClick={() => isSuperadmin && set("github_release_check_enabled", !values.github_release_check_enabled)}
            disabled={!isSuperadmin}
            className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none disabled:opacity-60 ${
              values.github_release_check_enabled ? "bg-primary" : "bg-muted-foreground/30"
            }`}
          >
            <span
              className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                values.github_release_check_enabled ? "translate-x-4" : "translate-x-0"
              }`}
            />
          </button>
        </Field>
      </Section>
    </div>
    </div>
  );
}
