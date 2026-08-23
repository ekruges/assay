from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from .facts import (
    ResolvedFact,
    concept_spec,
    resolve_consistent_fact_history,
    resolve_fact_history,
    resolve_quarterly_fact_history,
)
from .market import (
    PriceBar,
    distance_from_52_week_high,
    momentum_12_1,
    sector_relative_strength,
    split_adjusted_market_equity,
)


DENOMINATOR_FLOOR = 100_000


@dataclass(frozen=True)
class PanelMetric:
    name: str
    value: float | None
    unit: str
    period_end: str | None
    inputs: tuple[ResolvedFact, ...] = ()
    market_inputs: dict[str, Any] | None = None
    reason: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": "resolved" if self.value is not None else "unresolved",
            "value": self.value,
            "unit": self.unit,
            "period_end": self.period_end,
            "inputs": [fact.to_dict() for fact in self.inputs],
            "market_inputs": self.market_inputs or {},
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ImpliedExpectationsResult:
    metrics: tuple[PanelMetric, ...]
    as_of: str
    live_inputs: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": "implied_expectations",
            "feeds_grade": False,
            "verdict": None,
            "predictive_claim": False,
            "as_of": self.as_of,
            "coverage": {
                "resolved": sum(metric.value is not None for metric in self.metrics),
                "wanted": len(self.metrics),
            },
            "metrics": [metric.to_dict() for metric in self.metrics],
            "live_inputs": self.live_inputs,
        }


def compute_implied_expectations(
    companyfacts: dict[str, Any],
    as_of: date,
    raw_bars: list[PriceBar],
    split_adjusted_bars: list[PriceBar],
    sector_bars: list[PriceBar] | None = None,
    return_bars: list[PriceBar] | None = None,
) -> ImpliedExpectationsResult:
    returns_source = return_bars if return_bars is not None else split_adjusted_bars
    annual = {
        name: resolve_fact_history(companyfacts, concept_spec(name), as_of)
        for name in (
            "operating_income",
            "operating_cash_flow",
            "capital_expenditure",
            "revenue",
        )
    }
    balance = {
        name: resolve_quarterly_fact_history(
            companyfacts, concept_spec(name), as_of
        )
        for name in ("total_debt", "cash", "book_equity", "shares_outstanding")
    }
    shares = balance["shares_outstanding"][0] if balance["shares_outstanding"] else None
    market_equity: float | None = None
    current_price: PriceBar | None = None
    market_error: str | None = None
    split_factors: tuple[float, float] | None = None
    if shares is None:
        market_error = "a current share-count fact is required"
    else:
        try:
            market_equity, current_price, statement_factor, current_factor = (
                split_adjusted_market_equity(
                    shares, raw_bars, split_adjusted_bars, as_of
                )
            )
            split_factors = (statement_factor, current_factor)
        except ValueError as exc:
            market_error = str(exc)

    debt = balance["total_debt"][0] if balance["total_debt"] else None
    cash = balance["cash"][0] if balance["cash"] else None
    book_equity = balance["book_equity"][0] if balance["book_equity"] else None
    market_receipt = (
        {
            "price": current_price.to_dict(),
            "market_equity": market_equity,
            "statement_split_factor": split_factors[0],
            "current_split_factor": split_factors[1],
        }
        if current_price and split_factors and market_equity is not None
        else {"error": market_error}
    )

    ebit = annual["operating_income"][0] if annual["operating_income"] else None
    revenue = annual["revenue"][0] if annual["revenue"] else None
    operating_cash_flow, capex = _latest_aligned(
        annual["operating_cash_flow"], annual["capital_expenditure"]
    )
    fcf = (
        float(operating_cash_flow.value - capex.value)
        if operating_cash_flow and capex and operating_cash_flow.unit == capex.unit
        else None
    )
    metrics = (
        _ebit_to_ev(ebit, debt, cash, market_equity, market_receipt),
        _yield_metric(
            "fcf_yield",
            fcf,
            operating_cash_flow.end if operating_cash_flow else None,
            tuple(fact for fact in (operating_cash_flow, capex) if fact),
            market_equity,
            market_receipt,
        ),
        _yield_metric(
            "book_to_price",
            float(book_equity.value) if book_equity else None,
            book_equity.end if book_equity else None,
            (book_equity,) if book_equity else (),
            market_equity,
            market_receipt,
        ),
        _yield_metric(
            "sales_to_price",
            float(revenue.value) if revenue else None,
            revenue.end if revenue else None,
            (revenue,) if revenue else (),
            market_equity,
            market_receipt,
        ),
        _price_metric("momentum_12_1", returns_source, as_of),
        _price_metric("distance_from_52_week_high", split_adjusted_bars, as_of),
        _relative_strength(returns_source, sector_bars, as_of),
        _composite_equity_issuance(
            companyfacts,
            as_of,
            raw_bars,
            returns_source,
            market_equity,
            market_receipt,
        ),
    )
    return ImpliedExpectationsResult(
        metrics,
        as_of.isoformat(),
        {
            "price": current_price.to_dict() if current_price else None,
            "shares": shares.to_dict() if shares else None,
            "market_equity": market_equity,
            "ebit": ebit.value if ebit else None,
            "free_cash_flow": fcf,
            "book_equity": book_equity.value if book_equity else None,
            "sales": revenue.value if revenue else None,
            "debt": debt.value if debt else None,
            "cash": cash.value if cash else None,
        },
    )


