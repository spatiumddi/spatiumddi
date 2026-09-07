/** #980 — reading the DHCP packet-loss counters without lying about them.
 *
 * Two counters ride on every metric bucket and they do not mean the same
 * thing:
 *
 *  * `socket_drop` — the kernel discarded the datagram because Kea's receive
 *    buffer was full, so Kea never saw it. Unambiguous loss, usually a node
 *    short of CPU.
 *  * `receive_drop` — Kea read the packet and discarded it, which INCLUDES
 *    doing so on purpose: a blocklisted MAC (the shipped DHCP MAC blocklist
 *    renders a Kea `DROP` class) or an HA standby declining an out-of-scope
 *    query both land here.
 *
 * So everything user-facing reads `socket_drop` alone. Extracted from the
 * server-detail modal because the null handling below is the load-bearing
 * property of the whole feature and is invisible to `tsc`.
 */

/** One bucket, as the API sends it — deliberately looser than the generated
 *  type. `undefined` models a control plane older than #980, which omits the
 *  key entirely; the point of this module is that such a bucket must not read
 *  as "measured, no loss". */
export interface LossBucket {
  socket_drop?: number | null;
}

/**
 * Kernel-side loss for one bucket, or `null` when it was not measured.
 *
 * `?? null` normalises at the boundary: the generated type says
 * `number | null`, but an omitted key arrives as `undefined` and every
 * downstream check is `!== null`. Without this a version-skewed API reads as
 * measured-and-zero — the one thing this feature exists not to say.
 */
export function bucketLoss(b: LossBucket): number | null {
  return b.socket_drop ?? null;
}

export interface LossSummary {
  /** False when NOTHING in the window measured loss — an agent older than
   *  #980, or one that cannot read /proc/net/udp. Distinct from measuring
   *  zero, and must render as "not measured" rather than as a clean bill. */
  measured: boolean;
  total: number;
}

/**
 * Roll a window of buckets into the three states the UI distinguishes:
 * measured and lossy, measured and clean, never measured.
 *
 * A window that mixes measured and unmeasured buckets counts as measured and
 * sums the ones that were: partial knowledge is still knowledge, and the
 * alternative would hide real loss behind one agent restart.
 */
export function summariseLoss(buckets: LossBucket[]): LossSummary {
  const measured = buckets
    .map(bucketLoss)
    .filter((v): v is number => v !== null);
  if (measured.length === 0) return { measured: false, total: 0 };
  return { measured: true, total: measured.reduce((a, v) => a + v, 0) };
}
