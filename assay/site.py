"""The whole static site: front page, method pages, screens, sector pages, company pages.

    python3 -m assay.site --data data --out site --history .state/grade_history.json \
        --prices prices.json --descriptions descriptions.json --calendar filing_calendar.json \
        --history-index data/history/index [--tickers INTC LCID | --all]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import shutil
import statistics
from datetime import date, timedelta
from html import escape
from pathlib import Path
from typing import Any

from . import animals, export, home
from .pdf import company_report
from .history import ticker_history
from .render import CHARSET, CSS, FACTORS, FLAG_LABELS, TIP_SCRIPT, WARN_SYMBOL, credit_footer, _company_cell, _money, density_chart, load_peers, long_date, nav_bar, pct_points, peer_factor_medians, render as render_company
from .stratum import load_reliability

SECTOR_LABELS = {
    "business_equipment": "Business equipment", "healthcare": "Healthcare", "other": "Other", "manufacturing": "Manufacturing",
    "shops": "Shops", "consumer_nondurables": "Consumer nondurables", "utilities": "Utilities", "consumer_durables": "Consumer durables",
    "chemicals": "Chemicals", "energy": "Energy", "telecom": "Telecom",
}
REFUSALS = {
    "financial_or_reit": "SIC 6000 to 6999: banks, insurers, brokers and REITs. Their statements do not carry the inputs the grade needs.",
    "foreign_issuer": "Latest annual report is a 20-F or 40-F, or no 10-K or 10-Q on record and foreign forms present.",
    "otc_or_unlisted": "No exchange on the SEC record, or an OTC market.",
    "no_domestic_operating_reports": "No 10-K, 10-Q, 10-KT or 10-QT on record.",
    "registered_fund": "No operating reports; N-series fund forms on record.",
    "registration_stage": "No operating reports; only S-1 or F-1 registration filings.",
    "ownership_forms_only": "No operating reports; only ownership forms (3, 4, 5, 144, 13D, 13G, 13F).",
    "asset_backed_issuer": "No operating reports; 10-D asset-backed reports on record.",
    "missing_sic": "No SIC code on the SEC record, so no sector; left unresolved.",
    "missing_companyfacts": "No XBRL facts file for the CIK; left unresolved.",
    "missing_submission_history": "No filings at all on the SEC record; left unresolved.",
}
CHECKS = [
    ("dilution", "Dilution", "split-adjusted shares outstanding up 25% or more in one year"),
    ("short_runway", "Short runway", "cash and equivalents divided by trailing four-quarter operating cash burn under 6 months; only when burning cash"),
    ("late_filer", "Late filer", "any NT 10-K or NT 10-Q in the last 730 days"),
    ("distress", "Distress", "failure probability in the top decile of the filers where it resolves"),
    ("degenerate_inputs", "Degenerate inputs", "latest revenue or total assets at or below $100,000"),
    ("fortress", "Fortress balance sheet", "cash above total debt; informational, never a warning"),
    ("thin_data", "Thin data", "fewer than 7 of the 14 inputs computable; no letter is given"),
]
PAPERS = {
    "gross_profitability": "Novy-Marx, 2013", "accruals": "Sloan, 1996", "chs_12m": "Campbell, Hilscher and Szilagyi, 2008",
    "altman_z_double_prime": "Altman, 1993", "net_share_issuance": "Pontiff and Woodgate, 2008", "asset_growth_3y": "Cooper, Gulen and Schill, 2008",
}
LETTER_NAMES = {"A": "strongest fifth", "B": "second fifth", "C": "middle fifth", "D": "fourth fifth", "E": "weakest fifth"}


# ---------------------------------------------------------------- chrome

def hero(name: str, alt: str) -> str:
    src = animals.photo(name, 640)
    return f'<img class="hero" src="{src}" alt="{escape(alt)}">' if src else ""


def chrome(title: str, body: str, as_of: str, page_link: str = "./", credits: bool = False) -> str:
    return (
        f"{CHARSET}<title>{escape(title)}</title><style>{CSS}{home.HOME_CSS}</style>{WARN_SYMBOL}<div class=\"page\" id=\"top\" data-base=\"{escape(page_link)}\">"
        '<div class="center"><h1>Assay</h1><p><b>Financial-condition grades for SEC filers</b></p></div>'
        f"{nav_bar(page_link)}{body}"
        '<hr><div class="footer"><p class="small">Not investment advice. Informational and educational only. No adviser relationship. Data from SEC EDGAR, may contain errors, is not warranted. The grade ranks reported financial condition and is not a return forecast.</p>'
        + (credit_footer() if credits else "")
        + f'<p class="small">Updated {escape(long_date(as_of))}</p></div></div>' + TIP_SCRIPT
    )


def heading(text: str) -> str:
    return f'<h2>{escape(text)} <a class="top" href="#top">[top]</a></h2>'


def table(headers: list[str], rows: list[str], wide: bool = True) -> str:
    head = "".join(f'<th class="n">{escape(h[1:])}</th>' if h.startswith(">") else f"<th>{escape(h)}</th>" for h in headers)
    return f'<div class="scroll{" peers" if wide else ""}"><table class="data"><tr>{head}</tr>{"".join(rows)}</table></div>'


def cell(state: dict[str, Any], row: dict[str, Any], page_link: str) -> str:
    return _company_cell(row, state["mcap"].get(row["ticker"]), None, page_link)


# ---------------------------------------------------------------- history indexes

def load_history_index(folder: Path | None) -> dict[str, dict[str, dict[str, Any]]]:
    """date -> ticker -> {sector, grade, percentile, status} from the per-date index files."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    if not folder or not folder.exists():
        return out
    for path in sorted(folder.glob("*.json")):
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out[index["as_of"]] = {r["ticker"]: {"sector": r.get("sector"), "grade": r.get("grade"), "percentile": r.get("percentile"), "status": r.get("status")} for r in index.get("tickers", [])}
    return out


# ---------------------------------------------------------------- method pages

def methodology_page(state: dict[str, Any], data: Path, page_link: str = "./") -> str:
    m = json.loads((data / "methodology.json").read_text(encoding="utf-8"))
    g = m["grade"]
    summary = state["index"].get("summary") or {}
    rel = load_reliability()
    out = [heading("Methodology")]
    out.append("<p>The grade is a rank of reported financial condition among sector peers, with one market-informed estimate of failure risk among its inputs. It is not a return forecast.</p>")
    out.append(
        "<h2>How the Rank Is Made</h2><ol>"
        "<li><b>Fourteen inputs in three sleeves.</b> Profitability: what the business earns on what it owns. Solvency: whether it can carry what it owes. Growth and financing: whether it grows without leaning on new shares or debt. The table below gives every formula.</li>"
        f"<li><b>Percentiles within the sector.</b> Each input becomes a percentile among the graded companies of the company's sector. Lower is stronger everywhere on the site. A sector needs at least {g.get('minimum_peer_group')} graded companies.</li>"
        "<li><b>Equal weights.</b> A sleeve score is the mean of its inputs' percentiles, and the composite is the mean of the three sleeves.</li>"
        f"<li><b>Letters are fifths.</b> A is the strongest fifth of the sector, E the weakest. A grade needs at least {g.get('minimum_computable_factors')} of the 14 inputs and a score in every sleeve.</li>"
        "<li><b>Boundary letters.</b> A percentile has sampling error because the peer group is finite. When it lies within one standard error of a band edge, the page shows both letters, as in B/C.</li></ol>"
    )
    rows = []
    for sleeve, names in g["sleeves"].items():
        for name in names:
            d = g["factor_definitions"].get(name, {})
            label = FACTORS.get(name, (name, "", ""))[0]
            rows.append(f'<tr><td>{escape(sleeve.replace("_", " "))}</td><td class="w">{escape(label)}</td><td class="w">{escape(d.get("formula") or "")}{("; " + escape(d["notes"])) if d.get("notes") else ""}</td><td>{escape(d.get("direction") or "")}</td><td>{escape(PAPERS.get(name, ""))}</td></tr>')
    out.append("<h2>The Fourteen Inputs</h2>" + table(["Sleeve", "Input", "Formula", "Stronger when", "Source"], rows, wide=False))
    out.append(
        "<h2>Point in Time</h2>"
        "<p>Only facts the SEC had received by the rescan date enter that rescan. The cutoff is the receipt date, not the period the numbers describe. "
        f'A comparative figure that arrives up to {m.get("point_in_time", {}).get("late_comparative_limit_days")} days after its period is accepted. Missing values stay missing; nothing is filled in. '
        "Across the current universe a filing arrives 57 days after its period end at the median and 90 days at the 90th percentile.</p>"
    )
    chs = m.get("chs_12m") or {}
    out.append(
        '<h2 id="reliability">Failure Model and Reliability</h2>'
        f'<p>One input uses the market: the twelve-month failure probability from {escape(chs.get("source") or "")}, a logistic model of accounting ratios and market equity. It was fitted on {escape(chs.get("fitted_sample") or "")}, and {escape(chs.get("caveat") or "")}. '
        f'The figure is a {escape(chs.get("probability_type") or "")}. Its inputs are trimmed at the {escape(chs.get("winsorization") or "")} of the run. '
        f'It resolves for {summary.get("chs_resolved", 0):,} of {summary.get("eligible", 0):,} eligible filers, and the distress warning marks the top tenth among them.</p>'
        f'<p>Reliability is measured against one outcome: {escape(rel.get("outcome") or "")}. The model is {escape(rel.get("model") or "")}, fitted on {escape(rel.get("fit_window") or "")} and tested on {escape(rel.get("test_window") or "")}. '
        'AUC is the chance that a company that went on to fail ranks above one that did not; 0.5 is a coin toss and 1 is perfect. '
        f'Status: {escape(str(rel.get("status") or "").replace("_", " "))}; {escape(rel.get("confirmation") or "")}.</p>'
        + reliability_table(rel)
    )
    out.append("<h2>Warnings</h2>" + table(["Check", "Rule"], [f'<tr><td>{escape(label)}</td><td class="w">{escape(rule)}</td></tr>' for _, label, rule in CHECKS], wide=False))
    from .universe import SECTOR_RANGES
    counts: dict[str, int] = {}
    for r in state["graded"]:
        counts[r.get("sector") or "other"] = counts.get(r.get("sector") or "other", 0) + 1
    rows = [f'<tr><td><a href="{page_link}sector-{sector}.html">{escape(SECTOR_LABELS.get(sector, sector))}</a></td><td class="w">{escape(", ".join(f"{lo} to {hi}" for lo, hi in ranges))}</td><td class="n">{counts.get(sector, 0)}</td></tr>' for sector, ranges in SECTOR_RANGES]
    rows.append(f'<tr><td><a href="{page_link}sector-other.html">Other</a></td><td class="w">every other code</td><td class="n">{counts.get("other", 0)}</td></tr>')
    out.append("<h2>Peer Groups</h2><p>A company's sector comes from the four-digit SIC code on its SEC record. Banks, insurers, brokers and REITs, SIC 6000 to 6999, are outside the universe because their statements do not carry the inputs the grade needs.</p>" + table(["Sector", "SIC ranges", ">Graded today"], rows, wide=False))
    md = m.get("market_data") or {}
    sp = md.get("sp500_market_value") or {}
    out.append(
        "<h2>Sources</h2>"
        f'<p>Accounting facts from the SEC EDGAR bulk XBRL archives, refreshed nightly. Prices from {escape(md.get("historical_provider") or "")} ({escape(md.get("historical_feed") or "")} feed); the last close is the {escape(md.get("live_feed") or "")} feed. '
        f'S&amp;P 500 market value {_money(sp.get("value") or 0)} as of {escape(long_date(sp.get("as_of")))}, from <a href="{escape(sp.get("source") or "#")}">S&amp;P Dow Jones Indices</a>, used for relative size in the failure model. No market price enters the letter.</p>'
        "<h2>Diagnostics That Do Not Feed the Grade</h2><p>The Piotroski F-score, cash runway, filing gap, late-filer status and median dollar volume appear on every company page and never enter the letter. Price measures appear as measurements, with no verdict.</p>"
    )
    return chrome("Assay Methodology", "\n".join(out), state["index"]["as_of"], page_link)


def reliability_table(rel: dict[str, Any]) -> str:
    rows = [f'<tr><td>{escape(s["label"])}</td><td class="n">{s["auc"]:.3f}</td><td class="n">{s["ci"][0]:.3f} to {s["ci"][1]:.3f}</td><td class="n">{s["test_events"]}</td><td class="n">{s["base_rate_annual"] * 100:.2f}%</td></tr>' for s in rel["strata"]]
    a = rel["all"]
    rows.append(f'<tr class="total"><td>All filers</td><td class="n">{a["auc"]:.3f}</td><td class="n">{a["ci"][0]:.3f} to {a["ci"][1]:.3f}</td><td class="n">{a["test_events"]}</td><td class="n"></td></tr>')
    u = rel["standard_universe"]
    return (table(["Size band, total assets", ">AUC", ">95% interval", ">Test events", ">Failure rate a year"], rows, wide=False)
            + f'<p class="small">Standard universe: {escape(u["rule"])}. Inside: AUC {u["inside"]["auc"]:.3f}, {u["inside"]["test_events"]} events. Outside: AUC {u["outside"]["auc"]:.3f}, {u["outside"]["test_events"]} events. Failure rates are the {escape(rel.get("base_rate_window") or "")}.</p>')


def coverage_page(state: dict[str, Any], data: Path, page_link: str = "./") -> str:
    u = json.loads((data / "universe.json").read_text(encoding="utf-8"))
    s = u.get("summary") or {}
    reasons = s.get("reasons") or {}
    out = [heading("Coverage and Refusals")]
    out.append(f'<p>The SEC ticker file lists {s.get("tickers", 0):,} tickers for {s.get("unique_ciks", 0):,} filers. {s.get("eligible", 0):,} are eligible: US operating companies on an exchange, with XBRL statements and a sector code. '
               f'{s.get("excluded", 0):,} are excluded for one of the reasons below, and {s.get("unresolved", 0):,} could not be resolved. Every ticker has a page stating its status and the reason.</p>')
    out.append(table(["Reason", ">Tickers", "Rule"], [f'<tr><td>{escape(reason.replace("_", " "))}</td><td class="n">{count:,}</td><td class="w">{escape(REFUSALS.get(reason, ""))}</td></tr>' for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]) if reason != "eligible"], wide=False))
    summary = state["index"].get("summary") or {}
    letters = summary.get("grades") or {}
    graded_count = sum(v for k, v in letters.items() if k != "unresolved")
    out.append(f'<h2>Among the Eligible</h2><p>{graded_count:,} of the {s.get("eligible", 0):,} eligible companies carry a letter. The rest have fewer than 7 of the 14 inputs computable, or sit in a sector with fewer than 100 graded companies.</p>')
    by_exchange: dict[str, list[int]] = {}
    by_band: dict[str, list[int]] = {}
    by_sector: dict[str, list[int]] = {}
    for r in state["rows"]:
        for bucket, key in ((by_exchange, r.get("exchange") or "none"), (by_band, r.get("stratum") or "unassigned"), (by_sector, r.get("sector") or "other")):
            b = bucket.setdefault(key, [0, 0])
            b[0] += 1
            b[1] += 1 if r.get("grade") else 0
    labels = {s_["id"]: s_["label"] for s_ in load_reliability()["strata"]}
    out.append(table(["Exchange", ">Eligible", ">Graded"], [f'<tr><td>{escape(k)}</td><td class="n">{v[0]:,}</td><td class="n">{v[1]:,}</td></tr>' for k, v in sorted(by_exchange.items(), key=lambda kv: -kv[1][0])], wide=False))
    out.append(table(["Size band", ">Eligible", ">Graded"], [f'<tr><td>{escape(labels.get(k, k))}</td><td class="n">{v[0]:,}</td><td class="n">{v[1]:,}</td></tr>' for k, v in sorted(by_band.items())], wide=False))
    out.append(table(["Sector", ">Eligible", ">Graded"], [f'<tr><td><a href="{page_link}sector-{escape(k)}.html">{escape(SECTOR_LABELS.get(k, k))}</a></td><td class="n">{v[0]:,}</td><td class="n">{v[1]:,}</td></tr>' for k, v in sorted(by_sector.items(), key=lambda kv: -kv[1][0])], wide=False))
    return chrome("Assay Coverage", "\n".join(out), state["index"]["as_of"], page_link)


