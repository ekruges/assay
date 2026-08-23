from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Iterable, Literal


PeriodKind = Literal["duration", "instant"]
ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "10-KT/A"}
QUARTERLY_FORMS = {"10-Q", "10-Q/A", "10-QT", "10-QT/A"}
FactRow = tuple[int, str, str, str, dict[str, Any]]


@dataclass(frozen=True)
class ConceptSpec:
    name: str
    candidates: tuple[tuple[str, str], ...]
    period: PeriodKind
    preferred_units: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedFact:
    concept: str
    value: int | float
    unit: str
    namespace: str
    tag: str
    start: str | None
    end: str
    filed: str
    form: str
    accession: str
    fiscal_year: int | None
    fiscal_period: str | None
    filing_url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MissingFact:
    concept: str
    reason: Literal["missing_input", "not_yet_filed", "stale_comparative"]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QuarterlyFact:
    concept: str
    value: int | float
    unit: str
    end: str
    fiscal_year: int | None
    fiscal_period: str | None
    method: Literal[
        "direct",
        "ytd_difference",
        "annual_less_ytd_q3",
        "annual_less_q1_q2_q3",
    ]
    inputs: tuple[ResolvedFact, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "concept": self.concept,
            "value": self.value,
            "unit": self.unit,
            "end": self.end,
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
            "method": self.method,
            "inputs": [fact.to_dict() for fact in self.inputs],
        }


