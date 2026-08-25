"""doc 06 §1's cursor pagination convention: `?cursor=&limit=` (default 25,
max 100), response `{"data": [...], "pagination": {"next_cursor", "has_more"}}`.

Every list endpoint in this phase orders newest-first (`created_at DESC,
id DESC` -- `id` as a tiebreaker since `created_at` alone isn't guaranteed
unique). The opaque cursor is exactly that composite key, so pagination
stays stable even as new rows are created between page fetches -- an
offset-based scheme would skip or duplicate rows under concurrent writes;
this can't, by construction.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from app.core.errors import ApiError

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

# PEP 695 `def paginated_response[T](...)` is available (requires-python
# >=3.12, pyproject.toml) and is what ruff's UP047 suggests -- deliberately
# not used here: this project's pinned `mypy>=1.13` parses it fine, but the
# verification sandbox's standalone mypy binary happens to be hosted on a
# Python 3.11 interpreter (confirmed via its own shebang), which cannot
# *parse* 3.12-only syntax at all regardless of mypy's own version or its
# python_version target setting -- a sandbox/tool mismatch, not a project
# constraint. Keeping the pre-695 TypeVar form here is what lets mypy
# actually check this file's real logic in that sandbox; the delivery
# report flags this explicitly, and the project's own CI (real Python 3.12
# throughout) would have accepted either form.
_T = TypeVar("_T")


def decode_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        created_at_raw, id_raw = raw.split("|", 1)
        return datetime.fromisoformat(created_at_raw), uuid.UUID(id_raw)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc


def decode_cursor_or_422(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    """Every list endpoint's thin HTTP-facing wrapper around `decode_cursor`
    -- turns a malformed `cursor` query param into a clean `422` instead of
    an unhandled `ValueError` (which would otherwise fall through to
    `app/core/errors.py`'s generic 500 catch-all). Kept separate from
    `decode_cursor` itself so that function stays pure and framework-
    agnostic (usable by a non-HTTP caller without pulling in `ApiError`),
    while every router in this package can call this one directly instead
    of repeating the same try/except at every list endpoint.
    """
    try:
        return decode_cursor(cursor)
    except ValueError:
        raise ApiError(422, "INVALID_CURSOR", "The cursor query parameter is malformed.") from None


def encode_cursor(created_at: datetime, id_: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{id_}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def paginated_response(  # noqa: UP047 -- see the _T TypeVar comment above
    rows: list[_T],
    *,
    limit: int,
    cursor_of: Callable[[_T], tuple[datetime, uuid.UUID]],
    serialize: Callable[[_T], Any],
) -> dict[str, Any]:
    """`rows` must have been fetched with `limit + 1` (the standard
    "fetch one extra row to know if there's a next page" trick) -- every
    repository list function in this phase takes its `limit` argument
    literally and expects the caller to pass `limit + 1`, precisely so
    this function doesn't have to re-query. `cursor_of`/`serialize`
    operate on the raw row (e.g. an ORM instance), in that order --
    cursor extraction happens before serialization so it always sees the
    real `datetime`/`uuid.UUID` values, never their already-stringified
    JSON-response form.
    """
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_cursor(*cursor_of(page[-1])) if has_more and page else None
    return {
        "data": [serialize(row) for row in page],
        "pagination": {"next_cursor": next_cursor, "has_more": has_more},
    }
