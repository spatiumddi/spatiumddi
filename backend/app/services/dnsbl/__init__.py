"""DNSBL / RBL reputation monitoring service (#528).

Public entry points:

* :func:`app.services.dnsbl.catalog.seed_dnsbl_catalog` — idempotent
  startup seed of the curated blocklist catalog (platform rows).
* :func:`app.services.dnsbl.sweep.run_sweep` — the full candidate ×
  enabled-list sweep (called by the Celery beat task).
* :func:`app.services.dnsbl.sweep.check_ip_now` — single-IP on-demand
  check (the IP-detail-modal "Check now" button + MCP re-check).
"""

from app.services.dnsbl.catalog import seed_dnsbl_catalog

__all__ = ["seed_dnsbl_catalog"]
