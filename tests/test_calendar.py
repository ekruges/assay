from __future__ import annotations

import unittest
from datetime import date

from assay.calendar import expected, filer_class, fiscal_periods, shift_months


class CalendarTests(unittest.TestCase):
    def test_shift_months_clamps_to_month_length(self) -> None:
        self.assertEqual(shift_months(date(2026, 5, 31), -3), date(2026, 2, 28))
        self.assertEqual(shift_months(date(2025, 12, 27), -6), date(2025, 6, 27))

    def test_filer_class(self) -> None:
        self.assertEqual(filer_class("Large accelerated filer"), "large")
        self.assertEqual(filer_class("Accelerated filer"), "accelerated")
        self.assertEqual(filer_class("Non-accelerated filer"), "other")
        self.assertEqual(filer_class(None), "other")

    def test_periods_cover_the_last_year(self) -> None:
        periods = fiscal_periods("1227", date(2026, 8, 21))
        self.assertEqual(periods[-1], (date(2026, 6, 27), "10-Q"))
        self.assertIn((date(2025, 12, 27), "10-K"), periods)

    def test_received_due_and_overdue(self) -> None:
        submissions = {"fiscalYearEnd": "1227", "category": "Large accelerated filer", "filings": {"recent": {
            "form": ["10-Q", "10-K"], "filingDate": ["2026-07-24", "2026-01-23"], "reportDate": ["2026-06-27", "2025-12-27"]}}}
        got = expected(submissions, date(2026, 8, 21))
        self.assertEqual((got["form"], got["status"], got["filed"], got["due"]), ("10-Q", "received", "2026-07-24", "2026-08-06"))
        self.assertEqual(got["next"], {"period_end": "2026-09-27", "form": "10-Q", "due": "2026-11-06"})
        overdue = expected({"fiscalYearEnd": "1227", "category": "Non-accelerated filer", "filings": {"recent": {"form": [], "filingDate": [], "reportDate": []}}}, date(2026, 8, 21))
        self.assertEqual((overdue["status"], overdue["due"], overdue["days"]), ("overdue", "2026-08-11", 10))
        due = expected({"fiscalYearEnd": "1227", "category": "Non-accelerated filer", "filings": {"recent": {"form": [], "filingDate": [], "reportDate": []}}}, date(2026, 8, 1))
        self.assertEqual((due["status"], due["days"]), ("due", 10))


if __name__ == "__main__":
    unittest.main()
