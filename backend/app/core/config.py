"""Centralized application configuration.

All runtime configuration is read from environment variables (optionally
via a local .env file during development). Nothing here should ever
contain a real secret -- see .env.example for the placeholder values used
in local development.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from the environment.

    Note: Celery worker concurrency (HTTP_WORKER_CONCURRENCY,
    BROWSER_WORKER_CONCURRENCY) is intentionally NOT modeled here. Those
    values are consumed purely as shell-level ${VAR} substitution in each
    worker's `celery ... -c` command in docker-compose.yml, not read by
    any Python code, so they are not declared as settings fields.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://scraper:change-me-locally@pgbouncer:6432/scraper"
    redis_url: str = "redis://redis:6379/0"

    cors_allow_origins: list[str] = ["http://localhost:3000"]

    scheduler_heartbeat_interval_seconds: int = 15

    # --- Phase 2 (doc 18) -------------------------------------------------
    # §4.1: the scheduler's real claim-and-dispatch poll loop, distinct
    # from the (unchanged) heartbeat above -- both run concurrently in the
    # scheduler process, see app/scheduler/main.py.
    scheduler_claim_interval_seconds: int = 10
    scheduler_claim_batch_size: int = 20

    # §1.1/doc 07 §5: the reconciliation sweep, reused directly. Doc 07 §5's
    # own shipped defaults ("every 2 minutes", "stuck past 10 minutes").
    reconciliation_interval_seconds: int = 120
    reconciliation_stuck_task_timeout_minutes: int = 10

    # §7.5: retention purge. HISTORY_RETENTION_DAYS' default of 90 is the
    # literal value amendment 6 specified; the run cadence (hourly) is a
    # judgment call doc 18 leaves to implementation -- distinct from the
    # ~10s claim-poll cadence since a DELETE sweep doesn't need to run
    # every poll tick.
    history_retention_days: int = 90
    retention_purge_interval_seconds: int = 3600

    # §7.3: the production deployment guard. Requires an explicit,
    # separately-named opt-in before the unauthenticated source/run/product
    # routers mount when environment=="production" -- see app/main.py.
    unauthenticated_source_api_ack: bool = False

    # --- Authentication ------------------------------------------------
    # JWT_SECRET_KEY must be supplied through the environment. The empty
    # default intentionally makes an incorrectly configured deployment fail
    # when auth is first used instead of silently signing tokens with a
    # predictable development secret.
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    # --- Phase 3 (doc 19) ---------------------------------------------
    # Task execution lease and per-domain politeness defaults. The worker
    # coordination layer consumes these values; declaring them here alone
    # does not change scraper behavior.
    task_execution_lease_seconds: int = 60
    domain_max_concurrency: int = 3
    domain_rate_limit_seconds: int = 2
    domain_cooldown_seconds: int = 600

    # Phase 1 (doc 17): Compose-internal address of the mock-store service
    # -- the fetcher/adapter run inside worker-http's container, which has
    # no localhost:4000, so this must stay a Compose service name, not a
    # host-facing URL.
    mock_store_base_url: str = "http://mock-store:4000"

    # doc 03 §3's SSRF hardening blocks any hostname resolving to a
    # private/loopback/link-local address. mock-store legitimately does
    # (it's a Docker-network-internal service standing in for "a target
    # site") -- this is the one, narrow, explicit exception, checked
    # before the private-IP rule (app/scraping/fetcher.py). Real internal
    # services (Postgres/Redis/etc.) are never added here.
    ssrf_allowed_hosts: list[str] = ["mock-store"]

    # doc 03 §4: "a clear, honest User-Agent identifying the bot and
    # linking to an info/contact page... we do not spoof a generic
    # desktop-browser UA to evade detection." example.com is IANA's
    # reserved documentation domain -- an intentionally-fake placeholder,
    # like DATABASE_URL's "change-me-locally" password. Real deployments
    # must override this via the SCRAPER_USER_AGENT env var with a real,
    # reachable info/contact URL.
    scraper_user_agent: str = "ScraperAutomationBot/0.1 (+https://example.com/bot)"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings singleton."""
    return Settings()
