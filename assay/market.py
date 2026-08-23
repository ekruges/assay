from __future__ import annotations

import math
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Any, Mapping

from .facts import (
    QuarterlyFact,
    ResolvedFact,
    concept_spec,
    resolve_consistent_fact_history,
    resolve_discrete_quarters,
    resolve_fact_history,
    resolve_quarterly_fact_history,
)
from .factors import ChsInputs, FactorResult


CHS_PHI = 2 ** (-1 / 3)


@dataclass(frozen=True)
class PriceBar:
    day: date
    close: float
    volume: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "date": self.day.isoformat(),
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class ChsRawResult:
    status: str
    inputs: ChsInputs | None
    accounting_period_end: str | None
    accounting_receipts: dict[str, tuple[ResolvedFact, ...]]
    market_receipts: dict[str, Any]
    reason: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": "CHS",
            "stage": "raw_inputs",
            "status": self.status,
            "inputs": vars(self.inputs) if self.inputs else None,
            "accounting_period_end": self.accounting_period_end,
            "accounting_receipts": {
                name: [fact.to_dict() for fact in facts]
                for name, facts in self.accounting_receipts.items()
            },
            "market_receipts": self.market_receipts,
            "reason": self.reason,
            "detail": self.detail,
            "winsorized": False,
            "imputed": False,
        }


def monthly_log_excess_returns(
    stock: list[PriceBar],
    benchmark: list[PriceBar],
    as_of: date,
    months: int = 12,
    *,
    include_partial_month: bool = False,
) -> list[float]:
    """Return newest-first monthly log stock returns minus benchmark returns."""
    stock_returns = _monthly_log_returns(stock, as_of, include_partial_month)
    benchmark_returns = _monthly_log_returns(benchmark, as_of, include_partial_month)
    latest = (
        (as_of.year, as_of.month)
        if include_partial_month
        else _previous_month((as_of.year, as_of.month))
    )
    required = [latest]
    for _ in range(months - 1):
        required.append(_previous_month(required[-1]))
    missing = [
        month
        for month in required
        if month not in stock_returns or month not in benchmark_returns
    ]
    if missing:
        labels = ", ".join(f"{year:04d}-{month:02d}" for year, month in missing)
        raise ValueError(f"missing required monthly returns: {labels}")
    return [stock_returns[month] - benchmark_returns[month] for month in required]


def annualized_three_month_sigma(bars: list[PriceBar], as_of: date) -> float:
    """Compute the CHS zero-centered daily volatility proxy."""
    cutoff = _subtract_months(as_of, 3)
    eligible = [bar for bar in bars if bar.day <= as_of]
    returns = [
        current.close / previous.close - 1
        for previous, current in zip(eligible, eligible[1:])
        if current.day > cutoff
    ]
    nonzero = sum(value != 0 for value in returns)
    if len(returns) < 2 or nonzero < 5:
        raise ValueError("need at least five nonzero daily returns over three months")
    return math.sqrt(252 * sum(value * value for value in returns) / (len(returns) - 1))


def nimta(net_income: float, market_equity: float, liabilities: float) -> float:
    denominator = _market_assets(market_equity, liabilities)
    return net_income / denominator


def weighted_nimta(nimta_quarters: list[float]) -> float:
    """Combine four newest-first quarterly NIMTA values, halving each quarter."""
    if len(nimta_quarters) < 4:
        raise ValueError(f"need four quarterly NIMTA values; found {len(nimta_quarters)}")
    if any(not math.isfinite(value) for value in nimta_quarters[:4]):
        raise ValueError("quarterly NIMTA values must be finite")
    normalization = (1 - CHS_PHI**3) / (1 - CHS_PHI**12)
    return normalization * sum(
        CHS_PHI ** (3 * offset) * value
        for offset, value in enumerate(nimta_quarters[:4])
    )


