from __future__ import annotations

import gzip
import hashlib
import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from assay.diagnostics import compute_cash_runway, compute_piotroski
from assay.factors import compute_accounting_factors
from assay.facts import concept_spec, resolve_discrete_quarters, resolve_fact_history
from assay.market import (
    PriceBar,
    build_company_chs_raw,
    compute_net_share_issuance,
)


FIXTURE = Path(__file__).parent / "fixtures" / "aapl_CIK0000320193.json.gz"
AS_OF = date(2025, 11, 1)
SP500_MARKET_VALUE = 67_075_482_580_000


def market_tape() -> tuple[list[PriceBar], list[PriceBar]]:
    stock: list[PriceBar] = []
    benchmark: list[PriceBar] = []
    day = date(2023, 9, 1)
    trading_day = 0
    while day <= AS_OF:
        if day.weekday() < 5:
            stock.append(
                PriceBar(day, 180 * 1.00025**trading_day, 50_000_000)
            )
            benchmark.append(
                PriceBar(day, 5_000 * 1.00015**trading_day, 1_000_000)
            )
            trading_day += 1
        day += timedelta(days=1)
    return stock, benchmark


class RealCompanyfactsRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = gzip.decompress(FIXTURE.read_bytes())
        cls.companyfacts = json.loads(cls.raw)
        cls.stock, cls.benchmark = market_tape()

    def test_fixture_is_the_frozen_real_sec_response(self) -> None:
        self.assertEqual(
            hashlib.sha256(self.raw).hexdigest(),
            "31f9ab4398402faabc733178497af89dbf94dd5038c6e36d4c894317de8a4647",
        )
        self.assertEqual(self.companyfacts["cik"], 320193)
        self.assertEqual(self.companyfacts["entityName"], "Apple Inc.")

    def test_2025_annual_values_match_the_hand_checked_10k(self) -> None:
        expected = {
            "revenue": 416_161_000_000,
            "gross_profit": 195_201_000_000,
            "operating_income": 133_050_000_000,
            "net_income": 112_010_000_000,
            "operating_cash_flow": 111_482_000_000,
            "assets": 359_241_000_000,
            "liabilities": 285_508_000_000,
            "current_assets": 147_957_000_000,
            "current_liabilities": 165_631_000_000,
            "long_term_debt": 78_328_000_000,
            "book_equity": 73_733_000_000,
            "cash": 35_934_000_000,
        }
        for name, value in expected.items():
            fact = resolve_fact_history(
                self.companyfacts, concept_spec(name), AS_OF, limit=1
            )[0]
            with self.subTest(name=name):
                self.assertEqual(fact.value, value)
                self.assertEqual(fact.end, "2025-09-27")
                self.assertEqual(fact.accession, "0000320193-25-000079")

        factors = {item.name: item for item in compute_accounting_factors(
            self.companyfacts, AS_OF
        )}
        self.assertEqual(
            sum(item.value is not None for item in factors.values()), 11
        )
        self.assertIsNone(factors["interest_coverage"].value)
        self.assertEqual(factors["interest_coverage"].reason, "period_mismatch")
        self.assertIn("2023-09-30", factors["interest_coverage"].detail)

    def test_ytd_cash_flows_are_reconstructed_into_real_quarters(self) -> None:
        net_income = resolve_discrete_quarters(
            self.companyfacts, concept_spec("net_income"), AS_OF, limit=4
        )
        self.assertEqual(
            [quarter.value for quarter in net_income],
            [27_466_000_000, 23_434_000_000, 24_780_000_000, 36_330_000_000],
        )
        operating_cash_flow = resolve_discrete_quarters(
            self.companyfacts,
            concept_spec("operating_cash_flow"),
            AS_OF,
            limit=4,
        )
        self.assertEqual(
            [quarter.value for quarter in operating_cash_flow],
            [29_728_000_000, 27_867_000_000, 23_952_000_000, 29_935_000_000],
        )
        self.assertEqual(
            [quarter.method for quarter in operating_cash_flow],
            [
                "annual_less_ytd_q3",
                "ytd_difference",
                "ytd_difference",
                "direct",
            ],
        )
        runway = compute_cash_runway(self.companyfacts, AS_OF)
        self.assertEqual(runway.status, "resolved")
        self.assertEqual(runway.trailing_operating_cash_flow, 111_482_000_000)
        self.assertFalse(runway.burning_cash)

    def test_cover_dates_do_not_create_a_phantom_share_year(self) -> None:
        issuance = compute_net_share_issuance(
            self.companyfacts, AS_OF, self.stock, self.stock
        )
        self.assertAlmostEqual(issuance.value, -0.022457923726680318)
        self.assertEqual(
            [fact.end for fact in issuance.inputs],
            ["2025-10-17", "2024-10-18"],
        )
        self.assertTrue(
            all(
                fact.tag == "EntityCommonStockSharesOutstanding"
                for fact in issuance.inputs
            )
        )

        piotroski = compute_piotroski(
            self.companyfacts,
            AS_OF,
            split_adjusted_share_growth=issuance.value,
            share_inputs=issuance.inputs,
        )
        self.assertEqual(piotroski.score, 8)
        self.assertEqual(
            {component.name: component.passed for component in piotroski.components},
            {
                "positive_roa": True,
                "positive_operating_cash_flow": True,
                "improving_roa": True,
                "cash_flow_exceeds_net_income": False,
                "declining_long_term_leverage": True,
                "improving_current_ratio": True,
                "no_new_shares": True,
                "improving_gross_margin": True,
                "improving_asset_turnover": True,
            },
        )

    def test_chs_inputs_match_fixed_market_tape_and_filing_values(self) -> None:
        result = build_company_chs_raw(
            self.companyfacts,
            AS_OF,
            self.stock,
            self.stock,
            self.benchmark,
            sp500_market_value=SP500_MARKET_VALUE,
        )
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.accounting_period_end, "2025-09-27")
        expected = {
            "nimtaavg": 0.008076001049622188,
            "tlmta": 0.08527532820881495,
            "exretavg": 0.0021996929476652024,
            "sigma": 0.003999511688945211,
            "rsize": -3.0865658824587214,
            "cashmta": 0.016336861408568416,
            "mb": 8.219086958641974,
            "price": 2.70805020110221,
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertAlmostEqual(getattr(result.inputs, name), value, places=12)

        self.assertEqual(
            [fact.value for fact in result.accounting_receipts["liabilities"]],
            [285_508_000_000, 265_665_000_000, 264_437_000_000, 277_327_000_000],
        )
        self.assertEqual(
            [fact.value for fact in result.accounting_receipts["shares_outstanding"]],
            [14_773_260_000, 14_856_722_000, 14_939_315_000, 15_040_731_000],
        )
        self.assertEqual(
            [
                fact.value
                for fact in result.accounting_receipts[
                    "cash_and_short_term_investments"
                ]
            ],
            [35_934_000_000, 18_763_000_000],
        )


if __name__ == "__main__":
    unittest.main()
