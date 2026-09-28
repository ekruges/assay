# Operations

How assay is laid out, built, run nightly, and published.

Fourteen accounting inputs in three sleeves are ranked within sector peer groups.
The letter is the fifth of the sector a company lands in: A is the strongest fifth,
E the weakest. No market price enters the letter. A company page shows each input's
value, its peer median, its percentile, and a numbered receipt that opens the SEC
filing the value was read from.

The grade ranks reported financial condition. It is not a return forecast.

## Layout

```text
assay/          the package, standard library only
  pipeline.py   all-ticker run: universe, facts, factors, grading, artifacts
  render.py     company page from a ticker artifact
  home.py       front page: features chosen by rule, largest twenty, the year's moves
  site.py       every other page and the whole-site build
  describe.py   the opening of Item 1 of each company's latest 10-K, cached by accession
  calendar.py   expected periodic filings from fiscal year end and filer category
  prices.py     twelve months of closes per ticker for the charts
  history.py    the grade change log carried between runs
  stratum.py    size bands, standard universe, and the reliability figures
  animals.py    photographs for the letters, features and sectors (assets/, credited)
  export.py     per-rescan records (slim index, inputs behind every letter), retention, the exports
  pdf.py        one PDF report per company, written with the standard library
  runlog.py     the nightly run record behind the public run log
config/         measured reliability figures with their windows and sources
functions/      Cloudflare Pages function for live quotes, optional
scripts/        nightly.sh, the whole run; homelab-nightly.sh, its unattended wrapper on a
                container; backfill_history.py
tests/          unittest suite
```

## Data flow

1. `python3 -m assay --all` reads the SEC bulk archives and the market cache,
   classifies every ticker, resolves point-in-time facts, computes inputs, grades
   each sector, and writes one artifact per ticker plus `index.json`,
   `universe.json`, `methodology.json`, sector and peer-group files. The verifier
   rebuilds every percentile and letter from the published receipts before the new
   tree replaces the old one.
2. `assay.history` records one change point per letter or size-band change and
   carries the log between runs.
3. `assay.prices`, `assay.describe` and `assay.calendar` produce the sidecars the
   pages need.
4. `assay.export --snapshot` records the rescan: a slim index and the inputs behind
   every letter, one file each per date, daily for 92 days and monthly before that.
   `assay.runlog` appends the run record.
5. `python3 -m assay.site --all` renders the front page, the method and screen
   pages, eleven sector pages, the time machine, the point-in-time reader, the run
   log, one page per eligible ticker with its downloads (PDF, artifact JSON, inputs
   and rescans as CSV), and a stub for every other SEC ticker stating why it is not
   graded. It also writes the exports and, with `--pdf`, one PDF report per graded
   company. `assay.prices --full` writes the whole price history per ticker for the
   scrollable chart.

## Commands

```bash
cp .env.example .env.local            # SEC user agent with a contact email; Alpaca keys
python3 -m unittest discover -s tests
python3 -m assay AAPL --as-of 2026-08-21
python3 -m assay --all --refresh --sync-market --output-dir data --json > run-summary.json
python3 -m assay --verify-output data --json
python3 -m assay.prices --data data --market-db .cache/market/market.sqlite3 --out prices.json
python3 -m assay.describe --cache-dir .cache/sec --data data --all --out descriptions.json
python3 -m assay.calendar --cache-dir .cache/sec --data data --as-of 2026-08-21 --out filing_calendar.json
python3 -m assay.site --data data --out site --all --history .state/grade_history.json \
  --history-index data/history/index --prices prices.json --descriptions descriptions.json \
  --calendar filing_calendar.json --asset-base img/ --pdf
python3 -m assay.export --data data --history-index .state/history --detail .state/detail --snapshot
python3 -m assay.prices --data data --market-db .cache/market/market.sqlite3 --full --out-dir site/prices
python3 -m assay.pdf --data data --ticker INTC --out INTC.pdf --prices prices.json --descriptions descriptions.json
```

