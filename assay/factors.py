from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import pstdev
from typing import Any, Literal

from .facts import ResolvedFact, concept_spec, resolve_fact_history


Direction = Literal["higher", "lower"]
FailureReason = Literal[
    "missing_input",
    "non_positive_denominator",
    "insufficient_history",
    "period_mismatch",
    "unit_mismatch",
]
ASSET_FLOOR = 100_000
INVESTED_CAPITAL_FLOOR = 1_000_000
CHS_12M_INTERCEPT = -9.164
CHS_12M_COEFFICIENTS = {
    "nimtaavg": -20.264,
    "tlmta": 1.416,
    "exretavg": -7.129,
    "sigma": 1.411,
    "rsize": -0.045,
    "cashmta": -2.132,
    "mb": 0.075,
    "price": -0.058,
}
FACTOR_SLEEVES = {
    "gross_profitability": "profitability",
    "roic": "profitability",
    "accruals": "profitability",
    "gross_margin_stability_5y": "profitability",
    "chs_12m": "solvency",
    "altman_z_double_prime": "solvency",
    "net_debt_to_ebitda": "solvency",
    "interest_coverage": "solvency",
    "current_ratio": "solvency",
    "revenue_cagr_3y": "growth_and_financing",
    "fcf_cagr_3y": "growth_and_financing",
    "reinvestment_rate": "growth_and_financing",
    "asset_growth_3y": "growth_and_financing",
    "net_share_issuance": "growth_and_financing",
}
FACTOR_UNITS = {
    "gross_profitability": "ratio",
    "roic": "ratio",
    "accruals": "ratio",
    "gross_margin_stability_5y": "standard_deviation",
    "chs_12m": "probability",
    "altman_z_double_prime": "score",
    "net_debt_to_ebitda": "ratio",
    "interest_coverage": "ratio",
    "current_ratio": "ratio",
    "revenue_cagr_3y": "annual_rate",
    "fcf_cagr_3y": "annual_rate",
    "reinvestment_rate": "ratio",
    "asset_growth_3y": "annual_rate",
    "net_share_issuance": "growth_rate",
}
FACTOR_DEFINITIONS = {
    "gross_profitability": {
        "formula": "gross profit / total assets",
        "direction": "higher",
        "notes": "gross profit may be derived as revenue minus cost of revenue",
    },
    "roic": {
        "formula": "NOPAT / average invested capital",
        "direction": "higher",
        "notes": "NOPAT is operating income after the reported effective tax rate; invested capital is total debt plus book equity minus cash",
    },
    "accruals": {
        "formula": "(net income - operating cash flow) / average total assets",
        "direction": "lower",
    },
    "gross_margin_stability_5y": {
        "formula": "population standard deviation of gross profit / revenue over five consecutive annual periods",
        "direction": "lower",
    },
    "chs_12m": {
        "formula": "Campbell-Hilscher-Szilagyi Table IV month-12 logit",
        "direction": "lower",
    },
    "altman_z_double_prime": {
        "formula": "6.56*working_capital/assets + 3.26*retained_earnings/assets + 6.72*operating_income/assets + 1.05*book_equity/liabilities",
        "direction": "higher",
    },
    "net_debt_to_ebitda": {
        "formula": "(total debt - cash) / (operating income + depreciation and amortization)",
        "direction": "lower",
    },
    "interest_coverage": {
        "formula": "operating income / absolute interest expense",
        "direction": "higher",
    },
    "current_ratio": {
        "formula": "current assets / current liabilities",
        "direction": "higher",
    },
    "revenue_cagr_3y": {
        "formula": "(latest revenue / revenue three annual periods earlier)^(1/3) - 1",
        "direction": "higher",
    },
    "fcf_cagr_3y": {
        "formula": "(latest free cash flow / free cash flow three annual periods earlier)^(1/3) - 1",
        "direction": "higher",
        "notes": "free cash flow is operating cash flow minus capital expenditure",
    },
    "reinvestment_rate": {
        "formula": "(capital expenditure - depreciation and amortization + change in current assets minus current liabilities) / NOPAT",
        "direction": "higher",
    },
    "asset_growth_3y": {
        "formula": "(latest total assets / total assets three annual periods earlier)^(1/3) - 1",
        "direction": "higher",
    },
    "net_share_issuance": {
        "formula": "split-adjusted shares / split-adjusted shares one annual period earlier - 1",
        "direction": "lower",
    },
}


