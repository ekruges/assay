"""Point-in-time rescans for past dates, merged into one grade change log.

    python3 scripts/backfill_history.py --cache-dir .cache/sec --market-db .cache/market/market.sqlite3 \
        --out backfill --dates 2025-08-21 2025-09-21 ... --parallel 4 [--final-index data/index.json]

Each date runs the all-ticker pipeline with facts filed on or before that date, keeps that
run's slim index, its detail record (the inputs behind every letter, for the point-in-time
page) and its summary, and discards the rest of the tree. Dates are then replayed in
order through the same change-point recorder the nightly uses, so the result is the file the
nightly carries forward. The packaged S&P reference only covers dates within 120 days after
its measurement, so runs before that window have no failure-model input; the grade still
resolves from the remaining inputs.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assay.history import empty_history, record_grade, write_grade_history  # noqa: E402
from assay.export import detail, slim_index  # noqa: E402


def run_date(day: str, args: argparse.Namespace) -> tuple[str, bool]:
    run_dir = args.out / "runs" / day
    keep = args.out / "index" / f"{day}.json"
    if keep.exists():
        return day, True
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "assay", "--all", "--as-of", day,
        "--cache-dir", str(args.cache_dir), "--market-db", str(args.market_db),
        "--output-dir", str(run_dir / "data"), "--json",
    ]
    log = args.out / "logs" / f"{day}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as handle:
        result = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=handle, text=True)
    if result.returncode != 0:
        return day, False
    keep.parent.mkdir(parents=True, exist_ok=True)
    index = json.loads((run_dir / "data" / "index.json").read_text(encoding="utf-8"))
    keep.write_text(json.dumps(slim_index(index), separators=(",", ":")), encoding="utf-8")
    (args.out / "detail").mkdir(parents=True, exist_ok=True)
    (args.out / "detail" / f"{day}.json").write_text(json.dumps(detail(run_dir / "data", index), separators=(",", ":")), encoding="utf-8")
    (args.out / "summary").mkdir(parents=True, exist_ok=True)
    (args.out / "summary" / f"{day}.json").write_text(result.stdout, encoding="utf-8")
    shutil.rmtree(run_dir, ignore_errors=True)
    return day, True


def merge(args: argparse.Namespace) -> Path:
    history = empty_history()
    days = sorted(args.dates)
    for day in days:
        index = json.loads((args.out / "index" / f"{day}.json").read_text(encoding="utf-8"))
        _record(history, index, date.fromisoformat(day))
    if args.final_index:
        index = json.loads(Path(args.final_index).read_text(encoding="utf-8"))
        _record(history, index, date.fromisoformat(index["as_of"]))
    target = args.out / "grade_history.json"
    write_grade_history(target, history)
    return target


def _record(history: dict, index: dict, day: date) -> None:
    for row in index["tickers"]:
        if row.get("status") != "eligible":
            continue
        record_grade(history, row["ticker"], day, row.get("grade"), row.get("percentile"), row.get("stratum"))
    history["updated"] = day.isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--market-db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--final-index", type=Path, help="index.json of the current run, recorded last")
    parser.add_argument("--merge-only", action="store_true")
    args = parser.parse_args()
    for day in args.dates:
        date.fromisoformat(day)
    if not args.merge_only:
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            results = list(pool.map(lambda d: run_date(d, args), sorted(args.dates)))
        failed = [day for day, ok in results if not ok]
        if failed:
            print(f"failed: {', '.join(failed)}; see {args.out / 'logs'}", file=sys.stderr)
            return 1
    print(merge(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
