from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from .sec import Company


UniverseStatus = Literal["eligible", "excluded", "unresolved"]

SECTOR_RANGES: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
    (
        "consumer_nondurables",
        ((100, 999), (2000, 2399), (2700, 2749), (2770, 2799), (3100, 3199), (3940, 3989)),
    ),
    (
        "consumer_durables",
        ((2500, 2519), (2590, 2599), (3630, 3659), (3710, 3711), (3714, 3714),
         (3716, 3716), (3750, 3751), (3792, 3792), (3900, 3939), (3990, 3999)),
    ),
    (
        "manufacturing",
        ((2520, 2589), (2600, 2699), (2750, 2769), (3000, 3099), (3200, 3569),
         (3580, 3629), (3700, 3709), (3712, 3713), (3715, 3715), (3717, 3749),
         (3752, 3791), (3793, 3799), (3830, 3839), (3860, 3899)),
    ),
    ("energy", ((1200, 1399), (2900, 2999))),
    ("chemicals", ((2800, 2829), (2840, 2899))),
    (
        "business_equipment",
        ((3570, 3579), (3660, 3692), (3694, 3699), (3810, 3829), (7370, 7379)),
    ),
    ("telecom", ((4800, 4899),)),
    ("utilities", ((4900, 4949),)),
    ("shops", ((5000, 5999), (7200, 7299), (7600, 7699))),
    ("healthcare", ((2830, 2839), (3693, 3693), (3840, 3859), (8000, 8099))),
)


@dataclass(frozen=True)
class UniverseEntry:
    ticker: str
    cik: int
    name: str
    sic: int | None
    sic_description: str
    exchange: str
    sector: str | None
    status: UniverseStatus
    reason: str | None
    has_companyfacts: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def companies_from_ticker_file(data: dict[str, Any]) -> list[Company]:
    companies: dict[str, Company] = {}
    for row in data.values():
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker or ticker in companies:
            continue
        try:
            cik = int(row["cik_str"])
        except (KeyError, TypeError, ValueError):
            continue
        companies[ticker] = Company(ticker, cik, str(row.get("title", ticker)))
    return sorted(companies.values(), key=lambda company: company.ticker)


def classify_company(
    company: Company,
    submissions: dict[str, Any],
    *,
    has_companyfacts: bool | None = None,
) -> UniverseEntry:
    forms = {
        str(form).upper()
        for form in submissions.get("filings", {}).get("recent", {}).get("form", [])
    }
    exchange = _exchange_for_ticker(company.ticker, submissions)
    try:
        sic = int(submissions.get("sic"))
    except (TypeError, ValueError):
        sic = None
    base = {
        "ticker": company.ticker,
        "cik": company.cik,
        "name": str(submissions.get("name") or company.name),
        "sic": sic,
        "sic_description": str(submissions.get("sicDescription") or ""),
        "exchange": exchange,
        "has_companyfacts": has_companyfacts,
    }

    operating = bool(forms & {"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "10-QT"})
    latest_annual_form = _latest_annual_form(submissions)
    reason: str | None = None
    status: UniverseStatus = "excluded"
    if not forms:
        status, reason = "unresolved", "missing_submission_history"
    elif latest_annual_form in {"20-F", "20-F/A", "40-F", "40-F/A"}:
        reason = "foreign_issuer"
    elif not operating and forms & {"20-F", "20-F/A", "40-F", "40-F/A", "F-6", "F-6/A"}:
        reason = "foreign_issuer"
    elif not operating and any(form.startswith("N-") for form in forms):
        reason = "registered_fund"
    elif not operating and forms & {"10-D", "10-D/A"}:
        reason = "asset_backed_issuer"
    elif not operating and forms & {"S-1", "S-1/A", "F-1", "F-1/A"}:
        reason = "registration_stage"
    elif not operating and _has_ownership_form(forms):
        reason = "ownership_forms_only"
    elif not operating:
        reason = "no_domestic_operating_reports"
    elif sic is None:
        status, reason = "unresolved", "missing_sic"
    elif 6000 <= sic <= 6999:
        reason = "financial_or_reit"
    elif not exchange or "OTC" in exchange.upper():
        reason = "otc_or_unlisted"
    elif has_companyfacts is False:
        status, reason = "unresolved", "missing_companyfacts"
    else:
        status = "eligible"

    return UniverseEntry(
        **base,
        sector=sector_for_sic(sic) if status == "eligible" and sic is not None else None,
        status=status,
        reason=reason,
    )


def sector_for_sic(sic: int) -> str:
    if 6000 <= sic <= 6999:
        return "financials"
    for sector, ranges in SECTOR_RANGES:
        if any(lower <= sic <= upper for lower, upper in ranges):
            return sector
    return "other"


def _exchange_for_ticker(ticker: str, submissions: dict[str, Any]) -> str:
    tickers = [str(value).upper() for value in submissions.get("tickers", [])]
    exchanges = [str(value) for value in submissions.get("exchanges", [])]
    try:
        index = tickers.index(ticker.upper())
    except ValueError:
        return ""
    return exchanges[index] if index < len(exchanges) else ""


def _has_ownership_form(forms: set[str]) -> bool:
    prefixes = ("SC 13", "13F", "13D", "13G")
    return bool(forms & {"3", "4", "5", "144"}) or any(
        form.startswith(prefixes) for form in forms
    )


def _latest_annual_form(submissions: dict[str, Any]) -> str | None:
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    annual = {"10-K", "10-K/A", "10-KT", "20-F", "20-F/A", "40-F", "40-F/A"}
    candidates = [
        (str(filed), str(form).upper())
        for form, filed in zip(forms, dates)
        if str(form).upper() in annual
    ]
    return max(candidates)[1] if candidates else None
