from __future__ import annotations

import json
import math
import os
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .market import PriceBar


ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
ALPACA_SNAPSHOTS_URL = "https://data.alpaca.markets/v2/stocks/snapshots"
NEW_YORK = ZoneInfo("America/New_York")


class MarketDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketSyncResult:
    requested_tickers: int
    tickers_with_data: int
    complete_tickers: int
    tickers_with_data_by_adjustment: dict[str, int]
    bars_written: int
    start: str
    end: str
    feed: str
    adjustments: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return vars(self)


@dataclass(frozen=True)
class SnapshotSyncResult:
    requested_tickers: int
    snapshots_written: int
    retrieved_at: str
    feed: str

    def to_dict(self) -> dict[str, Any]:
        return vars(self)


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    feed: str
    retrieved_at: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        trade = self.payload.get("latestTrade") or {}
        quote = self.payload.get("latestQuote") or {}
        minute = self.payload.get("minuteBar") or {}
        daily = self.payload.get("dailyBar") or {}
        previous = self.payload.get("prevDailyBar") or {}
        price = _positive_float(trade.get("p")) or _positive_float(minute.get("c"))
        previous_close = _positive_float(previous.get("c"))
        bid = _positive_float(quote.get("bp"))
        ask = _positive_float(quote.get("ap"))
        change = price - previous_close if price and previous_close else None
        return {
            "status": "resolved" if price else "unresolved",
            "symbol": self.symbol,
            "feed": self.feed,
            "retrieved_at": self.retrieved_at,
            "price": price,
            "price_timestamp": trade.get("t") or minute.get("t"),
            "previous_close": previous_close,
            "day_change": change,
            "day_change_percent": change / previous_close if change is not None else None,
            "bid": bid,
            "ask": ask,
            "quoted_spread": ask - bid if bid and ask and ask >= bid else None,
            "quoted_spread_percent": (
                (ask - bid) / ((ask + bid) / 2)
                if bid and ask and ask >= bid
                else None
            ),
            "minute_bar": minute or None,
            "daily_bar": daily or None,
            "feeds_grade": False,
            "reason": None if price else "missing_live_price",
        }


class AlpacaClient:
    def __init__(
        self,
        key_id: str | None = None,
        secret_key: str | None = None,
        *,
        requests_per_minute: int = 180,
        base_url: str = ALPACA_BARS_URL,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self.key_id = key_id or os.environ.get("APCA_API_KEY_ID")
        self.secret_key = secret_key or os.environ.get("APCA_API_SECRET_KEY")
        self.minimum_interval = 60 / requests_per_minute
        self.base_url = base_url
        self._last_request_at = 0.0

    def fetch_snapshots(
        self, symbols: Iterable[str], *, feed: str = "iex"
    ) -> dict[str, dict[str, Any]]:
        wanted = sorted({symbol.upper() for symbol in symbols if symbol})
        if not wanted:
            return {}
        if feed not in {"sip", "iex", "delayed_sip"}:
            raise ValueError(f"unsupported live market-data feed {feed!r}")
        payload = self._request_url_json(
            ALPACA_SNAPSHOTS_URL,
            {"symbols": ",".join(wanted), "feed": feed},
        )
        return {
            symbol: value
            for symbol, value in payload.items()
            if symbol in wanted and isinstance(value, dict)
        }

    def fetch_bars(
        self,
        symbols: Iterable[str],
        start: date,
        end: date,
        *,
        adjustment: str = "split",
        feed: str = "sip",
    ) -> dict[str, list[PriceBar]]:
        wanted = sorted({symbol.upper() for symbol in symbols if symbol})
        if not wanted:
            return {}
        if adjustment not in {"raw", "split", "dividend", "all"}:
            raise ValueError(f"unsupported adjustment {adjustment!r}")
        if start > end:
            raise ValueError("market-data start date must not be after end date")

        output = {symbol: [] for symbol in wanted}
        page_token: str | None = None
        seen_tokens: set[str] = set()
        delayed_end = _market_history_end(end)
        while True:
            parameters = {
                "symbols": ",".join(wanted),
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": delayed_end,
                "limit": "10000",
                "adjustment": adjustment,
                "feed": feed,
                "sort": "asc",
                "asof": end.isoformat(),
            }
            if page_token:
                parameters["page_token"] = page_token
            payload = self._request_json(parameters)
            parsed = parse_alpaca_bars(payload)
            for symbol, bars in parsed.items():
                output.setdefault(symbol, []).extend(bars)
            next_token = payload.get("next_page_token")
            if not next_token:
                break
            if not isinstance(next_token, str) or next_token in seen_tokens:
                raise MarketDataError("Alpaca returned an invalid pagination token")
            seen_tokens.add(next_token)
            page_token = next_token
        return {
            symbol: sorted({bar.day: bar for bar in bars}.values(), key=lambda bar: bar.day)
            for symbol, bars in output.items()
        }

    def _request_json(self, parameters: dict[str, str]) -> dict[str, Any]:
        return self._request_url_json(self.base_url, parameters)

    def _request_url_json(
        self, url: str, parameters: dict[str, str]
    ) -> dict[str, Any]:
        if not self.key_id or not self.secret_key:
            raise MarketDataError(
                "Alpaca credentials are required in APCA_API_KEY_ID and "
                "APCA_API_SECRET_KEY"
            )
        query = urllib.parse.urlencode(parameters)
        request = urllib.request.Request(
            f"{url}?{query}",
            headers={
                "Accept": "application/json",
                "APCA-API-KEY-ID": self.key_id,
                "APCA-API-SECRET-KEY": self.secret_key,
            },
        )
        last_error: Exception | None = None
        for attempt in range(5):
            self._throttle()
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise MarketDataError("Alpaca response is not a JSON object")
                return payload
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504}:
                    detail = exc.read(1000).decode("utf-8", errors="replace")
                    raise MarketDataError(
                        f"Alpaca HTTP {exc.code}: {detail or exc.reason}"
                    ) from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                last_error = exc
            time.sleep(min(2**attempt, 16))
        raise MarketDataError(f"Alpaca request failed after retries: {last_error}")

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.minimum_interval:
            time.sleep(self.minimum_interval - elapsed)
        self._last_request_at = time.monotonic()


class MarketStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bars (
                symbol TEXT NOT NULL,
                day TEXT NOT NULL,
                adjustment TEXT NOT NULL,
                feed TEXT NOT NULL,
                close REAL NOT NULL,
                volume INTEGER,
                PRIMARY KEY (symbol, day, adjustment, feed)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                symbol TEXT NOT NULL,
                feed TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (symbol, feed)
            )
            """
        )
        self.connection.commit()

    def __enter__(self) -> MarketStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def put_bars(
        self,
        symbol: str,
        bars: Iterable[PriceBar],
        *,
        adjustment: str,
        feed: str,
    ) -> int:
        rows = [
            (
                symbol.upper(),
                bar.day.isoformat(),
                adjustment,
                feed,
                bar.close,
                bar.volume,
            )
            for bar in bars
        ]
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO bars (symbol, day, adjustment, feed, close, volume)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, day, adjustment, feed) DO UPDATE SET
                    close=excluded.close,
                    volume=excluded.volume
                """,
                rows,
            )
        return len(rows)

    def bars(
        self,
        symbol: str,
        *,
        adjustment: str = "split",
        feed: str = "sip",
        start: date | None = None,
        end: date | None = None,
    ) -> list[PriceBar]:
        conditions = ["symbol = ?", "adjustment = ?", "feed = ?"]
        values: list[str] = [symbol.upper(), adjustment, feed]
        if start:
            conditions.append("day >= ?")
            values.append(start.isoformat())
        if end:
            conditions.append("day <= ?")
            values.append(end.isoformat())
        rows = self.connection.execute(
            "SELECT day, close, volume FROM bars WHERE "
            + " AND ".join(conditions)
            + " ORDER BY day",
            values,
        )
        return [
            PriceBar(date.fromisoformat(day), float(close), volume)
            for day, close, volume in rows
        ]

    def latest_day(
        self, symbol: str, *, adjustment: str, feed: str
    ) -> date | None:
        row = self.connection.execute(
            "SELECT MAX(day) FROM bars WHERE symbol = ? AND adjustment = ? AND feed = ?",
            (symbol.upper(), adjustment, feed),
        ).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def put_snapshot(self, snapshot: MarketSnapshot) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO snapshots (symbol, feed, retrieved_at, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol, feed) DO UPDATE SET
                    retrieved_at=excluded.retrieved_at,
                    payload=excluded.payload
                """,
                (
                    snapshot.symbol.upper(),
                    snapshot.feed,
                    snapshot.retrieved_at,
                    json.dumps(snapshot.payload, separators=(",", ":")),
                ),
            )

    def snapshot(self, symbol: str, *, feed: str = "iex") -> MarketSnapshot | None:
        row = self.connection.execute(
            "SELECT retrieved_at, payload FROM snapshots WHERE symbol = ? AND feed = ?",
            (symbol.upper(), feed),
        ).fetchone()
        if row is None:
            return None
        retrieved_at, payload = row
        return MarketSnapshot(
            symbol.upper(), feed, str(retrieved_at), json.loads(payload)
        )


def parse_alpaca_bars(payload: dict[str, Any]) -> dict[str, list[PriceBar]]:
    source = payload.get("bars", {})
    if source is None:
        return {}
    if not isinstance(source, dict):
        raise MarketDataError("Alpaca bars field is not an object")
    output: dict[str, list[PriceBar]] = {}
    for symbol, rows in source.items():
        if not isinstance(symbol, str) or not isinstance(rows, list):
            continue
        parsed: list[PriceBar] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                day = date.fromisoformat(str(row["t"])[:10])
                close = float(row["c"])
                volume = int(row["v"]) if row.get("v") is not None else None
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(close) and close > 0 and (
                volume is None or volume >= 0
            ):
                parsed.append(PriceBar(day, close, volume))
        output[symbol.upper()] = parsed
    return output


def sync_market_data(
    tickers: Iterable[str],
    client: AlpacaClient,
    store: MarketStore,
    start: date,
    end: date,
    *,
    adjustments: tuple[str, ...] = ("raw", "split", "all"),
    feed: str = "sip",
    batch_size: int = 100,
) -> MarketSyncResult:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if not adjustments:
        raise ValueError("at least one market-data adjustment is required")
    wanted, provider_to_assay = _provider_symbol_map(tickers)
    provider_symbols = sorted(provider_to_assay)
    with_data: set[str] = set()
    with_data_by_adjustment = {adjustment: set() for adjustment in adjustments}
    bars_written = 0
    for adjustment in adjustments:
        starts: dict[date, list[str]] = {}
        for provider_symbol in provider_symbols:
            ticker = provider_to_assay[provider_symbol]
            latest = store.latest_day(ticker, adjustment=adjustment, feed=feed)
            if latest is not None:
                with_data.add(ticker)
                with_data_by_adjustment[adjustment].add(ticker)
            fetch_start = max(start, latest - timedelta(days=7)) if latest else start
            starts.setdefault(fetch_start, []).append(provider_symbol)
        for fetch_start, symbols in sorted(starts.items()):
            if fetch_start > end:
                continue
            for offset in range(0, len(symbols), batch_size):
                batch = symbols[offset : offset + batch_size]
                response = client.fetch_bars(
                    batch, fetch_start, end, adjustment=adjustment, feed=feed
                )
                for provider_symbol, bars in response.items():
                    ticker = provider_to_assay.get(provider_symbol)
                    if ticker is None or not bars:
                        continue
                    bars_written += store.put_bars(
                        ticker, bars, adjustment=adjustment, feed=feed
                    )
                    with_data.add(ticker)
                    with_data_by_adjustment[adjustment].add(ticker)
    complete = set.intersection(*with_data_by_adjustment.values())
    return MarketSyncResult(
        len(wanted),
        len(with_data),
        len(complete),
        {
            adjustment: len(tickers)
            for adjustment, tickers in with_data_by_adjustment.items()
        },
        bars_written,
        start.isoformat(),
        end.isoformat(),
        feed,
        adjustments,
    )


def sync_market_snapshots(
    tickers: Iterable[str],
    client: AlpacaClient,
    store: MarketStore,
    *,
    feed: str = "iex",
    batch_size: int = 100,
    retrieved_at: datetime | None = None,
) -> SnapshotSyncResult:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    wanted, provider_to_assay = _provider_symbol_map(tickers)
    provider_symbols = sorted(provider_to_assay)
    timestamp = (retrieved_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    timestamp_text = timestamp.isoformat().replace("+00:00", "Z")
    written = 0
    for offset in range(0, len(provider_symbols), batch_size):
        batch = provider_symbols[offset : offset + batch_size]
        response = client.fetch_snapshots(batch, feed=feed)
        for provider_symbol, payload in response.items():
            ticker = provider_to_assay.get(provider_symbol)
            if ticker is None:
                continue
            store.put_snapshot(
                MarketSnapshot(ticker, feed, timestamp_text, payload)
            )
            written += 1
    return SnapshotSyncResult(len(wanted), written, timestamp_text, feed)


def alpaca_symbol(ticker: str) -> str:
    return ticker.upper().replace("-", ".")


def _provider_symbol_map(
    tickers: Iterable[str],
) -> tuple[list[str], dict[str, str]]:
    wanted = sorted({ticker.upper() for ticker in tickers if ticker})
    provider_to_assay: dict[str, str] = {}
    for ticker in wanted:
        provider_symbol = alpaca_symbol(ticker)
        previous = provider_to_assay.get(provider_symbol)
        if previous is not None and previous != ticker:
            raise MarketDataError(
                f"tickers {previous} and {ticker} map to the same Alpaca symbol "
                f"{provider_symbol}"
            )
        provider_to_assay[provider_symbol] = ticker
    return wanted, provider_to_assay


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _market_history_end(end: date, now: datetime | None = None) -> str:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    market_now = now_utc.astimezone(NEW_YORK)
    latest_closed = market_now.date()
    if (market_now.hour, market_now.minute) < (16, 16):
        latest_closed -= timedelta(days=1)
    effective_day = min(end, latest_closed)
    if effective_day == market_now.date():
        cutoff = now_utc - timedelta(minutes=16)
    else:
        cutoff = datetime.combine(
            effective_day + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ) - timedelta(seconds=1)
    return cutoff.isoformat().replace("+00:00", "Z")
