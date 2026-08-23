from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from typing import Any, Sequence
from uuid import uuid4

from .market import PriceBar
from .market_data import MarketStore


def record_forecast(
    path: str | Path,
    market_store: MarketStore,
    ticker: str,
    as_of: date,
    *,
    horizon_days: int,
    probability: float,
    thesis: str,
    grade_at_call: str | None = None,
    market_feed: str = "sip",
    called_at: datetime | None = None,
) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    thesis = thesis.strip()
    if not ticker or ticker == "SPY":
        raise ValueError("forecast ticker must be a non-SPY symbol")
    if horizon_days <= 0:
        raise ValueError("forecast horizon must be positive")
    if not 0 < probability < 1:
        raise ValueError("forecast probability must be strictly between 0 and 1")
    if not thesis:
        raise ValueError("forecast thesis must not be empty")
    timestamp = called_at or datetime.now(timezone.utc).replace(microsecond=0)
    if timestamp.tzinfo is None:
        raise ValueError("forecast timestamp must include a timezone")
    stock_bar, spy_bar = _latest_common_bars(
        market_store, ticker, "SPY", as_of, market_feed
    )
    event = {
        "schema_version": 1,
        "event": "call",
        "id": str(uuid4()),
        "ts": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ticker": ticker,
        "horizon_days": horizon_days,
        "target_date": (stock_bar.day + timedelta(days=horizon_days)).isoformat(),
        "p_outperform_spy": probability,
        "thesis": thesis,
        "grade_at_call": grade_at_call,
        "price_basis": {
            "date": stock_bar.day.isoformat(),
            "adjustment": "all",
            "feed": market_feed,
            "stock_reference_close": stock_bar.close,
            "spy_reference_close": spy_bar.close,
        },
    }
    _append_event(path, event)
    return event


def settle_due_forecasts(
    path: str | Path,
    market_store: MarketStore,
    as_of: date,
    *,
    market_feed: str = "sip",
    resolved_at: datetime | None = None,
) -> dict[str, Any]:
    events = read_forecast_events(path)
    calls, resolved_ids = _index_events(events)
    timestamp = resolved_at or datetime.now(timezone.utc).replace(microsecond=0)
    if timestamp.tzinfo is None:
        raise ValueError("resolution timestamp must include a timezone")
    settled: list[str] = []
    for call in calls:
        if call["id"] in resolved_ids:
            continue
        target_date = date.fromisoformat(call["target_date"])
        if target_date > as_of:
            continue
        price_date = date.fromisoformat(call["price_basis"]["date"])
        bars = _settlement_bars(
            market_store,
            call["ticker"],
            price_date,
            target_date,
            as_of,
            market_feed,
        )
        if bars is None:
            continue
        stock_start, spy_start, stock_end, spy_end = bars
        stock_return = stock_end.close / stock_start.close - 1
        spy_return = spy_end.close / spy_start.close - 1
        outcome = int(stock_return > spy_return)
        probability = float(call["p_outperform_spy"])
        resolution = {
            "schema_version": 1,
            "event": "resolution",
            "call_id": call["id"],
            "resolved_at": timestamp.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "ticker": call["ticker"],
            "price_start_date": stock_start.day.isoformat(),
            "price_end_date": stock_end.day.isoformat(),
            "stock_start_close": stock_start.close,
            "stock_end_close": stock_end.close,
            "spy_start_close": spy_start.close,
            "spy_end_close": spy_end.close,
            "stock_return": stock_return,
            "spy_return": spy_return,
            "excess_return": stock_return - spy_return,
            "outcome": outcome,
            "brier": (probability - outcome) ** 2,
            "log_loss": _log_loss(probability, outcome),
        }
        _append_event(path, resolution)
        settled.append(call["id"])
    return {"settled": len(settled), "call_ids": settled, **forecast_scorecard(path)}


