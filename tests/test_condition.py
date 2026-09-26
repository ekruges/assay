from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from assay.diagnostics import detect_filing_gap
from assay.facts import MissingFact, ResolvedFact
from assay.history import (
    empty_history,
    read_grade_history,
    record_grade,
    ticker_history,
    write_grade_history,
)
from assay.market_data import MarketStore
from assay.pipeline import run_full_pipeline, verify_output_tree
from assay.sec import BulkSecStore
from assay.stratum import (
    RELIABILITY_PATH,
    assign_stratum,
    build_condition,
    data_age,
    load_reliability,
    reliability_for,
    standard_universe,
    validate_reliability,
)
from test_assay import full_factor_companyfacts


def _fact(concept: str, value: float, filed: str, end: str = "2023-12-31") -> ResolvedFact:
    return ResolvedFact(
        concept=concept,
        value=value,
        unit="USD",
        namespace="us-gaap",
        tag=concept.title(),
        start=None,
        end=end,
        filed=filed,
        form="10-K",
        accession="0000000001-24-000001",
        fiscal_year=2023,
        fiscal_period="FY",
        filing_url="https://www.sec.gov/Archives/edgar/data/1/000000000124000001/",
    )


class ReliabilityConfigTests(unittest.TestCase):
    def test_shipped_config_is_contiguous_and_bracketed(self) -> None:
        config = load_reliability(RELIABILITY_PATH)
        self.assertEqual([s["id"] for s in config["strata"]], ["Q1", "Q2", "Q3", "Q4", "Q5"])
        self.assertEqual(config["status"], "post_hoc_pending_confirmation")
        self.assertEqual(config["standard_universe"]["revenue_min"], 1_000_000)
        self.assertEqual(config["standard_universe"]["assets_min"], 10_000_000)

    def test_validation_rejects_a_gap_between_strata(self) -> None:
        config = load_reliability(RELIABILITY_PATH)
        config["strata"][1]["assets_min"] = 13_000_000
        with self.assertRaisesRegex(ValueError, "does not start where"):
            validate_reliability(config)

    def test_validation_rejects_an_interval_that_misses_its_auc(self) -> None:
        config = load_reliability(RELIABILITY_PATH)
        config["strata"][0]["ci"] = [0.9, 0.95]
        with self.assertRaisesRegex(ValueError, "bracket"):
            validate_reliability(config)


class StratumTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_reliability(RELIABILITY_PATH)

    def test_bands_are_half_open_on_the_upper_edge(self) -> None:
        self.assertEqual(assign_stratum(12_499_999, self.config)["id"], "Q1")
        self.assertEqual(assign_stratum(12_500_000, self.config)["id"], "Q2")
        self.assertEqual(assign_stratum(2_670_000_000, self.config)["id"], "Q5")
        self.assertEqual(assign_stratum(0, self.config)["id"], "Q1")

    def test_unusable_assets_get_no_stratum(self) -> None:
        for value in (None, -1.0, float("nan"), float("inf"), True):
            self.assertIsNone(assign_stratum(value, self.config), value)

    def test_standard_universe_needs_both_inputs(self) -> None:
        inside = standard_universe(2_000_000, 20_000_000, self.config)
        self.assertTrue(inside["inside"])
        outside = standard_universe(500_000, 20_000_000, self.config)
        self.assertFalse(outside["inside"])
        unknown = standard_universe(None, 20_000_000, self.config)
        self.assertIsNone(unknown["inside"])
        self.assertEqual(unknown["reason"], "missing_input")
        self.assertIn("revenue", unknown["detail"])
        exact = standard_universe(1_000_000, 10_000_000, self.config)
        self.assertFalse(exact["inside"])

    def test_reliability_lookup_reports_stratum_and_universe_side(self) -> None:
        result = reliability_for("Q1", False, self.config)
        self.assertEqual(result["stratum"]["auc"], 0.7605)
        self.assertEqual(result["stratum"]["test_events"], 63)
        self.assertEqual(result["stratum"]["base_rate_annual"], 0.00604)
        self.assertEqual(result["universe"]["auc"], 0.7452)
        self.assertFalse(result["universe"]["inside"])
        self.assertEqual(result["all"]["auc"], 0.8767)
        self.assertEqual(result["status"], "post_hoc_pending_confirmation")
        unknown = reliability_for(None, None, self.config)
        self.assertIsNone(unknown["stratum"])
        self.assertIsNone(unknown["universe"])

    def test_data_age_uses_the_latest_receipt_date(self) -> None:
        facts = [
            _fact("assets", 5e7, "2026-05-01"),
            _fact("revenue", 3e6, "2026-08-11"),
            MissingFact("cash", "missing_input", "no tag"),
        ]
        age = data_age(facts, date(2026, 9, 25))
        self.assertEqual(age["latest_filed"], "2026-08-11")
        self.assertEqual(age["days"], 45)
        empty = data_age([MissingFact("assets", "missing_input", "no tag")], date(2026, 9, 25))
        self.assertIsNone(empty["days"])

    def test_condition_carries_receipts_for_every_number_it_uses(self) -> None:
        facts = [_fact("assets", 5e7, "2026-05-01"), _fact("revenue", 3e6, "2026-08-11")]
        condition = build_condition(facts, date(2026, 9, 25), self.config)
        self.assertEqual(condition["stratum"]["id"], "Q2")
        self.assertEqual(condition["stratum"]["receipt"]["tag"], "us-gaap:Assets")
        self.assertTrue(condition["standard_universe"]["inside"])
        self.assertEqual(set(condition["standard_universe"]["receipts"]), {"revenue", "assets"})
        self.assertEqual(condition["reliability"]["stratum"]["id"], "Q2")
        self.assertEqual(condition["data_age"]["days"], 45)


