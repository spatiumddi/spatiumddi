"""Shared Technitium DNS Server helpers.

Technitium is reachable from the control plane in two unrelated ways, and
both need the same rdata translation:

* the one-shot live-pull importer (:mod:`app.services.dns_import.technitium`,
  issue #744), and
* the agentless ``technitium_api`` driver
  (:mod:`app.drivers.dns.technitium_api`, issue #810).

:mod:`app.services.technitium.rdata` holds that translation so the two
cannot disagree about what a TLSA record looks like.
"""

from __future__ import annotations

__all__: list[str] = []
