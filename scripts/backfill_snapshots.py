"""Site snapshots for past rescan dates, published under wayback/<date>/ like the nightly's own.

    python3 scripts/backfill_snapshots.py --cache-dir .cache/sec --market-db .cache/market/market.sqlite3 \
        --state .state --work /srv/backfill --publish-ssh ezra@host --publish-dir /srv/www/assay \
        --dates 2026-10-18 2026-10-17 ... [--parallel 1]

Each date runs the all-ticker pipeline with facts filed on or before that date, records the
rescan's slim index and detail under the state folders, replays the change log up to that
date, builds the whole site from that tree (no PDFs, no descriptions, no calendar) and
uploads it as that day's copy. Price history and the records folder are left out of the
copy, as the nightly publish does. A date whose copy already exists on the host is skipped.
One date at a time, and never while the nightly runs: a pipeline run holds about 3.5GB.
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

from assay.export import detail, slim_index  # noqa: E402
from assay.history import empty_history, record_grade, write_grade_history  # noqa: E402


def history_through(state_history: Path, day: str, target: Path) -> int:
    """Replay the slim indexes dated on or before `day` into a change log at `target`; returns the dates used."""
    history = empty_history()
    days = sorted(p.stem for p in state_history.glob("*.json") if p.stem <= day)
    for d in days:
        index = json.loads((state_history / f"{d}.json").read_text(encoding="utf-8"))
        for row in index["tickers"]:
            if row.get("status") == "eligible":
                record_grade(history, row["ticker"], date.fromisoformat(d), row.get("grade"), row.get("percentile"), row.get("stratum"))
        history["updated"] = d
    write_grade_history(target, history)
    return len(days)


def index_subset(state_history: Path, day: str, target: Path) -> int:
    """A folder of links to the slim indexes dated on or before `day`."""
    target.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in sorted(state_history.glob("*.json")):
        if p.stem <= day:
            link = target / p.name
            if not link.exists():
                link.symlink_to(p.resolve())
            n += 1
    return n


def run(cmd: list[str], log, cwd: Path = ROOT) -> None:
    log.write("$ " + " ".join(cmd) + "\n")
    log.flush()
    subprocess.run(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=True)


def published(args: argparse.Namespace, day: str) -> bool:
    probe = subprocess.run(["ssh", args.publish_ssh, f"test -f '{args.publish_dir}/wayback/{day}/index.html'"], capture_output=True)
    return probe.returncode == 0


def wait_for_nightly(args: argparse.Namespace) -> None:
    """Hold while the nightly service runs, so the two never share the container's memory."""
    import time
    while args.pause_for and subprocess.run(["systemctl", "is-active", "--quiet", args.pause_for]).returncode == 0:
        time.sleep(60)


def snapshot(day: str, args: argparse.Namespace) -> tuple[str, str]:
    if published(args, day):
        return day, "kept"
    wait_for_nightly(args)
    work = args.work / day
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    (args.work / "logs").mkdir(exist_ok=True)
    with open(args.work / "logs" / f"{day}.log", "w", encoding="utf-8") as log:
        try:
            data = work / "data"
            run([sys.executable, "-m", "assay", "--all", "--as-of", day, "--cache-dir", str(args.cache_dir), "--market-db", str(args.market_db), "--output-dir", str(data), "--json"], log)
            index = json.loads((data / "index.json").read_text(encoding="utf-8"))
            (args.state / "history").mkdir(parents=True, exist_ok=True)
            (args.state / "detail").mkdir(parents=True, exist_ok=True)
            (args.state / "history" / f"{day}.json").write_text(json.dumps(slim_index(index), separators=(",", ":")), encoding="utf-8")
            (args.state / "detail" / f"{day}.json").write_text(json.dumps(detail(data, index), separators=(",", ":")), encoding="utf-8")
            history_through(args.state / "history", day, work / "grade_history.json")
            index_subset(args.state / "history", day, work / "hist")
            run([sys.executable, "-m", "assay.prices", "--data", str(data), "--market-db", str(args.market_db), "--as-of", day, "--out", str(work / "prices.json")], log)
            run([sys.executable, "-m", "assay.site", "--data", str(data), "--out", str(work / "site"), "--all", "--history", str(work / "grade_history.json"),
                 "--history-index", str(work / "hist"), "--prices", str(work / "prices.json"), "--asset-base", "img/", "--site-root", args.site_root, "--page-link", "./"], log)
            target = f"{args.publish_dir}/wayback/{day}"
            upload = subprocess.Popen(["tar", "-C", str(work / "site"), "--exclude=./prices", "--exclude=./history", "-cf", "-", "."], stdout=subprocess.PIPE)
            remote = subprocess.run(["ssh", args.publish_ssh, f"rm -rf '{target}' && mkdir -p '{target}' && tar -xf - -C '{target}'"], stdin=upload.stdout, stdout=log, stderr=subprocess.STDOUT)
            upload.wait()
            if upload.returncode != 0 or remote.returncode != 0:
                raise RuntimeError(f"upload failed: tar {upload.returncode}, remote {remote.returncode}")
            subprocess.run(["ssh", args.publish_ssh, f"cd '{args.publish_dir}/wayback' && ls | grep -E '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$' | sort | sed 's/.*/\"&\"/' | paste -sd, - | sed 's/.*/[&]/' > dates.json"], stdout=log, stderr=subprocess.STDOUT, check=True)
        except Exception as exc:  # noqa: BLE001 - the log carries the detail; the driver reports the date
            log.write(f"FAILED {exc}\n")
            return day, "failed"
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return day, "done"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--market-db", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True, help="the .state folder with history/ and detail/")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--publish-ssh", required=True)
    parser.add_argument("--publish-dir", required=True)
    parser.add_argument("--site-root", default="/assay/")
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--pause-for", default="assay-nightly.service", help="a systemd unit to wait out before each date; empty to never wait")
    args = parser.parse_args()
    for day in args.dates:
        date.fromisoformat(day)
    args.work.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        for day, status in pool.map(lambda d: snapshot(d, args), args.dates):
            print(day, status, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