def forecasts_page(state: dict[str, Any], data: Path, page_link: str = "./") -> str:
    path = data / "forecasts.json"
    f = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"scorecard": {}, "events": []}
    sc = f.get("scorecard") or {}
    out = [heading("Forecast Log")]
    out.append("<p>A call is a dated probability that a company fails within a stated horizon, recorded with the letter it carried at the time. Calls settle against the outcome and are scored by log loss against the base rate for the size band. Nothing on a company page is a call; the grade is a rank, not a forecast.</p>")
    out.append(f'<p>Calls: {sc.get("calls", 0)}; resolved: {sc.get("resolved", 0)}; pending: {sc.get("pending", 0)}.' + (" No calls have been recorded." if not f.get("events") else "") + "</p>")
    if f.get("events"):
        rows = [f'<tr><td>{escape(str(e.get("as_of") or ""))}</td><td>{escape(str(e.get("ticker") or ""))}</td><td class="n">{e.get("probability")}</td><td>{escape(str(e.get("grade_at_call") or ""))}</td><td class="w">{escape(str(e.get("thesis") or ""))}</td><td>{escape(str(e.get("status") or ""))}</td></tr>' for e in f["events"]]
        out.append(table(["Date", "Ticker", ">Probability", "Letter", "Thesis", "Status"], rows, wide=False))
    return chrome("Assay Forecast Log", "\n".join(out), state["index"]["as_of"], page_link)


def data_page(state: dict[str, Any], out_dir: Path, page_link: str = "./") -> str:
    files = [
        ("index.csv", "every ticker, one row: status, letter, percentile, composite, size band, warnings, price measures"),
        ("inputs.csv.gz", "every computable input of every graded company: value, percentile, peers, period end"),
        ("history/rescans.csv.gz", "long format: as_of, ticker, letter, percentile, composite, band, momentum, one row per company per rescan"),
        ("history/cube.json", "per ticker, arrays of letters and percentiles aligned to the list of rescan dates; what the time machine reads"),
        ("assay.sqlite3.gz", "one SQLite database: universe, rescans, inputs, warnings, with indexes on ticker"),
        ("index.json", "every ticker: status, letter, percentile, warnings, price measures, size band, filing gap"),
        ("universe.json", "every ticker with its eligibility status and reason"),
        ("grade_history.json", "one change point per letter or band change, per ticker"),
        ("filing_calendar.json", "expected periodic filings per eligible ticker: class, period, due date, status"),
        ("methodology.json", "the run's method parameters and factor formulas"),
        ("forecasts.json", "the forecast log and scorecard"),
    ]
    rows = []
    for name, desc in files:
        path = out_dir / name
        size = "" if not path.exists() else (f"{path.stat().st_size / 1e6:.1f} MB" if path.stat().st_size > 1e6 else f"{path.stat().st_size / 1e3:.0f} KB")
        rows.append(f'<tr><td><a href="{page_link}{name}">{escape(name)}</a></td><td class="n">{size}</td><td class="w">{escape(desc)}</td></tr>')
    body = heading("Data") + f"<p>Every page is rendered from these files, and all of them are free to download. Each company page comes from its own artifact at <a href=\"{page_link}tickers/INTC.json\">tickers/TICKER.json</a>, which carries every input with its receipt: tag, value, period end, filed date, form, accession and filing URL. The Downloads section of a company page offers its PDF, its artifact, and its inputs and rescans as CSV. The rescan history covers every rescan on file; the <a href=\"{page_link}time-machine.html\">time machine</a> browses it and the <a href=\"{page_link}as-of.html\">point-in-time reader</a> opens any company on any rescan date.</p>" + table(["File", ">Size", "Contents"], rows, wide=False)
    body += '<p class="small">Field conventions: percentiles run 0 to 1 with lower stronger; money in US dollars as reported; dates ISO 8601; a missing value is null with a reason beside it. Data as is, without warranty.</p>'
    return chrome("Assay Data", body, state["index"]["as_of"], page_link)


def companies_page(state: dict[str, Any], page_link: str = "./") -> str:
    graded = sorted(state["graded"], key=lambda r: r["ticker"])
    out = [heading("Companies"), f'<p>{len(graded):,} graded companies, by ticker. {len(state["rows"]) - len(graded):,} more are eligible without a letter; see <a href="{page_link}coverage.html">coverage</a>.</p>']
    first = sorted({r["ticker"][0] for r in graded})
    out.append("<p>" + " ".join(f'<a href="#t-{escape(c)}">{escape(c)}</a>' for c in first) + "</p>")
    rows, current = [], None
    for r in graded:
        if r["ticker"][0] != current:
            current = r["ticker"][0]
            rows.append(f'<tr class="group" id="t-{escape(current)}"><td colspan="6">{escape(current)}</td></tr>')
        rows.append(f'<tr><td>{escape(r["ticker"])}</td><td>{cell(state, r, page_link)}</td><td>{escape(SECTOR_LABELS.get(r.get("sector") or "other", "Other"))}</td><td class="n"><span class="lt">{escape(r["grade"])}</span></td><td class="n">{pct_points(r["percentile"])}</td><td class="w">{escape(home._flags(r))}</td></tr>')
    out.append(table(["Ticker", "Company", "Sector", ">Letter", ">Percentile", "Warnings"], rows))
    return chrome("Assay Companies", "\n".join(out), state["index"]["as_of"], page_link)


def sector_page(state: dict[str, Any], sector: str, page_link: str = "./") -> str:
    rows = sorted((r for r in state["graded"] if (r.get("sector") or "other") == sector), key=lambda r: r["percentile"])
    mcap, history = state["mcap"], state["history"]
    label = SECTOR_LABELS.get(sector, sector)
    out = [heading(label), hero(f"sector-{sector}", label), f'<p>{len(rows):,} graded companies. Letters are fifths of this list; lower percentile is stronger.</p>']
    scored = [r for r in rows if isinstance(r.get("composite"), (int, float))]
    largest = sorted((r for r in scored if mcap.get(r["ticker"])), key=lambda r: -mcap[r["ticker"]])[:6]
    named = [(r["ticker"], r["composite"], home._flags(r) or r["ticker"]) for r in largest]
    if scored:
        hits = [(r["composite"], f'{home._company_name(r.get("name") or "")} ({r["ticker"]})|{r["grade"]}, percentile {pct_points(r["percentile"])}', f'{page_link}{r["ticker"]}.html') for r in scored]
        out.append(density_chart([r["composite"] for r in scored], None, "", named, hits))
        out.append('<p class="caption">Composite score of every graded company in the sector, smoothed. Vertical rules are the letter band edges. Dots are the six largest by market equity.</p>')
    if history:
        moves = []
        for r in rows:
            points = [c for c in (history["tickers"].get(r["ticker"]) or {}).get("changes", []) if c[1]]
            if len(points) >= 2 and points[0][1][0] != points[-1][1][0]:
                moves.append((r, points[0][1], points[-1][1], points[-1][2] - points[0][2]))
        for title, group in (("Weaker this year", sorted((m for m in moves if m[3] > 0), key=lambda m: -m[3])[:5]), ("Stronger this year", sorted((m for m in moves if m[3] < 0), key=lambda m: m[3])[:5])):
            if group:
                out.append(f"<h2>{title}</h2>" + table(["Company", "Letters", ">Move"], [f'<tr><td>{cell(state, r, page_link)}</td><td>{escape(a)} to {escape(b)}</td><td class="n">{d * 100:+.0f}</td></tr>' for r, a, b, d in group]))
    out.append("<h2>Every Graded Company</h2>" + table([">Rank", "Company", ">Letter", ">Percentile", ">Market equity", ">12-1", "Warnings"], [
        f'<tr><td class="n">{i}</td><td>{cell(state, r, page_link)}</td><td class="n"><span class="lt">{escape(r["grade"])}</span></td><td class="n">{pct_points(r["percentile"])}</td><td class="n">{_money(mcap[r["ticker"]]) if mcap.get(r["ticker"]) else ""}</td><td class="n">{home._mom_text(r)}</td><td class="w">{escape(home._flags(r))}</td></tr>'
        for i, r in enumerate(rows, 1)]))
    return chrome(f"Assay {label}", "\n".join(out), state["index"]["as_of"], page_link, credits=True)


def sparkline(series: list[float], series_b: list[float] | None = None, ymax: float = 0.4, labels: list[str] | None = None) -> str:
    w, h = 170, 36
    n = max(len(series), 2)

    def path(values: list[float]) -> str:
        return "M" + "L".join(f"{4 + i * (w - 8) / (n - 1):.1f} {h - 4 - min(v, ymax) / ymax * (h - 8):.1f}" for i, v in enumerate(values))

    b = f'<path d="{path(series_b)}" fill="none" stroke="#000080" stroke-width="1" stroke-dasharray="2 2"/>' if series_b else ""
    hover = ""
    if labels:
        data = ";".join(f"{4 + i * (w - 8) / (n - 1):.1f}:{labels[i]}" for i in range(len(series)))
        hover = f' data-series="{escape(data)}"'
    guide = f'<line class="guide" x1="0" y1="2" x2="0" y2="{h - 4}" stroke="#000080" stroke-dasharray="2 2"/><rect x="0" y="0" width="{w}" height="{h}" fill="transparent"/>' if labels else ""
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true" style="vertical-align:middle"{hover}><line x1="4" y1="{h - 4}" x2="{w - 4}" y2="{h - 4}" stroke="#c8c8d8"/>{b}<path d="{path(series)}" fill="none" stroke="#000080" stroke-width="1.2"/>{guide}</svg>'


# ---------------------------------------------------------------- figures

def tiles(items: list[tuple[str, str]]) -> str:
    """A row of plain figures: value on top, label under. Used only where the figures are the point of the page."""
    return '<div class="tiles">' + "".join(f'<div class="tile"><div class="v">{escape(v)}</div><div class="l">{escape(k)}</div></div>' for k, v in items) + "</div>"


def hbars(items: list[tuple[str, float, str]], width: int = 640, label_width: int = 170, zero_center: bool = False) -> str:
    """Horizontal hairline bars: (label, value, printed value). Negative values run left when zero_center is set."""
    if not items:
        return ""
    row_h, top = 18, 4
    h = top + row_h * len(items) + 6
    values = [v for _, v, _ in items]
    span = max(abs(v) for v in values) or 1
    x0 = label_width + (width - label_width - 60) / 2 if zero_center else label_width
    scale = ((width - label_width - 60) / 2 if zero_center else (width - label_width - 60)) / span
    out = [f'<svg class="chart" viewBox="0 0 {width} {h}" aria-label="bar chart">']
    if zero_center:
        out.append(f'<line x1="{x0:.1f}" y1="{top}" x2="{x0:.1f}" y2="{h - 4}" stroke="#000080"/>')
    for i, (label, v, printed) in enumerate(items):
        y = top + i * row_h
        x = x0 + (min(v, 0) * scale if zero_center else 0)
        out.append(f'<text x="{label_width - 8}" y="{y + 12}" font-size="11" text-anchor="end">{escape(label)}</text>')
        out.append(f'<rect x="{x:.1f}" y="{y + 3}" width="{abs(v) * scale:.1f}" height="{row_h - 7}" fill="#000080" fill-opacity="0.85" data-tip="{escape(label)}: {escape(printed)}"/>')
        tx = x0 + (max(v, 0) * scale if zero_center else abs(v) * scale) + 5
        out.append(f'<text x="{tx:.1f}" y="{y + 12}" font-size="11">{escape(printed)}</text>')
    out.append("</svg>")
    return "".join(out)


