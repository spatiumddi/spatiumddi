import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, StopCircle } from "lucide-react";
import { type NmapScanRead, type NmapScanStatus, nmapApi } from "@/lib/api";
import { HeaderButton } from "@/components/ui/header-button";
import { cn } from "@/lib/utils";

const TERMINAL: NmapScanStatus[] = ["completed", "failed", "cancelled"];

function StatusPill({ status }: { status: NmapScanStatus }) {
  const styles: Record<NmapScanStatus, string> = {
    queued: "bg-muted text-muted-foreground",
    running: "bg-blue-500/10 text-blue-700 dark:text-blue-400",
    completed: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
    failed: "bg-destructive/10 text-destructive",
    cancelled: "bg-amber-500/10 text-amber-700 dark:text-amber-400",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium",
        styles[status],
      )}
    >
      {status}
    </span>
  );
}

export interface NmapScanLiveViewerProps {
  scanId: string;
  onClose?: () => void;
}

/**
 * Live SSE-driven output viewer plus parsed-summary panel.
 *
 * The viewer opens an EventSource against
 * ``/api/v1/nmap/scans/{id}/stream`` and appends every ``data:``
 * frame to a rolling buffer. When the ``done`` event arrives we
 * refetch the full scan record so the parsed summary (open ports,
 * OS guess, exit code) and final stdout buffer are rendered from
 * the persisted row.
 */
export function NmapScanLiveViewer({
  scanId,
  onClose,
}: NmapScanLiveViewerProps) {
  const qc = useQueryClient();
  const [lines, setLines] = useState<string[]>([]);
  const [streamStatus, setStreamStatus] = useState<
    "connecting" | "open" | "done" | "error"
  >("connecting");
  const preRef = useRef<HTMLPreElement | null>(null);

  // The server side persists everything we need, so once the stream
  // ends we just refetch the record to render the final summary.
  const { data: scan } = useQuery({
    queryKey: ["nmap-scan", scanId],
    queryFn: () => nmapApi.getScan(scanId),
    refetchInterval: (q) => {
      const data = q.state.data as NmapScanRead | undefined;
      if (!data) return 2000;
      return TERMINAL.includes(data.status) ? false : 2000;
    },
  });

  useEffect(() => {
    const url = nmapApi.streamUrl(scanId);
    const es = new EventSource(url);

    es.onopen = () => setStreamStatus("open");
    es.onmessage = (ev) => {
      setLines((prev) => [...prev, ev.data]);
    };
    es.addEventListener("done", () => {
      setStreamStatus("done");
      es.close();
      qc.invalidateQueries({ queryKey: ["nmap-scan", scanId] });
      qc.invalidateQueries({ queryKey: ["nmap-scans"] });
    });
    es.onerror = () => {
      // EventSource auto-reconnects; surface the state once but don't
      // tear it down — server-side close after `done` fires onerror
      // too in some browsers.
      setStreamStatus((s) => (s === "done" ? s : "error"));
    };

    return () => {
      es.close();
    };
  }, [scanId, qc]);

  // Auto-scroll to bottom on new lines.
  useEffect(() => {
    if (preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight;
    }
  }, [lines.length]);

  const status = scan?.status ?? "queued";
  const isTerminal = TERMINAL.includes(status);
  const cancel = async () => {
    try {
      await nmapApi.cancelScan(scanId);
      qc.invalidateQueries({ queryKey: ["nmap-scan", scanId] });
    } catch {
      // Surface via refetch — don't block the UI.
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <StatusPill status={status} />
        {scan?.command_line && (
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">
            {scan.command_line}
          </code>
        )}
        {streamStatus === "connecting" && (
          <span className="inline-flex items-center gap-1 text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" /> connecting…
          </span>
        )}
        <div className="ml-auto flex gap-1.5">
          {!isTerminal && (
            <HeaderButton variant="destructive" onClick={cancel}>
              <StopCircle className="h-3.5 w-3.5" />
              Cancel
            </HeaderButton>
          )}
          {onClose && (
            <HeaderButton variant="secondary" onClick={onClose}>
              Close
            </HeaderButton>
          )}
        </div>
      </div>

      <pre
        ref={preRef}
        className="max-h-[400px] overflow-auto rounded-md border bg-zinc-950 p-3 font-mono text-[11px] leading-relaxed text-zinc-100"
      >
        {lines.length === 0
          ? "Waiting for first output line…"
          : lines.join("\n")}
      </pre>

      {scan?.error_message && (
        <p className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">
          {scan.error_message}
        </p>
      )}

      {isTerminal && scan?.summary && <SummaryPanel scan={scan} />}
    </div>
  );
}

function SummaryPanel({ scan }: { scan: NmapScanRead }) {
  const summary = scan.summary;
  if (!summary) return null;
  const openPorts = summary.ports.filter((p) => p.state === "open");
  return (
    <div className="space-y-2 rounded-md border bg-muted/20 p-3">
      <div className="flex flex-wrap gap-3 text-xs">
        <span>
          Host state: <strong>{summary.host_state}</strong>
        </span>
        {scan.exit_code !== null && (
          <span>
            Exit: <strong>{scan.exit_code}</strong>
          </span>
        )}
        {scan.duration_seconds !== null && (
          <span>Duration: {scan.duration_seconds.toFixed(1)}s</span>
        )}
        {summary.os?.name && (
          <span>
            OS guess: <strong>{summary.os.name}</strong>
            {summary.os.accuracy ? ` (${summary.os.accuracy}%)` : ""}
          </span>
        )}
      </div>
      {openPorts.length > 0 ? (
        <div className="overflow-x-auto rounded-md border bg-background">
          <table className="w-full min-w-[480px] text-xs">
            <thead>
              <tr className="border-b bg-muted/30">
                <th className="px-2 py-1.5 text-left">Port</th>
                <th className="px-2 py-1.5 text-left">Proto</th>
                <th className="px-2 py-1.5 text-left">State</th>
                <th className="px-2 py-1.5 text-left">Service</th>
                <th className="px-2 py-1.5 text-left">Version</th>
              </tr>
            </thead>
            <tbody>
              {openPorts.map((p) => (
                <tr
                  key={`${p.proto}-${p.port}`}
                  className="border-b last:border-0"
                >
                  <td className="px-2 py-1 font-mono">{p.port}</td>
                  <td className="px-2 py-1">{p.proto}</td>
                  <td className="px-2 py-1">{p.state}</td>
                  <td className="px-2 py-1">
                    {p.service ?? (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="px-2 py-1 text-muted-foreground">
                    {[p.product, p.version, p.extrainfo]
                      .filter(Boolean)
                      .join(" ") || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">No open ports detected.</p>
      )}
    </div>
  );
}
