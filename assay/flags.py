from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from .diagnostics import CashRunwayResult, LateFilerResult
from .facts import ResolvedFact, concept_spec, resolve_fact_history
from .factors import FactorResult


@dataclass(frozen=True)
class FlagResult:
    name: str
    status: str
    active: bool | None
    detail: str
    inputs: tuple[ResolvedFact, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "active": self.active,
            "detail": self.detail,
            "inputs": [fact.to_dict() for fact in self.inputs],
        }


def build_company_flags(
    companyfacts: dict[str, Any],
    as_of: date,
    factors: tuple[FactorResult, ...],
    cash_runway: CashRunwayResult,
    late_filer: LateFilerResult,
    *,
    chs_probability: float | None,
    distress_threshold: float | None,
    minimum_factors: int = 7,
) -> tuple[FlagResult, ...]:
    factor_map = {factor.name: factor for factor in factors}
    issuance = factor_map.get("net_share_issuance")
    dilution = (
        FlagResult(
            "dilution",
            "resolved",
            issuance.value >= 0.25,
            f"split-adjusted year-over-year share growth is {issuance.value:.1%}",
            issuance.inputs,
        )
        if issuance and issuance.value is not None
        else FlagResult(
            "dilution",
            "unresolved",
            None,
            issuance.detail if issuance and issuance.detail else "split-adjusted share growth is unavailable",
            issuance.inputs if issuance else (),
        )
    )
    short_runway = (
        FlagResult(
            "short_runway",
            "resolved",
            bool(cash_runway.short_runway),
            (
                f"cash runway is {cash_runway.months:.1f} months"
                if cash_runway.months is not None
                else "trailing operating cash flow is nonnegative"
            ),
            (cash_runway.cash_input,) if cash_runway.cash_input else (),
        )
        if cash_runway.status == "resolved"
        else FlagResult(
            "short_runway",
            "unresolved",
            None,
            cash_runway.detail or cash_runway.reason or "cash runway is unavailable",
        )
    )
    late = (
        FlagResult(
            "late_filer",
            "resolved",
            bool(late_filer.late_filer),
            f"found {len(late_filer.filings)} NT 10-K/Q filings in the last eight quarters",
        )
        if late_filer.status == "resolved"
        else FlagResult(
            "late_filer",
            "unresolved",
            None,
            late_filer.reason or "submission history is unavailable",
        )
    )
    resolved_count = sum(factor.value is not None for factor in factors)
    thin = FlagResult(
        "thin_data",
        "resolved",
        resolved_count < minimum_factors,
        f"{resolved_count} of 14 grade factors are computable; minimum is {minimum_factors}",
    )
    degenerate = _degenerate_inputs(companyfacts, as_of)
    distress = (
        FlagResult(
            "distress",
            "resolved",
            chs_probability >= distress_threshold,
            f"CHS probability {chs_probability:.6%}; universe top-decile threshold {distress_threshold:.6%}",
        )
        if chs_probability is not None and distress_threshold is not None
        else FlagResult(
            "distress",
            "unresolved",
            None,
            "CHS probability or universe top-decile threshold is unavailable",
        )
    )
    fortress = _fortress(companyfacts, as_of)
    return (dilution, short_runway, late, thin, degenerate, distress, fortress)


def chs_distress_threshold(probabilities: list[float]) -> float | None:
    values = sorted(value for value in probabilities if math.isfinite(value))
    if not values:
        return None
    position = 0.9 * (len(values) - 1)
    left = math.floor(position)
    right = math.ceil(position)
    if left == right or values[left] == values[right]:
        return values[left]
    weight = position - left
    return values[left] * (1 - weight) + values[right] * weight


def _degenerate_inputs(
    companyfacts: dict[str, Any], as_of: date
) -> FlagResult:
    revenue = resolve_fact_history(
        companyfacts, concept_spec("revenue"), as_of, limit=1
    )
    assets = resolve_fact_history(companyfacts, concept_spec("assets"), as_of, limit=1)
    inputs = tuple(revenue + assets)
    if not revenue or not assets:
        return FlagResult(
            "degenerate_inputs",
            "unresolved",
            None,
            "latest revenue and assets are required",
            inputs,
        )
    active = revenue[0].value <= 100_000 or assets[0].value <= 100_000
    return FlagResult(
        "degenerate_inputs",
        "resolved",
        active,
        (
            "latest revenue or assets are at or below the $100,000 validity floor"
            if active
            else "latest revenue and assets both exceed the $100,000 validity floor"
        ),
        inputs,
    )


def _fortress(companyfacts: dict[str, Any], as_of: date) -> FlagResult:
    cash = resolve_fact_history(companyfacts, concept_spec("cash"), as_of)
    debt = resolve_fact_history(companyfacts, concept_spec("total_debt"), as_of)
    common = {fact.end for fact in cash} & {fact.end for fact in debt}
    if not common:
        return FlagResult(
            "fortress",
            "unresolved",
            None,
            "aligned cash and debt are required",
        )
    end = max(common)
    cash_fact = next(fact for fact in cash if fact.end == end)
    debt_fact = next(fact for fact in debt if fact.end == end)
    inputs = (cash_fact, debt_fact)
    if cash_fact.unit != debt_fact.unit:
        return FlagResult(
            "fortress",
            "unresolved",
            None,
            "cash and debt use different currencies",
            inputs,
        )
    net_cash = cash_fact.value - debt_fact.value
    return FlagResult(
        "fortress",
        "resolved",
        net_cash > 0,
        f"cash minus debt is {net_cash:,.0f} {cash_fact.unit}; Altman Z is not used",
        inputs,
    )
