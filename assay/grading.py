from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from .factors import FactorResult


GRADE_FACTORS = {
    "profitability": (
        "gross_profitability",
        "roic",
        "accruals",
        "gross_margin_stability_5y",
    ),
    "solvency": (
        "chs_12m",
        "altman_z_double_prime",
        "net_debt_to_ebitda",
        "interest_coverage",
        "current_ratio",
    ),
    "growth_and_financing": (
        "revenue_cagr_3y",
        "fcf_cagr_3y",
        "reinvestment_rate",
        "asset_growth_3y",
        "net_share_issuance",
    ),
}
PARENT_SECTORS = {
    "consumer_nondurables": "consumer",
    "consumer_durables": "consumer",
    "shops": "consumer",
    "manufacturing": "industrial",
    "chemicals": "industrial",
    "business_equipment": "technology_and_communications",
    "telecom": "technology_and_communications",
    "energy": "energy_and_utilities",
    "utilities": "energy_and_utilities",
    "healthcare": "healthcare",
    "other": "other",
}
GRADE_BANDS = ((0.2, "A"), (0.4, "B"), (0.6, "C"), (0.8, "D"), (1.0, "E"))


@dataclass(frozen=True)
class CompanyFactors:
    ticker: str
    sector: str
    factors: tuple[FactorResult, ...]
    exchange: str = ""


@dataclass(frozen=True)
class GradeResult:
    ticker: str
    status: str
    grade: str | None
    percentile: float | None
    peer_group: str
    peer_count: int
    sampling_standard_deviation: float | None
    coverage: dict[str, int]
    composite: float | None
    sleeve_scores: dict[str, float]
    factor_percentiles: dict[str, dict[str, Any]]
    reason: str | None = None
    factor_weights: dict[str, float] = field(default_factory=dict)
    correlation_standard_deviation: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "status": self.status,
            "grade": self.grade,
            "percentile": self.percentile,
            "peer_group": self.peer_group,
            "peer_count": self.peer_count,
            "sampling_standard_deviation": self.sampling_standard_deviation,
            "coverage": self.coverage,
            "composite": self.composite,
            "sleeve_scores": self.sleeve_scores,
            "factor_percentiles": self.factor_percentiles,
            "reason": self.reason,
            "normalization": {
                "method": "sqrt(w'Rw)",
                "factor_weights": self.factor_weights,
                "correlation_standard_deviation": self.correlation_standard_deviation,
            },
        }


