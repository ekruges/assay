from __future__ import annotations

import json
import math
import tempfile
import unittest
import zipfile
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from assay.cli import _packaged_sp500_reference, build_parser, run, run_all
from assay.diagnostics import (
    compute_cash_runway,
    compute_piotroski,
    detect_late_filings,
)
from assay.expectations import (
    compute_implied_expectations,
    revalue_implied_expectations,
)
from assay.flags import build_company_flags, chs_distress_threshold
from assay.forecast import (
    forecast_scorecard,
    publish_forecast_artifact,
    read_forecast_events,
    record_forecast,
    settle_due_forecasts,
)
from assay.factors import (
    ChsInputs,
    FactorResult,
    compute_accounting_factors,
    compute_chs_12m,
)
from assay.facts import (
    ConceptSpec,
    MissingFact,
    ResolvedFact,
    concept_spec,
    resolve_fact,
    resolve_fact_history,
    resolve_discrete_quarters,
    resolve_quarterly_fact_history,
)
from assay.market import (
    PriceBar,
    annualized_three_month_sigma,
    build_company_chs_raw,
    build_raw_chs_inputs,
    compute_net_share_issuance,
    distance_from_52_week_high,
    median_dollar_adv,
    momentum_12_1,
    monthly_log_excess_returns,
    pooled_chs_bounds,
    weighted_excess_return,
    weighted_nimta,
    winsorize_chs_inputs,
)
from assay.market_data import (
    AlpacaClient,
    MarketDataError,
    MarketSnapshot,
    MarketStore,
    _market_history_end,
    alpaca_symbol,
    parse_alpaca_bars,
    sync_market_data,
    sync_market_snapshots,
)
from assay.grading import (
    CompanyFactors,
    _percentile,
    _percentile_map,
    grade_label,
    grade_sensitivity,
    grade_universe,
    sampling_standard_deviation,
)
from assay.pipeline import (
    build_universe,
    emit_universe,
    run_full_pipeline,
    verify_output_tree,
)
from assay.sec import BulkSecStore, Company, SecClient, SecError
from assay.universe import classify_company, companies_from_ticker_file, sector_for_sic


FIXTURE = Path(__file__).parent / "fixtures" / "companyfacts.json"


def full_factor_companyfacts() -> dict[str, object]:
    years = (2019, 2020, 2021, 2022, 2023)
    duration_values = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": (
            90_000_000,
            100_000_000,
            110_000_000,
            121_000_000,
            133_100_000,
        ),
        "GrossProfit": (36_000_000, 40_000_000, 44_000_000, 48_400_000, 53_240_000),
        "OperatingIncomeLoss": (
            9_000_000,
            10_000_000,
            11_000_000,
            12_100_000,
            13_310_000,
        ),
        "NetIncomeLoss": (7_200_000, 8_000_000, 8_800_000, 9_680_000, 10_648_000),
        "NetCashProvidedByUsedInOperatingActivities": (
            10_800_000,
            12_000_000,
            13_200_000,
            14_520_000,
            15_972_000,
        ),
        "PaymentsToAcquirePropertyPlantAndEquipment": (
            3_600_000,
            4_000_000,
            4_400_000,
            4_840_000,
            5_324_000,
        ),
        "DepreciationDepletionAndAmortization": (
            1_800_000,
            2_000_000,
            2_200_000,
            2_420_000,
            2_662_000,
        ),
        "InterestExpenseNonOperating": (
            900_000,
            1_000_000,
            1_100_000,
            1_210_000,
            1_331_000,
        ),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": (
            9_000_000,
            10_000_000,
            11_000_000,
            12_100_000,
            13_310_000,
        ),
        "IncomeTaxExpenseBenefit": (
            1_800_000,
            2_000_000,
            2_200_000,
            2_420_000,
            2_662_000,
        ),
    }
    instant_values = {
        "Assets": (90_000_000, 100_000_000, 110_000_000, 121_000_000, 133_100_000),
        "Liabilities": (45_000_000, 50_000_000, 55_000_000, 60_500_000, 66_550_000),
        "AssetsCurrent": (36_000_000, 40_000_000, 44_000_000, 48_400_000, 53_240_000),
        "LiabilitiesCurrent": (
            18_000_000,
            20_000_000,
            22_000_000,
            24_200_000,
            26_620_000,
        ),
        "LongTermDebtAndFinanceLeaseObligations": (
            17_000_000,
            18_000_000,
            19_000_000,
            20_000_000,
            22_000_000,
        ),
        "StockholdersEquity": (
            45_000_000,
            50_000_000,
            55_000_000,
            60_500_000,
            66_550_000,
        ),
        "RetainedEarningsAccumulatedDeficit": (
            18_000_000,
            20_000_000,
            22_000_000,
            24_200_000,
            26_620_000,
        ),
        "CashAndCashEquivalentsAtCarryingValue": (
            9_000_000,
            10_000_000,
            11_000_000,
            12_100_000,
            13_310_000,
        ),
    }

    facts: dict[str, object] = {}
    for tag, values in duration_values.items():
        facts[tag] = {
            "units": {
                "USD": [
                    {
                        "start": f"{year}-01-01",
                        "end": f"{year}-12-31",
                        "val": value,
                        "accn": f"0000000001-{str(year + 1)[-2:]}-000001",
                        "fy": year,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": f"{year + 1}-02-20",
                    }
                    for year, value in zip(years, values)
                ]
            }
        }
    for tag, values in instant_values.items():
        facts[tag] = {
            "units": {
                "USD": [
                    {
                        "end": f"{year}-12-31",
                        "val": value,
                        "accn": f"0000000001-{str(year + 1)[-2:]}-000001",
                        "fy": year,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": f"{year + 1}-02-20",
                    }
                    for year, value in zip(years, values)
                ]
            }
        }
    return {"cik": 1, "entityName": "Full Factor Fixture", "facts": {"us-gaap": facts}}


def chs_ready_companyfacts(cik: int, index: int) -> dict[str, object]:
    companyfacts = deepcopy(full_factor_companyfacts())
    companyfacts["cik"] = cik
    companyfacts["entityName"] = f"Synthetic Company {index:03d}"
    facts = companyfacts["facts"]["us-gaap"]
    latest_multipliers = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": 0.8 + index / 250,
        "GrossProfit": 0.7 + index / 180,
        "OperatingIncomeLoss": 0.6 + index / 160,
        "NetIncomeLoss": 0.5 + index / 150,
        "NetCashProvidedByUsedInOperatingActivities": 0.7 + index / 200,
        "PaymentsToAcquirePropertyPlantAndEquipment": 0.8 + index / 300,
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": 0.6
        + index / 160,
        "IncomeTaxExpenseBenefit": 0.6 + index / 160,
        "Assets": 0.9 + index / 500,
        "Liabilities": 1.1 - index / 1000,
        "AssetsCurrent": 0.8 + index / 300,
        "LiabilitiesCurrent": 1.1 - index / 1000,
        "LongTermDebtAndFinanceLeaseObligations": 1.1 - index / 1000,
        "StockholdersEquity": 0.8 + index / 300,
        "RetainedEarningsAccumulatedDeficit": 0.7 + index / 250,
        "CashAndCashEquivalentsAtCarryingValue": 0.8 + index / 300,
    }
    for tag, multiplier in latest_multipliers.items():
        facts[tag]["units"]["USD"][-1]["val"] *= multiplier

    quarterly = (
        ("Q1", "2024-01-01", "2024-03-31", "2024-05-01"),
        ("Q2", "2024-04-01", "2024-06-30", "2024-08-01"),
        ("Q3", "2024-07-01", "2024-09-30", "2024-11-01"),
    )
    quarterly_income = 1_000_000 * (0.5 + index / 120)
    for quarter, start, end, filed in quarterly:
        facts["NetIncomeLoss"]["units"]["USD"].append(
            {
                "start": start,
                "end": end,
                "val": quarterly_income,
                "accn": f"{cik:010d}-24-{quarter[-1]}00000",
                "fy": 2024,
                "fp": quarter,
                "form": "10-Q",
                "filed": filed,
            }
        )
    facts["NetIncomeLoss"]["units"]["USD"].append(
        {
            "start": "2024-01-01",
            "end": "2024-12-31",
            "val": 4 * quarterly_income,
            "accn": f"{cik:010d}-25-000001",
            "fy": 2024,
            "fp": "FY",
            "form": "10-K",
            "filed": "2025-02-20",
        }
    )
    quarter_ends = (*((quarter, end, filed) for quarter, _, end, filed in quarterly), ("FY", "2024-12-31", "2025-02-20"))
    for tag, value in (
        ("Liabilities", 100_000_000 - index * 200_000),
        ("StockholdersEquity", 50_000_000 + index * 300_000),
    ):
        for quarter, end, filed in quarter_ends:
            facts[tag]["units"]["USD"].append(
                {
                    "end": end,
                    "val": value,
                    "accn": f"{cik:010d}-25-{quarter[-1]}00000",
                    "fy": 2024,
                    "fp": quarter,
                    "form": "10-K" if quarter == "FY" else "10-Q",
                    "filed": filed,
                }
            )
    facts["CashCashEquivalentsAndShortTermInvestments"] = {
        "units": {
            "USD": [
                {
                    "end": end,
                    "val": 15_000_000 + index * 100_000,
                    "accn": f"{cik:010d}-25-{quarter[-1]}00000",
                    "fy": 2024,
                    "fp": quarter,
                    "form": "10-K" if quarter == "FY" else "10-Q",
                    "filed": filed,
                }
                for quarter, end, filed in quarter_ends
            ]
        }
    }
    shares_2024 = 10_000_000 + index * 10_000
    companyfacts["facts"]["dei"] = {
        "EntityCommonStockSharesOutstanding": {
            "units": {
                "shares": [
                    {
                        "end": "2023-12-31",
                        "val": 9_800_000,
                        "accn": f"{cik:010d}-24-000001",
                        "fy": 2023,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2024-02-20",
                    },
                    *[
                        {
                            "end": end,
                            "val": shares_2024,
                            "accn": f"{cik:010d}-25-{quarter[-1]}00000",
                            "fy": 2024,
                            "fp": quarter,
                            "form": "10-K" if quarter == "FY" else "10-Q",
                            "filed": filed,
                        }
                        for quarter, end, filed in quarter_ends
                    ],
                ]
            }
        }
    }
    return companyfacts


class FactResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.companyfacts = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_latest_available_period_wins_before_tag_priority(self) -> None:
        spec = ConceptSpec(
            "revenue",
            (
                (
                    "us-gaap",
                    "RevenueFromContractWithCustomerExcludingAssessedTax",
                ),
                ("us-gaap", "Revenues"),
            ),
            "duration",
        )
        result = resolve_fact(self.companyfacts, spec, date(2024, 3, 1))
        self.assertIsInstance(result, ResolvedFact)
        self.assertEqual(result.value, 120000000)
        self.assertEqual(result.tag, "Revenues")
        self.assertEqual(result.end, "2023-12-31")

    def test_as_of_date_blocks_future_filing(self) -> None:
        spec = ConceptSpec("revenue", (("us-gaap", "Revenues"),), "duration")
        before = resolve_fact(self.companyfacts, spec, date(2024, 12, 31))
        after = resolve_fact(self.companyfacts, spec, date(2025, 3, 1))
        self.assertEqual(before.value, 120000000)
        self.assertEqual(after.value, 140000000)

    def test_latest_known_amendment_is_used(self) -> None:
        spec = ConceptSpec("assets", (("us-gaap", "Assets"),), "instant")
        before = resolve_fact(self.companyfacts, spec, date(2024, 3, 1))
        after = resolve_fact(self.companyfacts, spec, date(2024, 6, 1))
        self.assertEqual(before.value, 200000000)
        self.assertEqual(after.value, 205000000)
        self.assertEqual(after.form, "10-K/A")

    def test_late_comparative_is_rejected(self) -> None:
        spec = ConceptSpec(
            "operating_income", (("us-gaap", "OperatingIncomeLoss"),), "duration"
        )
        result = resolve_fact(self.companyfacts, spec, date(2023, 1, 1))
        self.assertIsInstance(result, MissingFact)
        self.assertEqual(result.reason, "stale_comparative")

    def test_late_primary_tag_cannot_displace_a_timely_fallback(self) -> None:
        companyfacts = {
            "cik": 1,
            "facts": {
                "us-gaap": {
                    "PrimaryRevenue": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2022-01-01",
                                    "end": "2022-12-31",
                                    "val": 999,
                                    "accn": "0000000001-24-000001",
                                    "fy": 2022,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2024-02-20",
                                }
                            ]
                        }
                    },
                    "FallbackRevenue": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2022-01-01",
                                    "end": "2022-12-31",
                                    "val": 100,
                                    "accn": "0000000001-23-000001",
                                    "fy": 2022,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2023-02-20",
                                }
                            ]
                        }
                    },
                }
            },
        }
        spec = ConceptSpec(
            "revenue",
            (("us-gaap", "PrimaryRevenue"), ("us-gaap", "FallbackRevenue")),
            "duration",
        )

        result = resolve_fact(companyfacts, spec, date(2024, 3, 1))

        self.assertIsInstance(result, ResolvedFact)
        self.assertEqual(result.tag, "FallbackRevenue")
        self.assertEqual(result.value, 100)

    def test_transition_reports_are_valid_domestic_periods(self) -> None:
        companyfacts = full_factor_companyfacts()
        latest = companyfacts["facts"]["us-gaap"][
            "RevenueFromContractWithCustomerExcludingAssessedTax"
        ]["units"]["USD"][-1]
        latest["form"] = "10-KT"

        result = resolve_fact(
            companyfacts, concept_spec("revenue"), date(2024, 3, 1)
        )

        self.assertIsInstance(result, ResolvedFact)
        self.assertEqual(result.form, "10-KT")
        self.assertEqual(result.end, "2023-12-31")

    def test_restricted_cash_is_not_treated_as_available_cash(self) -> None:
        companyfacts = {
            "cik": 1,
            "facts": {
                "us-gaap": {
                    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": {
                        "units": {
                            "USD": [
                                {
                                    "end": "2023-12-31",
                                    "val": 1_000_000,
                                    "accn": "0000000001-24-000001",
                                    "fy": 2023,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2024-02-20",
                                }
                            ]
                        }
                    }
                }
            },
        }

        result = resolve_fact(companyfacts, concept_spec("cash"), date(2024, 3, 1))

        self.assertIsInstance(result, MissingFact)
        self.assertEqual(result.reason, "missing_input")

    def test_history_is_newest_first_and_point_in_time(self) -> None:
        history = resolve_fact_history(
            self.companyfacts, concept_spec("assets"), date(2024, 3, 1)
        )
        self.assertEqual([fact.end for fact in history], ["2023-12-31", "2022-12-31"])
        self.assertEqual([fact.value for fact in history], [200000000, 180000000])

    def test_quarterly_history_ignores_year_to_date_durations(self) -> None:
        companyfacts = deepcopy(self.companyfacts)
        entries = companyfacts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
        entries.extend(
            [
                {
                    "start": "2024-04-01",
                    "end": "2024-06-30",
                    "val": 4_000_000,
                    "accn": "0000320193-24-000020",
                    "fy": 2024,
                    "fp": "Q2",
                    "form": "10-Q",
                    "filed": "2024-08-01",
                },
                {
                    "start": "2024-01-01",
                    "end": "2024-06-30",
                    "val": 7_000_000,
                    "accn": "0000320193-24-000020",
                    "fy": 2024,
                    "fp": "Q2",
                    "form": "10-Q",
                    "filed": "2024-08-01",
                },
            ]
        )

        history = resolve_quarterly_fact_history(
            companyfacts, concept_spec("net_income"), date(2024, 8, 2)
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].value, 4_000_000)
        self.assertEqual(history[0].start, "2024-04-01")

    def test_fourth_quarter_is_derived_with_all_source_receipts(self) -> None:
        companyfacts = deepcopy(self.companyfacts)
        entries = companyfacts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
        for quarter, start, end, value, filed in (
            ("Q1", "2024-01-01", "2024-03-31", 3_000_000, "2024-05-01"),
            ("Q2", "2024-04-01", "2024-06-30", 4_000_000, "2024-08-01"),
            ("Q3", "2024-07-01", "2024-09-30", 5_000_000, "2024-11-01"),
        ):
            entries.append(
                {
                    "start": start,
                    "end": end,
                    "val": value,
                    "accn": f"0000320193-24-{quarter[-1]}00000",
                    "fy": 2024,
                    "fp": quarter,
                    "form": "10-Q",
                    "filed": filed,
                }
            )
        entries.append(
            {
                "start": "2024-01-01",
                "end": "2024-12-31",
                "val": 18_000_000,
                "accn": "0000320193-25-000010",
                "fy": 2024,
                "fp": "FY",
                "form": "10-K",
                "filed": "2025-02-20",
            }
        )

        quarters = resolve_discrete_quarters(
            companyfacts, concept_spec("net_income"), date(2025, 3, 1), limit=4
        )
        self.assertEqual([quarter.fiscal_period for quarter in quarters], ["Q4", "Q3", "Q2", "Q1"])
        self.assertEqual(quarters[0].value, 6_000_000)
        self.assertEqual(quarters[0].method, "annual_less_q1_q2_q3")
        self.assertEqual(len(quarters[0].inputs), 4)

    def test_cli_fixture_is_structured_and_keeps_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = build_parser().parse_args(
                [
                    "TEST",
                    "--as-of",
                    "2024-03-01",
                    "--companyfacts-file",
                    str(FIXTURE),
                    "--market-db",
                    str(Path(directory) / "market.sqlite3"),
                ]
            )
            report = run(args)
        revenue = next(fact for fact in report["facts"] if fact["concept"] == "revenue")
        self.assertEqual(report["coverage"], {"resolved": 6, "wanted": 8})
        self.assertEqual(report["factor_coverage"], {"resolved": 2, "wanted": 14})
        self.assertEqual(revenue["tag"], "Revenues")
        self.assertIn("0000320193-24-000010-index.html", revenue["filing_url"])
        self.assertEqual(report["models"]["chs_12m"]["status"], "unresolved")

    def test_packaged_sp500_reference_is_derived_and_expires(self) -> None:
        reference = _packaged_sp500_reference(date(2026, 8, 21))
        self.assertEqual(reference["value_usd"], 67_075_482_580_000)
        self.assertIsNone(_packaged_sp500_reference(date(2027, 1, 1)))
        self.assertIsNone(_packaged_sp500_reference(date(2026, 7, 30)))


class FactorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.companyfacts = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_accounting_factors_keep_their_input_receipts(self) -> None:
        factors = compute_accounting_factors(self.companyfacts, date(2024, 3, 1))
        gross_profitability = next(
            factor for factor in factors if factor.name == "gross_profitability"
        )
        accruals = next(factor for factor in factors if factor.name == "accruals")

        self.assertAlmostEqual(gross_profitability.value, 0.24)
        self.assertEqual(gross_profitability.period_end, "2023-12-31")
        self.assertEqual(
            [fact.concept for fact in gross_profitability.inputs],
            ["gross_profit", "assets"],
        )
        self.assertAlmostEqual(accruals.value, -6_000_000 / 190_000_000)
        self.assertEqual(len(accruals.to_dict()["inputs"]), 4)

    def test_accruals_fail_closed_without_prior_assets(self) -> None:
        companyfacts = deepcopy(self.companyfacts)
        assets = companyfacts["facts"]["us-gaap"]["Assets"]["units"]["USD"]
        companyfacts["facts"]["us-gaap"]["Assets"]["units"]["USD"] = [
            fact for fact in assets if fact["end"] != "2022-12-31"
        ]

        factors = compute_accounting_factors(companyfacts, date(2024, 3, 1))
        accruals = next(factor for factor in factors if factor.name == "accruals")
        self.assertIsNone(accruals.value)
        self.assertEqual(accruals.reason, "insufficient_history")

    def test_unresolved_factors_keep_their_declared_units(self) -> None:
        factors = {
            factor.name: factor
            for factor in compute_accounting_factors(
                {"cik": 1, "facts": {}}, date(2024, 3, 1)
            )
        }

        self.assertEqual(factors["altman_z_double_prime"].unit, "score")
        self.assertEqual(factors["revenue_cagr_3y"].unit, "annual_rate")
        self.assertEqual(
            factors["gross_margin_stability_5y"].unit,
            "standard_deviation",
        )

    def test_full_accounting_factor_set_is_computable_and_auditable(self) -> None:
        factors = compute_accounting_factors(
            full_factor_companyfacts(), date(2024, 3, 1)
        )
        by_name = {factor.name: factor for factor in factors}
        self.assertEqual(len(by_name), 12)
        self.assertTrue(all(factor.value is not None for factor in factors))
        self.assertAlmostEqual(by_name["gross_profitability"].value, 0.4)
        self.assertAlmostEqual(by_name["gross_margin_stability_5y"].value, 0.0)
        self.assertAlmostEqual(by_name["current_ratio"].value, 2.0)
        self.assertAlmostEqual(by_name["revenue_cagr_3y"].value, 0.1)
        self.assertAlmostEqual(by_name["fcf_cagr_3y"].value, 0.1)
        self.assertAlmostEqual(by_name["asset_growth_3y"].value, 0.1)
        self.assertEqual(by_name["altman_z_double_prime"].unit, "score")
        self.assertTrue(all(factor.inputs for factor in factors))
        self.assertTrue(
            all(factor.to_dict()["sleeve"] is not None for factor in factors)
        )

    def test_gross_profit_can_be_derived_from_revenue_and_cost(self) -> None:
        companyfacts = full_factor_companyfacts()
        facts = companyfacts["facts"]["us-gaap"]
        gross_rows = facts.pop("GrossProfit")["units"]["USD"]
        revenue_rows = facts[
            "RevenueFromContractWithCustomerExcludingAssessedTax"
        ]["units"]["USD"]
        facts["CostOfRevenue"] = {
            "units": {
                "USD": [
                    {**revenue, "val": revenue["val"] - gross["val"]}
                    for revenue, gross in zip(revenue_rows, gross_rows)
                ]
            }
        }

        factors = {
            factor.name: factor
            for factor in compute_accounting_factors(companyfacts, date(2024, 3, 1))
        }
        profitability = factors["gross_profitability"]
        stability = factors["gross_margin_stability_5y"]
        self.assertIsNotNone(profitability.value)
        self.assertIn("derived", profitability.detail)
        self.assertEqual(
            [fact.concept for fact in profitability.inputs],
            ["revenue", "cost_of_revenue", "assets"],
        )
        self.assertIsNotNone(stability.value)

        piotroski = compute_piotroski(
            companyfacts,
            date(2024, 3, 1),
            split_adjusted_share_growth=0,
        )
        margin = next(
            component
            for component in piotroski.components
            if component.name == "improving_gross_margin"
        )
        self.assertIsNotNone(margin.passed)
        self.assertIn("cost_of_revenue", {fact.concept for fact in margin.inputs})

    def test_complete_chs_12_month_equation(self) -> None:
        inputs = ChsInputs(
            nimtaavg=-0.02,
            tlmta=0.6,
            exretavg=-0.05,
            sigma=0.8,
            rsize=-10.0,
            cashmta=0.1,
            mb=2.0,
            price=1.5,
        )
        expected = (
            -9.164
            - 20.264 * inputs.nimtaavg
            + 1.416 * inputs.tlmta
            - 7.129 * inputs.exretavg
            + 1.411 * inputs.sigma
            - 0.045 * inputs.rsize
            - 2.132 * inputs.cashmta
            + 0.075 * inputs.mb
            - 0.058 * inputs.price
        )

        score = compute_chs_12m(inputs)
        self.assertAlmostEqual(score.log_odds, expected)
        self.assertAlmostEqual(
            score.conditional_failure_probability,
            1 / (1 + math.exp(-expected)),
        )
        self.assertAlmostEqual(score.contributions["sigma"], 1.411 * inputs.sigma)
        self.assertFalse(score.to_dict()["cumulative_probability"])

    def test_chs_rejects_non_finite_inputs(self) -> None:
        inputs = ChsInputs(0, 0, 0, float("nan"), 0, 0, 0, 0)
        with self.assertRaisesRegex(ValueError, "sigma"):
            compute_chs_12m(inputs)


class DiagnosticTests(unittest.TestCase):
    def test_piotroski_exposes_nine_components_but_needs_adjusted_shares(self) -> None:
        companyfacts = full_factor_companyfacts()
        unresolved = compute_piotroski(companyfacts, date(2024, 3, 1))
        self.assertIsNone(unresolved.score)
        self.assertEqual(len(unresolved.components), 9)
        self.assertEqual(
            sum(component.passed is not None for component in unresolved.components),
            8,
        )
        no_shares = next(
            component
            for component in unresolved.components
            if component.name == "no_new_shares"
        )
        self.assertEqual(no_shares.reason, "missing_input")

        resolved = compute_piotroski(
            companyfacts,
            date(2024, 3, 1),
            split_adjusted_share_growth=-0.01,
        )
        self.assertIsNotNone(resolved.score)
        self.assertEqual(
            resolved.score,
            sum(component.passed is True for component in resolved.components),
        )
        self.assertFalse(resolved.to_dict()["feeds_grade"])
        self.assertTrue(
            next(
                component
                for component in resolved.components
                if component.name == "no_new_shares"
            ).passed
        )

    def test_piotroski_uses_the_papers_asset_denominators(self) -> None:
        result = compute_piotroski(
            full_factor_companyfacts(),
            date(2024, 3, 1),
            split_adjusted_share_growth=0,
        )
        components = {component.name: component for component in result.components}

        leverage = components["declining_long_term_leverage"]
        expected_leverage_change = 22_000_000 / (
            (133_100_000 + 121_000_000) / 2
        ) - 20_000_000 / ((121_000_000 + 110_000_000) / 2)
        self.assertAlmostEqual(leverage.metric, expected_leverage_change)

        turnover = components["improving_asset_turnover"]
        expected_turnover_change = (
            133_100_000 / 121_000_000 - 121_000_000 / 110_000_000
        )
        self.assertAlmostEqual(turnover.metric, expected_turnover_change)

    def test_cash_runway_uses_four_discrete_quarters(self) -> None:
        companyfacts = full_factor_companyfacts()
        cash_flows = companyfacts["facts"]["us-gaap"][
            "NetCashProvidedByUsedInOperatingActivities"
        ]["units"]["USD"]
        for quarter, start, end, value, filed in (
            ("Q1", "2024-01-01", "2024-03-31", -1_000_000, "2024-05-01"),
            ("Q2", "2024-04-01", "2024-06-30", -2_000_000, "2024-08-01"),
            ("Q3", "2024-07-01", "2024-09-30", -3_000_000, "2024-11-01"),
        ):
            cash_flows.append(
                {
                    "start": start,
                    "end": end,
                    "val": value,
                    "accn": f"0000000001-24-{quarter[-1]}00000",
                    "fy": 2024,
                    "fp": quarter,
                    "form": "10-Q",
                    "filed": filed,
                }
            )
        cash_flows.append(
            {
                "start": "2024-01-01",
                "end": "2024-12-31",
                "val": -12_000_000,
                "accn": "0000000001-25-000001",
                "fy": 2024,
                "fp": "FY",
                "form": "10-K",
                "filed": "2025-02-20",
            }
        )
        companyfacts["facts"]["us-gaap"][
            "CashAndCashEquivalentsAtCarryingValue"
        ]["units"]["USD"].append(
            {
                "end": "2024-12-31",
                "val": 5_000_000,
                "accn": "0000000001-25-000001",
                "fy": 2024,
                "fp": "FY",
                "form": "10-K",
                "filed": "2025-02-20",
            }
        )

        result = compute_cash_runway(companyfacts, date(2025, 3, 1))
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.trailing_operating_cash_flow, -12_000_000)
        self.assertEqual(result.months, 5.0)
        self.assertTrue(result.short_runway)
        self.assertEqual(result.cash_flow_inputs[0].method, "annual_less_q1_q2_q3")

    def test_late_filer_uses_only_nt_forms_in_the_last_eight_quarters(self) -> None:
        submissions = {
            "cik": "1",
            "filings": {
                "recent": {
                    "form": ["10-K", "NT 10-Q", "NT 10-K", "NT 10-Q"],
                    "filingDate": [
                        "2025-02-20",
                        "2024-11-01",
                        "2022-01-01",
                        "2025-04-01",
                    ],
                    "accessionNumber": ["a", "b", "c", "d"],
                }
            },
        }
        result = detect_late_filings(submissions, date(2025, 3, 1))
        self.assertEqual(result.status, "resolved")
        self.assertTrue(result.late_filer)
        self.assertEqual([filing.accession for filing in result.filings], ["b"])

    def test_absolute_flags_do_not_reuse_altman_for_fortress(self) -> None:
        companyfacts = full_factor_companyfacts()
        accounting = compute_accounting_factors(companyfacts, date(2024, 3, 1))
        factors = accounting + (
            FactorResult(
                "net_share_issuance", 0.25, "growth_rate", "lower", "2023-12-31"
            ),
            FactorResult("chs_12m", 0.20, "probability", "lower", "2023-12-31"),
        )
        late = detect_late_filings(
            {
                "cik": "1",
                "filings": {
                    "recent": {
                        "form": ["10-K"],
                        "filingDate": ["2024-02-20"],
                        "accessionNumber": ["a"],
                    }
                },
            },
            date(2024, 3, 1),
        )
        flags = build_company_flags(
            companyfacts,
            date(2024, 3, 1),
            factors,
            compute_cash_runway(companyfacts, date(2024, 3, 1)),
            late,
            chs_probability=0.20,
            distress_threshold=0.10,
        )
        by_name = {flag.name: flag for flag in flags}
        self.assertTrue(by_name["dilution"].active)
        self.assertTrue(by_name["distress"].active)
        self.assertFalse(by_name["fortress"].active)
        self.assertIn("Altman Z is not used", by_name["fortress"].detail)
        self.assertAlmostEqual(chs_distress_threshold(list(range(10))), 8.1)


