import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Box,
  Container as ContainerIcon,
  HardDrive,
  Network,
  ScrollText,
  ShieldCheck,
  Wrench,
} from "lucide-react";

import { applianceApi } from "@/lib/api";
import { useSessionState } from "@/lib/useSessionState";
import { CertificatesTab } from "./CertificatesTab";
import { ContainersTab } from "./ContainersTab";
import { LogsTab } from "./LogsTab";
import { MaintenanceTab } from "./MaintenanceTab";
import { NetworkTab } from "./NetworkTab";
import { ReleasesTab } from "./ReleasesTab";
import { SlotUpgradeCard } from "./SlotUpgradeCard";

/**
 * SpatiumDDI OS appliance management hub (issue #134, Phase 4).
 *
 * Phase 4a (this commit) lands just the frame:
 *   - sidebar visibility gate via versionInfo.appliance_mode
 *   - permission-gated /api/v1/appliance/info call
 *   - tab shell with six placeholders for the sub-surfaces 4b-4g
 *     will fill in
 *
 * Each tab is a hard-coded "Coming in Phase 4x" panel right now so
 * the route is reachable, navigable, and obviously incomplete. The
 * goal of 4a is to ship the scaffolding everything else lands behind
 * — TLS first (4b, prevents secrets-over-plain-HTTP regressions),
 * then releases / containers / logs / network / wizard in any order.
 */
type Tab =
  | "tls"
  | "releases"
  | "os-image"
  | "containers"
  | "logs"
  | "network"
  | "maintenance";

interface TabSpec {
  key: Tab;
  label: string;
  phase: string;
  icon: typeof ShieldCheck;
  summary: string;
}

const TABS: TabSpec[] = [
  {
    key: "tls",
    label: "Web UI Certificate",
    phase: "4b",
    icon: ShieldCheck,
    summary:
      "Upload an existing certificate + key, generate a CSR, or have the appliance issue a Let's Encrypt cert against a public DNS name. Reuses the existing ACME service (app/services/acme/).",
  },
  {
    key: "releases",
    label: "Releases",
    phase: "4c",
    icon: Box,
    summary:
      "GitHub Releases list with one-click pull-and-recycle, a rollback target picker, and the release notes inline so operators see what they're applying before they apply it.",
  },
  {
    key: "os-image",
    label: "OS Image",
    phase: "8b-3",
    icon: HardDrive,
    summary:
      "Atomic A/B OS image upgrade (Phase 8). Writes a slot .raw.xz into the inactive partition, arms grub one-shot, and rolls back automatically if /health/live doesn't come up on the new slot. Distinct from container-stack releases above — this upgrades the host OS + kernel + bundled tooling, not just the SpatiumDDI containers.",
  },
  {
    key: "containers",
    label: "Containers",
    phase: "4d",
    icon: ContainerIcon,
    summary:
      "Container list driven off the docker socket, with start / stop / restart and live log streaming over websocket. Drives the spatium stack — the appliance compose mounts /var/run/docker.sock read-write for this surface.",
  },
  {
    key: "logs",
    label: "Logs & Diagnostics",
    phase: "4e",
    icon: ScrollText,
    summary:
      'System log viewer wired to journalctl, the "Run self-test" health-check button (DNS resolution + DHCP issuance + web reachability), and the "Download diagnostic bundle" one-click zip with secrets redacted.',
  },
  {
    key: "network",
    label: "Network & Host",
    phase: "4f",
    icon: Network,
    summary:
      "Hostname, NTP, DNS resolvers, IPv4/IPv6 mode (DHCP vs static, with the wizard's same form), nftables drop-in editor for /etc/nftables.d/, SSH key upload, proxy config, and a reboot-pending banner.",
  },
  {
    key: "maintenance",
    label: "Maintenance",
    phase: "4f",
    icon: Activity,
    summary:
      "Maintenance-mode toggle that drains DNS/DHCP traffic before letting the operator perform host work, plus reboot / shutdown buttons with confirmation prompts so accidental clicks don't take an appliance offline.",
  },
];

export function AppliancePage() {
  const [tab, setTab] = useSessionState<Tab>("appliance.tab", "tls");

  const { data: info } = useQuery({
    queryKey: ["appliance", "info"],
    queryFn: applianceApi.getInfo,
    staleTime: 5 * 60 * 1000,
  });

  const active = TABS.find((t) => t.key === tab) ?? TABS[0];

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="border-b bg-card px-6 py-4">
        <div className="flex items-center gap-2">
          <Wrench className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">Appliance management</h1>
          {info?.appliance_version && (
            <span className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
              v{info.appliance_version}
            </span>
          )}
          {info?.appliance_hostname && (
            <span className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
              {info.appliance_hostname}
            </span>
          )}
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          Manage the SpatiumDDI OS appliance — TLS, releases, containers, logs,
          host network, and lifecycle. This surface is appliance-only; on plain
          Docker / Kubernetes deployments the sidebar entry is hidden.
        </p>
        <div className="-mb-px mt-3 flex flex-wrap gap-1 border-b">
          {TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button
                key={t.key}
                type="button"
                onClick={() => setTab(t.key)}
                className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-1.5 text-sm ${
                  tab === t.key
                    ? "border-primary text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground"
                }`}
              >
                <Icon className="h-3.5 w-3.5" />
                {t.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex-1 overflow-auto bg-background p-6">
        {tab === "tls" ? (
          <CertificatesTab />
        ) : tab === "releases" ? (
          <ReleasesTab />
        ) : tab === "os-image" ? (
          <SlotUpgradeCard />
        ) : tab === "containers" ? (
          <ContainersTab />
        ) : tab === "logs" ? (
          <LogsTab />
        ) : tab === "network" ? (
          <NetworkTab />
        ) : tab === "maintenance" ? (
          <MaintenanceTab />
        ) : (
          <PhasePlaceholder spec={active} />
        )}
      </div>
    </div>
  );
}

function PhasePlaceholder({ spec }: { spec: TabSpec }) {
  const Icon = spec.icon;
  return (
    <div className="mx-auto max-w-2xl rounded-lg border bg-card p-6 shadow-sm">
      <div className="flex items-start gap-3">
        <div className="rounded-md bg-muted p-2 text-muted-foreground">
          <Icon className="h-5 w-5" />
        </div>
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold">{spec.label}</h2>
            <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-600 dark:text-amber-400">
              Phase {spec.phase}
            </span>
          </div>
          <p className="mt-2 text-sm text-muted-foreground">{spec.summary}</p>
          <p className="mt-4 text-xs text-muted-foreground">
            This surface lands in a follow-up commit. The Phase 4a frame ships
            the gate, permission family, and tab shell so each sub-surface can
            slot in without re-litigating the navigation.
          </p>
        </div>
      </div>
    </div>
  );
}
