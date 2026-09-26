from __future__ import annotations

import unittest

from assay.describe import business_overview, latest_annual

FILING = """
<html><body>
<ix:header><div>Hidden facts Item 1. Business nonsense that must be skipped entirely and never quoted</div></ix:header>
<div>TABLE OF CONTENTS</div>
<table><tr><td>Item 1.</td><td>Business</td><td>4</td></tr><tr><td>Item 1A.</td><td>Risk Factors</td><td>12</td></tr></table>
<div>PART I</div>
<p><b>Item 1. Business</b></p>
<p>Overview</p>
<p>Zenith Battery Systems designs, manufactures and installs grid-scale lithium iron phosphate storage
systems for utilities and independent power producers in North America. The company was incorporated in
Delaware in 2016 and completed its initial public offering in 2021.</p>
<p>We sell through a direct sales force and long-term supply agreements with three utilities that together
accounted for 71% of revenue in the year ended June 30, 2026.</p>
<p><b>Item 1A. Risk Factors</b></p>
<p>Investing in our common stock involves risk.</p>
</body></html>
"""


class DescribeTests(unittest.TestCase):
    def test_takes_the_body_of_item_1_not_the_contents_or_hidden_header(self) -> None:
        text = business_overview(FILING)
        self.assertIsNotNone(text)
        self.assertTrue(text.startswith("Zenith Battery Systems designs"), text)
        self.assertNotIn("Hidden facts", text)
        self.assertNotIn("Risk Factors", text)
        self.assertLessEqual(len(text), 700)

    def test_cuts_long_text_at_a_sentence(self) -> None:
        long = FILING.replace("independent power producers", "independent power producers " + "and partners " * 80)
        text = business_overview(long, max_chars=300)
        self.assertLessEqual(len(text), 302)
        self.assertTrue(text.endswith((".", "...")), text)

    def test_falls_back_to_an_overview_heading_when_item_1_has_no_body(self) -> None:
        filing = """<html><body>
        <table><tr><td>Item 1.</td><td>Business: see Overview</td><td>3</td></tr></table>
        <p>Overview</p><p>2</p>
        <p>Overview</p>
        <p>We are a global designer and manufacturer of semiconductor products. The CPUs and other semiconductor
        solutions that we design, manufacture, market, sell and service are incorporated in computing and related
        end products and services used by consumers and businesses around the world.</p>
        <p>Our Strategy</p>
        </body></html>"""
        text = business_overview(filing)
        self.assertIsNotNone(text)
        self.assertTrue(text.startswith("We are a global designer"), text)

    def test_latest_annual_prefers_the_newest_annual_form(self) -> None:
        submissions = {"filings": {"recent": {
            "form": ["10-Q", "10-K", "10-K"],
            "filingDate": ["2026-07-24", "2026-01-23", "2025-01-31"],
            "accessionNumber": ["c", "b", "a"],
            "primaryDocument": ["q.htm", "k-2025.htm", "k-2024.htm"],
        }}}
        filing = latest_annual(submissions)
        self.assertEqual(filing["accession"], "b")
        self.assertEqual(filing["primary_document"], "k-2025.htm")


if __name__ == "__main__":
    unittest.main()