class FilingGapTests(unittest.TestCase):
    def test_gap_measures_the_latest_periodic_form_only(self) -> None:
        submissions = {
            "cik": "1",
            "filings": {
                "recent": {
                    "form": ["8-K", "NT 10-Q", "10-K", "10-Q", "10-Q"],
                    "filingDate": [
                        "2026-09-01",
                        "2026-05-15",
                        "2025-02-20",
                        "2025-05-10",
                        "2026-11-01",
                    ],
                    "accessionNumber": ["a", "b", "c", "d", "e"],
                }
            },
        }
        result = detect_filing_gap(submissions, date(2026, 9, 25))
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.last_form, "10-Q")
        self.assertEqual(result.last_filing_date, "2025-05-10")
        self.assertEqual(result.days, 503)
        self.assertTrue(result.ceased)
        self.assertEqual(result.to_dict()["name"], "filing_gap")

    def test_recent_filer_is_not_ceased(self) -> None:
        submissions = {
            "cik": "1",
            "filings": {
                "recent": {
                    "form": ["10-Q"],
                    "filingDate": ["2026-08-11"],
                    "accessionNumber": ["a"],
                }
            },
        }
        result = detect_filing_gap(submissions, date(2026, 9, 25))
        self.assertEqual(result.days, 45)
        self.assertFalse(result.ceased)

    def test_no_periodic_filing_is_unresolved_not_zero(self) -> None:
        submissions = {"cik": "1", "filings": {"recent": {"form": ["8-K"], "filingDate": ["2026-01-01"]}}}
        result = detect_filing_gap(submissions, date(2026, 9, 25))
        self.assertEqual(result.status, "unresolved")
        self.assertIsNone(result.days)
        self.assertIsNone(result.ceased)
        self.assertEqual(result.reason, "no_periodic_filing_on_record")


