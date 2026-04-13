"""IPAM background tasks (discovery, utilization recalc)."""

import structlog

from app.celery_app import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.ipam.recalc_utilization", bind=True, max_retries=3)
def recalc_utilization(self: object, subnet_id: str) -> None:  # type: ignore[type-arg]
    """
    Recompute utilization_percent, total_ips, and allocated_ips for a subnet.
    Idempotent — safe to retry.
    """
    logger.info("recalc_utilization_started", subnet_id=subnet_id)
    # TODO: implement in Phase 1 build-out
    logger.info("recalc_utilization_complete", subnet_id=subnet_id)


@celery_app.task(name="app.tasks.ipam.discovery_scan", bind=True, max_retries=2)
def discovery_scan(self: object, subnet_id: str) -> None:  # type: ignore[type-arg]
    """
    Async ICMP ping sweep of a subnet to discover IPs not yet in IPAM.
    Idempotent — safe to retry.
    """
    logger.info("discovery_scan_started", subnet_id=subnet_id)
    # TODO: implement in Phase 2 build-out
    logger.info("discovery_scan_complete", subnet_id=subnet_id)
