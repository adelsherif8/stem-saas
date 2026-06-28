from celery import Celery

from .config import settings

celery_app = Celery(
    "stemsaas",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_always_eager=settings.celery_eager,
    task_eager_propagates=True,
    task_track_started=True,
    result_expires=3600,
)
