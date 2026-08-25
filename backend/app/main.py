"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.scrapes import router as scrapes_router
from app.core.config import get_settings
from app.core.errors import register_error_handling
from app.core.health import router as health_router
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    logger.info("application startup")
    yield
    logger.info("application shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(title="Scraper Automation System API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handling(app)

    # Health endpoints stay unprefixed (doc 16); /api/v1 arrives with
    # Phase 1's first real endpoints (doc 17).
    app.include_router(health_router)
    app.include_router(scrapes_router, prefix="/api/v1")

    return app


app = create_app()