def weighted_excess_return(monthly_excess_returns: list[float]) -> float:
    """Combine 12 newest-first excess returns, halving the weight each quarter."""
    if len(monthly_excess_returns) < 12:
        raise ValueError(
            f"need 12 monthly excess returns; found {len(monthly_excess_returns)}"
        )
    if any(not math.isfinite(value) for value in monthly_excess_returns[:12]):
        raise ValueError("monthly excess returns must be finite")
    normalization = (1 - CHS_PHI) / (1 - CHS_PHI**12)
    return normalization * sum(
        CHS_PHI**offset * value
        for offset, value in enumerate(monthly_excess_returns[:12])
    )


def build_raw_chs_inputs(
    *,
    nimta_quarters: list[float],
    liabilities: float,
    cash_and_short_term_investments: float,
    market_equity: float,
    book_equity: float,
    sp500_market_value: float,
    monthly_excess_returns: list[float],
    sigma: float,
    share_price: float,
) -> ChsInputs:
    """Build pre-winsorization CHS inputs from source-level measurements."""
    measurements = {
        "liabilities": liabilities,
        "cash_and_short_term_investments": cash_and_short_term_investments,
        "market_equity": market_equity,
        "book_equity": book_equity,
        "sp500_market_value": sp500_market_value,
        "sigma": sigma,
        "share_price": share_price,
    }
    invalid = [name for name, value in measurements.items() if not math.isfinite(value)]
    if invalid:
        raise ValueError(f"CHS source measurements must be finite: {', '.join(invalid)}")
    market_assets = _market_assets(market_equity, liabilities)
    if sp500_market_value <= 0:
        raise ValueError("S&P 500 market value must be positive")
    if share_price <= 0:
        raise ValueError("share price must be positive")
    if liabilities < 0 or cash_and_short_term_investments < 0 or sigma < 0:
        raise ValueError("liabilities, cash, and sigma must be nonnegative")

    adjusted_book_equity = book_equity + 0.1 * (market_equity - book_equity)
    adjusted_book_equity = max(adjusted_book_equity, 1.0)
    return ChsInputs(
        nimtaavg=weighted_nimta(nimta_quarters),
        tlmta=liabilities / market_assets,
        exretavg=weighted_excess_return(monthly_excess_returns),
        sigma=sigma,
        rsize=math.log(market_equity / sp500_market_value),
        cashmta=cash_and_short_term_investments / market_assets,
        mb=market_equity / adjusted_book_equity,
        price=math.log(min(share_price, 15.0)),
    )


def winsorize_chs_inputs(
    inputs: ChsInputs, bounds: Mapping[str, tuple[float, float]]
) -> ChsInputs:
    """Apply explicit pooled 5/95 bounds produced by a universe run."""
    values: dict[str, float] = {}
    for name, value in vars(inputs).items():
        try:
            lower, upper = bounds[name]
        except KeyError as exc:
            raise ValueError(f"missing winsorization bounds for {name}") from exc
        if not math.isfinite(lower) or not math.isfinite(upper) or not lower <= upper:
            raise ValueError(f"invalid winsorization bounds for {name}")
        values[name] = min(max(value, lower), upper)
    return ChsInputs(**values)


def median_dollar_adv(
    bars: list[PriceBar],
    as_of: date,
    *,
    trading_days: int = 63,
    minimum_days: int = 20,
) -> float:
    """Median close times volume over the latest completed trading days."""
    eligible = [
        bar
        for bar in bars
        if bar.day <= as_of and bar.volume is not None and bar.volume >= 0
    ][-trading_days:]
    if len(eligible) < minimum_days:
        raise ValueError(
            f"need at least {minimum_days} price-volume observations; found {len(eligible)}"
        )
    return float(median(bar.close * bar.volume for bar in eligible))


