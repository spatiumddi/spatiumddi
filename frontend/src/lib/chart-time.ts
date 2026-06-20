// Shared chart X-axis tick formatting for the per-server metric timeseries
// modals (DNS + DHCP Stats tabs). Kept in one place so the date-vs-time label
// rule can't drift between the two surfaces.

export function pad(n: number): string {
  return n.toString().padStart(2, "0");
}

/**
 * Format a bucket timestamp for a chart X-axis tick (in the browser's local
 * timezone — operators read their own clock).
 *
 * `withDate` prefixes the month/day. Pass it `true` for any window that spans
 * more than a single day, where a bare `HH:MM` repeats across days and can't
 * be told apart (e.g. a 7-day chart of 30-minute buckets). Pass `false` for
 * intra-day windows where the time alone is unambiguous.
 */
export function formatBucket(iso: string, withDate: boolean): string {
  const d = new Date(iso);
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return withDate ? `${d.getMonth() + 1}/${d.getDate()} ${hm}` : hm;
}
