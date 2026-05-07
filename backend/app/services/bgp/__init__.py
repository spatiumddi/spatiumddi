"""BGP enrichment surface — RIPEstat + PeeringDB clients with
in-process caching (issue #122).

The control plane already tracks the *registry* side of an ASN
(WHOIS/RDAP holder + RPKI ROAs). This module covers the *routing
table* side: announced prefixes, peers, IXP presence, and prefix
origin lookup. All data sources are public + free; we cache
aggressively in-memory so the operator copilot can answer
'who's announcing 8.8.8.8?' or 'what does AS15169 announce?' in
hundreds of ms even on installs that don't pre-warm anything.
"""

from app.services.bgp.peeringdb import (
    fetch_asn_ixps,
    fetch_asn_network,
)
from app.services.bgp.ripestat import (
    fetch_announced_prefixes,
    fetch_as_overview,
    fetch_prefix_overview,
    fetch_routing_history,
)

__all__ = [
    "fetch_announced_prefixes",
    "fetch_as_overview",
    "fetch_prefix_overview",
    "fetch_routing_history",
    "fetch_asn_network",
    "fetch_asn_ixps",
]
