from __future__ import annotations

import json
import re
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

from assay.market_data import MarketStore
from assay.pipeline import run_full_pipeline
from assay.render import render
from assay.sec import BulkSecStore
from test_assay import full_factor_companyfacts


class RenderTests(unittest.TestCase):
    def _output(self, root: Path) -> Path:
        ticker_data = {"0": {"ticker": "AAA", "cik_str": 1, "title": "Eligible"}}
        submissions = {
            "cik": "1",
            "sic": "2834",
            "tickers": ["AAA"],
            "exchanges": ["Nasdaq"],
            "filings": {"recent": {"form": ["10-K"], "filingDate": ["2024-02-20"], "accessionNumber": ["a"]}},
        }
        with zipfile.ZipFile(root / "companyfacts.zip", "w") as archive:
            archive.writestr("CIK0000000001.json", json.dumps(full_factor_companyfacts()))
        with zipfile.ZipFile(root / "submissions.zip", "w") as archive:
            archive.writestr("CIK0000000001.json", json.dumps(submissions))
        output = root / "output"
        with (
            BulkSecStore(root / "companyfacts.zip", root / "submissions.zip") as sec_store,
            MarketStore(root / "market.sqlite3") as market_store,
        ):
            run_full_pipeline(ticker_data, sec_store, market_store, date(2024, 3, 1), output)
        return output

    def test_page_links_every_receipt_to_sec_and_draws_the_chart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = self._output(Path(directory))
            report = json.loads((output / "tickers" / "AAA.json").read_text(encoding="utf-8"))
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            start = date(2023, 3, 1)
            prices = [[(start + timedelta(days=i)).isoformat(), 10 + (i % 7) / 10] for i in range(0, 366, 1)]
            html = render(report, output, prices, index)

        self.assertTrue(html.startswith('<meta charset="utf-8">'))
        self.assertIn("<title>Assay AAA</title>", html[:200])
        self.assertNotIn("None:", html)
        self.assertEqual(html.count("<script"), 1, "only the inline tooltip script")
        self.assertNotIn("<script src", html)
        external = {href for href in re.findall(r'href="(https?://[^"]+)"', html)}
        self.assertTrue(external, "expected receipt links")
        allowed = ("https://www.sec.gov/", "https://data.sec.gov/", "https://commons.wikimedia.org/", "https://github.com/ekruges/assay", "https://ezrakruger.cc")
        self.assertTrue(all(href.startswith(allowed) for href in external), external)
        self.assertIn('<sup class="c">[', html)
        self.assertIn("Split-adjusted close over twelve months", html)
        self.assertIn('id="sources"', html)
        # every numbered citation resolves to a row in the sources table
        numbers: set[int] = set()
        for group in re.findall(r'(?:<sup class="c">|<span class="cref">)\[(.*?)\]', html):
            for first, last in re.findall(r">(\d+)(?:-(\d+))?<", group):
                numbers.update(range(int(first), int(last or first) + 1))
        rows = len(re.findall(r'<td class="n">(\d+)</td><td>', html.split('id="sources"')[1]))
        self.assertEqual(max(numbers), rows)


if __name__ == "__main__":
    unittest.main()