def grade_universe(
    companies: list[CompanyFactors],
    *,
    min_peers: int = 100,
    minimum_factors: int = 7,
    peer_policy: Literal["auto", "all", "exchange"] = "auto",
    weight_policy: Literal["sleeves", "factors"] = "sleeves",
) -> dict[str, GradeResult]:
    peer_memberships = _peer_memberships(
        companies, min_peers, peer_policy, minimum_factors
    )
    factor_percentiles = _factor_percentiles(companies, peer_memberships)
    correlations = _factor_correlations(companies)
    composites: dict[str, float] = {}
    sleeves_by_ticker: dict[str, dict[str, float]] = {}
    weights_by_ticker: dict[str, dict[str, float]] = {}
    standard_deviations: dict[str, float] = {}

    for company in companies:
        percentiles = factor_percentiles[company.ticker]
        sleeve_scores = _sleeve_scores(percentiles)
        sleeves_by_ticker[company.ticker] = sleeve_scores
        if len(percentiles) < minimum_factors or len(sleeve_scores) < len(GRADE_FACTORS):
            continue
        weights = _factor_weights(percentiles, weight_policy)
        weights_by_ticker[company.ticker] = weights
        variance = sum(
            left_weight
            * right_weight
            * correlations.get((left, right), 1.0 if left == right else 0.0)
            for left, left_weight in weights.items()
            for right, right_weight in weights.items()
        )
        if variance <= 0:
            continue
        standard_deviations[company.ticker] = math.sqrt(variance)
        composites[company.ticker] = sum(
            weights[name] * (percentiles[name]["percentile"] - 0.5)
            for name in weights
        ) / standard_deviations[company.ticker]

    composite_percentiles: dict[str, dict[str, float]] = {}
    composite_counts: dict[str, int] = {}
    for group_name, peer_tickers in _membership_groups(peer_memberships).items():
        peer_composites = {
            ticker: composites[ticker]
            for ticker in peer_tickers
            if ticker in composites
        }
        composite_percentiles[group_name] = _percentile_map(
            peer_composites, lower_is_better=True
        )
        composite_counts[group_name] = len(peer_composites)

    results: dict[str, GradeResult] = {}
    for company in companies:
        peer_group, peer_tickers = peer_memberships[company.ticker]
        percentiles = factor_percentiles[company.ticker]
        coverage = {"resolved": len(percentiles), "wanted": 14}
        if company.ticker not in composites:
            reason = (
                "computable_factors_below_threshold"
                if len(percentiles) < minimum_factors
                else "missing_sleeve_or_degenerate_correlation"
            )
            results[company.ticker] = GradeResult(
                company.ticker,
                "unresolved",
                None,
                None,
                peer_group,
                len(peer_tickers),
                None,
                coverage,
                None,
                sleeves_by_ticker[company.ticker],
                percentiles,
                reason=reason,
                factor_weights=weights_by_ticker.get(company.ticker, {}),
                correlation_standard_deviation=standard_deviations.get(
                    company.ticker
                ),
            )
            continue
        percentile = composite_percentiles[peer_group][company.ticker]
        peer_count = composite_counts[peer_group]
        if peer_count < min_peers:
            results[company.ticker] = GradeResult(
                company.ticker,
                "unresolved",
                None,
                None,
                peer_group,
                peer_count,
                None,
                coverage,
                None,
                sleeves_by_ticker[company.ticker],
                percentiles,
                reason="insufficient_peer_group",
                factor_weights=weights_by_ticker[company.ticker],
                correlation_standard_deviation=standard_deviations[company.ticker],
            )
            continue
        sampling_sd = sampling_standard_deviation(peer_count)
        grade = grade_label(percentile, sampling_sd)
        results[company.ticker] = GradeResult(
            company.ticker,
            "resolved",
            grade,
            percentile,
            peer_group,
            peer_count,
            sampling_sd,
            coverage,
            composites[company.ticker],
            sleeves_by_ticker[company.ticker],
            percentiles,
            factor_weights=weights_by_ticker[company.ticker],
            correlation_standard_deviation=standard_deviations[company.ticker],
        )
    return results


def grade_sensitivity(
    companies: list[CompanyFactors], ticker: str, *, min_peers: int = 100
) -> dict[str, dict[str, Any]]:
    return grade_sensitivity_universe(companies, min_peers=min_peers)[ticker]


def grade_sensitivity_universe(
    companies: list[CompanyFactors], *, min_peers: int = 100
) -> dict[str, dict[str, dict[str, Any]]]:
    scenarios = {
        "baseline_sector_rollup_equal_sleeves": ("auto", "sleeves"),
        "all_eligible_peer_universe_equal_sleeves": ("all", "sleeves"),
        "same_exchange_peer_universe_equal_sleeves": ("exchange", "sleeves"),
        "sector_rollup_equal_factors": ("auto", "factors"),
    }
    output: dict[str, dict[str, dict[str, Any]]] = {
        company.ticker: {} for company in companies
    }
    for name, (peer_policy, weight_policy) in scenarios.items():
        results = grade_universe(
            companies,
            min_peers=min_peers,
            peer_policy=peer_policy,
            weight_policy=weight_policy,
        )
        for ticker, result in results.items():
            output[ticker][name] = {
                "status": result.status,
                "grade": result.grade,
                "percentile": result.percentile,
                "peer_group": result.peer_group,
                "peer_count": result.peer_count,
                "peer_policy": peer_policy,
                "weight_policy": weight_policy,
            }
    return output


def grade_label(percentile: float, sampling_sd: float) -> str:
    plain = next(label for upper, label in GRADE_BANDS if percentile <= upper)
    labels = [label for _, label in GRADE_BANDS]
    for index, (edge, _) in enumerate(GRADE_BANDS[:-1]):
        if abs(percentile - edge) <= sampling_sd:
            return f"{labels[index]}/{labels[index + 1]}"
    return plain


