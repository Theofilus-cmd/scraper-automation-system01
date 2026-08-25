"""Structured JSON logging configuration.

Every process (API, scheduler, workers) calls configure_logging() once at
startup so that log output is uniformly structured JSON, suitable for
collection by any log aggregator later without code changes.

Acceptance-review fix (real mypy errors, real Docker/Postgres run):
`pythonjsonlogger.jsonlogger.JsonFormatter.__init__` resolves as an
untyped call under mypy in this project's pinned version, so calling it
directly from this fully-typed module (`disallow_untyped_calls`, part of
`strict = true`) reported `no-untyped-call`. `_typed_json_formatter()`
below is the one, single, narrow place that real, untyped call happens;
its own outer signature is fully explicit (`str -> logging.Formatter`),
so mypy verifies every real call to it (there is exactly one, just
below) without needing to relax `disallow_untyped_calls` project-wide.
The `cast()` documents a real, true fact -- `JsonFormatter` genuinely
*is* a `logging.Formatter` subclass at runtime -- rather than papering
over anything; it is what keeps this function's own `-> logging.
Formatter` honest regardless of exactly how precisely the untyped
constructor call's result resolves under a given mypy/stub combination
(`warn_return_any`, also part of `strict = true`). At runtime this still
constructs and returns the exact same `JsonFormatter` instance,
unchanged.
"""

import logging
import sys
from typing import cast

from pythonjsonlogger import jsonlogger

from app.core.config import get_settings

_CONFIGURED = False


def _typed_json_formatter(fmt: str) -> logging.Formatter:
    """See module docstring for the full "why" of the cast and ignore
    below.
    """
    return cast(logging.Formatter, jsonlogger.JsonFormatter(fmt))  # type: ignore[no-untyped-call]


def configure_logging() -> None:
    """Configure the root logger for structured JSON output.

    Safe to call multiple times; only the first call has an effect.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()

    handler = logging.StreamHandler(sys.stdout)
    formatter = _typed_json_formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger, ensuring logging is configured first."""
    configure_logging()
    return logging.getLogger(name)
