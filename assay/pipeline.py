from __future__ import annotations

import json
import math
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .diagnostics import compute_cash_runway, compute_piotroski, detect_late_filings
from .expectations import compute_implied_expectations, revalue_implied_expectations
from .factors import (
    CHS_12M_COEFFICIENTS,
    CHS_12M_INTERCEPT,
    FACTOR_DEFINITIONS,
    FactorResult,
    compute_accounting_factors,
    compute_chs_12m,
)
from .facts import ResolvedFact, resolve_core_facts
from .flags import build_company_flags, chs_distress_threshold
from .grading import (
    CompanyFactors,
    GRADE_BANDS,
    GRADE_FACTORS,
    PARENT_SECTORS,
    _percentile_map,
    grade_label,
    grade_sensitivity_universe,
    grade_universe,
    sampling_standard_deviation,
)
from .market import (
    ChsRawResult,
    build_company_chs_raw,
    compute_net_share_issuance,
    median_dollar_adv,
    pooled_chs_bounds,
    winsorize_chs_inputs,
)
from .market_data import MarketStore
from .sec import BulkSecStore, Company, SecError
from .universe import UniverseEntry, classify_company


SECTOR_BENCHMARKS = {
    "consumer_nondurables": "XLP",
    "consumer_durables": "XLY",
    "manufacturing": "XLI",
    "energy": "XLE",
    "chemicals": "XLB",
    "business_equipment": "XLK",
    "telecom": "XLC",
    "utilities": "XLU",
    "shops": "XLY",
    "healthcare": "XLV",
    "other": "SPY",
}


