from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from assay.export import build, detail, retained, slim_index, snapshot
from assay.runlog import record, record_failure
from assay.site import company_csvs, load_runs, point_in_time_page, runs_page


def tree(root: Path, as_of: str = "2026-09-26") -> Path:
    """A two-ticker output tree: one graded, one excluded."""
    data = root / "data"
    (data / "tickers").mkdir(parents=True)
    factor = {"name": "gross_profitability", "sleeve": "profitability", "status": "resolved", "unit": "ratio", "value": 0.31, "period_end": "2025-12-31", "direction": "higher",
              "inputs": [{"accession": "0000000001-26-000001", "form": "10-K", "filed": "2026-02-20", "filing_url": "https://www.sec.gov/x/1-index.html", "concept": "gross_profit"}]}
    missing = {"name": "interest_coverage", "sleeve": "solvency", "status": "unresolved", "unit": "ratio", "value": None, "period_end": None, "direction": "higher", "inputs": []}
    report = {"as_of": as_of, "company": {"ticker": "AAA", "name": "Alpha Inc", "sector": "healthcare", "cik": 1},
              "grade": {"grade": "B", "percentile": 0.3, "composite": 0.3, "sleeve_scores": {"profitability": 0.2, "solvency": 0.4, "growth_and_financing": 0.3},
                        "factor_percentiles": {"gross_profitability": {"percentile": 0.2, "peer_count": 120}}, "coverage": {"resolved": 1, "wanted": 14}},
              "factors": [factor, missing], "flags": [{"name": "fortress", "active": True, "detail": "net cash", "status": "resolved"}, {"name": "dilution", "active": False, "detail": "", "status": "resolved"}]}
    (data / "tickers" / "AAA.json").write_text(json.dumps(report), encoding="utf-8")
    rows = [{"ticker": "AAA", "cik": 1, "name": "Alpha Inc", "status": "eligible", "sector": "healthcare", "grade": "B", "percentile": 0.3, "composite": 0.3, "stratum": "Q3",
             "standard_universe": True, "coverage": {"resolved": 1, "wanted": 14}, "active_flags": ["fortress"], "implied_expectations": {"momentum_12_1": 0.1}, "artifact": "tickers/AAA.json", "live_price": 10.5},
            {"ticker": "BBB", "cik": 2, "name": "Beta Bank", "status": "excluded", "reason": "financial_or_reit"}]
    (data / "index.json").write_text(json.dumps({"as_of": as_of, "tickers": rows}), encoding="utf-8")
    return data


class ExportTests(unittest.TestCase):
    def test_slim_index_keeps_eligible_rows_and_the_input_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = tree(Path(directory))
            slim = slim_index(json.loads((data / "index.json").read_text()))
        self.assertEqual([r["ticker"] for r in slim["tickers"]], ["AAA"])
        self.assertEqual(slim["tickers"][0]["inputs_computable"], 1)
        self.assertNotIn("artifact", slim["tickers"][0])

    def test_detail_carries_inputs_receipts_and_active_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = tree(Path(directory))
            d = detail(data, json.loads((data / "index.json").read_text()))
        rec = d["tickers"]["AAA"]
        self.assertEqual(rec["inputs"][0][:2], ["gross_profitability", "profitability"])
        self.assertEqual(rec["inputs"][0][3], 0.2)
        self.assertEqual(rec["inputs"][0][8], "ratio")
        self.assertEqual(rec["inputs"][1][7], "unresolved")
        self.assertEqual(rec["receipts"]["0000000001-26-000001"][0], "10-K")
        self.assertEqual(rec["warnings"], [["fortress", "net cash"]])

    def test_retention_keeps_recent_days_and_monthly_anchors(self) -> None:
        dates = ["2025-01-21", "2025-01-22", "2025-02-19", "2025-02-21", "2025-02-22", "2026-08-01", "2026-09-25", "2026-09-26"]
        self.assertEqual(retained(dates, "2026-09-26"), ["2025-01-21", "2025-02-21", "2026-08-01", "2026-09-25", "2026-09-26"])

    def test_snapshot_writes_records_and_prunes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = tree(root)
            index_dir, detail_dir = root / "history", root / "detail"
            index_dir.mkdir()
            (index_dir / "2020-03-03.json").write_text("{}", encoding="utf-8")
            summary = snapshot(data, index_dir, detail_dir)
            self.assertEqual(summary, {"as_of": "2026-09-26", "kept": 1, "removed": 1})
            self.assertTrue((index_dir / "2026-09-26.json").exists())
            self.assertTrue((detail_dir / "2026-09-26.json").exists())
            self.assertFalse((index_dir / "2020-03-03.json").exists())

    def test_build_writes_exports_dates_and_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = tree(root)
            out = root / "site"
            result = build(data, out, None, None)
            self.assertEqual(result["dates"], 1)
            self.assertEqual(json.loads((out / "history" / "dates.json").read_text()), ["2026-09-26"])
            self.assertTrue((out / "history" / "index" / "2026-09-26.json").exists())
            self.assertIn("AAA", json.loads((out / "history" / "detail" / "2026-09-26.json").read_text())["tickers"])
            rows = gzip.decompress((out / "inputs.csv.gz").read_bytes()).decode().splitlines()
            self.assertEqual(len(rows), 3)

    def test_company_csvs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = tree(Path(directory))
            report = json.loads((data / "tickers" / "AAA.json").read_text())
        inputs, rescans = company_csvs(report, [("2026-08-21", "C", 0.5), ("2026-09-26", "B", 0.3)])
        self.assertIn("gross_profitability,Gross profitability,profitability,0.31,ratio,0.2,120,2025-12-31,resolved,https://www.sec.gov/x/1-index.html", inputs)
        self.assertEqual(rescans.splitlines()[1:], ["2026-08-21,AAA,C,0.5", "2026-09-26,AAA,B,0.3"])

    def test_run_records_and_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = tree(root)
            runs = root / "runs.jsonl"
            (root / "verification.json").write_text(json.dumps({"as_of": "2026-09-26", "tickers": 1}), encoding="utf-8")
            entry = record(runs, data, root / "verification.json", "2026-09-26T07:00:00Z", "logs/2026-09-26.log")
            self.assertTrue(entry["verified"])
            self.assertEqual(entry["letters"]["B"], 1)
            record_failure(runs, "2026-09-27", "2026-09-27T07:00:00Z", "logs/2026-09-27.log")
            loaded = load_runs(runs)
            self.assertEqual([r.get("failed") for r in loaded], [False, True])
            state = {"index": json.loads((data / "index.json").read_text()), "graded": [{"ticker": "AAA"}]}
            html = runs_page(loaded, "2026-09-27")
            self.assertIn("failed", html)
            self.assertIn("logs/2026-09-27.log", html)
            from assay.site import error_page, not_found_page
            lost = not_found_page(state, "/assay/")
            self.assertIn('href="/assay/index.html"', lost)
            self.assertIn("nf-ticker", lost)
            self.assertIn("Try again", error_page(state, "/assay/"))
            page = point_in_time_page(state)
            self.assertIn("history/dates.json", page)
            self.assertIn("Time machine", page)
            self.assertEqual(page.count("<script"), 2, "the page script plus the point-in-time reader")


if __name__ == "__main__":
    unittest.main()