def momentum_12_1(bars: list[PriceBar], as_of: date) -> float:
    """Prior 12-to-2 month return, excluding the most recent complete month."""
    returns = _monthly_log_returns(bars, as_of, include_partial_month=False)
    skipped = _previous_month((as_of.year, as_of.month))
    month = _previous_month(skipped)
    required: list[tuple[int, int]] = []
    for _ in range(11):
        required.append(month)
        month = _previous_month(month)
    missing = [item for item in required if item not in returns]
    if missing:
        labels = ", ".join(f"{year:04d}-{number:02d}" for year, number in missing)
        raise ValueError(f"missing required 12-1 monthly returns: {labels}")
    return math.exp(sum(returns[item] for item in required)) - 1


def distance_from_52_week_high(
    bars: list[PriceBar], as_of: date, *, trading_days: int = 252
) -> float:
    eligible = [bar for bar in bars if bar.day <= as_of][-trading_days:]
    if len(eligible) < 20:
        raise ValueError(f"need at least 20 price observations; found {len(eligible)}")
    high = max(bar.close for bar in eligible)
    return eligible[-1].close / high - 1


def sector_relative_strength(
    stock: list[PriceBar], sector: list[PriceBar], as_of: date
) -> float:
    stock_return = momentum_12_1(stock, as_of)
    sector_return = momentum_12_1(sector, as_of)
    return math.log1p(stock_return) - math.log1p(sector_return)


def split_adjustment_factor(
    raw_bars: list[PriceBar], split_adjusted_bars: list[PriceBar], period_end: date
) -> float:
    raw = _latest_on_or_before(raw_bars, period_end)
    adjusted = _latest_on_or_before(split_adjusted_bars, period_end)
    if raw is None or adjusted is None or raw.day != adjusted.day:
        raise ValueError(f"raw and split-adjusted prices do not align at {period_end}")
    factor = raw.close / adjusted.close
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError(f"invalid split adjustment factor at {period_end}")
    return factor


def compute_net_share_issuance(
    companyfacts: dict[str, object],
    as_of: date,
    raw_bars: list[PriceBar],
    split_adjusted_bars: list[PriceBar],
) -> FactorResult:
    """Compute year-over-year issuance after putting both share counts on one basis."""
    name = "net_share_issuance"
    shares = resolve_consistent_fact_history(
        companyfacts, concept_spec("shares_outstanding"), as_of, limit=2
    )
    if len(shares) < 2:
        return FactorResult(
            name,
            None,
            "growth_rate",
            "lower",
            shares[0].end if shares else None,
            tuple(shares),
            "insufficient_history",
            f"two annual share counts are required; found {len(shares)}",
        )
    current, prior = shares
    gap = (date.fromisoformat(current.end) - date.fromisoformat(prior.end)).days
    if not 300 <= gap <= 430:
        return FactorResult(
            name,
            None,
            "growth_rate",
            "lower",
            current.end,
            (current, prior),
            "period_mismatch",
            f"prior share count is {gap} days earlier",
        )
    if current.unit != prior.unit:
        return FactorResult(
            name,
            None,
            "growth_rate",
            "lower",
            current.end,
            (current, prior),
            "unit_mismatch",
            f"share counts use {current.unit} and {prior.unit}",
        )
    try:
        current_factor = split_adjustment_factor(
            raw_bars, split_adjusted_bars, date.fromisoformat(current.end)
        )
        prior_factor = split_adjustment_factor(
            raw_bars, split_adjusted_bars, date.fromisoformat(prior.end)
        )
    except ValueError as exc:
        return FactorResult(
            name,
            None,
            "growth_rate",
            "lower",
            current.end,
            (current, prior),
            "missing_input",
            str(exc),
        )
    adjusted_current = current.value * current_factor
    adjusted_prior = prior.value * prior_factor
    if adjusted_prior <= 0:
        return FactorResult(
            name,
            None,
            "growth_rate",
            "lower",
            current.end,
            (current, prior),
            "non_positive_denominator",
            "split-adjusted prior shares must be positive",
        )
    growth = adjusted_current / adjusted_prior - 1
    return FactorResult(
        name,
        float(growth),
        "growth_rate",
        "lower",
        current.end,
        (current, prior),
        detail=(
            f"provider split factors current={current_factor:.8g}, "
            f"prior={prior_factor:.8g}"
        ),
    )