@dataclass(frozen=True)
class UniverseBuild:
    as_of: str
    generated_at: str
    entries: tuple[UniverseEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        reasons = Counter(entry.reason or entry.status for entry in self.entries)
        sectors = Counter(
            entry.sector for entry in self.entries if entry.sector is not None
        )
        return {
            "schema_version": 1,
            "as_of": self.as_of,
            "generated_at": self.generated_at,
            "summary": {
                "tickers": len(self.entries),
                "unique_ciks": len({entry.cik for entry in self.entries}),
                "eligible": sum(entry.status == "eligible" for entry in self.entries),
                "excluded": sum(entry.status == "excluded" for entry in self.entries),
                "unresolved": sum(entry.status == "unresolved" for entry in self.entries),
                "reasons": dict(sorted(reasons.items())),
                "sectors": dict(sorted(sectors.items())),
            },
            "tickers": [entry.to_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class FullPipelineResult:
    output_dir: str
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"output_dir": self.output_dir, **self.summary}


def build_universe(
    ticker_data: dict[str, Any], store: BulkSecStore, as_of: date
) -> UniverseBuild:
    submissions_cache: dict[int, dict[str, Any]] = {}
    entries: list[UniverseEntry] = []
    for company, mapping_error in _ticker_mappings(ticker_data):
        if mapping_error:
            entries.append(
                UniverseEntry(
                    company.ticker,
                    company.cik,
                    company.name,
                    None,
                    "",
                    "",
                    None,
                    "unresolved",
                    mapping_error,
                    None,
                )
            )
            continue
        if company.cik not in submissions_cache:
            try:
                submissions_cache[company.cik] = store.submissions(company.cik)
            except SecError:
                submissions_cache[company.cik] = {}
        entries.append(
            classify_company(
                company,
                submissions_cache[company.cik],
                has_companyfacts=store.has_companyfacts(company.cik),
            )
        )
    return UniverseBuild(
        as_of.isoformat(),
        datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        tuple(entries),
    )


def _ticker_mappings(
    ticker_data: dict[str, Any],
) -> list[tuple[Company, str | None]]:
    rows_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for raw in ticker_data.values():
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker", "")).strip().upper()
        if ticker:
            rows_by_ticker.setdefault(ticker, []).append(raw)
    output: list[tuple[Company, str | None]] = []
    for ticker, rows in sorted(rows_by_ticker.items()):
        ciks: set[int] = set()
        invalid_cik = False
        for row in rows:
            try:
                ciks.add(int(row["cik_str"]))
            except (KeyError, TypeError, ValueError):
                invalid_cik = True
        name = str(rows[0].get("title") or ticker)
        if invalid_cik or not ciks:
            output.append((Company(ticker, 0, name), "invalid_cik_mapping"))
        elif len(ciks) > 1:
            output.append((Company(ticker, min(ciks), name), "conflicting_cik_mappings"))
        else:
            output.append((Company(ticker, next(iter(ciks)), name), None))
    return output


def emit_universe(build: UniverseBuild, output_dir: str | Path) -> Path:
    path = Path(output_dir) / "universe.json"
    _write_json(path, build.to_dict())
    return path


def run_full_pipeline(
    ticker_data: dict[str, Any],
    sec_store: BulkSecStore,
    market_store: MarketStore,
    as_of: date,
    output_dir: str | Path,
    *,
    sp500_market_value: float | None = None,
    sp500_market_value_as_of: date | None = None,
    sp500_market_value_source: str | None = None,
    market_feed: str = "sip",
    live_market_feed: str = "iex",
) -> FullPipelineResult:
    """Analyze every SEC ticker, then atomically publish one complete artifact tree."""
    destination = Path(output_dir).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent)
    )
    try:
        result = _run_full_pipeline_to_directory(
            ticker_data,
            sec_store,
            market_store,
            as_of,
            staging,
            sp500_market_value=sp500_market_value,
            sp500_market_value_as_of=sp500_market_value_as_of,
            sp500_market_value_source=sp500_market_value_source,
            market_feed=market_feed,
            live_market_feed=live_market_feed,
        )
        verify_output_tree(staging)
        _replace_directory(staging, destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return FullPipelineResult(str(destination), result.summary)


def verify_output_tree(output_dir: str | Path) -> dict[str, Any]:
    """Verify a published tree without trusting its own summary counters."""
    root = Path(output_dir).resolve()
    required = (
        "universe.json",
        "index.json",
        "methodology.json",
        "audit/coverage.json",
        "audit/construction_sensitivity.json",
        "sectors/index.json",
        "peer_groups/index.json",
    )
    documents = {name: _read_json(root / name) for name in required}
    universe = documents["universe.json"]
    index = documents["index.json"]
    as_of = index.get("as_of")
    generated_at = index.get("generated_at")
    if (
        not isinstance(as_of, str)
        or not isinstance(generated_at, str)
        or universe.get("as_of") != as_of
        or universe.get("generated_at") != generated_at
    ):
        raise ValueError("output as-of dates do not agree")
    for name, document in documents.items():
        if document.get("as_of") != as_of or document.get("generated_at") != generated_at:
            raise ValueError(f"{name} has different run metadata")

    universe_rows = _rows_by_ticker(universe.get("tickers"), "universe")
    index_rows = _rows_by_ticker(index.get("tickers"), "index")
    if universe_rows.keys() != index_rows.keys():
        raise ValueError("universe and index ticker sets do not agree")
    expected_tickers = set(universe_rows)
    summary = universe.get("summary")
    index_summary = index.get("summary")
    if not isinstance(summary, dict) or not isinstance(index_summary, dict):
        raise ValueError("universe and index summaries must be objects")
    ciks = [row.get("cik") for row in universe_rows.values()]
    if any(type(cik) is not int for cik in ciks):
        raise ValueError("universe contains an invalid CIK")
    expected_universe_summary = {
        "tickers": len(expected_tickers),
        "unique_ciks": len(set(ciks)),
        "eligible": sum(
            row.get("status") == "eligible" for row in universe_rows.values()
        ),
        "excluded": sum(
            row.get("status") == "excluded" for row in universe_rows.values()
        ),
        "unresolved": sum(
            row.get("status") == "unresolved" for row in universe_rows.values()
        ),
        "reasons": dict(
            sorted(
                Counter(
                    row.get("reason") or row.get("status")
                    for row in universe_rows.values()
                ).items()
            )
        ),
        "sectors": dict(
            sorted(
                Counter(
                    row.get("sector")
                    for row in universe_rows.values()
                    if row.get("sector") is not None
                ).items()
            )
        ),
    }
    if summary != expected_universe_summary:
        raise ValueError("universe summary cannot be reproduced from its rows")
    for status in ("eligible", "excluded", "unresolved"):
        expected = sum(row.get("status") == status for row in universe_rows.values())
        if summary.get(status) != expected or index_summary.get(status) != expected:
            raise ValueError(f"{status} summary count does not match the universe")
    if index_summary.get("processed") != len(expected_tickers):
        raise ValueError("processed count does not match the universe")
    if index_summary.get("ticker_artifacts") != len(expected_tickers):
        raise ValueError("ticker-artifact count does not match the universe")
    try:
        as_of_date = date.fromisoformat(as_of)
        sp500_as_of = (
            date.fromisoformat(index_summary["sp500_market_value_as_of"])
            if index_summary.get("sp500_market_value_as_of") is not None
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("output contains invalid methodology dates") from exc
    expected_methodology = _methodology_artifact(
        generated_at,
        as_of_date,
        index_summary.get("sp500_market_value"),
        sp500_as_of,
        index_summary.get("sp500_market_value_source"),
        index_summary.get("market_feed"),
        index_summary.get("live_market_feed"),
    )
    if documents["methodology.json"] != expected_methodology:
        raise ValueError("methodology does not match the implemented run contract")

    expected_files: set[str] = set()
    analyzed = 0
    analysis_errors = 0
    grade_receipts: dict[str, dict[str, Any]] = {}
    analyzed_reports: dict[str, dict[str, Any]] = {}
    expected_factor_names = {
        name for names in GRADE_FACTORS.values() for name in names
    }
    for ticker, universe_row in universe_rows.items():
        index_row = index_rows[ticker]
        expected_artifact = f"tickers/{quote(ticker, safe='-.')}.json"
        if index_row.get("artifact") != expected_artifact:
            raise ValueError(f"{ticker} has an unexpected artifact path")
        expected_files.add(Path(expected_artifact).name)
        report = _read_json(root / expected_artifact)
        company = report.get("company")
        if company != universe_row:
            raise ValueError(f"{ticker} artifact identifies a different company")
        if (
            report.get("as_of") != as_of
            or report.get("generated_at") != generated_at
        ):
            raise ValueError(f"{ticker} artifact has different run metadata")
        for field, value in universe_row.items():
            if index_row.get(field) != value:
                raise ValueError(f"{ticker} index and universe rows do not agree")
        if universe_row.get("status") != "eligible":
            if (
                report.get("status") != universe_row.get("status")
                or report.get("reason") != universe_row.get("reason")
                or report.get("grade") is not None
            ):
                raise ValueError(f"{ticker} artifact has the wrong universe status")
            continue
        if report.get("status") == "unresolved" and report.get("reason") == "analysis_error":
            analysis_errors += 1
            continue
        if report.get("status") != "analyzed":
            raise ValueError(f"{ticker} eligible artifact is neither analyzed nor an error")
        analyzed += 1
        analyzed_reports[ticker] = report
        factors = report.get("factors")
        if not isinstance(factors, list) or any(
            not isinstance(factor, dict) for factor in factors
        ):
            raise ValueError(f"{ticker} factors are malformed")
        factor_names = [factor.get("name") for factor in factors]
        if len(factor_names) != 14 or set(factor_names) != expected_factor_names:
            raise ValueError(f"{ticker} does not contain the exact 14-factor set")
        if any(
            (
                factor.get("value") is not None
                and (
                    isinstance(factor.get("value"), bool)
                    or not isinstance(factor.get("value"), (int, float))
                    or not math.isfinite(factor["value"])
                )
            )
            or
            factor.get("direction")
            != FACTOR_DEFINITIONS[factor["name"]]["direction"]
            or factor.get("status")
            != ("resolved" if factor.get("value") is not None else "unresolved")
            for factor in factors
        ):
            raise ValueError(f"{ticker} factor metadata is malformed")
        coverage = report.get("coverage")
        resolved = sum(factor.get("value") is not None for factor in factors)
        if coverage != {"resolved": resolved, "wanted": 14}:
            raise ValueError(f"{ticker} factor coverage does not match its factors")
        grade = report.get("grade")
        if not isinstance(grade, dict) or grade.get("ticker") != ticker:
            raise ValueError(f"{ticker} grade receipt is malformed")
        if grade.get("coverage") != coverage:
            raise ValueError(f"{ticker} grade and report coverage do not agree")
        for field in (
            "grade",
            "percentile",
            "composite",
            "peer_group",
            "sampling_standard_deviation",
            "coverage",
        ):
            if index_row.get(field) != grade.get(field):
                raise ValueError(f"{ticker} index and grade receipt do not agree")
        grade_status = grade.get("status")
        if grade_status not in {"resolved", "unresolved"}:
            raise ValueError(f"{ticker} grade status is malformed")
        percentiles = grade.get("factor_percentiles")
        normalization = grade.get("normalization")
        if not isinstance(percentiles, dict) or not isinstance(normalization, dict):
            raise ValueError(f"{ticker} grade decomposition is malformed")
        resolved_factors = {
            factor["name"]: factor
            for factor in factors
            if factor.get("value") is not None
        }
        if set(percentiles) != set(resolved_factors):
            raise ValueError(f"{ticker} grade factors do not match report factors")
        sleeve_scores = grade.get("sleeve_scores")
        if not isinstance(sleeve_scores, dict):
            raise ValueError(f"{ticker} grade sleeve scores are malformed")
        expected_sleeve_scores = {
            sleeve: sum(percentiles[name]["percentile"] for name in names if name in percentiles)
            / sum(name in percentiles for name in names)
            for sleeve, names in GRADE_FACTORS.items()
            if any(name in percentiles for name in names)
        }
        if set(sleeve_scores) != set(expected_sleeve_scores) or any(
            not isinstance(sleeve_scores[sleeve], (int, float))
            or not math.isclose(
                sleeve_scores[sleeve], score, rel_tol=1e-12, abs_tol=1e-12
            )
            for sleeve, score in expected_sleeve_scores.items()
        ):
            raise ValueError(f"{ticker} grade sleeve scores cannot be reproduced")
        peer_group = grade.get("peer_group")
        for name, receipt in percentiles.items():
            factor = resolved_factors[name]
            if (
                not isinstance(receipt, dict)
                or receipt.get("raw_value") != factor.get("value")
                or receipt.get("direction") != factor.get("direction")
                or receipt.get("peer_group") != peer_group
                or not isinstance(receipt.get("peer_count"), int)
                or receipt["peer_count"] <= 0
                or not isinstance(receipt.get("percentile"), (int, float))
                or not 0 <= receipt["percentile"] <= 1
            ):
                raise ValueError(f"{ticker} {name} percentile receipt is malformed")
        weights = normalization.get("factor_weights")
        standard_deviation = normalization.get("correlation_standard_deviation")
        if normalization.get("method") != "sqrt(w'Rw)" or not isinstance(
            weights, dict
        ):
            raise ValueError(f"{ticker} grade normalization is malformed")
        composite = grade.get("composite")
        if weights:
            expected_weights = {
                name: 1 / len(expected_sleeve_scores) / len(available)
                for names in GRADE_FACTORS.values()
                if (available := [name for name in names if name in percentiles])
                for name in available
            }
            if (
                set(weights) != set(percentiles)
                or not all(
                    isinstance(weight, (int, float))
                    and math.isfinite(weight)
                    and weight > 0
                    for weight in weights.values()
                )
                or not math.isclose(
                    sum(weights.values()), 1.0, rel_tol=1e-12, abs_tol=1e-12
                )
                or any(
                    not math.isclose(
                        weights[name], expected_weights[name], rel_tol=1e-12, abs_tol=1e-12
                    )
                    for name in weights
                )
                or not isinstance(standard_deviation, (int, float))
                or not math.isfinite(standard_deviation)
                or standard_deviation <= 0
            ):
                raise ValueError(f"{ticker} grade normalization is malformed")
            expected_composite = sum(
                weights[name] * (percentiles[name]["percentile"] - 0.5)
                for name in weights
            ) / standard_deviation
            if composite is not None and not math.isclose(
                composite, expected_composite, rel_tol=1e-12, abs_tol=1e-12
            ):
                raise ValueError(f"{ticker} grade composite cannot be reproduced")
        elif standard_deviation is not None or composite is not None:
            raise ValueError(f"{ticker} grade normalization is malformed")
        if grade_status == "resolved":
            if (
                not isinstance(composite, (int, float))
                or not math.isfinite(composite)
                or not isinstance(grade.get("percentile"), (int, float))
                or not isinstance(grade.get("grade"), str)
                or grade.get("reason") is not None
            ):
                raise ValueError(f"{ticker} resolved grade is malformed")
        elif any(
            grade.get(field) is not None
            for field in ("grade", "percentile", "composite", "sampling_standard_deviation")
        ) or not isinstance(grade.get("reason"), str):
            raise ValueError(f"{ticker} unresolved grade is malformed")
        grade_receipts[ticker] = grade
        for field in (
            "implied_expectations",
            "market_snapshot",
            "live_implied_expectations",
        ):
            value = report.get(field)
            if not isinstance(value, dict) or value.get("feeds_grade") is not False:
                raise ValueError(f"{ticker} {field} can feed the grade")

    if analysis_errors:
        raise ValueError(
            f"output contains {analysis_errors} analysis-error ticker artifacts"
        )
    actual_files = {
        path.name for path in (root / "tickers").glob("*.json") if path.is_file()
    }
    if actual_files != expected_files:
        raise ValueError("ticker artifact set does not exactly match the universe")
    sector_artifacts = _verify_group_artifacts(
        root,
        documents["sectors/index.json"],
        "sectors",
        "sector",
        as_of,
        generated_at,
    )
    expected_sectors: dict[str, set[str]] = {}
    for ticker, row in index_rows.items():
        if row.get("status") == "eligible" and isinstance(row.get("sector"), str):
            expected_sectors.setdefault(row["sector"], set()).add(ticker)
    if sector_artifacts != expected_sectors:
        raise ValueError("sector artifacts do not contain the expected ticker sets")
    peer_artifacts = _verify_group_artifacts(
        root,
        documents["peer_groups/index.json"],
        "peer_groups",
        "peer_group",
        as_of,
        generated_at,
    )
    eligible_rows = {
        ticker: row
        for ticker, row in index_rows.items()
        if row.get("status") == "eligible"
    }
    expected_peer_groups: dict[str, set[str]] = {}
    for group in {
        row.get("peer_group")
        for row in eligible_rows.values()
        if isinstance(row.get("peer_group"), str)
    }:
        if group == "all_eligible":
            members = set(eligible_rows)
        elif group in PARENT_SECTORS.values():
            members = {
                ticker
                for ticker, row in eligible_rows.items()
                if PARENT_SECTORS.get(row.get("sector"), "other") == group
            }
        else:
            members = {
                ticker
                for ticker, row in eligible_rows.items()
                if row.get("sector") == group
            }
        expected_peer_groups[group] = members
    if peer_artifacts != expected_peer_groups:
        raise ValueError("peer-group artifacts do not contain the expected ticker sets")

    factor_values = {
        ticker: {
            factor["name"]: factor["value"]
            for factor in report["factors"]
            if factor.get("value") is not None
        }
        for ticker, report in analyzed_reports.items()
    }
    factor_ranks: dict[tuple[str, str], dict[str, float]] = {}
    for ticker, grade in grade_receipts.items():
        peer_group = grade["peer_group"]
        if peer_group not in peer_artifacts:
            raise ValueError(f"{ticker} grade names a missing peer group")
        for name, receipt in grade["factor_percentiles"].items():
            key = (peer_group, name)
            if key not in factor_ranks:
                peer_values = {
                    member: factor_values[member][name]
                    for member in peer_artifacts[peer_group]
                    if name in factor_values[member]
                }
                factor_ranks[key] = _percentile_map(
                    peer_values,
                    lower_is_better=(
                        FACTOR_DEFINITIONS[name]["direction"] == "lower"
                    ),
                )
            ranks = factor_ranks[key]
            if (
                ticker not in ranks
                or receipt["peer_count"] != len(ranks)
                or not math.isclose(
                    receipt["percentile"],
                    ranks[ticker],
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
            ):
                raise ValueError(
                    f"{ticker} {name} factor percentile cannot be reproduced"
                )

    for ticker, grade in grade_receipts.items():
        if grade["status"] != "resolved":
            continue
        peer_group = grade["peer_group"]
        if peer_group not in peer_artifacts:
            raise ValueError(f"{ticker} grade names a missing peer group")
        peer_composites = {
            member: index_rows[member]["composite"]
            for member in peer_artifacts[peer_group]
            if isinstance(index_rows[member].get("composite"), (int, float))
        }
        peer_count = len(peer_composites)
        if ticker not in peer_composites or peer_count < 100:
            raise ValueError(f"{ticker} grade has an insufficient peer group")
        if grade.get("peer_count") != peer_count:
            raise ValueError(f"{ticker} grade peer count cannot be reproduced")
        expected_percentile = _percentile_map(
            peer_composites, lower_is_better=True
        )[ticker]
        if not math.isclose(
            grade["percentile"], expected_percentile, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise ValueError(f"{ticker} grade percentile cannot be reproduced")
        expected_sampling_sd = sampling_standard_deviation(peer_count)
        if not math.isclose(
            grade["sampling_standard_deviation"],
            expected_sampling_sd,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError(
                f"{ticker} grade sampling uncertainty cannot be reproduced"
            )
        if grade["grade"] != grade_label(expected_percentile, expected_sampling_sd):
            raise ValueError(f"{ticker} grade label cannot be reproduced")

    for sector, members in sector_artifacts.items():
        report = _read_json(root / "sectors" / f"{quote(sector, safe='-.')}.json")
        rows = _rows_by_ticker(report.get("tickers"), f"{sector} sectors")
        expected_rows = {ticker: index_rows[ticker] for ticker in members}
        if rows != expected_rows:
            raise ValueError(f"{sector} sector rows do not match the index")
        if report.get("peer_cloud") != _peer_cloud(list(rows.values())):
            raise ValueError(f"{sector} peer cloud cannot be reproduced")
        if report.get("display_rules") != {
            "continuous": True,
            "quadrants": False,
            "verdict": None,
            "predictive_claim": False,
        }:
            raise ValueError(f"{sector} sector display rules are unsafe")
    for group, members in peer_artifacts.items():
        report = _read_json(
            root / "peer_groups" / f"{quote(group, safe='-.')}.json"
        )
        rows = _rows_by_ticker(report.get("tickers"), f"{group} peer_groups")
        expected_rows = {ticker: index_rows[ticker] for ticker in members}
        if rows != expected_rows or report.get("ticker_count") != len(rows):
            raise ValueError(f"{group} peer-group rows do not match the index")
        expected_curve = _distribution_curve(
            [row["composite"] for row in rows.values() if row["composite"] is not None]
        )
        if report.get("distribution_curve") != expected_curve:
            raise ValueError(f"{group} distribution curve cannot be reproduced")

    for field, value in expected_universe_summary.items():
        if index_summary.get(field) != value:
            raise ValueError(f"index {field} summary cannot be reproduced")
    expected_grade_counts = dict(
        sorted(
            Counter(
                row.get("grade") or "unresolved" for row in index_rows.values()
            ).items()
        )
    )
    expected_factor_failures: Counter[str] = Counter()
    expected_active_flags: Counter[str] = Counter()
    chs_probabilities: list[float] = []
    live_snapshots = 0
    for report in analyzed_reports.values():
        for factor in report["factors"]:
            if factor.get("value") is None:
                expected_factor_failures[
                    factor.get("failure_class", "missing_input")
                ] += 1
        for flag in report.get("flags", []):
            if isinstance(flag, dict) and flag.get("active") is True:
                expected_active_flags[flag.get("name")] += 1
        chs = (report.get("models") or {}).get("chs_12m") or {}
        probability = chs.get("conditional_failure_probability")
        if isinstance(probability, (int, float)):
            chs_probabilities.append(probability)
        if report["market_snapshot"].get("status") == "resolved":
            live_snapshots += 1
    expected_index_counts = {
        "grades": expected_grade_counts,
        "chs_resolved": len(chs_probabilities),
        "chs_distress_top_decile_threshold": chs_distress_threshold(
            chs_probabilities
        ),
        "factor_failures": dict(sorted(expected_factor_failures.items())),
        "active_flags": dict(sorted(expected_active_flags.items())),
        "analysis_errors": {},
        "live_snapshots": live_snapshots,
    }
    if any(
        index_summary.get(field) != value
        for field, value in expected_index_counts.items()
    ):
        raise ValueError("index analysis summaries cannot be reproduced")

    expected_coverage = {
        "schema_version": 1,
        "generated_at": generated_at,
        "as_of": as_of,
        **_coverage_audit_from_reports(analyzed_reports, index_rows),
    }
    if documents["audit/coverage.json"] != expected_coverage:
        raise ValueError("coverage audit cannot be reproduced")
    sensitivities = {
        ticker: report.get("grade_sensitivity")
        for ticker, report in analyzed_reports.items()
    }
    if any(not isinstance(value, dict) for value in sensitivities.values()):
        raise ValueError("ticker grade sensitivity is malformed")
    expected_sensitivity = {
        "schema_version": 1,
        "generated_at": generated_at,
        "as_of": as_of,
        "scenarios": _sensitivity_summary(sensitivities),
        "scope_note": (
            "These scenarios vary peer universe and weighting. Portfolio-sort "
            "30/70 breakpoints do not map directly to this descriptive percentile grade."
        ),
    }
    if documents["audit/construction_sensitivity.json"] != expected_sensitivity:
        raise ValueError("construction-sensitivity audit cannot be reproduced")
    return {
        "status": "verified",
        "output_dir": str(root),
        "as_of": as_of,
        "tickers": len(expected_tickers),
        "analyzed": analyzed,
        "analysis_errors": analysis_errors,
        "excluded": summary["excluded"],
        "unresolved": summary["unresolved"],
    }


def _run_full_pipeline_to_directory(
    ticker_data: dict[str, Any],
    sec_store: BulkSecStore,
    market_store: MarketStore,
    as_of: date,
    output: Path,
    *,
    sp500_market_value: float | None,
    sp500_market_value_as_of: date | None,
    sp500_market_value_source: str | None,
    market_feed: str,
    live_market_feed: str,
) -> FullPipelineResult:
    """Write a complete artifact tree in two bounded-memory passes."""
    universe = build_universe(ticker_data, sec_store, as_of)
    emit_universe(universe, output)
    eligible = [entry for entry in universe.entries if entry.status == "eligible"]
    cik_counts = Counter(entry.cik for entry in universe.entries)
    benchmark = market_store.bars("SPY", adjustment="all", feed=market_feed)

    compact_factors: dict[str, tuple[FactorResult, ...]] = {}
    raw_chs: dict[str, ChsRawResult] = {}
    analysis_errors: Counter[str] = Counter()
    for entry in eligible:
        try:
            companyfacts = sec_store.companyfacts(entry.cik)
            raw = market_store.bars(entry.ticker, adjustment="raw", feed=market_feed)
            split = market_store.bars(
                entry.ticker, adjustment="split", feed=market_feed
            )
            returns = market_store.bars(
                entry.ticker, adjustment="all", feed=market_feed
            )
            accounting = compute_accounting_factors(companyfacts, as_of)
            issuance = (
                _ambiguous_share_issuance()
                if cik_counts[entry.cik] > 1
                else compute_net_share_issuance(companyfacts, as_of, raw, split)
            )
            chs_raw = _build_chs_raw(
                companyfacts,
                as_of,
                raw,
                split,
                returns,
                benchmark,
                sp500_market_value,
                multiple_share_classes=cik_counts[entry.cik] > 1,
            )
            compact_factors[entry.ticker] = tuple(
                _compact_factor(factor) for factor in (*accounting, issuance)
            )
            raw_chs[entry.ticker] = _compact_chs_raw(chs_raw)
        except (KeyError, OSError, OverflowError, SecError, TypeError, ValueError) as exc:
            analysis_errors[type(exc).__name__] += 1
            compact_factors[entry.ticker] = tuple()
            raw_chs[entry.ticker] = ChsRawResult(
                "unresolved", None, None, {}, {}, "analysis_error", str(exc)
            )

    raw_input_rows = [
        result.inputs
        for result in raw_chs.values()
        if result.inputs is not None
    ]
    chs_bounds = pooled_chs_bounds(raw_input_rows) if raw_input_rows else None
    chs_scores: dict[str, Any] = {}
    grade_companies: list[CompanyFactors] = []
    for entry in eligible:
        raw_result = raw_chs[entry.ticker]
        if raw_result.inputs is not None and chs_bounds is not None:
            score = compute_chs_12m(
                winsorize_chs_inputs(raw_result.inputs, chs_bounds)
            )
            chs_scores[entry.ticker] = score
            chs_factor = FactorResult(
                "chs_12m",
                score.conditional_failure_probability,
                "probability",
                "lower",
                raw_result.accounting_period_end,
            )
        else:
            chs_factor = FactorResult(
                "chs_12m",
                None,
                "probability",
                "lower",
                raw_result.accounting_period_end,
                reason="missing_input",
                detail=raw_result.detail or raw_result.reason,
            )
        factors = compact_factors[entry.ticker] + (chs_factor,)
        compact_factors[entry.ticker] = factors
        grade_companies.append(
            CompanyFactors(entry.ticker, entry.sector or "other", factors, entry.exchange)
        )

    grades = grade_universe(grade_companies)
    sensitivities = grade_sensitivity_universe(grade_companies)
    distress_cutoff = chs_distress_threshold(
        [score.conditional_failure_probability for score in chs_scores.values()]
    )
    sector_histories = {
        symbol: market_store.bars(symbol, adjustment="all", feed=market_feed)
        for symbol in {
            SECTOR_BENCHMARKS.get(entry.sector or "other", "SPY")
            for entry in eligible
        }
    }

    index_rows: list[dict[str, Any]] = []
    factor_failures: Counter[str] = Counter()
    active_flags: Counter[str] = Counter()
    factor_sector_audit: dict[str, dict[str, Counter[str]]] = {}
    tag_sector_audit: dict[str, dict[str, Counter[str]]] = {}
    issuance_size_audit: dict[str, Counter[str]] = {}
    ticker_artifacts = 0
    live_snapshots = 0
    for entry in universe.entries:
        ticker_path = f"tickers/{quote(entry.ticker, safe='-.')}.json"
        if entry.status != "eligible":
            report = _ineligible_report(entry, as_of, universe.generated_at)
            _write_json(output / ticker_path, report)
            index_rows.append(
                {
                    **entry.to_dict(),
                    "grade": None,
                    "percentile": None,
                    "composite": None,
                    "peer_group": None,
                    "sampling_standard_deviation": None,
                    "coverage": None,
                    "active_flags": [],
                    "implied_expectations": None,
                    "artifact": ticker_path,
                }
            )
            ticker_artifacts += 1
            continue
        try:
            companyfacts = sec_store.companyfacts(entry.cik)
            submissions = sec_store.submissions(entry.cik)
            raw = market_store.bars(entry.ticker, adjustment="raw", feed=market_feed)
            split = market_store.bars(
                entry.ticker, adjustment="split", feed=market_feed
            )
            returns = market_store.bars(
                entry.ticker, adjustment="all", feed=market_feed
            )
            accounting = compute_accounting_factors(companyfacts, as_of)
            issuance = (
                _ambiguous_share_issuance()
                if cik_counts[entry.cik] > 1
                else compute_net_share_issuance(companyfacts, as_of, raw, split)
            )
            detailed_chs_raw = _build_chs_raw(
                companyfacts,
                as_of,
                raw,
                split,
                returns,
                benchmark,
                sp500_market_value,
                multiple_share_classes=cik_counts[entry.cik] > 1,
            )
            if entry.ticker in chs_scores:
                score = chs_scores[entry.ticker]
                if detailed_chs_raw.inputs != raw_chs[entry.ticker].inputs:
                    raise ValueError("CHS inputs changed between pipeline passes")
                chs_factor = FactorResult(
                    "chs_12m",
                    score.conditional_failure_probability,
                    "probability",
                    "lower",
                    detailed_chs_raw.accounting_period_end,
                )
            else:
                score = None
                chs_factor = FactorResult(
                    "chs_12m",
                    None,
                    "probability",
                    "lower",
                    detailed_chs_raw.accounting_period_end,
                    reason="missing_input",
                    detail=detailed_chs_raw.detail or detailed_chs_raw.reason,
                )
            factors = accounting + (chs_factor, issuance)
            piotroski = compute_piotroski(
                companyfacts,
                as_of,
                split_adjusted_share_growth=issuance.value,
                share_inputs=issuance.inputs,
            )
            runway = compute_cash_runway(companyfacts, as_of)
            late = detect_late_filings(submissions, as_of)
            sector_ticker = SECTOR_BENCHMARKS.get(entry.sector or "other", "SPY")
            sector_bars = sector_histories[sector_ticker]
            panel_raw = [] if cik_counts[entry.cik] > 1 else raw
            expectations = compute_implied_expectations(
                companyfacts,
                as_of,
                panel_raw,
                split,
                sector_bars,
                returns,
            )
            core_facts = resolve_core_facts(companyfacts, as_of)
            try:
                adv = {
                    "status": "resolved",
                    "value": median_dollar_adv(split, as_of),
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
            flags = build_company_flags(
                companyfacts,
                as_of,
                factors,
                runway,
                late,
                chs_probability=(
                    score.conditional_failure_probability if score else None
                ),
                distress_threshold=distress_cutoff,
            )
            snapshot = market_store.snapshot(
                entry.ticker, feed=live_market_feed
            )
            market_snapshot = (
                snapshot.to_dict()
                if snapshot
                else {
                    "status": "unresolved",
                    "reason": "missing_snapshot",
                    "feed": live_market_feed,
                    "feeds_grade": False,
                }
            )
            if market_snapshot["status"] == "resolved":
                live_snapshots += 1
            for flag in flags:
                if flag.active:
                    active_flags[flag.name] += 1
            for factor in factors:
                factor_counts = factor_sector_audit.setdefault(
                    entry.sector or "other", {}
                ).setdefault(factor.name, Counter())
                factor_counts["eligible"] += 1
                if factor.value is None:
                    failure_class = factor.to_dict().get(
                        "failure_class", "missing_input"
                    )
                    factor_failures[failure_class] += 1
                    factor_counts[f"unresolved:{failure_class}"] += 1
                else:
                    factor_counts["resolved"] += 1
            receipts: set[tuple[str, str, str]] = {
                (fact.concept, fact.namespace, fact.tag)
                for fact in core_facts
                if isinstance(fact, ResolvedFact)
            }
            receipts.update(
                (fact.concept, fact.namespace, fact.tag)
                for factor in factors
                for fact in factor.inputs
            )
            receipts.update(
                (fact.concept, fact.namespace, fact.tag)
                for facts in detailed_chs_raw.accounting_receipts.values()
                for fact in facts
            )
            for concept, namespace, tag in receipts:
                tag_sector_audit.setdefault(entry.sector or "other", {}).setdefault(
                    concept, Counter()
                )[f"{namespace}:{tag}"] += 1
            issuance = next(
                factor for factor in factors if factor.name == "net_share_issuance"
            )
            size_bucket = _market_equity_bucket(
                expectations.live_inputs.get("market_equity")
            )
            issuance_counts = issuance_size_audit.setdefault(size_bucket, Counter())
            issuance_counts["eligible"] += 1
            issuance_counts[
                "resolved" if issuance.value is not None else "unresolved"
            ] += 1
            grade = grades[entry.ticker]
            chs_model = detailed_chs_raw.to_dict()
            if sp500_market_value is not None:
                chs_model["market_receipts"]["sp500_market_value_reference"] = {
                    "value": sp500_market_value,
                    "as_of": (
                        sp500_market_value_as_of.isoformat()
                        if sp500_market_value_as_of
                        else None
                    ),
                    "source": sp500_market_value_source,
                }
            if score and chs_bounds:
                chs_model.update(
                    {
                        "stage": "scored",
                        "winsorized": True,
                        "winsorization_bounds": chs_bounds,
                        "winsorized_inputs": vars(
                            winsorize_chs_inputs(
                                detailed_chs_raw.inputs, chs_bounds
                            )
                        ),
                        **score.to_dict(),
                    }
                )
            report = {
                "schema_version": 1,
                "generated_at": universe.generated_at,
                "as_of": as_of.isoformat(),
                "company": entry.to_dict(),
                "status": "analyzed",
                "market_scope": (
                    {
                        "status": "unresolved",
                        "reason": "multiple_share_classes",
                        "detail": "firm-wide XBRL shares cannot be assigned to one ticker class",
                    }
                    if cik_counts[entry.cik] > 1
                    else {"status": "resolved"}
                ),
                "facts": [fact.to_dict() for fact in core_facts],
                "factors": [factor.to_dict() for factor in factors],
                "coverage": {
                    "resolved": sum(factor.value is not None for factor in factors),
                    "wanted": 14,
                },
                "grade": grade.to_dict(),
                "grade_sensitivity": sensitivities[entry.ticker],
                "models": {"chs_12m": chs_model},
                "diagnostics": {
                    "piotroski_f_score": piotroski.to_dict(),
                    "cash_runway": runway.to_dict(),
                    "late_filer": late.to_dict(),
                    "median_dollar_adv": adv,
                },
                "flags": [flag.to_dict() for flag in flags],
                "implied_expectations": expectations.to_dict(),
                "market_snapshot": market_snapshot,
                "live_implied_expectations": revalue_implied_expectations(
                    expectations.live_inputs,
                    market_snapshot.get("price"),
                    price_timestamp=market_snapshot.get("price_timestamp"),
                ),
                "disclaimer": "Informational and educational only. Not investment advice. SEC and market data may contain errors.",
            }
            _write_json(output / ticker_path, report)
            index_rows.append(
                {
                    **entry.to_dict(),
                    "grade": grade.grade,
                    "percentile": grade.percentile,
                    "composite": grade.composite,
                    "peer_group": grade.peer_group,
                    "sampling_standard_deviation": grade.sampling_standard_deviation,
                    "coverage": grade.coverage,
                    "active_flags": [flag.name for flag in flags if flag.active],
                    "live_price": market_snapshot.get("price"),
                    "live_price_timestamp": market_snapshot.get("price_timestamp"),
                    "implied_expectations": {
                        metric.name: metric.value for metric in expectations.metrics
                    },
                    "artifact": ticker_path,
                }
            )
        except (KeyError, OSError, OverflowError, SecError, TypeError, ValueError) as exc:
            analysis_errors[type(exc).__name__] += 1
            report = _analysis_error_report(
                entry, as_of, universe.generated_at, str(exc)
            )
            _write_json(output / ticker_path, report)
            index_rows.append(
                {
                    **entry.to_dict(),
                    "grade": None,
                    "percentile": None,
                    "composite": None,
                    "peer_group": None,
                    "sampling_standard_deviation": None,
                    "coverage": None,
                    "active_flags": [],
                    "implied_expectations": None,
                    "artifact": ticker_path,
                    "analysis_error": str(exc),
                }
            )
        ticker_artifacts += 1

    grade_counts = Counter(row["grade"] or "unresolved" for row in index_rows)
    summary = {
        **universe.to_dict()["summary"],
        "processed": len(index_rows),
        "ticker_artifacts": ticker_artifacts,
        "grades": dict(sorted(grade_counts.items())),
        "chs_resolved": len(chs_scores),
        "chs_distress_top_decile_threshold": distress_cutoff,
        "factor_failures": dict(sorted(factor_failures.items())),
        "active_flags": dict(sorted(active_flags.items())),
        "analysis_errors": dict(sorted(analysis_errors.items())),
        "sp500_market_value": sp500_market_value,
        "sp500_market_value_as_of": (
            sp500_market_value_as_of.isoformat() if sp500_market_value_as_of else None
        ),
        "sp500_market_value_source": sp500_market_value_source,
        "market_feed": market_feed,
        "live_market_feed": live_market_feed,
        "live_snapshots": live_snapshots,
    }
    _write_json(
        output / "index.json",
        {
            "schema_version": 1,
            "generated_at": universe.generated_at,
            "as_of": as_of.isoformat(),
            "summary": summary,
            "tickers": index_rows,
        },
    )
    sectors: dict[str, list[dict[str, Any]]] = {}
    for row in index_rows:
        if row["status"] == "eligible" and row["sector"]:
            sectors.setdefault(row["sector"], []).append(row)
    for sector, rows in sectors.items():
        _write_json(
            output / "sectors" / f"{sector}.json",
            {
                "schema_version": 1,
                "generated_at": universe.generated_at,
                "as_of": as_of.isoformat(),
                "sector": sector,
                "tickers": rows,
                "peer_cloud": _peer_cloud(rows),
                "display_rules": {
                    "continuous": True,
                    "quadrants": False,
                    "verdict": None,
                    "predictive_claim": False,
                },
            },
        )
    _write_json(
        output / "sectors" / "index.json",
        {
            "schema_version": 1,
            "generated_at": universe.generated_at,
            "as_of": as_of.isoformat(),
            "sectors": [
                {"sector": sector, "ticker_count": len(rows)}
                for sector, rows in sorted(sectors.items())
            ],
        },
    )
    peer_groups = _peer_group_rows(index_rows)
    for group, rows in peer_groups.items():
        _write_json(
            output / "peer_groups" / f"{quote(group, safe='-.')}.json",
            {
                "schema_version": 1,
                "generated_at": universe.generated_at,
                "as_of": as_of.isoformat(),
                "peer_group": group,
                "ticker_count": len(rows),
                "distribution_curve": _distribution_curve(
                    [row["composite"] for row in rows if row["composite"] is not None]
                ),
                "tickers": rows,
            },
        )
    _write_json(
        output / "peer_groups" / "index.json",
        {
            "schema_version": 1,
            "generated_at": universe.generated_at,
            "as_of": as_of.isoformat(),
            "peer_groups": [
                {
                    "peer_group": group,
                    "ticker_count": len(rows),
                    "artifact": f"peer_groups/{quote(group, safe='-.')}.json",
                }
                for group, rows in sorted(peer_groups.items())
            ],
        },
    )
    _write_json(
        output / "audit" / "coverage.json",
        {
            "schema_version": 1,
            "generated_at": universe.generated_at,
            "as_of": as_of.isoformat(),
            "factor_computability_by_sector": _serialize_nested_counters(
                factor_sector_audit
            ),
            "selected_tags_by_sector": _serialize_nested_counters(
                tag_sector_audit
            ),
            "net_share_issuance_by_market_equity_bucket": {
                bucket: dict(sorted(counts.items()))
                for bucket, counts in sorted(issuance_size_audit.items())
            },
            "size_measure": "split-adjusted market equity, not public float",
        },
    )
    _write_json(
        output / "audit" / "construction_sensitivity.json",
        {
            "schema_version": 1,
            "generated_at": universe.generated_at,
            "as_of": as_of.isoformat(),
            "scenarios": _sensitivity_summary(sensitivities),
            "scope_note": (
                "These scenarios vary peer universe and weighting. Portfolio-sort "
                "30/70 breakpoints do not map directly to this descriptive percentile grade."
            ),
        },
    )
    _write_json(
        output / "methodology.json",
        _methodology_artifact(
            universe.generated_at,
            as_of,
            sp500_market_value,
            sp500_market_value_as_of,
            sp500_market_value_source,
            market_feed,
            live_market_feed,
        ),
    )
    expected_ticker_files = {
        f"{quote(entry.ticker, safe='-.')}.json" for entry in universe.entries
    }
    actual_ticker_files = {
        path.name for path in (output / "tickers").glob("*.json")
    }
    indexed_tickers = {row["ticker"] for row in index_rows}
    expected_tickers = {entry.ticker for entry in universe.entries}
    if (
        ticker_artifacts != len(universe.entries)
        or len(index_rows) != len(universe.entries)
        or indexed_tickers != expected_tickers
        or actual_ticker_files != expected_ticker_files
    ):
        raise RuntimeError("exhaustiveness audit failed: not every ticker was emitted")
    return FullPipelineResult(str(output.resolve()), summary)


def _replace_directory(staging: Path, destination: Path) -> None:
    if not destination.exists():
        staging.replace(destination)
        return
    backup_root = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.backup-", dir=destination.parent)
    )
    backup = backup_root / "previous"
    destination.replace(backup)
    try:
        staging.replace(destination)
    except Exception:
        backup.replace(destination)
        raise
    shutil.rmtree(backup_root)


def _peer_cloud(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "ticker": row["ticker"],
            "financial_condition_percentile": row["percentile"],
            "metrics": row["implied_expectations"],
        }
        for row in rows
        if row["percentile"] is not None and row["implied_expectations"] is not None
    ]


def _peer_group_rows(index_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    eligible = [row for row in index_rows if row["status"] == "eligible"]
    group_names = sorted(
        {row["peer_group"] for row in eligible if row["peer_group"] is not None}
    )
    output: dict[str, list[dict[str, Any]]] = {}
    for group in group_names:
        if group == "all_eligible":
            rows = eligible
        elif group in PARENT_SECTORS.values():
            rows = [
                row
                for row in eligible
                if PARENT_SECTORS.get(row["sector"], "other") == group
            ]
        else:
            rows = [row for row in eligible if row["sector"] == group]
        output[group] = rows
    return output


def _distribution_curve(values: list[float], *, points: int = 81) -> dict[str, Any]:
    if len(values) < 2 or min(values) == max(values):
        return {
            "status": "unresolved",
            "reason": "degenerate_distribution",
            "points": [],
        }
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    standard_deviation = math.sqrt(variance)
    lower, upper = min(values), max(values)
    span = upper - lower
    bandwidth = max(1.06 * standard_deviation * len(values) ** -0.2, span / 100)
    grid = [lower + span * index / (points - 1) for index in range(points)]
    scale = bandwidth * math.sqrt(2 * math.pi)
    densities = [
        sum(
            math.exp(-0.5 * ((position - value) / bandwidth) ** 2)
            for value in values
        )
        / (len(values) * scale)
        for position in grid
    ]
    peak = max(densities)
    return {
        "status": "resolved",
        "method": "gaussian_kernel_density",
        "bandwidth": bandwidth,
        "domain": [lower, upper],
        "points": [
            {
                "x": (position - lower) / span,
                "y": density / peak,
                "value": position,
            }
            for position, density in zip(grid, densities)
        ],
    }


def _market_equity_bucket(value: Any) -> str:
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return "unresolved_market_equity"
    if value < 32_000_000:
        return "under_32m"
    if value < 316_000_000:
        return "32m_to_316m"
    if value < 3_200_000_000:
        return "316m_to_3.2b"
    return "over_3.2b"


def _serialize_nested_counters(
    values: dict[str, dict[str, Counter[str]]],
) -> dict[str, dict[str, dict[str, int]]]:
    return {
        outer: {
            inner: dict(sorted(counts.items()))
            for inner, counts in sorted(children.items())
        }
        for outer, children in sorted(values.items())
    }


def _coverage_audit_from_reports(
    reports: dict[str, dict[str, Any]],
    index_rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    factor_sector: dict[str, dict[str, Counter[str]]] = {}
    tag_sector: dict[str, dict[str, Counter[str]]] = {}
    issuance_size: dict[str, Counter[str]] = {}
    for ticker, report in reports.items():
        sector = index_rows[ticker].get("sector") or "other"
        factors = report["factors"]
        for factor in factors:
            counts = factor_sector.setdefault(sector, {}).setdefault(
                factor["name"], Counter()
            )
            counts["eligible"] += 1
            if factor.get("value") is None:
                counts[
                    f"unresolved:{factor.get('failure_class', 'missing_input')}"
                ] += 1
            else:
                counts["resolved"] += 1

        receipt_rows = list(report.get("facts", []))
        receipt_rows.extend(
            receipt
            for factor in factors
            for receipt in factor.get("inputs", [])
        )
        chs_receipts = (
            ((report.get("models") or {}).get("chs_12m") or {}).get(
                "accounting_receipts"
            )
            or {}
        )
        receipt_rows.extend(
            receipt
            for receipts in chs_receipts.values()
            if isinstance(receipts, list)
            for receipt in receipts
        )
        receipts = {
            (receipt.get("concept"), receipt.get("namespace"), receipt.get("tag"))
            for receipt in receipt_rows
            if isinstance(receipt, dict)
            and all(
                isinstance(receipt.get(field), str)
                for field in ("concept", "namespace", "tag")
            )
        }
        for concept, namespace, tag in receipts:
            tag_sector.setdefault(sector, {}).setdefault(concept, Counter())[
                f"{namespace}:{tag}"
            ] += 1

        issuance = next(
            factor for factor in factors if factor["name"] == "net_share_issuance"
        )
        market_equity = (
            (report.get("implied_expectations") or {})
            .get("live_inputs", {})
            .get("market_equity")
        )
        counts = issuance_size.setdefault(
            _market_equity_bucket(market_equity), Counter()
        )
        counts["eligible"] += 1
        counts["resolved" if issuance.get("value") is not None else "unresolved"] += 1
    return {
        "factor_computability_by_sector": _serialize_nested_counters(factor_sector),
        "selected_tags_by_sector": _serialize_nested_counters(tag_sector),
        "net_share_issuance_by_market_equity_bucket": {
            bucket: dict(sorted(counts.items()))
            for bucket, counts in sorted(issuance_size.items())
        },
        "size_measure": "split-adjusted market equity, not public float",
    }


def _sensitivity_summary(
    sensitivities: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    baseline_name = "baseline_sector_rollup_equal_sleeves"
    scenario_names = sorted(
        {name for company in sensitivities.values() for name in company}
    )
    output: dict[str, dict[str, Any]] = {}
    for scenario in scenario_names:
        grades = Counter(
            company[scenario]["grade"] or "unresolved"
            for company in sensitivities.values()
        )
        output[scenario] = {
            "grade_distribution": dict(sorted(grades.items())),
            "changed_from_baseline": sum(
                company[scenario]["grade"] != company[baseline_name]["grade"]
                for company in sensitivities.values()
            ),
            "companies": len(sensitivities),
        }
    return output


def _methodology_artifact(
    generated_at: str,
    as_of: date,
    sp500_market_value: float | None,
    sp500_market_value_as_of: date | None,
    sp500_market_value_source: str | None,
    market_feed: str,
    live_market_feed: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "as_of": as_of.isoformat(),
        "claim": (
            "The grade is an ordinal peer ranking of reported financial condition "
            "with a market-informed corporate-failure estimate. It is not a return forecast."
        ),
        "grade": {
            "sleeves": {
                sleeve: list(factors) for sleeve, factors in GRADE_FACTORS.items()
            },
            "factor_definitions": FACTOR_DEFINITIONS,
            "sleeve_weighting": "equal",
            "within_sleeve_weighting": "equal",
            "minimum_computable_factors": 7,
            "all_three_sleeves_required": True,
            "minimum_peer_group": 100,
            "peer_minimum_basis": "companies with a computable composite",
            "thin_coverage_normalization": "divide by sqrt(w'Rw) from the run's empirical rank-correlation matrix",
            "bands": [
                {"upper_percentile": upper, "grade": grade}
                for upper, grade in GRADE_BANDS
            ],
            "boundary_rule": "show both adjacent letters within one sampling standard deviation",
            "live_snapshot_feeds_grade": False,
        },
        "chs_12m": {
            "intercept": CHS_12M_INTERCEPT,
            "coefficients": CHS_12M_COEFFICIENTS,
            "winsorization": "pooled 5th and 95th percentiles",
            "imputation": False,
            "fitted_sample": "1963-2003",
            "caveat": "fitted pre-2009 and not presented as a current calibration",
            "probability_type": "conditional month-12 failure probability, not cumulative",
            "source": "Campbell, Hilscher, and Szilagyi (2008)",
        },
        "market_data": {
            "historical_provider": "Alpaca",
            "historical_feed": market_feed,
            "live_provider": "Alpaca",
            "live_feed": live_market_feed,
            "live_cache_seconds": 15,
            "sp500_market_value": {
                "value": sp500_market_value,
                "as_of": (
                    sp500_market_value_as_of.isoformat()
                    if sp500_market_value_as_of
                    else None
                ),
                "source": sp500_market_value_source,
            },
        },
        "point_in_time": {
            "fundamental_cutoff": "filed date on or before as_of",
            "late_comparative_limit_days": 400,
            "missing_data_imputed": False,
        },
        "implied_expectations": {
            "feeds_grade": False,
            "verdict": None,
            "predictive_claim": False,
            "quadrants": False,
        },
        "disclaimer": "Informational and educational only. Not investment advice.",
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(
                value,
                handle,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
        temp_path.replace(path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, parse_constant=_reject_json_constant)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read output artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"output artifact {path} is not a JSON object")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value}")


def _rows_by_ticker(value: Any, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{label} ticker rows are malformed")
    rows: dict[str, dict[str, Any]] = {}
    for row in value:
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or not ticker or ticker in rows:
            raise ValueError(f"{label} contains a missing or duplicate ticker")
        rows[ticker] = row
    return rows


def _verify_group_artifacts(
    root: Path,
    index: dict[str, Any],
    directory: str,
    name_field: str,
    as_of: str,
    generated_at: str,
) -> dict[str, set[str]]:
    rows = index.get(directory)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{directory} index rows are malformed")
    expected_files = {"index.json"}
    names: set[str] = set()
    memberships: dict[str, set[str]] = {}
    for row in rows:
        name = row.get(name_field)
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f"{directory} index has a missing or duplicate name")
        names.add(name)
        filename = f"{quote(name, safe='-.')}.json"
        expected_files.add(filename)
        expected_artifact = f"{directory}/{filename}"
        if "artifact" in row and row["artifact"] != expected_artifact:
            raise ValueError(f"{name} has an unexpected {directory} artifact path")
        report = _read_json(root / expected_artifact)
        if (
            report.get(name_field) != name
            or report.get("as_of") != as_of
            or report.get("generated_at") != generated_at
        ):
            raise ValueError(f"{name} {directory} artifact metadata does not agree")
        tickers = report.get("tickers")
        if not isinstance(tickers, list) or row.get("ticker_count") != len(tickers):
            raise ValueError(f"{name} {directory} ticker count does not agree")
        memberships[name] = set(_rows_by_ticker(tickers, f"{name} {directory}"))
    actual_files = {
        path.name for path in (root / directory).glob("*.json") if path.is_file()
    }
    if actual_files != expected_files:
        raise ValueError(f"{directory} artifact set does not match its index")
    return memberships


def _compact_factor(factor: FactorResult) -> FactorResult:
    return FactorResult(
        factor.name,
        factor.value,
        factor.unit,
        factor.direction,
        factor.period_end,
        reason=factor.reason,
        detail=factor.detail,
        sleeve=factor.sleeve,
    )


def _compact_chs_raw(result: ChsRawResult) -> ChsRawResult:
    return ChsRawResult(
        result.status,
        result.inputs,
        result.accounting_period_end,
        {},
        {},
        result.reason,
        result.detail,
    )


def _ambiguous_share_issuance() -> FactorResult:
    return FactorResult(
        "net_share_issuance",
        None,
        "growth_rate",
        "lower",
        None,
        reason="missing_input",
        detail="firm-wide XBRL shares cannot be assigned to one ticker class",
    )


def _build_chs_raw(
    companyfacts: dict[str, Any],
    as_of: date,
    raw: list[Any],
    split: list[Any],
    returns: list[Any],
    benchmark: list[Any],
    sp500_market_value: float | None,
    *,
    multiple_share_classes: bool,
) -> ChsRawResult:
    if multiple_share_classes:
        return ChsRawResult(
            "unresolved",
            None,
            None,
            {},
            {},
            "multiple_share_classes",
            "firm-wide XBRL shares cannot be assigned to one ticker class",
        )
    if sp500_market_value is None:
        return ChsRawResult(
            "unresolved",
            None,
            None,
            {},
            {},
            "missing_sp500_market_value",
            "an as-of S&P 500 constituent market value is required for RSIZE",
        )
    return build_company_chs_raw(
        companyfacts,
        as_of,
        raw,
        split,
        benchmark,
        sp500_market_value=sp500_market_value,
        return_bars=returns,
    )


def _ineligible_report(
    entry: UniverseEntry, as_of: date, generated_at: str
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "as_of": as_of.isoformat(),
        "company": entry.to_dict(),
        "status": entry.status,
        "reason": entry.reason,
        "grade": None,
        "disclaimer": "Informational and educational only. Not investment advice.",
    }


def _analysis_error_report(
    entry: UniverseEntry, as_of: date, generated_at: str, detail: str
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "as_of": as_of.isoformat(),
        "company": entry.to_dict(),
        "status": "unresolved",
        "reason": "analysis_error",
        "detail": detail,
        "grade": None,
        "disclaimer": "Informational and educational only. Not investment advice.",
    }
