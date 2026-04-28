/**
 * Stable client-side ID generator that doesn't rely on `crypto.randomUUID`,
 * which is only available in secure contexts (HTTPS or localhost). When the
 * dev server is reached over plain HTTP from a remote address (typical
 * for the in-house lab setup), `crypto.randomUUID` is undefined.
 *
 * Falls back to a `crypto.getRandomValues` based UUID v4, then to
 * `Math.random` if neither WebCrypto API is present (e.g. very old browsers).
 * The IDs are only used as opaque keys in the planner tree — uniqueness
 * within a single tree is enough.
 */
export function newNodeId(): string {
  // Preferred path: WebCrypto's randomUUID, available on HTTPS / localhost.
  const c = (globalThis as { crypto?: Crypto }).crypto;
  if (c && typeof c.randomUUID === "function") {
    return c.randomUUID();
  }
  // Compose a v4 UUID from getRandomValues.
  if (c && typeof c.getRandomValues === "function") {
    const buf = new Uint8Array(16);
    c.getRandomValues(buf);
    buf[6] = (buf[6] & 0x0f) | 0x40; // version 4
    buf[8] = (buf[8] & 0x3f) | 0x80; // variant 10
    const hex = Array.from(buf, (b) => b.toString(16).padStart(2, "0"));
    return (
      hex.slice(0, 4).join("") +
      "-" +
      hex.slice(4, 6).join("") +
      "-" +
      hex.slice(6, 8).join("") +
      "-" +
      hex.slice(8, 10).join("") +
      "-" +
      hex.slice(10, 16).join("")
    );
  }
  // Last-resort fallback. Not cryptographically random but fine as a tree key.
  return (
    "n-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10)
  );
}
