"""Standard-MIB OID constants used by the SNMP poller.

Vendor-neutral throughout — every OID below is from an IETF
standards-track MIB. We avoid private-enterprise subtrees on purpose
so the same code path works against Cisco, Juniper, Arista, Aruba,
MikroTik, OPNsense / pfSense (BSD net-snmp), FortiNet, Cumulus,
SONiC, etc.
"""

from __future__ import annotations

from typing import Final

# ── SNMPv2-MIB system group (RFC 3418 §2) ──────────────────────────────
# Per-instance scalars — used by ``test_connection`` to verify the
# transport + creds before trying any tables.
OID_SYS_DESCR: Final[str] = "1.3.6.1.2.1.1.1.0"
OID_SYS_OBJECT_ID: Final[str] = "1.3.6.1.2.1.1.2.0"
OID_SYS_UP_TIME: Final[str] = "1.3.6.1.2.1.1.3.0"
OID_SYS_NAME: Final[str] = "1.3.6.1.2.1.1.5.0"

# ── IF-MIB ifTable (RFC 2863 §3.1.1) + ifXTable (§3.1.2) ───────────────
# Walked together to build the ``network_interface`` rows.
OID_IF_INDEX: Final[str] = "1.3.6.1.2.1.2.2.1.1"
OID_IF_DESCR: Final[str] = "1.3.6.1.2.1.2.2.1.2"
OID_IF_TYPE: Final[str] = "1.3.6.1.2.1.2.2.1.3"
OID_IF_SPEED: Final[str] = "1.3.6.1.2.1.2.2.1.5"  # 32-bit, capped at 4.29 Gb/s
OID_IF_PHYS_ADDRESS: Final[str] = "1.3.6.1.2.1.2.2.1.6"
OID_IF_ADMIN_STATUS: Final[str] = "1.3.6.1.2.1.2.2.1.7"  # 1=up,2=down,3=testing
OID_IF_OPER_STATUS: Final[str] = "1.3.6.1.2.1.2.2.1.8"  # 1=up,2=down,…
OID_IF_LAST_CHANGE: Final[str] = "1.3.6.1.2.1.2.2.1.9"
# IF-MIB ifXTable — the 64-bit + alias extensions.
OID_IF_NAME: Final[str] = "1.3.6.1.2.1.31.1.1.1.1"
OID_IF_HIGH_SPEED: Final[str] = "1.3.6.1.2.1.31.1.1.1.15"  # in megabits/s
OID_IF_ALIAS: Final[str] = "1.3.6.1.2.1.31.1.1.1.18"

# ── IP-MIB ipNetToPhysicalTable (RFC 4293 §3.5) ────────────────────────
# Modern, IPv6-aware ARP / neighbour table. Replaces the legacy
# ipNetToMediaTable for v4-and-v6 networks. The table is indexed by
# (ifIndex, INET address-type, INET address) — we walk all three
# columns we need below.
OID_IP_NTP_PHYS_ADDRESS: Final[str] = "1.3.6.1.2.1.4.35.1.4"
OID_IP_NTP_TYPE: Final[str] = "1.3.6.1.2.1.4.35.1.5"  # 1=other,2=invalid,3=dynamic,4=static,5=local
OID_IP_NTP_STATE: Final[str] = "1.3.6.1.2.1.4.35.1.7"  # 1=reachable,2=stale,…

# ── RFC1213-MIB ipNetToMediaTable (RFC 1213 §6) ────────────────────────
# IPv4-only legacy fallback for devices that don't expose
# ipNetToPhysicalTable. Indexed by (ifIndex, IPv4 address).
OID_IP_NTM_PHYS_ADDRESS: Final[str] = "1.3.6.1.2.1.4.22.1.2"
OID_IP_NTM_TYPE: Final[str] = "1.3.6.1.2.1.4.22.1.4"  # 1=other,2=invalid,3=dynamic,4=static

# ── Q-BRIDGE-MIB dot1qTpFdbTable (RFC 4188 §6) ─────────────────────────
# VLAN-aware bridge forwarding database. Indexed by (vlanId, mac).
OID_DOT1Q_TP_FDB_PORT: Final[str] = "1.3.6.1.2.1.17.7.1.2.2.1.2"
OID_DOT1Q_TP_FDB_STATUS: Final[str] = (
    "1.3.6.1.2.1.17.7.1.2.2.1.3"  # 1=other,2=invalid,3=learned,4=self,5=mgmt
)

# ── BRIDGE-MIB dot1dTpFdbTable (RFC 4188 §5) ───────────────────────────
# VLAN-unaware fallback for switches that don't speak Q-BRIDGE-MIB
# (rare today but still found on cheap unmanaged-managed switches).
# Indexed by mac only.
OID_DOT1D_TP_FDB_PORT: Final[str] = "1.3.6.1.2.1.17.4.3.1.2"
OID_DOT1D_TP_FDB_STATUS: Final[str] = "1.3.6.1.2.1.17.4.3.1.3"

# ── BRIDGE-MIB dot1dBasePortIfIndex (RFC 4188 §3) ──────────────────────
# FDB tables index by bridge-port number; this scalar maps each bridge
# port to the underlying ifIndex so we can join FDB → ifTable.
OID_DOT1D_BASE_PORT_IF_INDEX: Final[str] = "1.3.6.1.2.1.17.1.4.1.2"

