"""Structured JSON logging configuration.

Every process (API, scheduler, workers) calls configure_logging() once at
startup so that log output is uniformly structured JSON, suitable for
collection by any log aggregator later without code changes.
"""

import logging
import sys

from pythonjsonlogger import jsonlogger

from app.core.config import get_settings

_CONFIGURED = False


def configure_logging() -> None:
    """Configure the root logger for structured JSON output.

    Safe to call multiple times; only the first call has an effect.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()

    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger, ensuring logging is configured first."""
    configure_logging()
    return logging.getLogger(name)
