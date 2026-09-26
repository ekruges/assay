"""Expected periodic filings from fiscal year end and filer category.

Deadlines under Exchange Act Rules 13a-1 and 13a-13: a 10-K is due 60, 75 or 90 days after
fiscal year end for large accelerated, accelerated and other filers; a 10-Q is due 40 days
after quarter end for accelerated filers and 45 for others. Quarter ends are taken three, six
and nine months before fiscal year end; a filing counts for a period when its report date
lies within ten days of that period end, which covers 52/53-week calendars.

    python3 -m assay.calendar --cache-dir .cache/sec --data data --as-of 2026-08-21 --out filing_calendar.json
"""

from __future__ import annotations

import argparse
import calendar as _cal
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .sec import BulkSecStore, SecError

DEADLINES = {"large": (60, 40), "accelerated": (75, 40), "other": (90, 45)}
PERIODIC = {"10-K", "10-KT", "10-Q", "10-QT"}


def filer_class(category: str | None) -> str:
    text = (category or "").lower()
    if "large accelerated" in text:
        return "large"
    if "non-accelerated" in text:
        return "other"
    if "accelerated" in text:
        return "accelerated"
    return "other"


def shift_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, _cal.monthrange(year, month)[1]))


def fiscal_periods(fye: str, as_of: date) -> list[tuple[date, str]]:
    """Period ends within the 400 days before as_of, oldest first, each tagged 10-K or 10-Q."""
    if not fye or len(fye) != 4 or not fye.isdigit():
        return []
    month, day = int(fye[:2]), int(fye[2:])
    if not 1 <= month <= 12 or day < 1:
        return []
    periods: list[tuple[date, str]] = []
    for year in (as_of.year - 1, as_of.year, as_of.year + 1):
        year_end = date(year, month, min(day, _cal.monthrange(year, month)[1]))
        periods.append((year_end, "10-K"))
        for back in (3, 6, 9):
            periods.append((shift_months(year_end, -back), "10-Q"))
    return sorted((p, f) for p, f in periods if as_of - timedelta(days=400) <= p <= as_of)


def expected(submissions: dict[str, Any], as_of: date) -> dict[str, Any]:
    cls = filer_class(submissions.get("category"))
    k_days, q_days = DEADLINES[cls]
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms, filed, reported = recent.get("form") or [], recent.get("filingDate") or [], recent.get("reportDate") or []
    filings = [(str(f), fd, rd) for f, fd, rd in zip(forms, filed, reported) if str(f) in PERIODIC and fd and rd]
    periods = fiscal_periods(str(submissions.get("fiscalYearEnd") or ""), as_of)
    record: dict[str, Any] = {"class": cls, "fiscal_year_end": submissions.get("fiscalYearEnd"), "category": submissions.get("category")}
    record["received"] = [
        {"form": f, "filed": fd, "period_end": rd} for f, fd, rd in filings
        if as_of - timedelta(days=7) <= date.fromisoformat(fd) <= as_of
    ]
    if not periods:
        record["status"] = "unknown"
        return record
    period_end, form = periods[-1]
    due = period_end + timedelta(days=k_days if form == "10-K" else q_days)
    match = None
    for f, fd, rd in filings:
        if f.startswith(form[:4]) and abs((date.fromisoformat(rd) - period_end).days) <= 10:
            if match is None or fd < match:
                match = fd
    record.update({"period_end": period_end.isoformat(), "form": form, "due": due.isoformat(), "filed": match})
    if match:
        record["status"] = "received"
        record["days"] = (date.fromisoformat(match) - period_end).days
    elif as_of > due:
        record["status"] = "overdue"
        record["days"] = (as_of - due).days
    else:
        record["status"] = "due"
        record["days"] = (due - as_of).days
    upcoming = [(p, f) for p, f in fiscal_periods(str(submissions.get("fiscalYearEnd") or ""), as_of + timedelta(days=400)) if p > period_end]
    if upcoming:
        next_end, next_form = upcoming[0]
        record["next"] = {"period_end": next_end.isoformat(), "form": next_form, "due": (next_end + timedelta(days=k_days if next_form == "10-K" else q_days)).isoformat()}
    return record


def build(cache_dir: Path, data: Path, as_of: date) -> dict[str, Any]:
    index = json.loads((data / "index.json").read_text(encoding="utf-8"))
    out: dict[str, Any] = {"as_of": as_of.isoformat(), "tickers": {}}
    with BulkSecStore(cache_dir / "bulk" / "companyfacts.zip", cache_dir / "bulk" / "submissions.zip") as store:
        for row in index["tickers"]:
            if row.get("status") != "eligible":
                continue
            try:
                submissions = store.submissions(int(row["cik"]))
            except (SecError, ValueError, TypeError):
                continue
            out["tickers"][row["ticker"]] = expected(submissions, as_of)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="expected periodic filings for every eligible ticker")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/sec"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build(args.cache_dir, args.data, args.as_of)
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    print(args.out, len(result["tickers"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
