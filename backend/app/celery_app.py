from celery import Celery

from app.config import settings

celery_app = Celery(
    "spatiumddi",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.ipam",
        "app.tasks.dns",
    ],
)

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
    },
)
