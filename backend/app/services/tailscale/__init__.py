"""Tailscale integration — read-only REST mirror into IPAM.

Service layer. The reconciler + Tailscale API client live here;
per-tenant row CRUD lives under ``app.api.v1.tailscale``; Celery
beat sweep lives under ``app.tasks.tailscale_sync``.
"""

from __future__ import annotations
