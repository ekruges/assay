# Assay frontend handoff

Status: ready for product UI work against backend data dated 2026-08-21.

This is the authoritative frontend brief. `SPEC-v2.md` remains the research and
methodology source, but one later product decision overrides its old
"accounting-only" sentence: the grade now contains thirteen accounting factors
and one market-informed corporate-failure estimate, CHS. A current quote never
moves the grade.

## Product promise

Use this wording as the baseline:

> Assay ranks a company's reported financial condition against comparable US
> companies. The grade combines reported accounting evidence with a
> market-informed corporate-failure estimate. It is not a return forecast.

The separate implied-expectations display shows how today's price relates to the
latest reported evidence. It has no verdict and never changes the grade.

Never use these words as product labels: quality, mispricing, misvaluation,
intrinsic value, sentiment, or hype. They make claims the system does not measure.
"Hype versus reality" can describe the product idea in conversation, but it is
not a metric name.

## Current design direction

The UI should look like a restrained research product, not a dashboard template.

- Use a dark navy or charcoal theme, not pure black.
- Use rectangular sections, hairline rules, and a small number of surfaces.
- The main grade and all three reasoning sleeves use one shared, segmented A-E
  half-dial. Left is stronger; right is weaker. Print the percentile so color is
  never the only signal.
- Put a peer-median tick at percentile 50 on every dial. State the point
  difference from the median below it.
- Do not invent official sleeve letter grades. The sleeves expose percentiles,
  raw values, and their contribution to the final letter.
- Keep headlines restrained. Let grades, comparisons, charts, and evidence carry
  the page.
- Use Twemoji for small pattern indicators. Emoji are secondary labels, not the
  main evidence.
- No glass panels, gradients, decorative particles, fake terminal copy, generic
  AI language, or a separate card around every number.
- Live charts are data displays, not wallpaper. Motion must explain a changed
  value or rank.

Current direction reference:
`/Users/ezrakruger/.codex/visualizations/2026/08/21/01a0261c-9b8c-7ac2-aafb-bc24a9c79ea4/assay-grade-reasoning.html`.

## Information architecture

The first UI pass needs five routes and no router dependency:

- `/`: product claim, search, current as-of date, and one restrained example.
- `/stocks/{ticker}`: the complete company page.
- `/methodology`: factors, formulas, CHS caveat, construction sensitivity, and
  data provenance.
- `/coverage`: universe inclusion, refusal reasons, factor failure counts, and
  coverage by sector and size bucket.
- `/forecasts`: append-only calls, resolutions, Brier score, log loss, and
  calibration. Empty state is acceptable until a real call exists.

The URL is the state. Search and metric selection may use query parameters. Do
not add global state management.

## Company page hierarchy

Render in this order:

1. Company identity, exchange, ticker, sector, filing as-of date, and live price.
2. Active warning flags. Distress, degenerate inputs, short runway, late filing,
   dilution, and thin evidence appear before the grade. Fortress is a separate
   positive balance-sheet fact, never a safety verdict.
3. Grade dial, peer count, coverage, sampling standard deviation, and as-of date.
   Lower peer percentile means stronger reported financial condition.
4. Three reasoning dials: profitability, solvency, and growth and financing. Use
   the same segmented A-E percentile scale, peer-median tick, comparison text,
   and raw factor values under every dial.
5. Peer comparison: the full distribution curve and direct percentile-versus-
   median comparisons.
6. Implied expectations: continuous peer scatter and metric selector.
7. Diagnostics: CHS, Piotroski components, cash runway, late filings, and median
   dollar trading volume.
8. Evidence drawer: factors, raw facts, failure reasons, resolved tags, filed
   dates, accession numbers, and filing links.
9. Disclaimer and data-source note.

The letter is a summary. Flags, low coverage, and missing evidence must remain
visible when the letter is on screen. This matters for real cases such as STEX,
which has a B at the seven-factor minimum alongside distress, short runway, late
filings, dilution, and degenerate-input warnings.

## Grade dial and peer curve

The half-dials use the published percentiles directly. Split the 0-100 scale into
five visible bands: A 0-20, B 20-40, C 40-60, D 60-80, and E 80-100. The main
marker uses `grade.percentile`; each sleeve marker uses the corresponding value
in `grade.sleeve_scores`. The backend values are 0-1, so multiply by 100 only for
display.

