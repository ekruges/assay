from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .facts import (
    QuarterlyFact,
    ResolvedFact,
    concept_spec,
    filing_index_url,
    resolve_discrete_quarters,
    resolve_fact_history,
    resolve_quarterly_fact_history,
)


VALUE_FLOOR = 100_000


@dataclass(frozen=True)
class DiagnosticComponent:
    name: str
    passed: bool | None
    metric: float | None
    unit: str
    period_end: str | None
    inputs: tuple[ResolvedFact, ...] = ()
    reason: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "status": "resolved" if self.passed is not None else "unresolved",
            "passed": self.passed,
            "metric": self.metric,
            "unit": self.unit,
            "period_end": self.period_end,
            "inputs": [fact.to_dict() for fact in self.inputs],
        }
        if self.reason:
            result["reason"] = self.reason
        if self.detail:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True)
class PiotroskiResult:
    score: int | None
    components: tuple[DiagnosticComponent, ...]
    period_end: str | None

    def to_dict(self) -> dict[str, Any]:
        resolved = sum(component.passed is not None for component in self.components)
        return {
            "name": "piotroski_f_score",
            "kind": "diagnostic",
            "feeds_grade": False,
            "status": "resolved" if self.score is not None else "unresolved",
            "score": self.score,
            "maximum": 9,
            "period_end": self.period_end,
            "coverage": {"resolved": resolved, "wanted": 9},
            "components": [component.to_dict() for component in self.components],
            "reason": None if self.score is not None else "incomplete_components",
        }


@dataclass(frozen=True)
class CashRunwayResult:
    status: str
    months: float | None
    trailing_operating_cash_flow: float | None
    burning_cash: bool | None
    short_runway: bool | None
    period_end: str | None
    cash_input: ResolvedFact | None = None
    cash_flow_inputs: tuple[QuarterlyFact, ...] = ()
    reason: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": "cash_runway",
            "kind": "diagnostic",
            "status": self.status,
            "months": self.months,
            "trailing_operating_cash_flow": self.trailing_operating_cash_flow,
            "burning_cash": self.burning_cash,
            "short_runway": self.short_runway,
            "threshold_months": 6,
            "period_end": self.period_end,
            "cash_input": self.cash_input.to_dict() if self.cash_input else None,
            "cash_flow_inputs": [fact.to_dict() for fact in self.cash_flow_inputs],
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class LateFilingReceipt:
    form: str
    filing_date: str
    accession: str
    filing_url: str

    def to_dict(self) -> dict[str, str]:
        return {
            "form": self.form,
            "filing_date": self.filing_date,
            "accession": self.accession,
            "filing_url": self.filing_url,
        }


@dataclass(frozen=True)
class LateFilerResult:
    status: str
    late_filer: bool | None
    lookback_days: int
    filings: tuple[LateFilingReceipt, ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": "late_filer",
            "kind": "diagnostic",
            "status": self.status,
            "late_filer": self.late_filer,
            "lookback_days": self.lookback_days,
            "forms": ["NT 10-K", "NT 10-Q"],
            "filings": [filing.to_dict() for filing in self.filings],
            "reason": self.reason,
        }


def compute_piotroski(
    companyfacts: dict[str, Any],
    as_of: date,
    *,
    split_adjusted_share_growth: float | None = None,
    share_inputs: tuple[ResolvedFact, ...] = (),
) -> PiotroskiResult:
    """Compute the nine-component F-score as a display-only diagnostic."""
    names = (
        "revenue",
        "gross_profit",
        "cost_of_revenue",
        "net_income",
        "operating_cash_flow",
        "assets",
        "current_assets",
        "current_liabilities",
        "long_term_debt",
    )
    histories = {
        name: resolve_fact_history(companyfacts, concept_spec(name), as_of)
        for name in names
    }
    assets = histories["assets"]
    current_end = assets[0].end if assets else None
    if current_end is None:
        components = tuple(
            _component_failure(name, "missing_input", "annual assets are missing")
            for name in _component_names()
        )
        return PiotroskiResult(None, components, None)

    prior_end = _prior_end(assets, current_end)
    older_end = _prior_end(assets, prior_end) if prior_end else None
    components = (
        _positive_roa(histories, current_end, prior_end),
        _positive_cash_flow(histories, current_end),
        _improving_roa(histories, current_end, prior_end, older_end),
        _cash_exceeds_income(histories, current_end),
        _declining_leverage(histories, current_end, prior_end, older_end),
        _improving_current_ratio(histories, current_end, prior_end),
        _no_new_shares(split_adjusted_share_growth, share_inputs, current_end),
        _improving_gross_margin(histories, current_end, prior_end),
        _improving_asset_turnover(histories, current_end, prior_end, older_end),
    )
    score = (
        sum(component.passed is True for component in components)
        if all(component.passed is not None for component in components)
        else None
    )
    return PiotroskiResult(score, components, current_end)