class MarketInputTests(unittest.TestCase):
    def test_market_transforms_follow_chs_definitions(self) -> None:
        months = [(2023 + (month - 1) // 12, (month - 1) % 12 + 1) for month in range(1, 14)]
        stock = [
            PriceBar(date(year, month, 28), 100 * 1.10**offset)
            for offset, (year, month) in enumerate(months)
        ]
        benchmark = [
            PriceBar(date(year, month, 28), 100 * 1.05**offset)
            for offset, (year, month) in enumerate(months)
        ]
        excess = monthly_log_excess_returns(
            stock, benchmark, date(2024, 2, 15), months=12
        )
        expected_excess = math.log(1.10 / 1.05)
        self.assertEqual(len(excess), 12)
        self.assertTrue(all(abs(value - expected_excess) < 1e-12 for value in excess))
        self.assertAlmostEqual(weighted_excess_return(excess), expected_excess)
        self.assertAlmostEqual(weighted_nimta([0.04, 0.03, 0.02, 0.01]), 0.03266666666666667)

        with self.assertRaisesRegex(ValueError, "2023-07"):
            monthly_log_excess_returns(
                [bar for bar in stock if bar.day != date(2023, 7, 28)],
                benchmark,
                date(2024, 2, 15),
                months=12,
            )

    def test_sigma_requires_real_trading_observations(self) -> None:
        bars = [
            PriceBar(date(2024, 1, day), 100 * 1.01**offset)
            for offset, day in enumerate(range(2, 12))
        ]
        sigma = annualized_three_month_sigma(bars, date(2024, 1, 12))
        self.assertGreater(sigma, 0)

        with self.assertRaisesRegex(ValueError, "five nonzero"):
            annualized_three_month_sigma(bars[:4], date(2024, 1, 12))

    def test_raw_inputs_are_separate_from_universe_winsorization(self) -> None:
        raw = build_raw_chs_inputs(
            nimta_quarters=[0.04, 0.03, 0.02, 0.01],
            liabilities=60,
            cash_and_short_term_investments=10,
            market_equity=40,
            book_equity=20,
            sp500_market_value=10_000,
            monthly_excess_returns=[0.01] * 12,
            sigma=0.5,
            share_price=20,
        )
        self.assertAlmostEqual(raw.tlmta, 0.6)
        self.assertAlmostEqual(raw.cashmta, 0.1)
        self.assertAlmostEqual(raw.price, math.log(15))

        bounds = {name: (-1.0, 1.0) for name in vars(raw)}
        bounded = winsorize_chs_inputs(raw, bounds)
        self.assertEqual(bounded.rsize, -1.0)

    def test_liquidity_and_live_price_diagnostics(self) -> None:
        bars = [
            PriceBar(date(2024, 1, day), float(day), 100)
            for day in range(1, 32)
        ]
        self.assertEqual(
            median_dollar_adv(bars, date(2024, 1, 31)),
            median(day * 100 for day in range(1, 32)),
        )
        self.assertEqual(
            distance_from_52_week_high(bars, date(2024, 1, 31)),
            0.0,
        )

        months = [
            (2023 + (month - 1) // 12, (month - 1) % 12 + 1)
            for month in range(1, 14)
        ]
        monthly_bars = [
            PriceBar(date(year, month, 28), 100 * 1.10**offset)
            for offset, (year, month) in enumerate(months)
        ]
        self.assertAlmostEqual(
            momentum_12_1(monthly_bars, date(2024, 2, 15)),
            1.10**11 - 1,
        )

    def test_share_issuance_uses_provider_split_adjustment(self) -> None:
        companyfacts = full_factor_companyfacts()
        companyfacts["facts"]["dei"] = {
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        {
                            "end": "2022-12-31",
                            "val": 25_000_000,
                            "accn": "0000000001-23-000001",
                            "fy": 2022,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2023-02-20",
                        },
                        {
                            "end": "2023-12-31",
                            "val": 100_000_000,
                            "accn": "0000000001-24-000001",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-02-20",
                        },
                    ]
                }
            }
        }
        raw = [
            PriceBar(date(2022, 12, 30), 400),
            PriceBar(date(2023, 12, 29), 110),
        ]
        adjusted = [
            PriceBar(date(2022, 12, 30), 100),
            PriceBar(date(2023, 12, 29), 110),
        ]
        result = compute_net_share_issuance(
            companyfacts, date(2024, 3, 1), raw, adjusted
        )
        self.assertAlmostEqual(result.value, 0.0)
        self.assertIn("prior=4", result.detail)

    def test_full_chs_raw_assembly_is_auditable_and_does_not_impute(self) -> None:
        companyfacts = full_factor_companyfacts()
        facts = companyfacts["facts"]["us-gaap"]
        net_income = facts["NetIncomeLoss"]["units"]["USD"]
        for quarter, start, end, filed in (
            ("Q1", "2024-01-01", "2024-03-31", "2024-05-01"),
            ("Q2", "2024-04-01", "2024-06-30", "2024-08-01"),
            ("Q3", "2024-07-01", "2024-09-30", "2024-11-01"),
        ):
            net_income.append(
                {
                    "start": start,
                    "end": end,
                    "val": 1_000_000,
                    "accn": f"0000000001-24-{quarter[-1]}00000",
                    "fy": 2024,
                    "fp": quarter,
                    "form": "10-Q",
                    "filed": filed,
                }
            )
        net_income.append(
            {
                "start": "2024-01-01",
                "end": "2024-12-31",
                "val": 4_000_000,
                "accn": "0000000001-25-000001",
                "fy": 2024,
                "fp": "FY",
                "form": "10-K",
                "filed": "2025-02-20",
            }
        )
        for tag, value in (
            ("Liabilities", 100_000_000),
            ("StockholdersEquity", 50_000_000),
        ):
            entries = facts[tag]["units"]["USD"]
            for quarter, end, filed in (
                ("Q1", "2024-03-31", "2024-05-01"),
                ("Q2", "2024-06-30", "2024-08-01"),
                ("Q3", "2024-09-30", "2024-11-01"),
                ("FY", "2024-12-31", "2025-02-20"),
            ):
                entries.append(
                    {
                        "end": end,
                        "val": value,
                        "accn": f"0000000001-25-{quarter[-1]}00000",
                        "fy": 2024,
                        "fp": quarter,
                        "form": "10-K" if quarter == "FY" else "10-Q",
                        "filed": filed,
                    }
                )
        facts["CashCashEquivalentsAndShortTermInvestments"] = {
            "units": {
                "USD": [
                    {
                        "end": end,
                        "val": 20_000_000,
                        "accn": f"0000000001-25-{quarter[-1]}00000",
                        "fy": 2024,
                        "fp": quarter,
                        "form": "10-K" if quarter == "FY" else "10-Q",
                        "filed": filed,
                    }
                    for quarter, end, filed in (
                        ("Q1", "2024-03-31", "2024-05-01"),
                        ("Q2", "2024-06-30", "2024-08-01"),
                        ("Q3", "2024-09-30", "2024-11-01"),
                        ("FY", "2024-12-31", "2025-02-20"),
                    )
                ]
            }
        }
        companyfacts["facts"]["dei"] = {
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        {
                            "end": end,
                            "val": 10_000_000,
                            "accn": f"0000000001-25-{quarter[-1]}00000",
                            "fy": 2024,
                            "fp": quarter,
                            "form": "10-K" if quarter == "FY" else "10-Q",
                            "filed": filed,
                        }
                        for quarter, end, filed in (
                            ("Q1", "2024-03-31", "2024-05-01"),
                            ("Q2", "2024-06-30", "2024-08-01"),
                            ("Q3", "2024-09-30", "2024-11-01"),
                            ("FY", "2024-12-31", "2025-02-20"),
                        )
                    ]
                }
            }
        }
        months = [
            (2024 + (number - 1) // 12, (number - 1) % 12 + 1)
            for number in range(1, 15)
        ]
        stock = [
            PriceBar(date(year, month, 28), 10 * 1.01**offset, 1_000_000)
            for offset, (year, month) in enumerate(months)
        ]
        benchmark = [
            PriceBar(date(year, month, 28), 100 * 1.005**offset, 1_000_000)
            for offset, (year, month) in enumerate(months)
        ]
        stock.extend(
            PriceBar(date(2025, 2, day), 11 + day / 100, 1_000_000)
            for day in range(15, 28)
        )
        stock.sort(key=lambda bar: bar.day)

        result = build_company_chs_raw(
            companyfacts,
            date(2025, 3, 1),
            stock,
            stock,
            benchmark,
            sp500_market_value=50_000_000_000_000,
        )
        self.assertEqual(result.status, "resolved")
        self.assertIsNotNone(result.inputs)
        self.assertEqual(result.accounting_period_end, "2024-12-31")
        self.assertEqual(len(result.accounting_receipts["shares_outstanding"]), 4)
        self.assertFalse(result.to_dict()["imputed"])

        bounds = pooled_chs_bounds([result.inputs, result.inputs])
        self.assertEqual(bounds["sigma"], (result.inputs.sigma, result.inputs.sigma))

    def test_implied_expectations_panel_stays_separate_and_has_no_verdict(self) -> None:
        companyfacts = full_factor_companyfacts()
        companyfacts["facts"]["dei"] = {
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        {
                            "end": f"{year}-12-31",
                            "val": value,
                            "accn": f"0000000001-{str(year + 1)[-2:]}-000001",
                            "fy": year,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": f"{year + 1}-02-20",
                        }
                        for year, value in (
                            (2018, 10_000_000),
                            (2019, 11_000_000),
                            (2020, 12_000_000),
                            (2021, 13_000_000),
                            (2022, 14_000_000),
                            (2023, 15_000_000),
                        )
                    ]
                }
            }
        }
        months = [
            (2018 + number // 12, number % 12 + 1)
            for number in range(11, 74)
        ]
        stock = [
            PriceBar(date(year, month, 28), 10 * 1.01**offset, 1_000_000)
            for offset, (year, month) in enumerate(months)
        ]
        total_return = [
            PriceBar(bar.day, bar.close * 1.001**offset, bar.volume)
            for offset, bar in enumerate(stock)
        ]
        result = compute_implied_expectations(
            companyfacts,
            date(2024, 3, 1),
            stock,
            stock,
            total_return,
            total_return,
        )
        output = result.to_dict()
        self.assertFalse(output["feeds_grade"])
        self.assertIsNone(output["verdict"])
        self.assertFalse(output["predictive_claim"])
        self.assertEqual(output["coverage"], {"resolved": 8, "wanted": 8})
        metrics = {metric.name: metric for metric in result.metrics}
        self.assertAlmostEqual(metrics["sector_relative_strength"].value, 0.0)
        self.assertAlmostEqual(
            metrics["composite_equity_issuance_5y"].value,
            math.log(1.5) - math.log(1.001 ** (len(stock) - 1)),
        )
        self.assertIn(
            "current_total_return_price",
            metrics["composite_equity_issuance_5y"].market_inputs,
        )
        self.assertIn("enterprise_value", metrics["ebit_to_enterprise_value"].market_inputs)
        revalued = revalue_implied_expectations(
            result.live_inputs,
            result.live_inputs["price"]["close"] * 2,
            price_timestamp="2024-03-01T15:00:00Z",
        )
        self.assertEqual(revalued["status"], "resolved")
        self.assertAlmostEqual(
            revalued["market_equity"], result.live_inputs["market_equity"] * 2
        )
        self.assertAlmostEqual(
            revalued["metrics"]["book_to_price"],
            metrics["book_to_price"].value / 2,
        )
        self.assertFalse(revalued["feeds_grade"])
        self.assertIsNone(revalued["verdict"])


class MarketDataTests(unittest.TestCase):
    def test_market_history_uses_only_a_fully_closed_session(self) -> None:
        before_close = datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc)
        after_delay = datetime(2026, 8, 21, 20, 16, tzinfo=timezone.utc)
        self.assertEqual(
            _market_history_end(date(2026, 8, 21), before_close),
            "2026-08-20T23:59:59Z",
        )
        self.assertEqual(
            _market_history_end(date(2026, 8, 21), after_delay),
            "2026-08-21T20:00:00Z",
        )

    def test_alpaca_shape_and_pagination_are_parsed(self) -> None:
        class FakeClient(AlpacaClient):
            def __init__(self) -> None:
                super().__init__("key", "secret")
                self.calls: list[dict[str, str]] = []

            def _request_json(self, parameters: dict[str, str]) -> dict[str, object]:
                self.calls.append(parameters)
                if "page_token" not in parameters:
                    return {
                        "bars": {
                            "AAPL": [
                                {"t": "2024-01-02T05:00:00Z", "c": 100, "v": 10}
                            ]
                        },
                        "next_page_token": "next",
                    }
                return {
                    "bars": {
                        "AAPL": [
                            {"t": "2024-01-03T05:00:00Z", "c": 101, "v": 11}
                        ]
                    },
                    "next_page_token": None,
                }

        client = FakeClient()
        bars = client.fetch_bars(
            ["AAPL"], date(2024, 1, 1), date(2024, 1, 4), adjustment="split"
        )
        self.assertEqual([bar.close for bar in bars["AAPL"]], [100, 101])
        self.assertEqual(client.calls[1]["page_token"], "next")
        self.assertEqual(client.calls[0]["adjustment"], "split")
        self.assertEqual(client.calls[0]["end"], "2024-01-04T23:59:59Z")

    def test_sqlite_market_store_and_universe_sync(self) -> None:
        class FakeClient:
            def fetch_bars(
                self,
                symbols: list[str],
                start: date,
                end: date,
                *,
                adjustment: str,
                feed: str,
            ) -> dict[str, list[PriceBar]]:
                if adjustment == "all" and symbols == ["BRK.B"]:
                    return {}
                return {
                    symbol: [PriceBar(date(2024, 1, 2), 10, 100)]
                    for symbol in symbols
                }

        with tempfile.TemporaryDirectory() as directory:
            with MarketStore(Path(directory) / "market.sqlite3") as store:
                result = sync_market_data(
                    ["BRK-B", "AAPL"],
                    FakeClient(),
                    store,
                    date(2024, 1, 1),
                    date(2024, 1, 3),
                    batch_size=1,
                )
                self.assertEqual(result.requested_tickers, 2)
                self.assertEqual(result.tickers_with_data, 2)
                self.assertEqual(result.complete_tickers, 1)
                self.assertEqual(
                    result.tickers_with_data_by_adjustment,
                    {"raw": 2, "split": 2, "all": 1},
                )
                self.assertEqual(result.bars_written, 5)
                self.assertEqual(store.bars("BRK-B", adjustment="raw")[0].close, 10)
                self.assertEqual(
                    store.bars("BRK-B", adjustment="split")[0].volume, 100
                )
        self.assertEqual(alpaca_symbol("brk-b"), "BRK.B")

    def test_market_sync_backfills_new_symbols_and_refreshes_a_short_overlap(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[tuple[str, ...], date, str]] = []

            def fetch_bars(
                self,
                symbols: list[str],
                start: date,
                end: date,
                *,
                adjustment: str,
                feed: str,
            ) -> dict[str, list[PriceBar]]:
                self.calls.append((tuple(symbols), start, adjustment))
                return {
                    symbol: [PriceBar(end, 10, 100)] for symbol in symbols
                }

        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            with MarketStore(Path(directory) / "market.sqlite3") as store:
                store.put_bars(
                    "OLD",
                    [PriceBar(date(2024, 1, 20), 9, 90)],
                    adjustment="split",
                    feed="sip",
                )
                result = sync_market_data(
                    ["NEW", "OLD"],
                    client,
                    store,
                    date(2023, 1, 1),
                    date(2024, 1, 31),
                    adjustments=("split",),
                )

        calls = {symbols: start for symbols, start, _ in client.calls}
        self.assertEqual(calls[("NEW",)], date(2023, 1, 1))
        self.assertEqual(calls[("OLD",)], date(2024, 1, 13))
        self.assertEqual(result.complete_tickers, 2)

    def test_malformed_alpaca_rows_are_ignored(self) -> None:
        parsed = parse_alpaca_bars(
            {
                "bars": {
                    "TEST": [
                        {"t": "2024-01-02T00:00:00Z", "c": "12.5", "v": 2},
                        {"t": "bad", "c": 3},
                        {"t": "2024-01-03T00:00:00Z", "c": -1},
                        {"t": "2024-01-04T00:00:00Z", "c": "Infinity"},
                        {"t": "2024-01-05T00:00:00Z", "c": 4, "v": -1},
                    ]
                }
            }
        )
        self.assertEqual(len(parsed["TEST"]), 1)
        self.assertEqual(parsed["TEST"][0].close, 12.5)

    def test_market_sync_refuses_provider_symbol_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with MarketStore(Path(directory) / "market.sqlite3") as store:
                with self.assertRaisesRegex(MarketDataError, "same Alpaca symbol"):
                    sync_market_data(
                        ["ABC-D", "ABC.D"],
                        object(),
                        store,
                        date(2024, 1, 1),
                        date(2024, 1, 3),
                    )

    def test_live_snapshots_are_cached_and_expose_timestamped_context(self) -> None:
        class FakeClient:
            def fetch_snapshots(
                self, symbols: list[str], *, feed: str
            ) -> dict[str, dict[str, object]]:
                return {
                    symbol: {
                        "latestTrade": {"t": "2024-01-03T15:00:00Z", "p": 12.0},
                        "latestQuote": {"bp": 11.9, "ap": 12.1},
                        "minuteBar": {"t": "2024-01-03T14:59:00Z", "c": 12.0},
                        "dailyBar": {"t": "2024-01-03T05:00:00Z", "c": 12.0},
                        "prevDailyBar": {"t": "2024-01-02T05:00:00Z", "c": 10.0},
                    }
                    for symbol in symbols
                }

        with tempfile.TemporaryDirectory() as directory:
            with MarketStore(Path(directory) / "market.sqlite3") as store:
                result = sync_market_snapshots(
                    ["BRK-B", "AAPL"],
                    FakeClient(),
                    store,
                    retrieved_at=datetime(2024, 1, 3, 15, tzinfo=timezone.utc),
                )
                snapshot = store.snapshot("BRK-B")
                self.assertIsInstance(snapshot, MarketSnapshot)
                output = snapshot.to_dict()
                self.assertEqual(output["price"], 12.0)
                self.assertAlmostEqual(output["day_change_percent"], 0.2)
                self.assertAlmostEqual(output["quoted_spread_percent"], 0.2 / 12)
                self.assertFalse(output["feeds_grade"])
                self.assertEqual(result.snapshots_written, 2)


