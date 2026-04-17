from celery import Celery
from celery.schedules import schedule

from app.config import settings

celery_app = Celery(
    "spatiumddi",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.ipam",
        "app.tasks.ipam_dns_sync",
        "app.tasks.dns",
        "app.tasks.dns_pull",
        "app.tasks.dhcp_health",
        "app.tasks.dhcp_lease_cleanup",
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
        "app.tasks.ipam_dns_sync.*": {"queue": "ipam"},
        "app.tasks.dns.*": {"queue": "dns"},
        "app.tasks.dns_pull.*": {"queue": "dns"},
        "app.tasks.dhcp_health.*": {"queue": "dhcp"},
        "app.tasks.dhcp_lease_cleanup.*": {"queue": "dhcp"},
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
        # Every 5 min, sweep DHCP leases whose expires_at has passed the grace
        # window and remove their mirrored IPAM rows (auto_from_lease=True).
        "dhcp-lease-cleanup": {
            "task": "app.tasks.dhcp_lease_cleanup.sweep_expired_leases",
            "schedule": schedule(run_every=300.0),
        },
        # Every 60 s, tick the IPAM↔DNS auto-sync task. The task itself
        # checks ``PlatformSettings.dns_auto_sync_enabled`` and the per-run
        # interval, so changing cadence in the UI takes effect without
        # restarting celery-beat.
        "ipam-dns-auto-sync": {
            "task": "app.tasks.ipam_dns_sync.auto_sync_ipam_dns",
            "schedule": schedule(run_every=60.0),
        },
        # Every 60 s, tick the DNS pull-from-server task. The task itself
        # checks ``PlatformSettings.dns_pull_from_server_enabled`` and the
        # per-run interval, so cadence changes in the UI take effect without
        # restarting celery-beat. Additive-only — never deletes existing
        # DB rows.
        "dns-pull-from-server": {
            "task": "app.tasks.dns_pull.auto_pull_dns_from_servers",
            "schedule": schedule(run_every=60.0),
        },
    },
)
