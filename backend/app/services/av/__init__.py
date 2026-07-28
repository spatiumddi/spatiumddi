"""AV / Audio-Video-over-IP awareness — issue #540 (part of #543).

The AV vertical deliberately has no address plane of its own: a Dante
flow *is* a ``MulticastGroup``, and ``app.models.av`` only adds the
AV-specific descriptor (:class:`~app.models.av.AVFlowProfile`) plus
the operator-declared per-protocol ranges
(:class:`~app.models.av.AVReservedRange`) that make the descriptor
checkable.

This package holds the reasoning that isn't request-shaped — the
containment maths over those declared ranges. It is pure DB + stdlib
``ipaddress``; nothing here opens a socket, and nothing here talks to
an AV device. Discovery (mDNS/Dante, NMOS IS-04) is explicitly out of
scope for #540 Phase 1.

Callers import by full path::

    from app.services.av.ranges import (
        check_allocation_conflict,
        resolve_range_for_address,
        suggest_default_range,
    )
"""

from __future__ import annotations
