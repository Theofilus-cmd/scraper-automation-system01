from datetime import UTC, datetime

import pytest

from app.scraping.fetcher import _parse_retry_after_seconds

NOW = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("header_value", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("0", 0.0),
        ("120", 120.0),
        (" 2 ", 2.0),
        ("2.5", 2.5),
        ("-1", None),
        ("nan", None),
        ("inf", None),
        ("-inf", None),
        ("1e309", None),
        ("not-a-date", None),
        ("Wed, 21 Oct 2026 25:28:00 GMT", None),
        ("Wed, 21 Oct 2026 07:30:00 GMT", 120.0),
        ("Wed, 21 Oct 2026 07:28:00 GMT", 0.0),
        ("Wed, 21 Oct 2026 07:27:00 GMT", 0.0),
        ("Wed, 21 Oct 2026 09:30:00 +0200", 120.0),
        ("Wednesday, 21-Oct-26 07:30:00 GMT", 120.0),
        ("Wed Oct 21 07:30:00 2026", 120.0),
    ],
)
def test_parse_retry_after_seconds(
    header_value: str | None, expected: float | None
) -> None:
    assert _parse_retry_after_seconds(header_value, now=NOW) == expected
