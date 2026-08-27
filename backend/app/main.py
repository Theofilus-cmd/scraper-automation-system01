"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.error_mapping import domain_error_handler
from app.api.v1.products import router as products_router
from app.api.v1.runs import router as runs_router
from app.api.v1.scrapes import LegacyScrapesDeprecationMiddleware
from app.api.v1.scrapes import router as scrapes_router
from app.api.v1.sources import router as sources_router
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handling
from app.core.health import router as health_router
from app.core.logging import configure_logging, get_logger
from app.domain.errors import DomainError
from app.api.v1.auth import router as auth_router
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    logger.info("application startup")
    yield
    logger.info("application shutdown")


def _enforce_production_deployment_guard(settings: Settings) -> None:
    """doc 18 §7.3, amendment 7: every `/sources`, `/runs`, `/products`
    endpoint, old and new, is unauthenticated (no accounts system exists
    yet, C1). In a `production`-flagged deployment this refuses to start
    the app at all unless `UNAUTHENTICATED_SOURCE_API_ACK=true` is
    explicitly set -- preventing these endpoints from being exposed *by
    omission* under the `production` label this system already uses to
    distinguish deployment intent. Stated as honestly as doc 18 itself
    states it: this cannot detect or prevent an operator running a
    `development`-flagged instance on a publicly-reachable network
    interface, or manually overriding the ack -- it is not a network
    firewall and is not presented as one.

    Extended, deliberately and flagged here rather than silently: also
    covers the legacy `/api/v1/scrapes` alias (doc 18 §6.6). Doc 18 §7.3's
    literal text names only sources/runs/products, but `POST
    /api/v1/scrapes` can create a source and trigger a run just as freely
    as the new endpoints combined can -- leaving it unguarded would quietly
    reopen exactly the hole this guard exists to close, via a different
    path. `/products` has no mutation power either way, but is gated
    identically for one uniform rule ("every /api/v1 router except health")
    rather than a router-by-router exception list to keep in sync by hand.
    """
    if settings.environment == "production" and not settings.unauthenticated_source_api_ack:
        raise RuntimeError(
            "Refusing to start: environment=production but "
            "UNAUTHENTICATED_SOURCE_API_ACK is not set to true. The /sources, /runs, "
            "/products, and /api/v1/scrapes endpoints are unauthenticated (doc 18 §7.3) "
            "and must not be exposed in production without an explicit acknowledgement. "
            "Set UNAUTHENTICATED_SOURCE_API_ACK=true to proceed, or use "
            "environment=development for a local/single-operator deployment."
        )


def create_app() -> FastAPI:
    settings = get_settings()
    _enforce_production_deployment_guard(settings)

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