def compute_cash_runway(
    companyfacts: dict[str, Any], as_of: date
) -> CashRunwayResult:
    quarters = resolve_discrete_quarters(
        companyfacts, concept_spec("operating_cash_flow"), as_of, limit=4
    )
    if len(quarters) < 4:
        return CashRunwayResult(
            "unresolved",
            None,
            None,
            None,
            None,
            quarters[0].end if quarters else None,
            cash_flow_inputs=tuple(quarters),
            reason="insufficient_history",
            detail=f"four discrete operating-cash-flow quarters are required; found {len(quarters)}",
        )
    if not _consecutive_quarters(quarters):
        return CashRunwayResult(
            "unresolved",
            None,
            None,
            None,
            None,
            quarters[0].end,
            cash_flow_inputs=tuple(quarters),
            reason="period_mismatch",
            detail="the four latest operating-cash-flow quarters are not consecutive",
        )
    if len({quarter.unit for quarter in quarters}) != 1:
        return CashRunwayResult(
            "unresolved",
            None,
            None,
            None,
            None,
            quarters[0].end,
            cash_flow_inputs=tuple(quarters),
            reason="unit_mismatch",
            detail="quarterly operating cash flows use multiple units",
        )

    cash_history = resolve_quarterly_fact_history(
        companyfacts, concept_spec("cash"), as_of
    )
    cash = next((fact for fact in cash_history if fact.end == quarters[0].end), None)
    if cash is None:
        return CashRunwayResult(
            "unresolved",
            None,
            None,
            None,
            None,
            quarters[0].end,
            cash_flow_inputs=tuple(quarters),
            reason="missing_input",
            detail="cash is missing at the end of the latest cash-flow quarter",
        )
    if cash.unit != quarters[0].unit:
        return CashRunwayResult(
            "unresolved",
            None,
            None,
            None,
            None,
            quarters[0].end,
            cash,
            tuple(quarters),
            "unit_mismatch",
            f"cash is {cash.unit}; operating cash flow is {quarters[0].unit}",
        )
    trailing_cash_flow = float(sum(quarter.value for quarter in quarters))
    if cash.value < VALUE_FLOOR:
        return CashRunwayResult(
            "unresolved",
            None,
            trailing_cash_flow,
            trailing_cash_flow < 0,
            None,
            quarters[0].end,
            cash,
            tuple(quarters),
            "non_positive_or_below_floor_input",
            f"cash must be at least ${VALUE_FLOOR:,}",
        )
    if trailing_cash_flow >= 0:
        return CashRunwayResult(
            "resolved",
            None,
            trailing_cash_flow,
            False,
            False,
            quarters[0].end,
            cash,
            tuple(quarters),
            detail="trailing operating cash flow is nonnegative, so runway is not finite",
        )
    months = float(cash.value / (-trailing_cash_flow / 12))
    return CashRunwayResult(
        "resolved",
        months,
        trailing_cash_flow,
        True,
        months < 6,
        quarters[0].end,
        cash,
        tuple(quarters),
    )


def detect_late_filings(
    submissions: dict[str, Any], as_of: date, *, lookback_days: int = 730
) -> LateFilerResult:
    recent = submissions.get("filings", {}).get("recent")
    if not isinstance(recent, dict) or not isinstance(recent.get("form"), list):
        return LateFilerResult(
            "unresolved", None, lookback_days, reason="missing_submissions_recent_filings"
        )
    forms = recent["form"]
    filing_dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    cik_text = submissions.get("cik", 0)
    try:
        cik = int(cik_text)
    except (TypeError, ValueError):
        cik = 0
    cutoff = as_of - timedelta(days=lookback_days)
    receipts: list[LateFilingReceipt] = []
    for index, form in enumerate(forms):
        if form not in {"NT 10-K", "NT 10-Q"}:
            continue
        try:
            filed = date.fromisoformat(filing_dates[index])
        except (IndexError, TypeError, ValueError):
            continue
        if not cutoff <= filed <= as_of:
            continue
        accession = str(accessions[index]) if index < len(accessions) else ""
        receipts.append(
            LateFilingReceipt(
                form,
                filed.isoformat(),
                accession,
                filing_index_url(cik, accession),
            )
        )
    receipts.sort(key=lambda receipt: receipt.filing_date, reverse=True)
    return LateFilerResult("resolved", bool(receipts), lookback_days, tuple(receipts))