`ASSAY_SITE_DIR` makes `scripts/nightly.sh` run the sidecars and the site build after
the pipeline and the verifier.

## Past rescans

`scripts/backfill_history.py` runs the pipeline for a list of past dates using only
facts filed by each date, keeps each date's `index.json`, and replays them through
the change-point recorder. The per-date index files feed the barometer, the
then-and-now page, the time machine and the exports. Rescans are monthly from
January 2012. Two caveats hold for backfilled dates: the ticker universe is
today's, so companies delisted since are absent from past peer groups, and the
packaged S&P 500 reference covers only dates within 120 days after its measurement,
so the failure-model input is unresolved further back.

## Exports and the point-in-time reader

`site/history/cube.json` holds every retained rescan as one letter and percentile
per ticker per date. `site/history/rescans.csv.gz` is the same in long format,
`site/index.csv` the current universe, `site/inputs.csv.gz` every input value with
its percentile and period end, and `site/assay.sqlite3.gz` the four tables
`universe`, `rescans`, `inputs` and `warnings`. Per company, `downloads/` holds the
inputs and the rescans as CSV beside the PDF and the artifact JSON.

`site/history/index/<date>.json` is the universe as it stood on that rescan and
`site/history/detail/<date>.json` the inputs, receipts and warnings behind every
letter that night. `as-of.html#TICKER~DATE` reads both in the browser and shows the
company as it was computed on that date, with its peers, its inputs, downloads cut
for that date, and a timeline to step between rescans. The time machine page reads
the cube and draws any date's universe or any ticker's path.

## Method in brief

- Point in time: only facts filed on or before the rescan date; missing data is
  never imputed. Across the current universe the lag from period end to SEC receipt
  is 57 days at the median and 90 at the 90th percentile.
- Grade: at least 7 of 14 inputs and a score in every sleeve; sectors need 100 or
  more graded companies; sleeves and inputs within a sleeve are equal-weighted; a
  boundary letter marks a percentile within one sampling standard deviation of a
  band edge.
- Failure model: the twelve-month probability from Campbell, Hilscher and Szilagyi
  (2008) is the one market-informed input. Reliability is measured out of sample on
  the outcome "8-K Item 1.03 within 365 days", per size band, and recorded in
  `config/reliability.json` with its windows; the within-band result awaits its
  pre-registered confirmation on 2026 filings.
- Warnings never enter the letter: dilution, short runway, late filer, distress,
  degenerate inputs, thin data, and the informational fortress flag.

## Unattended runs

`404.html` and `error.html` are the site's own error pages, built with absolute
links from `--site-root`; the web server maps them with `ErrorDocument`. A missing ticker page names the
ticker and offers the EDGAR company search for it.

`status.json` at the site root and `runs.html` show the latest run; a run that did
not complete is recorded with its log.

The front page features rotate: `.state/featured.jsonl` logs each day's picks, and a
company featured within the last 14 days yields to the next candidate unless it is
first (for the bear, last) in its sector that day.

## Nightly run

`scripts/homelab-nightly.sh` under a systemd timer at 03:00 America/New_York, on a
container with the caches on local disk. It runs `scripts/nightly.sh`, keeps the
output as the public log, records the run for the run log page, and publishes the
site over SSH with an atomic directory swap. `.env.local` holds
`ASSAY_SEC_USER_AGENT`, `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`; `ASSAY_SITE_DIR`,
`ASSAY_SITE_ROOT`, `ASSAY_PUBLISH_SSH` and `ASSAY_PUBLISH_DIR` name the output, the
served path, and the web host.

## Sources and credits

SEC EDGAR bulk XBRL archives; Alpaca end-of-day bars (SIP) and IEX snapshots; the
S&P 500 market value from S&P Dow Jones Indices for the failure model's size term.
Photographs are from Wikimedia Commons and are credited in `assay/assets/CREDITS.md`
and in the footer of every page that shows them.

Informational and educational only. Not investment advice.