@dataclass(frozen=True)
class FactorResult:
    name: str
    value: float | None
    unit: str
    direction: Direction
    period_end: str | None
    inputs: tuple[ResolvedFact, ...] = ()
    reason: FailureReason | None = None
    detail: str | None = None
    sleeve: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "status": "resolved" if self.value is not None else "unresolved",
            "value": self.value,
            "unit": self.unit,
            "direction": self.direction,
            "period_end": self.period_end,
            "inputs": [fact.to_dict() for fact in self.inputs],
            "sleeve": self.sleeve or FACTOR_SLEEVES.get(self.name),
        }
        if self.reason:
            result["reason"] = self.reason
            result["failure_class"] = {
                "missing_input": "missing_input",
                "not_yet_filed": "missing_input",
                "unit_mismatch": "missing_input",
                "non_positive_denominator": "non_positive_or_below_floor_denominator",
                "insufficient_history": "insufficient_history",
                "period_mismatch": "insufficient_history",
            }.get(self.reason, self.reason)
        if self.detail:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True)
class ChsInputs:
    nimtaavg: float
    tlmta: float
    exretavg: float
    sigma: float
    rsize: float
    cashmta: float
    mb: float
    price: float


@dataclass(frozen=True)
class ChsScore:
    log_odds: float
    conditional_failure_probability: float
    contributions: dict[str, float]
    horizon_months: int = 12
    fitted_sample: str = "1963-2003"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": "CHS",
            "status": "resolved",
            "horizon_months": self.horizon_months,
            "fitted_sample": self.fitted_sample,
            "log_odds": self.log_odds,
            "conditional_failure_probability": self.conditional_failure_probability,
            "contributions": self.contributions,
            "cumulative_probability": False,
        }


def compute_accounting_factors(
    companyfacts: dict[str, Any], as_of: date
) -> tuple[FactorResult, ...]:
    names = (
        "revenue",
        "gross_profit",
        "cost_of_revenue",
        "operating_income",
        "net_income",
        "operating_cash_flow",
        "capital_expenditure",
        "depreciation_amortization",
        "interest_expense",
        "pretax_income",
        "income_tax",
        "assets",
        "liabilities",
        "current_assets",
        "current_liabilities",
        "total_debt",
        "book_equity",
        "retained_earnings",
        "cash",
    )
    histories = {
        name: resolve_fact_history(companyfacts, concept_spec(name), as_of)
        for name in names
    }
    results = (
        _gross_profitability(
            histories["gross_profit"],
            histories["revenue"],
            histories["cost_of_revenue"],
            histories["assets"],
        ),
        _roic(histories),
        _accruals(
            histories["net_income"],
            histories["operating_cash_flow"],
            histories["assets"],
        ),
        _gross_margin_stability(
            histories["gross_profit"],
            histories["revenue"],
            histories["cost_of_revenue"],
        ),
        _altman_z_double_prime(histories),
        _net_debt_to_ebitda(histories),
        _interest_coverage(histories),
        _current_ratio(histories),
        _cagr("revenue_cagr_3y", histories["revenue"], "higher"),
        _fcf_cagr(histories),
        _reinvestment_rate(histories),
        _cagr("asset_growth_3y", histories["assets"], "higher"),
    )
    latest_annual_end = histories["assets"][0].end if histories["assets"] else None
    return tuple(
        _require_latest_annual_period(result, latest_annual_end)
        for result in results
    )


def compute_chs_12m(inputs: ChsInputs) -> ChsScore:
    """Evaluate CHS Table IV's 12-month-horizon conditional failure model.

    Inputs must already follow the paper's definitions and pooled 5/95
    winsorization. The logistic output is conditional failure at month 12, not
    cumulative failure over the next 12 months and not a modern calibration.
    """
    values = vars(inputs)
    invalid = [name for name, value in values.items() if not math.isfinite(value)]
    if invalid:
        raise ValueError(f"CHS inputs must be finite: {', '.join(invalid)}")

    contributions = {
        name: value * CHS_12M_COEFFICIENTS[name] for name, value in values.items()
    }
    log_odds = CHS_12M_INTERCEPT + sum(contributions.values())
    if log_odds >= 0:
        probability = 1 / (1 + math.exp(-log_odds))
    else:
        exp_log_odds = math.exp(log_odds)
        probability = exp_log_odds / (1 + exp_log_odds)
    return ChsScore(log_odds, probability, contributions)


