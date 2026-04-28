"""Celery wrapper for on-demand nmap scans.

The runner in :mod:`app.services.nmap.runner` does all the real work;
this module exists purely so the API can dispatch a scan onto a
worker and return 202 immediately. Scans are explicitly **not
retried** — the operator triggers them, and re-running on a worker
crash would replay potentially noisy port traffic without consent.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog

from app.celery_app import celery_app
from app.services.nmap.runner import run_scan

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.nmap.run_scan", bind=True, max_retries=0)
def run_scan_task(self: Any, scan_id_str: str) -> dict[str, str]:  # noqa: ARG001
    """Drive one nmap scan from start to finish.

    The runner persists everything it needs into the row as it goes,
    so this wrapper has no return payload of its own — we just echo
    the scan id back so celery_result_backend has something to display.
    """
    scan_id = uuid.UUID(scan_id_str)
    logger.info("nmap_run_scan_task_started", scan_id=scan_id_str)
    asyncio.run(run_scan(scan_id))
    return {"scan_id": scan_id_str}
