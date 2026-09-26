"""Machine-readable exports and the per-rescan records.

    python3 -m assay.export --data data --history-index .state/history --detail .state/detail --snapshot
    python3 -m assay.export --data data --history-index .state/history --detail .state/detail --out site

A snapshot writes the current run's slim index and detail file under their folders and applies the
retention rule: every rescan within 92 days, and the first rescan on or after the 21st of every
earlier month. The site build copies the retained records, writes the rescan cube, the long-format
CSV files and one SQLite database.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import shutil
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .render import _company_name

FLAGS = ("dilution", "short_runway", "late_filer", "thin_data", "degenerate_inputs", "distress", "fortress")
SLIM_KEYS = ("ticker", "cik", "status", "sector", "sic", "exchange", "grade", "percentile", "composite", "sampling_standard_deviation",
             "stratum", "standard_universe", "data_age_days", "filing_gap_days", "ceased", "active_flags", "implied_expectations")
RETENTION_DAYS = 92


def slim_index(index: dict[str, Any]) -> dict[str, Any]:
    """The record kept for a rescan: eligible rows with the fields the history pages and exports read. The snapshot
    price is left out because a backfilled run carries the price of its run day, not of its rescan date."""
    rows = []
    for r in index["tickers"]:
        if r.get("status") != "eligible":
            continue
        row = {k: r[k] for k in SLIM_KEYS if r.get(k) is not None}
        row["name"] = _company_name(r.get("name") or r["ticker"])
        row["inputs_computable"] = (r.get("coverage") or {}).get("resolved", r.get("inputs_computable"))
        rows.append(row)
    return {"as_of": index["as_of"], "tickers": rows}


def detail(data: Path, index: dict[str, Any]) -> dict[str, Any]:
    """Per ticker for one rescan: sleeve scores, the fourteen inputs with percentile, peer count, period end and
    receipts, and the active warnings. Input rows are [name, sleeve, value, percentile, peers, period_end, accessions, status, unit]."""
    tickers: dict[str, Any] = {}
    for r in index["tickers"]:
        if r.get("status") != "eligible":
            continue
        try:
            report = json.loads((data / r["artifact"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        grade = report.get("grade") or {}
        percentiles = grade.get("factor_percentiles") or {}
        receipts: dict[str, list[Any]] = {}
        inputs = []
        for f in report.get("factors", []):
            pc = percentiles.get(f["name"]) or {}
            accessions: list[str] = []
            for fact in f.get("inputs") or []:
                accession = fact.get("accession")
                if accession and accession not in accessions:
                    accessions.append(accession)
                if accession and accession not in receipts:
                    receipts[accession] = [fact.get("form"), fact.get("filed"), fact.get("filing_url")]
            inputs.append([f["name"], f.get("sleeve"), f.get("value"), pc.get("percentile"), pc.get("peer_count"), f.get("period_end"), accessions, f.get("status"), f.get("unit")])
        warnings = [[fl["name"], fl.get("detail")] for fl in report.get("flags", []) if fl.get("active")]
        tickers[r["ticker"]] = {"sleeves": grade.get("sleeve_scores"), "inputs": inputs, "receipts": receipts, "warnings": warnings}
    return {"as_of": index["as_of"], "tickers": tickers}


def retained(dates: list[str], as_of: str, days: int = RETENTION_DAYS) -> list[str]:
    """Every rescan within `days` of as_of, plus the first rescan on or after the 21st of each earlier month."""
    cutoff = (date.fromisoformat(as_of) - timedelta(days=days)).isoformat()
    keep = {d for d in dates if d >= cutoff}
    anchors: dict[str, str] = {}
    for d in sorted(dates):
        if int(d[8:10]) >= 21:
            anchors.setdefault(d[:7], d)
    return sorted(keep | set(anchors.values()))


def snapshot(data: Path, index_dir: Path, detail_dir: Path, days: int = RETENTION_DAYS) -> dict[str, Any]:
    """Write the current run's records under the state folders and drop the records outside retention."""
    index = json.loads((data / "index.json").read_text(encoding="utf-8"))
    as_of = index["as_of"]
    index_dir.mkdir(parents=True, exist_ok=True)
    detail_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / f"{as_of}.json").write_text(json.dumps(slim_index(index), separators=(",", ":")), encoding="utf-8")
    (detail_dir / f"{as_of}.json").write_text(json.dumps(detail(data, index), separators=(",", ":")), encoding="utf-8")
    keep = set(retained(sorted(p.stem for p in index_dir.glob("*.json")), as_of, days))
    removed = 0
    for folder in (index_dir, detail_dir):
        for path in folder.glob("*.json"):
            if path.stem not in keep:
                path.unlink()
                removed += 1
    return {"as_of": as_of, "kept": len(keep), "removed": removed}


