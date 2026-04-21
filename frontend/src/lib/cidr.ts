// Minimal IPv4 CIDR utilities for client-side validation of drag-drop targets.
// IPv6 is not supported here (matches Phase-1 IPAM scope).

function ipToInt(ip: string): number | null {
  const parts = ip.split(".");
  if (parts.length !== 4) return null;
  let out = 0;
  for (const part of parts) {
    const n = Number(part);
    if (!Number.isInteger(n) || n < 0 || n > 255) return null;
    out = out * 256 + n;
  }
  // coerce to unsigned 32-bit
  return out >>> 0;
}

export interface ParsedCidr {
  base: number;
  prefix: number;
  mask: number;
}

export function parseCidr(cidr: string): ParsedCidr | null {
  const [ip, prefixStr] = cidr.split("/");
  if (!ip || !prefixStr) return null;
  const prefix = Number(prefixStr);
  if (!Number.isInteger(prefix) || prefix < 0 || prefix > 32) return null;
  const ipInt = ipToInt(ip);
  if (ipInt === null) return null;
  const mask = prefix === 0 ? 0 : (~0 << (32 - prefix)) >>> 0;
  const base = (ipInt & mask) >>> 0;
  return { base, prefix, mask };
}

/**
 * Return true if `child` CIDR is fully contained within `parent` CIDR.
 * (A network is a subnet of itself.) IPv4 only.
 */
export function cidrContains(parent: string, child: string): boolean {
  const p = parseCidr(parent);
  const c = parseCidr(child);
  if (!p || !c) return false;
  if (c.prefix < p.prefix) return false;
  return (c.base & p.mask) >>> 0 === p.base;
}

/**
 * Convert a bare IP address (no prefix) to a BigInt for numeric ordering.
 * Works for both IPv4 ("10.0.0.0") and IPv6 ("2001:db8::1"). Returns 0n
 * for malformed input — callers should only feed strings that came from
 * backend-validated CIDRs, so this is acceptable as a fall-back.
 */
function addressToBigInt(addr: string): bigint {
  if (addr.includes(":")) {
    // IPv6 — expand `::` into the required number of zero groups.
    let head = addr;
    let tail = "";
    if (addr.includes("::")) {
      const [a, b = ""] = addr.split("::");
      head = a;
      tail = b;
    }
    const headGroups = head ? head.split(":").filter(Boolean) : [];
    const tailGroups = tail ? tail.split(":").filter(Boolean) : [];
    const fill = 8 - headGroups.length - tailGroups.length;
    const groups = [...headGroups, ...Array(fill).fill("0"), ...tailGroups];
    let out = 0n;
    for (const g of groups) out = (out << 16n) + BigInt(parseInt(g, 16) || 0);
    return out;
  }
  const parts = addr.split(".");
  if (parts.length !== 4) return 0n;
  let out = 0n;
  for (const p of parts) out = (out << 8n) + BigInt(Number(p) & 0xff);
  return out;
}

/**
 * Comparator for two CIDR strings suitable for `Array.sort`. Sorts by
 * the first address numerically, falling back to prefix length so
 * same-start siblings (a supernet + subset-as-sibling) sort supernet
 * first. IPv4 sorts before IPv6 if ever mixed at the same level.
 */
export function compareNetwork(a: string, b: string): number {
  const [addrA, prefixA = "0"] = a.split("/");
  const [addrB, prefixB = "0"] = b.split("/");
  const v4a = !addrA.includes(":");
  const v4b = !addrB.includes(":");
  if (v4a !== v4b) return v4a ? -1 : 1;
  const na = addressToBigInt(addrA);
  const nb = addressToBigInt(addrB);
  if (na < nb) return -1;
  if (na > nb) return 1;
  return Number(prefixA) - Number(prefixB);
}
