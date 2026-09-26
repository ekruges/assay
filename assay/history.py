"""Grade history: one change point per ticker per change, carried between nightly runs.

The file stores only the nights on which a ticker's letter or size stratum changed,
plus the last night it was seen. That is what the price chart marks, and it keeps the
file to a few megabytes a year across the whole universe.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def empty_history() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "updated": None, "tickers": {}}


def read_grade_history(path: str | Path) -> dict[str, Any]:
    location = Path(path)
    if not location.exists() or location.stat().st_size == 0:
        return empty_history()
    history = json.loads(location.read_text(encoding="utf-8"))
    if (
        not isinstance(history, dict)
        or history.get("schema_version") != SCHEMA_VERSION
        or not isinstance(history.get("tickers"), dict)
    ):
        raise ValueError(f"{location} is not a grade history file")
    for ticker, entry in history["tickers"].items():
        changes = entry.get("changes") if isinstance(entry, dict) else None
        if not isinstance(changes, list) or any(
            not isinstance(point, list) or len(point) != 4 for point in changes
        ):
            raise ValueError(f"{location} has a malformed entry for {ticker}")
    return history


def write_grade_history(path: str | Path, history: dict[str, Any]) -> None:
    location = Path(path)
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_text(
        json.dumps(history, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )


def record_grade(
    history: dict[str, Any],
    ticker: str,
    as_of: date,
    grade: str | None,
    percentile: float | None,
    stratum: str | None,
) -> None:
    day = as_of.isoformat()
    entry = history["tickers"].setdefault(ticker, {"last_seen": None, "changes": []})
    changes = entry["changes"]
    point = [day, grade, percentile, stratum]
    if changes:
        last_day, last_grade, _, last_stratum = changes[-1]
        if day < last_day:
            raise ValueError(
                f"{ticker}: cannot record {day} before the existing entry for {last_day}"
            )
        if day == last_day:
            changes[-1] = point
        elif last_grade != grade or last_stratum != stratum:
            changes.append(point)
    else:
        changes.append(point)
    entry["last_seen"] = day
    history["updated"] = max(history.get("updated") or day, day)


def ticker_history(history: dict[str, Any], ticker: str) -> list[dict[str, Any]]:
    entry = history["tickers"].get(ticker)
    if not entry:
        return []
    return [
        {"date": day, "grade": grade, "percentile": percentile, "stratum": stratum}
        for day, grade, percentile, stratum in entry["changes"]
    ]