def split_adjusted_market_equity(
    shares: ResolvedFact,
    raw_bars: list[PriceBar],
    split_adjusted_bars: list[PriceBar],
    as_of: date,
) -> tuple[float, PriceBar, float, float]:
    if shares.unit != "shares" or shares.value <= 0:
        raise ValueError("a positive share-count fact is required")
    current_raw = _latest_on_or_before(raw_bars, as_of)
    current_split = _latest_on_or_before(split_adjusted_bars, as_of)
    if current_raw is None or current_split is None or current_raw.day != current_split.day:
        raise ValueError("current raw and split-adjusted prices are required")
    statement_factor = split_adjustment_factor(
        raw_bars, split_adjusted_bars, date.fromisoformat(shares.end)
    )
    current_factor = split_adjustment_factor(
        raw_bars, split_adjusted_bars, current_raw.day
    )
    current_share_basis = shares.value * statement_factor / current_factor
    return (
        float(current_share_basis * current_raw.close),
        current_raw,
        statement_factor,
        current_factor,
    )


def build_company_chs_raw(
    companyfacts: dict[str, Any],
    as_of: date,
    raw_bars: list[PriceBar],
    split_adjusted_bars: list[PriceBar],
    benchmark_bars: list[PriceBar],
    *,
    sp500_market_value: float,
    return_bars: list[PriceBar] | None = None,
) -> ChsRawResult:
    """Assemble all eight CHS inputs without cross-sectional winsorization."""
    net_income = resolve_discrete_quarters(
        companyfacts, concept_spec("net_income"), as_of, limit=4
    )
    if len(net_income) < 4 or not _consecutive_quarter_facts(net_income):
        return _chs_failure(
            "insufficient_history",
            f"four consecutive discrete net-income quarters are required; found {len(net_income)}",
            net_income[0].end if net_income else None,
            {"net_income": _quarter_receipts(net_income)},
        )
    period_end = net_income[0].end
    histories = {
        name: resolve_quarterly_fact_history(
            companyfacts, concept_spec(name), as_of
        )
        for name in (
            "liabilities",
            "cash",
            "short_term_investments",
            "cash_and_short_term_investments",
            "book_equity",
            "shares_outstanding",
        )
    }
    receipts: dict[str, tuple[ResolvedFact, ...]] = {
        "net_income": _quarter_receipts(net_income)
    }
    nimta_quarters: list[float] = []
    nimta_market_inputs: list[dict[str, Any]] = []
    for quarter in net_income:
        liabilities = _at_end(histories["liabilities"], quarter.end)
        shares = _cover_share_for_period(
            histories["shares_outstanding"], quarter.end
        )
        if liabilities is None or shares is None:
            return _chs_failure(
                "missing_input",
                f"liabilities and a nearby cover-page share count are required at {quarter.end}",
                period_end,
                receipts,
            )
        if quarter.unit != liabilities.unit or shares.unit != "shares":
            return _chs_failure(
                "unit_mismatch",
                f"CHS quarterly inputs have incompatible units at {quarter.end}",
                period_end,
                receipts,
            )
        price = _latest_on_or_before(raw_bars, date.fromisoformat(quarter.end))
        if price is None:
            return _chs_failure(
                "missing_input",
                f"market price is missing at {quarter.end}",
                period_end,
                receipts,
            )
        try:
            cover_factor = split_adjustment_factor(
                raw_bars, split_adjusted_bars, date.fromisoformat(shares.end)
            )
            period_factor = split_adjustment_factor(
                raw_bars, split_adjusted_bars, price.day
            )
        except ValueError as exc:
            return _chs_failure(
                "invalid_market_input", str(exc), period_end, receipts
            )
        adjusted_shares = shares.value * cover_factor / period_factor
        market_equity = adjusted_shares * price.close
        try:
            nimta_quarters.append(
                nimta(float(quarter.value), market_equity, float(liabilities.value))
            )
        except ValueError as exc:
            return _chs_failure(
                "non_positive_denominator", str(exc), period_end, receipts
            )
        receipts.setdefault("liabilities", tuple())
        receipts["liabilities"] += (liabilities,)
        receipts.setdefault("shares_outstanding", tuple())
        receipts["shares_outstanding"] += (shares,)
        nimta_market_inputs.append(
            {
                "period_end": quarter.end,
                "price": price.to_dict(),
                "share_fact_end": shares.end,
                "cover_split_factor": cover_factor,
                "period_split_factor": period_factor,
                "split_adjusted_shares": adjusted_shares,
                "market_equity": market_equity,
            }
        )

    liabilities = _at_end(histories["liabilities"], period_end)
    book_equity = _at_end(histories["book_equity"], period_end)
    shares = _cover_share_for_period(histories["shares_outstanding"], period_end)
    cash_value, cash_receipts = _cash_and_investments(histories, period_end)
    if liabilities is None or book_equity is None or shares is None or cash_value is None:
        return _chs_failure(
            "missing_input",
            "latest liabilities, cash plus short-term investments, book equity, and shares are required",
            period_end,
            receipts,
        )
    receipts["book_equity"] = (book_equity,)
    receipts["cash_and_short_term_investments"] = cash_receipts
    financial_facts = (liabilities, book_equity, *cash_receipts)
    if any(fact.unit != liabilities.unit for fact in financial_facts):
        return _chs_failure(
            "unit_mismatch",
            "latest CHS accounting values use multiple currencies",
            period_end,
            receipts,
        )

    try:
        returns_source = return_bars if return_bars is not None else split_adjusted_bars
        market_equity, current_raw, statement_factor, current_factor = (
            split_adjusted_market_equity(
                shares, raw_bars, split_adjusted_bars, as_of
            )
        )
        excess_returns = monthly_log_excess_returns(
            returns_source, benchmark_bars, as_of, months=12
        )
        sigma = annualized_three_month_sigma(returns_source, as_of)
        inputs = build_raw_chs_inputs(
            nimta_quarters=nimta_quarters,
            liabilities=float(liabilities.value),
            cash_and_short_term_investments=float(cash_value),
            market_equity=float(market_equity),
            book_equity=float(book_equity.value),
            sp500_market_value=sp500_market_value,
            monthly_excess_returns=excess_returns,
            sigma=sigma,
            share_price=current_raw.close,
        )
    except ValueError as exc:
        return _chs_failure("invalid_market_input", str(exc), period_end, receipts)

    return ChsRawResult(
        "resolved",
        inputs,
        period_end,
        receipts,
        {
            "price_as_of": current_raw.to_dict(),
            "benchmark_price_as_of": (
                _latest_on_or_before(benchmark_bars, as_of).to_dict()
                if _latest_on_or_before(benchmark_bars, as_of)
                else None
            ),
            "sp500_market_value": sp500_market_value,
            "statement_split_factor": statement_factor,
            "current_split_factor": current_factor,
            "monthly_excess_returns": excess_returns,
            "sigma": sigma,
            "nimta_quarters": nimta_market_inputs,
        },
    )