Use `peer_groups/{grade.peer_group}.json` for the deeper distribution display.

- `distribution_curve.points[].x` and `.y` are already normalized to 0 through 1.
- The focal marker is placed by mapping `grade.composite` through
  `distribution_curve.domain`.
- The displayed rank is `grade.percentile`.
- Sampling error is `grade.sampling_standard_deviation`. Display 0.022 as
  "±2.2 percentile points," not as a probability.
- Boundary grades such as B/C are exact backend output. Never collapse them.
- The grade, curve, and peer set stay fixed intraday.

Suggested sequence, all complete within 800 ms:

- 0-120 ms: labels and curve frame fade in.
- 80-520 ms: SVG curve draws with stroke dash offset.
- 220-680 ms: focal marker moves only with `transform`.
- 300-740 ms: sleeve summaries stagger by 70 ms.

With `prefers-reduced-motion: reduce`, show the final state immediately. Pause
ambient work when `document.visibilityState !== "visible"`.

## Implied expectations

Use `sectors/{company.sector}.json`.

- Y axis: `financial_condition_percentile`.
- X axis: one selected metric from `peer_cloud[].metrics`.
- Show the whole peer cloud plus one labeled focal marker.
- Do not draw or name quadrants.
- Do not use a good-to-bad color ramp.
- Do not print a verdict, target, or fair value.
- State the reading rule in words: the chart shows the financial-condition rank
  and the selected price-linked measure together so the viewer can inspect the
  gap against peers.
- Show the selected metric's arithmetic and receipts below the chart.

The live quote updates only EBIT/EV, FCF yield, book/price, and sales/price. Use
`revalueImpliedExpectations` from `frontend/src/lib/assay.ts`. The peer cloud and
all price-history metrics remain on the nightly close. Animate the focal X
position with a spring; do not redraw the cloud and do not animate the grade.

The ambient/background motion should also carry information. Reuse faint peer
points, curve traces, and live-marker movement. Do not add generic particles,
stock tickers flying past, or decorative candlesticks.

## Data loading

All generated paths are under `/data` in the built site:

| Need | Artifact |
| --- | --- |
| Search and overview | `/data/index.json` |
| Inclusion universe | `/data/universe.json` |
| Company page | `/data/tickers/{TICKER}.json` |
| Grade curve | `/data/peer_groups/{GROUP}.json` |
| Implied-expectations cloud | `/data/sectors/{SECTOR}.json` |
| Methodology | `/data/methodology.json` |
| Coverage | `/data/audit/coverage.json` |
| Construction sensitivity | `/data/audit/construction_sensitivity.json` |
| Forecast events and scorecard | `/data/forecasts.json` |
| Live quote | `/api/quote?ticker={TICKER}` |

Load `index.json` after the search is visible, then cache it for the session. Load
the company, peer group, and sector in parallel after ticker selection. A normal
company page needs three static requests and one non-blocking live request.

Use `frontend/src/lib/assay.ts` instead of writing request code in components:

```ts
import {
  loadCompanyPage,
  loadCompanyLiveData,
  loadIndex,
  loadMethodology,
  loadCoverageAudit,
  loadConstructionSensitivity,
  loadForecasts,
} from "./lib/assay";
```

`loadCompanyPage(ticker)` returns the company report, peer curve, and sector
cloud. Render those first. Then call `loadCompanyLiveData(page.report)` for the
live quote and revalued price-linked metrics. A quote failure is returned as
`quote: null` plus `quoteError`; the nightly report remains usable.

Do not wait for the quote before rendering. Start with the nightly snapshot. If
the quote fails, retain the nightly values and say "live quote unavailable."

## Field rules

- `company.status` controls eligible, excluded, and unresolved screens.
- `grade.status` controls resolved versus refused grade. Never infer from the
  presence of a letter alone.
- Factor raw values live in `factors[]`. Their peer ranks live separately in
  `grade.factor_percentiles[factor.name]`.
- Every unresolved factor carries a reason, detail, and failure class. Show them.
- Every resolved fact receipt includes `filed`, `end`, `tag`, `accession`, and
  `filing_url`. Lead with `filed` because point-in-time handling is based on when
  the SEC received the filing.
