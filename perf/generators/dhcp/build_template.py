#!/usr/bin/env python3
"""Build perfdhcp relay packet templates that stamp a per-subnet giaddr (§3.1.2).

perfdhcp's ``-T <template>`` flag takes a HEX-encoded DHCPv4 packet template — perfdhcp
sends a copy of the template per simulated client, mutating only the fields named by
``-X`` offsets (xid, etc). For the relay topology we need every packet from a given
shard to carry a fixed ``giaddr`` so Kea selects the right subnet (Kea matches
``giaddr`` against each scope's ``relay.ip-addresses`` —
backend/app/drivers/dhcp/kea.py:236-241). So we emit ONE template per giaddr, with that
giaddr baked into the BOOTP ``giaddr`` field at fixed offset 24.

This script writes ``relay_templates/giaddr-<ip>.hex`` for every giaddr in a manifest
(or an explicit ``--giaddr`` list), plus the matching ``-X`` offset hints documented in
the README. The 8-giaddr → 8-subnet mapping is fixed by the manifest (giaddr ``10.i.0.1``
→ subnet ``10.i.0.0/16``; see relay_templates/README.md).

The template is a minimal but valid BOOTREQUEST / DHCPDISCOVER:

  BOOTP fixed header (236 bytes) + magic cookie (4) + options:
    op=1 (BOOTREQUEST), htype=1 (ethernet), hlen=6, hops=1 (relayed),
    xid=0x00000000 (perfdhcp mutates via -X 4),
    secs=0, flags=0x0000 (unicast reply to the relay),
    ciaddr=0, yiaddr=0, siaddr=0,
    giaddr=<the per-subnet giaddr>  ← offset 24, 4 bytes (the whole point),
    chaddr=00..00 (perfdhcp's -b mac=<base> + -X mutate the client MAC at offset 28),
    sname[64]=0, file[128]=0,
    magic cookie 63 82 53 63,
    option 53 (DHCP message type) = 1 (DISCOVER),
    option 55 (param request list) = 1,3,6,15,28,51 (typical),
    option 12 (host name) placeholder (perfdhcp can leave it),
    option 255 (end).

CLI:
    python3 build_template.py --manifest <path>            # all giaddrs in the manifest
    python3 build_template.py --giaddr 10.0.0.1 10.1.0.1   # explicit list
    python3 build_template.py --giaddr 10.0.0.1 --print    # hex to stdout, don't write

Grounding (real backend, cited file:line):
  * Kea subnet-by-giaddr selection — backend/app/drivers/dhcp/kea.py:236-241
    (``out["relay"] = {"ip-addresses": list(scope.relay_addresses)}``).
  * Relay topology requires one giaddr per subnet, carried in the manifest's 8-entry
    ``giaddr`` list — see manifest.validate() backend constraint mirrored in
    perf/harness/spddi_perf/manifest.py:302-309.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys

import spddi_perf.manifest as manifest_mod

# ── BOOTP / DHCP field offsets (RFC 2131 §2) ──────────────────────────────────────
GIADDR_OFFSET = 24       # bytes into the packet where the 4-byte giaddr lives
XID_OFFSET = 4           # perfdhcp -X 4 mutates the transaction id here
CHADDR_OFFSET = 28       # perfdhcp -b mac=<base> mutates the client MAC here

MAGIC_COOKIE = bytes((0x63, 0x82, 0x53, 0x63))


def build_discover_template(giaddr: str) -> bytes:
    """Return the raw bytes of a DHCPDISCOVER template carrying ``giaddr``."""
    gi = ipaddress.IPv4Address(giaddr).packed  # 4 bytes, network order

    pkt = bytearray()
    pkt += bytes((1,))      # op = BOOTREQUEST
    pkt += bytes((1,))      # htype = ethernet
    pkt += bytes((6,))      # hlen = 6
    pkt += bytes((1,))      # hops = 1 (relayed)
    pkt += bytes(4)         # xid (offset 4) — perfdhcp mutates via -X 4
    pkt += bytes(2)         # secs
    pkt += bytes((0x00, 0x00))  # flags = 0 (unicast reply to the relay)
    pkt += bytes(4)         # ciaddr
    pkt += bytes(4)         # yiaddr
    pkt += bytes(4)         # siaddr
    assert len(pkt) == GIADDR_OFFSET, f"giaddr offset drift: {len(pkt)}"
    pkt += gi               # giaddr (offset 24) ← the per-subnet relay IP
    assert len(pkt) == CHADDR_OFFSET, f"chaddr offset drift: {len(pkt)}"
    pkt += bytes(16)        # chaddr (offset 28, 16 bytes; perfdhcp -b mac mutates)
    pkt += bytes(64)        # sname
    pkt += bytes(128)       # file
    assert len(pkt) == 236, f"BOOTP fixed header must be 236 bytes, got {len(pkt)}"
    pkt += MAGIC_COOKIE     # magic cookie (offset 236)

    # Options.
    pkt += bytes((53, 1, 1))                 # option 53 (msg type) = DISCOVER
    pkt += bytes((55, 6, 1, 3, 6, 15, 28, 51))  # option 55 param request list
    pkt += bytes((255,))                     # option 255 (end)
    return bytes(pkt)


def to_hex(pkt: bytes) -> str:
    """perfdhcp expects an uppercase hex string (no spaces, no newline)."""
    return pkt.hex().upper()


def giaddrs_from_manifest(path: str) -> list[str]:
    m = manifest_mod.load(path)
    if m.target.dhcp.topology != "relay":
        raise SystemExit(
            f"{path}: topology is {m.target.dhcp.topology!r}, not 'relay' — no relay "
            "templates needed (broadcast targets the node directly, §3.1.2).")
    if not m.target.dhcp.giaddr:
        raise SystemExit(f"{path}: relay topology but no giaddr list (§3.1.2)")
    return list(m.target.dhcp.giaddr)


def write_template(giaddr: str, out_dir: str) -> str:
    pkt = build_discover_template(giaddr)
    path = os.path.join(out_dir, f"giaddr-{giaddr}.hex")
    with open(path, "w", encoding="ascii") as f:
        f.write(to_hex(pkt) + "\n")
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate perfdhcp -T relay templates (one giaddr per subnet, §3.1.2).")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--manifest", help="manifest YAML — use its 8-entry giaddr list")
    src.add_argument("--giaddr", nargs="+", help="explicit giaddr IP(s)")
    p.add_argument("--out-dir", default=None,
                   help="output dir (default: ./relay_templates next to this script)")
    p.add_argument("--print", dest="print_only", action="store_true",
                   help="print the hex to stdout instead of writing files")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    giaddrs = (args.giaddr if args.giaddr else giaddrs_from_manifest(args.manifest))

    if args.print_only:
        for g in giaddrs:
            print(f"# giaddr {g} (subnet {_subnet_for(g)})")
            print(to_hex(build_discover_template(g)))
        return 0

    out_dir = args.out_dir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "relay_templates")
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for g in giaddrs:
        path = write_template(g, out_dir)
        written.append(path)
        print(f"wrote {path}  (giaddr {g} → subnet {_subnet_for(g)})")
    return 0


def _subnet_for(giaddr: str) -> str:
    """Best-effort human label of the /16 a giaddr selects (10.i.0.1 → 10.i.0.0/16)."""
    try:
        a = ipaddress.IPv4Address(giaddr)
        octets = str(a).split(".")
        return f"{octets[0]}.{octets[1]}.0.0/16"
    except ValueError:
        return "?"


if __name__ == "__main__":
    sys.exit(main())
