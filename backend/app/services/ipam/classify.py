"""IP-address classification helpers.

Centralised so the DNS split-horizon safety guard (issue #25) and any
future caller share one definition of "private". Python's
``ipaddress.IPv4Address.is_private`` is too narrow for our purpose —
it doesn't include CGNAT (RFC 6598) and treats some link-local
edge cases inconsistently. We layer the SpatiumDDI-specific rules
on top.
"""

from __future__ import annotations

import ipaddress
from typing import Final

# CGNAT range (RFC 6598). Python's stdlib treats this as "global" /
# non-private — but for split-horizon-publishing safety it's the same
# class as RFC 1918: not routed on the public internet, so publishing
# it through a public-facing resolver is a misconfiguration.
# Constructed via the concrete IPv4Network (not ``ip_network``, which
# infers the IPv4|IPv6 union) so ``subnet_of`` in ``is_cgnat_cidr``
# type-checks against a definite IPv4Network arg.
_CGNAT_V4: Final = ipaddress.IPv4Network("100.64.0.0/10")

# Unique-Local Addresses (RFC 4193). Not on the public internet.
_ULA_V6: Final = ipaddress.IPv6Network("fc00::/7")


def is_private_ip(value: str) -> bool:
    """Return True for IPs that should never be exposed through a
    public-facing resolver:

    * RFC 1918 (10/8, 172.16/12, 192.168/16)
    * CGNAT (100.64/10, RFC 6598)
    * Link-local IPv4 (169.254/16)
    * Link-local IPv6 (fe80::/10)
    * Unique-Local IPv6 (fc00::/7, RFC 4193)
    * Loopback (127/8, ::1)

    Public IPv4 + global IPv6 unicast return False.
    """
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False

    # Stdlib catches RFC 1918 + loopback + most link-local cases;
    # we layer CGNAT + ULA on top because stdlib treats them as global.
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_V4:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip in _ULA_V6:
        return True
    return False


def is_cgnat_cidr(value: str) -> bool:
    """Return True when a network (CIDR) sits inside CGNAT space
    (RFC 6598, ``100.64.0.0/10``).

    Operator-facing classification only (issue #42): drives the
    "CGNAT" subnet badge + the New-Subnet advisory hint. CGNAT is the
    one reserved IPv4 range that's actively *allocated* by overlays
    (Tailscale carves ``100.64.0.0/10`` per-tenant), so operators who
    reach for it as a normal on-prem LAN hit silent overlap pain —
    surfacing it is a footgun-preventer.

    A leaf subnet is "CGNAT" when its whole CIDR is contained in
    ``100.64.0.0/10``. Supernets that merely *overlap* (e.g. a
    hypothetical ``100.0.0.0/8``) are not flagged — real managed
    subnets are leaves well inside the range. IPv6 always returns
    False (CGNAT is an IPv4-only concept).
    """
    try:
        net = ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    if not isinstance(net, ipaddress.IPv4Network):
        return False
    return net.subnet_of(_CGNAT_V4)


__all__ = ["is_private_ip", "is_cgnat_cidr"]
