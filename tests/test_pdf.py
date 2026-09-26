from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

from assay.market_data import MarketStore
from assay.pdf import Pdf, company_report, text_width, wrap
from assay.pipeline import run_full_pipeline
from assay.sec import BulkSecStore
from test_assay import full_factor_companyfacts


def xref_is_consistent(data: bytes) -> tuple[int, int]:
    startxref = int(data[data.rindex(b"startxref") + 9:].split()[0])
    assert data[startxref:startxref + 4] == b"xref"
    lines = data[startxref:].split(b"\n")
    count = int(lines[1].split()[1])
    bad = 0
    for i, line in enumerate(lines[3:2 + count], 1):
        offset = int(line.split()[0])
        if not data[offset:offset + 12].startswith(f"{i} 0 obj".encode()):
            bad += 1
    return count - 1, bad


class PdfTests(unittest.TestCase):
    def test_writer_produces_a_consistent_file(self) -> None:
        pdf = Pdf()
        pdf.text(72, 700, "Assay (test) 100% & more", 12, bold=True)
        pdf.line(72, 690, 300, 690)
        pdf.rect(72, 600, 100, 50, fill=(0, 0, 0.5))
        pdf.circle(200, 620, 5)
        pdf.link(72, 700, 100, 12, "https://www.sec.gov/")
        pdf.new_page()
        pdf.text(72, 700, "page two", 10)
        data = pdf.build()
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        objects, bad = xref_is_consistent(data)
        self.assertEqual(bad, 0)
        self.assertEqual(data.count(b"/Type /Page "), 2)
        self.assertIn(b"/Subtype /Link", data)
        self.assertGreater(text_width("MMMM", 10), text_width("iiii", 10))
        self.assertEqual(len(wrap("one two three four five six seven", 60, 10)) > 1, True)

    def test_company_report_from_a_pipeline_artifact(self) -> None:
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
            report = json.loads((output / "tickers" / "AAA.json").read_text(encoding="utf-8"))
            start = date(2023, 3, 1)
            prices = [[(start + timedelta(days=i)).isoformat(), 10 + (i % 7) / 10] for i in range(366)]
            data = company_report(report, prices, {"text": "A test company.", "form": "10-K", "filed": "2024-02-20"}, report.get("grade_history"))
        objects, bad = xref_is_consistent(data)
        self.assertEqual(bad, 0)
        self.assertGreaterEqual(data.count(b"/Type /Page "), 1)
        self.assertIn(b"/Subtype /Link", data, "receipt links present")


if __name__ == "__main__":
    unittest.main()