def forecast_scorecard(path: str | Path) -> dict[str, Any]:
    events = read_forecast_events(path)
    calls, _ = _index_events(events)
    calls_by_id = {call["id"]: call for call in calls}
    resolutions = [event for event in events if event["event"] == "resolution"]
    if not resolutions:
        return {
            "calls": len(calls),
            "resolved": 0,
            "pending": len(calls),
            "model": None,
            "base_rate_reference": None,
            "calibration": [],
        }
    outcomes = [int(event["outcome"]) for event in resolutions]
    probabilities = [
        float(calls_by_id[event["call_id"]]["p_outperform_spy"])
        for event in resolutions
    ]
    base_rate = fmean(outcomes)
    model_brier = fmean((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes))
    model_log_loss = fmean(
        _log_loss(probability, outcome)
        for probability, outcome in zip(probabilities, outcomes)
    )
    base_brier = fmean((base_rate - outcome) ** 2 for outcome in outcomes)
    base_log_loss = fmean(_log_loss(base_rate, outcome) for outcome in outcomes)
    return {
        "calls": len(calls),
        "resolved": len(resolutions),
        "pending": len(calls) - len(resolutions),
        "model": {
            "mean_brier": model_brier,
            "mean_log_loss": model_log_loss,
            "brier_skill_vs_observed_base_rate": (
                1 - model_brier / base_brier if base_brier > 0 else None
            ),
        },
        "base_rate_reference": {
            "observed_outperformance_rate": base_rate,
            "mean_brier": base_brier,
            "mean_log_loss": base_log_loss,
            "in_sample": True,
        },
        "calibration": _calibration(probabilities, outcomes),
    }


def read_forecast_events(path: str | Path) -> list[dict[str, Any]]:
    log_path = Path(path)
    if not log_path.exists():
        return []
    events: list[dict[str, Any]] = []
    with log_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid forecast log JSON on line {line_number}: {exc.msg}"
                ) from exc
            _validate_forecast_event(event, line_number)
            events.append(event)
    return events


