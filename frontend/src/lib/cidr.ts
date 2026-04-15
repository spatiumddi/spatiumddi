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
  return ((c.base & p.mask) >>> 0) === p.base;
}
