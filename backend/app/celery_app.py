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
        "app.tasks.dhcp_mac_blocks",
        "app.tasks.dhcp_pull_leases",
        "app.tasks.alerts",
        "app.tasks.heartbeat",
        "app.tasks.oui_update",
        "app.tasks.prune_metrics",
        "app.tasks.update_check",
        "app.tasks.kubernetes_sync",
        "app.tasks.docker_sync",
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
        "app.tasks.dhcp_mac_blocks.*": {"queue": "dhcp"},
        "app.tasks.dhcp_pull_leases.*": {"queue": "dhcp"},
        "app.tasks.alerts.*": {"queue": "default"},
        "app.tasks.heartbeat.*": {"queue": "default"},
        "app.tasks.oui_update.*": {"queue": "default"},
        "app.tasks.prune_metrics.*": {"queue": "default"},
        "app.tasks.update_check.*": {"queue": "default"},
        "app.tasks.kubernetes_sync.*": {"queue": "default"},
        "app.tasks.docker_sync.*": {"queue": "default"},
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
        # Every 10 s, tick the DHCP lease-pull task. The task itself checks
        # ``PlatformSettings.dhcp_pull_leases_enabled`` and the per-run
        # interval (in seconds, min 10), so cadence changes in the UI take
        # effect without restarting celery-beat. The 10-second tick caps the
        # fastest achievable cadence — enough for near-real-time IPAM
        # population from Windows DHCP while leaving the WinRM endpoint
        # breathing room. Only applies to agentless drivers (windows_dhcp).
        # Additive-only — the existing lease-cleanup sweep handles stale
        # expiry.
        "dhcp-pull-leases": {
            "task": "app.tasks.dhcp_pull_leases.auto_pull_dhcp_leases",
            "schedule": schedule(run_every=10.0),
        },
        # Every 60 s, reconcile the Windows DHCP server-level deny
        # filter list against each group's active MAC blocks. Kea is
        # driven by the ConfigBundle DROP class render and needs no
        # periodic push. Windows has no built-in expiry so this tick
        # handles the ``expires_at`` transitions uniformly.
        "dhcp-mac-blocks-sync": {
            "task": "app.tasks.dhcp_mac_blocks.sync_dhcp_mac_blocks",
            "schedule": schedule(run_every=60.0),
        },
        # Every 60 s, evaluate every enabled AlertRule. Idempotent —
        # opens events for fresh matches, resolves events whose
        # subject no longer matches. Delivery reuses the
        # audit-forward syslog + webhook targets.
        "alerts-evaluate": {
            "task": "app.tasks.alerts.evaluate_alerts",
            "schedule": schedule(run_every=60.0),
        },
        # Every hour, tick the IEEE OUI refresh. Opt-in feature — the
        # task itself checks ``PlatformSettings.oui_lookup_enabled`` and
        # ``oui_update_interval_hours`` (default 24h) so the effective
        # cadence is UI-controlled. Hourly beat is the granularity knob;
        # anyone who wants "every 4 hours" sets interval_hours=4 and the
        # task self-skips the other 3 fires.
        "oui-refresh": {
            "task": "app.tasks.oui_update.auto_update_oui_database",
            "schedule": schedule(run_every=3600.0),
        },
        # Nightly prune of dns/dhcp metric_sample rows older than the
        # configured retention window (default 7 d). Keeps the two
        # tables a bounded size without coarsening resolution. Cadence
        # is deliberately slow — the tables are already small and
        # pruning more often just burns cycles.
        "metric-samples-prune": {
            "task": "app.tasks.prune_metrics.prune_metric_samples",
            "schedule": schedule(run_every=24 * 3600.0),
        },
        # Once a day, check GitHub for the latest release tag. Gated
        # on ``PlatformSettings.github_release_check_enabled`` — so
        # operators can turn this off in air-gapped deployments
        # without restarting beat. Unauthenticated call; the 60/hour
        # rate limit is plenty for a daily tick.
        "update-check": {
            "task": "app.tasks.update_check.check_github_release",
            "schedule": schedule(run_every=24 * 3600.0),
        },
        # Every 30 s, sweep every enabled KubernetesCluster. The per-
        # cluster ``sync_interval_seconds`` (min 30) gates the actual
        # reconciler pass, so a cluster configured with a 5-minute
        # interval sees 10 beat ticks between passes. Gated overall by
        # ``PlatformSettings.integration_kubernetes_enabled`` — turn
        # the master toggle off and no cluster is polled.
        "kubernetes-sync-sweep": {
            "task": "app.tasks.kubernetes_sync.sweep_kubernetes_clusters",
            "schedule": schedule(run_every=30.0),
        },
        # Same pattern for Docker — 30 s beat, per-host interval gates
        # the actual reconcile pass. Gated overall by
        # ``PlatformSettings.integration_docker_enabled``.
        "docker-sync-sweep": {
            "task": "app.tasks.docker_sync.sweep_docker_hosts",
            "schedule": schedule(run_every=30.0),
        },
        # Beat self-heartbeat — writes a redis key every 30 s with a
        # 5-min TTL so the platform-health endpoint can tell a stalled
        # beat from a healthy one. Celery has no built-in beat-liveness
        # primitive, so a trivial heartbeat task is the cheapest signal.
        "platform-beat-heartbeat": {
            "task": "app.tasks.heartbeat.beat_tick",
            "schedule": schedule(run_every=30.0),
        },
    },
)
