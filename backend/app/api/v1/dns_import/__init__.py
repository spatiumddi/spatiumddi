"""DNS configuration importer endpoints (issue #128).

Mounted at ``/dns/import`` and gated behind the ``dns.import``
feature module so operators who don't need the surface can hide
it. Phase 1 ships BIND9; Phase 2 + 3 add Windows DNS + PowerDNS
under the same prefix.
"""

from .router import router

__all__ = ["router"]
