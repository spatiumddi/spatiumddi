import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, StopCircle } from "lucide-react";
import { type NmapScanRead, type NmapScanStatus, nmapApi } from "@/lib/api";
import { HeaderButton } from "@/components/ui/header-button";
import { cn } from "@/lib/utils";
import { NmapResultPanel } from "./NmapResultPanel";

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
  /** Fires once when the scan reaches a terminal state. The parent
   *  uses this to switch tabs and stash the finished scan as the
   *  "Last result" view. Receives the most recent ``NmapScanRead``
   *  available; if a refetch is in flight, the parent can still call
   *  the API again later if needed. */
  onComplete?: (scan: NmapScanRead) => void;
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
  onComplete,
}: NmapScanLiveViewerProps) {
  const qc = useQueryClient();
  const [lines, setLines] = useState<string[]>([]);
  const [streamStatus, setStreamStatus] = useState<
    "connecting" | "open" | "done" | "error"
  >("connecting");
  const preRef = useRef<HTMLPreElement | null>(null);
  // Guard against firing onComplete twice — the SSE done event AND
  // the polling refetch can both observe the terminal status.
  const completedFiredRef = useRef<string | null>(null);

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
    // Starting a fresh scan reuses this component (parent just swaps
    // the ``scanId`` prop). Clear the buffer + reconnect state so we
    // don't see stale lines from the previous scan flicker through
    // before the first new ``data:`` frame arrives.
    setLines([]);
    setStreamStatus("connecting");

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

  useEffect(() => {
    if (
      isTerminal &&
      scan &&
      onComplete &&
      completedFiredRef.current !== scanId
    ) {
      completedFiredRef.current = scanId;
      onComplete(scan);
    }
  }, [isTerminal, scan, onComplete, scanId]);

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

      {isTerminal && scan?.summary && <NmapResultPanel scan={scan} />}
    </div>
  );
}