class ForecastTests(unittest.TestCase):
    def test_append_only_forecast_log_records_settles_and_scores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "forecast_log.jsonl"
            with MarketStore(root / "market.sqlite3") as store:
                for symbol, start, end in (
                    ("TEST", 100.0, 120.0),
                    ("SPY", 100.0, 105.0),
                ):
                    store.put_bars(
                        symbol,
                        [
                            PriceBar(date(2024, 1, 2), start),
                            PriceBar(date(2024, 4, 2), end),
                        ],
                        adjustment="all",
                        feed="sip",
                    )
                call = record_forecast(
                    log,
                    store,
                    "test",
                    date(2024, 1, 2),
                    horizon_days=90,
                    probability=0.7,
                    thesis="The company will outgrow the broad market.",
                    grade_at_call="B",
                    called_at=datetime(2024, 1, 2, 22, tzinfo=timezone.utc),
                )
                result = settle_due_forecasts(
                    log,
                    store,
                    date(2024, 4, 2),
                    resolved_at=datetime(2024, 4, 2, 22, tzinfo=timezone.utc),
                )
                second = settle_due_forecasts(log, store, date(2024, 4, 3))

            events = read_forecast_events(log)
            self.assertEqual([event["event"] for event in events], ["call", "resolution"])
            self.assertEqual(events[1]["call_id"], call["id"])
            self.assertEqual(events[1]["outcome"], 1)
            self.assertAlmostEqual(events[1]["brier"], 0.09)
            self.assertEqual(result["settled"], 1)
            self.assertEqual(second["settled"], 0)
            scorecard = forecast_scorecard(log)
            self.assertEqual(scorecard["resolved"], 1)
            self.assertEqual(scorecard["pending"], 0)
            self.assertAlmostEqual(scorecard["model"]["mean_brier"], 0.09)
            self.assertEqual(scorecard["calibration"][0]["range"], "0.7-0.8")
            published = publish_forecast_artifact(
                log,
                root / "data" / "forecasts.json",
                date(2024, 4, 2),
                generated_at=datetime(2024, 4, 2, 23, tzinfo=timezone.utc),
            )
            self.assertEqual(published["schema_version"], 1)
            self.assertEqual(published["as_of"], "2024-04-02")
            self.assertEqual(published["scorecard"]["resolved"], 1)
            self.assertEqual(len(published["events"]), 2)
            self.assertEqual(
                json.loads((root / "data" / "forecasts.json").read_text()),
                published,
            )

    def test_forecast_call_refuses_invalid_or_unpriced_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with MarketStore(root / "market.sqlite3") as store:
                with self.assertRaisesRegex(ValueError, "strictly between"):
                    record_forecast(
                        root / "forecast_log.jsonl",
                        store,
                        "TEST",
                        date(2024, 1, 2),
                        horizon_days=90,
                        probability=1.0,
                        thesis="Certain claim",
                    )
                with self.assertRaisesRegex(ValueError, "no common"):
                    record_forecast(
                        root / "forecast_log.jsonl",
                        store,
                        "TEST",
                        date(2024, 1, 2),
                        horizon_days=90,
                        probability=0.5,
                        thesis="Unpriced claim",
                    )

    def test_forecast_log_refuses_semantically_invalid_events(self) -> None:
        invalid_events = (
            ({"event": "call", "id": "", "ticker": "TEST"}, "call id"),
            (
                {
                    "event": "call",
                    "id": "call-1",
                    "ticker": "TEST",
                    "target_date": "not-a-date",
                    "p_outperform_spy": 0.5,
                    "price_basis": {"date": "2024-01-02"},
                },
                "target date",
            ),
            (
                {
                    "event": "call",
                    "id": "call-1",
                    "ticker": "TEST",
                    "target_date": "2024-04-01",
                    "p_outperform_spy": float("inf"),
                    "price_basis": {"date": "2024-01-02"},
                },
                "probability",
            ),
            ({"event": "resolution", "call_id": "call-1", "outcome": 2}, "outcome"),
        )
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "forecast_log.jsonl"
            for event, message in invalid_events:
                with self.subTest(message=message):
                    log.write_text(
                        json.dumps(event, allow_nan=True) + "\n", encoding="utf-8"
                    )
                    with self.assertRaisesRegex(ValueError, message):
                        read_forecast_events(log)