def load_indexes(history_index: Path | None, current: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """as_of -> index rows, oldest first, with the current run included."""
    out: dict[str, list[dict[str, Any]]] = {}
    if history_index and history_index.exists():
        for path in sorted(history_index.glob("*.json")):
            try:
                index = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out[index["as_of"]] = index["tickers"]
    out[current["as_of"]] = current["tickers"]
    return dict(sorted(out.items()))


def cube(indexes: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """dates, and per ticker two aligned arrays: letters and percentiles (null where not graded)."""
    dates = list(indexes)
    tickers: dict[str, dict[str, list[Any]]] = {}
    for i, (day, rows) in enumerate(indexes.items()):
        for r in rows:
            if r.get("status") != "eligible":
                continue
            entry = tickers.setdefault(r["ticker"], {"letters": [None] * len(dates), "percentiles": [None] * len(dates), "sector": r.get("sector"), "name": r.get("name")})
            entry["letters"][i] = r.get("grade")
            entry["percentiles"][i] = round(r["percentile"], 4) if isinstance(r.get("percentile"), (int, float)) else None
            entry["sector"] = r.get("sector") or entry["sector"]
            entry["name"] = r.get("name") or entry["name"]
    return {"dates": dates, "tickers": tickers}


def rescans_csv(indexes: dict[str, list[dict[str, Any]]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["as_of", "ticker", "cik", "sector", "letter", "percentile", "composite", "stratum", "momentum_12_1"])
    for day, rows in indexes.items():
        for r in rows:
            if r.get("status") != "eligible":
                continue
            writer.writerow([day, r["ticker"], r.get("cik"), r.get("sector"), r.get("grade"), r.get("percentile"), r.get("composite"), r.get("stratum"), (r.get("implied_expectations") or {}).get("momentum_12_1")])
    return gzip.compress(buffer.getvalue().encode("utf-8"))


def index_csv(index: dict[str, Any]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    ie_keys = ["momentum_12_1", "distance_from_52_week_high", "ebit_to_enterprise_value", "fcf_yield", "book_to_price", "sales_to_price", "composite_equity_issuance_5y", "sector_relative_strength"]
    writer.writerow(["ticker", "cik", "name", "status", "reason", "exchange", "sector", "sic", "letter", "percentile", "composite", "sampling_sd", "stratum", "standard_universe", "inputs_computable", "data_age_days", "filing_gap_days", "ceased", "live_price", "active_flags", *ie_keys])
    for r in index["tickers"]:
        ie = r.get("implied_expectations") or {}
        writer.writerow([r["ticker"], r.get("cik"), r.get("name"), r.get("status"), r.get("reason"), r.get("exchange"), r.get("sector"), r.get("sic"), r.get("grade"), r.get("percentile"), r.get("composite"),
                         r.get("sampling_standard_deviation"), r.get("stratum"), r.get("standard_universe"), (r.get("coverage") or {}).get("resolved"), r.get("data_age_days"), r.get("filing_gap_days"), r.get("ceased"),
                         r.get("live_price"), ";".join(r.get("active_flags") or []), *[ie.get(k) for k in ie_keys]])
    return buffer.getvalue().encode("utf-8")


def inputs_rows(data: Path, index: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for r in index["tickers"]:
        if r.get("status") != "eligible":
            continue
        try:
            report = json.loads((data / r["artifact"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        percentiles = (report.get("grade") or {}).get("factor_percentiles") or {}
        for f in report.get("factors", []):
            pc = percentiles.get(f["name"]) or {}
            rows.append([index["as_of"], r["ticker"], f["name"], f.get("sleeve"), f.get("value"), pc.get("percentile"), pc.get("peer_count"), f.get("period_end"), f.get("reason")])
    return rows


def inputs_csv(rows: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["as_of", "ticker", "input", "sleeve", "value", "percentile", "peers", "period_end", "reason"])
    writer.writerows(rows)
    return gzip.compress(buffer.getvalue().encode("utf-8"))


def sqlite_db(index: dict[str, Any], indexes: dict[str, list[dict[str, Any]]], inputs: list[list[Any]]) -> bytes:
    db = sqlite3.connect(":memory:")
    db.executescript("""
        create table universe (ticker text primary key, cik integer, name text, status text, reason text, exchange text, sector text, sic integer,
            letter text, percentile real, composite real, sampling_sd real, stratum text, standard_universe integer, inputs_computable integer,
            data_age_days integer, filing_gap_days integer, ceased integer, live_price real, active_flags text);
        create table rescans (as_of text, ticker text, sector text, letter text, percentile real, composite real, stratum text, momentum_12_1 real, primary key (as_of, ticker));
        create table inputs (as_of text, ticker text, input text, sleeve text, value real, percentile real, peers integer, period_end text, reason text);
        create table warnings (ticker text, warning text, primary key (ticker, warning));
        create index rescans_ticker on rescans (ticker, as_of);
        create index inputs_ticker on inputs (ticker);
    """)
    for r in index["tickers"]:
        db.execute("insert or replace into universe values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            r["ticker"], r.get("cik"), r.get("name"), r.get("status"), r.get("reason"), r.get("exchange"), r.get("sector"), r.get("sic"), r.get("grade"), r.get("percentile"), r.get("composite"),
            r.get("sampling_standard_deviation"), r.get("stratum"), None if r.get("standard_universe") is None else int(bool(r["standard_universe"])), (r.get("coverage") or {}).get("resolved"),
            r.get("data_age_days"), r.get("filing_gap_days"), None if r.get("ceased") is None else int(bool(r["ceased"])), r.get("live_price"), ";".join(r.get("active_flags") or [])))
        for flag in r.get("active_flags") or []:
            db.execute("insert or replace into warnings values (?,?)", (r["ticker"], flag))
    for day, rows in indexes.items():
        db.executemany("insert or replace into rescans values (?,?,?,?,?,?,?,?)", [
            (day, r["ticker"], r.get("sector"), r.get("grade"), r.get("percentile"), r.get("composite"), r.get("stratum"), (r.get("implied_expectations") or {}).get("momentum_12_1"))
            for r in rows if r.get("status") == "eligible"])
    db.executemany("insert into inputs values (?,?,?,?,?,?,?,?,?)", inputs)
    db.commit()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "assay.sqlite3"
        disk = sqlite3.connect(path)
        db.backup(disk)
        disk.close()
        return gzip.compress(path.read_bytes())


def build(data: Path, out: Path, history_index: Path | None, detail_dir: Path | None = None) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    index = json.loads((data / "index.json").read_text(encoding="utf-8"))
    indexes = load_indexes(history_index, index)
    for folder in ("history/index", "history/detail"):
        (out / folder).mkdir(parents=True, exist_ok=True)
    for day, rows in indexes.items():
        target = out / "history" / "index" / f"{day}.json"
        source = history_index / f"{day}.json" if history_index else None
        if source and source.exists():
            shutil.copyfile(source, target)
        else:
            target.write_text(json.dumps(slim_index({"as_of": day, "tickers": rows}), separators=(",", ":")), encoding="utf-8")
    if detail_dir and detail_dir.exists():
        for path in detail_dir.glob("*.json"):
            if path.stem in indexes:
                shutil.copyfile(path, out / "history" / "detail" / path.name)
    (out / "history" / "detail" / f"{index['as_of']}.json").write_text(json.dumps(detail(data, index), separators=(",", ":")), encoding="utf-8")
    c = cube(indexes)
    (out / "history" / "dates.json").write_text(json.dumps(c["dates"]), encoding="utf-8")
    (out / "history" / "cube.json").write_text(json.dumps(c, separators=(",", ":")), encoding="utf-8")
    (out / "history" / "rescans.csv.gz").write_bytes(rescans_csv(indexes))
    (out / "index.csv").write_bytes(index_csv(index))
    rows = inputs_rows(data, index)
    (out / "inputs.csv.gz").write_bytes(inputs_csv(rows))
    (out / "assay.sqlite3.gz").write_bytes(sqlite_db(index, indexes, rows))
    return {"dates": len(c["dates"]), "tickers": len(c["tickers"]), "input_rows": len(rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="write the per-rescan records or the machine-readable exports")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--history-index", type=Path, help="folder of per-date slim index files")
    parser.add_argument("--detail", type=Path, help="folder of per-date detail files")
    parser.add_argument("--out", type=Path, help="site folder for the exports")
    parser.add_argument("--snapshot", action="store_true", help="record the current run under --history-index and --detail, then prune")
    args = parser.parse_args(argv)
    if args.snapshot:
        if not (args.history_index and args.detail):
            parser.error("--snapshot needs --history-index and --detail")
        print(json.dumps(snapshot(args.data, args.history_index, args.detail)))
        return 0
    if not args.out:
        parser.error("--out is required unless --snapshot")
    print(json.dumps(build(args.data, args.out, args.history_index, args.detail)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
