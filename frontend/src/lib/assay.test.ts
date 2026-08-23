import assert from "node:assert/strict";
import test from "node:test";
import { loadCompanyLiveData, loadCompanyPage, revalueImpliedExpectations, searchCompanies, type IndexRow } from "./assay.ts";

const row = (ticker: string, name: string): IndexRow => ({
  ticker,
  name,
  cik: 1,
  exchange: "NYSE",
  status: "eligible",
  reason: null,
  sector: "other",
  sic: 1,
  sic_description: "Test",
  has_companyfacts: true,
  grade: "B",
  percentile: 0.3,
  composite: 0,
  peer_group: "other",
  sampling_standard_deviation: 0.02,
  coverage: { resolved: 14, wanted: 14 },
  active_flags: [],
  implied_expectations: {},
  artifact: `tickers/${ticker}.json`,
});

test("search gives exact and ticker-prefix matches priority", () => {
  const rows = [row("AAP", "Advance Auto Parts"), row("AAPL", "Apple Inc."), row("APLE", "Apple Hospitality")];
  assert.deepEqual(searchCompanies(rows, "AAPL").map((item) => item.ticker), ["AAPL"]);
  assert.deepEqual(searchCompanies(rows, "app").map((item) => item.ticker), ["AAPL", "APLE"]);
});

test("live price revalues only market denominators", () => {
  const result = revalueImpliedExpectations(
    {
      price: { close: 100 },
      market_equity: 1_000_000,
      debt: 200_000,
      cash: 100_000,
      ebit: 55_000,
      free_cash_flow: 50_000,
      book_equity: 400_000,
      sales: 800_000,
    },
    200,
    "2026-08-22T12:00:00Z",
  );
  assert.equal(result.status, "resolved");
  assert.equal(result.market_equity, 2_000_000);
  assert.equal(result.enterprise_value, 2_100_000);
  assert.equal(result.metrics.fcf_yield, 0.025);
  assert.equal(result.metrics.book_to_price, 0.2);
  assert.equal(result.metrics.sales_to_price, 0.4);
  assert.equal(result.metrics.ebit_to_enterprise_value, 55_000 / 2_100_000);
  assert.equal(result.feeds_grade, false);
  assert.equal(result.verdict, null);
});

test("live revaluation refuses incomplete baselines", () => {
  assert.deepEqual(revalueImpliedExpectations({}, 100), {
    status: "unresolved",
    reason: "missing_live_or_baseline_price",
    metrics: {},
    feeds_grade: false,
    verdict: null,
  });
});

test("company page loading keeps nightly values when the live quote fails", async () => {
  const originalFetch = globalThis.fetch;
  const report = {
    schema_version: 1,
    generated_at: "2026-08-22T00:00:00Z",
    as_of: "2026-08-21",
    status: "resolved",
    disclaimer: "Test",
    company: {
      ticker: "TEST",
      cik: 1,
      name: "Test Company",
      exchange: "NYSE",
      status: "eligible",
      reason: null,
      sector: "other",
      sic: 1,
      sic_description: "Test",
      has_companyfacts: true,
    },
    grade: {
      status: "resolved",
      grade: "B",
      percentile: 0.3,
      composite: 0,
      peer_count: 100,
      peer_group: "other",
      sampling_standard_deviation: 0.02,
      coverage: { resolved: 14, wanted: 14 },
      sleeve_scores: {},
      factor_percentiles: {},
      reason: null,
    },
    live_implied_expectations: {
      status: "resolved",
      as_of: "2026-08-21T20:00:00Z",
      price: 100,
      metrics: { fcf_yield: 0.04 },
      feeds_grade: false,
      predictive_claim: false,
      verdict: null,
    },
  };
  const artifact = (extra: Record<string, unknown>) =>
    new Response(JSON.stringify({ schema_version: 1, generated_at: report.generated_at, as_of: report.as_of, ...extra }));

  try {
    globalThis.fetch = async (input) => {
      const url = String(input);
      if (url.includes("/api/quote")) return new Response("upstream unavailable", { status: 502 });
      if (url.endsWith("/tickers/TEST.json")) return new Response(JSON.stringify(report));
      if (url.endsWith("/peer_groups/other.json")) {
        return artifact({ peer_group: "other", ticker_count: 1, distribution_curve: { status: "resolved", points: [] }, tickers: [] });
      }
      if (url.endsWith("/sectors/other.json")) {
        return artifact({ sector: "other", display_rules: { continuous: true, quadrants: false, verdict: null, predictive_claim: false }, peer_cloud: [], tickers: [] });
      }
      return new Response("not found", { status: 404 });
    };

    const page = await loadCompanyPage("test");
    const live = await loadCompanyLiveData(page.report);
    assert.equal(page.report.company.ticker, "TEST");
    assert.equal(page.peerGroup?.peer_group, "other");
    assert.equal(page.sector?.sector, "other");
    assert.equal(live.quote, null);
    assert.match(live.quoteError ?? "", /502/);
    assert.equal(live.liveImpliedExpectations?.metrics.fcf_yield, 0.04);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
