"""Health and readiness endpoints.

/healthz: process-liveness only. No dependency checks. Always returns 200
if the process is running and able to handle a request at all.

/readyz: real dependency checks (Postgres, Redis). Returns 503 if either
dependency is unreachable. Deliberately does NOT check MinIO -- object
storage is not required for Phase 0 and is behind the optional "storage"
Compose profile, so readiness must not depend on it.
"""

from typing import Any

from fastapi import APIRouter, Response

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis_client import get_async_redis_client
from app.db.session import check_database_connection

router = APIRouter()
logger = get_logger(__name__)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe: process is up and can serve requests."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(response: Response) -> dict[str, Any]:
    """Readiness probe: verifies real connectivity to Postgres and Redis."""
    settings = get_settings()

    database_ok = await check_database_connection()
    redis_ok = await _check_redis(settings.redis_url)

    ready = database_ok and redis_ok
    if not ready:
        response.status_code = 503
        logger.warning(
            "readiness check failed",
            extra={"database": database_ok, "redis": redis_ok},
        )

    return {
        "status": "ok" if ready else "unavailable",
        "database": database_ok,
        "redis": redis_ok,
    }


async def _check_redis(redis_url: str) -> bool:
    client = get_async_redis_client(redis_url)
    try:
        await client.ping()
        return True
    except Exception:
        logger.exception("redis readiness check failed")
        return False
    finally:
        await client.aclose()
