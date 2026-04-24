"""Proxmox VE integration — read-only REST mirror into IPAM + DNS.

Service layer. The reconciler + PVE API client live here; per-endpoint
row CRUD lives under ``app.api.v1.proxmox``; Celery beat sweep lives
under ``app.tasks.proxmox_sync``.
"""

from __future__ import annotations