- CHS is a conditional month-12 failure probability, not a cumulative annual
  default probability. Always show: "Fitted on 1963-2003 data; not a current
  calibration."
- `feeds_grade: false` is an invariant on live and implied-expectations data.

## Required states and real fixtures

Use these current artifacts while building components:

| State | Ticker |
| --- | --- |
| Fully resolved A with fortress | NVDA |
| Strong company with B and live expectations | AAPL |
| Boundary grade | AMZN |
| Thin-data refusal | AA |
| Eligible but unresolved grade | ORCL |
| Excluded financial | HOOD |
| Universe classification unresolved | ARCC |
| Distress and short runway | LCID |
| Grade/flag conflict at minimum coverage | STEX |
| Multiple share classes | GOOGL |
| Missing live snapshot | ACHR-WT |

No page is complete until all eleven states render without an exception.

## Formatting

- Use tabular numerals everywhere numbers can change.
- Percentiles: whole number normally, one decimal near grade boundaries.
- Sampling error: one decimal percentile point.
- Ratios such as ROIC and FCF yield: percent, one decimal normally; add precision
  below 1%.
- CHS: enough decimals to avoid turning a small resolved probability into zero.
- Currency: compact on summary cards, exact in evidence rows.
- Missing values: "Not resolved," followed by the backend reason. Never use zero.
- Dates: readable local date in the UI, exact ISO value in evidence details.
- Never use color as the only carrier of grade, flag, status, or live change.

## Accessibility and responsive behavior

- Every chart needs a text summary and a table fallback.
- SVG markers and curves are decorative when the same values are printed. Hide
  those SVG elements from assistive technology.
- All drawers, selectors, search results, and filing links are keyboard usable.
- Minimum target size is 44 by 44 CSS pixels.
- Focus is visible and never trapped in a closed drawer.
- Announce quote refreshes politely, but do not announce every animated frame.
- At narrow widths, stack the grade and implied-expectations panels. Preserve the
  same reading order as the desktop page.
- Test at 320 px, 768 px, 1024 px, and a wide desktop.

## Performance budget

- No animation library. Use SVG, CSS transforms and opacity, Svelte `Spring` or
  `Tween`, and `requestAnimationFrame` only for displayed counters.
- No chart library. The backend already emits curve geometry and peer-cloud rows.
- Do not animate layout properties.
- Do not fetch all ticker artifacts.
- Lazy-load evidence receipts and methodology charts.
- The current static tree has 10,431 files. Its largest file is 5.96 MB, below
  Cloudflare Pages' 25 MiB per-file limit, and the file count is below the Free
  plan's 20,000-file limit as checked on 2026-08-22.

## Local and production builds

Local:

```bash
cd frontend
npm install
npm run dev
```

The pre-script links the ignored repository `data/` tree into `public/data`.
Use `VITE_ASSAY_DATA_URL` only when the static data lives on another origin. Use
`VITE_ASSAY_API_URL` only when the quote function lives on another origin. Both
default to the same origin in production.

Cloudflare Pages settings:

- Root directory: repository root.
- Build command: `npm --prefix frontend ci && npm --prefix frontend run build`.
- Output directory: `frontend/dist`.
- Production branch: `main`.
- Environment: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, and
  `ALPACA_LIVE_FEED=iex` for the quote function.

Before that build command runs, the orphan `data` branch must be restored as the
repository's ignored `data/` directory. The cleanest final deployment path is a
GitHub Action that checks out `main`, checks out `data` into `data/`, builds once,
and deploys `frontend/dist` plus the root `functions/` directory. Do not connect
Cloudflare's automatic main-branch build until that restore step exists.

Cloudflare's free Pages limits currently fit the site, but only by about 9,500
files. Recheck before adding generated per-company assets.

## UI completion gate

The UI phase is done only when:

1. All eleven real states above pass component and browser tests.
2. The grade curve marker agrees with the ticker composite and peer domain.
3. Live revaluation matches the nightly Python formula for all four metrics.
4. Quote failure leaves the nightly page intact.
5. No quadrant, verdict, banned term, or live-moving grade exists.
6. Reduced motion removes every nonessential transition.
7. Keyboard, screen-reader text, narrow layouts, and contrast have been checked.
8. A production bundle contains every current ticker artifact and stays within
   hosting limits.
9. The published site passes the backend verifier for the same as-of date shown
   in the UI.
