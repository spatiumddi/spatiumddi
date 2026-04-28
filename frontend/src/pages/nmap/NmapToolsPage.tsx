import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Radar, Trash2 } from "lucide-react";
import { type NmapScanRead, nmapApi } from "@/lib/api";
import { NmapScanForm } from "./NmapScanForm";
import { NmapScanLiveViewer } from "./NmapScanLiveViewer";
import { ConfirmDeleteScanModal } from "./ConfirmDeleteScanModal";
import { humanTime } from "@/pages/network/_shared";

/**
 * Standalone Nmap scanner page at ``/tools/nmap``.
 *
 * Lets operators kick off a scan against any IP without going via
 * the IPAM detail modal — useful for ad-hoc reachability checks or
 * scanning addresses that aren't in IPAM yet.
 *
 * Layout: form on the left, live viewer + recent-scan history on
 * the right (stacked on small screens).
 */
export function NmapToolsPage() {
  const [activeScan, setActiveScan] = useState<NmapScanRead | null>(null);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="border-b bg-card px-6 py-4">
        <div className="flex items-center gap-2">
          <Radar className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">Nmap scanner</h1>
        </div>
        <p className="mt-1 max-w-3xl text-xs text-muted-foreground">
          Run nmap against any IPv4 / IPv6 host from the SpatiumDDI host
          perspective. Output streams live — scans run as the API container's
          non-root user, so privileged scan modes (raw SYN, OS detection without
          privilege) fall back to TCP-connect.
        </p>
      </div>

      <div className="flex-1 overflow-auto p-6">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="rounded-lg border bg-card p-4">
            <h2 className="mb-3 text-sm font-medium">New scan</h2>
            <NmapScanForm onScanStarted={setActiveScan} />
          </div>

          <div className="rounded-lg border bg-card p-4">
            <h2 className="mb-3 text-sm font-medium">
              {activeScan
                ? `Live output — ${activeScan.target_ip}`
                : "Recent scans"}
            </h2>
            {activeScan ? (
              <NmapScanLiveViewer
                scanId={activeScan.id}
                onClose={() => setActiveScan(null)}
              />
            ) : (
              <RecentScansList onSelect={setActiveScan} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function RecentScansList({
  onSelect,
}: {
  onSelect: (s: NmapScanRead) => void;
}) {
  const qc = useQueryClient();
  const [pendingDelete, setPendingDelete] = useState<NmapScanRead | null>(null);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["nmap-scans", "recent"],
    queryFn: () => nmapApi.listScans({ page_size: 25 }),
    refetchInterval: 5000,
  });
  const deleteMut = useMutation({
    mutationFn: (id: string) => nmapApi.cancelScan(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["nmap-scans"] });
      setPendingDelete(null);
    },
  });

  if (isLoading) {
    return (
      <p className="inline-flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" /> Loading recent scans…
      </p>
    );
  }
  if (isError) {
    return (
      <p className="text-xs text-destructive">Failed to load recent scans.</p>
    );
  }
  const items = data?.items ?? [];
  if (items.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        No scans yet — kick one off on the left.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full min-w-[460px] text-xs">
        <thead>
          <tr className="border-b bg-muted/30">
            <th className="px-2 py-1.5 text-left">When</th>
            <th className="px-2 py-1.5 text-left">Target</th>
            <th className="px-2 py-1.5 text-left">Preset</th>
            <th className="px-2 py-1.5 text-left">Status</th>
            <th className="px-2 py-1.5 text-left">Open</th>
            <th className="px-2 py-1.5"></th>
          </tr>
        </thead>
        <tbody>
          {items.map((s) => {
            const open =
              s.summary?.ports.filter((p) => p.state === "open").length ?? 0;
            return (
              <tr
                key={s.id}
                onClick={() => onSelect(s)}
                className="cursor-pointer border-b last:border-0 hover:bg-muted/20"
              >
                <td className="px-2 py-1 text-muted-foreground">
                  {humanTime(s.created_at)}
                </td>
                <td className="px-2 py-1 font-mono">{s.target_ip}</td>
                <td className="px-2 py-1">{s.preset}</td>
                <td className="px-2 py-1">{s.status}</td>
                <td className="px-2 py-1 tabular-nums">{open}</td>
                <td
                  className="px-2 py-1 text-right"
                  onClick={(e) => e.stopPropagation()}
                >
                  <button
                    type="button"
                    title={
                      s.status === "queued" || s.status === "running"
                        ? "Cancel scan"
                        : "Delete scan"
                    }
                    disabled={deleteMut.isPending}
                    onClick={() => setPendingDelete(s)}
                    className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {pendingDelete && (
        <ConfirmDeleteScanModal
          scan={pendingDelete}
          pending={deleteMut.isPending}
          onConfirm={() => deleteMut.mutate(pendingDelete.id)}
          onClose={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}