def revalue_implied_expectations(
    live_inputs: dict[str, Any], live_price: float | None, *, price_timestamp: str | None
) -> dict[str, Any]:
    baseline_price = _positive_number(
        (live_inputs.get("price") or {}).get("close")
    )
    baseline_market_equity = _positive_number(live_inputs.get("market_equity"))
    current_price = _positive_number(live_price)
    if not baseline_price or not baseline_market_equity or not current_price:
        return {
            "status": "unresolved",
            "reason": "missing_live_or_baseline_price",
            "feeds_grade": False,
            "verdict": None,
            "metrics": {},
        }
    market_equity = baseline_market_equity * current_price / baseline_price
    debt = _number(live_inputs.get("debt"))
    cash = _number(live_inputs.get("cash"))
    enterprise_value = (
        market_equity + debt - cash if debt is not None and cash is not None else None
    )
    numerators = {
        "fcf_yield": live_inputs.get("free_cash_flow"),
        "book_to_price": live_inputs.get("book_equity"),
        "sales_to_price": live_inputs.get("sales"),
    }
    metrics: dict[str, float | None] = {
        name: (
            _number(value) / market_equity
            if _number(value) is not None and market_equity > DENOMINATOR_FLOOR
            else None
        )
        for name, value in numerators.items()
    }
    ebit = _number(live_inputs.get("ebit"))
    metrics["ebit_to_enterprise_value"] = (
        ebit / enterprise_value
        if ebit is not None
        and enterprise_value is not None
        and enterprise_value > DENOMINATOR_FLOOR
        else None
    )
    return {
        "status": "resolved",
        "as_of": price_timestamp,
        "price": current_price,
        "market_equity": market_equity,
        "enterprise_value": enterprise_value,
        "metrics": metrics,
        "feeds_grade": False,
        "verdict": None,
        "predictive_claim": False,
    }


def _ebit_to_ev(
    ebit: ResolvedFact | None,
    debt: ResolvedFact | None,
    cash: ResolvedFact | None,
    market_equity: float | None,
    market_receipt: dict[str, Any],
) -> PanelMetric:
    name = "ebit_to_enterprise_value"
    inputs = tuple(fact for fact in (ebit, debt, cash) if fact)
    if ebit is None or debt is None or cash is None or market_equity is None:
        return _missing(name, ebit.end if ebit else None, inputs, market_receipt)
    if len({fact.unit for fact in inputs}) != 1:
        return _failure(name, ebit.end, inputs, market_receipt, "unit_mismatch", "EBIT, debt, and cash use multiple currencies")
    enterprise_value = market_equity + debt.value - cash.value
    if enterprise_value <= DENOMINATOR_FLOOR:
        return _failure(name, ebit.end, inputs, market_receipt, "non_positive_denominator", f"enterprise value must exceed ${DENOMINATOR_FLOOR:,}")
    return PanelMetric(
        name,
        float(ebit.value / enterprise_value),
        "ratio",
        ebit.end,
        inputs,
        {**market_receipt, "enterprise_value": enterprise_value},
    )


def _yield_metric(
    name: str,
    numerator: float | None,
    period_end: str | None,
    inputs: tuple[ResolvedFact, ...],
    market_equity: float | None,
    market_receipt: dict[str, Any],
) -> PanelMetric:
    if numerator is None or market_equity is None:
        return _missing(name, period_end, inputs, market_receipt)
    if market_equity <= DENOMINATOR_FLOOR:
        return _failure(name, period_end, inputs, market_receipt, "non_positive_denominator", f"market equity must exceed ${DENOMINATOR_FLOOR:,}")
    return PanelMetric(name, numerator / market_equity, "ratio", period_end, inputs, market_receipt)