class SecClientTests(unittest.TestCase):
    def test_cached_ticker_and_submission_files_need_no_network_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / "company_tickers.json").write_text(
                json.dumps(
                    {
                        "0": {
                            "ticker": "TEST",
                            "cik_str": 320193,
                            "title": "Fixture Corporation",
                        }
                    }
                ),
                encoding="utf-8",
            )
            (cache / "CIK0000320193-submissions.json").write_text(
                json.dumps({"cik": "0000320193", "filings": {"recent": {}}}),
                encoding="utf-8",
            )

            client = SecClient(cache)
            company = client.resolve_ticker("test")
            submissions = client.submissions(company.cik)

            self.assertEqual(company.name, "Fixture Corporation")
            self.assertEqual(submissions["cik"], "0000320193")

    def test_uncached_live_request_requires_real_contact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = SecClient(directory, user_agent=None)
            with self.assertRaisesRegex(SecError, "real contact email"):
                client.companyfacts(320193)
            invalid = SecClient(directory, user_agent="assay bot")
            with self.assertRaisesRegex(SecError, "real contact email"):
                invalid.companyfacts(320193)
            placeholder = SecClient(
                directory, user_agent="assay research your-email@example.com"
            )
            with self.assertRaisesRegex(SecError, "real contact email"):
                placeholder.companyfacts(320193)
        with self.assertRaisesRegex(ValueError, "below 10"):
            SecClient("unused", requests_per_second=10)

    def test_bulk_store_reads_archives_by_zero_padded_cik(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            companyfacts_path = Path(directory) / "companyfacts.zip"
            submissions_path = Path(directory) / "submissions.zip"
            with zipfile.ZipFile(companyfacts_path, "w") as archive:
                archive.writestr("CIK0000320193.json", json.dumps({"cik": 320193}))
            with zipfile.ZipFile(submissions_path, "w") as archive:
                archive.writestr(
                    "CIK0000320193.json", json.dumps({"cik": "0000320193"})
                )

            with BulkSecStore(companyfacts_path, submissions_path) as store:
                self.assertTrue(store.has_companyfacts(320193))
                self.assertEqual(store.companyfacts(320193)["cik"], 320193)
                self.assertEqual(store.submissions(320193)["cik"], "0000320193")

    def test_all_ticker_cli_uses_cached_bulk_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            bulk = cache / "bulk"
            bulk.mkdir(parents=True)
            (cache / "company_tickers.json").write_text(
                json.dumps(
                    {
                        "0": {
                            "ticker": "TEST",
                            "cik_str": 1,
                            "title": "Fixture Corporation",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with zipfile.ZipFile(bulk / "companyfacts.zip", "w") as archive:
                archive.writestr("CIK0000000001.json", "{}")
            with zipfile.ZipFile(bulk / "submissions.zip", "w") as archive:
                archive.writestr(
                    "CIK0000000001.json",
                    json.dumps(
                        {
                            "sic": "2834",
                            "tickers": ["TEST"],
                            "exchanges": ["Nasdaq"],
                            "filings": {"recent": {"form": ["10-K"]}},
                        }
                    ),
                )
            output = Path(directory) / "output"
            args = build_parser().parse_args(
                [
                    "--all",
                    "--cache-dir",
                    str(cache),
                    "--market-db",
                    str(Path(directory) / "market.sqlite3"),
                    "--output-dir",
                    str(output),
                ]
            )
            report = run_all(args)
            self.assertEqual(report["tickers"], 1)
            self.assertEqual(report["eligible"], 1)
            self.assertTrue((output / "universe.json").exists())


class NightlyWorkflowTests(unittest.TestCase):
    def test_nightly_schedule_is_timezone_aware_and_never_publishes_to_main(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "nightly.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('cron: "0 3 * * *"', workflow)
        self.assertIn('timezone: "America/New_York"', workflow)
        self.assertIn("scripts/nightly.sh", workflow)
        self.assertIn("HEAD:data", workflow)
        self.assertNotIn("HEAD:main", workflow)


class UniverseTests(unittest.TestCase):
    def test_ticker_file_is_deduplicated_and_sorted(self) -> None:
        data = {
            "0": {"ticker": "ZZZ", "cik_str": 2, "title": "Zed"},
            "1": {"ticker": "AAA", "cik_str": 1, "title": "Aye"},
            "2": {"ticker": "AAA", "cik_str": 1, "title": "Duplicate"},
        }
        companies = companies_from_ticker_file(data)
        self.assertEqual([company.ticker for company in companies], ["AAA", "ZZZ"])

    def test_operating_company_is_classified_before_coverage(self) -> None:
        company = Company("TEST", 1, "Test")
        submissions = {
            "name": "Test Corporation",
            "sic": "3571",
            "sicDescription": "Electronic Computers",
            "tickers": ["TEST"],
            "exchanges": ["Nasdaq"],
            "filings": {"recent": {"form": ["8-K", "10-K", "10-Q"]}},
        }
        entry = classify_company(company, submissions, has_companyfacts=True)
        self.assertEqual(entry.status, "eligible")
        self.assertEqual(entry.sector, "business_equipment")

    def test_foreign_financial_and_otc_issuers_are_excluded(self) -> None:
        company = Company("TEST", 1, "Test")
        common = {"tickers": ["TEST"], "exchanges": ["NYSE"]}
        foreign = classify_company(
            company,
            {**common, "sic": "2834", "filings": {"recent": {"form": ["20-F"]}}},
        )
        transitioned_foreign = classify_company(
            company,
            {
                **common,
                "sic": "2834",
                "filings": {
                    "recent": {
                        "form": ["20-F", "10-K"],
                        "filingDate": ["2025-03-01", "2024-03-01"],
                    }
                },
            },
        )
        financial = classify_company(
            company,
            {**common, "sic": "6021", "filings": {"recent": {"form": ["10-K"]}}},
        )
        otc = classify_company(
            company,
            {
                **common,
                "sic": "2834",
                "exchanges": ["OTC"],
                "filings": {"recent": {"form": ["10-K"]}},
            },
        )
        self.assertEqual(foreign.reason, "foreign_issuer")
        self.assertEqual(transitioned_foreign.reason, "foreign_issuer")
        self.assertEqual(financial.reason, "financial_or_reit")
        self.assertEqual(otc.reason, "otc_or_unlisted")
        self.assertEqual(sector_for_sic(2834), "healthcare")

    def test_exhaustive_universe_build_preserves_every_ticker(self) -> None:
        ticker_data = {
            "0": {"ticker": "AAA", "cik_str": 1, "title": "Eligible"},
            "1": {"ticker": "BBB", "cik_str": 2, "title": "Foreign"},
            "2": {"ticker": "CCC", "cik_str": 3, "title": "No facts"},
            "3": {"ticker": "BAD", "cik_str": "not-a-cik", "title": "Bad mapping"},
            "4": {"ticker": "DUP", "cik_str": 4, "title": "Conflict"},
            "5": {"ticker": "DUP", "cik_str": 5, "title": "Conflict"},
        }
        submissions = {
            1: {
                "sic": "2834",
                "tickers": ["AAA"],
                "exchanges": ["Nasdaq"],
                "filings": {"recent": {"form": ["10-K"]}},
            },
            2: {
                "sic": "2834",
                "tickers": ["BBB"],
                "exchanges": ["NYSE"],
                "filings": {"recent": {"form": ["20-F"]}},
            },
            3: {
                "sic": "2834",
                "tickers": ["CCC"],
                "exchanges": ["Nasdaq"],
                "filings": {"recent": {"form": ["10-K"]}},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            companyfacts_path = Path(directory) / "companyfacts.zip"
            submissions_path = Path(directory) / "submissions.zip"
            with zipfile.ZipFile(companyfacts_path, "w") as archive:
                archive.writestr("CIK0000000001.json", "{}")
            with zipfile.ZipFile(submissions_path, "w") as archive:
                for cik, payload in submissions.items():
                    archive.writestr(f"CIK{cik:010d}.json", json.dumps(payload))

            with BulkSecStore(companyfacts_path, submissions_path) as store:
                build = build_universe(ticker_data, store, date(2024, 1, 1))
            report = build.to_dict()
            self.assertEqual(report["summary"]["tickers"], 5)
            self.assertEqual(report["summary"]["eligible"], 1)
            self.assertEqual(report["summary"]["excluded"], 1)
            self.assertEqual(report["summary"]["unresolved"], 3)
            reasons = {entry.ticker: entry.reason for entry in build.entries}
            self.assertEqual(reasons["BAD"], "invalid_cik_mapping")
            self.assertEqual(reasons["DUP"], "conflicting_cik_mappings")

            path = emit_universe(build, Path(directory) / "output")
            emitted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(emitted["tickers"]), 5)

    def test_full_pipeline_emits_one_artifact_per_ticker(self) -> None:
        ticker_data = {
            "0": {"ticker": "AAA", "cik_str": 1, "title": "Eligible"},
            "1": {"ticker": "BBB", "cik_str": 2, "title": "Foreign"},
            "2": {"ticker": "CCC", "cik_str": 3, "title": "No facts"},
        }
        submissions = {
            1: {
                "cik": "1",
                "sic": "2834",
                "tickers": ["AAA"],
                "exchanges": ["Nasdaq"],
                "filings": {
                    "recent": {
                        "form": ["10-K"],
                        "filingDate": ["2023-02-20"],
                        "accessionNumber": ["a"],
                    }
                },
            },
            2: {
                "cik": "2",
                "sic": "2834",
                "tickers": ["BBB"],
                "exchanges": ["NYSE"],
                "filings": {"recent": {"form": ["20-F"]}},
            },
            3: {
                "cik": "3",
                "sic": "2834",
                "tickers": ["CCC"],
                "exchanges": ["Nasdaq"],
                "filings": {"recent": {"form": ["10-K"]}},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            companyfacts_path = root / "companyfacts.zip"
            submissions_path = root / "submissions.zip"
            with zipfile.ZipFile(companyfacts_path, "w") as archive:
                archive.writestr(
                    "CIK0000000001.json", json.dumps(full_factor_companyfacts())
                )
            with zipfile.ZipFile(submissions_path, "w") as archive:
                for cik, payload in submissions.items():
                    archive.writestr(f"CIK{cik:010d}.json", json.dumps(payload))
            output = root / "output"
            output.mkdir()
            (output / "stale.json").write_text("{}", encoding="utf-8")
            with (
                BulkSecStore(companyfacts_path, submissions_path) as sec_store,
                MarketStore(root / "market.sqlite3") as market_store,
            ):
                market_store.put_snapshot(
                    MarketSnapshot(
                        "AAA",
                        "iex",
                        "2024-03-01T15:00:00Z",
                        {
                            "latestTrade": {
                                "t": "2024-03-01T14:59:59Z",
                                "p": 12.0,
                            },
                            "prevDailyBar": {"c": 10.0},
                        },
                    )
                )
                result = run_full_pipeline(
                    ticker_data,
                    sec_store,
                    market_store,
                    date(2024, 3, 1),
                    output,
                )
            self.assertEqual(result.summary["processed"], 3)
            self.assertEqual(result.summary["ticker_artifacts"], 3)
            self.assertTrue((output / "index.json").exists())
            self.assertTrue((output / "methodology.json").exists())
            self.assertTrue((output / "tickers" / "AAA.json").exists())
            self.assertTrue((output / "tickers" / "BBB.json").exists())
            self.assertTrue((output / "tickers" / "CCC.json").exists())
            self.assertTrue((output / "sectors" / "index.json").exists())
            self.assertTrue((output / "peer_groups" / "index.json").exists())
            self.assertTrue((output / "audit" / "coverage.json").exists())
            self.assertTrue(
                (output / "audit" / "construction_sensitivity.json").exists()
            )
            self.assertFalse((output / "stale.json").exists())
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(len(index["tickers"]), 3)
            eligible_report = json.loads(
                (output / "tickers" / "AAA.json").read_text(encoding="utf-8")
            )
            self.assertEqual(eligible_report["status"], "analyzed")
            self.assertEqual(len(eligible_report["factors"]), 14)
            self.assertIn("grade_sensitivity", eligible_report)
            self.assertEqual(eligible_report["market_snapshot"]["price"], 12.0)
            self.assertFalse(eligible_report["market_snapshot"]["feeds_grade"])
            sector = json.loads(
                (output / "sectors" / "healthcare.json").read_text(encoding="utf-8")
            )
            self.assertFalse(sector["display_rules"]["quadrants"])
            methodology = json.loads(
                (output / "methodology.json").read_text(encoding="utf-8")
            )
            self.assertFalse(methodology["grade"]["live_snapshot_feeds_grade"])
            self.assertEqual(
                len(methodology["grade"]["factor_definitions"]), 14
            )
            self.assertIn(
                "revenue minus cost of revenue",
                methodology["grade"]["factor_definitions"][
                    "gross_profitability"
                ]["notes"],
            )
            self.assertFalse(methodology["chs_12m"]["imputation"])

            verification = verify_output_tree(output)
            self.assertEqual(verification["status"], "verified")
            self.assertEqual(verification["tickers"], 3)
            self.assertEqual(verification["analyzed"], 1)
            (output / "tickers" / "UNEXPECTED.json").write_text(
                "{}\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "artifact set"):
                verify_output_tree(output)

    def test_multiple_share_classes_fail_closed_on_share_based_inputs(self) -> None:
        ticker_data = {
            "0": {"ticker": "AAA", "cik_str": 1, "title": "Class A"},
            "1": {"ticker": "AAB", "cik_str": 1, "title": "Class B"},
        }
        submissions = {
            "cik": "1",
            "sic": "2834",
            "tickers": ["AAA", "AAB"],
            "exchanges": ["Nasdaq", "Nasdaq"],
            "filings": {
                "recent": {
                    "form": ["10-K"],
                    "filingDate": ["2024-02-20"],
                    "accessionNumber": ["a"],
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            companyfacts_path = root / "companyfacts.zip"
            submissions_path = root / "submissions.zip"
            with zipfile.ZipFile(companyfacts_path, "w") as archive:
                archive.writestr(
                    "CIK0000000001.json", json.dumps(full_factor_companyfacts())
                )
            with zipfile.ZipFile(submissions_path, "w") as archive:
                archive.writestr("CIK0000000001.json", json.dumps(submissions))
            with (
                BulkSecStore(companyfacts_path, submissions_path) as sec_store,
                MarketStore(root / "market.sqlite3") as market_store,
            ):
                run_full_pipeline(
                    ticker_data,
                    sec_store,
                    market_store,
                    date(2024, 3, 1),
                    root / "output",
                    sp500_market_value=50_000_000_000_000,
                )
            report = json.loads(
                (root / "output" / "tickers" / "AAA.json").read_text(
                    encoding="utf-8"
                )
            )
            issuance = next(
                factor
                for factor in report["factors"]
                if factor["name"] == "net_share_issuance"
            )
            self.assertEqual(report["market_scope"]["reason"], "multiple_share_classes")
            self.assertEqual(
                report["models"]["chs_12m"]["reason"], "multiple_share_classes"
            )
            self.assertEqual(issuance["status"], "unresolved")
            self.assertIn("one ticker class", issuance["detail"])

    def test_full_pipeline_resolves_chs_grades_and_curve_at_peer_scale(self) -> None:
        ticker_data = {
            str(index): {
                "ticker": f"T{index:03d}",
                "cik_str": index + 1,
                "title": f"Synthetic Company {index:03d}",
            }
            for index in range(100)
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            companyfacts_path = root / "companyfacts.zip"
            submissions_path = root / "submissions.zip"
            with zipfile.ZipFile(
                companyfacts_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for index in range(100):
                    archive.writestr(
                        f"CIK{index + 1:010d}.json",
                        json.dumps(chs_ready_companyfacts(index + 1, index)),
                    )
            with zipfile.ZipFile(
                submissions_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for index in range(100):
                    archive.writestr(
                        f"CIK{index + 1:010d}.json",
                        json.dumps(
                            {
                                "cik": str(index + 1),
                                "sic": "2834",
                                "tickers": [f"T{index:03d}"],
                                "exchanges": ["Nasdaq"],
                                "filings": {
                                    "recent": {
                                        "form": ["10-K"],
                                        "filingDate": ["2025-02-20"],
                                        "accessionNumber": [f"{index + 1}-25-1"],
                                    }
                                },
                            }
                        ),
                    )
            start = date(2023, 12, 1)
            days = [start + timedelta(days=3 * offset) for offset in range(153)]
            benchmark = [
                PriceBar(day, 100 * 1.001**offset, 10_000_000)
                for offset, day in enumerate(days)
            ]
            output = root / "output"
            with (
                BulkSecStore(companyfacts_path, submissions_path) as sec_store,
                MarketStore(root / "market.sqlite3") as market_store,
            ):
                for symbol in ("SPY", "XLV"):
                    market_store.put_bars(
                        symbol, benchmark, adjustment="all", feed="sip"
                    )
                for index in range(100):
                    ticker = f"T{index:03d}"
                    bars = [
                        PriceBar(
                            day,
                            (8 + index / 10) * (1.0012 + index / 1_000_000) ** offset,
                            1_000_000 + index * 100,
                        )
                        for offset, day in enumerate(days)
                    ]
                    for adjustment in ("raw", "split", "all"):
                        market_store.put_bars(
                            ticker, bars, adjustment=adjustment, feed="sip"
                        )
                result = run_full_pipeline(
                    ticker_data,
                    sec_store,
                    market_store,
                    date(2025, 3, 1),
                    output,
                    sp500_market_value=50_000_000_000_000,
                    sp500_market_value_as_of=date(2025, 2, 28),
                    sp500_market_value_source="authoritative-test-reference",
                )

            self.assertEqual(result.summary["processed"], 100)
            self.assertEqual(result.summary["chs_resolved"], 100)
            self.assertNotIn("unresolved", result.summary["grades"])
            report = json.loads(
                (output / "tickers" / "T050.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["coverage"], {"resolved": 14, "wanted": 14})
            self.assertEqual(report["grade"]["peer_count"], 100)
            self.assertEqual(report["models"]["chs_12m"]["stage"], "scored")
            self.assertTrue(report["models"]["chs_12m"]["accounting_receipts"])
            self.assertEqual(
                report["models"]["chs_12m"]["market_receipts"][
                    "sp500_market_value_reference"
                ]["as_of"],
                "2025-02-28",
            )
            peer_group = json.loads(
                (output / "peer_groups" / "healthcare.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(peer_group["ticker_count"], 100)
            self.assertEqual(peer_group["distribution_curve"]["status"], "resolved")
            self.assertEqual(len(peer_group["distribution_curve"]["points"]), 81)
            sector = json.loads(
                (output / "sectors" / "healthcare.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(sector["peer_cloud"]), 100)
            audit = json.loads(
                (output / "audit" / "coverage.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                audit["factor_computability_by_sector"]["healthcare"]["chs_12m"][
                    "resolved"
                ],
                100,
            )
            self.assertEqual(
                sum(
                    bucket["eligible"]
                    for bucket in audit[
                        "net_share_issuance_by_market_equity_bucket"
                    ].values()
                ),
                100,
            )
            sensitivity = json.loads(
                (output / "audit" / "construction_sensitivity.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                sensitivity["scenarios"][
                    "baseline_sector_rollup_equal_sleeves"
                ]["companies"],
                100,
            )
            self.assertIn("do not map directly", sensitivity["scope_note"])
            self.assertEqual(verify_output_tree(output)["analyzed"], 100)

            methodology_path = output / "methodology.json"
            methodology = json.loads(methodology_path.read_text(encoding="utf-8"))
            methodology["grade"]["minimum_peer_group"] = 99
            methodology_path.write_text(json.dumps(methodology), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "methodology"):
                verify_output_tree(output)
            methodology["grade"]["minimum_peer_group"] = 100
            methodology_path.write_text(json.dumps(methodology), encoding="utf-8")

            audit_path = output / "audit" / "coverage.json"
            audit["factor_computability_by_sector"]["healthcare"]["chs_12m"][
                "resolved"
            ] = 99
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "coverage audit"):
                verify_output_tree(output)
            audit["factor_computability_by_sector"]["healthcare"]["chs_12m"][
                "resolved"
            ] = 100
            audit_path.write_text(json.dumps(audit), encoding="utf-8")

            sensitivity_path = output / "audit" / "construction_sensitivity.json"
            baseline = "baseline_sector_rollup_equal_sleeves"
            sensitivity["scenarios"][baseline]["companies"] = 99
            sensitivity_path.write_text(json.dumps(sensitivity), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "construction-sensitivity"):
                verify_output_tree(output)
            sensitivity["scenarios"][baseline]["companies"] = 100
            sensitivity_path.write_text(json.dumps(sensitivity), encoding="utf-8")

            sector_path = output / "sectors" / "healthcare.json"
            sector["peer_cloud"][0]["financial_condition_percentile"] += 0.1
            sector_path.write_text(json.dumps(sector), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "peer cloud"):
                verify_output_tree(output)
            sector["peer_cloud"][0]["financial_condition_percentile"] -= 0.1
            sector_path.write_text(json.dumps(sector), encoding="utf-8")

            peer_group_path = output / "peer_groups" / "healthcare.json"
            peer_group["distribution_curve"]["points"][0]["y"] += 0.1
            peer_group_path.write_text(json.dumps(peer_group), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "distribution curve"):
                verify_output_tree(output)
            peer_group["distribution_curve"]["points"][0]["y"] -= 0.1
            peer_group_path.write_text(json.dumps(peer_group), encoding="utf-8")

            factor_name = "gross_profitability"
            factor_receipt = report["grade"]["factor_percentiles"][factor_name]
            factor_receipt["peer_count"] += 1
            (output / "tickers" / "T050.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "factor percentile"):
                verify_output_tree(output)
            factor_receipt["peer_count"] -= 1
            (output / "tickers" / "T050.json").write_text(
                json.dumps(report), encoding="utf-8"
            )

            index = json.loads(
                (output / "index.json").read_text(encoding="utf-8")
            )
            index_row = next(
                row for row in index["tickers"] if row["ticker"] == "T050"
            )
            report["grade"]["composite"] += 0.1
            index_row["composite"] += 0.1
            (output / "tickers" / "T050.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            (output / "index.json").write_text(json.dumps(index), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot be reproduced"):
                verify_output_tree(output)

            report["grade"]["composite"] -= 0.1
            index_row["composite"] -= 0.1
            index_row["grade"] = "wrong"
            (output / "tickers" / "T050.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            (output / "index.json").write_text(json.dumps(index), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "index and grade receipt"):
                verify_output_tree(output)

            wrong_label = "E" if report["grade"]["grade"] != "E" else "A"
            report["grade"]["grade"] = wrong_label
            index_row["grade"] = wrong_label
            (output / "tickers" / "T050.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            (output / "index.json").write_text(json.dumps(index), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "label cannot be reproduced"):
                verify_output_tree(output)

            report = {
                "schema_version": 1,
                "generated_at": report["generated_at"],
                "as_of": report["as_of"],
                "company": report["company"],
                "status": "unresolved",
                "reason": "analysis_error",
                "detail": "synthetic crash",
                "grade": None,
            }
            for field in (
                "grade",
                "percentile",
                "composite",
                "peer_group",
                "sampling_standard_deviation",
                "coverage",
            ):
                index_row[field] = None
            (output / "tickers" / "T050.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            (output / "index.json").write_text(json.dumps(index), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "analysis-error ticker"):
                verify_output_tree(output)


class GradingTests(unittest.TestCase):
    @staticmethod
    def _company(index: int, factor_count: int = 14) -> CompanyFactors:
        names = (
            "gross_profitability",
            "roic",
            "accruals",
            "gross_margin_stability_5y",
            "chs_12m",
            "altman_z_double_prime",
            "net_debt_to_ebitda",
            "interest_coverage",
            "current_ratio",
            "revenue_cagr_3y",
            "fcf_cagr_3y",
            "reinvestment_rate",
            "asset_growth_3y",
            "net_share_issuance",
        )
        factors = tuple(
            FactorResult(name, float(index), "ratio", "higher", "2023-12-31")
            for name in names[:factor_count]
        )
        return CompanyFactors(f"T{index:03d}", "healthcare", factors)

    def test_peer_percentiles_letters_and_boundary_uncertainty(self) -> None:
        companies = [self._company(index) for index in range(120)]
        grades = grade_universe(companies)
        self.assertEqual(grades["T119"].grade, "A")
        self.assertEqual(grades["T000"].grade, "E")
        self.assertEqual(grades["T096"].grade, "A/B")
        self.assertEqual(grades["T119"].peer_group, "healthcare")
        self.assertEqual(grades["T119"].coverage, {"resolved": 14, "wanted": 14})
        receipt = grades["T119"].to_dict()["normalization"]
        self.assertAlmostEqual(sum(receipt["factor_weights"].values()), 1.0)
        expected = sum(
            receipt["factor_weights"][name]
            * (grades["T119"].factor_percentiles[name]["percentile"] - 0.5)
            for name in receipt["factor_weights"]
        ) / receipt["correlation_standard_deviation"]
        self.assertAlmostEqual(grades["T119"].composite, expected)

    def test_bulk_percentiles_match_the_reference_math_with_ties(self) -> None:
        values = {"A": 1.0, "B": 2.0, "C": 2.0, "D": 4.0}
        for lower_is_better in (True, False):
            with self.subTest(lower_is_better=lower_is_better):
                expected = {
                    ticker: _percentile(
                        values, ticker, lower_is_better=lower_is_better
                    )
                    for ticker in values
                }
                self.assertEqual(
                    _percentile_map(values, lower_is_better=lower_is_better),
                    expected,
                )

    def test_thin_coverage_refuses_a_grade(self) -> None:
        companies = [self._company(index) for index in range(120)]
        companies[119] = self._company(119, factor_count=6)
        grade = grade_universe(companies)["T119"]
        self.assertEqual(grade.status, "unresolved")
        self.assertEqual(grade.reason, "computable_factors_below_threshold")

    def test_peer_minimum_counts_companies_with_computable_grades(self) -> None:
        healthcare = [
            CompanyFactors(
                f"H{index:03d}",
                "healthcare",
                self._company(index, 14 if index < 50 else 6).factors,
            )
            for index in range(100)
        ]
        technology = [
            CompanyFactors(
                f"B{index:03d}",
                "business_equipment",
                self._company(index + 100).factors,
            )
            for index in range(100)
        ]
        grades = grade_universe(healthcare + technology)

        self.assertEqual(grades["H049"].peer_group, "all_eligible")
        self.assertEqual(grades["H049"].peer_count, 150)
        self.assertEqual(grades["B099"].peer_group, "business_equipment")
        self.assertEqual(grades["B099"].peer_count, 100)

    def test_grade_is_refused_when_the_whole_universe_has_under_100_peers(self) -> None:
        grades = grade_universe([self._company(index) for index in range(99)])
        self.assertTrue(
            all(result.reason == "insufficient_peer_group" for result in grades.values())
        )

    def test_all_universe_fallback_does_not_overwrite_sector_receipts(self) -> None:
        healthcare = [self._company(index) for index in range(100)]
        base = self._company(100)
        utility = CompanyFactors(
            base.ticker, "utilities", base.factors, base.exchange
        )
        grades = grade_universe([*healthcare, utility])

        sector_grade = grades[healthcare[0].ticker]
        self.assertEqual(sector_grade.peer_group, "healthcare")
        self.assertTrue(
            all(
                receipt["peer_group"] == "healthcare"
                for receipt in sector_grade.factor_percentiles.values()
            )
        )
        self.assertEqual(grades[utility.ticker].peer_group, "all_eligible")

    def test_sampling_error_anchors_and_grade_boundaries(self) -> None:
        self.assertEqual(sampling_standard_deviation(30), 0.052)
        self.assertEqual(sampling_standard_deviation(100), 0.040)
        self.assertEqual(sampling_standard_deviation(500), 0.022)
        self.assertEqual(grade_label(0.19, 0.02), "A/B")

    def test_grade_construction_sensitivity_is_published(self) -> None:
        companies = [self._company(index) for index in range(120)]
        sensitivity = grade_sensitivity(companies, "T119")
        self.assertEqual(
            set(sensitivity),
            {
                "baseline_sector_rollup_equal_sleeves",
                "all_eligible_peer_universe_equal_sleeves",
                "same_exchange_peer_universe_equal_sleeves",
                "sector_rollup_equal_factors",
            },
        )
        self.assertTrue(all(row["grade"] is not None for row in sensitivity.values()))


if __name__ == "__main__":
    unittest.main()