def transition_grid(counts: dict[tuple[str, str], int], row_label: str, col_label: str, members: dict[tuple[str, str], list[str]] | None = None) -> str:
    """Five by five grid of letter moves, cells shaded by count; the diagonal is unchanged letters.
    Hovering a cell names its companies; clicking lists them under the grid."""
    letters = "ABCDE"
    cell, left, top = 58, 70, 40
    w, h = left + cell * 5 + 10, top + cell * 5 + 10
    peak = max(counts.values(), default=1) or 1
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" style="max-width:{w}px" aria-label="letter transition grid">']
    out.append(f'<text x="{left + cell * 2.5:.1f}" y="12" font-size="11" text-anchor="middle">{escape(col_label)}</text>')
    out.append(f'<text x="12" y="{top + cell * 2.5:.1f}" font-size="11" text-anchor="middle" transform="rotate(-90 12 {top + cell * 2.5:.1f})">{escape(row_label)}</text>')
    for j, c in enumerate(letters):
        out.append(f'<text x="{left + j * cell + cell / 2:.1f}" y="{top - 8}" font-size="12" font-weight="bold" text-anchor="middle">{c}</text>')
    for i, r in enumerate(letters):
        out.append(f'<text x="{left - 10}" y="{top + i * cell + cell / 2 + 4:.1f}" font-size="12" font-weight="bold" text-anchor="end">{r}</text>')
        for j, c in enumerate(letters):
            n = counts.get((r, c), 0)
            x, y = left + j * cell, top + i * cell
            alpha = 0.06 + 0.7 * (n / peak) if n else 0
            fill = f'fill="#000080" fill-opacity="{alpha:.2f}"' if n else 'fill="#ffffff"'
            color = "#ffffff" if alpha > 0.45 else "#000080"
            names = (members or {}).get((r, c), [])
            tip = f"{r} to {c}: {n} compan{'y' if n == 1 else 'ies'}" + ("|" + ", ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "") if names else "")
            attrs = f' class="hit" data-tip="{escape(tip)}" data-list="{escape(",".join(names))}" data-title="{r} to {c}"' if n else ""
            out.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" {fill} stroke="#c8c8d8"{attrs}/>')
            out.append(f'<text x="{x + cell / 2:.1f}" y="{y + cell / 2 + 4:.1f}" font-size="12" text-anchor="middle" fill="{color}" style="fill:{color};pointer-events:none">{n if n else ""}</text>')
    out.append("</svg>")
    return "".join(out)


def day_timeline(days: list[tuple[str, int, int]], today: str, lists: dict[str, dict[str, list[str]]] | None = None) -> str:
    """One column per day: filings due (above the line) and received (below), today marked.
    Hovering a day names its filers; clicking lists them under the chart."""
    if not days:
        return ""
    w, h, left, mid = 640, 150, 34, 82
    col = (w - left - 10) / len(days)
    peak_due = max((d for _, d, _ in days), default=1) or 1
    peak_rec = max((r for _, _, r in days), default=1) or 1
    scale_due, scale_rec = (mid - 24) / peak_due, (h - mid - 26) / peak_rec
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" aria-label="filings due and received by day">']
    out.append(f'<line x1="{left}" y1="{mid}" x2="{w - 10}" y2="{mid}" stroke="#000080"/>')
    out.append(f'<text x="{left - 6}" y="{mid - 30}" font-size="10" text-anchor="end">due</text><text x="{left - 6}" y="{mid + 34}" font-size="10" text-anchor="end">received</text>')
    for i, (day, due, rec) in enumerate(days):
        x = left + i * col
        if day == today:
            out.append(f'<rect x="{x:.1f}" y="6" width="{col:.1f}" height="{h - 26}" fill="#000080" fill-opacity="0.07"/>')
        if due:
            out.append(f'<rect x="{x + 1:.1f}" y="{mid - due * scale_due:.1f}" width="{max(col - 2, 1):.1f}" height="{due * scale_due:.1f}" fill="#000080" fill-opacity="0.85"/>')
            if due >= peak_due * 0.5:
                out.append(f'<text x="{x + col / 2:.1f}" y="{mid - due * scale_due - 3:.1f}" font-size="9" text-anchor="middle">{due}</text>')
        if rec:
            out.append(f'<rect x="{x + 1:.1f}" y="{mid + 1}" width="{max(col - 2, 1):.1f}" height="{rec * scale_rec:.1f}" fill="#000080" fill-opacity="0.35"/>')
            if rec >= peak_rec * 0.5:
                out.append(f'<text x="{x + col / 2:.1f}" y="{mid + rec * scale_rec + 11:.1f}" font-size="9" text-anchor="middle">{rec}</text>')
        d = date.fromisoformat(day)
        if d.weekday() == 0 or i == 0:
            out.append(f'<text x="{x + col / 2:.1f}" y="{h - 6}" font-size="9" text-anchor="middle">{d.strftime("%b %-d")}</text>')
        entry = (lists or {}).get(day) or {}
        due_names, rec_names = entry.get("due") or [], entry.get("received") or []
        tip = f"{long_date(day)}|due: {due}" + (" (" + ", ".join(due_names[:8]) + (", more" if len(due_names) > 8 else "") + ")" if due_names else "") + f"|received: {rec}" + (" (" + ", ".join(rec_names[:8]) + (", more" if len(rec_names) > 8 else "") + ")" if rec_names else "")
        out.append(f'<rect class="hit" x="{x:.1f}" y="4" width="{col:.1f}" height="{h - 22}" fill="transparent" style="pointer-events:all" data-tip="{escape(tip)}" data-list="{escape(",".join(due_names + rec_names))}" data-title="{escape(long_date(day))}"/>')
    out.append("</svg>")
    return "".join(out)


def quantile_strips(groups: list[tuple[str, list[float]]], transform, ticks: list[tuple[float, str]], width: int = 640, fmt=lambda v: f"{v:.2f}") -> str:
    """Per group: a line from the 10th to the 90th percentile, a box from the 25th to the 75th, a tick at the median."""
    if not groups:
        return ""
    left, row_h, top = 150, 26, 18
    h = top + row_h * len(groups) + 26
    all_t = [transform(v) for _, vs in groups for v in vs]
    lo, hi = min(all_t + [transform(t) for t, _ in ticks]), max(all_t + [transform(t) for t, _ in ticks])

    def x_at(v: float) -> float:
        return left + (transform(v) - lo) / (hi - lo) * (width - left - 20)

    out = [f'<svg class="chart" viewBox="0 0 {width} {h}" aria-label="distribution strips">']
    for t, label in ticks:
        out.append(f'<line x1="{x_at(t):.1f}" y1="{top - 6}" x2="{x_at(t):.1f}" y2="{h - 22}" stroke="#c8c8d8"/><text x="{x_at(t):.1f}" y="{h - 8}" font-size="10" text-anchor="middle">{escape(label)}</text>')
    for i, (label, vs) in enumerate(groups):
        y = top + i * row_h + row_h / 2
        q = statistics.quantiles(sorted(vs), n=20) if len(vs) >= 20 else None
        if not q:
            continue
        p10, p25, med, p75, p90 = q[1], q[4], statistics.median(vs), q[14], q[17]
        tip = f"{label}: {len(vs):,} companies|10th {fmt(p10)}, 25th {fmt(p25)}, median {fmt(med)}, 75th {fmt(p75)}, 90th {fmt(p90)}"
        out.append(f'<text x="{left - 10}" y="{y + 4:.1f}" font-size="11" text-anchor="end">{escape(label)}</text>')
        out.append(f'<rect x="{left}" y="{y - row_h / 2:.1f}" width="{width - left - 20}" height="{row_h}" fill="transparent" style="pointer-events:all" data-tip="{escape(tip)}"/>')
        out.append(f'<line x1="{x_at(p10):.1f}" y1="{y:.1f}" x2="{x_at(p90):.1f}" y2="{y:.1f}" stroke="#000080" style="pointer-events:none"/>')
        out.append(f'<rect x="{x_at(p25):.1f}" y="{y - 6:.1f}" width="{max(x_at(p75) - x_at(p25), 1):.1f}" height="12" fill="#000080" fill-opacity="0.18" stroke="#000080" style="pointer-events:none"/>')
        out.append(f'<line x1="{x_at(med):.1f}" y1="{y - 8:.1f}" x2="{x_at(med):.1f}" y2="{y + 8:.1f}" stroke="#000080" stroke-width="2"/>')
    out.append("</svg>")
    return "".join(out)


def stacked_shares(rows: list[tuple[str, dict[str, int]]], width: int = 640) -> str:
    """One bar per row split into A to E shares, letters printed inside when the segment is wide enough."""
    if not rows:
        return ""
    left, row_h, top = 170, 24, 6
    h = top + row_h * len(rows) + 4
    out = [f'<svg class="chart" viewBox="0 0 {width} {h}" aria-label="letter shares">']
    alphas = {"A": 0.15, "B": 0.3, "C": 0.45, "D": 0.62, "E": 0.85}
    for i, (label, counts) in enumerate(rows):
        total = sum(counts.values()) or 1
        y = top + i * row_h
        x = left
        out.append(f'<text x="{left - 8}" y="{y + 15}" font-size="11" text-anchor="end">{escape(label)}</text>')
        for g in "ABCDE":
            wseg = counts.get(g, 0) / total * (width - left - 10)
            if wseg <= 0:
                continue
            out.append(f'<rect x="{x:.1f}" y="{y + 3}" width="{wseg:.1f}" height="{row_h - 6}" fill="#000080" fill-opacity="{alphas[g]}" stroke="#ffffff" data-tip="{escape(label)}: {g} {counts.get(g, 0):,} of {total:,} ({counts.get(g, 0) / total * 100:.0f}%)"/>')
            if wseg > 26:
                color = "#ffffff" if alphas[g] > 0.5 else "#000080"
                out.append(f'<text x="{x + wseg / 2:.1f}" y="{y + 15}" font-size="10" text-anchor="middle" style="fill:{color};pointer-events:none">{g} {counts.get(g, 0) / total * 100:.0f}%</text>')
            x += wseg
    out.append("</svg>")
    return "".join(out)


def dot_plot(items: list[tuple[str, float, float, float]], lo: float, hi: float, width: int = 640) -> str:
    """Label, value, low, high: a dot with a whisker, on a fixed scale."""
    left, row_h, top = 170, 22, 18
    h = top + row_h * len(items) + 24

    def x_at(v: float) -> float:
        return left + (v - lo) / (hi - lo) * (width - left - 20)

    out = [f'<svg class="chart" viewBox="0 0 {width} {h}" aria-label="dot plot">']
    for t in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        if lo <= t <= hi:
            out.append(f'<line x1="{x_at(t):.1f}" y1="{top - 6}" x2="{x_at(t):.1f}" y2="{h - 20}" stroke="#c8c8d8"/><text x="{x_at(t):.1f}" y="{h - 6}" font-size="10" text-anchor="middle">{t:.1f}</text>')
    for i, (label, v, a, b) in enumerate(items):
        y = top + i * row_h + row_h / 2
        out.append(f'<text x="{left - 10}" y="{y + 4:.1f}" font-size="11" text-anchor="end">{escape(label)}</text>')
        out.append(f'<line x1="{x_at(a):.1f}" y1="{y:.1f}" x2="{x_at(b):.1f}" y2="{y:.1f}" stroke="#000080"/>')
        out.append(f'<circle cx="{x_at(v):.1f}" cy="{y:.1f}" r="4" fill="#000080" data-tip="{escape(label)}: AUC {v:.3f}, 95% interval {a:.3f} to {b:.3f}"/><text x="{x_at(b) + 6:.1f}" y="{y + 4:.1f}" font-size="10">{v:.3f}</text>')
    out.append("</svg>")
    return "".join(out)


def process_strip(steps: list[tuple[str, str]], width: int = 640) -> str:
    n = len(steps)
    box_w, gap, h = (width - 10 * (n - 1)) / n, 10, 74
    out = [f'<svg class="chart" viewBox="0 0 {width} {h}" aria-label="how a grade is made">']
    for i, (title, sub) in enumerate(steps):
        x = i * (box_w + gap)
        out.append(f'<rect x="{x:.1f}" y="4" width="{box_w:.1f}" height="{h - 8}" fill="#ffffff" stroke="#000080"/>')
        out.append(f'<text x="{x + box_w / 2:.1f}" y="28" font-size="12" font-weight="bold" text-anchor="middle">{escape(title)}</text>')
        for k, line in enumerate(sub.split("|")):
            out.append(f'<text x="{x + box_w / 2:.1f}" y="{44 + k * 12}" font-size="10" text-anchor="middle">{escape(line)}</text>')
        if i < n - 1:
            out.append(f'<line x1="{x + box_w:.1f}" y1="{h / 2}" x2="{x + box_w + gap:.1f}" y2="{h / 2}" stroke="#000080"/>')
    out.append("</svg>")
    return "".join(out)


def _letter(grade: str | None) -> str | None:
    return grade[0] if grade else None


# ---------------------------------------------------------------- screens

def sectors_page(state: dict[str, Any], hist: dict[str, dict[str, dict[str, Any]]], page_link: str = "./") -> str:
    out = [heading("Sectors")]
    by_sector: dict[str, list[dict[str, Any]]] = {}
    for r in state["graded"]:
        by_sector.setdefault(r.get("sector") or "other", []).append(r)
    ordered = sorted(by_sector.items(), key=lambda kv: -len(kv[1]))
    out.append(f"<p>{len(by_sector)} sectors from the four-digit SIC code, {len(state['graded']):,} graded companies. Each letter is one fifth of its sector, so the sectors differ in who holds the letters, in size, and in how their prices moved.</p>")
    moms = []
    for sector, rs in ordered:
        ms = [m for m in (home._momentum(r) for r in rs) if m is not None]
        moms.append((SECTOR_LABELS.get(sector, sector), statistics.median(ms) if ms else 0.0, f"{statistics.median(ms) * 100:+.0f}%" if ms else "n/a"))
    out.append("<h2>Median 12-1 Momentum by Sector</h2>" + hbars(sorted(moms, key=lambda m: -m[1]), zero_center=True))
    out.append('<p class="caption">Median twelve-month price change, skipping the latest month, across the graded companies of each sector.</p>')
    rows = []
    for sector, rs in ordered:
        counts = {g: sum(1 for r in rs if r["grade"][0] == g) for g in "ABCDE"}
        best = min(rs, key=lambda r: r.get("percentile") or 1)
        thumb = animals.photo(f"sector-{sector}", 240)
        pic = f'<a href="{page_link}sector-{escape(sector)}.html"><img src="{thumb}" alt="" style="width:72px;height:48px;object-fit:cover;border:1px solid #000080;vertical-align:middle"></a>' if thumb else ""
        big = sum(1 for r in rs if state["mcap"].get(r["ticker"], 0) >= 1e10)
        rows.append(f'<tr><td>{pic}</td><td><a href="{page_link}sector-{escape(sector)}.html">{escape(SECTOR_LABELS.get(sector, sector))}</a></td><td class="n">{len(rs)}</td><td class="n">{big}</td>' + "".join(f'<td class="n">{counts[g]}</td>' for g in "ABCDE") + f'<td>{cell(state, best, page_link)}</td></tr>')
    out.append("<h2>Every Sector</h2>" + table(["", "Sector", ">Graded", ">Over $10B", ">A", ">B", ">C", ">D", ">E", "Best ranked"], rows))
    dates = sorted(hist)
    if len(dates) >= 2:
        out.append(f"<h2>Barometer</h2><p>Share of each sector graded E (solid) and A (dashed) at each of the {len(dates)} rescans from {escape(long_date(dates[0]))} to {escape(long_date(dates[-1]))}. Letters are fifths on each date, so the shares move only through boundary letters; which companies hold them is on the sector pages.</p>")
        rows = []
        for sector, _ in ordered:
            e_share, a_share = [], []
            for d in dates:
                rs = [t for t in hist[d].values() if t.get("sector") == sector and t.get("grade")]
                e_share.append(sum(1 for t in rs if t["grade"][0] == "E") / len(rs) if rs else 0)
                a_share.append(sum(1 for t in rs if t["grade"][0] == "A") / len(rs) if rs else 0)
            labels = [f"{long_date(d)}|E {e * 100:.0f}%, A {a * 100:.0f}%" for d, e, a in zip(dates, e_share, a_share)]
            rows.append(f'<tr><td>{escape(SECTOR_LABELS.get(sector, sector))}</td><td>{sparkline(e_share, a_share, labels=labels)}</td><td class="n">{e_share[0] * 100:.0f}% to {e_share[-1] * 100:.0f}%</td><td class="n">{a_share[0] * 100:.0f}% to {a_share[-1] * 100:.0f}%</td></tr>')
        out.append(table(["Sector", "E solid, A dashed", ">Share E, first to last", ">Share A, first to last"], rows, wide=False))
    return chrome("Assay Sectors", "\n".join(out), state["index"]["as_of"], page_link, credits=True)


def calendar_page(state: dict[str, Any], calendar: dict[str, Any] | None, page_link: str = "./") -> str:
    as_of = state["index"]["as_of"]
    out = [heading("Filings Calendar")]
    if not calendar:
        out.append("<p>No calendar file was supplied for this build.</p>")
        return chrome("Assay Calendar", "\n".join(out), as_of, page_link)
    by_ticker = {r["ticker"]: r for r in state["rows"]}
    records = [(t, rec) for t, rec in calendar["tickers"].items() if t in by_ticker]
    counts = {"received": 0, "due": 0, "overdue": 0, "unknown": 0}
    for _, rec in records:
        counts[rec.get("status", "unknown")] = counts.get(rec.get("status", "unknown"), 0) + 1
    received_week = sum(len(rec.get("received") or []) for _, rec in records)
    out.append(tiles([("received, latest period", f"{counts['received']:,}"), ("not yet due", f"{counts['due']:,}"), ("overdue", f"{counts['overdue']:,}"), ("received in the last 7 days", f"{received_week:,}")]))
    today = date.fromisoformat(as_of)
    days = [(today + timedelta(days=i)).isoformat() for i in range(-7, 31)]
    due_by_day: dict[str, int] = {d: 0 for d in days}
    rec_by_day: dict[str, int] = {d: 0 for d in days}
    lists: dict[str, dict[str, list[str]]] = {d: {"due": [], "received": []} for d in days}
    for t, rec in records:
        if rec.get("status") == "due" and rec.get("due") in due_by_day:
            due_by_day[rec["due"]] += 1
            lists[rec["due"]]["due"].append(t)
        nxt = rec.get("next") or {}
        if nxt.get("due") in due_by_day and rec.get("status") == "received":
            due_by_day[nxt["due"]] += 1
            lists[nxt["due"]]["due"].append(t)
        for f in rec.get("received") or []:
            if f.get("filed") in rec_by_day:
                rec_by_day[f["filed"]] += 1
                lists[f["filed"]]["received"].append(t)
    out.append(day_timeline([(d, due_by_day[d], rec_by_day[d]) for d in days], as_of, lists))
    out.append(f'<p class="caption">Seven days back and thirty ahead of {escape(long_date(as_of))}. Above the line, periodic reports falling due that day; below, reports received. Today is shaded. Hover a day for names, click to list them.</p>')
    out.append("<p>Due dates from fiscal year end and filer category under Exchange Act Rules 13a-1 and 13a-13: a 10-K within 60, 75 or 90 days for large accelerated, accelerated and other filers; a 10-Q within 40 or 45 days. A filing counts for a period when its report date lies within ten days of the period end.</p>")
    overdue = sorted(((t, rec) for t, rec in records if rec.get("status") == "overdue"), key=lambda tr: -tr[1].get("days", 0))
    out.append(f"<h2>Overdue</h2><p class=\"small\">{len(overdue)} filers past their deadline without the report on file.</p>" + table(["Company", "Form", "Period end", "Due", ">Days late", "Class", ">Letter"], [
        f'<tr><td>{cell(state, by_ticker[t], page_link)}</td><td>{escape(rec["form"])}</td><td class="date">{escape(rec["period_end"])}</td><td class="date">{escape(rec["due"])}</td><td class="n">{rec["days"]}</td><td>{escape(rec["class"])}</td><td class="n">{escape(by_ticker[t].get("grade") or "")}</td></tr>'
        for t, rec in overdue]))
    due = sorted(((t, rec) for t, rec in records if rec.get("status") == "due" and rec.get("days", 99) <= 14), key=lambda tr: (tr[1]["due"], tr[0]))
    out.append(f"<h2>Due in the Next Fourteen Days</h2><p class=\"small\">{len(due)} filers.</p>" + table(["Company", "Form", "Period end", "Due", ">Days", "Class", ">Letter"], [
        f'<tr><td>{cell(state, by_ticker[t], page_link)}</td><td>{escape(rec["form"])}</td><td class="date">{escape(rec["period_end"])}</td><td class="date">{escape(rec["due"])}</td><td class="n">{rec["days"]}</td><td>{escape(rec["class"])}</td><td class="n">{escape(by_ticker[t].get("grade") or "")}</td></tr>'
        for t, rec in due]))
    received = sorted(((f["filed"], t, f) for t, rec in records for f in (rec.get("received") or [])), key=lambda x: (x[0], x[1]), reverse=True)
    out.append(f"<h2>Received in the Last Seven Days</h2><p class=\"small\">{len(received)} periodic reports.</p>" + table(["Filed", "Company", "Form", "Period end", ">Letter"], [
        f'<tr><td class="date">{escape(fd)}</td><td>{cell(state, by_ticker[t], page_link)}</td><td>{escape(f["form"])}</td><td class="date">{escape(f["period_end"])}</td><td class="n">{escape(by_ticker[t].get("grade") or "")}</td></tr>'
        for fd, t, f in received]))
    return chrome("Assay Calendar", "\n".join(out), as_of, page_link)


def last_night_page(state: dict[str, Any], hist: dict[str, dict[str, dict[str, Any]]], page_link: str = "./") -> str:
    as_of = state["index"]["as_of"]
    history = state["history"] or {"tickers": {}}
    by_ticker = {r["ticker"]: r for r in state["rows"]}
    out = [heading(f"Last Rescan, {long_date(as_of)}")]
    previous = max((d for d in hist if d < as_of), default=None)
    weaker, stronger, gained, lost = [], [], [], []
    matrix: dict[tuple[str, str], int] = {}
    members: dict[tuple[str, str], list[str]] = {}
    for ticker, entry in history["tickers"].items():
        changes = entry.get("changes") or []
        if not changes or changes[-1][0] != as_of or len(changes) < 2 or ticker not in by_ticker:
            continue
        old, new = changes[-2], changes[-1]
        if old[1] and new[1]:
            if old[1][0] == new[1][0]:
                continue
            matrix[(old[1][0], new[1][0])] = matrix.get((old[1][0], new[1][0]), 0) + 1
            members.setdefault((old[1][0], new[1][0]), []).append(ticker)
            (weaker if new[2] > old[2] else stronger).append((by_ticker[ticker], old[1], new[1], new[2] - old[2]))
        elif new[1] and not old[1]:
            gained.append((by_ticker[ticker], "none", new[1], 0.0))
        elif old[1] and not new[1]:
            lost.append((by_ticker[ticker], old[1], "none", 0.0))
    unchanged = sum(1 for r in state["graded"]) - len(weaker) - len(stronger) - len(gained)
    out.append(tiles([("letters weaker", str(len(weaker))), ("letters stronger", str(len(stronger))), ("letters gained", str(len(gained))), ("letters lost", str(len(lost))), ("unchanged", f"{unchanged:,}")]))
    if matrix:
        out.append('<div class="cols even"><div>' + transition_grid(matrix, "letter before", "letter after", members) + '<p class="caption">Every letter change at this rescan, before against after. Darker cells hold more companies; hover a cell for names, click to list them.</p></div><div>')
        out.append(f'<p>Changes recorded at the rescan of {escape(long_date(as_of))}' + (f", against the previous rescan of {escape(long_date(previous))}" if previous else "") + ". A letter moves when a company's own filings arrive, when peers' filings shift the sector ranking, or when the sampling error crosses a band edge and the boundary letter switches.</p></div></div>")
    for title, group, key in (("Weaker", weaker, lambda x: -x[3]), ("Stronger", stronger, lambda x: x[3]), ("Letter gained", gained, lambda x: x[0]["ticker"]), ("Letter lost", lost, lambda x: x[0]["ticker"])):
        group.sort(key=key)
        out.append(f"<h2>{title}</h2><p class=\"small\">{len(group)} companies" + ("; the first 60 are listed." if len(group) > 60 else ".") + "</p>" + table(["Company", "From", "To", ">Move", ">Market equity"], [
            f'<tr><td>{cell(state, r, page_link)}</td><td>{escape(a)}</td><td>{escape(b)}</td><td class="n">{d * 100:+.0f}</td><td class="n">{_money(state["mcap"][r["ticker"]]) if state["mcap"].get(r["ticker"]) else ""}</td></tr>'
            for r, a, b, d in group[:60]]))
    week = (date.fromisoformat(as_of) - timedelta(days=7)).isoformat()
    filings = sorted(((state["detail"][t]["gap"].get("last_filing_date"), t) for t in by_ticker if t in state["detail"] and (state["detail"][t]["gap"].get("last_filing_date") or "") >= week), reverse=True)
    out.append(f"<h2>Periodic Filings Received in the Last Seven Days</h2><p class=\"small\">{len(filings)} eligible filers.</p>" + table(["Received", "Company", "Form", ">Letter", ">Days before rescan"], [
        f'<tr><td class="date">{escape(fd)}</td><td>{cell(state, by_ticker[t], page_link)}</td><td><a href="{escape(state["detail"][t]["gap"].get("last_filing_url") or "#")}">{escape(state["detail"][t]["gap"].get("last_form") or "")}</a></td><td class="n">{escape(by_ticker[t].get("grade") or "")}</td><td class="n">{state["detail"][t]["gap"].get("days")}</td></tr>'
        for fd, t in filings]))
    if previous:
        then = {t for t, v in hist[previous].items() if v.get("status") == "eligible"}
        now = set(by_ticker)
        entered, left = sorted(now - then), sorted(then - now)
        out.append(f"<h2>Universe</h2><p>Since {escape(long_date(previous))}: {len(entered)} tickers entered the eligible universe, {len(left)} left.</p>")
        if entered:
            out.append('<p class="small">Entered: ' + ", ".join(f'<a href="{page_link}{escape(t)}.html">{escape(t)}</a>' for t in entered[:80]) + ("." if len(entered) <= 80 else f" and {len(entered) - 80} more.") + "</p>")
        if left:
            out.append('<p class="small">Left: ' + ", ".join(escape(t) for t in left[:80]) + ("." if len(left) <= 80 else f" and {len(left) - 80} more.") + "</p>")
    return chrome("Assay Last Rescan", "\n".join(out), as_of, page_link)


def warnings_page(state: dict[str, Any], page_link: str = "./") -> str:
    out = [heading("Warning Screens")]
    summary = state["index"].get("summary") or {}
    flags = summary.get("active_flags") or {}
    eligible = len(state["rows"]) or 1
    out.append(f"<p>Seven checks run on every eligible filer, {eligible:,} today. A warning is a measured value past a stated threshold. Warnings never enter the letter; fortress and thin data are informational.</p>")
    out.append(hbars([(label, flags.get(key, 0), f"{flags.get(key, 0):,} ({flags.get(key, 0) / eligible * 100:.0f}%)") for key, label, _ in CHECKS]))
    out.append('<p class="caption">Eligible filers triggering each check, with the share of the universe.</p>')
    out.append("<p>" + "; ".join(f'<a href="#{k}">{escape(label)}</a>' for k, label, _ in CHECKS) + ".</p>")
    for key, label, rule in CHECKS:
        rows = [r for r in state["rows"] if key in (r.get("active_flags") or [])]
        rows.sort(key=lambda r: -state["mcap"].get(r["ticker"], 0))
        graded = [r for r in rows if r.get("grade")]
        letters = {g: sum(1 for r in graded if r["grade"][0] == g) for g in "ABCDE"}
        out.append(f'<h2 id="{key}">{escape(label)}</h2><p class="small">Rule: {escape(rule)}. {len(rows):,} triggered; among the {len(graded):,} with a letter: ' + ", ".join(f"{g} {letters[g]}" for g in "ABCDE") + (". The largest 150 by market equity are listed." if len(rows) > 150 else ".") + "</p>")
        if key == "thin_data":
            continue
        out.append(table(["Company", "Measured", ">Letter", ">Percentile", ">Market equity"], [
            f'<tr><td>{cell(state, r, page_link)}</td><td class="w">{escape(state["detail"].get(r["ticker"], {}).get("flags", {}).get(key, ""))}</td><td class="n">{escape(r.get("grade") or "none")}</td><td class="n">{pct_points(r.get("percentile"))}</td><td class="n">{_money(state["mcap"][r["ticker"]]) if state["mcap"].get(r["ticker"]) else ""}</td></tr>'
            for r in rows[:150]]))
    return chrome("Assay Warnings", "\n".join(out), state["index"]["as_of"], page_link)


def scatter_chart(points: list[tuple[float, float, str, str]]) -> str:
    w, h, left, right, top, bottom = 640, 320, 44, 16, 14, 30

    def tx(m: float) -> float:
        return math.copysign(math.log10(1 + 10 * abs(m)), m)

    xs = [tx(m) for m, _, _, _ in points]
    lo, hi = min(xs + [-1.1]), max(xs + [2.0])

    def x_at(v: float) -> float:
        return left + (v - lo) / (hi - lo) * (w - left - right)

    def y_at(p: float) -> float:
        return top + p * (h - top - bottom)

    dots = "".join(f'<circle class="hit" cx="{x_at(tx(m)):.1f}" cy="{y_at(p):.1f}" r="1.7" fill="#000080" fill-opacity="0.35" stroke="transparent" stroke-width="5" style="pointer-events:all" data-tip="{escape(tip)}" data-href="{escape(href)}"/>' for m, p, tip, href in points)
    ticks = "".join(f'<line x1="{x_at(tx(m)):.1f}" y1="{h - bottom}" x2="{x_at(tx(m)):.1f}" y2="{h - bottom + 5}" stroke="#000080"/><text x="{x_at(tx(m)):.1f}" y="{h - bottom + 16}" font-size="10" text-anchor="middle">{label}</text>' for m, label in ((-0.5, "-50%"), (0, "0"), (1, "+100%"), (5, "+500%"), (50, "+5000%")) if lo <= tx(m) <= hi)
    bands = "".join(f'<line x1="{left}" y1="{y_at(q):.1f}" x2="{w - right}" y2="{y_at(q):.1f}" stroke="#c8c8d8"/>' for q in (0.2, 0.4, 0.6, 0.8))
    letters = "".join(f'<text x="{left - 8}" y="{y_at(q + 0.1) + 4:.1f}" font-size="11" font-weight="bold" text-anchor="end">{g}</text>' for q, g in zip((0, 0.2, 0.4, 0.6, 0.8), "ABCDE"))
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" aria-label="Twelve-month momentum against financial-condition percentile for every graded company">'
            f'{bands}{letters}<line x1="{left}" y1="{h - bottom}" x2="{w - right}" y2="{h - bottom}" stroke="#000080"/><line x1="{x_at(0):.1f}" y1="{top}" x2="{x_at(0):.1f}" y2="{h - bottom}" stroke="#c8c8d8"/>{ticks}{dots}'
            f'<text x="{left}" y="{h - 4}" font-size="10">colder</text><text x="{w - right}" y="{h - 4}" font-size="10" text-anchor="end">hotter</text></svg>')


