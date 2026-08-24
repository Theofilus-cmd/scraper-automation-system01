"""HTTP-fetch queue task stubs.

Phase 0 ships only a trivial `ping` task to prove the worker process,
queue routing, and broker connection all work end-to-end. Real fetch
tasks arrive in a later phase.
"""

from app.core.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


@celery_app.task(name="app.workers.tasks_http.ping")
def ping() -> dict[str, str]:
    logger.info("ping task executed", extra={"queue": "http"})
    return {"queue": "http", "status": "pong"}