def _gross_profitability(
    gross_profit: list[ResolvedFact],
    revenue: list[ResolvedFact],
    cost_of_revenue: list[ResolvedFact],
    assets: list[ResolvedFact],
) -> FactorResult:
    name = "gross_profitability"
    direct_ends = {fact.end for fact in gross_profit} & {fact.end for fact in assets}
    derived_ends = (
        {fact.end for fact in revenue}
        & {fact.end for fact in cost_of_revenue}
        & {fact.end for fact in assets}
    )
    available_ends = direct_ends | derived_ends
    if not available_ends:
        return _missing_or_misaligned(
            name, "higher", gross_profit or revenue, cost_of_revenue, assets
        )

    common_end = max(available_ends)
    total_assets = _at_end(assets, common_end)
    if common_end in direct_ends:
        gross = _at_end(gross_profit, common_end)
        gross_value = gross.value
        gross_unit = gross.unit
        inputs = (gross, total_assets)
        detail = None
    else:
        sales = _at_end(revenue, common_end)
        costs = _at_end(cost_of_revenue, common_end)
        inputs = (sales, costs, total_assets)
        if sales.unit != costs.unit:
            return _unit_failure(name, "higher", common_end, inputs)
        gross_value = sales.value - costs.value
        gross_unit = sales.unit
        detail = "gross profit derived as revenue minus cost of revenue"
    if gross_unit != total_assets.unit:
        return _failure(
            name,
            "higher",
            common_end,
            inputs,
            "unit_mismatch",
            f"gross profit is {gross_unit}; assets are {total_assets.unit}",
        )
    if total_assets.value <= ASSET_FLOOR:
        return _failure(
            name,
            "higher",
            common_end,
            inputs,
            "non_positive_denominator",
            f"assets must exceed the ${ASSET_FLOOR:,} validity floor",
        )
    return FactorResult(
        name,
        float(gross_value / total_assets.value),
        "ratio",
        "higher",
        common_end,
        inputs,
        detail=detail,
    )


def _accruals(
    net_income: list[ResolvedFact],
    operating_cash_flow: list[ResolvedFact],
    assets: list[ResolvedFact],
) -> FactorResult:
    name = "accruals"
    common_end = _latest_common_end(net_income, operating_cash_flow, assets)
    if common_end is None:
        return _missing_or_misaligned(
            name, "lower", net_income, operating_cash_flow, assets
        )

    income = _at_end(net_income, common_end)
    cash_flow = _at_end(operating_cash_flow, common_end)
    current_assets = _at_end(assets, common_end)
    earlier_assets = [fact for fact in assets if fact.end < common_end]
    if not earlier_assets:
        return _failure(
            name,
            "lower",
            common_end,
            (income, cash_flow, current_assets),
            "insufficient_history",
            "a prior annual assets observation is required",
        )
    prior_assets = max(earlier_assets, key=lambda fact: fact.end)
    gap = (date.fromisoformat(common_end) - date.fromisoformat(prior_assets.end)).days
    inputs = (income, cash_flow, current_assets, prior_assets)
    if not 300 <= gap <= 430:
        return _failure(
            name,
            "lower",
            common_end,
            inputs,
            "period_mismatch",
            f"prior assets observation is {gap} days earlier",
        )
    units = {fact.unit for fact in inputs}
    if len(units) != 1:
        return _failure(
            name,
            "lower",
            common_end,
            inputs,
            "unit_mismatch",
            f"inputs use multiple units: {', '.join(sorted(units))}",
        )
    average_assets = (current_assets.value + prior_assets.value) / 2
    if average_assets <= ASSET_FLOOR:
        return _failure(
            name,
            "lower",
            common_end,
            inputs,
            "non_positive_denominator",
            f"average assets must exceed the ${ASSET_FLOOR:,} validity floor",
        )
    return FactorResult(
        name,
        float((income.value - cash_flow.value) / average_assets),
        "ratio",
        "lower",
        common_end,
        inputs,
    )


