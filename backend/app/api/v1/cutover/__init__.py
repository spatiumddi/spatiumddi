"""Windows → SpatiumDDI cutover endpoints (issue #756).

Exposes ``/migration/cutover/plans/…`` — the guided migration surface that
picks up where the one-shot DNS (#128) / DHCP (#129) importers stop. The
importers get an operator's Windows estate *into* SpatiumDDI; this router
drives the rest: per-item parity against the live Windows server, a shadow
query comparison during the parallel run, the TTL pre-flight and DHCP lease
handover, the switch itself, the rollback, and the decommission checklist.

See :mod:`app.services.cutover` for the workflow logic — every handler here
is transport, authorization, audit, and the timeline row.
"""

from __future__ import annotations

from .router import router

__all__ = ["router"]
