/**
 * The QR render is verified by DECODING it, not by eyeballing it.
 *
 * The encoding comes from `qrcode-generator` and is not the part worth
 * doubting. What needed pinning is everything this repo wrote on top: a
 * transposed row/column, an inverted polarity, or a missing quiet zone all
 * produce a code that looks entirely plausible on screen and scans as
 * nothing at all. There is no way to catch that by looking.
 *
 * `jsqr` is an independent decoder (dev-only — it is never bundled), so a
 * round-trip through it proves the rendered modules are a real QR code of
 * the intended payload. Cross-checked once during development against
 * ZXing, which agreed.
 */

import jsQR from "jsqr";
import { describe, expect, it } from "vitest";

import { buildEnrolmentUri } from "./enrolment";
import { QUIET_ZONE, buildQrMatrix } from "./qr";

/**
 * Rasterise a matrix exactly as `QrCode` renders it — quiet zone included,
 * dark modules black on a white ground — into the RGBA buffer jsQR wants.
 */
function rasterise(value: string, scale = 4) {
  const m = buildQrMatrix(value);
  const side = m.extent * scale;
  const data = new Uint8ClampedArray(side * side * 4).fill(255);
  for (let row = 0; row < m.count; row++) {
    for (let col = 0; col < m.count; col++) {
      if (!m.isDark(row, col)) continue;
      for (let dy = 0; dy < scale; dy++) {
        for (let dx = 0; dx < scale; dx++) {
          const x = (col + QUIET_ZONE) * scale + dx;
          const y = (row + QUIET_ZONE) * scale + dy;
          const i = (y * side + x) * 4;
          data[i] = data[i + 1] = data[i + 2] = 0;
        }
      }
    }
  }
  return { data, side, matrix: m };
}

function decode(value: string): string | null {
  const { data, side } = rasterise(value);
  return jsQR(data, side, side)?.data ?? null;
}

describe("buildQrMatrix round-trips through a real decoder", () => {
  it("decodes a full enrolment URI with token and fingerprint", () => {
    const uri = buildEnrolmentUri({
      host: "ddi.internal.example",
      port: 8443,
      scheme: "https",
      token: "sddi_R7xK2mQ4vB8nL1pS5tW9zC3jH6dF0gA",
      fingerprint: "a1b2c3d4e5f6".repeat(5) + "abcd",
    });
    expect(decode(uri)).toBe(uri);
  });

  it("decodes a bare token", () => {
    const token = "sddi_R7xK2mQ4vB8nL1pS5tW9zC3jH6dF0gA";
    expect(decode(token)).toBe(token);
  });

  it("decodes an IPv6 host, which brackets and percent-encodes", () => {
    const uri = buildEnrolmentUri({
      host: "2001:db8::1",
      port: 8443,
      scheme: "https",
      token: "sddi_abc",
    });
    expect(decode(uri)).toBe(uri);
  });

  it("decodes a non-ASCII payload as UTF-8, not truncated to latin1", () => {
    // qrcode-generator's DEFAULT byte encoder is ``charCodeAt(i) & 0xff``,
    // which mangles anything outside latin1 and reports no error — the exact
    // silent corruption this file exists to catch. `qr.ts` installs a
    // TextEncoder-backed one; without it this decodes to mojibake.
    const value = "münchen.example — ✓";
    expect(decode(value)).toBe(value);
  });

  it("decodes the longest payload this feature can produce", () => {
    // A worst case: long hostname, non-default port, explicit http, a token
    // and a fingerprint. If the version bump this forces were mishandled the
    // code would still render — as an unscannable square.
    const uri = buildEnrolmentUri({
      host: `${"a".repeat(50)}.internal.example.com`,
      port: 65535,
      scheme: "http",
      token: `sddi_${"Z9".repeat(32)}`,
      fingerprint: "f".repeat(64),
    });
    expect(uri.length).toBeGreaterThan(180);
    expect(decode(uri)).toBe(uri);
  });
});

describe("matrix geometry", () => {
  it("puts a 4-module quiet zone inside the viewBox extent", () => {
    // Without a light margin scanners cannot find the finder patterns'
    // outer edges, and the code silently stops working at a distance.
    const m = buildQrMatrix("hello");
    expect(QUIET_ZONE).toBe(4);
    expect(m.extent).toBe(m.count + QUIET_ZONE * 2);
  });

  it("renders the three finder patterns dark-side-out", () => {
    // Mask-invariant, so this holds for any payload — and it fails loudly if
    // the polarity is ever inverted.
    const m = buildQrMatrix("hello");
    const RING = [
      [1, 1, 1, 1, 1, 1, 1],
      [1, 0, 0, 0, 0, 0, 1],
      [1, 0, 1, 1, 1, 0, 1],
      [1, 0, 1, 1, 1, 0, 1],
      [1, 0, 1, 1, 1, 0, 1],
      [1, 0, 0, 0, 0, 0, 1],
      [1, 1, 1, 1, 1, 1, 1],
    ];
    const finderAt = (top: number, left: number) =>
      RING.every((row, r) =>
        row.every((want, c) => m.isDark(top + r, left + c) === (want === 1)),
      );
    expect(finderAt(0, 0)).toBe(true);
    expect(finderAt(0, m.count - 7)).toBe(true);
    expect(finderAt(m.count - 7, 0)).toBe(true);
  });

  it("emits one path segment per dark module, at (x=col, y=row)", () => {
    // Swapping the pair mirrors the code along its diagonal — which still
    // renders as a tidy square and scans as nothing.
    const m = buildQrMatrix("hello");
    let dark = 0;
    for (let r = 0; r < m.count; r++) {
      for (let c = 0; c < m.count; c++) if (m.isDark(r, c)) dark++;
    }
    expect(m.d.split("M").length - 1).toBe(dark);
    // The top-left finder guarantees a dark module at (0,0), which must land
    // at the quiet-zone offset in BOTH axes.
    expect(m.d.startsWith(`M${QUIET_ZONE} ${QUIET_ZONE}h1v1h-1z`)).toBe(true);
  });

  it("grows the version to fit rather than truncating the payload", () => {
    const small = buildQrMatrix("hi");
    const large = buildQrMatrix("x".repeat(600));
    expect(large.count).toBeGreaterThan(small.count);
    expect(decode("x".repeat(600))).toBe("x".repeat(600));
  });
});