def _roic(histories: dict[str, list[ResolvedFact]]) -> FactorResult:
    name = "roic"
    current_names = (
        "operating_income",
        "pretax_income",
        "income_tax",
        "total_debt",
        "book_equity",
        "cash",
    )
    current_histories = [histories[key] for key in current_names]
    current_end = _latest_common_end(*current_histories)
    if current_end is None:
        return _missing_or_misaligned(name, "higher", *current_histories)
    current = {
        key: _at_end(histories[key], current_end) for key in current_names
    }
    capital_names = ("total_debt", "book_equity", "cash")
    capital_ends = set.intersection(
        *({fact.end for fact in histories[key]} for key in capital_names)
    )
    prior_ends = [end for end in capital_ends if end < current_end]
    if not prior_ends:
        return _failure(
            name,
            "higher",
            current_end,
            tuple(current.values()),
            "insufficient_history",
            "a prior annual debt, equity, and cash observation is required",
        )
    prior_end = max(prior_ends)
    gap = (date.fromisoformat(current_end) - date.fromisoformat(prior_end)).days
    prior = {key: _at_end(histories[key], prior_end) for key in capital_names}
    inputs = tuple(current.values()) + tuple(prior.values())
    if not 300 <= gap <= 430:
        return _failure(
            name,
            "higher",
            current_end,
            inputs,
            "period_mismatch",
            f"prior invested-capital observation is {gap} days earlier",
        )
    if not _same_unit(inputs):
        return _unit_failure(name, "higher", current_end, inputs)
    tax_rate = _effective_tax_rate(current["income_tax"], current["pretax_income"])
    if tax_rate is None:
        return _failure(
            name,
            "higher",
            current_end,
            inputs,
            "non_positive_denominator",
            "pretax income must exceed the validity floor and imply a 0%-100% tax rate",
        )
    nopat = current["operating_income"].value * (1 - tax_rate)
    current_capital = (
        current["total_debt"].value
        + current["book_equity"].value
        - current["cash"].value
    )
    prior_capital = (
        prior["total_debt"].value
        + prior["book_equity"].value
        - prior["cash"].value
    )
    average_capital = (current_capital + prior_capital) / 2
    if average_capital <= INVESTED_CAPITAL_FLOOR:
        return _failure(
            name,
            "higher",
            current_end,
            inputs,
            "non_positive_denominator",
            f"average invested capital must exceed ${INVESTED_CAPITAL_FLOOR:,}",
        )
    return FactorResult(
        name,
        float(nopat / average_capital),
        "ratio",
        "higher",
        current_end,
        inputs,
    )


def _gross_margin_stability(
    gross_profit: list[ResolvedFact],
    revenue: list[ResolvedFact],
    cost_of_revenue: list[ResolvedFact],
) -> FactorResult:
    name = "gross_margin_stability_5y"
    revenue_ends = {fact.end for fact in revenue}
    direct_ends = {fact.end for fact in gross_profit} & revenue_ends
    derived_ends = {fact.end for fact in cost_of_revenue} & revenue_ends
    common_ends = sorted(direct_ends | derived_ends, reverse=True)
    if len(common_ends) < 5:
        return _failure(
            name,
            "lower",
            common_ends[0] if common_ends else None,
            (),
            "insufficient_history",
            f"five annual gross margins are required; found {len(common_ends)}",
        )
    ends = common_ends[:5]
    if not _consecutive_annual_ends(ends):
        return _failure(
            name,
            "lower",
            ends[0],
            (),
            "insufficient_history",
            "five consecutive annual gross margins are required",
        )
    inputs: list[ResolvedFact] = []
    margins: list[float] = []
    for end in ends:
        sales = _at_end(revenue, end)
        if end in direct_ends:
            gross = _at_end(gross_profit, end)
            inputs.extend((gross, sales))
            gross_value = gross.value
            gross_unit = gross.unit
        else:
            costs = _at_end(cost_of_revenue, end)
            inputs.extend((sales, costs))
            gross_value = sales.value - costs.value
            gross_unit = sales.unit
            if costs.unit != sales.unit:
                return _unit_failure(name, "lower", ends[0], tuple(inputs))
        if gross_unit != sales.unit:
            return _unit_failure(name, "lower", ends[0], tuple(inputs))
        if sales.value <= ASSET_FLOOR:
            return _failure(
                name,
                "lower",
                ends[0],
                tuple(inputs),
                "non_positive_denominator",
                f"annual revenue must exceed ${ASSET_FLOOR:,}",
            )
        margins.append(gross_value / sales.value)
    return FactorResult(
        name,
        float(pstdev(margins)),
        "standard_deviation",
        "lower",
        ends[0],
        tuple(inputs),
    )


