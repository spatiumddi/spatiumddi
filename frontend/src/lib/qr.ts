/**
 * QR matrix → SVG path (issue #906).
 *
 * Split out of the component so it can be tested. The encoding itself comes
 * from `qrcode-generator` (the Kazuhiko Arase implementation most other QR
 * libraries derive from — MIT, zero dependencies) and is not the part worth
 * doubting; what needed pinning is everything below it. A transposed
 * row/column or an inverted dark/light polarity produces a code that looks
 * entirely plausible on screen and scans as nothing at all, and there is no
 * way to notice by eye.
 */

import qrcode from "qrcode-generator";

export type QrLevel = "L" | "M" | "Q" | "H";

/**
 * Quiet zone in modules. The spec requires 4 and scanners genuinely need it —
 * without a light margin they cannot locate the finder patterns' outer edges.
 */
export const QUIET_ZONE = 4;

export interface QrMatrix {
  /** SVG path data covering every dark module, in module units. */
  d: string;
  /** Edge length of the viewBox in module units, quiet zone included. */
  extent: number;
  /** Module count of the code itself, excluding the quiet zone. */
  count: number;
  /** `true` when the module at (row, col) is dark. Excludes the quiet zone. */
  isDark: (row: number, col: number) => boolean;
}

export function buildQrMatrix(value: string, level: QrLevel = "M"): QrMatrix {
  // typeNumber 0 = "smallest version that fits". An enrolment URI carrying a
  // token and a fingerprint runs ~180 characters, landing around version 9;
  // letting the library pick keeps the module count — and so the scanning
  // difficulty — at the minimum the payload allows.
  const qr = qrcode(0, level);
  qr.addData(value);
  qr.make();

  const count = qr.getModuleCount();
  // One path for every dark module rather than one <rect> each: a version-9
  // code is 53x53, so this is one DOM node instead of several hundred.
  const parts: string[] = [];
  for (let row = 0; row < count; row++) {
    for (let col = 0; col < count; col++) {
      if (qr.isDark(row, col)) {
        // x = col, y = row. Getting this pair the wrong way round mirrors the
        // code along its diagonal, which still renders as a tidy square.
        parts.push(`M${col + QUIET_ZONE} ${row + QUIET_ZONE}h1v1h-1z`);
      }
    }
  }

  return {
    d: parts.join(""),
    extent: count + QUIET_ZONE * 2,
    count,
    isDark: (row, col) => qr.isDark(row, col),
  };
}
