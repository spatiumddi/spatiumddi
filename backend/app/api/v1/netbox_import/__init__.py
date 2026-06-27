"""NetBox one-shot IPAM importer endpoints (issue #36).

Exposes ``/ipam/import/netbox/{test-connection,preview,commit}`` — the
read-only live-pull migration surface that mirrors the DNS (#128) and
DHCP (#129) one-shot importers: a side-effect-free preview returns the
full canonical IR, the operator round-trips it back verbatim as
``CommitIn.plan``, and commit writes native IPAM rows. See
:mod:`app.services.netbox_import` for the source reader + committer.
"""

from __future__ import annotations

from .router import router

__all__ = ["router"]
