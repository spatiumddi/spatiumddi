"""OPNsense integration — read-only REST mirror into IPAM.

Service layer. The reconciler + OPNsense API client live here;
per-firewall row CRUD lives under ``app.api.v1.opnsense``; the Celery
beat sweep lives under ``app.tasks.opnsense_sync``.
"""

from __future__ import annotations