def _positive_roa(
    histories: dict[str, list[ResolvedFact]],
    current_end: str,
    prior_end: str | None,
) -> DiagnosticComponent:
    name = "positive_roa"
    income = _at(histories["net_income"], current_end)
    prior_assets = _at(histories["assets"], prior_end)
    if income is None or prior_assets is None:
        return _component_failure(name, "insufficient_history", "current income and beginning assets are required", current_end)
    return _ratio_test(name, income, prior_assets, lambda value: value > 0, current_end)


def _positive_cash_flow(
    histories: dict[str, list[ResolvedFact]], current_end: str
) -> DiagnosticComponent:
    name = "positive_operating_cash_flow"
    cash_flow = _at(histories["operating_cash_flow"], current_end)
    if cash_flow is None:
        return _component_failure(name, "missing_input", "current operating cash flow is required", current_end)
    return DiagnosticComponent(name, cash_flow.value > 0, float(cash_flow.value), cash_flow.unit, current_end, (cash_flow,))


def _improving_roa(
    histories: dict[str, list[ResolvedFact]],
    current_end: str,
    prior_end: str | None,
    older_end: str | None,
) -> DiagnosticComponent:
    name = "improving_roa"
    current_income = _at(histories["net_income"], current_end)
    prior_income = _at(histories["net_income"], prior_end)
    prior_assets = _at(histories["assets"], prior_end)
    older_assets = _at(histories["assets"], older_end)
    inputs = tuple(fact for fact in (current_income, prior_income, prior_assets, older_assets) if fact)
    if len(inputs) != 4:
        return _component_failure(name, "insufficient_history", "two income years and three asset years are required", current_end, inputs)
    if not _same_unit(inputs):
        return _component_failure(name, "unit_mismatch", "ROA inputs use multiple units", current_end, inputs)
    if prior_assets.value <= VALUE_FLOOR or older_assets.value <= VALUE_FLOOR:
        return _component_failure(name, "non_positive_denominator", f"beginning assets must exceed ${VALUE_FLOOR:,}", current_end, inputs)
    change = current_income.value / prior_assets.value - prior_income.value / older_assets.value
    return DiagnosticComponent(name, change > 0, float(change), "change_in_ratio", current_end, inputs)


def _cash_exceeds_income(
    histories: dict[str, list[ResolvedFact]], current_end: str
) -> DiagnosticComponent:
    name = "cash_flow_exceeds_net_income"
    cash_flow = _at(histories["operating_cash_flow"], current_end)
    income = _at(histories["net_income"], current_end)
    inputs = tuple(fact for fact in (cash_flow, income) if fact)
    if len(inputs) != 2:
        return _component_failure(name, "missing_input", "current cash flow and net income are required", current_end, inputs)
    if not _same_unit(inputs):
        return _component_failure(name, "unit_mismatch", "cash flow and income use different units", current_end, inputs)
    difference = cash_flow.value - income.value
    return DiagnosticComponent(name, difference > 0, float(difference), cash_flow.unit, current_end, inputs)


def _declining_leverage(
    histories: dict[str, list[ResolvedFact]],
    current_end: str,
    prior_end: str | None,
    older_end: str | None,
) -> DiagnosticComponent:
    name = "declining_long_term_leverage"
    current_debt = _at(histories["long_term_debt"], current_end)
    prior_debt = _at(histories["long_term_debt"], prior_end)
    current_assets = _at(histories["assets"], current_end)
    prior_assets = _at(histories["assets"], prior_end)
    older_assets = _at(histories["assets"], older_end)
    inputs = tuple(
        fact
        for fact in (
            current_debt,
            prior_debt,
            current_assets,
            prior_assets,
            older_assets,
        )
        if fact
    )
    if len(inputs) != 5:
        return _component_failure(
            name,
            "insufficient_history",
            "two debt years and three asset years are required",
            current_end,
            inputs,
        )
    if not _same_unit(inputs):
        return _component_failure(
            name,
            "unit_mismatch",
            "long-term-leverage inputs use multiple units",
            current_end,
            inputs,
        )
    current_average_assets = (current_assets.value + prior_assets.value) / 2
    prior_average_assets = (prior_assets.value + older_assets.value) / 2
    if current_average_assets <= VALUE_FLOOR or prior_average_assets <= VALUE_FLOOR:
        return _component_failure(
            name,
            "non_positive_denominator",
            f"average assets must exceed ${VALUE_FLOOR:,}",
            current_end,
            inputs,
        )
    change = (
        current_debt.value / current_average_assets
        - prior_debt.value / prior_average_assets
    )
    return DiagnosticComponent(
        name, change < 0, float(change), "change_in_ratio", current_end, inputs
    )


