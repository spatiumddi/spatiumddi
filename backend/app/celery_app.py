from celery import Celery
from celery.schedules import schedule

from app.config import settings

celery_app = Celery(
    "spatiumddi",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.ipam",
        "app.tasks.dns",
        "app.tasks.dhcp_health",
    ],
)

celery_app.conf.beat_schedule = {
    "dns-agent-stale-sweep": {
        "task": "app.tasks.dns.agent_stale_sweep",
        "schedule": 60.0,
    },
}

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,  # Required for idempotency — task not acked until complete
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "app.tasks.ipam.*": {"queue": "ipam"},
        "app.tasks.dns.*": {"queue": "dns"},
        "app.tasks.dhcp_health.*": {"queue": "dhcp"},
    },
    beat_schedule={
        # Every 60s, fan-out health checks to every registered DNS server.
        "dns-health-sweep": {
            "task": "app.tasks.dns.check_all_dns_servers_health",
            "schedule": schedule(run_every=60.0),
        },
        # Every 60s, fan-out health checks to every registered DHCP server.
        "dhcp-health-sweep": {
            "task": "app.tasks.dhcp_health.check_all_dhcp_servers_health",
            "schedule": schedule(run_every=60.0),
        },
    },
)
