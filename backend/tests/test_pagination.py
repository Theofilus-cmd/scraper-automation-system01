"""Pure unit tests for app/api/v1/pagination.py -- doc 06 §1's cursor
convention. No DB, no network; every list endpoint in app/api/v1/ builds
on these functions, so their correctness is worth pinning down directly.
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.api.v1.pagination import (
    MAX_LIMIT,
    clamp_limit,
    decode_cursor,
    decode_cursor_or_422,
    encode_cursor,
    paginated_response,
)
from app.core.errors import ApiError


def test_decode_cursor_none_is_none() -> None:
    assert decode_cursor(None) is None


def test_encode_decode_cursor_round_trips() -> None:
    created_at = datetime(2026, 8, 25, 12, 30, 0, tzinfo=UTC)
    id_ = uuid.uuid4()

    cursor = encode_cursor(created_at, id_)
    decoded = decode_cursor(cursor)

    assert decoded == (created_at, id_)


def test_decode_cursor_rejects_malformed_input() -> None:
    with pytest.raises(ValueError):
        decode_cursor("not-valid-base64-or-shape")


def test_decode_cursor_or_422_passes_through_valid_cursor() -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    id_ = uuid.uuid4()
    cursor = encode_cursor(created_at, id_)

    assert decode_cursor_or_422(cursor) == (created_at, id_)


def test_decode_cursor_or_422_raises_api_error_on_malformed_input() -> None:
    with pytest.raises(ApiError) as exc_info:
        decode_cursor_or_422("garbage")

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "INVALID_CURSOR"


@pytest.mark.parametrize(
    ("raw_limit", "expected"),
    [
        (-5, 1),
        (0, 1),
        (1, 1),
        (25, 25),
        (MAX_LIMIT, MAX_LIMIT),
        (MAX_LIMIT + 1, MAX_LIMIT),
        (10_000, MAX_LIMIT),
    ],
)
def test_clamp_limit_bounds(raw_limit: int, expected: int) -> None:
    assert clamp_limit(raw_limit) == expected


def test_paginated_response_reports_no_next_page() -> None:
    rows = [(datetime(2026, 1, 1, tzinfo=UTC), uuid.uuid4(), "a")]

    result = paginated_response(
        rows, limit=25, cursor_of=lambda r: (r[0], r[1]), serialize=lambda r: r[2]
    )

    assert result["data"] == ["a"]
    assert result["pagination"] == {"next_cursor": None, "has_more": False}


def test_paginated_response_reports_next_page_from_the_fetch_one_extra_row_trick() -> None:
    """`rows` here has limit+1=3 rows (the caller's own contract, per
    paginated_response's docstring) -- the 3rd is the lookahead row that
    signals a next page and is never included in `data` itself.
    """
    rows = [
        (datetime(2026, 1, 3, tzinfo=UTC), uuid.uuid4(), "newest"),
        (datetime(2026, 1, 2, tzinfo=UTC), uuid.uuid4(), "middle"),
        (datetime(2026, 1, 1, tzinfo=UTC), uuid.uuid4(), "lookahead"),
    ]

    result = paginated_response(
        rows, limit=2, cursor_of=lambda r: (r[0], r[1]), serialize=lambda r: r[2]
    )

    assert result["data"] == ["newest", "middle"]
    assert result["pagination"]["has_more"] is True
    assert result["pagination"]["next_cursor"] == encode_cursor(rows[1][0], rows[1][1])


def test_paginated_response_empty_rows() -> None:
    result = paginated_response(
        [], limit=25, cursor_of=lambda r: (r[0], r[1]), serialize=lambda r: r
    )
    assert result == {"data": [], "pagination": {"next_cursor": None, "has_more": False}}