def price_condition_page(state: dict[str, Any], page_link: str = "./") -> str:
    out = [heading("Price and Condition")]
    rows = [r for r in state["graded"] if home._momentum(r) is not None]

    def stats(rs: list[dict[str, Any]]) -> tuple[int, float, float, float]:
        ms = [home._momentum(r) for r in rs]
        return len(ms), statistics.median(ms), sum(m > 1 for m in ms) / len(ms), sum(m < -0.5 for m in ms) / len(ms)

    n_a, med_a, up_a, down_a = stats([r for r in rows if r["grade"][0] == "A"])
    n_e, med_e, up_e, down_e = stats([r for r in rows if r["grade"][0] == "E"])
    out.append(tiles([("median 12-1, graded A", f"{med_a * 100:+.0f}%"), ("median 12-1, graded E", f"{med_e * 100:+.0f}%"), ("doubled, A", f"{up_a * 100:.0f}%"), ("doubled, E", f"{up_e * 100:.0f}%"), ("lost half, A", f"{down_a * 100:.0f}%"), ("lost half, E", f"{down_e * 100:.0f}%")]))
    out.append(f"<p>{len(rows):,} graded companies with twelve months of price history. The letter is a rank of reported financial condition and no price enters it; this page measures how price moved next to it. Doubling is as common in every letter; losing half is not.</p>")
    tx = lambda m: math.copysign(math.log10(1 + 10 * abs(m)), m)
    out.append("<h2>Twelve-Month Price Change by Letter</h2>" + quantile_strips([(f"{g}, the {LETTER_NAMES[g]}", [home._momentum(r) for r in rows if r["grade"][0] == g]) for g in "ABCDE"], tx, [(-0.5, "-50%"), (0, "0"), (1, "+100%"), (5, "+500%")], fmt=lambda v: f"{v * 100:+.0f}%"))
    out.append('<p class="caption">Line from the 10th to the 90th percentile of price change, box from the 25th to the 75th, tick at the median. Compressed scale.</p>')
    out.append("<h2>Every Company</h2>" + scatter_chart([(home._momentum(r), r["percentile"], f'{home._company_name(r.get("name") or "")} ({r["ticker"]})|{r["grade"]}, percentile {pct_points(r["percentile"])}|12-1 {home._mom_text(r)}', f'{page_link}{r["ticker"]}.html') for r in rows]))
    out.append('<p class="caption">Each dot is a company: price change across, condition percentile down, strongest at the top. Horizontal rules are the letter band edges.</p>')
    by_letter = [f'<tr><td>{g}, the {LETTER_NAMES[g]}</td><td class="n">{n:,}</td><td class="n">{med * 100:+.1f}%</td><td class="n">{up * 100:.1f}%</td><td class="n">{down * 100:.1f}%</td></tr>'
                 for g in "ABCDE" for n, med, up, down in [stats([r for r in rows if r["grade"][0] == g])] if n]
    out.append("<h2>By Letter</h2>" + table(["Letter", ">Companies", ">Median 12-1", ">More than doubled", ">Lost more than half"], by_letter, wide=False))
    by_sector: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_sector.setdefault(r.get("sector") or "other", []).append(r)
    sector_rows = []
    for sector, rs in sorted(by_sector.items(), key=lambda kv: -len(kv[1])):
        n, med, up, down = stats(rs)
        sector_rows.append(f'<tr><td><a href="{page_link}sector-{escape(sector)}.html">{escape(SECTOR_LABELS.get(sector, sector))}</a></td><td class="n">{n:,}</td><td class="n">{med * 100:+.1f}%</td><td class="n">{up * 100:.1f}%</td><td class="n">{down * 100:.1f}%</td></tr>')
    out.append("<h2>By Sector</h2>" + table(["Sector", ">Companies", ">Median 12-1", ">More than doubled", ">Lost more than half"], sector_rows, wide=False))
    hot = sorted((r for r in rows if r["grade"][0] == "E" and state["mcap"].get(r["ticker"], 0) > 1e9), key=lambda r: -home._momentum(r))[:15]
    cold = sorted((r for r in rows if r["grade"][0] == "A" and state["mcap"].get(r["ticker"], 0) > 1e9), key=lambda r: home._momentum(r))[:15]
    for title, group in (("Graded E, Hottest, Over $1B", hot), ("Graded A, Coldest, Over $1B", cold)):
        out.append(f"<h2>{title}</h2>" + table(["Company", ">Letter", ">Percentile", ">12-1", ">Market equity", "Warnings"], [
            f'<tr><td>{cell(state, r, page_link)}</td><td class="n">{escape(r["grade"])}</td><td class="n">{pct_points(r["percentile"])}</td><td class="n">{home._mom_text(r)}</td><td class="n">{_money(state["mcap"][r["ticker"]])}</td><td class="w">{escape(home._flags(r))}</td></tr>'
            for r in group]))
    return chrome("Assay Price and Condition", "\n".join(out), state["index"]["as_of"], page_link)