def _improving_current_ratio(
    histories: dict[str, list[ResolvedFact]],
    current_end: str,
    prior_end: str | None,
) -> DiagnosticComponent:
    current_assets = _at(histories["current_assets"], current_end)
    current_liabilities = _at(histories["current_liabilities"], current_end)
    prior_assets = _at(histories["current_assets"], prior_end)
    prior_liabilities = _at(histories["current_liabilities"], prior_end)
    inputs = tuple(
        fact
        for fact in (current_assets, current_liabilities, prior_assets, prior_liabilities)
        if fact
    )
    name = "improving_current_ratio"
    if len(inputs) != 4:
        return _component_failure(name, "insufficient_history", "two years of current assets and liabilities are required", current_end, inputs)
    if not _same_unit(inputs):
        return _component_failure(name, "unit_mismatch", "current-ratio inputs use multiple units", current_end, inputs)
    if current_liabilities.value <= VALUE_FLOOR or prior_liabilities.value <= VALUE_FLOOR:
        return _component_failure(name, "non_positive_denominator", f"current liabilities must exceed ${VALUE_FLOOR:,}", current_end, inputs)
    change = current_assets.value / current_liabilities.value - prior_assets.value / prior_liabilities.value
    return DiagnosticComponent(name, change > 0, float(change), "change_in_ratio", current_end, inputs)


def _no_new_shares(
    share_growth: float | None,
    inputs: tuple[ResolvedFact, ...],
    current_end: str,
) -> DiagnosticComponent:
    name = "no_new_shares"
    if share_growth is None:
        return _component_failure(
            name,
            "missing_input",
            "split-adjusted year-over-year share growth is required",
            current_end,
            inputs,
        )
    return DiagnosticComponent(name, share_growth <= 0, share_growth, "growth_rate", current_end, inputs)


def _improving_gross_margin(
    histories: dict[str, list[ResolvedFact]],
    current_end: str,
    prior_end: str | None,
) -> DiagnosticComponent:
    name = "improving_gross_margin"
    current = _gross_margin_inputs(histories, current_end)
    prior = _gross_margin_inputs(histories, prior_end)
    inputs = tuple(fact for item in (current, prior) if item for fact in item[1])
    if current is None or prior is None:
        return _component_failure(
            name,
            "insufficient_history",
            "two years of revenue and direct or derived gross profit are required",
            current_end,
            inputs,
        )
    if not _same_unit(inputs):
        return _component_failure(
            name,
            "unit_mismatch",
            "gross-margin inputs use multiple units",
            current_end,
            inputs,
        )
    current_gross, _, current_revenue = current
    prior_gross, _, prior_revenue = prior
    if current_revenue <= VALUE_FLOOR or prior_revenue <= VALUE_FLOOR:
        return _component_failure(
            name,
            "non_positive_denominator",
            f"revenue must exceed ${VALUE_FLOOR:,}",
            current_end,
            inputs,
        )
    change = current_gross / current_revenue - prior_gross / prior_revenue
    return DiagnosticComponent(
        name, change > 0, float(change), "change_in_ratio", current_end, inputs
    )


def _gross_margin_inputs(
    histories: dict[str, list[ResolvedFact]], end: str | None
) -> tuple[float, tuple[ResolvedFact, ...], float] | None:
    if end is None:
        return None
    revenue = _at(histories["revenue"], end)
    if revenue is None:
        return None
    gross_profit = _at(histories["gross_profit"], end)
    if gross_profit is not None:
        return float(gross_profit.value), (gross_profit, revenue), float(revenue.value)
    cost = _at(histories["cost_of_revenue"], end)
    if cost is None:
        return None
    return float(revenue.value - cost.value), (revenue, cost), float(revenue.value)


