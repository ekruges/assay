# Assay

Nightly financial-condition grades for SEC filers: the pipeline in `assay/`, the
static site renderers, and the sidecars they read.

Read first: `docs/frontend-handoff.md` (copy rule, visual system) and
`docs/operations.md` (layout, data flow, commands, nightly and publishing).

## Company page

Company pages are static HTML rendered from the published ticker JSON by
`assay/render.py`, no scripts, no external requests. The front page comes from
`assay/home.py`: three features picked by stated rules (Bull of the Day, Bear of
the Day, Sleeper), the largest twenty, the year's biggest letter moves, a
letters legend, warnings and sectors. Each letter has an animal photograph
(A bull, B elk, C tortoise, D sloth, E bear) from `assay/assets`, credited in
`assay/assets/CREDITS.md` and in every footer.

```bash
python3 -m assay.render --data data --ticker INTC --out INTC.html --prices prices.json --history .state/grade_history.json --descriptions descriptions.json --page-link ./
python3 -m assay.home --data data --history .state/grade_history.json --out index.html
python3 -m assay.describe --cache-dir .cache/sec --tickers INTC --out descriptions.json
```

`assay.describe` lifts the opening of Item 1 from each company's latest 10-K,
cached by accession under `.cache/sec/descriptions`. `assay.pdf` writes the same
company report as a PDF with the standard library (built-in Times fonts, AFM
widths, link annotations to the receipts). `assay.export` records each rescan
(`--snapshot`: slim index plus the inputs behind every letter, daily for 92 days
and monthly before) and writes the rescan cube, the CSV files and the SQLite
database. `as-of.html` reads the per-date records in the browser and shows a
company as computed on any rescan, in time machine mode with its own downloads.
`assay.runlog` keeps the run record behind `runs.html`. `assay.prices --full`
writes the whole price history per ticker; the company page's price chart scrolls
through it with a lollipop at every rescan.

Style is the Berkshire Hathaway house style and nothing else: Times, navy text on
white, standard link colors, gray rules, small-caps headings, inline SVG charts
drawn with hairlines. Every figure carries a bracketed citation that links to the
SEC filing it was read from; the Sources table at the bottom lists every receipt
with filing, viewer and series links.

Copy rule: every sentence states a number with its source, defines a term, or
states a rule of the method. No first person, no fragments, no closers.

Non-negotiable product rules:

- The grade ranks reported financial condition. It is not a return forecast.
- A market price never changes the grade.
- Missing values stay missing and show their backend reason.
- Active warnings appear before the inputs.
- Show percentiles, peer medians, raw values, and receipts.
- Never label a metric quality, mispricing, misvaluation, intrinsic value,
  sentiment, or hype.
- No chart, animation, or UI library. No em dashes anywhere.

`functions/api/quote.js` is an optional Cloudflare Pages function for live quotes; company pages call it and fall back to the last close when it is absent.

## Data

`data/` is a symlink to the NAS tree and is never committed. The bulk SEC
archives and the market SQLite live on the NAS under `.cache/`; SQLite must be
copied to local disk before use. The grade change log carried between runs is
`.state/grade_history.json`, ignored by Git.

Before handing work back:

```bash
python3 -m unittest discover -s tests
```
