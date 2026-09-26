"""The company's own description of its business, lifted from Item 1 of its latest 10-K.

EDGAR's structured data carries no description text, so the source is the filing's
primary document. The opening of Item 1 is kept, cut at a sentence boundary, and returned
with the document URL as its receipt.

    python3 -m assay.describe --cache-dir .cache/sec --tickers INTC LCID --out descriptions.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .sec import BulkSecStore, SecError

ANNUAL_FORMS = ("10-K", "10-KT", "20-F", "40-F")
MAX_CHARS = 700
ITEM_1 = re.compile(r"item\s*1\s*[\.\:\-–—]?\s*(?:business|description of business)\b", re.IGNORECASE)
ITEM_1A = re.compile(r"item\s*1a\b", re.IGNORECASE)


class _Text(HTMLParser):
    """Visible text of a filing, block elements separated by newlines, hidden iXBRL header skipped."""

    VOID = frozenset({"br", "img", "hr", "meta", "link", "input", "col", "area", "base", "wbr", "source"})
    BLOCK = frozenset({"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[bool] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        style = (dict(attrs).get("style") or "").replace(" ", "").lower()
        hidden = tag in ("script", "style", "ix:header") or "display:none" in style
        if tag not in self.VOID:
            self.stack.append(hidden)
            if hidden:
                self.skip += 1
        if tag in self.BLOCK and not self.skip:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID:
            return
        if self.stack and self.stack.pop():
            self.skip = max(0, self.skip - 1)
        if tag in self.BLOCK and not self.skip:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts).replace("\xa0", " ")
        raw = re.sub(r"[ \t]+", " ", raw)
        return re.sub(r"\n\s*\n+", "\n", raw)


def business_overview(html: str, max_chars: int = MAX_CHARS) -> str | None:
    """Opening of Item 1. The table of contents also says Item 1, so the longest span before Item 1A wins."""
    parser = _Text()
    parser.feed(html)
    text = parser.text()
    best = ""
    for match in ITEM_1.finditer(text):
        start = match.end()
        stop = ITEM_1A.search(text, start)
        span = text[start: stop.start() if stop else start + 20000]
        if len(span) > len(best):
            best = span
    if len(best) < 200:
        best = _after_overview_heading(text)
    if len(best) < 200:
        return None
    lines = [line.strip() for line in best.split("\n")]
    paragraphs = [line for line in lines if len(line) >= 60 and not line.lower().startswith(("table of contents", "part i"))]
    if not paragraphs:
        return None
    body = " ".join(paragraphs)
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) <= max_chars:
        return body
    cut = body[:max_chars]
    end = max(cut.rfind(". "), cut.rfind(".” "), cut.rfind("? "))
    return (cut[: end + 1] if end > 200 else cut[: max_chars - 3].rstrip() + "...").strip()


OVERVIEW_HEADINGS = frozenset({"overview", "our business", "business overview", "company overview", "our company", "business", "general"})


def _after_overview_heading(text: str) -> str:
    """Filings without an Item 1 body: the first overview-style heading followed by a real paragraph."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower().rstrip(":") not in OVERVIEW_HEADINGS:
            continue
        following = [item.strip() for item in lines[i + 1: i + 4] if item.strip()]
        if following and len(following[0]) >= 200:
            return "\n".join(lines[i + 1: i + 60])
    return ""


def latest_annual(submissions: dict[str, Any]) -> dict[str, Any] | None:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    for i, form in enumerate(forms):
        if form in ANNUAL_FORMS:
            return {
                "form": form,
                "filed": recent["filingDate"][i],
                "accession": recent["accessionNumber"][i],
                "primary_document": (recent.get("primaryDocument") or [None] * len(forms))[i],
            }
    return None


def document_url(cik: int, accession: str, primary_document: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{primary_document}"


def fetch(url: str, user_agent: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def describe(cik: int, submissions: dict[str, Any], user_agent: str, cache_dir: Path) -> dict[str, Any]:
    filing = latest_annual(submissions)
    if not filing or not filing.get("primary_document"):
        return {"status": "unresolved", "reason": "no_annual_report_on_record"}
    cache = cache_dir / "descriptions" / f"{filing['accession']}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    url = document_url(cik, filing["accession"], filing["primary_document"])
    try:
        html = fetch(url, user_agent)
    except OSError as exc:
        return {"status": "unresolved", "reason": "fetch_failed", "detail": str(exc), "source_url": url}
    text = business_overview(html)
    record = {
        "status": "resolved" if text else "unresolved",
        "reason": None if text else "item_1_not_found",
        "text": text,
        "form": filing["form"],
        "filed": filing["filed"],
        "accession": filing["accession"],
        "source_url": url,
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="lift each company's Item 1 opening from its latest annual report")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/sec"))
    parser.add_argument("--tickers", nargs="*")
    parser.add_argument("--data", type=Path, help="with --all: the output tree whose eligible tickers are described")
    parser.add_argument("--all", action="store_true", help="every eligible ticker in --data/index.json")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--user-agent", default=os.environ.get("ASSAY_SEC_USER_AGENT"))
    args = parser.parse_args(argv)
    if not args.user_agent:
        parser.error("set ASSAY_SEC_USER_AGENT or pass --user-agent with a real contact email")
    if args.all:
        index = json.loads(((args.data or Path("data")) / "index.json").read_text(encoding="utf-8"))
        wanted = [r["ticker"] for r in index["tickers"] if r.get("status") == "eligible"]
    elif args.tickers:
        wanted = args.tickers
    else:
        parser.error("pass --tickers or --all")
    tickers = json.loads((args.cache_dir / "company_tickers.json").read_text(encoding="utf-8"))
    by_ticker = {str(row["ticker"]).upper(): int(row["cik_str"]) for row in tickers.values()}
    results: dict[str, Any] = {}
    if args.out.exists():
        results = json.loads(args.out.read_text(encoding="utf-8"))
    with BulkSecStore(args.cache_dir / "bulk" / "companyfacts.zip", args.cache_dir / "bulk" / "submissions.zip") as store:
        for ticker in [t.upper() for t in wanted]:
            if results.get(ticker, {}).get("status") == "resolved" and not args.tickers:
                continue
            cik = by_ticker.get(ticker)
            if cik is None:
                results[ticker] = {"status": "unresolved", "reason": "unknown_ticker"}
                continue
            try:
                submissions = store.submissions(cik)
            except SecError as exc:
                results[ticker] = {"status": "unresolved", "reason": "missing_submissions", "detail": str(exc)}
                continue
            results[ticker] = describe(cik, submissions, args.user_agent, args.cache_dir)
            print(ticker, results[ticker]["status"], (results[ticker].get("text") or results[ticker].get("reason") or "")[:80], file=sys.stderr)
            time.sleep(0.15)
    args.out.write_text(json.dumps(results, indent=1, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
