"""Export-generation queue task stubs.

Phase 0 ships only a trivial `ping` task. Real CSV/export generation
arrives in a later phase.
"""

from app.core.logging import get_logger
from app.workers.celery_app import typed_task

logger = get_logger(__name__)


@typed_task(name="app.workers.tasks_exports.ping")
def ping() -> dict[str, str]:
    logger.info("ping task executed", extra={"queue": "exports"})
    return {"queue": "exports", "status": "pong"}