def _altman_z_double_prime(
    histories: dict[str, list[ResolvedFact]],
) -> FactorResult:
    name = "altman_z_double_prime"
    keys = (
        "current_assets",
        "current_liabilities",
        "retained_earnings",
        "operating_income",
        "book_equity",
        "assets",
        "liabilities",
    )
    fact_histories = [histories[key] for key in keys]
    end = _latest_common_end(*fact_histories)
    if end is None:
        return _missing_or_misaligned(name, "higher", *fact_histories)
    facts = {key: _at_end(histories[key], end) for key in keys}
    inputs = tuple(facts.values())
    if not _same_unit(inputs):
        return _unit_failure(name, "higher", end, inputs)
    assets = facts["assets"].value
    liabilities = facts["liabilities"].value
    if assets <= ASSET_FLOOR or liabilities <= ASSET_FLOOR:
        return _failure(
            name,
            "higher",
            end,
            inputs,
            "non_positive_denominator",
            f"assets and liabilities must exceed ${ASSET_FLOOR:,}",
        )
    working_capital = facts["current_assets"].value - facts["current_liabilities"].value
    score = (
        6.56 * working_capital / assets
        + 3.26 * facts["retained_earnings"].value / assets
        + 6.72 * facts["operating_income"].value / assets
        + 1.05 * facts["book_equity"].value / liabilities
    )
    return FactorResult(name, float(score), "score", "higher", end, inputs)


def _net_debt_to_ebitda(
    histories: dict[str, list[ResolvedFact]],
) -> FactorResult:
    name = "net_debt_to_ebitda"
    keys = ("total_debt", "cash", "operating_income", "depreciation_amortization")
    fact_histories = [histories[key] for key in keys]
    end = _latest_common_end(*fact_histories)
    if end is None:
        return _missing_or_misaligned(name, "lower", *fact_histories)
    facts = {key: _at_end(histories[key], end) for key in keys}
    inputs = tuple(facts.values())
    if not _same_unit(inputs):
        return _unit_failure(name, "lower", end, inputs)
    ebitda = facts["operating_income"].value + facts["depreciation_amortization"].value
    if ebitda <= ASSET_FLOOR:
        return _failure(
            name,
            "lower",
            end,
            inputs,
            "non_positive_denominator",
            f"EBITDA must exceed ${ASSET_FLOOR:,}",
        )
    net_debt = facts["total_debt"].value - facts["cash"].value
    return FactorResult(name, float(net_debt / ebitda), "ratio", "lower", end, inputs)


def _interest_coverage(
    histories: dict[str, list[ResolvedFact]],
) -> FactorResult:
    name = "interest_coverage"
    keys = ("operating_income", "interest_expense")
    fact_histories = [histories[key] for key in keys]
    end = _latest_common_end(*fact_histories)
    if end is None:
        return _missing_or_misaligned(name, "higher", *fact_histories)
    operating_income, interest = (_at_end(histories[key], end) for key in keys)
    inputs = (operating_income, interest)
    if not _same_unit(inputs):
        return _unit_failure(name, "higher", end, inputs)
    denominator = abs(interest.value)
    if denominator <= ASSET_FLOOR:
        return _failure(
            name,
            "higher",
            end,
            inputs,
            "non_positive_denominator",
            f"interest expense must exceed ${ASSET_FLOOR:,}",
        )
    return FactorResult(
        name,
        float(operating_income.value / denominator),
        "ratio",
        "higher",
        end,
        inputs,
    )


