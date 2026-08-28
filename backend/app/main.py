"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.auth import router as auth_router
from app.api.v1.error_mapping import domain_error_handler
from app.api.v1.products import router as products_router
from app.api.v1.runs import router as runs_router
from app.api.v1.scrapes import LegacyScrapesDeprecationMiddleware
from app.api.v1.scrapes import router as scrapes_router
from app.api.v1.sources import router as sources_router
from app.core.config import get_settings
from app.core.errors import register_error_handling
from app.core.health import router as health_router
from app.core.logging import configure_logging, get_logger
from app.domain.errors import DomainError

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
    app.add_middleware(LegacyScrapesDeprecationMiddleware)
    register_error_handling(app)
    # Domain exceptions (app/domain/errors.py) are raised directly by every
    # router in app/api/v1/ -- this is the one place they're translated
    # into the doc 06 §1 envelope, alongside register_error_handling()'s
    # existing ApiError/RequestValidationError/Exception handlers above.
    app.add_exception_handler(DomainError, domain_error_handler)  # type: ignore

    # Health endpoints stay unprefixed (doc 16); /api/v1 arrives with
    # Phase 1's first real endpoints (doc 17), extended with Phase 2's
    # source/run/product lifecycle (doc 18 §6).
    app.include_router(health_router)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(sources_router, prefix="/api/v1")
    app.include_router(runs_router, prefix="/api/v1")
    app.include_router(products_router, prefix="/api/v1")
    app.include_router(scrapes_router, prefix="/api/v1")

    return app


app = create_app()