def _improving_asset_turnover(
    histories: dict[str, list[ResolvedFact]],
    current_end: str,
    prior_end: str | None,
    older_end: str | None,
) -> DiagnosticComponent:
    name = "improving_asset_turnover"
    current_revenue = _at(histories["revenue"], current_end)
    prior_revenue = _at(histories["revenue"], prior_end)
    prior_assets = _at(histories["assets"], prior_end)
    older_assets = _at(histories["assets"], older_end)
    inputs = tuple(
        fact
        for fact in (
            current_revenue,
            prior_revenue,
            prior_assets,
            older_assets,
        )
        if fact
    )
    if len(inputs) != 4:
        return _component_failure(name, "insufficient_history", "two revenue years and two beginning-asset observations are required", current_end, inputs)
    if not _same_unit(inputs):
        return _component_failure(name, "unit_mismatch", "asset-turnover inputs use multiple units", current_end, inputs)
    if prior_assets.value <= VALUE_FLOOR or older_assets.value <= VALUE_FLOOR:
        return _component_failure(name, "non_positive_denominator", f"beginning assets must exceed ${VALUE_FLOOR:,}", current_end, inputs)
    change = current_revenue.value / prior_assets.value - prior_revenue.value / older_assets.value
    return DiagnosticComponent(name, change > 0, float(change), "change_in_ratio", current_end, inputs)


def _improving_ratio(
    name: str,
    numerators: list[ResolvedFact],
    denominators: list[ResolvedFact],
    current_end: str,
    prior_end: str | None,
    *,
    lower_is_better: bool,
) -> DiagnosticComponent:
    current_numerator = _at(numerators, current_end)
    current_denominator = _at(denominators, current_end)
    prior_numerator = _at(numerators, prior_end)
    prior_denominator = _at(denominators, prior_end)
    inputs = tuple(
        fact
        for fact in (
            current_numerator,
            current_denominator,
            prior_numerator,
            prior_denominator,
        )
        if fact
    )
    if len(inputs) != 4:
        return _component_failure(name, "insufficient_history", "two aligned annual ratios are required", current_end, inputs)
    if not _same_unit(inputs):
        return _component_failure(name, "unit_mismatch", "ratio inputs use multiple units", current_end, inputs)
    if current_denominator.value <= VALUE_FLOOR or prior_denominator.value <= VALUE_FLOOR:
        return _component_failure(name, "non_positive_denominator", f"ratio denominators must exceed ${VALUE_FLOOR:,}", current_end, inputs)
    change = current_numerator.value / current_denominator.value - prior_numerator.value / prior_denominator.value
    passed = change < 0 if lower_is_better else change > 0
    return DiagnosticComponent(name, passed, float(change), "change_in_ratio", current_end, inputs)


def _ratio_test(
    name: str,
    numerator: ResolvedFact,
    denominator: ResolvedFact,
    test: Any,
    period_end: str,
) -> DiagnosticComponent:
    inputs = (numerator, denominator)
    if not _same_unit(inputs):
        return _component_failure(name, "unit_mismatch", "ratio inputs use different units", period_end, inputs)
    if denominator.value <= VALUE_FLOOR:
        return _component_failure(name, "non_positive_denominator", f"denominator must exceed ${VALUE_FLOOR:,}", period_end, inputs)
    ratio = float(numerator.value / denominator.value)
    return DiagnosticComponent(name, bool(test(ratio)), ratio, "ratio", period_end, inputs)


def _component_failure(
    name: str,
    reason: str,
    detail: str,
    period_end: str | None = None,
    inputs: tuple[ResolvedFact, ...] = (),
) -> DiagnosticComponent:
    return DiagnosticComponent(name, None, None, "", period_end, inputs, reason, detail)


def _component_names() -> tuple[str, ...]:
    return (
        "positive_roa",
        "positive_operating_cash_flow",
        "improving_roa",
        "cash_flow_exceeds_net_income",
        "declining_long_term_leverage",
        "improving_current_ratio",
        "no_new_shares",
        "improving_gross_margin",
        "improving_asset_turnover",
    )


def _prior_end(history: list[ResolvedFact], end: str | None) -> str | None:
    if end is None:
        return None
    previous = [fact.end for fact in history if fact.end < end]
    if not previous:
        return None
    candidate = max(previous)
    gap = (date.fromisoformat(end) - date.fromisoformat(candidate)).days
    return candidate if 300 <= gap <= 430 else None


def _at(history: list[ResolvedFact], end: str | None) -> ResolvedFact | None:
    if end is None:
        return None
    return next((fact for fact in history if fact.end == end), None)


def _same_unit(facts: tuple[ResolvedFact, ...]) -> bool:
    return len({fact.unit for fact in facts}) == 1


def _consecutive_quarters(quarters: list[QuarterlyFact]) -> bool:
    return all(
        70
        <= (date.fromisoformat(newer.end) - date.fromisoformat(older.end)).days
        <= 110
        for newer, older in zip(quarters, quarters[1:])
    )
