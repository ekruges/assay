"""The nightly run record: one JSON line per run, read by the run log page and status.json.

    python3 -m assay.runlog --runs .state/runs.jsonl --data data --verification verification-summary.json \
        --started 2026-09-27T07:00:12Z --log logs/2026-09-27.log
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _finish(started: str) -> tuple[str, int]:
    finished = datetime.now(timezone.utc).replace(microsecond=0)
    begun = datetime.fromisoformat(started.replace("Z", "+00:00"))
    return finished.isoformat().replace("+00:00", "Z"), int((finished - begun).total_seconds())


def _append(runs: Path, entry: dict[str, Any]) -> dict[str, Any]:
    runs.parent.mkdir(parents=True, exist_ok=True)
    with open(runs, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


def record_failure(runs: Path, as_of: str, started: str, log: str | None) -> dict[str, Any]:
    """Append the record of a run that did not complete; the log holds the reason."""
    finished, seconds = _finish(started)
    return _append(runs, {"as_of": as_of, "started": started, "finished": finished, "seconds": seconds, "failed": True, "verified": False, "log": log})


def record(runs: Path, data: Path, verification: Path | None, started: str, log: str | None, history: Path | None = None) -> dict[str, Any]:
    """Append this run's record and return it. The verifier exits non-zero on any mismatch, so its summary exists only for a verified tree."""
    index = json.loads((data / "index.json").read_text(encoding="utf-8"))
    rows = index["tickers"]
    graded = [r for r in rows if r.get("status") == "eligible" and r.get("grade")]
    letters = {g: sum(1 for r in graded if r["grade"][0] == g) for g in "ABCDE"}
    verified = None
    if verification and verification.exists():
        try:
            summary = json.loads(verification.read_text(encoding="utf-8"))
            verified = summary.get("as_of") == index["as_of"] and "tickers" in summary
        except ValueError:
            verified = False
    changes = None
    if history and history.exists():
        log_data = json.loads(history.read_text(encoding="utf-8"))
        changes = sum(1 for entry in log_data.get("tickers", {}).values() for c in entry.get("changes", []) if c[0] == index["as_of"] and c[1])
    finished, seconds = _finish(started)
    return _append(runs, {
        "as_of": index["as_of"], "started": started, "finished": finished, "seconds": seconds, "tickers": len(rows),
        "eligible": sum(1 for r in rows if r.get("status") == "eligible"), "graded": len(graded), "letters": letters,
        "letter_changes": changes, "verified": verified, "failed": False, "log": log,
    })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="append the nightly run record")
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--verification", type=Path)
    parser.add_argument("--history", type=Path, help="grade_history.json, for the count of letters changed by this run")
    parser.add_argument("--started", required=True, help="UTC start time, ISO 8601")
    parser.add_argument("--log", help="site-relative path of this run's log")
    parser.add_argument("--failed", metavar="AS_OF", help="record a run that did not complete, for this rescan date")
    args = parser.parse_args(argv)
    if args.failed:
        print(json.dumps(record_failure(args.runs, args.failed, args.started, args.log)))
        return 0
    print(json.dumps(record(args.runs, args.data, args.verification, args.started, args.log, args.history)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
