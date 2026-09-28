"""Front page: three features chosen by rule, the largest companies, the year's biggest moves.

    python3 -m assay.home --data data --history .state/grade_history.json --out index.html
    python3 -m assay.home --data data --history .state/grade_history.json --list-featured
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import date, timedelta
from html import escape
from pathlib import Path
from typing import Any

from . import animals
from .render import CHARSET, CSS, FLAG_LABELS, TIP_SCRIPT, WARN_SYMBOL, credit_footer, site_map, _company_cell, _company_name, _money, long_date, nav_bar, pct_points

HOME_CSS = """
  .features { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 6px 0 14px; }
  @media (max-width: 52em) { .features { grid-template-columns: minmax(0, 1fr); } }
  .feature { border: 1px solid #000080; padding: 0; }
  .feature img { display: block; width: 100%; height: 150px; object-fit: cover; object-position: 50% 30%; border-bottom: 1px solid #000080; }
  .feature .body { padding: 6px 9px 8px; }
  .feature .kicker { font-variant: small-caps; font-weight: bold; font-size: 13px; }
  .feature .name { font-size: 17px; font-weight: bold; margin: 2px 0; }
  .feature p { margin: 0 0 3px; font-size: 13px; }
  .feature .letter { font-size: 22px; font-weight: bold; margin-right: 6px; }
  .layout { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 1.25fr); gap: 0 28px; align-items: start; }
  @media (max-width: 52em) { .layout { grid-template-columns: minmax(0, 1fr); } }
  .legend { display: grid; grid-template-columns: 56px 1fr; gap: 6px 10px; align-items: center; margin: 4px 0 8px; font-size: 13px; }
  .legend img { width: 56px; height: 42px; object-fit: cover; border: 1px solid #000080; }
  .stats { font-size: 13px; margin: 0 0 4px; }
  .stats span { margin-right: 14px; }
  table.data td.w { max-width: 22em; }
  .tiles { display: flex; flex-wrap: wrap; gap: 0; border: 1px solid #000080; margin: 6px 0 12px; }
  .tile { flex: 1 1 7em; padding: 8px 10px; border-right: 1px solid #000080; border-bottom: 1px solid #000080; margin-bottom: -1px; }
  .tile:last-child { border-right: 0; }
  .tile .v { font-size: 24px; font-weight: bold; line-height: 1.1; font-variant-numeric: tabular-nums; }
  .tile .l { font-size: 12px; margin-top: 2px; }
"""


def load(data: Path, history_path: Path | None) -> dict[str, Any]:
    index = json.loads((data / "index.json").read_text(encoding="utf-8"))
    rows = [r for r in index["tickers"] if r.get("status") == "eligible"]
    graded = [r for r in rows if r.get("grade")]
    mcap: dict[str, float] = {}
    detail: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            report = json.loads((data / r["artifact"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        equity = ((report.get("implied_expectations") or {}).get("live_inputs") or {}).get("market_equity")
        if isinstance(equity, (int, float)):
            mcap[r["ticker"]] = float(equity)
        diagnostics = report.get("diagnostics") or {}
        detail[r["ticker"]] = {
            "flags": {f["name"]: f.get("detail") or "" for f in report.get("flags", []) if f.get("active")},
            "late": (diagnostics.get("late_filer") or {}).get("filings") or [],
            "gap": diagnostics.get("filing_gap") or {},
            "data_age": ((report.get("condition") or {}).get("data_age") or {}).get("days"),
            "inputs": (report.get("coverage") or {}).get("resolved"),
        }
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path and history_path.exists() else None
    return {"index": index, "rows": rows, "graded": graded, "mcap": mcap, "history": history, "detail": detail}


def _momentum(row: dict[str, Any]) -> float | None:
    value = (row.get("implied_expectations") or {}).get("momentum_12_1")
    return float(value) if isinstance(value, (int, float)) else None


FEATURE_COOLDOWN_DAYS = 14


def load_featured(path: Path | None, as_of: str, days: int = FEATURE_COOLDOWN_DAYS) -> set[str]:
    """Tickers featured within `days` before as_of, from the featured log (one JSON line per run)."""
    if not path or not path.exists():
        return set()
    since = (date.fromisoformat(as_of) - timedelta(days=days)).isoformat()
    recent: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if since <= entry.get("as_of", "") < as_of:
            recent.update(t for t in entry.get("tickers", []) if t)
    return recent


def record_featured(path: Path, as_of: str, picks: list[dict[str, Any]]) -> None:
    """Append the day's picks unless the day is already on file, so a rebuild does not advance the rotation."""
    if path.exists() and any(line.strip().startswith('{"as_of": "%s"' % as_of) for line in path.read_text(encoding="utf-8").splitlines()):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"as_of": as_of, "tickers": [p["row"]["ticker"] for p in picks], "kickers": [p["kicker"] for p in picks]}) + "\n")


def features(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Three companies picked by stated rules; each carries its rule for the page. A company featured within the last
    FEATURE_COOLDOWN_DAYS days yields to the next candidate unless it is first (for the bear, last) in its sector that day."""
    graded, mcap = state["graded"], state["mcap"]
    recent = state.get("recent_features") or set()
    first: dict[str, float] = {}
    last: dict[str, float] = {}
    for r in graded:
        sector = r.get("sector") or "other"
        first[sector] = min(first.get(sector, 1.0), r["percentile"])
        last[sector] = max(last.get(sector, 0.0), r["percentile"])
    big = [r for r in graded if mcap.get(r["ticker"], 0) >= 1e10]
    small = [r for r in graded if 0 < mcap.get(r["ticker"], 0) < 2e9]
    bulls = sorted((r for r in big if r["grade"][0] == "A"), key=lambda r: r["percentile"])
    bears = sorted((r for r in big if r["grade"][0] == "E"), key=lambda r: -r["percentile"])
    sleepers = sorted((r for r in small if r["grade"][0] == "A" and (_momentum(r) or 0) < 0), key=lambda r: r["percentile"])
    cooldown = f"; not repeated within {FEATURE_COOLDOWN_DAYS} days unless"
    picks = []
    for kicker, animal, group, rule, extreme in (
        ("Bull of the Day", "bull", bulls, "graded A with market equity over $10B; strongest percentile in its sector", first),
        ("Bear of the Day", "bear", bears, "graded E with market equity over $10B; weakest percentile in its sector", last),
        ("Sleeper", "fox", sleepers, "graded A with market equity under $2B and 12-1 momentum below zero; strongest percentile", first),
    ):
        fresh = [r for r in group if r["ticker"] not in recent or r["percentile"] == extreme.get(r.get("sector") or "other")]
        if fresh:
            picks.append({"kicker": kicker, "animal": animal, "row": fresh[0], "rule": rule + cooldown + (" last in its sector" if extreme is last else " first in its sector")})
    return picks


def year_before(as_of: str) -> str:
    return (date.fromisoformat(as_of) - timedelta(days=365)).isoformat()


def best_ranked(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The row with the lowest percentile; a percentile of exactly zero is the strongest, not a missing value."""
    ranked = [r for r in rows if isinstance(r.get("percentile"), (int, float))]
    return min(ranked, key=lambda r: r["percentile"]) if ranked else None


def movers(state: dict[str, Any], min_equity: float = 5e8) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Companies whose letter changed over the year to the latest rescan, ranked by percentile move."""
    history = state["history"]
    if not history:
        return [], []
    as_of = state["index"]["as_of"]
    since = year_before(as_of)
    by_ticker = {r["ticker"]: r for r in state["graded"]}
    moves = []
    for ticker, entry in history["tickers"].items():
        if ticker not in by_ticker or state["mcap"].get(ticker, 0) < min_equity:
            continue
        points = [c for c in entry.get("changes", []) if c[1]]
        before = [c for c in points if c[0] <= since]
        during = [c for c in points if since < c[0] <= as_of]
        if not during:
            continue
        first, last = (before[-1] if before else during[0]), during[-1]
        if first is last or first[1][0] == last[1][0]:
            continue
        held = [c[1] for c in during if c is not first]
        moves.append({"row": by_ticker[ticker], "first": first, "last": last, "delta": last[2] - first[2], "path": [first[1]] + held})
    weaker = sorted((m for m in moves if m["delta"] > 0), key=lambda m: -m["delta"])[:10]
    stronger = sorted((m for m in moves if m["delta"] < 0), key=lambda m: m["delta"])[:10]
    return weaker, stronger


def largest(state: dict[str, Any], n: int = 20) -> list[dict[str, Any]]:
    return sorted((r for r in state["graded"] if state["mcap"].get(r["ticker"])), key=lambda r: -state["mcap"][r["ticker"]])[:n]


def featured(state: dict[str, Any]) -> list[str]:
    weaker, stronger = movers(state)
    tickers = [f["row"]["ticker"] for f in features(state)] + [r["ticker"] for r in largest(state)] + [m["row"]["ticker"] for m in weaker + stronger]
    by_sector: dict[str, dict[str, Any]] = {}
    for r in state["graded"]:
        best = by_sector.get(r.get("sector"))
        if isinstance(r.get("percentile"), (int, float)) and (best is None or r["percentile"] < best["percentile"]):
            by_sector[r.get("sector")] = r
    tickers += [r["ticker"] for r in by_sector.values()]
    return list(dict.fromkeys(tickers))


def _mom_text(row: dict[str, Any]) -> str:
    value = _momentum(row)
    return f"{value * 100:+.0f}%" if value is not None else "n/a"


def _flags(row: dict[str, Any]) -> str:
    return ", ".join(FLAG_LABELS.get(f, f).lower() for f in (row.get("active_flags") or []))


def _feature(pick: dict[str, Any], mcap: dict[str, float], page_link: str) -> str:
    row = pick["row"]
    src = animals.photo(pick["animal"], 640)
    img = f'<img src="{src}" alt="{escape(pick["animal"])}">' if src else ""
    name = _company_name(row.get("name") or "")
    sector = (row.get("sector") or "").replace("_", " ")
    return (
        f'<div class="feature">{img}<div class="body"><div class="kicker">{escape(pick["kicker"])}</div>'
        f'<div class="name"><a href="{escape(page_link)}{escape(row["ticker"])}.html">{escape(name)}</a> ({escape(row["ticker"])})</div>'
        f'<p><span class="letter">{escape(row["grade"])}</span>percentile {pct_points(row["percentile"])} of {row.get("peer_count") or ""} {escape(sector)} peers</p>'
        f'<p>market equity {_money(mcap.get(row["ticker"], 0))}; 12-1 momentum {_mom_text(row)}; {escape(_flags(row)) or "no active warnings"}</p>'
        f'<p class="small">Rule: {escape(pick["rule"])}.</p></div></div>'
    )


def render(state: dict[str, Any], page_link: str = "./") -> str:
    index, graded, mcap, history = state["index"], state["graded"], state["mcap"], state["history"]
    summary = index.get("summary") or {}
    as_of = index["as_of"]
    sd = None
    rescans, first_rescan = 0, None
    if history:
        dates = sorted({c[0] for entry in history["tickers"].values() for c in entry.get("changes", [])})
        first_rescan = dates[0] if dates else None
        rescans = len(sorted({entry.get("last_seen") for entry in history["tickers"].values() if entry.get("last_seen")} | set(dates)))
    letters = summary.get("grades") or {}
    graded_count = sum(v for k, v in letters.items() if k != "unresolved")
    out = [
        CHARSET, "<title>Assay Front Page</title>", f"<style>{CSS}{HOME_CSS}</style>", WARN_SYMBOL, f'<div class="page" id="top" data-base="{escape(page_link)}">',
        '<div class="center"><h1>Assay</h1><p><b>Financial-condition grades for SEC filers</b></p></div>',
        nav_bar(page_link),
        f'<p class="stats"><span>Updated {escape(long_date(as_of))}</span><span>{summary.get("tickers", 0):,} SEC tickers</span><span>{summary.get("eligible", 0):,} eligible</span>'
        f'<span>{graded_count:,} graded</span>' + (f'<span>{rescans} rescans since {escape(long_date(first_rescan))}</span>' if first_rescan else "") + "</p>",
    ]
    picks = features(state)
    if picks:
        out.append('<div class="features">' + "".join(_feature(p, mcap, page_link) for p in picks) + "</div>")

    out.append('<div class="layout"><div>')
    # largest
    out.append('<h2 id="largest">The Largest Twenty <a class="top" href="#top">[top]</a></h2>')
    out.append('<p class="small">By market equity at the last close. Letter and percentile within the sector; lower is stronger.</p>')
    rows = "".join(
        f'<tr><td class="n">{i + 1}</td><td>{_company_cell(r, mcap.get(r["ticker"]), sd, page_link)}</td><td class="n">{_money(mcap[r["ticker"]])}</td>'
        f'<td class="n"><span class="lt">{escape(r["grade"])}</span></td><td class="n">{pct_points(r["percentile"])}</td><td class="n">{_mom_text(r)}</td><td class="w">{escape(_flags(r))}</td></tr>'
        for i, r in enumerate(largest(state))
    )
    out.append(f'<div class="scroll peers"><table class="data"><tr><th class="n">#</th><th>Company</th><th class="n">Market equity</th><th class="n">Letter</th><th class="n">Percentile</th><th class="n">12-1</th><th>Warnings</th></tr>{rows}</table></div>')

    # movers
    weaker, stronger = movers(state)
    if weaker or stronger:
        out.append(f'<h2 id="moves">Moved Most, {escape(long_date(year_before(as_of)))} to {escape(long_date(as_of))} <a class="top" href="#top">[top]</a></h2>')
        out.append('<p class="small">Market equity over $500M; letter changed over the year to the latest rescan. Letters held, in order.</p>')
        for title, group in (("Weaker", weaker), ("Stronger", stronger)):
            rows = "".join(
                f'<tr><td>{_company_cell(m["row"], mcap.get(m["row"]["ticker"]), sd, page_link)}</td><td>{escape(", ".join(m["path"]))}</td>'
                f'<td class="n">{m["delta"] * 100:+.0f}</td><td class="n">{pct_points(m["row"]["percentile"])}</td><td class="n">{_money(mcap[m["row"]["ticker"]])}</td><td class="w">{escape(_flags(m["row"]))}</td></tr>'
                for m in group
            )
            out.append(f'<div class="scroll peers"><table class="data"><tr><th colspan="6">{title}</th></tr><tr><th>Company</th><th>Letters</th><th class="n">Move</th><th class="n">Percentile</th><th class="n">Market equity</th><th>Warnings</th></tr>{rows}</table></div>')
    out.append('</div><div class="side">')

    # sidebar: letters legend
    out.append('<h2>The Letters <a class="top" href="#top">[top]</a></h2><div class="legend">')
    for letter, (name, label) in animals.LETTERS.items():
        src = animals.photo(name, 240)
        count = letters.get(letter, 0)
        boundary = letters.get(f"{letter}/{chr(ord(letter) + 1)}", 0)
        fifth = {"A": "strongest fifth", "B": "second fifth", "C": "middle fifth", "D": "fourth fifth", "E": "weakest fifth"}[letter]
        out.append((f'<img src="{src}" alt="{escape(label)}">' if src else "<span></span>") + f'<div><b>{letter}</b>, the {escape(label.lower())}: {fifth} of its sector. {count:,} today' + (f", plus {boundary:,} on the {letter}/{chr(ord(letter) + 1)} boundary" if boundary else "") + "</div>")
    out.append("</div>")

    # sidebar: warnings
    flags = summary.get("active_flags") or {}
    out.append('<h2 id="warnings">Warnings Today <a class="top" href="#top">[top]</a></h2>')
    rows = "".join(f'<tr><td><svg class="ic"><use href="#warn"/></svg>{escape(FLAG_LABELS.get(k, k))}</td><td class="n">{v:,}</td></tr>' for k, v in sorted(flags.items(), key=lambda kv: -kv[1]) if k not in ("fortress", "thin_data"))
    rows += "".join(f'<tr><td>{escape(FLAG_LABELS.get(k, k))}</td><td class="n">{v:,}</td></tr>' for k, v in flags.items() if k in ("fortress", "thin_data"))
    out.append(f'<div class="scroll"><table class="data"><tr><th>Check</th><th class="n">Filers</th></tr>{rows}</table></div>')
    out.append(f'<p class="small">Distress is the top decile of the failure model across the {summary.get("chs_resolved", 0)} filers where it resolves. Fortress means cash above debt. Thin data means fewer than 7 of 14 inputs and no letter.</p>')

    # sidebar: sectors
    out.append('<h2 id="sectors">Sectors <a class="top" href="#top">[top]</a></h2>')
    by_sector: dict[str, list[dict[str, Any]]] = {}
    for r in graded:
        by_sector.setdefault(r.get("sector") or "other", []).append(r)
    rows = ""
    for sector, rs in sorted(by_sector.items(), key=lambda kv: -len(kv[1])):
        moms = [m for m in (_momentum(r) for r in rs) if m is not None]
        best = best_ranked(rs) or rs[0]
        rows += (f'<tr><td>{escape(sector.replace("_", " "))}</td><td class="n">{len(rs)}</td><td class="n">{statistics.median(moms) * 100:+.0f}%</td>'
                 f'<td>{_company_cell(best, mcap.get(best["ticker"]), sd, page_link)}</td></tr>')
    out.append(f'<div class="scroll peers"><table class="data"><tr><th>Sector</th><th class="n">Graded</th><th class="n">Median 12-1</th><th>Best ranked</th></tr>{rows}</table></div>')
    out.append("</div></div>")

    # footer
    out.append(
        '<hr><div class="footer"><p class="small">Not investment advice. Informational and educational only. No adviser relationship. Data from SEC EDGAR, may contain errors, is not warranted. The grade ranks reported financial condition and is not a return forecast.</p>'
        + site_map(page_link)
        + credit_footer()
        + '<p class="small">If you have any comments about this page, the methodology and every line of code that produced it are in the public repository. However, due to the limited number of personnel, we are unable to provide a direct response.</p>'
        f'<p class="small">Updated {escape(long_date(as_of))}</p></div></div>' + TIP_SCRIPT
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="render the front page from an assay output tree")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--history", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--page-link", default="./")
    parser.add_argument("--list-featured", action="store_true")
    args = parser.parse_args(argv)
    state = load(args.data, args.history)
    if args.list_featured:
        print(" ".join(featured(state)))
        return 0
    html = render(state, args.page_link)
    if args.out:
        args.out.write_text(html, encoding="utf-8")
        print(args.out)
    else:
        print(html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