def then_now_page(state: dict[str, Any], hist: dict[str, dict[str, dict[str, Any]]], page_link: str = "./") -> str:
    as_of = state["index"]["as_of"]
    out = [heading("Then and Now")]
    dates = sorted(d for d in hist if d < as_of)
    if not dates:
        out.append("<p>No earlier rescan is on file.</p>")
        return chrome("Assay Then and Now", "\n".join(out), as_of, page_link)
    target = (date.fromisoformat(as_of) - timedelta(days=365)).isoformat()
    first = min(dates, key=lambda d: abs((date.fromisoformat(d) - date.fromisoformat(target)).days))
    then = hist[first]
    history = state["history"] or {"tickers": {}}
    matrix: dict[tuple[str, str], int] = {}
    members: dict[tuple[str, str], list[str]] = {}
    same = moved2 = both = 0
    for r in state["graded"]:
        t = then.get(r["ticker"])
        if t and t.get("grade"):
            both += 1
            a, b = t["grade"][0], r["grade"][0]
            matrix[(a, b)] = matrix.get((a, b), 0) + 1
            members.setdefault((a, b), []).append(r["ticker"])
            same += a == b
            moved2 += abs("ABCDE".index(a) - "ABCDE".index(b)) >= 2
    out.append(tiles([("graded on both dates", f"{both:,}"), ("same letter", f"{same / both * 100:.0f}%" if both else "n/a"), ("moved one letter", f"{(both - same - moved2) / both * 100:.0f}%" if both else "n/a"), ("moved two or more", f"{moved2 / both * 100:.0f}%" if both else "n/a")]))
    out.append('<div class="cols even"><div>' + transition_grid(matrix, f"letter on {long_date(first)}", f"letter on {long_date(as_of)}", members) + '<p class="caption">Hover a cell for names, click to list them.</p></div><div>')
    out.append(f"<p>{escape(long_date(first))} against {escape(long_date(as_of))}, each rescan using only facts filed by its own date. The diagonal holds companies whose letter did not change; cells far from it are the year's real moves. Letters are fifths of a sector, so a company moves when its own filings change or when the sector around it does. Rescans on file run from {escape(long_date(dates[0]))}.</p></div></div>")
    largest = home.largest(state, 100)
    rows = []
    for i, r in enumerate(largest, 1):
        t = then.get(r["ticker"]) or {}
        changes = [c for c in (history["tickers"].get(r["ticker"]) or {}).get("changes", []) if c[1]]
        before = [c for c in changes if c[0] <= first]
        path = ", ".join(([before[-1][1]] if before else []) + [c[1] for c in changes if first < c[0] <= as_of])
        rows.append(f'<tr><td class="n">{i}</td><td>{cell(state, r, page_link)}</td><td class="n">{escape(t.get("grade") or "none")}</td><td class="n">{pct_points(t.get("percentile"))}</td><td class="n"><span class="lt">{escape(r["grade"])}</span></td><td class="n">{pct_points(r["percentile"])}</td><td>{escape(path)}</td><td class="n">{_money(state["mcap"][r["ticker"]])}</td></tr>')
    out.append("<h2>The Largest Hundred</h2>" + table([">#", "Company", ">Then", ">Percentile", ">Now", ">Percentile", "Letters held since then", ">Market equity"], rows))
    return chrome("Assay Then and Now", "\n".join(out), as_of, page_link)


def size_bands_page(state: dict[str, Any], page_link: str = "./") -> str:
    out = [heading("Size Bands")]
    rel = load_reliability()
    labels = {s["id"]: s["label"] for s in rel["strata"]}
    out.append("<p>Every eligible filer is placed in one of five bands by total assets, with cut points fixed on the 2012 to 2019 training window. The failure model is fit and tested within each band, and the band sets the peer failure rate shown on a company page. Within a sector the letter tracks size, and this page shows by how much.</p>")
    shares, rows = [], []
    for band in ("Q1", "Q2", "Q3", "Q4", "Q5", None):
        rs = [r for r in state["rows"] if r.get("stratum") == band]
        if not rs:
            continue
        graded = [r for r in rs if r.get("grade")]
        inputs = [state["detail"][r["ticker"]]["inputs"] for r in rs if r["ticker"] in state["detail"] and isinstance(state["detail"][r["ticker"]].get("inputs"), int)]
        counts = {g: sum(1 for r in graded if r["grade"][0] == g) for g in "ABCDE"}
        if graded and band:
            shares.append((labels.get(band, band), counts))
        rows.append(f'<tr><td>{escape(labels.get(band, "unassigned"))}</td><td class="n">{len(rs):,}</td><td class="n">{len(graded):,}</td><td class="n">{statistics.median(inputs) if inputs else ""}</td>' + "".join(f'<td class="n">{counts[g] / len(graded) * 100:.0f}%' if graded else "<td class=\"n\">" for g in "ABCDE") + "</td></tr>")
    out.append("<h2>Letters by Band</h2>" + stacked_shares(shares))
    out.append('<p class="caption">Share of each letter among the graded companies of each band, smallest assets at the top.</p>')
    out.append(table(["Band, total assets", ">Eligible", ">Graded", ">Median inputs of 14", ">A", ">B", ">C", ">D", ">E"], rows, wide=False))
    out.append("<h2>Reliability by Band</h2>" + dot_plot([(s["label"], s["auc"], s["ci"][0], s["ci"][1]) for s in rel["strata"]], 0.6, 1.0))
    out.append('<p class="caption">Out-of-sample AUC of the failure model within each band, with its 95% interval; filings 2020 to 2025.</p>' + reliability_table(rel))
    inside = sum(1 for r in state["rows"] if r.get("standard_universe") is True)
    outside = sum(1 for r in state["rows"] if r.get("standard_universe") is False)
    out.append(f"<h2>Standard Universe</h2><p>{escape(rel['standard_universe']['rule'])}. Today {inside:,} eligible filers are inside and {outside:,} outside. Membership is reported on every company page and never required for a letter.</p>")
    return chrome("Assay Size Bands", "\n".join(out), state["index"]["as_of"], page_link)


def ceased_page(state: dict[str, Any], page_link: str = "./") -> str:
    out = [heading("Ceased and Stale")]
    detail = state["detail"]
    gaps = [r.get("filing_gap_days") for r in state["rows"] if isinstance(r.get("filing_gap_days"), int)]
    bins = [("0 to 45", 0, 45), ("46 to 90", 46, 90), ("91 to 180", 91, 180), ("181 to 365", 181, 365), ("over 365", 366, 10**9)]
    counts = [(label, sum(1 for g in gaps if lo <= g <= hi)) for label, lo, hi in bins]
    ceased = sorted((r for r in state["rows"] if r.get("ceased")), key=lambda r: -(r.get("filing_gap_days") or 0))
    stale = sorted((r for r in state["rows"] if (r.get("filing_gap_days") or 0) > 180 and not r.get("ceased")), key=lambda r: -(r.get("filing_gap_days") or 0))
    late = sorted((r for r in state["rows"] if "late_filer" in (r.get("active_flags") or [])), key=lambda r: -state["mcap"].get(r["ticker"], 0))
    out.append(tiles([("ceased, over 365 days", str(len(ceased))), ("stale, 181 to 365 days", str(len(stale))), ("late filers", f"{len(late):,}"), ("median days since last report", f"{int(statistics.median(gaps))}" if gaps else "n/a")]))
    out.append("<h2>Days Since the Last Periodic Report</h2>" + hbars([(label, n, f"{n:,}") for label, n in counts]))
    out.append('<p class="caption">Eligible filers by the age of their latest 10-K, 10-Q, 20-F or 40-F. Stale begins past 180 days, ceased past 365.</p>')
    out.append("<p>Ceased: no periodic report received for more than 365 days while the ticker remains in the SEC file. Stale: the last periodic filing is more than 180 days old. Late: an NT 10-K or NT 10-Q in the last two years. None of these enter the letter.</p>")
    out.append(f"<h2>Ceased</h2><p class=\"small\">{len(ceased)} eligible filers.</p>" + table(["Company", "Last periodic filing", ">Days ago", ">Letter"], [
        f'<tr><td>{cell(state, r, page_link)}</td><td><a href="{escape(detail.get(r["ticker"], {}).get("gap", {}).get("last_filing_url") or "#")}">{escape(detail.get(r["ticker"], {}).get("gap", {}).get("last_form") or "")} {escape(long_date(detail.get(r["ticker"], {}).get("gap", {}).get("last_filing_date")))}</a></td><td class="n">{r.get("filing_gap_days")}</td><td class="n">{escape(r.get("grade") or "none")}</td></tr>'
        for r in ceased]))
    out.append(f"<h2>Stale</h2><p class=\"small\">{len(stale)} eligible filers; the 150 longest gaps are listed.</p>" + table(["Company", "Last periodic filing", ">Days ago", ">Letter", ">Market equity"], [
        f'<tr><td>{cell(state, r, page_link)}</td><td>{escape(detail.get(r["ticker"], {}).get("gap", {}).get("last_form") or "")} {escape(long_date(detail.get(r["ticker"], {}).get("gap", {}).get("last_filing_date")))}</td><td class="n">{r.get("filing_gap_days")}</td><td class="n">{escape(r.get("grade") or "none")}</td><td class="n">{_money(state["mcap"][r["ticker"]]) if state["mcap"].get(r["ticker"]) else ""}</td></tr>'
        for r in stale[:150]]))
    rows = []
    for r in late[:200]:
        forms = detail.get(r["ticker"], {}).get("late") or []
        text = "; ".join(f'{escape(str(f.get("form") or ""))} {escape(str(f.get("filed") or f.get("filingDate") or ""))}' if isinstance(f, dict) else escape(str(f)) for f in forms[:4])
        rows.append(f'<tr><td>{cell(state, r, page_link)}</td><td class="w">{text}</td><td class="n">{escape(r.get("grade") or "none")}</td><td class="n">{_money(state["mcap"][r["ticker"]]) if state["mcap"].get(r["ticker"]) else ""}</td></tr>')
    out.append(f"<h2>Late Filers</h2><p class=\"small\">{len(late)} eligible filers; the largest 200 are listed.</p>" + table(["Company", "Notices of late filing", ">Letter", ">Market equity"], rows))
    return chrome("Assay Ceased and Stale", "\n".join(out), state["index"]["as_of"], page_link)


def about_page(state: dict[str, Any], page_link: str = "./") -> str:
    summary = state["index"].get("summary") or {}
    rel = load_reliability()
    auc = (rel.get("all") or {}).get("auc")
    out = [heading("About")]
    out.append(process_strip([
        ("EDGAR", "bulk XBRL|nightly refresh"),
        ("Facts", "point in time|filed by rescan"),
        ("14 inputs", "three sleeves|receipts kept"),
        ("Sector rank", "percentile among|100 or more peers"),
        ("Letter", "fifth of sector|boundary at 1 SD"),
        ("Page", "every figure cited|no price in letter"),
    ]))
    out.append('<p class="caption">How a grade is made, left to right.</p>')
    out.append(
        "<h2>What the Letter Means</h2>"
        "<p>Assay grades the financial condition of every eligible company listed on a US exchange, every night, from the accounts each company filed with the SEC. "
        "Every company gets one letter. The letter says where the company stands among the companies in its own sector: A is the strongest fifth, E the weakest fifth, C the middle. "
        "Half of every sector sits at C or better by construction. A letter is a rank, not a score.</p>"
        "<p>The rank comes from fourteen accounting inputs in three groups: profitability, solvency, and growth and financing. "
        "Each input becomes a percentile among the graded companies of the sector, the three groups are averaged with equal weight, and that average is the rank. No share price enters the letter. "
        f"A company needs at least 7 of the 14 inputs and a sector with at least 100 graded companies to get a letter. The <a href=\"{page_link}methodology.html\">method page</a> lists every input with its formula and its source.</p>"
        "<h2>Where the Numbers Come From</h2>"
        "<p>Every figure on a company page was read from an SEC filing, and the bracketed number beside it opens that filing on sec.gov. "
        "Only facts the SEC had received by the rescan date are used, so a page for a past date shows what could have been known on that date and nothing filed later. "
        "Share prices come from Alpaca's end-of-day feed. They are shown next to the grade for context and never move the letter.</p>"
        "<h2>How to Read a Company Page</h2>"
        "<p>The scoreboard gives the letter, the rank among the sector's graded companies, the three group scores, the size band and any warnings. "
        "Warnings appear only when a check triggered: dilution, a short cash runway, a late filing, distress, degenerate inputs or thin data. "
        "The fourteen inputs follow, each with its value, its percentile, the peer median and the receipt. "
        "The peer curve shows where the company sits in its sector, and the rescan chart shows the letter it held at every past rescan. Every past rescan opens the page as it was computed that day.</p>"
        "<h2>What the Grade Is Not</h2>"
        "<p>The grade is not a return forecast and not investment advice. Across free data since 2015, no accounting input used here showed a return premium with a confidence interval that excluded zero, so the site makes no return claim and shows price measures without a verdict. "
        "What the grade has been tested against is failure: whether a company filed a bankruptcy notice, an 8-K Item 1.03, within a year. "
        + (f"On that outcome the failure model scores {auc:.3f} AUC out of sample, which means it ranks a company that went on to fail above one that did not about {auc * 100:.0f} times in 100. " if auc else "")
        + f"The <a href=\"{page_link}methodology.html#reliability\">reliability table</a> gives the figure for each size band with its interval.</p>"
        "<h2>How It Runs</h2>"
        "<p>The job runs every night at 03:00 New York time. It downloads the SEC's bulk XBRL archives, recomputes every company from scratch, checks every percentile and letter against the published receipts before the new pages replace the old, records the rescan, and rebuilds the site. "
        f"Rescans are kept daily for {export.RETENTION_DAYS} days and monthly back to January 2012. The <a href=\"{page_link}runs.html\">run log</a> lists every run with its log. "
        f"Today the SEC file lists {summary.get('tickers', 0):,} tickers, {summary.get('eligible', 0):,} of them eligible and {len(state['graded']):,} graded; the <a href=\"{page_link}coverage.html\">coverage page</a> gives the reason for every exclusion.</p>"
        "<p>Pages are static files with one small script for tooltips and charts, no cookies and no trackers. The code is Python with nothing beyond the standard library, and it is public.</p>"
        '<p class="small">Code: <a href="https://github.com/ekruges/assay">github.com/ekruges/assay</a> &middot; <a href="https://ezrakruger.cc">ezrakruger.cc</a></p>'
    )
    return chrome("Assay About", "\n".join(out), state["index"]["as_of"], page_link)