def pooled_chs_bounds(
    inputs: list[ChsInputs], *, lower: float = 0.05, upper: float = 0.95
) -> dict[str, tuple[float, float]]:
    if not inputs:
        raise ValueError("at least one CHS input row is required")
    if not 0 <= lower <= upper <= 1:
        raise ValueError("winsorization quantiles must satisfy 0 <= lower <= upper <= 1")
    return {
        name: (
            _quantile(sorted(getattr(item, name) for item in inputs), lower),
            _quantile(sorted(getattr(item, name) for item in inputs), upper),
        )
        for name in vars(inputs[0])
    }


def _market_assets(market_equity: float, liabilities: float) -> float:
    denominator = market_equity + liabilities
    if market_equity <= 0 or denominator <= 0:
        raise ValueError("market equity and market-valued assets must be positive")
    return denominator


def _latest_on_or_before(bars: list[PriceBar], day: date) -> PriceBar | None:
    eligible = [bar for bar in bars if bar.day <= day]
    return max(eligible, key=lambda bar: bar.day) if eligible else None


def _at_end(history: list[ResolvedFact], end: str) -> ResolvedFact | None:
    return next((fact for fact in history if fact.end == end), None)


def _cover_share_for_period(
    history: list[ResolvedFact], period_end: str, *, max_days_after: int = 60
) -> ResolvedFact | None:
    """Match a cover-page share count without treating its cover date as a period."""
    exact = _at_end(history, period_end)
    if exact is not None:
        return exact
    target = date.fromisoformat(period_end)
    candidates = [
        fact
        for fact in history
        if 0 < (date.fromisoformat(fact.end) - target).days <= max_days_after
    ]
    return min(candidates, key=lambda fact: fact.end) if candidates else None


def _cash_and_investments(
    histories: dict[str, list[ResolvedFact]], end: str
) -> tuple[float | None, tuple[ResolvedFact, ...]]:
    combined = _at_end(histories["cash_and_short_term_investments"], end)
    if combined is not None:
        return float(combined.value), (combined,)
    cash = _at_end(histories["cash"], end)
    investments = _at_end(histories["short_term_investments"], end)
    if cash is None or investments is None or cash.unit != investments.unit:
        return None, tuple(fact for fact in (cash, investments) if fact)
    return float(cash.value + investments.value), (cash, investments)


def _quarter_receipts(quarters: list[QuarterlyFact]) -> tuple[ResolvedFact, ...]:
    return tuple(fact for quarter in quarters for fact in quarter.inputs)


def _consecutive_quarter_facts(quarters: list[QuarterlyFact]) -> bool:
    return all(
        70
        <= (date.fromisoformat(newer.end) - date.fromisoformat(older.end)).days
        <= 110
        for newer, older in zip(quarters, quarters[1:])
    )


def _chs_failure(
    reason: str,
    detail: str,
    period_end: str | None,
    receipts: dict[str, tuple[ResolvedFact, ...]],
) -> ChsRawResult:
    return ChsRawResult(
        "unresolved", None, period_end, receipts, {}, reason, detail
    )


def _quantile(values: list[float], probability: float) -> float:
    if len(values) == 1:
        return float(values[0])
    position = probability * (len(values) - 1)
    left = math.floor(position)
    right = math.ceil(position)
    if left == right:
        return float(values[left])
    if values[left] == values[right]:
        return float(values[left])
    weight = position - left
    return float(values[left] * (1 - weight) + values[right] * weight)


def _monthly_log_returns(
    bars: list[PriceBar], as_of: date, include_partial_month: bool
) -> dict[tuple[int, int], float]:
    month_ends: dict[tuple[int, int], PriceBar] = {}
    for bar in bars:
        month = (bar.day.year, bar.day.month)
        if bar.day > as_of or (
            not include_partial_month and month >= (as_of.year, as_of.month)
        ):
            continue
        previous = month_ends.get(month)
        if previous is None or bar.day > previous.day:
            month_ends[month] = bar

    returns: dict[tuple[int, int], float] = {}
    for month, current in month_ends.items():
        previous = month_ends.get(_previous_month(month))
        if previous:
            returns[month] = math.log(current.close / previous.close)
    return returns


def _previous_month(month: tuple[int, int]) -> tuple[int, int]:
    year, number = month
    return (year - 1, 12) if number == 1 else (year, number - 1)


def _subtract_months(day: date, count: int) -> date:
    month_index = day.year * 12 + day.month - 1 - count
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))
