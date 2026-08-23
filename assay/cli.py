from __future__ import annotations

import argparse
import importlib.resources
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

from .diagnostics import compute_cash_runway, compute_piotroski, detect_late_filings
from .expectations import compute_implied_expectations, revalue_implied_expectations
from .factors import FactorResult, compute_accounting_factors
from .facts import MissingFact, ResolvedFact, resolve_core_facts
from .flags import build_company_flags
from .market import (
    ChsRawResult,
    build_company_chs_raw,
    compute_net_share_issuance,
    median_dollar_adv,
)
from .market_data import (
    AlpacaClient,
    MarketDataError,
    MarketStore,
    sync_market_data,
    sync_market_snapshots,
)
from .pipeline import (
    SECTOR_BENCHMARKS,
    build_universe,
    emit_universe,
    run_full_pipeline,
    verify_output_tree,
)
from .sec import BulkSecStore, Company, SecClient, SecError
from .universe import sector_for_sic


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assay", description="Resolve point-in-time SEC financial facts"
    )
    parser.add_argument("ticker", nargs="?", help="US ticker symbol")
    parser.add_argument(
        "--all",
        action="store_true",
        help="classify every SEC ticker from the nightly bulk archives",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=date.today(),
        metavar="YYYY-MM-DD",
        help="only use facts filed on or before this date",
    )
    parser.add_argument("--cache-dir", default=".cache/sec")
    parser.add_argument("--user-agent", help="SEC user agent with a real contact email")
    parser.add_argument("--refresh", action="store_true", help="ignore cached SEC JSON")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--output-dir", default="data", help="generated data directory")
    parser.add_argument(
        "--verify-output",
        type=Path,
        metavar="DIR",
        help="verify an existing all-ticker output tree and exit",
    )
    parser.add_argument(
        "--universe-only",
        action="store_true",
        help="with --all, stop after eligibility classification",
    )
    parser.add_argument(
        "--market-db",
        default=".cache/market/market.sqlite3",
        help="SQLite market-history cache",
    )
    parser.add_argument(
        "--sync-market",
        action="store_true",
        help="download Alpaca daily bars before an all-ticker run",
    )
    parser.add_argument(
        "--market-feed",
        choices=("sip", "iex"),
        default="sip",
        help="Alpaca stock-data feed",
    )
    parser.add_argument(
        "--live-market-feed",
        choices=("sip", "iex", "delayed_sip"),
        default="iex",
        help="Alpaca feed for current snapshots; IEX works on the free plan",
    )
    parser.add_argument(
        "--market-start",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="first market-history date; defaults to six years before --as-of",
    )
    parser.add_argument(
        "--sp500-market-value",
        type=float,
        help="as-of S&P 500 constituent market value in USD for CHS RSIZE",
    )
    parser.add_argument(
        "--sp500-market-value-as-of",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="measurement date for --sp500-market-value",
    )
    parser.add_argument(
        "--sp500-market-value-source",
        help="source URL or citation for --sp500-market-value",
    )
    parser.add_argument(
        "--companyfacts-file",
        type=Path,
        help="read companyfacts from a local JSON file instead of the SEC",
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.ticker:
        raise ValueError("a ticker is required unless --all is used")
    if args.companyfacts_file:
        companyfacts = _load_json(args.companyfacts_file)
        submissions: dict[str, Any] = {}
        company = Company(
            ticker=args.ticker.upper(),
            cik=int(companyfacts.get("cik", 0)),
            name=str(companyfacts.get("entityName", args.ticker.upper())),
        )
    else:
        client = SecClient(args.cache_dir, args.user_agent, args.refresh)
        company = client.resolve_ticker(args.ticker)
        companyfacts = client.companyfacts(company.cik)
        submissions = client.submissions(company.cik)

    results = resolve_core_facts(companyfacts, args.as_of)
    resolved = sum(isinstance(result, ResolvedFact) for result in results)
    accounting = compute_accounting_factors(companyfacts, args.as_of)
    with MarketStore(args.market_db) as market_store:
        raw = market_store.bars(
            company.ticker, adjustment="raw", feed=args.market_feed
        )
        split = market_store.bars(
            company.ticker, adjustment="split", feed=args.market_feed
        )
        returns = market_store.bars(
            company.ticker, adjustment="all", feed=args.market_feed
        )
        benchmark = market_store.bars(
            "SPY", adjustment="all", feed=args.market_feed
        )
        multiple_share_classes = len(
            {str(ticker).upper() for ticker in submissions.get("tickers", [])}
        ) > 1
        issuance = (
            FactorResult(
                "net_share_issuance",
                None,
                "growth_rate",
                "lower",
                None,
                reason="missing_input",
                detail="firm-wide XBRL shares cannot be assigned to one ticker class",
            )
            if multiple_share_classes
            else compute_net_share_issuance(companyfacts, args.as_of, raw, split)
        )
        if multiple_share_classes:
            chs_raw = ChsRawResult(
                "unresolved",
                None,
                None,
                {},
                {},
                "multiple_share_classes",
                "firm-wide XBRL shares cannot be assigned to one ticker class",
            )
        elif args.sp500_market_value:
            chs_raw = build_company_chs_raw(
                companyfacts,
                args.as_of,
                raw,
                split,
                benchmark,
                sp500_market_value=args.sp500_market_value,
                return_bars=returns,
            )
            chs_raw.market_receipts["sp500_market_value_reference"] = {
                "value": args.sp500_market_value,
                "as_of": args.sp500_market_value_as_of.isoformat(),
                "source": args.sp500_market_value_source,
            }
        else:
            chs_raw = ChsRawResult(
                "unresolved",
                None,
                None,
                {},
                {},
                "missing_sp500_market_value",
                "pass --sp500-market-value to assemble CHS RSIZE",
            )
        chs_factor = FactorResult(
            "chs_12m",
            None,
            "probability",
            "lower",
            chs_raw.accounting_period_end,
            reason="missing_input",
            detail=(
                "a full-universe run is required for pooled 5th/95th percentile winsorization"
                if chs_raw.inputs
                else chs_raw.detail or chs_raw.reason
            ),
        )
        try:
            sic = int(submissions.get("sic"))
            sector = sector_for_sic(sic)
        except (TypeError, ValueError):
            sector = "other"
        sector_bars = market_store.bars(
            SECTOR_BENCHMARKS.get(sector, "SPY"),
            adjustment="all",
            feed=args.market_feed,
        )
        expectations = compute_implied_expectations(
            companyfacts,
            args.as_of,
            [] if multiple_share_classes else raw,
            split,
            sector_bars,
            returns,
        )
        snapshot = market_store.snapshot(
            company.ticker, feed=args.live_market_feed
        )
    factors = accounting + (chs_factor, issuance)
    resolved_factors = sum(factor.value is not None for factor in factors)
    piotroski = compute_piotroski(
        companyfacts,
        args.as_of,
        split_adjusted_share_growth=issuance.value,
        share_inputs=issuance.inputs,
    )
    cash_runway = compute_cash_runway(companyfacts, args.as_of)
    late_filer = detect_late_filings(submissions, args.as_of)
    flags = build_company_flags(
        companyfacts,
        args.as_of,
        factors,
        cash_runway,
        late_filer,
        chs_probability=None,
        distress_threshold=None,
    )
    try:
        adv = {
            "status": "resolved",
            "value": median_dollar_adv(split, args.as_of),
            "unit": "USD_per_day",
            "window_trading_days": 63,
        }
    except ValueError as exc:
        adv = {
            "status": "unresolved",
            "value": None,
            "unit": "USD_per_day",
            "window_trading_days": 63,
            "reason": "insufficient_history",
            "detail": str(exc),
        }
    return {
        "ticker": company.ticker,
        "cik": company.cik,
        "name": company.name,
        "as_of": args.as_of.isoformat(),
        "coverage": {"resolved": resolved, "wanted": len(results)},
        "facts": [result.to_dict() for result in results],
        "factor_coverage": {
            "resolved": resolved_factors,
            "wanted": len(factors),
        },
        "factors": [factor.to_dict() for factor in factors],
        "diagnostics": {
            "piotroski_f_score": piotroski.to_dict(),
            "cash_runway": cash_runway.to_dict(),
            "late_filer": late_filer.to_dict(),
            "median_dollar_adv": adv,
        },
        "models": {"chs_12m": chs_raw.to_dict()},
        "flags": [flag.to_dict() for flag in flags],
        "implied_expectations": expectations.to_dict(),
        "market_snapshot": (
            snapshot.to_dict()
            if snapshot
            else {
                "status": "unresolved",
                "reason": "missing_snapshot",
                "feed": args.live_market_feed,
                "feeds_grade": False,
            }
        ),
        "live_implied_expectations": revalue_implied_expectations(
            expectations.live_inputs,
            snapshot.to_dict().get("price") if snapshot else None,
            price_timestamp=(
                snapshot.to_dict().get("price_timestamp") if snapshot else None
            ),
        ),
    }


def run_all(args: argparse.Namespace) -> dict[str, Any]:
    client = SecClient(args.cache_dir, args.user_agent, args.refresh)
    ticker_data = client.company_tickers()
    companyfacts_zip, submissions_zip = client.bulk_archives()
    with BulkSecStore(companyfacts_zip, submissions_zip) as store:
        build = build_universe(ticker_data, store, args.as_of)
        if args.universe_only:
            path = emit_universe(build, args.output_dir)
            return {
                "mode": "universe-only",
                "output": str(path.resolve()),
                **build.to_dict()["summary"],
            }
        eligible_tickers = [
            entry.ticker for entry in build.entries if entry.status == "eligible"
        ]
        with MarketStore(args.market_db) as market_store:
            market_sync = None
            snapshot_sync = None
            if args.sync_market:
                client_market = AlpacaClient()
                market_start = args.market_start or (
                    args.as_of - timedelta(days=6 * 366)
                )
                market_sync = sync_market_data(
                    [*eligible_tickers, "SPY", *SECTOR_BENCHMARKS.values()],
                    client_market,
                    market_store,
                    market_start,
                    args.as_of,
                    feed=args.market_feed,
                ).to_dict()
                snapshot_sync = sync_market_snapshots(
                    eligible_tickers,
                    client_market,
                    market_store,
                    feed=args.live_market_feed,
                ).to_dict()
            result = run_full_pipeline(
                ticker_data,
                store,
                market_store,
                args.as_of,
                args.output_dir,
                sp500_market_value=args.sp500_market_value,
                sp500_market_value_as_of=args.sp500_market_value_as_of,
                sp500_market_value_source=args.sp500_market_value_source,
                market_feed=args.market_feed,
                live_market_feed=args.live_market_feed,
            ).to_dict()
    return {
        "mode": "all",
        "market_sync": market_sync,
        "snapshot_sync": snapshot_sync,
        **result,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.verify_output and (
            args.ticker
            or args.all
            or args.universe_only
            or args.sync_market
            or args.companyfacts_file
        ):
            raise ValueError(
                "--verify-output cannot be combined with a ticker or run option"
            )
        if args.all and args.ticker:
            raise ValueError("do not pass a ticker with --all")
        if args.all and args.companyfacts_file:
            raise ValueError("--companyfacts-file cannot be used with --all")
        if args.universe_only and not args.all:
            raise ValueError("--universe-only requires --all")
        if args.sync_market and not args.all:
            raise ValueError("--sync-market requires --all")
        if args.sp500_market_value is not None and args.sp500_market_value <= 0:
            raise ValueError("--sp500-market-value must be positive")
        reference_fields = (
            args.sp500_market_value,
            args.sp500_market_value_as_of,
            args.sp500_market_value_source,
        )
        if any(value is not None for value in reference_fields) and not all(
            value is not None for value in reference_fields
        ):
            raise ValueError(
                "S&P 500 market value, measurement date, and source are all required together"
            )
        if (
            args.sp500_market_value_as_of is not None
            and args.sp500_market_value_as_of > args.as_of
        ):
            raise ValueError("S&P 500 market-value reference cannot be after --as-of")
        if not args.verify_output and not any(
            value is not None for value in reference_fields
        ):
            reference = _packaged_sp500_reference(args.as_of)
            if reference:
                args.sp500_market_value = reference["value_usd"]
                args.sp500_market_value_as_of = date.fromisoformat(
                    reference["as_of"]
                )
                args.sp500_market_value_source = reference["source_url"]
        report = (
            verify_output_tree(args.verify_output)
            if args.verify_output
            else run_all(args) if args.all else run(args)
        )
    except (
        MarketDataError,
        SecError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        parser.exit(2, f"assay: {exc}\n")

    if args.json:
        json.dump(report, sys.stdout, indent=2, allow_nan=False)
        print()
    else:
        if args.verify_output:
            print(
                f"verified {report['tickers']} ticker artifacts as of {report['as_of']}"
            )
        elif args.all:
            print(
                f"processed {report['tickers']} tickers: {report['eligible']} eligible, "
                f"{report['excluded']} excluded, {report['unresolved']} unresolved"
            )
            print(report.get("output", report.get("output_dir")))
        else:
            _print_report(report)
    return 0


def _print_report(report: dict[str, Any]) -> None:
    coverage = report["coverage"]
    print(f"{report['ticker']}  {report['name']}  CIK {report['cik']:010d}")
    print(f"as of {report['as_of']}  coverage {coverage['resolved']}/{coverage['wanted']}")
    print()
    for fact in report["facts"]:
        if "value" not in fact:
            print(f"{fact['concept']:<24} — {fact['reason']}: {fact['detail']}")
            continue
        print(
            f"{fact['concept']:<24} {fact['value']:>16,.4g} {fact['unit']}  "
            f"{fact['end']}  filed {fact['filed']}  {fact['namespace']}:{fact['tag']}"
        )
        if fact["filing_url"]:
            print(f"{'':24} {fact['filing_url']}")

    factor_coverage = report["factor_coverage"]
    print()
    print(
        "grade factors       "
        f"coverage {factor_coverage['resolved']}/{factor_coverage['wanted']}"
    )
    for factor in report["factors"]:
        if factor["status"] == "unresolved":
            print(f"{factor['name']:<24} — {factor['reason']}: {factor['detail']}")
        else:
            print(
                f"{factor['name']:<24} {factor['value']:>16,.6f}  "
                f"{factor['period_end']}  {factor['direction']} is better"
            )

    chs = report["models"]["chs_12m"]
    print()
    if "conditional_failure_probability" in chs:
        print(
            "CHS 12-month             "
            f"{chs['conditional_failure_probability']:.4%} conditional probability"
        )
    elif chs["status"] == "resolved":
        print("CHS 12-month             raw inputs ready; universe scoring required")
    else:
        print(f"CHS 12-month             — {chs['reason']}: {chs['detail']}")

    diagnostics = report["diagnostics"]
    piotroski = diagnostics["piotroski_f_score"]
    print()
    if piotroski["status"] == "resolved":
        print(f"Piotroski diagnostic     {piotroski['score']}/9  does not feed grade")
    else:
        component_coverage = piotroski["coverage"]
        print(
            "Piotroski diagnostic     — "
            f"{component_coverage['resolved']}/{component_coverage['wanted']} components"
        )
    runway = diagnostics["cash_runway"]
    if runway["status"] == "unresolved":
        print(f"cash runway              — {runway['reason']}: {runway['detail']}")
    elif runway["burning_cash"]:
        print(f"cash runway              {runway['months']:.1f} months")
    else:
        print("cash runway              not burning cash on trailing operating cash flow")
    late = diagnostics["late_filer"]
    if late["status"] == "unresolved":
        print(f"late filer               — {late['reason']}")
    else:
        print(f"late filer               {'yes' if late['late_filer'] else 'no'}")


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _packaged_sp500_reference(as_of: date) -> dict[str, Any] | None:
    resource = importlib.resources.files("assay").joinpath(
        "reference_data/sp500_market_value.json"
    )
    with resource.open("r", encoding="utf-8") as handle:
        reference = json.load(handle)
    reference_date = date.fromisoformat(reference["as_of"])
    age = (as_of - reference_date).days
    if age < 0 or age > 120:
        return None
    expected = (
        int(reference["constituent_count"])
        * float(reference["mean_market_cap_usd_millions"])
        * 1_000_000
    )
    if not math.isclose(float(reference["value_usd"]), expected, rel_tol=1e-12):
        raise ValueError("packaged S&P 500 market-value reference is inconsistent")
    return reference