def time_machine_page(state: dict[str, Any], hist: dict[str, dict[str, dict[str, Any]]], page_link: str = "./") -> str:
    """Every rescan on file, and a reader over history/cube.json: the universe as it stood on any date, any ticker's path."""
    as_of = state["index"]["as_of"]
    dates = sorted(hist)
    out = [heading("Time Machine")]
    out.append(f"<p>{len(dates)} rescans on file, {escape(long_date(dates[0]))} to {escape(long_date(dates[-1]))}, each computed from the facts filed by that date. Pick a date to see the universe as it stood, or a ticker to see every letter it has held; every date opens the company as it was computed that day.</p>")
    out.append('<div class="cols even"><div><p><label for="tm-date"><b>Rescan:</b></label> <select id="tm-date"></select> <span id="tm-status" class="small">loading history</span></p><div id="tm-universe"></div></div>'
               '<div><p><label for="tm-ticker"><b>Ticker:</b></label> <input id="tm-ticker" placeholder="TICKER" maxlength="8" autocomplete="off" style="font-family:Times, serif; font-size:15px; width:7em; border:1px solid #000080; padding:2px 4px; text-transform:uppercase"> <button id="tm-go" type="button" style="font-family:Times, serif; font-size:15px; border:1px solid #000080; background:#e8e8f0; color:#000080">Show</button></p><div id="tm-ticker-out"></div></div></div>')
    rows = []
    for d in dates:
        graded = [v for v in hist[d].values() if v.get("grade")]
        counts = {g: sum(1 for v in graded if v["grade"][0] == g) for g in "ABCDE"}
        rows.append(f'<tr><td class="date">{escape(long_date(d))}</td><td class="n">{len(graded):,}</td>' + "".join(f'<td class="n">{counts[g]}</td>' for g in "ABCDE") + "</tr>")
    out.append("<h2>Every Rescan</h2>" + table(["Rescan", ">Graded", ">A", ">B", ">C", ">D", ">E"], rows, wide=False))
    script = """<script>
(function(){
  var sel=document.getElementById('tm-date'),status=document.getElementById('tm-status'),uni=document.getElementById('tm-universe'),tin=document.getElementById('tm-ticker'),tout=document.getElementById('tm-ticker-out');
  var page=document.querySelector('.page');var base=(page&&page.getAttribute('data-base'))||'./';var cube=null;
  function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function universe(i){var counts={A:0,B:0,C:0,D:0,E:0},sectors={},n=0;Object.keys(cube.tickers).forEach(function(t){var e=cube.tickers[t];var g=e.letters[i];if(!g)return;n++;counts[g[0]]++;var s=e.sector||'other';sectors[s]=sectors[s]||{n:0,A:0,E:0};sectors[s].n++;if(g[0]==='A')sectors[s].A++;if(g[0]==='E')sectors[s].E++;});
    var prev=i>0?i-1:null;var moves=[];if(prev!==null){Object.keys(cube.tickers).forEach(function(t){var e=cube.tickers[t];var a=e.letters[prev],b=e.letters[i];if(a&&b&&a[0]!==b[0]){moves.push([t,a,b,(e.percentiles[i]-e.percentiles[prev])*100]);}});}
    moves.sort(function(x,y){return Math.abs(y[3])-Math.abs(x[3]);});
    var h='<p class="small">'+n.toLocaleString()+' graded on '+esc(cube.dates[i])+': A '+counts.A+', B '+counts.B+', C '+counts.C+', D '+counts.D+', E '+counts.E+'.</p>';
    h+='<div class="scroll"><table class="data"><tr><th>Sector</th><th class="n">Graded</th><th class="n">A</th><th class="n">E</th></tr>'+Object.keys(sectors).sort(function(a,b){return sectors[b].n-sectors[a].n;}).map(function(s){return '<tr><td>'+esc(s.replace(/_/g,' '))+'</td><td class="n">'+sectors[s].n+'</td><td class="n">'+sectors[s].A+'</td><td class="n">'+sectors[s].E+'</td></tr>';}).join('')+'</table></div>';
    if(moves.length){h+='<p class="small">Largest letter moves since the previous rescan:</p><div class="scroll"><table class="data"><tr><th>Ticker</th><th>From</th><th>To</th><th class="n">Move</th></tr>'+moves.slice(0,20).map(function(m){return '<tr><td><a href="'+base+esc(m[0])+'.html">'+esc(m[0])+'</a></td><td>'+esc(m[1])+'</td><td><a href="'+base+'as-of.html#'+esc(m[0])+'~'+esc(cube.dates[i])+'">'+esc(m[2])+'</a></td><td class="n">'+(m[3]>=0?'+':'')+m[3].toFixed(0)+'</td></tr>';}).join('')+'</table></div>';}
    uni.innerHTML=h;}
  function ticker(){var t=(tin.value||'').trim().toUpperCase();var e=cube&&cube.tickers[t];if(!e){tout.innerHTML='<p class="small">'+(t?esc(t)+' is not in the graded history.':'')+'</p>';return;}
    var rows='',held=null;for(var i=0;i<cube.dates.length;i++){var g=e.letters[i];rows+='<tr><td class="date"><a href="'+base+'as-of.html#'+esc(t)+'~'+esc(cube.dates[i])+'">'+esc(cube.dates[i])+'</a></td><td class="n">'+(g?esc(g):'none')+'</td><td class="n">'+(e.percentiles[i]===null?'':(e.percentiles[i]*100).toFixed(1))+'</td></tr>';}
    var w=440,h=110,L=24,pts=[],years='',seen={};for(var i=0;i<cube.dates.length;i++){var x=L+i/(cube.dates.length-1)*(w-L-8);if(e.percentiles[i]!==null)pts.push(x.toFixed(1)+' '+(8+e.percentiles[i]*(h-24)).toFixed(1));var y=cube.dates[i].slice(0,4);if(!seen[y]){seen[y]=1;if(i>0)years+='<text x="'+x.toFixed(1)+'" y="'+(h-4)+'" font-size="9" text-anchor="middle">'+esc(y)+'</text>';}}
    var svg='<svg class="chart" viewBox="0 0 '+w+' '+h+'">'+[0.2,0.4,0.6,0.8].map(function(q){return '<line x1="'+L+'" y1="'+(8+q*(h-24))+'" x2="'+(w-8)+'" y2="'+(8+q*(h-24))+'" stroke="#c8c8d8"/>';}).join('')+'ABCDE'.split('').map(function(g,k){return '<text x="'+(L-5)+'" y="'+(8+(k*0.2+0.1)*(h-24)+3)+'" font-size="9" font-weight="bold" text-anchor="end">'+g+'</text>';}).join('')+(pts.length>1?'<path d="M'+pts.join('L')+'" fill="none" stroke="#000080" stroke-width="1.2"/>':'')+years+'</svg>';
    tout.innerHTML='<p><b><a href="'+base+esc(t)+'.html">'+esc(e.name||t)+'</a></b> ('+esc(t)+'), '+esc((e.sector||'').replace(/_/g,' '))+'</p>'+svg+'<div class="scroll" style="max-height:22em;overflow-y:auto"><table class="data"><tr><th>Rescan</th><th class="n">Letter</th><th class="n">Percentile</th></tr>'+rows+'</table></div>';}
  fetch(base+'history/cube.json').then(function(r){return r.json();}).then(function(c){cube=c;cube.dates.forEach(function(d,i){var o=document.createElement('option');o.value=i;o.textContent=d;sel.appendChild(o);});sel.value=cube.dates.length-1;status.textContent=cube.dates.length+' rescans loaded';universe(cube.dates.length-1);sel.addEventListener('change',function(){universe(parseInt(sel.value,10));});
    document.getElementById('tm-go').addEventListener('click',ticker);tin.addEventListener('keydown',function(ev){if(ev.key==='Enter')ticker();});}).catch(function(){status.textContent='history file not available';});
})();
</script>"""
    return chrome("Assay Time Machine", "\n".join(out) + script, as_of, page_link)



