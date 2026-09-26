from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import json

from assay.prices import export, export_full


class PricesTests(unittest.TestCase):
    def test_exports_a_year_of_split_adjusted_closes_per_ticker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "market.sqlite3"
            with sqlite3.connect(db) as conn:
                conn.execute("create table bars (symbol text, day text, adjustment text, feed text, close real, volume integer)")
                start = date(2025, 1, 1)
                rows = []
                for i in range(600):
                    day = (start + timedelta(days=i)).isoformat()
                    rows.append(("AAA", day, "split", "sip", 10 + i / 100, 1))
                    rows.append(("AAA", day, "raw", "sip", 99.0, 1))
                rows += [("BBB", "2026-08-01", "split", "sip", 5.0, 1)]
                conn.executemany("insert into bars values (?,?,?,?,?,?)", rows)
            out = export(db, ["AAA", "BBB", "CCC"], date(2026, 8, 21))
        self.assertEqual(set(out), {"AAA"}, "BBB has too few bars, CCC none")
        self.assertEqual(out["AAA"][0][0], "2025-08-20")
        self.assertEqual(out["AAA"][-1][0], "2026-08-21")
        self.assertTrue(all(c != 99.0 for _, c in out["AAA"]), "split-adjusted series, not raw")

    def test_full_export_writes_one_compact_file_per_ticker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "market.sqlite3"
            with sqlite3.connect(db) as conn:
                conn.execute("create table bars (symbol text, day text, adjustment text, feed text, close real, volume integer)")
                start = date(2012, 1, 3)
                conn.executemany("insert into bars values (?,?,?,?,?,?)", [("AAA", (start + timedelta(days=i)).isoformat(), "split", "sip", 20 + i / 50, 1) for i in range(0, 5000, 3)])
            out_dir = Path(directory) / "prices"
            written = export_full(db, ["AAA", "ZZZ"], date(2025, 9, 1), out_dir)
            payload = json.loads((out_dir / "AAA.json").read_text())
        self.assertEqual(written, 1)
        self.assertEqual(len(payload["d"]), len(payload["c"]))
        self.assertEqual(payload["d"][0], "2012-01-03")
        self.assertLessEqual(payload["d"][-1], "2025-09-01")


if __name__ == "__main__":
    unittest.main()