class GradeHistoryTests(unittest.TestCase):
    def test_only_changes_are_kept_and_same_day_reruns_replace(self) -> None:
        history = empty_history()
        record_grade(history, "AAA", date(2026, 9, 1), "B", 0.31, "Q3")
        record_grade(history, "AAA", date(2026, 9, 2), "B", 0.33, "Q3")
        record_grade(history, "AAA", date(2026, 9, 3), "C", 0.41, "Q3")
        record_grade(history, "AAA", date(2026, 9, 3), "C/D", 0.58, "Q3")
        points = ticker_history(history, "AAA")
        self.assertEqual([p["grade"] for p in points], ["B", "C/D"])
        self.assertEqual(points[-1]["date"], "2026-09-03")
        self.assertEqual(history["tickers"]["AAA"]["last_seen"], "2026-09-03")
        self.assertEqual(history["updated"], "2026-09-03")

    def test_stratum_change_is_a_change_point_even_with_the_same_letter(self) -> None:
        history = empty_history()
        record_grade(history, "AAA", date(2026, 9, 1), "B", 0.31, "Q2")
        record_grade(history, "AAA", date(2026, 9, 2), "B", 0.31, "Q3")
        self.assertEqual(len(ticker_history(history, "AAA")), 2)

    def test_history_refuses_to_run_backwards(self) -> None:
        history = empty_history()
        record_grade(history, "AAA", date(2026, 9, 2), "B", 0.3, "Q2")
        with self.assertRaisesRegex(ValueError, "before the existing entry"):
            record_grade(history, "AAA", date(2026, 9, 1), "B", 0.3, "Q2")

    def test_round_trip_and_empty_file_tolerance(self) -> None:
        history = empty_history()
        record_grade(history, "AAA", date(2026, 9, 1), None, None, None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "grade_history.json"
            write_grade_history(path, history)
            self.assertEqual(read_grade_history(path), history)
            path.write_text("", encoding="utf-8")
            self.assertEqual(read_grade_history(path), empty_history())
            path.write_text(json.dumps({"schema_version": 1, "tickers": {"AAA": {"changes": [[1]]}}}))
            with self.assertRaisesRegex(ValueError, "malformed"):
                read_grade_history(path)
            self.assertEqual(read_grade_history(Path(directory) / "missing.json"), empty_history())


class PipelineConditionTests(unittest.TestCase):
    def _run(self, root: Path, as_of: date, history_path: Path) -> dict:
        ticker_data = {"0": {"ticker": "AAA", "cik_str": 1, "title": "Eligible"}}
        submissions = {
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
        }
        companyfacts_path = root / "companyfacts.zip"
        submissions_path = root / "submissions.zip"
        with zipfile.ZipFile(companyfacts_path, "w") as archive:
            archive.writestr("CIK0000000001.json", json.dumps(full_factor_companyfacts()))
        with zipfile.ZipFile(submissions_path, "w") as archive:
            archive.writestr("CIK0000000001.json", json.dumps(submissions))
        output = root / "output"
        with (
            BulkSecStore(companyfacts_path, submissions_path) as sec_store,
            MarketStore(root / "market.sqlite3") as market_store,
        ):
            run_full_pipeline(
                ticker_data,
                sec_store,
                market_store,
                as_of,
                output,
                grade_history_path=history_path,
            )
        return json.loads((output / "tickers" / "AAA.json").read_text(encoding="utf-8"))

    def test_reports_carry_condition_history_and_filing_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history_path = root / "state" / "grade_history.json"
            report = self._run(root, date(2024, 3, 1), history_path)
            self.assertEqual(report["status"], "analyzed")
            condition = report["condition"]
            self.assertIn(condition["stratum"]["id"], {"Q1", "Q2", "Q3", "Q4", "Q5"})
            self.assertIn("reliability", condition)
            self.assertEqual(condition["reliability"]["stratum"]["id"], condition["stratum"]["id"])
            self.assertIsNotNone(condition["data_age"]["days"])
            gap = report["diagnostics"]["filing_gap"]
            self.assertEqual(gap["last_filing_date"], "2023-02-20")
            self.assertEqual(gap["days"], 375)
            self.assertTrue(gap["ceased"])
            self.assertEqual(len(report["grade_history"]), 1)
            self.assertEqual(report["grade_history"][0]["date"], "2024-03-01")
            self.assertEqual(report["grade_history"][0]["grade"], report["grade"]["grade"])
            self.assertTrue(history_path.exists())
            output = root / "output"
            published = json.loads((output / "grade_history.json").read_text(encoding="utf-8"))
            self.assertEqual(published["tickers"]["AAA"]["last_seen"], "2024-03-01")
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            row = index["tickers"][0]
            self.assertEqual(row["stratum"], condition["stratum"]["id"])
            self.assertEqual(row["filing_gap_days"], 375)
            self.assertTrue(row["ceased"])
            self.assertIn("strata", index["summary"])
            methodology = json.loads((output / "methodology.json").read_text(encoding="utf-8"))
            self.assertEqual(methodology["reliability"]["status"], "post_hoc_pending_confirmation")
            self.assertEqual(verify_output_tree(output)["status"], "verified")

            second = self._run(root, date(2024, 3, 2), history_path)
            self.assertEqual(len(second["grade_history"]), 1)
            carried = read_grade_history(history_path)
            self.assertEqual(carried["tickers"]["AAA"]["last_seen"], "2024-03-02")
            self.assertEqual(carried["updated"], "2024-03-02")


if __name__ == "__main__":
    unittest.main()