def point_in_time_page(state: dict[str, Any], page_link: str = "./") -> str:
    """A company as it stood on any rescan, in time machine mode: a banner names the date, the scoreboard, peers and the
    fourteen inputs come from that rescan's records, the timeline scrubs between rescans, and the downloads are cut for
    that date. Reads history/index/<date>.json, history/detail/<date>.json and downloads/<ticker>-rescans.csv in the
    browser; the hash is TICKER~DATE."""
    as_of = state["index"]["as_of"]
    out = ['<style>.tm{background:#000080;color:#fff;padding:9px 12px;margin:0 0 12px;font-size:14px;line-height:1.45}.tm a{color:#fff}.tm b{font-variant:small-caps;letter-spacing:.04em;margin-right:6px}'
           '.tm .nav{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:3px;font-size:13px}svg.timeline a circle:hover{r:4}</style>']
    out.append('<div id="pt-banner" class="tm" hidden></div>')
    out.append(heading("Point in Time"))
    out.append(f"<p>Any graded company as it was computed on any rescan on file, from the facts received by the SEC on or before that date. "
               f"Rescans are kept daily for the last {export.RETENTION_DAYS} days and monthly before that, on or after the 21st. "
               f"Open a date from a company page or the time machine, or pick one here. Arrow keys step between rescans.</p>")
    out.append('<p><label for="pt-ticker"><b>Ticker:</b></label> <input id="pt-ticker" placeholder="TICKER" maxlength="8" autocomplete="off" style="font-family:Times, serif; font-size:15px; width:7em; border:1px solid #000080; padding:2px 4px; text-transform:uppercase"> '
               '<label for="pt-date"><b>Rescan:</b></label> <select id="pt-date"></select> '
               '<button id="pt-go" type="button" style="font-family:Times, serif; font-size:15px; border:1px solid #000080; background:#e8e8f0; color:#000080">Show</button> <span id="pt-status" class="small"></span></p><div id="pt-out"></div>')
    script = """<script>
(function(){
  var labels=LABELS_JSON,flagLabels=FLAGS_JSON,sleeves=[["profitability","Profitability"],["solvency","Solvency"],["growth_and_financing","Growth and financing"]];
  var page=document.querySelector('.page');var base=(page&&page.getAttribute('data-base'))||'./';
  var tin=document.getElementById('pt-ticker'),sel=document.getElementById('pt-date'),status=document.getElementById('pt-status'),out=document.getElementById('pt-out'),banner=document.getElementById('pt-banner');
  var dates=[],current=null,blobs=[];
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function longDate(d){var m=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];return m[parseInt(d.slice(5,7),10)-1]+' '+parseInt(d.slice(8,10),10)+', '+d.slice(0,4);}
  function pct(p){return p==null?'':(p*100).toFixed(1);}
  function money(v){var a=Math.abs(v);var u=a>=1e12?[1e12,'T']:a>=1e9?[1e9,'B']:a>=1e6?[1e6,'M']:a>=1e3?[1e3,'K']:[1,''];return (v<0?'-':'')+'$'+(a/u[0]).toFixed(u[0]===1?0:1)+u[1];}
  function num(v,unit){if(v==null)return 'unresolved';if(unit==='USD')return money(v);var a=Math.abs(v);var t=a>=100?v.toFixed(0):a>=10?v.toFixed(1):a>=1?v.toFixed(2):v.toFixed(3);return unit&&unit!=='ratio'&&unit!=='percent'?t+' '+unit:t;}
  function ruler(p){var w=150,h=14;var t=[0,0.2,0.4,0.6,0.8,1].map(function(q){return '<line x1="'+(4+q*(w-8)).toFixed(1)+'" y1="4" x2="'+(4+q*(w-8)).toFixed(1)+'" y2="10" stroke="#000080"/>';}).join('');
    return '<svg class="pbar" width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'" style="vertical-align:middle"><line x1="4" y1="7" x2="'+(w-4)+'" y2="7" stroke="#000080"/>'+t+(p==null?'':'<rect x="'+(4+p*(w-8)-2).toFixed(1)+'" y="2" width="4" height="10" fill="#000080"/>')+'</svg>';}
  function fifth(g,n,sector){if(!g)return 'not graded';var names={A:'strongest fifth',B:'second fifth',C:'middle fifth',D:'fourth fifth',E:'weakest fifth'};return (g.length>1?'on the '+g+' boundary':names[g])+' of '+n.toLocaleString()+' graded peers in '+sector;}
  function density(values,focal){var xs=values.slice().sort(function(a,b){return a-b;}),n=xs.length;if(n<10)return '';var mean=0,i,j;for(i=0;i<n;i++)mean+=xs[i];mean/=n;var v=0;for(i=0;i<n;i++)v+=(xs[i]-mean)*(xs[i]-mean);var sd=Math.sqrt(v/n)||1e-6,bw=1.06*sd*Math.pow(n,-0.2);
    var lo=xs[0]-2*bw,hi=xs[n-1]+2*bw,span=hi-lo,w=640,h=150,L=30,R=30,T=26,B=24,grid=[],max=0;
    for(i=0;i<=120;i++){var x=lo+span*i/120,s=0;for(j=0;j<n;j++){var z=(x-xs[j])/bw;s+=Math.exp(-0.5*z*z);}s/=(n*bw*2.5066);grid.push(s);if(s>max)max=s;}
    function X(q){return L+(q-lo)/span*(w-L-R);}function Y(d){return T+(h-T-B)-(d/max)*(h-T-B);}
    var pts=grid.map(function(d,k){return X(lo+span*k/120).toFixed(1)+' '+Y(d).toFixed(1);});
    var area='M'+X(lo).toFixed(1)+' '+Y(0).toFixed(1)+'L'+pts.join('L')+'L'+X(hi).toFixed(1)+' '+Y(0).toFixed(1)+'Z';
    var edges=[0.2,0.4,0.6,0.8].map(function(q){return xs[Math.min(n-1,Math.floor(q*n))];});
    var bands=edges.map(function(e){return '<line x1="'+X(e).toFixed(1)+'" y1="'+T+'" x2="'+X(e).toFixed(1)+'" y2="'+(h-B)+'" stroke="#c8c8d8"/>';}).join('');
    var letters='ABCDE'.split('').map(function(g,k){var a=k?edges[k-1]:lo,b=k<4?edges[k]:hi;return '<text x="'+((X(a)+X(b))/2).toFixed(1)+'" y="'+(h-7)+'" font-size="10" font-weight="bold" text-anchor="middle">'+g+'</text>';}).join('');
    var mark=focal==null?'':'<line x1="'+X(focal).toFixed(1)+'" y1="'+(T-8)+'" x2="'+X(focal).toFixed(1)+'" y2="'+(h-B)+'" stroke="#000080" stroke-width="1.5"/><text x="'+X(focal).toFixed(1)+'" y="'+(T-11)+'" font-size="10" font-weight="bold" text-anchor="middle">this company</text>';
    return '<svg class="chart" viewBox="0 0 '+w+' '+h+'" aria-label="peer density">'+bands+'<path d="'+area+'" fill="#e8e8f0" stroke="#000080" stroke-width="1.2"/><line x1="'+L+'" y1="'+(h-B)+'" x2="'+(w-R)+'" y2="'+(h-B)+'" stroke="#000080"/>'+mark+letters+'</svg>';}
  function timeline(t,rows,d){var n=rows.length;if(n<2)return '';var w=640,h=110,L=24,R=10,T=8,B=18;function X(i){return L+i/(n-1)*(w-L-R);}function Y(p){return T+p*(h-T-B);}
    var bands=[0.2,0.4,0.6,0.8].map(function(q){return '<line x1="'+L+'" y1="'+Y(q).toFixed(1)+'" x2="'+(w-R)+'" y2="'+Y(q).toFixed(1)+'" stroke="#c8c8d8"/>';}).join('');
    var letters='ABCDE'.split('').map(function(g,k){return '<text x="'+(L-6)+'" y="'+(Y(k*0.2+0.1)+3).toFixed(1)+'" font-size="9" font-weight="bold" text-anchor="end">'+g+'</text>';}).join('');
    var segs=[],cur=[],i;for(i=0;i<n;i++){if(rows[i].pct==null){if(cur.length)segs.push(cur);cur=[];}else cur.push(X(i).toFixed(1)+' '+Y(rows[i].pct).toFixed(1));}if(cur.length)segs.push(cur);
    var path=segs.map(function(sg){return sg.length>1?'<path d="M'+sg.join('L')+'" fill="none" stroke="#000080" stroke-width="1.2"/>':'';}).join('');
    var dots='',years='',seen={};for(i=0;i<n;i++){var y=rows[i].date.slice(0,4);if(!seen[y]){seen[y]=1;if(i>0)years+='<text x="'+X(i).toFixed(1)+'" y="'+(h-4)+'" font-size="9" text-anchor="middle">'+esc(y)+'</text>';}
      if(rows[i].pct!=null){var here=rows[i].date===d;dots+='<a href="#'+esc(t)+'~'+esc(rows[i].date)+'"><circle cx="'+X(i).toFixed(1)+'" cy="'+Y(rows[i].pct).toFixed(1)+'" r="'+(here?4:2)+'" fill="'+(here?'#c00000':'#000080')+'" data-tip="'+esc(longDate(rows[i].date))+', '+esc(rows[i].letter||'no letter')+(rows[i].pct==null?'':', percentile '+pct(rows[i].pct))+'"/></a>';}}
    var mark=rows.some(function(r){return r.date===d;})?'<line x1="'+X(rows.findIndex(function(r){return r.date===d;})).toFixed(1)+'" y1="'+T+'" x2="'+X(rows.findIndex(function(r){return r.date===d;})).toFixed(1)+'" y2="'+Y(1).toFixed(1)+'" stroke="#c00000" stroke-dasharray="2 2"/>':'';
    return '<svg class="chart timeline" viewBox="0 0 '+w+' '+h+'" aria-label="letter at every rescan">'+bands+letters+'<line x1="'+L+'" y1="'+Y(1).toFixed(1)+'" x2="'+(w-R)+'" y2="'+Y(1).toFixed(1)+'" stroke="#000080"/>'+path+mark+dots+years+'</svg>';}
  function parseCsv(text){var lines=text.split(/\\r?\\n/).filter(Boolean).slice(1);return lines.map(function(l){var c=l.split(',');return {date:c[0],letter:c[2]||null,pct:c[3]===''||c[3]==null?null:parseFloat(c[3])};});}
  function blob(text,type){var u=URL.createObjectURL(new Blob([text],{type:type}));blobs.push(u);return u;}
  function csvCell(v){v=v==null?'':String(v);return /[",\\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;}
  function fetchJson(url){return fetch(url).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;});}
  function fetchText(url){return fetch(url).then(function(r){return r.ok?r.text():null;}).catch(function(){return null;});}
  function render(t,d){status.textContent='loading '+t+' as of '+d;blobs.forEach(function(u){URL.revokeObjectURL(u);});blobs=[];
    var latest=dates.length?dates[dates.length-1]:null;
    Promise.all([fetchJson(base+'history/index/'+d+'.json'),fetchJson(base+'history/detail/'+d+'.json'),fetchText(base+'downloads/'+t+'-rescans.csv'),latest&&latest!==d?fetchJson(base+'history/index/'+latest+'.json'):null,fetchJson(base+'prices/'+t+'.json')]).then(function(res){var idx=res[0],det=res[1],csv=res[2],now=res[3],bars=res[4];
      current={t:t,d:d};
      if(!idx){banner.hidden=true;out.innerHTML='<p>No rescan on file for '+esc(d)+'.</p>';status.textContent='';return;}
      var rows=idx.tickers,row=null,i;for(i=0;i<rows.length;i++)if(rows[i].ticker===t){row=rows[i];break;}
      if(!row){banner.hidden=true;out.innerHTML='<p>'+esc(t)+' was not in the graded universe on '+esc(longDate(d))+'.</p>';status.textContent='';return;}
      var sector=row.sector||'other',sectorLabel=sector.replace(/_/g,' '),peers=rows.filter(function(r){return r.sector===sector&&r.grade;});peers.sort(function(a,b){return a.percentile-b.percentile;});
      var n=peers.length,rank=0;for(i=0;i<n;i++)if(peers[i].percentile<row.percentile)rank++;rank+=1;
      var rec=det&&det.tickers?det.tickers[t]:null;var ie=row.implied_expectations||{};var name=row.name||t;
      var close=null,mom=ie.momentum_12_1,dist=ie.distance_from_52_week_high,closeDay=null;
      if(bars&&bars.d&&bars.d.length){var bi=-1;for(i=0;i<bars.d.length;i++){if(bars.d[i]<=d)bi=i;else break;}
        if(bi>=0){close=bars.c[bi];closeDay=bars.d[bi];if(bi>=252){mom=bars.c[bi-21]/bars.c[bi-252]-1;}var hi52=0;for(i=Math.max(0,bi-252);i<=bi;i++)if(bars.c[i]>hi52)hi52=bars.c[i];if(hi52>0)dist=bars.c[bi]/hi52-1;}}
      var pos=dates.indexOf(d),prev=pos>0?dates[pos-1]:null,next=pos>=0&&pos<dates.length-1?dates[pos+1]:null;var isLatest=latest===d;
      var nowRow=null;if(now&&now.tickers)for(i=0;i<now.tickers.length;i++)if(now.tickers[i].ticker===t){nowRow=now.tickers[i];break;}
      banner.hidden=false;banner.innerHTML='<b>Time machine</b>'+esc(name)+' ('+esc(t)+') as computed on <b style="font-variant:normal;letter-spacing:0">'+esc(longDate(d))+'</b>, from facts received by the SEC on or before that date. '+(isLatest?'This is the latest rescan.':'The latest rescan is '+esc(longDate(latest))+'.')
        +'<div class="nav">'+(prev?'<a href="#'+esc(t)+'~'+esc(prev)+'">&larr; previous rescan, '+esc(longDate(prev))+'</a>':'<span>first rescan on file</span>')+(next?'<a href="#'+esc(t)+'~'+esc(next)+'">next rescan, '+esc(longDate(next))+' &rarr;</a>':'<span>latest rescan</span>')+'<a href="'+base+esc(t)+'.html">Exit to the latest report</a><a href="'+base+'time-machine.html">Time machine</a></div>';
      var h='<h2>'+esc(name)+' ('+esc(t)+') as of '+esc(longDate(d))+'</h2>';
      h+='<table class="score"><tr><th colspan="2">'+esc(name)+'<span class="sub">'+esc(t)+' &middot; '+esc(sectorLabel)+' &middot; rescan of '+esc(longDate(d))+'</span></th></tr>';
      h+='<tr><td class="k">Grade</td><td><span class="lt" style="font-size:28px;font-weight:bold">'+esc(row.grade||'none')+'</span> '+ruler(row.percentile)+'<div class="small">'+esc(fifth(row.grade,n,sectorLabel))+(row.percentile==null?'':'; percentile '+pct(row.percentile))+(row.sampling_standard_deviation==null?'':'; sampling error '+(row.sampling_standard_deviation*100).toFixed(1)+' points')+'</div></td></tr>';
      if(row.grade)h+='<tr><td class="k">Rank</td><td>'+rank+' of '+n+'</td></tr>';
      if(nowRow)h+='<tr><td class="k">Since then</td><td>'+(nowRow.grade?'<span class="lt">'+esc(nowRow.grade)+'</span>, percentile '+pct(nowRow.percentile):'not graded')+' on '+esc(longDate(latest))+' <span class="small"><a href="'+base+esc(t)+'.html">latest report</a></span></td></tr>';
      if(rec&&rec.sleeves)sleeves.forEach(function(sl){var v=rec.sleeves[sl[0]];h+='<tr><td class="k">'+esc(sl[1])+'</td><td>'+ruler(v)+' <span class="small">percentile '+pct(v)+'</span></td></tr>';});
      h+='<tr><td class="k">Size band</td><td>'+esc(row.stratum||'none')+'<div class="small">standard universe: '+(row.standard_universe===true?'inside':row.standard_universe===false?'outside':'unknown')+'</div></td></tr>';
      var warn=rec&&rec.warnings&&rec.warnings.length?rec.warnings.map(function(w){return '<b>'+esc(flagLabels[w[0]]||w[0])+'</b>'+(w[1]?': '+esc(w[1]):'');}).join('<br>'):(row.active_flags&&row.active_flags.length?row.active_flags.map(function(f){return esc(flagLabels[f]||f);}).join(', '):'none active');
      h+='<tr><td class="k">Warnings</td><td>'+warn+'</td></tr>';
      h+='<tr><td class="k">Inputs computable</td><td>'+(row.inputs_computable==null?'':row.inputs_computable+' of 14')+'</td></tr>';
      h+='<tr><td class="k">Data age</td><td>'+(row.data_age_days==null?'':row.data_age_days+' days from the annual statements behind the grade')+(row.filing_gap_days==null?'':'<div class="small">'+row.filing_gap_days+' days since the latest periodic filing</div>')+'</td></tr>';
      if(mom!=null||dist!=null)h+='<tr><td class="k">Price measures</td><td>'+(mom==null?'':'12-1 momentum '+(mom*100).toFixed(0)+'%')+(dist==null?'':(mom==null?'':'; ')+(dist*100).toFixed(0)+'% from the 52-week high')+'<div class="small">from the split-adjusted closes on file up to the rescan date; no price enters the letter</div></td></tr>';
      if(close!=null)h+='<tr><td class="k">Close</td><td>$'+Number(close).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})+' <span class="small">split-adjusted close of '+esc(longDate(closeDay))+'</span></td></tr>';
      h+='</table>';
      var path=csv?parseCsv(csv):[];
      if(path.length>1){h+='<h2>Every Rescan</h2>'+timeline(t,path,d)+'<p class="caption">Percentile at every rescan on file; the red mark is the rescan shown. Click a point to open that rescan; arrow keys step.</p>';}
      if(row.grade){h+='<h2>Peers on '+esc(longDate(d))+'</h2>'+density(peers.map(function(r){return r.composite;}),row.composite);
        var lo=Math.max(0,rank-5),hi=Math.min(n,rank+4);h+='<div class="scroll"><table class="data"><tr><th>#</th><th>Company</th><th class="n">Letter</th><th class="n">Percentile</th></tr>';
        for(i=lo;i<hi;i++){var r=peers[i];h+='<tr'+(r.ticker===t?' style="background:#e8e8f0"':'')+'><td class="n">'+(i+1)+'</td><td><a href="#'+esc(r.ticker)+'~'+esc(d)+'">'+esc(r.name||r.ticker)+'</a> ('+esc(r.ticker)+')</td><td class="n">'+esc(r.grade)+'</td><td class="n">'+pct(r.percentile)+'</td></tr>';}
        h+='</table></div><p class="caption">Neighbors by percentile within the sector on that date; each opens that company as of the same rescan.</p>';}
      h+='<h2>The Fourteen Inputs</h2>';
      var inputsCsv=null;
      if(rec&&rec.inputs){h+='<div class="scroll"><table class="data"><tr><th>Input</th><th>Sleeve</th><th class="n">Value</th><th class="n">Percentile</th><th class="n">Peers</th><th>Period end</th><th>Receipts</th></tr>';
        var lines=['as_of,ticker,input,label,sleeve,value,unit,percentile,peers,period_end,status,receipts'];
        rec.inputs.forEach(function(f){var recs=(f[6]||[]).map(function(a){var r=rec.receipts[a]||[];return r[2]?'<a href="'+esc(r[2])+'">'+esc(r[0]||'filing')+' '+esc(r[1]||'')+'</a>':esc(a);}).join(', ');
          lines.push([d,t,f[0],labels[f[0]]||f[0],f[1],f[2],f[8],f[3],f[4],f[5],f[7],(f[6]||[]).map(function(a){var r=rec.receipts[a]||[];return r[2]||a;}).join(';')].map(csvCell).join(','));
          h+='<tr><td>'+esc(labels[f[0]]||f[0])+'</td><td>'+esc((f[1]||'').replace(/_/g,' '))+'</td><td class="n">'+esc(num(f[2],f[8]))+'</td><td class="n">'+pct(f[3])+'</td><td class="n">'+(f[4]==null?'':f[4])+'</td><td class="date">'+esc(f[5]||'')+'</td><td>'+recs+'</td></tr>';});
        inputsCsv=lines.join('\\n')+'\\n';
        h+='</table></div><p class="caption">Values as computed on that rescan; receipts open the SEC filing index each value was read from.</p>';}
      else h+='<p class="small">Inputs are not on file for this rescan; the record holds the letter, the rank, the warnings and the peer group. Inputs are recorded for every rescan the nightly job runs.</p>';
      h+='<h2>Downloads</h2><p class="small">Cut for the rescan of '+esc(longDate(d))+(isLatest?'':', not the latest report')+'. The PDF report is generated for the latest rescan only; print this view for a past one.</p><table class="nav"><tr><td><ul>';
      var record={ticker:t,as_of:d,record:row,detail:rec||null};
      h+='<li><a href="'+blob(JSON.stringify(record,null,1),'application/json')+'" download="'+esc(t)+'-'+esc(d)+'.json">This rescan, JSON</a><span class="small">the letter, rank, warnings'+(rec?', inputs and receipts':'')+' for '+esc(t)+' on '+esc(longDate(d))+'</span></li>';
      if(inputsCsv)h+='<li><a href="'+blob(inputsCsv,'text/csv')+'" download="'+esc(t)+'-inputs-'+esc(d)+'.csv">Inputs CSV, this rescan</a><span class="small">the fourteen inputs as computed on '+esc(longDate(d))+'</span></li>';
      h+='<li><a href="'+base+'downloads/'+esc(t)+'-rescans.csv">Rescans CSV</a><span class="small">letter and percentile at every rescan on file</span></li>';
      h+='<li><a href="'+base+'history/index/'+esc(d)+'.json">Universe on this date, JSON</a><span class="small">every graded company\\'s letter, percentile and warnings on '+esc(longDate(d))+'</span></li>';
      if(rec)h+='<li><a href="'+base+'history/detail/'+esc(d)+'.json">Inputs for every company on this date, JSON</a><span class="small">the record behind this page</span></li>';
      h+='<li><a href="'+base+esc(t)+'.pdf">PDF report, latest rescan</a><span class="small">'+(isLatest?'this rescan':'as of '+esc(longDate(latest||''))+', not this date')+'</span></li>';
      h+='<li><a href="'+base+'tickers/'+esc(t)+'.json">Artifact JSON, latest rescan</a><span class="small">every input, receipt and diagnostic as last computed</span></li>';
      h+='<li><a href="#print" onclick="window.print();return false">Print this view</a><span class="small">the browser\\'s print dialog</span></li></ul></td></tr></table>';
      out.innerHTML=h;status.textContent='';window.scrollTo(0,0);});}
  function fromHash(){var m=/^#([A-Z0-9.\\-]+)~(\\d{4}-\\d{2}-\\d{2})$/.exec(location.hash||'');if(!m){banner.hidden=true;return;}tin.value=m[1];if(dates.indexOf(m[2])>=0)sel.value=m[2];render(m[1],m[2]);}
  function go(){var t=(tin.value||'').trim().toUpperCase(),d=sel.value;if(!t||!d)return;if(location.hash!=='#'+t+'~'+d)location.hash='#'+t+'~'+d;else render(t,d);}
  function step(k){if(!current)return;var pos=dates.indexOf(current.d)+k;if(pos<0||pos>=dates.length)return;location.hash='#'+current.t+'~'+dates[pos];}
  fetchJson(base+'history/dates.json').then(function(ds){dates=ds||[];dates.forEach(function(d){var o=document.createElement('option');o.value=d;o.textContent=d;sel.appendChild(o);});if(dates.length)sel.value=dates[dates.length-1];fromHash();});
  document.getElementById('pt-go').addEventListener('click',go);tin.addEventListener('keydown',function(ev){if(ev.key==='Enter')go();});window.addEventListener('hashchange',fromHash);
  document.addEventListener('keydown',function(ev){if(ev.target===tin||ev.target===sel)return;if(ev.key==='ArrowLeft')step(-1);if(ev.key==='ArrowRight')step(1);});
})();
</script>""".replace("LABELS_JSON", json.dumps({k: v[0] for k, v in FACTORS.items()})).replace("FLAGS_JSON", json.dumps(FLAG_LABELS))
    return chrome("Assay Point in Time", "\n".join(out) + script, as_of, page_link)