def _current_ratio(histories: dict[str, list[ResolvedFact]]) -> FactorResult:
    name = "current_ratio"
    assets = histories["current_assets"]
    liabilities = histories["current_liabilities"]
    end = _latest_common_end(assets, liabilities)
    if end is None:
        return _missing_or_misaligned(name, "higher", assets, liabilities)
    current_assets = _at_end(assets, end)
    current_liabilities = _at_end(liabilities, end)
    inputs = (current_assets, current_liabilities)
    if not _same_unit(inputs):
        return _unit_failure(name, "higher", end, inputs)
    if current_liabilities.value <= ASSET_FLOOR:
        return _failure(
            name,
            "higher",
            end,
            inputs,
            "non_positive_denominator",
            f"current liabilities must exceed ${ASSET_FLOOR:,}",
        )
    return FactorResult(
        name,
        float(current_assets.value / current_liabilities.value),
        "ratio",
        "higher",
        end,
        inputs,
    )


def _cagr(name: str, history: list[ResolvedFact], direction: Direction) -> FactorResult:
    if len(history) < 4:
        return _failure(
            name,
            direction,
            history[0].end if history else None,
            tuple(history),
            "insufficient_history",
            f"four annual observations are required; found {len(history)}",
        )
    facts = history[:4]
    inputs = tuple(facts)
    if not _consecutive_annual_ends([fact.end for fact in facts]):
        return _failure(
            name,
            direction,
            facts[0].end,
            inputs,
            "insufficient_history",
            "four consecutive annual observations are required",
        )
    if not _same_unit(inputs):
        return _unit_failure(name, direction, facts[0].end, inputs)
    current, base = facts[0].value, facts[3].value
    if current <= 0 or base <= ASSET_FLOOR:
        return _failure(
            name,
            direction,
            facts[0].end,
            inputs,
            "non_positive_denominator",
            f"current value must be positive and the base must exceed ${ASSET_FLOOR:,}",
        )
    return FactorResult(
        name,
        float((current / base) ** (1 / 3) - 1),
        "annual_rate",
        direction,
        facts[0].end,
        inputs,
    )


def _fcf_cagr(histories: dict[str, list[ResolvedFact]]) -> FactorResult:
    name = "fcf_cagr_3y"
    cash_flow = histories["operating_cash_flow"]
    capex = histories["capital_expenditure"]
    ends = sorted(
        {fact.end for fact in cash_flow} & {fact.end for fact in capex}, reverse=True
    )
    if len(ends) < 4 or not _consecutive_annual_ends(ends[:4]):
        return _failure(
            name,
            "higher",
            ends[0] if ends else None,
            (),
            "insufficient_history",
            f"four consecutive annual FCF observations are required; found {len(ends)}",
        )
    inputs: list[ResolvedFact] = []
    values: list[float] = []
    for end in ends[:4]:
        operating = _at_end(cash_flow, end)
        spending = _at_end(capex, end)
        inputs.extend((operating, spending))
        if operating.unit != spending.unit:
            return _unit_failure(name, "higher", ends[0], tuple(inputs))
        values.append(operating.value - spending.value)
    if values[0] <= 0 or values[3] <= ASSET_FLOOR:
        return _failure(
            name,
            "higher",
            ends[0],
            tuple(inputs),
            "non_positive_denominator",
            f"current FCF must be positive and base FCF must exceed ${ASSET_FLOOR:,}",
        )
    return FactorResult(
        name,
        float((values[0] / values[3]) ** (1 / 3) - 1),
        "annual_rate",
        "higher",
        ends[0],
        tuple(inputs),
    )