def _price_metric(name: str, bars: list[PriceBar], as_of: date) -> PanelMetric:
    try:
        value = (
            momentum_12_1(bars, as_of)
            if name == "momentum_12_1"
            else distance_from_52_week_high(bars, as_of)
        )
    except ValueError as exc:
        return PanelMetric(name, None, "return", as_of.isoformat(), reason="insufficient_history", detail=str(exc))
    return PanelMetric(name, value, "return", as_of.isoformat())


def _relative_strength(
    stock: list[PriceBar], sector: list[PriceBar] | None, as_of: date
) -> PanelMetric:
    name = "sector_relative_strength"
    if not sector:
        return PanelMetric(name, None, "log_excess_return", as_of.isoformat(), reason="missing_input", detail="sector benchmark prices are required")
    try:
        value = sector_relative_strength(stock, sector, as_of)
    except ValueError as exc:
        return PanelMetric(name, None, "log_excess_return", as_of.isoformat(), reason="insufficient_history", detail=str(exc))
    return PanelMetric(name, value, "log_excess_return", as_of.isoformat())


def _composite_equity_issuance(
    companyfacts: dict[str, Any],
    as_of: date,
    raw_bars: list[PriceBar],
    total_return_bars: list[PriceBar],
    current_market_equity: float | None,
    market_receipt: dict[str, Any],
) -> PanelMetric:
    name = "composite_equity_issuance_5y"
    shares = resolve_consistent_fact_history(
        companyfacts, concept_spec("shares_outstanding"), as_of
    )
    if not shares or current_market_equity is None:
        return _missing(name, shares[0].end if shares else None, tuple(shares), market_receipt)
    current = shares[0]
    prior_candidates = [
        fact
        for fact in shares[1:]
        if 4.5 * 365
        <= (date.fromisoformat(current.end) - date.fromisoformat(fact.end)).days
        <= 5.5 * 365
    ]
    if not prior_candidates:
        return PanelMetric(name, None, "log_growth", current.end, (current,), market_receipt, "insufficient_history", "a share count from five years earlier is required")
    prior = min(
        prior_candidates,
        key=lambda fact: abs(
            (date.fromisoformat(current.end) - date.fromisoformat(fact.end)).days
            - 5 * 365
        ),
    )
    raw_prior = _latest(raw_bars, date.fromisoformat(prior.end))
    return_prior = _latest(total_return_bars, date.fromisoformat(prior.end))
    return_current = _latest(total_return_bars, as_of)
    inputs = (current, prior)
    if raw_prior is None or return_prior is None or return_current is None:
        return _failure(name, current.end, inputs, market_receipt, "missing_input", "five-year total-return history is required")
    prior_market_equity = prior.value * raw_prior.close
    if prior_market_equity <= 0 or current_market_equity <= 0:
        return _failure(name, current.end, inputs, market_receipt, "non_positive_denominator", "current and prior market equity must be positive")
    cumulative_return = math.log(return_current.close / return_prior.close)
    value = math.log(current_market_equity / prior_market_equity) - cumulative_return
    return PanelMetric(
        name,
        value,
        "log_growth",
        current.end,
        inputs,
        {
            **market_receipt,
            "prior_raw_price": raw_prior.to_dict(),
            "prior_total_return_price": return_prior.to_dict(),
            "current_total_return_price": return_current.to_dict(),
            "prior_market_equity": prior_market_equity,
            "cumulative_log_return": cumulative_return,
        },
    )


def _latest_aligned(
    left: list[ResolvedFact], right: list[ResolvedFact]
) -> tuple[ResolvedFact | None, ResolvedFact | None]:
    common = {fact.end for fact in left} & {fact.end for fact in right}
    if not common:
        return None, None
    end = max(common)
    return (
        next(fact for fact in left if fact.end == end),
        next(fact for fact in right if fact.end == end),
    )


def _latest(bars: list[PriceBar], as_of: date) -> PriceBar | None:
    eligible = [bar for bar in bars if bar.day <= as_of]
    return max(eligible, key=lambda bar: bar.day) if eligible else None


def _missing(
    name: str,
    period_end: str | None,
    inputs: tuple[ResolvedFact, ...],
    market_inputs: dict[str, Any],
) -> PanelMetric:
    return _failure(name, period_end, inputs, market_inputs, "missing_input", "required accounting or market input is missing")


def _failure(
    name: str,
    period_end: str | None,
    inputs: tuple[ResolvedFact, ...],
    market_inputs: dict[str, Any],
    reason: str,
    detail: str,
) -> PanelMetric:
    return PanelMetric(name, None, "ratio", period_end, inputs, market_inputs, reason, detail)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_number(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None
