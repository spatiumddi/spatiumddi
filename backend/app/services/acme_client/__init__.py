"""Embedded ACME *client* (issue #438 Phase 1).

SpatiumDDI acting as an ACME client against a public CA (Let's Encrypt),
driving the RFC 8555 DNS-01 flow to issue a CA-trusted TLS certificate
for the appliance Web UI. The DNS-01 challenge is solved through
SpatiumDDI's OWN managed DNS zones (the same ``record_ops`` pipeline the
ACME *provider* side uses).

Distinct from ``app.services.acme`` — that module is the *provider* side
(an acme-dns-compatible HTTP surface external clients use). This package
is the *client* side.

Layers:

* :mod:`.engine` — a minimal hand-rolled RFC 8555 client
  (:class:`~app.services.acme_client.engine.ACMEClient`). Pure protocol:
  account, order, authorizations, challenge, finalize, download. No DB,
  no DNS-write knowledge.
* :mod:`.dns01` — solve / cleanup of a single ``dns-01`` challenge by
  writing a TXT record into the matching SpatiumDDI-managed zone.
* :mod:`.orchestrator` — ties the two together end-to-end: drive one
  :class:`~app.models.acme_client.ACMEOrder` to issuance, land the chain
  in an :class:`~app.models.appliance.ApplianceCertificate`, deploy it,
  always clean up the challenge TXT.
"""

from __future__ import annotations

from app.services.acme_client.engine import (
    LE_PRODUCTION_DIRECTORY,
    LE_STAGING_DIRECTORY,
    ACMEClient,
    ACMEProtocolError,
)

__all__ = [
    "ACMEClient",
    "ACMEProtocolError",
    "LE_PRODUCTION_DIRECTORY",
    "LE_STAGING_DIRECTORY",
]