def build_forecast_artifact(
    path: str | Path,
    as_of: date,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    timestamp = generated_at or datetime.now(timezone.utc).replace(microsecond=0)
    if timestamp.tzinfo is None:
        raise ValueError("forecast artifact timestamp must include a timezone")
    return {
        "schema_version": 1,
        "generated_at": timestamp.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "as_of": as_of.isoformat(),
        "scorecard": forecast_scorecard(path),
        "events": read_forecast_events(path),
    }


def publish_forecast_artifact(
    log_path: str | Path,
    output_path: str | Path,
    as_of: date,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    artifact = build_forecast_artifact(
        log_path, as_of, generated_at=generated_at
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=destination.parent, delete=False
        ) as handle:
            temporary = handle.name
            json.dump(artifact, handle, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return artifact


def _validate_forecast_event(event: Any, line_number: int) -> None:
    if not isinstance(event, dict) or event.get("event") not in {
        "call",
        "resolution",
    }:
        raise ValueError(f"invalid forecast event on line {line_number}")
    if event["event"] == "resolution":
        if not isinstance(event.get("call_id"), str) or not event["call_id"].strip():
            raise ValueError(
                f"invalid forecast resolution call_id on line {line_number}"
            )
        if type(event.get("outcome")) is not int or event["outcome"] not in {0, 1}:
            raise ValueError(
                f"invalid forecast resolution outcome on line {line_number}"
            )
        return

    if not isinstance(event.get("id"), str) or not event["id"].strip():
        raise ValueError(f"invalid forecast call id on line {line_number}")
    if not isinstance(event.get("ticker"), str) or not event["ticker"].strip():
        raise ValueError(f"invalid forecast call ticker on line {line_number}")
    probability = event.get("p_outperform_spy")
    if (
        isinstance(probability, bool)
        or not isinstance(probability, (int, float))
        or not math.isfinite(probability)
        or not 0 < probability < 1
    ):
        raise ValueError(f"invalid forecast call probability on line {line_number}")
    price_basis = event.get("price_basis")
    if not isinstance(price_basis, dict):
        raise ValueError(f"invalid forecast call price basis on line {line_number}")
    for field, value in (
        ("target date", event.get("target_date")),
        ("price date", price_basis.get("date")),
    ):
        if not isinstance(value, str):
            raise ValueError(f"invalid forecast call {field} on line {line_number}")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"invalid forecast call {field} on line {line_number}"
            ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assay-forecast", description="Record and score append-only forecasts"
    )
    parser.add_argument("--log", default="forecast_log.jsonl")
    parser.add_argument("--market-db", default=".cache/market/market.sqlite3")
    parser.add_argument("--market-feed", choices=("sip", "iex"), default="sip")
    subparsers = parser.add_subparsers(dest="command", required=True)

    call = subparsers.add_parser("call")
    call.add_argument("ticker")
    call.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    call.add_argument("--horizon-days", type=int, required=True)
    call.add_argument("--probability", type=float, required=True)
    call.add_argument("--thesis", required=True)
    call.add_argument("--grade-at-call")

    settle = subparsers.add_parser("settle")
    settle.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    publish = subparsers.add_parser("publish")
    publish.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    publish.add_argument("--output", default="data/forecasts.json")
    subparsers.add_parser("score")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "score":
            result = forecast_scorecard(args.log)
        elif args.command == "publish":
            artifact = publish_forecast_artifact(
                args.log, args.output, args.as_of
            )
            result = {
                "output": str(Path(args.output).resolve()),
                "events": len(artifact["events"]),
                **artifact["scorecard"],
            }
        else:
            with MarketStore(args.market_db) as market_store:
                if args.command == "call":
                    result = record_forecast(
                        args.log,
                        market_store,
                        args.ticker,
                        args.as_of,
                        horizon_days=args.horizon_days,
                        probability=args.probability,
                        thesis=args.thesis,
                        grade_at_call=args.grade_at_call,
                        market_feed=args.market_feed,
                    )
                else:
                    result = settle_due_forecasts(
                        args.log,
                        market_store,
                        args.as_of,
                        market_feed=args.market_feed,
                    )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"assay-forecast: {exc}\n")
    json.dump(result, sys.stdout, indent=2, allow_nan=False)
    print()
    return 0


def _latest_common_bars(
    store: MarketStore, left: str, right: str, as_of: date, feed: str
) -> tuple[PriceBar, PriceBar]:
    left_by_date = {
        bar.day: bar
        for bar in store.bars(left, adjustment="all", feed=feed, end=as_of)
    }
    right_by_date = {
        bar.day: bar
        for bar in store.bars(right, adjustment="all", feed=feed, end=as_of)
    }
    common = left_by_date.keys() & right_by_date.keys()
    if not common:
        raise ValueError(f"no common {left}/SPY total-return price is cached by {as_of}")
    day = max(common)
    return left_by_date[day], right_by_date[day]


def _settlement_bars(
    store: MarketStore,
    ticker: str,
    price_date: date,
    target_date: date,
    as_of: date,
    feed: str,
) -> tuple[PriceBar, PriceBar, PriceBar, PriceBar] | None:
    stock = {
        bar.day: bar
        for bar in store.bars(
            ticker, adjustment="all", feed=feed, start=price_date, end=as_of
        )
    }
    spy = {
        bar.day: bar
        for bar in store.bars(
            "SPY", adjustment="all", feed=feed, start=price_date, end=as_of
        )
    }
    if price_date not in stock or price_date not in spy:
        return None
    end_dates = sorted(day for day in stock.keys() & spy.keys() if day >= target_date)
    if not end_dates:
        return None
    end_date = end_dates[0]
    return stock[price_date], spy[price_date], stock[end_date], spy[end_date]


def _index_events(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    calls: list[dict[str, Any]] = []
    call_ids: set[str] = set()
    resolved_ids: set[str] = set()
    for event in events:
        if event["event"] == "call":
            call_id = event.get("id")
            if not isinstance(call_id, str) or call_id in call_ids:
                raise ValueError("forecast log contains a missing or duplicate call id")
            call_ids.add(call_id)
            calls.append(event)
            continue
        call_id = event.get("call_id")
        if not isinstance(call_id, str) or call_id not in call_ids:
            raise ValueError("forecast resolution does not follow its call")
        if call_id in resolved_ids:
            raise ValueError(f"forecast {call_id} has more than one resolution")
        resolved_ids.add(call_id)
    return calls, resolved_ids


def _append_event(path: str | Path, event: dict[str, Any]) -> None:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            event,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _log_loss(probability: float, outcome: int) -> float:
    bounded = min(max(probability, 1e-15), 1 - 1e-15)
    return -(outcome * math.log(bounded) + (1 - outcome) * math.log(1 - bounded))


def _calibration(
    probabilities: list[float], outcomes: list[int]
) -> list[dict[str, Any]]:
    bins: dict[int, list[tuple[float, int]]] = {}
    for probability, outcome in zip(probabilities, outcomes):
        bins.setdefault(min(int(probability * 10), 9), []).append(
            (probability, outcome)
        )
    return [
        {
            "range": f"{index / 10:.1f}-{(index + 1) / 10:.1f}",
            "count": len(rows),
            "mean_probability": fmean(row[0] for row in rows),
            "observed_rate": fmean(row[1] for row in rows),
        }
        for index, rows in sorted(bins.items())
    ]


if __name__ == "__main__":
    raise SystemExit(main())
