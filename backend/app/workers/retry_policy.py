"""Explicit retry delays for the manual HTTP task retry handler."""

from __future__ import annotations

import math
import random

_MAX_BACKOFF_SECONDS = 60.0


def calculate_retry_delay(
    *,
    attempt_count: int,
    retry_after: float | None = None,
) -> float:
    """Use full jitter with a capped ceiling and a server-provided floor."""
    if attempt_count < 1:
        raise ValueError("attempt_count must be positive")

    exponent = min(attempt_count - 1, 6)
    ceiling = min(_MAX_BACKOFF_SECONDS, float(2 ** exponent))
    delay = random.uniform(0.0, ceiling)

    if retry_after is not None and math.isfinite(retry_after) and retry_after >= 0:
        delay = max(delay, retry_after)

    return delay
