"""Size stratum, standard-universe membership, and the reliability figures that go with them.

The numbers in config/reliability.json are measured, not fitted here. Each one carries
its window and its status. Nothing in this module changes a grade; it says which
population a company belongs to and how well the failure model ranks that population.
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

from .facts import MissingFact, ResolvedFact

RELIABILITY_PATH = Path(__file__).resolve().parents[1] / "config" / "reliability.json"

_STRATUM_FIELDS = ("id", "label", "auc", "ci", "test_events", "base_rate_annual")


def load_reliability(path: str | Path = RELIABILITY_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
    validate_reliability(config)
    return config


def validate_reliability(config: dict[str, Any]) -> None:
    strata = config.get("strata")
    if not isinstance(strata, list) or not strata:
        raise ValueError("reliability config needs a non-empty strata list")
    previous_max: float | None = None
    for stratum in strata:
        for field in _STRATUM_FIELDS:
            if field not in stratum:
                raise ValueError(f"stratum {stratum.get('id')!r} is missing {field}")
        low, high = stratum.get("assets_min"), stratum.get("assets_max")
        if previous_max is None and low is not None:
            raise ValueError("first stratum must be open below")
        if previous_max is not None and low != previous_max:
            raise ValueError(f"stratum {stratum['id']} does not start where the last one ended")
        if high is not None and low is not None and high <= low:
            raise ValueError(f"stratum {stratum['id']} has an empty range")
        _validate_estimate(stratum, stratum["id"])
        previous_max = high
    if previous_max is not None:
        raise ValueError("last stratum must be open above")
    universe = config.get("standard_universe")
    if not isinstance(universe, dict):
        raise ValueError("reliability config needs a standard_universe block")
    for key in ("revenue_min", "assets_min"):
        if not isinstance(universe.get(key), (int, float)) or universe[key] <= 0:
            raise ValueError(f"standard_universe.{key} must be a positive number")
    for side in ("inside", "outside"):
        _validate_estimate(universe[side], f"standard_universe.{side}")
    _validate_estimate(config["all"], "all")


def _validate_estimate(block: dict[str, Any], label: str) -> None:
    auc, ci = block.get("auc"), block.get("ci")
    if not isinstance(auc, (int, float)) or not 0.0 <= auc <= 1.0:
        raise ValueError(f"{label} AUC must lie in [0, 1]")
    if (
        not isinstance(ci, list)
        or len(ci) != 2
        or not all(isinstance(bound, (int, float)) for bound in ci)
        or not ci[0] <= auc <= ci[1]
    ):
        raise ValueError(f"{label} confidence interval must bracket its AUC")
    if type(block.get("test_events")) is not int or block["test_events"] <= 0:
        raise ValueError(f"{label} needs a positive integer event count")


def assign_stratum(assets: float | None, config: dict[str, Any]) -> dict[str, Any] | None:
    if assets is None or isinstance(assets, bool) or not math.isfinite(assets) or assets < 0:
        return None
    for stratum in config["strata"]:
        low = stratum.get("assets_min", -math.inf)
        high = stratum.get("assets_max", math.inf)
        if low <= assets < high:
            return {
                "id": stratum["id"],
                "label": stratum["label"],
                "assets_min": stratum.get("assets_min"),
                "assets_max": stratum.get("assets_max"),
            }
    return None


def standard_universe(
    revenue: float | None, assets: float | None, config: dict[str, Any]
) -> dict[str, Any]:
    rule = config["standard_universe"]
    missing = [name for name, value in (("revenue", revenue), ("assets", assets)) if value is None]
    result: dict[str, Any] = {
        "inside": None,
        "rule": rule["rule"],
        "revenue_min": rule["revenue_min"],
        "assets_min": rule["assets_min"],
        "revenue": revenue,
        "assets": assets,
    }
    if missing:
        result["reason"] = "missing_input"
        result["detail"] = f"{' and '.join(missing)} not resolved"
        return result
    result["inside"] = bool(revenue > rule["revenue_min"] and assets > rule["assets_min"])
    return result


def reliability_for(
    stratum_id: str | None, inside: bool | None, config: dict[str, Any]
) -> dict[str, Any]:
    stratum = next((s for s in config["strata"] if s["id"] == stratum_id), None)
    universe = config["standard_universe"]
    side = None if inside is None else ("inside" if inside else "outside")
    return {
        "model": config["model"],
        "outcome": config["outcome"],
        "fit_window": config["fit_window"],
        "test_window": config["test_window"],
        "status": config["status"],
        "confirmation": config["confirmation"],
        "source": config["source"],
        "base_rate_window": config.get("base_rate_window"),
        "all": dict(config["all"]),
        "stratum": (
            {
                "id": stratum["id"],
                "auc": stratum["auc"],
                "ci": list(stratum["ci"]),
                "test_events": stratum["test_events"],
                "base_rate_annual": stratum["base_rate_annual"],
            }
            if stratum
            else None
        ),
        "universe": (
            {"inside": inside, **{k: v for k, v in universe[side].items()}}
            if side
            else None
        ),
    }


def data_age(core_facts: list[ResolvedFact | MissingFact], as_of: date) -> dict[str, Any]:
    basis = "annual statements behind the grade; see diagnostics.filing_gap for the latest periodic filing"
    filed = [fact.filed for fact in core_facts if isinstance(fact, ResolvedFact)]
    if not filed:
        return {"latest_filed": None, "days": None, "as_of": as_of.isoformat(), "basis": basis}
    latest = max(filed)
    return {
        "latest_filed": latest,
        "days": (as_of - date.fromisoformat(latest)).days,
        "as_of": as_of.isoformat(),
        "basis": basis,
    }


def build_condition(
    core_facts: list[ResolvedFact | MissingFact],
    as_of: date,
    config: dict[str, Any],
) -> dict[str, Any]:
    resolved = {fact.concept: fact for fact in core_facts if isinstance(fact, ResolvedFact)}
    assets = resolved.get("assets")
    revenue = resolved.get("revenue")
    stratum = assign_stratum(assets.value if assets else None, config)
    if stratum and assets:
        stratum["assets"] = assets.value
        stratum["receipt"] = _receipt(assets)
    universe = standard_universe(
        revenue.value if revenue else None, assets.value if assets else None, config
    )
    universe["receipts"] = {
        name: _receipt(fact) for name, fact in (("revenue", revenue), ("assets", assets)) if fact
    }
    return {
        "stratum": stratum,
        "standard_universe": universe,
        "reliability": reliability_for(stratum["id"] if stratum else None, universe["inside"], config),
        "data_age": data_age(core_facts, as_of),
    }


def _receipt(fact: ResolvedFact) -> dict[str, Any]:
    return {
        "tag": f"{fact.namespace}:{fact.tag}",
        "end": fact.end,
        "filed": fact.filed,
        "form": fact.form,
        "accession": fact.accession,
        "filing_url": fact.filing_url,
    }