def not_found_page(state: dict[str, Any], site_root: str = "/") -> str:
    """The page served for any missing address. Links are absolute from the site root so it works at any depth;
    when the address looks like a ticker page, the script names the ticker and offers the EDGAR company search for it."""
    as_of = state["index"]["as_of"]
    body = heading("Nothing Found") + (
        '<p id="nf-msg">There is no page at this address.</p>'
        f'<p>Search another ticker: <form style="display:inline" onsubmit="var t=this.t.value.trim().toUpperCase(); if(t){{location.href=\'{escape(site_root)}\'+t+\'.html\'}} return false">'
        '<input id="nf-ticker" name="t" placeholder="TICKER" maxlength="8" autocomplete="off" aria-label="ticker" style="font-family:Times, serif; font-size:15px; width:7em; border:1px solid #000080; padding:2px 4px; text-transform:uppercase"> '
        '<button type="submit" style="font-family:Times, serif; font-size:15px; border:1px solid #000080; background:#e8e8f0; color:#000080">Go</button></form></p>'
        f'<p><a href="{escape(site_root)}index.html">Front page</a> &middot; <a href="{escape(site_root)}companies.html">Every graded company</a> &middot; '
        f'<a href="{escape(site_root)}coverage.html">Why a ticker may not be graded</a> &middot; <a id="nf-edgar" href="https://www.sec.gov/edgar/searchedgar/companysearch">EDGAR company search</a></p>'
        f'<p class="small">{len(state["graded"]):,} companies are graded as of {escape(long_date(as_of))}; every other SEC ticker has a page stating why it is not.</p>'
    )
    script = """<script>
(function(){var m=/\\/([A-Z0-9.\\-]{1,8})\\.html$/.exec(location.pathname);if(!m)return;var t=m[1];
  document.getElementById('nf-msg').textContent='No report for '+t+'. Either no SEC filer trades under that symbol, or it is not in the universe this site grades.';
  document.getElementById('nf-ticker').value=t;
  document.getElementById('nf-edgar').href='https://www.sec.gov/cgi-bin/browse-edgar?company='+encodeURIComponent(t)+'&action=getcompany';
  document.getElementById('nf-edgar').textContent='EDGAR company search for '+t;})();
</script>"""
    return chrome("Assay Not Found", body + script, as_of, site_root)


def error_page(state: dict[str, Any], site_root: str = "/") -> str:
    """The page served for a server error. The site is static, so a retry usually works; the run log shows whether the nightly is healthy."""
    as_of = state["index"]["as_of"]
    body = heading("Something Went Wrong") + (
        "<p>The server could not serve this page. Every page here is a static file, so trying again usually works.</p>"
        f'<p><a href="javascript:location.reload()">Try again</a> &middot; <a href="{escape(site_root)}index.html">Front page</a> &middot; '
        f'<a href="{escape(site_root)}runs.html">Run log</a> &middot; <a href="{escape(site_root)}status.json">Status</a></p>'
    )
    return chrome("Assay Error", body, as_of, site_root)


def load_runs(path: Path | None) -> list[dict[str, Any]]:
    """The nightly run records, oldest first."""
    if not path or not path.exists():
        return []
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            runs.append(json.loads(line))
        except ValueError:
            continue
    return sorted(runs, key=lambda r: r.get("started") or "")


def runs_page(runs: list[dict[str, Any]], as_of: str, page_link: str = "./") -> str:
    """Every nightly run on file: when it ran, how long, what it graded, whether it verified, and its log."""
    out = [heading("Run Log")]
    out.append("<p>The job runs every night at 03:00 New York time: it refreshes the SEC bulk archives, syncs prices, recomputes every company from the facts on file, "
               "rebuilds every percentile and letter from the published receipts before the new tree replaces the old one, records the rescan, and rebuilds the site. "
               "Every run is listed here with its log.</p>")
    if not runs:
        out.append("<p>No runs recorded yet.</p>")
        return chrome("Assay Run Log", "\n".join(out), as_of, page_link)
    last = runs[-1]
    durations = [r["seconds"] for r in runs if isinstance(r.get("seconds"), (int, float))]
    failed = sum(1 for r in runs if r.get("failed"))
    out.append(tiles([("runs on file", f"{len(runs):,}"), ("latest run", long_date(last.get("as_of") or as_of)), ("latest run", "failed" if last.get("failed") else ("verified" if last.get("verified") else "unverified")), ("runs failed", f"{failed:,}"),
                      ("median duration", f"{statistics.median(durations) / 60:.0f} min" if durations else "n/a")]))
    rows = []
    for r in reversed(runs):
        letters = r.get("letters") or {}
        started = (r.get("started") or "")[:16].replace("T", " ")
        log = f'<a href="{page_link}{escape(r["log"])}">log</a>' if r.get("log") else ""
        rows.append(f'<tr><td class="date">{escape(long_date(r.get("as_of") or ""))}</td><td class="date">{escape(started)} UTC</td>'
                    f'<td class="n">{(r.get("seconds") or 0) / 60:.0f} min</td><td class="n">{r.get("graded") or 0:,}</td>'
                    + "".join(f'<td class="n">{letters.get(g, 0)}</td>' for g in "ABCDE")
                    + f'<td class="n">{r.get("letter_changes") if r.get("letter_changes") is not None else ""}</td><td>{"failed" if r.get("failed") else ("verified" if r.get("verified") else "unverified")}</td><td>{log}</td></tr>')
    out.append("<h2>Every Run</h2>" + table(["Rescan", "Started", ">Duration", ">Graded", ">A", ">B", ">C", ">D", ">E", ">Letters changed", "Status", "Log"], rows))
    return chrome("Assay Run Log", "\n".join(out), as_of, page_link)


def company_csvs(report: dict[str, Any], rescans: list[tuple[str, str | None, float | None]]) -> tuple[str, str]:
    """Two CSV texts for one company: the fourteen inputs with receipts, and every rescan on file."""
    inputs = io.StringIO()
    w = csv.writer(inputs)
    w.writerow(["as_of", "ticker", "input", "label", "sleeve", "value", "unit", "percentile", "peers", "period_end", "status", "receipts"])
    ticker, as_of = report["company"]["ticker"], report["as_of"]
    percentiles = (report.get("grade") or {}).get("factor_percentiles") or {}
    for f in report.get("factors", []):
        pc = percentiles.get(f["name"]) or {}
        urls = []
        for fact in f.get("inputs") or []:
            if fact.get("filing_url") and fact["filing_url"] not in urls:
                urls.append(fact["filing_url"])
        w.writerow([as_of, ticker, f["name"], FACTORS.get(f["name"], (f["name"],))[0], f.get("sleeve"), f.get("value"), f.get("unit"), pc.get("percentile"), pc.get("peer_count"), f.get("period_end"), f.get("status"), ";".join(urls)])
    history = io.StringIO()
    w = csv.writer(history)
    w.writerow(["as_of", "ticker", "letter", "percentile"])
    for day, letter, pct in rescans:
        w.writerow([day, ticker, letter, pct])
    return inputs.getvalue(), history.getvalue()


# ---------------------------------------------------------------- build

def stub_page(entry: dict[str, Any], as_of: str, page_link: str = "./") -> str:
    """A page for a ticker outside the graded universe: status, reason, and the way to EDGAR."""
    name = home._company_name(entry.get("name") or entry["ticker"])
    reason = entry.get("reason") or ""
    body = heading(f"{name} ({entry['ticker']})") + f'<p><b>Status: {escape(entry.get("status") or "")}.</b> {escape(REFUSALS.get(reason, reason.replace("_", " ")))}</p>'
    body += f'<p>{escape(entry.get("exchange") or "no exchange on record")}; SIC {entry.get("sic") or "none"}{", " + escape(entry["sic_description"]) if entry.get("sic_description") else ""}. '
    body += f'<a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&amp;CIK={int(entry["cik"]):010d}&amp;type=&amp;dateb=&amp;owner=include&amp;count=40">All SEC filings for CIK {int(entry["cik"]):010d}</a>. No letter is computed; see <a href="{page_link}coverage.html">coverage and refusals</a> for the rule.</p>'
    return chrome(f"Assay {entry['ticker']}", body, as_of, page_link)


def build(data: Path, out: Path, history: Path | None, prices: Path | None, descriptions: Path | None, tickers: list[str] | None,
          page_link: str = "./", calendar: Path | None = None, history_index: Path | None = None, asset_base: str | None = None, pdfs: bool = False,
          detail: Path | None = None, runs: Path | None = None, logs: Path | None = None, only: list[str] | None = None, site_root: str = "/") -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    if asset_base:
        animals.configure(asset_base)
        (out / asset_base.rstrip("/")).mkdir(parents=True, exist_ok=True)
        for asset in animals.asset_files():
            (out / asset_base.rstrip("/") / asset.name).write_bytes(asset.read_bytes())
    state = home.load(data, history)
    prices_map = json.loads(prices.read_text(encoding="utf-8")) if prices and prices.exists() else {}
    desc_map = json.loads(descriptions.read_text(encoding="utf-8")) if descriptions and descriptions.exists() else {}
    cal = json.loads(calendar.read_text(encoding="utf-8")) if calendar and calendar.exists() else None
    hist = load_history_index(history_index)
    if state["history"]:
        hist[state["index"]["as_of"]] = {r["ticker"]: {"sector": r.get("sector"), "grade": r.get("grade"), "percentile": r.get("percentile"), "status": r.get("status")} for r in state["index"]["tickers"]}
    for name in ("index.json", "universe.json", "methodology.json", "forecasts.json"):
        if (data / name).exists():
            shutil.copyfile(data / name, out / name)
    if history and history.exists():
        shutil.copyfile(history, out / "grade_history.json")
    if calendar and calendar.exists():
        shutil.copyfile(calendar, out / "filing_calendar.json")
    pages = {
        "index.html": lambda: home.render(state, page_link),
        "methodology.html": lambda: methodology_page(state, data, page_link),
        "coverage.html": lambda: coverage_page(state, data, page_link),
        "forecasts.html": lambda: forecasts_page(state, data, page_link),
        "companies.html": lambda: companies_page(state, page_link),
        "sectors.html": lambda: sectors_page(state, hist, page_link),
        "calendar.html": lambda: calendar_page(state, cal, page_link),
        "last-night.html": lambda: last_night_page(state, hist, page_link),
        "warnings.html": lambda: warnings_page(state, page_link),
        "price-and-condition.html": lambda: price_condition_page(state, page_link),
        "then-and-now.html": lambda: then_now_page(state, hist, page_link),
        "size-bands.html": lambda: size_bands_page(state, page_link),
        "ceased.html": lambda: ceased_page(state, page_link),
        "about.html": lambda: about_page(state, page_link),
        "time-machine.html": lambda: time_machine_page(state, hist, page_link),
        "as-of.html": lambda: point_in_time_page(state, page_link),
        "runs.html": lambda: runs_page(load_runs(runs), state["index"]["as_of"], page_link),
        "404.html": lambda: not_found_page(state, site_root),
        "error.html": lambda: error_page(state, site_root),
    }
    if only:
        for name in only:
            (out / name).write_text(pages[name](), encoding="utf-8")
        (out / "logs").mkdir(exist_ok=True)
        if logs and logs.exists():
            for path in logs.glob("*.log"):
                shutil.copyfile(path, out / "logs" / path.name)
        run_records = load_runs(runs)
        (out / "status.json").write_text(json.dumps({"as_of": state["index"]["as_of"], "graded": len(state["graded"]), "last_run": run_records[-1] if run_records else None}, indent=1), encoding="utf-8")
        return {"companies": 0, "stubs": 0, "sectors": 0, "pages": len(only), "rescans": 0, "pdfs": 0}
    for name, render_page in pages.items():
        (out / name).write_text(render_page(), encoding="utf-8")
    sectors = sorted({r.get("sector") or "other" for r in state["graded"]})
    for sector in sectors:
        (out / f"sector-{sector}.html").write_text(sector_page(state, sector, page_link), encoding="utf-8")
    exported = export.build(data, out, history_index, detail)
    (out / "data.html").write_text(data_page(state, out, page_link), encoding="utf-8")
    (out / "logs").mkdir(exist_ok=True)
    if logs and logs.exists():
        for path in logs.glob("*.log"):
            shutil.copyfile(path, out / "logs" / path.name)
    run_records = load_runs(runs)
    (out / "status.json").write_text(json.dumps({"as_of": state["index"]["as_of"], "graded": len(state["graded"]), "rescans": exported["dates"], "last_run": run_records[-1] if run_records else None}, indent=1), encoding="utf-8")
    index = state["index"]
    dates = sorted(hist)
    series: dict[str, list[tuple[str, str | None, float | None]]] = {}
    for d in dates:
        for ticker, v in hist[d].items():
            if v.get("status") == "eligible":
                series.setdefault(ticker, []).append((d, v.get("grade"), v.get("percentile")))
    (out / "tickers").mkdir(exist_ok=True)
    (out / "downloads").mkdir(exist_ok=True)
    wanted = [t.upper() for t in tickers] if tickers else [r["ticker"] for r in state["rows"]]
    peer_cache: dict[str, tuple[list[dict[str, Any]], dict[str, float]]] = {}
    count = stubs = 0
    for ticker in wanted:
        path = data / "tickers" / f"{ticker}.json"
        if not path.exists():
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        if state["history"]:
            report["grade_history"] = ticker_history(state["history"], ticker)
        (out / f"{ticker}.html").write_text(render_company(report, data, prices_map.get(ticker), index, desc_map.get(ticker), page_link, series.get(ticker), pdfs, downloads=True), encoding="utf-8")
        shutil.copyfile(path, out / "tickers" / f"{ticker}.json")
        inputs_csv, rescans_csv = company_csvs(report, series.get(ticker) or [])
        (out / "downloads" / f"{ticker}-inputs.csv").write_text(inputs_csv, encoding="utf-8")
        (out / "downloads" / f"{ticker}-rescans.csv").write_text(rescans_csv, encoding="utf-8")
        if pdfs:
            group = report["grade"].get("peer_group") or report["company"].get("sector") or ""
            if group not in peer_cache:
                peers = load_peers(data, group)
                peer_cache[group] = (peers, peer_factor_medians(data, peers)[0] if peers else {})
            peers, medians = peer_cache[group]
            (out / f"{ticker}.pdf").write_bytes(company_report(report, prices_map.get(ticker), desc_map.get(ticker), report.get("grade_history"), peers, medians, series.get(ticker)))
        count += 1
    if not tickers:
        for entry in index["tickers"]:
            if entry.get("status") != "eligible" and entry.get("cik"):
                (out / f"{entry['ticker']}.html").write_text(stub_page(entry, index["as_of"], page_link), encoding="utf-8")
                stubs += 1
    return {"companies": count, "stubs": stubs, "sectors": len(sectors), "pages": len(pages) + 1, "rescans": exported["dates"], "pdfs": count if pdfs else 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build the static site from an assay output tree")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--history-index", type=Path, help="folder of per-date index.json files from past rescans")
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--descriptions", type=Path)
    parser.add_argument("--calendar", type=Path, help="filing_calendar.json from assay.calendar")
    parser.add_argument("--tickers", nargs="*")
    parser.add_argument("--all", action="store_true", help="render every eligible ticker")
    parser.add_argument("--page-link", default="./")
    parser.add_argument("--asset-base", help="serve photographs as files under this path, such as img/, instead of embedding them")
    parser.add_argument("--detail", type=Path, help="folder of per-date detail files for the point-in-time page")
    parser.add_argument("--runs", type=Path, help="runs.jsonl from assay.runlog")
    parser.add_argument("--logs", type=Path, help="folder of nightly logs to publish")
    parser.add_argument("--only", nargs="+", metavar="PAGE", help="write only these pages, the logs and status.json, from the existing tree")
    parser.add_argument("--site-root", default="/", help="absolute path the site is served under, for the error pages, such as /assay/")
    parser.add_argument("--pdf", action="store_true", help="also write a PDF report per company")
    args = parser.parse_args(argv)
    if not args.all and not args.tickers:
        parser.error("pass --tickers or --all")
    result = build(args.data, args.out, args.history, args.prices, args.descriptions, None if args.all else args.tickers, args.page_link, args.calendar, args.history_index, args.asset_base, args.pdf, args.detail, args.runs, args.logs, args.only, args.site_root)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
