"""Pure price-intelligence calculations (Phase 4). No I/O, no framework."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from statistics import median

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class Competitor:
    """One matched competitor listing, as observed."""

    match_id: str
    price: Decimal | None
    currency: str | None
    is_valid: bool = True


@dataclass(frozen=True)
class CompetitorComparison:
    match_id: str
    price: Decimal
    price_gap: Decimal
    price_gap_pct: Decimal | None
    map_violation: bool
    map_gap: Decimal | None  # map_price - competitor price when violating


@dataclass(frozen=True)
class Excluded:
    match_id: str
    reason: str


@dataclass
class IntelligenceResult:
    comparisons: list[CompetitorComparison] = field(default_factory=list)
    excluded: list[Excluded] = field(default_factory=list)
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    avg_price: Decimal | None = None
    median_price: Decimal | None = None
    price_position: str | None = None
    own_price_below_map: bool = False

    @property
    def map_violation_count(self) -> int:
        return sum(1 for c in self.comparisons if c.map_violation)


def _q(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def price_position(own_price: Decimal, prices: list[Decimal]) -> str | None:
    """`lowest` / `average` / `highest` of own price against competitors.

    Ties: own == min == max -> `average`; own == min -> `lowest`;
    own == max -> `highest`.
    """
    if not prices:
        return None
    low, high = min(prices), max(prices)
    if own_price == low == high:
        return "average"
    if own_price <= low:
        return "lowest"
    if own_price >= high:
        return "highest"
    return "average"


def compute_intelligence(
    *,
    own_price: Decimal,
    map_price: Decimal | None,
    currency: str,
    competitors: list[Competitor],
) -> IntelligenceResult:
    """Compare an internal product against matched competitors.

    Only valid observations in the same currency are compared (no FX).
    `price_gap = own - competitor` (positive: we are pricier);
    `price_gap_pct` is relative to the competitor price (None if it is 0).
    A competitor price strictly below `map_price` is a MAP violation.
    """
    result = IntelligenceResult()
    result.own_price_below_map = map_price is not None and own_price < map_price

    prices: list[Decimal] = []
    for comp in competitors:
        if comp.price is None or not comp.is_valid:
            result.excluded.append(Excluded(comp.match_id, "no_valid_observation"))
            continue
        if comp.currency is None or comp.currency.upper() != currency.upper():
            result.excluded.append(Excluded(comp.match_id, "currency_mismatch"))
            continue
        gap = own_price - comp.price
        pct = _q(gap / comp.price * 100) if comp.price != 0 else None
        violation = map_price is not None and comp.price < map_price
        result.comparisons.append(
            CompetitorComparison(
                match_id=comp.match_id,
                price=comp.price,
                price_gap=_q(gap),
                price_gap_pct=pct,
                map_violation=violation,
                map_gap=_q(map_price - comp.price) if violation and map_price is not None else None,
            )
        )
        prices.append(comp.price)

    if prices:
        result.min_price = min(prices)
        result.max_price = max(prices)
        result.avg_price = _q(sum(prices, Decimal(0)) / len(prices))
        result.median_price = _q(Decimal(median(prices)))
        result.price_position = price_position(own_price, prices)
    return result