# ── LLDP-MIB lldpRemTable (IEEE 802.1AB §11.5.2) ───────────────────────
# Vendor-neutral neighbour discovery — one row per (local-port,
# neighbour) tuple advertised on a wire. Walked once per poll to
# build ``network_neighbour`` rows.
#
# Index format is ``timeMark.localPortNum.remoteIndex`` — we only
# need ``localPortNum`` and ``remoteIndex`` for de-dupe, so the
# table-walk handler splits by '.' and keeps the last two integers.
#
# ``localPortNum`` from LLDP is a separate enumeration from ifIndex;
# on most modern switches the two coincide, but on Juniper / Aruba
# the local-port-num is a per-LLDP-agent counter. We map back to
# ifIndex via lldpLocPortIfIndex when present (currently we just
# trust localPortNum == ifIndex — fine for the tier-1 vendors and
# easy to enrich later via ENTITY-MIB / lldpLocPortDesc).
OID_LLDP_REM_CHASSIS_ID_SUBTYPE: Final[str] = "1.0.8802.1.1.2.1.4.1.1.4"
OID_LLDP_REM_CHASSIS_ID: Final[str] = "1.0.8802.1.1.2.1.4.1.1.5"
OID_LLDP_REM_PORT_ID_SUBTYPE: Final[str] = "1.0.8802.1.1.2.1.4.1.1.6"
OID_LLDP_REM_PORT_ID: Final[str] = "1.0.8802.1.1.2.1.4.1.1.7"
OID_LLDP_REM_PORT_DESC: Final[str] = "1.0.8802.1.1.2.1.4.1.1.8"
OID_LLDP_REM_SYS_NAME: Final[str] = "1.0.8802.1.1.2.1.4.1.1.9"
OID_LLDP_REM_SYS_DESC: Final[str] = "1.0.8802.1.1.2.1.4.1.1.10"
OID_LLDP_REM_SYS_CAP_ENABLED: Final[str] = "1.0.8802.1.1.2.1.4.1.1.12"

# ── LLDP-MIB lldpRemManAddrTable (§11.5.4) ─────────────────────────────
# Optional management addresses the neighbour advertises. Lets us
# cross-reference into IPAM. Most switches advertise their primary
# management IP here; some advertise IPv6 too.
OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE: Final[str] = "1.0.8802.1.1.2.1.4.2.1.3"

__all__ = [
    "OID_SYS_DESCR",
    "OID_SYS_OBJECT_ID",
    "OID_SYS_UP_TIME",
    "OID_SYS_NAME",
    "OID_IF_INDEX",
    "OID_IF_DESCR",
    "OID_IF_TYPE",
    "OID_IF_SPEED",
    "OID_IF_PHYS_ADDRESS",
    "OID_IF_ADMIN_STATUS",
    "OID_IF_OPER_STATUS",
    "OID_IF_LAST_CHANGE",
    "OID_IF_NAME",
    "OID_IF_HIGH_SPEED",
    "OID_IF_ALIAS",
    "OID_IP_NTP_PHYS_ADDRESS",
    "OID_IP_NTP_TYPE",
    "OID_IP_NTP_STATE",
    "OID_IP_NTM_PHYS_ADDRESS",
    "OID_IP_NTM_TYPE",
    "OID_DOT1Q_TP_FDB_PORT",
    "OID_DOT1Q_TP_FDB_STATUS",
    "OID_DOT1D_TP_FDB_PORT",
    "OID_DOT1D_TP_FDB_STATUS",
    "OID_DOT1D_BASE_PORT_IF_INDEX",
    "OID_LLDP_REM_CHASSIS_ID_SUBTYPE",
    "OID_LLDP_REM_CHASSIS_ID",
    "OID_LLDP_REM_PORT_ID_SUBTYPE",
    "OID_LLDP_REM_PORT_ID",
    "OID_LLDP_REM_PORT_DESC",
    "OID_LLDP_REM_SYS_NAME",
    "OID_LLDP_REM_SYS_DESC",
    "OID_LLDP_REM_SYS_CAP_ENABLED",
    "OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE",
    "OID_IGMP_CACHE_SELF",
    "OID_IGMP_CACHE_LAST_REPORTER",
    "OID_IGMP_CACHE_UP_TIME",
    "OID_IGMP_CACHE_STATUS",
]


# ── IGMP-STD-MIB (RFC 2933) — issue #126 Phase 3 ────────────────────
#
# ``igmpCacheTable`` (1.3.6.1.2.1.85.1) carries the per-interface
# IGMP join state. Index is ``(igmpCacheAddress, igmpCacheIfIndex)``
# — the multicast group + the interface that received the most
# recent IGMP report. ``igmpCacheLastReporter`` is the single
# most-actionable column for the multicast registry: it's the IP
# of the host whose last IGMPv2 / v3 membership-report packet
# arrived on the interface, i.e. the consumer endpoint.

OID_IGMP_CACHE_SELF: Final[str] = "1.3.6.1.2.1.85.1.2.1.3"
OID_IGMP_CACHE_LAST_REPORTER: Final[str] = "1.3.6.1.2.1.85.1.2.1.4"
OID_IGMP_CACHE_UP_TIME: Final[str] = "1.3.6.1.2.1.85.1.2.1.5"
OID_IGMP_CACHE_STATUS: Final[str] = "1.3.6.1.2.1.85.1.2.1.7"
