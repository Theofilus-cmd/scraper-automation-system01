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


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings singleton."""
    return Settings()
