"""Celery application definition shared by all worker processes.

Each worker process (worker-http, worker-browser, worker-notifications,
worker-exports) boots this same Celery app but is started with `-Q
<queue-name>` so it only pulls tasks from its own queue. The routing
below is what maps each task module to its queue.
"""

from celery import Celery

from app.core.config import get_settings
from app.core.logging import configure_logging

configure_logging()

settings = get_settings()

celery_app = Celery(
    "scraper_automation_system",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.tasks_http",
        "app.workers.tasks_browser",
        "app.workers.tasks_notifications",
        "app.workers.tasks_exports",
    ],
)

celery_app.conf.task_routes = {
    "app.workers.tasks_http.*": {"queue": "http"},
    "app.workers.tasks_browser.*": {"queue": "browser"},
    "app.workers.tasks_notifications.*": {"queue": "notifications"},
    "app.workers.tasks_exports.*": {"queue": "exports"},
}

celery_app.conf.task_default_queue = "http"
celery_app.conf.timezone = "UTC"
celery_app.conf.worker_hijack_root_logger = False