CORE_CONCEPTS: tuple[ConceptSpec, ...] = (
    ConceptSpec(
        "revenue",
        (
            ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
            ("us-gaap", "Revenues"),
            ("us-gaap", "SalesRevenueNet"),
            ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
        ),
        "duration",
    ),
    ConceptSpec("gross_profit", (("us-gaap", "GrossProfit"),), "duration"),
    ConceptSpec(
        "net_income",
        (("us-gaap", "NetIncomeLoss"), ("us-gaap", "ProfitLoss")),
        "duration",
    ),
    ConceptSpec(
        "operating_cash_flow",
        (
            ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
            (
                "us-gaap",
                "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            ),
        ),
        "duration",
    ),
    ConceptSpec("assets", (("us-gaap", "Assets"),), "instant"),
    ConceptSpec("liabilities", (("us-gaap", "Liabilities"),), "instant"),
    ConceptSpec(
        "cash",
        (("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),),
        "instant",
    ),
    ConceptSpec(
        "shares_outstanding",
        (
            ("dei", "EntityCommonStockSharesOutstanding"),
            ("us-gaap", "CommonStockSharesOutstanding"),
        ),
        "instant",
        ("shares",),
    ),
)

SUPPORTING_CONCEPTS: tuple[ConceptSpec, ...] = (
    ConceptSpec(
        "operating_income",
        (("us-gaap", "OperatingIncomeLoss"),),
        "duration",
    ),
    ConceptSpec(
        "capital_expenditure",
        (
            ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
            ("us-gaap", "PaymentsForAdditionsToPropertyPlantAndEquipment"),
            ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ),
        "duration",
    ),
    ConceptSpec(
        "cost_of_revenue",
        (
            ("us-gaap", "CostOfRevenue"),
            ("us-gaap", "CostOfGoodsAndServicesSold"),
        ),
        "duration",
    ),
    ConceptSpec(
        "depreciation_amortization",
        (
            ("us-gaap", "DepreciationDepletionAndAmortization"),
            (
                "us-gaap",
                "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
            ),
            ("us-gaap", "Depreciation"),
        ),
        "duration",
    ),
    ConceptSpec(
        "interest_expense",
        (
            ("us-gaap", "InterestExpenseNonOperating"),
            ("us-gaap", "InterestAndDebtExpense"),
            ("us-gaap", "InterestExpense"),
        ),
        "duration",
    ),
    ConceptSpec(
        "pretax_income",
        (
            (
                "us-gaap",
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            ),
            (
                "us-gaap",
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
            ),
            ("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"),
        ),
        "duration",
    ),
    ConceptSpec(
        "income_tax",
        (("us-gaap", "IncomeTaxExpenseBenefit"),),
        "duration",
    ),
    ConceptSpec(
        "current_assets",
        (("us-gaap", "AssetsCurrent"),),
        "instant",
    ),
    ConceptSpec(
        "current_liabilities",
        (("us-gaap", "LiabilitiesCurrent"),),
        "instant",
    ),
    ConceptSpec(
        "total_debt",
        (
            ("us-gaap", "LongTermDebtAndFinanceLeaseObligations"),
            ("us-gaap", "LongTermDebtAndCapitalLeaseObligations"),
            ("us-gaap", "LongTermDebt"),
        ),
        "instant",
    ),
    ConceptSpec(
        "long_term_debt",
        (
            ("us-gaap", "LongTermDebtAndFinanceLeaseObligationsNoncurrent"),
            ("us-gaap", "LongTermDebtNoncurrent"),
            ("us-gaap", "LongTermDebtAndFinanceLeaseObligations"),
            ("us-gaap", "LongTermDebtAndCapitalLeaseObligations"),
            ("us-gaap", "LongTermDebt"),
        ),
        "instant",
    ),
    ConceptSpec(
        "book_equity",
        (
            ("us-gaap", "StockholdersEquity"),
            (
                "us-gaap",
                "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            ),
            ("us-gaap", "PartnersCapital"),
        ),
        "instant",
    ),
    ConceptSpec(
        "retained_earnings",
        (("us-gaap", "RetainedEarningsAccumulatedDeficit"),),
        "instant",
    ),
    ConceptSpec(
        "inventory",
        (("us-gaap", "InventoryNet"),),
        "instant",
    ),
    ConceptSpec(
        "property_plant_equipment",
        (("us-gaap", "PropertyPlantAndEquipmentNet"),),
        "instant",
    ),
    ConceptSpec(
        "short_term_investments",
        (
            ("us-gaap", "ShortTermInvestments"),
            ("us-gaap", "MarketableSecuritiesCurrent"),
        ),
        "instant",
    ),
    ConceptSpec(
        "cash_and_short_term_investments",
        (
            ("us-gaap", "CashCashEquivalentsAndShortTermInvestments"),
            ("us-gaap", "CashAndShortTermInvestments"),
        ),
        "instant",
    ),
)

ALL_CONCEPTS = CORE_CONCEPTS + SUPPORTING_CONCEPTS


def resolve_fact(
    companyfacts: dict[str, Any], spec: ConceptSpec, as_of: date
) -> ResolvedFact | MissingFact:
    eligible, existed_after_as_of, stale_periods = _eligible_rows(
        companyfacts, spec, as_of
    )
    if not eligible:
        if stale_periods:
            return MissingFact(
                spec.name,
                "stale_comparative",
                "the first matching annual fact appeared more than 400 days after period end",
            )
        if existed_after_as_of:
            return MissingFact(
                spec.name,
                "not_yet_filed",
                f"matching data exists, but it was filed after {as_of.isoformat()}",
            )
        return MissingFact(spec.name, "missing_input", "no matching annual XBRL fact")

    latest_end = max(row[4]["end"] for row in eligible)
    return _resolve_period(companyfacts, spec, eligible, latest_end)


def resolve_fact_history(
    companyfacts: dict[str, Any],
    spec: ConceptSpec,
    as_of: date,
    limit: int | None = None,
) -> list[ResolvedFact]:
    """Return newest-first annual observations known by ``as_of``."""
    eligible, _, _ = _eligible_rows(companyfacts, spec, as_of)
    ends = sorted({row[4]["end"] for row in eligible}, reverse=True)
    if limit is not None:
        ends = ends[:limit]
    return [_resolve_period(companyfacts, spec, eligible, end) for end in ends]


def resolve_consistent_fact_history(
    companyfacts: dict[str, Any],
    spec: ConceptSpec,
    as_of: date,
    limit: int | None = None,
) -> list[ResolvedFact]:
    """Return one candidate-tag series, avoiding mixed-date phantom periods."""
    eligible, _, _ = _eligible_rows(companyfacts, spec, as_of)
    if not eligible:
        return []
    priority = min(row[0] for row in eligible)
    series = [row for row in eligible if row[0] == priority]
    ends = sorted({row[4]["end"] for row in series}, reverse=True)
    if limit is not None:
        ends = ends[:limit]
    return [_resolve_period(companyfacts, spec, series, end) for end in ends]


def resolve_quarterly_fact_history(
    companyfacts: dict[str, Any],
    spec: ConceptSpec,
    as_of: date,
    limit: int | None = None,
) -> list[ResolvedFact]:
    """Return direct quarter durations or quarter-end instants known by ``as_of``."""
    forms = (
        QUARTERLY_FORMS | ANNUAL_FORMS
        if spec.period == "instant"
        else QUARTERLY_FORMS
    )
    eligible, _, _ = _eligible_rows(
        companyfacts,
        spec,
        as_of,
        forms=forms,
        quarterly=True,
        stale_after_days=250,
    )
    ends = sorted({row[4]["end"] for row in eligible}, reverse=True)
    if limit is not None:
        ends = ends[:limit]
    return [
        _resolve_period(companyfacts, spec, eligible, end, ideal_duration_days=91)
        for end in ends
    ]


def resolve_discrete_quarters(
    companyfacts: dict[str, Any],
    spec: ConceptSpec,
    as_of: date,
    limit: int | None = None,
) -> list[QuarterlyFact]:
    """Resolve discrete quarters from direct, year-to-date, and annual facts."""
    if spec.period != "duration":
        raise ValueError("discrete quarters require a duration concept")
    direct = resolve_quarterly_fact_history(companyfacts, spec, as_of)
    ytd = _resolve_ytd_fact_history(companyfacts, spec, as_of)
    annual = resolve_fact_history(companyfacts, spec, as_of)
    output = [
        QuarterlyFact(
            fact.concept,
            fact.value,
            fact.unit,
            fact.end,
            fact.fiscal_year,
            fact.fiscal_period,
            "direct",
            (fact,),
        )
        for fact in direct
    ]
    by_end = {fact.end: fact for fact in output}
    by_start: dict[str, list[ResolvedFact]] = {}
    for fact in ytd:
        if fact.start:
            by_start.setdefault(fact.start, []).append(fact)
    for facts in by_start.values():
        facts.sort(key=lambda fact: fact.end)
        for previous, current in zip(facts, facts[1:]):
            gap = (
                date.fromisoformat(current.end) - date.fromisoformat(previous.end)
            ).days
            if current.end in by_end or not 70 <= gap <= 110:
                continue
            if current.unit != previous.unit:
                continue
            quarter = QuarterlyFact(
                spec.name,
                current.value - previous.value,
                current.unit,
                current.end,
                current.fiscal_year,
                current.fiscal_period,
                "ytd_difference",
                (current, previous),
            )
            output.append(quarter)
            by_end[quarter.end] = quarter

    for annual_fact in annual:
        ytd_candidates = [
            fact
            for fact in by_start.get(annual_fact.start or "", [])
            if fact.end < annual_fact.end
            and 70
            <= (
                date.fromisoformat(annual_fact.end)
                - date.fromisoformat(fact.end)
            ).days
            <= 110
        ]
        if annual_fact.end not in by_end and ytd_candidates:
            latest_ytd = max(ytd_candidates, key=lambda fact: fact.end)
            if latest_ytd.unit == annual_fact.unit:
                quarter = QuarterlyFact(
                    spec.name,
                    annual_fact.value - latest_ytd.value,
                    annual_fact.unit,
                    annual_fact.end,
                    annual_fact.fiscal_year,
                    "Q4",
                    "annual_less_ytd_q3",
                    (annual_fact, latest_ytd),
                )
                output.append(quarter)
                by_end[quarter.end] = quarter
                continue

        by_period = {
            fact.fiscal_period: fact
            for fact in direct
            if fact.fiscal_year == annual_fact.fiscal_year
            and fact.fiscal_period in {"Q1", "Q2", "Q3"}
            and fact.end < annual_fact.end
        }
        if set(by_period) != {"Q1", "Q2", "Q3"}:
            continue
        quarters = (by_period["Q1"], by_period["Q2"], by_period["Q3"])
        if any(fact.unit != annual_fact.unit for fact in quarters):
            continue
        if annual_fact.end not in by_end:
            quarter = QuarterlyFact(
                spec.name,
                annual_fact.value - sum(fact.value for fact in quarters),
                annual_fact.unit,
                annual_fact.end,
                annual_fact.fiscal_year,
                "Q4",
                "annual_less_q1_q2_q3",
                (annual_fact, *quarters),
            )
            output.append(quarter)
            by_end[quarter.end] = quarter
    output.sort(key=lambda fact: fact.end, reverse=True)
    return output[:limit] if limit is not None else output


def _resolve_ytd_fact_history(
    companyfacts: dict[str, Any], spec: ConceptSpec, as_of: date
) -> list[ResolvedFact]:
    eligible, _, _ = _eligible_rows(
        companyfacts,
        spec,
        as_of,
        forms=QUARTERLY_FORMS,
        ytd=True,
        stale_after_days=250,
    )
    ends = sorted({row[4]["end"] for row in eligible}, reverse=True)
    return [
        _resolve_period(companyfacts, spec, eligible, end, ideal_duration_days=182)
        for end in ends
    ]


def concept_spec(name: str) -> ConceptSpec:
    try:
        return next(spec for spec in ALL_CONCEPTS if spec.name == name)
    except StopIteration as exc:
        raise KeyError(f"unknown concept {name!r}") from exc


def _eligible_rows(
    companyfacts: dict[str, Any],
    spec: ConceptSpec,
    as_of: date,
    *,
    forms: set[str] = ANNUAL_FORMS,
    quarterly: bool = False,
    ytd: bool = False,
    stale_after_days: int = 400,
) -> tuple[list[FactRow], bool, set[str]]:
    eligible: list[FactRow] = []
    existed_after_as_of = False
    earliest_filed_by_series_end: dict[tuple[str, str, str, str], date] = {}

    for priority, (namespace, tag) in enumerate(spec.candidates):
        concept = companyfacts.get("facts", {}).get(namespace, {}).get(tag)
        if not concept:
            continue
        for unit, entries in concept.get("units", {}).items():
            for entry in entries:
                if entry.get("form") not in forms or not _valid_number(
                    entry.get("val")
                ):
                    continue
                if ytd:
                    period_matches = _matches_ytd(entry, spec.period)
                elif quarterly:
                    period_matches = _matches_quarter(entry, spec.period)
                else:
                    period_matches = _matches_period(entry, spec.period)
                if not period_matches:
                    continue
                try:
                    filed = date.fromisoformat(entry["filed"])
                    end = date.fromisoformat(entry["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                series_end = (namespace, tag, unit, entry["end"])
                previous = earliest_filed_by_series_end.get(series_end)
                if previous is None or filed < previous:
                    earliest_filed_by_series_end[series_end] = filed
                if filed > as_of:
                    existed_after_as_of = True
                    continue
                eligible.append((priority, namespace, tag, unit, entry))

    stale_series_ends = {
        series_end
        for series_end, first_filed in earliest_filed_by_series_end.items()
        if (first_filed - date.fromisoformat(series_end[3])).days > stale_after_days
    }
    stale_periods = {series_end[3] for series_end in stale_series_ends}
    return (
        [
            row
            for row in eligible
            if (row[1], row[2], row[3], row[4]["end"]) not in stale_series_ends
        ],
        existed_after_as_of,
        stale_periods,
    )


def _resolve_period(
    companyfacts: dict[str, Any],
    spec: ConceptSpec,
    eligible: list[FactRow],
    end: str,
    ideal_duration_days: int = 364,
) -> ResolvedFact:
    rows = [row for row in eligible if row[4]["end"] == end]
    best_priority = min(row[0] for row in rows)
    rows = [row for row in rows if row[0] == best_priority]
    rows = _prefer_unit(rows, spec.preferred_units)
    _, namespace, tag, unit, entry = max(
        rows,
        key=lambda row: (
            row[4]["filed"],
            _duration_quality(row[4], ideal_duration_days),
            row[4].get("accn", ""),
        ),
    )
    cik = int(companyfacts.get("cik", 0))
    accession = str(entry.get("accn", ""))
    return ResolvedFact(
        concept=spec.name,
        value=entry["val"],
        unit=unit,
        namespace=namespace,
        tag=tag,
        start=entry.get("start"),
        end=entry["end"],
        filed=entry["filed"],
        form=entry["form"],
        accession=accession,
        fiscal_year=entry.get("fy"),
        fiscal_period=entry.get("fp"),
        filing_url=filing_index_url(cik, accession),
    )


def resolve_core_facts(
    companyfacts: dict[str, Any], as_of: date
) -> list[ResolvedFact | MissingFact]:
    return [resolve_fact(companyfacts, spec, as_of) for spec in CORE_CONCEPTS]


def filing_index_url(cik: int, accession: str) -> str:
    if not cik or not accession:
        return ""
    accession_path = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_path}/"
        f"{accession}-index.html"
    )


def _matches_period(entry: dict[str, Any], period: PeriodKind) -> bool:
    start = entry.get("start")
    if period == "instant":
        return not start or start == entry.get("end")
    if not start:
        return False
    try:
        days = (date.fromisoformat(entry["end"]) - date.fromisoformat(start)).days
    except (KeyError, TypeError, ValueError):
        return False
    return 300 <= days <= 430 and entry.get("fp") in {None, "FY"}


def _matches_quarter(entry: dict[str, Any], period: PeriodKind) -> bool:
    start = entry.get("start")
    if period == "instant":
        return not start or start == entry.get("end")
    if not start:
        return False
    try:
        days = (date.fromisoformat(entry["end"]) - date.fromisoformat(start)).days
    except (KeyError, TypeError, ValueError):
        return False
    return 70 <= days <= 110 and entry.get("fp") in {None, "Q1", "Q2", "Q3"}


def _matches_ytd(entry: dict[str, Any], period: PeriodKind) -> bool:
    if period != "duration" or not entry.get("start"):
        return False
    try:
        days = (
            date.fromisoformat(entry["end"]) - date.fromisoformat(entry["start"])
        ).days
    except (KeyError, TypeError, ValueError):
        return False
    return 70 <= days <= 300 and entry.get("fp") in {None, "Q1", "Q2", "Q3"}


def _duration_quality(entry: dict[str, Any], ideal_days: int = 364) -> int:
    if not entry.get("start"):
        return 0
    try:
        days = (date.fromisoformat(entry["end"]) - date.fromisoformat(entry["start"])).days
    except (KeyError, TypeError, ValueError):
        return 0
    return -abs(days - ideal_days)


def _prefer_unit(
    rows: list[FactRow], preferred: Iterable[str]
) -> list[FactRow]:
    for unit in preferred:
        matches = [row for row in rows if row[3] == unit]
        if matches:
            return matches
    counts: dict[str, int] = {}
    for row in rows:
        counts[row[3]] = counts.get(row[3], 0) + 1
    chosen = max(counts, key=lambda unit: (counts[unit], unit))
    return [row for row in rows if row[3] == chosen]


def _valid_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )
