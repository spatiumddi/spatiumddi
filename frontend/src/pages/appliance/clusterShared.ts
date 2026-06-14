import { useEffect, useRef, useState } from "react";

import {
  applianceClusterApi,
  streamClusterHealth,
  type ClusterHealthSnapshot,
} from "@/lib/api";

/**
 * Shared, non-component cluster-health primitives (#416).
 *
 * Pulled out of ``ClusterOverview.tsx`` so the constants / formatting
 * helpers / the live SSE hook can be reused by the Console view without
 * tripping eslint's ``react-refresh/only-export-components`` (a .tsx file
 * may export multiple components, but mixing component + non-component
 * exports breaks Vite fast-refresh). Keep this file JSX-free.
 */

const MAX_POINTS = 90; // ~3 min of history at the 2s stream cadence

// Shared palette — the Console view (#416) reuses these so its chips /
// charts match the Overview exactly without re-declaring the hex.
export const EMERALD = "#10b981";
export const SKY = "#0ea5e9";
export const AMBER = "#f59e0b";
export const ROSE = "#f43f5e";
export const VIOLET = "#8b5cf6";
export const SLATE = "#64748b";

export interface HistPoint {
  i: number;
  cpu: number | null;
  mem: number | null;
}

// ── formatting helpers ──────────────────────────────────────────────────────

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  const u = ["KiB", "MiB", "GiB", "TiB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${u[i]}`;
}

export function fmtCores(c: number | null | undefined): string {
  if (c == null) return "—";
  return c < 1 ? `${Math.round(c * 1000)}m` : c.toFixed(2);
}

export function pct(
  used: number | null | undefined,
  cap: number | null | undefined,
): number | null {
  if (used == null || !cap) return null;
  return Math.max(0, Math.min(100, (used / cap) * 100));
}

export function fmtAge(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

export function usageColor(p: number | null): string {
  if (p == null) return SLATE;
  if (p < 60) return EMERALD;
  if (p < 85) return AMBER;
  return ROSE;
}

// ── live stream hook ─────────────────────────────────────────────────────────

export function useClusterHealthStream() {
  const [snapshot, setSnapshot] = useState<ClusterHealthSnapshot | null>(null);
  const [history, setHistory] = useState<HistPoint[]>([]);
  const [connected, setConnected] = useState(false);
  const seq = useRef(0);

  useEffect(() => {
    const ctrl = new AbortController();
    let stopped = false;

    const ingest = (snap: ClusterHealthSnapshot) => {
      setSnapshot(snap);
      if (snap.available) {
        const cpu = pct(snap.cpu_usage_cores, snap.cpu_capacity_cores);
        const mem = pct(
          snap.memory_working_set_bytes,
          snap.memory_capacity_bytes,
        );
        setHistory((prev) => {
          const next = [...prev, { i: seq.current++, cpu, mem }];
          return next.length > MAX_POINTS
            ? next.slice(next.length - MAX_POINTS)
            : next;
        });
      }
    };

    // Instant first paint while the SSE stream warms up.
    applianceClusterApi
      .health()
      .then((s) => {
        if (!stopped) ingest(s);
      })
      .catch(() => {
        /* stream will deliver shortly */
      });

    const run = async () => {
      while (!stopped) {
        try {
          for await (const snap of streamClusterHealth(ctrl.signal)) {
            if (stopped) break;
            setConnected(true);
            ingest(snap);
          }
        } catch {
          if (stopped) break;
        }
        setConnected(false);
        if (stopped) break;
        await new Promise((r) => setTimeout(r, 2000)); // backoff then reconnect
      }
    };
    void run();

    return () => {
      stopped = true;
      ctrl.abort();
    };
  }, []);

  return { snapshot, history, connected };
}