def sampling_standard_deviation(peer_count: int) -> float:
    """Interpolate the research audit's measured n=30, 100, and 500 values."""
    anchors = ((30, 0.052), (100, 0.040), (500, 0.022))
    if peer_count <= anchors[0][0]:
        return anchors[0][1] * math.sqrt(anchors[0][0] / max(peer_count, 1))
    for (left_n, left_sd), (right_n, right_sd) in zip(anchors, anchors[1:]):
        if peer_count <= right_n:
            position = (math.log(peer_count) - math.log(left_n)) / (
                math.log(right_n) - math.log(left_n)
            )
            return left_sd + position * (right_sd - left_sd)
    return anchors[-1][1] * math.sqrt(anchors[-1][0] / peer_count)


def _peer_memberships(
    companies: list[CompanyFactors],
    min_peers: int,
    policy: str,
    minimum_factors: int,
) -> dict[str, tuple[str, set[str]]]:
    all_peers = {company.ticker for company in companies}
    gradable = {
        company.ticker
        for company in companies
        if _has_minimum_coverage(company, minimum_factors)
    }
    if policy == "all":
        return {
            company.ticker: ("all_eligible", all_peers) for company in companies
        }
    if policy == "exchange":
        exchange_groups: dict[str, set[str]] = {}
        for company in companies:
            if company.exchange:
                exchange_groups.setdefault(company.exchange, set()).add(company.ticker)
        return {
            company.ticker: (
                (f"exchange:{company.exchange}", exchange_groups[company.exchange])
                if company.exchange
                and len(exchange_groups[company.exchange] & gradable) >= min_peers
                else ("all_eligible", all_peers)
            )
            for company in companies
        }
    sector_groups: dict[str, set[str]] = {}
    parent_groups: dict[str, set[str]] = {}
    for company in companies:
        sector_groups.setdefault(company.sector, set()).add(company.ticker)
        parent = PARENT_SECTORS.get(company.sector, "other")
        parent_groups.setdefault(parent, set()).add(company.ticker)
    memberships: dict[str, tuple[str, set[str]]] = {}
    for company in companies:
        sector = sector_groups[company.sector]
        parent = PARENT_SECTORS.get(company.sector, "other")
        if len(sector & gradable) >= min_peers:
            memberships[company.ticker] = (company.sector, sector)
        elif len(parent_groups[parent] & gradable) >= min_peers:
            memberships[company.ticker] = (parent, parent_groups[parent])
        else:
            memberships[company.ticker] = ("all_eligible", all_peers)
    return memberships


def _has_minimum_coverage(
    company: CompanyFactors, minimum_factors: int
) -> bool:
    resolved = {
        factor.name
        for factor in company.factors
        if factor.value is not None and factor.name in _all_factor_names()
    }
    return len(resolved) >= minimum_factors and all(
        resolved.intersection(names) for names in GRADE_FACTORS.values()
    )


def _membership_groups(
    memberships: dict[str, tuple[str, set[str]]],
) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {}
    for group_name, members in memberships.values():
        previous = groups.setdefault(group_name, members)
        if previous != members:
            raise ValueError(f"peer group {group_name!r} has inconsistent membership")
    return groups


