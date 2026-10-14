"""Deterministic coverage for the manual retry delay policy."""

import pytest

from app.workers import retry_policy


@pytest.mark.parametrize(
    ("attempt_count", "ceiling"),
    [(1, 1.0), (2, 2.0), (3, 4.0), (6, 32.0), (7, 60.0), (1000, 60.0)],
)
def test_backoff_ceiling_grows_and_is_capped(
    monkeypatch: pytest.MonkeyPatch,
    attempt_count: int,
    ceiling: float,
) -> None:
    def upper_bound(low: float, high: float) -> float:
        assert low == 0.0
        assert high == ceiling
        return high

    monkeypatch.setattr(retry_policy.random, "uniform", upper_bound)

    assert retry_policy.calculate_retry_delay(
        attempt_count=attempt_count
    ) == ceiling


@pytest.mark.parametrize("fraction", [0.0, 0.5, 1.0])
def test_jitter_uses_the_selected_value(
    monkeypatch: pytest.MonkeyPatch,
    fraction: float,
) -> None:
    monkeypatch.setattr(
        retry_policy.random,
        "uniform",
        lambda low, high: low + fraction * (high - low),
    )

    assert retry_policy.calculate_retry_delay(
        attempt_count=3
    ) == 4.0 * fraction


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        (None, 2.0),
        (0.0, 2.0),
        (1.0, 2.0),
        (10.0, 10.0),
        (120.0, 120.0),
        (-1.0, 2.0),
        (float("nan"), 2.0),
        (float("inf"), 2.0),
        (float("-inf"), 2.0),
    ],
)
def test_retry_after_is_a_valid_server_floor(
    monkeypatch: pytest.MonkeyPatch,
    retry_after: float | None,
    expected: float,
) -> None:
    monkeypatch.setattr(retry_policy.random, "uniform", lambda low, high: 2.0)

    assert retry_policy.calculate_retry_delay(
        attempt_count=3,
        retry_after=retry_after,
    ) == expected


@pytest.mark.parametrize("attempt_count", [0, -1])
def test_nonpositive_attempt_count_is_rejected(attempt_count: int) -> None:
    with pytest.raises(ValueError, match="attempt_count must be positive"):
        retry_policy.calculate_retry_delay(attempt_count=attempt_count)
