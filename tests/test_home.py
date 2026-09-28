from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from assay import animals
from assay.home import best_ranked, featured, features, load, load_featured, movers, record_featured, render
from assay.market_data import MarketStore
from assay.pipeline import run_full_pipeline
from assay.sec import BulkSecStore
from test_assay import full_factor_companyfacts


class HomeTests(unittest.TestCase):
    def test_front_page_renders_from_a_tree_and_embeds_the_letter_photos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ticker_data = {"0": {"ticker": "AAA", "cik_str": 1, "title": "Eligible"}}
            submissions = {"cik": "1", "sic": "2834", "tickers": ["AAA"], "exchanges": ["Nasdaq"],
                           "filings": {"recent": {"form": ["10-K"], "filingDate": ["2024-02-20"], "accessionNumber": ["a"]}}}
            with zipfile.ZipFile(root / "companyfacts.zip", "w") as archive:
                archive.writestr("CIK0000000001.json", json.dumps(full_factor_companyfacts()))
            with zipfile.ZipFile(root / "submissions.zip", "w") as archive:
                archive.writestr("CIK0000000001.json", json.dumps(submissions))
            output = root / "output"
            with BulkSecStore(root / "companyfacts.zip", root / "submissions.zip") as sec_store, MarketStore(root / "market.sqlite3") as market_store:
                run_full_pipeline(ticker_data, sec_store, market_store, date(2024, 3, 1), output)
            state = load(output, output / "grade_history.json")
            html = render(state)
            self.assertIsInstance(featured(state), list)
        self.assertTrue(html.startswith('<meta charset="utf-8">'))
        self.assertIn("<title>Assay Front Page</title>", html[:200])
        self.assertIn("The Letters", html)
        self.assertEqual(html.count("data:image/jpeg;base64,"), 5, "one embedded photo per letter in the legend")
        self.assertIn("Photographs from Wikimedia Commons", html)
        self.assertTrue({"bull", "bear", "elk", "tortoise", "sloth", "fox"} <= {c["name"] for c in animals.credits()})

    def test_best_ranked_treats_a_zero_percentile_as_the_strongest(self) -> None:
        rows = [{"ticker": "GNE", "percentile": 0.012}, {"ticker": "CWCO", "percentile": 0.0}, {"ticker": "NONE", "percentile": None}]
        self.assertEqual(best_ranked(rows)["ticker"], "CWCO")
        self.assertIsNone(best_ranked([{"ticker": "NONE", "percentile": None}]))

    def test_features_rotate_unless_first_in_sector(self) -> None:
        rows = [{"ticker": "AAA", "grade": "A", "percentile": 0.01, "sector": "shops", "implied_expectations": {}},
                {"ticker": "BBB", "grade": "A", "percentile": 0.02, "sector": "shops", "implied_expectations": {}},
                {"ticker": "CCC", "grade": "A", "percentile": 0.0, "sector": "energy", "implied_expectations": {}},
                {"ticker": "EEE", "grade": "E", "percentile": 0.99, "sector": "shops", "implied_expectations": {}}]
        state = {"graded": rows, "mcap": {"AAA": 2e10, "BBB": 2e10, "CCC": 2e10, "EEE": 2e10}, "recent_features": set()}
        self.assertEqual([p["row"]["ticker"] for p in features(state)], ["CCC", "EEE"])
        state["recent_features"] = {"CCC", "EEE"}
        self.assertEqual([p["row"]["ticker"] for p in features(state)], ["CCC", "EEE"], "first and last in their sectors stay featured")
        state["graded"][2]["percentile"] = 0.005
        state["graded"].append({"ticker": "DDD", "grade": "A", "percentile": 0.001, "sector": "energy", "implied_expectations": {}})
        state["mcap"]["DDD"] = 1e9
        self.assertEqual([p["row"]["ticker"] for p in features(state)][0], "AAA", "a recently featured bull that is no longer first yields")
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "featured.jsonl"
            record_featured(log, "2026-09-27", [{"row": {"ticker": "AAA"}, "kicker": "Bull of the Day"}])
            record_featured(log, "2026-09-27", [{"row": {"ticker": "ZZZ"}, "kicker": "Bull of the Day"}])
            self.assertEqual(load_featured(log, "2026-09-28"), {"AAA"})
            self.assertEqual(load_featured(log, "2026-10-20"), set())

    def test_movers_count_only_letter_changes_within_the_year(self) -> None:
        rows = [{"ticker": "OLD", "grade": "E", "percentile": 0.9}, {"ticker": "NEW", "grade": "E", "percentile": 0.9}]
        history = {"tickers": {
            "OLD": {"changes": [["2013-02-21", "A", 0.05, "Q5"], ["2015-02-21", "E", 0.9, "Q5"]]},
            "NEW": {"changes": [["2013-02-21", "A", 0.05, "Q5"], ["2024-02-21", "B", 0.3, "Q5"], ["2026-03-21", "E", 0.9, "Q5"]]},
        }}
        state = {"history": history, "index": {"as_of": "2026-08-21"}, "graded": rows, "mcap": {"OLD": 1e9, "NEW": 1e9}}
        weaker, stronger = movers(state)
        self.assertEqual([m["row"]["ticker"] for m in weaker], ["NEW"])
        self.assertEqual(weaker[0]["path"], ["B", "E"])
        self.assertEqual(stronger, [])


if __name__ == "__main__":
    unittest.main()