def _factor_percentiles(
    companies: list[CompanyFactors],
    memberships: dict[str, tuple[str, set[str]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    factor_maps = {
        company.ticker: {
            factor.name: factor
            for factor in company.factors
            if factor.value is not None and factor.name in _all_factor_names()
        }
        for company in companies
    }
    output: dict[str, dict[str, dict[str, Any]]] = {
        company.ticker: {} for company in companies
    }
    for group_name, peers in _membership_groups(memberships).items():
        for name in _all_factor_names():
            peer_values = {
                ticker: peer_factor.value
                for ticker in peers
                if (peer_factor := factor_maps[ticker].get(name)) is not None
            }
            if not peer_values:
                continue
            ranks = {
                direction: _percentile_map(
                    peer_values, lower_is_better=direction == "lower"
                )
                for direction in {
                    factor_maps[ticker][name].direction for ticker in peer_values
                }
            }
            for ticker in peer_values:
                if memberships[ticker][0] != group_name:
                    continue
                factor = factor_maps[ticker][name]
                output[ticker][name] = {
                    "percentile": ranks[factor.direction][ticker],
                    "peer_group": group_name,
                    "peer_count": len(peer_values),
                    "raw_value": factor.value,
                    "direction": factor.direction,
                }
    return output


def _percentile(
    values: dict[str, float], ticker: str, *, lower_is_better: bool
) -> float:
    target = values[ticker]
    better = sum(
        value < target if lower_is_better else value > target
        for value in values.values()
    )
    tied = sum(value == target for value in values.values())
    denominator = max(len(values) - 1, 1)
    return min(1.0, (better + (tied - 1) / 2) / denominator)


def _percentile_map(
    values: dict[str, float], *, lower_is_better: bool
) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: item[1], reverse=not lower_is_better)
    denominator = max(len(ordered) - 1, 1)
    ranks: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        percentile = min(1.0, (index + (end - index - 1) / 2) / denominator)
        for ticker, _ in ordered[index:end]:
            ranks[ticker] = percentile
        index = end
    return ranks


def _sleeve_scores(percentiles: dict[str, dict[str, Any]]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for sleeve, names in GRADE_FACTORS.items():
        values = [percentiles[name]["percentile"] for name in names if name in percentiles]
        if values:
            scores[sleeve] = sum(values) / len(values)
    return scores


def _factor_weights(
    percentiles: dict[str, dict[str, Any]], policy: str
) -> dict[str, float]:
    if policy == "factors":
        return {name: 1 / len(percentiles) for name in percentiles}
    available_sleeves = {
        sleeve: [name for name in names if name in percentiles]
        for sleeve, names in GRADE_FACTORS.items()
    }
    available_sleeves = {
        sleeve: names for sleeve, names in available_sleeves.items() if names
    }
    sleeve_weight = 1 / len(available_sleeves)
    return {
        name: sleeve_weight / len(names)
        for names in available_sleeves.values()
        for name in names
    }


def _factor_correlations(
    companies: list[CompanyFactors],
) -> dict[tuple[str, str], float]:
    raw_values = {
        company.ticker: {
            factor.name: factor
            for factor in company.factors
            if factor.value is not None and factor.name in _all_factor_names()
        }
        for company in companies
    }
    values: dict[str, dict[str, float]] = {company.ticker: {} for company in companies}
    for name in _all_factor_names():
        peers = {
            ticker: factor.value
            for ticker, factors in raw_values.items()
            if (factor := factors.get(name)) is not None
        }
        ranks = {
            direction: _percentile_map(
                peers, lower_is_better=direction == "lower"
            )
            for direction in {raw_values[ticker][name].direction for ticker in peers}
        }
        for ticker in peers:
            factor = raw_values[ticker][name]
            values[ticker][name] = ranks[factor.direction][ticker]
    correlations: dict[tuple[str, str], float] = {}
    names = sorted(_all_factor_names())
    for left in names:
        for right in names:
            if left == right:
                correlations[(left, right)] = 1.0
                continue
            pairs = [
                (row[left], row[right])
                for row in values.values()
                if left in row and right in row
            ]
            correlations[(left, right)] = _pearson(pairs)
    return correlations


def _pearson(pairs: list[tuple[float, float]]) -> float:
    if len(pairs) < 3:
        return 0.0
    left_mean = sum(left for left, _ in pairs) / len(pairs)
    right_mean = sum(right for _, right in pairs) / len(pairs)
    numerator = sum(
        (left - left_mean) * (right - right_mean) for left, right in pairs
    )
    left_variance = sum((left - left_mean) ** 2 for left, _ in pairs)
    right_variance = sum((right - right_mean) ** 2 for _, right in pairs)
    denominator = math.sqrt(left_variance * right_variance)
    return numerator / denominator if denominator else 0.0


def _all_factor_names() -> set[str]:
    return {name for names in GRADE_FACTORS.values() for name in names}
