"""Split-adjusted closes per eligible ticker: twelve months in one file for the static charts and the PDFs, or the
whole history as one compact file per ticker for the scrollable chart.

    python3 -m assay.prices --data data --market-db .cache/market/market.sqlite3 --as-of 2026-08-21 --out prices.json
    python3 -m assay.prices --data data --market-db .cache/market/market.sqlite3 --as-of 2026-08-21 --full --out-dir site/prices
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path


def export(market_db: Path, tickers: list[str], as_of: date, days: int = 366, feed: str = "sip") -> dict[str, list[list[object]]]:
    start = (as_of - timedelta(days=days)).isoformat()
    out: dict[str, list[list[object]]] = {}
    with closing(sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)) as db:
        for symbol in tickers:
            rows = db.execute(
                "select day, close from bars where symbol=? and adjustment='split' and feed=? and day>=? and day<=? order by day",
                (symbol, feed, start, as_of.isoformat()),
            ).fetchall()
            if len(rows) >= 20:
                out[symbol] = [[day, round(close, 4)] for day, close in rows]
    return out


def export_full(market_db: Path, tickers: list[str], as_of: date, out_dir: Path, feed: str = "sip", start: str = "2011-01-01") -> int:
    """One file per ticker, {"d": [days], "c": [closes]}, every bar from `start` to the rescan date."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    with closing(sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)) as db:
        for symbol in tickers:
            rows = db.execute(
                "select day, close from bars where symbol=? and adjustment='split' and feed=? and day>=? and day<=? order by day",
                (symbol, feed, start, as_of.isoformat()),
            ).fetchall()
            if len(rows) < 20:
                continue
            payload = {"d": [day for day, _ in rows], "c": [round(close, 2) for _, close in rows]}
            (out_dir / f"{symbol}.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="export twelve months of closes per eligible ticker")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--market-db", type=Path, default=Path(".cache/market/market.sqlite3"))
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--feed", default="sip")
    parser.add_argument("--out", type=Path, help="one file with twelve months for every ticker")
    parser.add_argument("--full", action="store_true", help="write the whole history, one file per ticker, under --out-dir")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--tickers", nargs="*", help="limit to these tickers")
    args = parser.parse_args(argv)
    index = json.loads((args.data / "index.json").read_text(encoding="utf-8"))
    as_of = args.as_of or date.fromisoformat(index["as_of"])
    tickers = [r["ticker"] for r in index["tickers"] if r.get("status") == "eligible"]
    if args.tickers:
        wanted = {t.upper() for t in args.tickers}
        tickers = [t for t in tickers if t in wanted]
    if args.full:
        if not args.out_dir:
            parser.error("--full needs --out-dir")
        print(args.out_dir, export_full(args.market_db, tickers, as_of, args.out_dir, feed=args.feed), "of", len(tickers))
        return 0
    if not args.out:
        parser.error("--out is required unless --full")
    prices = export(args.market_db, tickers, as_of, feed=args.feed)
    args.out.write_text(json.dumps(prices, separators=(",", ":")), encoding="utf-8")
    print(args.out, len(prices), "of", len(tickers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
