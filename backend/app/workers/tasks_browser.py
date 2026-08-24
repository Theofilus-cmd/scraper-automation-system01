"""Browser-rendering queue task stubs.

Phase 0 ships only a trivial `ping` task. Playwright/Chromium-backed
fetch tasks arrive in a later phase -- browser rendering is explicitly
out of scope for Phase 0 (see doc 16).
"""

from app.core.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


@celery_app.task(name="app.workers.tasks_browser.ping")
def ping() -> dict[str, str]:
    logger.info("ping task executed", extra={"queue": "browser"})
    return {"queue": "browser", "status": "pong"}