def _reinvestment_rate(histories: dict[str, list[ResolvedFact]]) -> FactorResult:
    name = "reinvestment_rate"
    current_names = (
        "capital_expenditure",
        "depreciation_amortization",
        "operating_income",
        "pretax_income",
        "income_tax",
        "current_assets",
        "current_liabilities",
    )
    fact_histories = [histories[key] for key in current_names]
    end = _latest_common_end(*fact_histories)
    if end is None:
        return _missing_or_misaligned(name, "higher", *fact_histories)
    current = {key: _at_end(histories[key], end) for key in current_names}
    balance_ends = {
        fact.end for fact in histories["current_assets"]
    } & {fact.end for fact in histories["current_liabilities"]}
    prior_ends = [candidate for candidate in balance_ends if candidate < end]
    if not prior_ends:
        return _failure(
            name,
            "higher",
            end,
            tuple(current.values()),
            "insufficient_history",
            "a prior annual working-capital observation is required",
        )
    prior_end = max(prior_ends)
    if not 300 <= (date.fromisoformat(end) - date.fromisoformat(prior_end)).days <= 430:
        return _failure(
            name,
            "higher",
            end,
            tuple(current.values()),
            "period_mismatch",
            "prior working capital is not one fiscal year earlier",
        )
    prior_assets = _at_end(histories["current_assets"], prior_end)
    prior_liabilities = _at_end(histories["current_liabilities"], prior_end)
    inputs = tuple(current.values()) + (prior_assets, prior_liabilities)
    if not _same_unit(inputs):
        return _unit_failure(name, "higher", end, inputs)
    tax_rate = _effective_tax_rate(current["income_tax"], current["pretax_income"])
    if tax_rate is None:
        return _failure(
            name,
            "higher",
            end,
            inputs,
            "non_positive_denominator",
            "pretax income must exceed the validity floor and imply a 0%-100% tax rate",
        )
    nopat = current["operating_income"].value * (1 - tax_rate)
    if nopat <= ASSET_FLOOR:
        return _failure(
            name,
            "higher",
            end,
            inputs,
            "non_positive_denominator",
            f"NOPAT must exceed ${ASSET_FLOOR:,}",
        )
    current_nwc = current["current_assets"].value - current["current_liabilities"].value
    prior_nwc = prior_assets.value - prior_liabilities.value
    reinvestment = (
        current["capital_expenditure"].value
        - current["depreciation_amortization"].value
        + current_nwc
        - prior_nwc
    )
    return FactorResult(
        name,
        float(reinvestment / nopat),
        "ratio",
        "higher",
        end,
        inputs,
    )


def _latest_common_end(*histories: list[ResolvedFact]) -> str | None:
    if any(not history for history in histories):
        return None
    common = set.intersection(*({fact.end for fact in history} for history in histories))
    return max(common) if common else None


def _require_latest_annual_period(
    result: FactorResult, latest_annual_end: str | None
) -> FactorResult:
    if (
        result.value is None
        or result.period_end is None
        or latest_annual_end is None
        or result.period_end == latest_annual_end
    ):
        return result
    return FactorResult(
        result.name,
        None,
        result.unit,
        result.direction,
        result.period_end,
        result.inputs,
        "period_mismatch",
        (
            f"latest aligned inputs end {result.period_end}; "
            f"latest annual assets end {latest_annual_end}"
        ),
        result.sleeve,
    )


def _at_end(history: list[ResolvedFact], end: str) -> ResolvedFact:
    return next(fact for fact in history if fact.end == end)


def _same_unit(facts: tuple[ResolvedFact, ...]) -> bool:
    return len({fact.unit for fact in facts}) == 1


def _unit_failure(
    name: str,
    direction: Direction,
    period_end: str | None,
    inputs: tuple[ResolvedFact, ...],
) -> FactorResult:
    return _failure(
        name,
        direction,
        period_end,
        inputs,
        "unit_mismatch",
        f"inputs use multiple units: {', '.join(sorted({fact.unit for fact in inputs}))}",
    )


def _effective_tax_rate(
    income_tax: ResolvedFact, pretax_income: ResolvedFact
) -> float | None:
    if abs(pretax_income.value) <= ASSET_FLOOR:
        return None
    rate = income_tax.value / pretax_income.value
    return float(rate) if 0 <= rate <= 1 else None


def _consecutive_annual_ends(ends: list[str]) -> bool:
    return all(
        300
        <= (date.fromisoformat(newer) - date.fromisoformat(older)).days
        <= 430
        for newer, older in zip(ends, ends[1:])
    )


def _missing_or_misaligned(
    name: str, direction: Direction, *histories: list[ResolvedFact]
) -> FactorResult:
    missing = [index for index, history in enumerate(histories) if not history]
    reason: FailureReason = "missing_input" if missing else "period_mismatch"
    detail = (
        "one or more required annual facts are missing"
        if missing
        else "required facts have no common fiscal period end"
    )
    return _failure(name, direction, None, (), reason, detail)


def _failure(
    name: str,
    direction: Direction,
    period_end: str | None,
    inputs: tuple[ResolvedFact, ...],
    reason: FailureReason,
    detail: str,
) -> FactorResult:
    return FactorResult(
        name=name,
        value=None,
        unit=FACTOR_UNITS.get(name, "ratio"),
        direction=direction,
        period_end=period_end,
        inputs=inputs,
        reason=reason,
        detail=detail,
    )
