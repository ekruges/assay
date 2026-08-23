# assay

`assay` turns SEC filings and market data into an evidence-linked view of a US
company's financial condition.

Every number keeps its source receipt. Missing data stays missing. Filing data is
limited by when it was filed, not just by the period it describes. The backend
never fills gaps with guesses.

The letter grade combines three equal-weight sleeves:

- Profitability: gross profitability, ROIC, accruals, and five-year margin stability.
- Solvency: CHS 12-month failure probability, Altman Z'', net debt/EBITDA,
  interest coverage, and current ratio.
- Growth and financing: revenue and free-cash-flow growth, reinvestment, asset
  growth, and split-adjusted net share issuance.

CHS is the one market-informed grade input. It estimates a non-return event,
corporate failure. Its coefficients were fitted on 1963-2003 data and are not
presented as a current calibration. Current price behavior and the implied-
expectations panel are separate from the letter grade.

## What the backend produces

An all-ticker run writes one complete static data tree:

```text
data/
  universe.json
  index.json
  methodology.json
  forecasts.json
  tickers/{TICKER}.json
  sectors/{SECTOR}.json
  sectors/index.json
  peer_groups/{GROUP}.json
  peer_groups/index.json
  audit/coverage.json
  audit/construction_sensitivity.json
```

The peer-group files contain normalized density-curve points for the site
animation. Sector files contain the continuous implied-expectations peer cloud.
They explicitly forbid quadrant labels and directional verdicts.

`forecasts.json` contains the append-only forecast events and their current
scorecard. An empty, versioned artifact is published before the first real call.

`functions/api/quote.js` is the Cloudflare Pages quote proxy. It keeps Alpaca
credentials server-side and caches each ticker for 15 seconds. The browser uses
that quote with the nightly accounting numerators to refresh EBIT/EV, FCF yield,
book/price, and sales/price. The grade stays fixed.

The frontend foundation is in `frontend/`. It contains the Svelte 5/Vite shell,
typed data client, live revaluation function, and a local link to the generated
tree. Product structure, copy, motion rules, required states, and deployment
settings are in `docs/frontend-handoff.md`.

The tree is built in a staging directory. It replaces the previous tree only
after the runner proves that every unique SEC ticker has an artifact. Invalid or
conflicting SEC mappings also get explicit unresolved artifacts.

## Inputs

- SEC nightly `companyfacts.zip`, `submissions.zip`, and `company_tickers.json`.
- Alpaca raw, split-adjusted, and total-return daily bars through the latest
  fully closed US session.
- Alpaca live snapshots. IEX is the default because it works on the free plan.
- S&P 500 constituent market value for CHS `RSIZE`.

The package includes a reference dated 2026-07-31, derived from S&P's published
503 constituents and mean market cap. It expires after 120 days. A newer value
must include its measurement date and source.

## Credentials

Copy `.env.example` to `.env.local`, then replace the placeholders. The local
file is ignored by Git and is loaded by `scripts/nightly.sh`.

```bash
cp .env.example .env.local
```

The SEC requires automated clients to identify themselves with real contact
information. Assay refuses an uncached SEC request without it.

If the nightly runner uses ephemeral storage, set `ASSAY_FORECAST_LOG` to a
durable mounted path. Forecast history must survive between runs.

## Commands

Inspect one ticker from cached or live inputs:

```bash
python3 -m assay AAPL --as-of 2026-08-21
```

Refresh source data, sync market history and live snapshots, then process every
SEC ticker:

```bash
python3 -m assay \
  --all \
  --refresh \
  --sync-market \
  --as-of 2026-08-21 \
  --output-dir data \
  --json > run-summary.json
```

Verify the published tree independently of its summary counters:

```bash
python3 -m assay --verify-output data --json
```

The verifier parses every ticker artifact, checks its exact one-to-one match with
the universe built from the SEC ticker file, recounts statuses and factor
coverage, and checks all sector and peer-group files. It also reproduces every
factor percentile, composite, peer percentile, sampling error, and letter from
the published receipts. Any analysis crash rejects the whole build. The same
verifier runs before the new tree replaces the old one and again in
`scripts/nightly.sh`.

`.github/workflows/nightly.yml` runs this at 03:00 America/New_York, restores
the market cache and forecast log, and force-publishes an orphan `data` branch.
Generated data never enters `main`. Configure the three credential names above
as GitHub Actions secrets before enabling the schedule.

For a newer S&P reference, pass all three fields together:

```bash
python3 -m assay --all \
  --sp500-market-value 67075482580000 \
  --sp500-market-value-as-of 2026-07-31 \
  --sp500-market-value-source "https://www.spglobal.com/spdji/en/indices/equity/sp-500/"
```

## Forecast ledger

Forecasts are the only part of assay allowed to make a return prediction. The
ledger is append-only JSONL. Resolution adds a new event instead of editing the
original call.

```bash
python3 -m assay.forecast call AAPL \
  --probability 0.62 \
  --horizon-days 90 \
  --thesis "One paragraph written before the outcome is known." \
  --grade-at-call B

python3 -m assay.forecast settle --as-of 2026-11-19
python3 -m assay.forecast score
```

The scorecard reports Brier score, log loss, an observed-base-rate reference,
and calibration bins. Outcomes are total-return outperformance against SPY.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q assay tests
node --test tests/test_quote.mjs
python3 -m pip wheel . --no-deps --wheel-dir /tmp/assay-wheels
cd frontend && npm test && npm run check && npm run build
```

The suite includes a 100-company system test. It resolves all 14 factors, all CHS
scores, all grades, all ticker artifacts, the peer cloud, and the precomputed
curve geometry. It also checks a frozen real Apple companyfacts response against
the 2025 10-K, including CHS inputs, all nine Piotroski components, cover-date
share handling, and discrete cash-flow reconstruction. Only the current live
all-ticker audit requires SEC and Alpaca credentials.

## Research boundaries

- Financials, REITs, OTC names, foreign issuers, funds, and non-operating filing
  classes are not graded.
- Piotroski F-Score is diagnostic only.
- The implied-expectations panel has no verdict and makes no predictive claim.
- Live snapshots do not move the letter grade.
- No investor-attention, market-psychology, or price-correctness measure exists.
- This is informational and educational software, not investment advice.

CHS follows [Campbell, Hilscher, and Szilagyi
(2008)](https://campbell.scholars.harvard.edu/sites/g/files/omnuum5881/files/campbell/files/campbellhilscherszilagyi_jf2008.pdf).
Market data endpoints follow [Alpaca's official stock-data
documentation](https://docs.alpaca.markets/us/reference/stocksnapshots-1).
SEC access follows the [SEC EDGAR API
guidance](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).
