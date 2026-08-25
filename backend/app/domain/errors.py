"""Domain-level exceptions for Phase 2's source/schedule/run lifecycle.

Pure, framework-agnostic (doc 04 §2's `domain/` boundary rule, reused
directly per doc 18 §1.1) -- no FastAPI import here. Repository functions
under `app/db/models/` raise these; `app/api/v1/*.py` is the only place
that ever catches one and translates it into an `ApiError` (doc 18 §6.4's
error-code table). Keeping the translation in exactly one layer means the
error-code mapping lives in one place, not scattered across every route
that can hit a given failure.
"""

from __future__ import annotations

import uuid


class DomainError(Exception):
    """Base class for every exception in this module."""


class SourceNotFoundError(DomainError):
    def __init__(self, source_id: uuid.UUID) -> None:
        self.source_id = source_id
        super().__init__(f"source not found: {source_id}")


class SourceArchivedError(DomainError):
    """Raised for any operation doc 18 §3.1/§6.1 blocks while a source is
    archived (PATCH, schedule upsert, manual/legacy run trigger)."""

    def __init__(self, source_id: uuid.UUID) -> None:
        self.source_id = source_id
        super().__init__(f"source is archived: {source_id}")


class SourceNotArchivedError(DomainError):
    """Raised by unarchive when the source isn't currently archived (doc 18
    §6.1's `409 SOURCE_NOT_ARCHIVED`)."""

    def __init__(self, source_id: uuid.UUID) -> None:
        self.source_id = source_id
        super().__init__(f"source is not archived: {source_id}")


class ScheduleNotFoundError(DomainError):
    def __init__(self, source_id: uuid.UUID) -> None:
        self.source_id = source_id
        super().__init__(f"no schedule for source: {source_id}")


class ScheduleIntervalTooShortError(DomainError):
    def __init__(self, interval_minutes: int, minimum: int) -> None:
        self.interval_minutes = interval_minutes
        self.minimum = minimum
        super().__init__(f"interval_minutes {interval_minutes} is below the minimum {minimum}")


class ScheduleIntervalTooLongError(DomainError):
    def __init__(self, interval_minutes: int, maximum: int) -> None:
        self.interval_minutes = interval_minutes
        self.maximum = maximum
        super().__init__(f"interval_minutes {interval_minutes} exceeds the maximum {maximum}")


class RunNotFoundError(DomainError):
    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        super().__init__(f"run not found: {run_id}")


class RunInProgressError(DomainError):
    """doc 18 §4.4/§6.2's `409 SOURCE_RUN_IN_PROGRESS` -- carries the
    existing in-flight run's id so the caller can be pointed at it directly."""

    def __init__(self, source_id: uuid.UUID, existing_run_id: uuid.UUID) -> None:
        self.source_id = source_id
        self.existing_run_id = existing_run_id
        super().__init__(f"source {source_id} already has an in-flight run: {existing_run_id}")


class ProductNotFoundError(DomainError):
    def __init__(self, product_id: uuid.UUID) -> None:
        self.product_id = product_id
        super().__init__(f"product not found: {product_id}")


class UnsupportedAdapterError(DomainError):
    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"no adapter matched: {url}")


class SsrfBlockedError(DomainError):
    """Raised by the create-time SSRF check (doc 18 §7.1) -- wraps
    `app.scraping.fetcher.FetchError` so `app/domain/` and
    `app/db/models/sources_repository.py` don't need to import from
    `app/scraping/` just to catch its exception type; the API layer maps
    this to `422 DNS_OR_SSRF_BLOCKED`.
    """

    def __init__(self, url: str, message: str) -> None:
        self.url = url
        super().__init__(message)
