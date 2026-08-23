import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";
import { resolve } from "node:path";
import { revalueImpliedExpectations, type IndexArtifact, type TickerReport } from "./assay.ts";

const data = resolve(import.meta.dirname, "../../../data");
const read = async <T>(path: string): Promise<T> =>
  JSON.parse(await readFile(resolve(data, path), "utf8")) as T;
const ticker = (symbol: string) => read<TickerReport>(`tickers/${symbol}.json`);

test("the current tree contains every indexed ticker artifact", async () => {
  const index = await read<IndexArtifact>("index.json");
  const files = (await readdir(resolve(data, "tickers"))).filter((name) => name.endsWith(".json"));
  assert.equal(index.tickers.length, 10_403);
  assert.equal(files.length, index.tickers.length);
  assert.equal(index.summary.tickers, index.tickers.length);
});

test("every route-level frontend artifact is versioned and available", async () => {
  const paths = [
    "universe.json",
    "methodology.json",
    "peer_groups/index.json",
    "sectors/index.json",
    "audit/coverage.json",
    "audit/construction_sensitivity.json",
    "forecasts.json",
  ];
  const artifacts = await Promise.all(paths.map((path) => read<{ schema_version: number }>(path)));
  assert.ok(artifacts.every((artifact) => artifact.schema_version === 1));
});

test("the real UI state catalog remains available", async () => {
  const [nvda, apple, amazon, alcoa, oracle, hood, arcc, lucid, streamex, alphabet, archerWarrant] =
    await Promise.all(["NVDA", "AAPL", "AMZN", "AA", "ORCL", "HOOD", "ARCC", "LCID", "STEX", "GOOGL", "ACHR-WT"].map(ticker));

  assert.equal(nvda.grade?.grade, "A");
  assert.ok(nvda.flags?.some((flag) => flag.name === "fortress" && flag.active));
  assert.equal(apple.grade?.grade, "B");
  assert.equal(amazon.grade?.grade, "A/B");
  assert.equal(alcoa.grade?.status, "unresolved");
  assert.ok(alcoa.flags?.some((flag) => flag.name === "thin_data" && flag.active));
  assert.equal(oracle.grade?.status, "unresolved");
  assert.equal(hood.company.status, "excluded");
  assert.equal(hood.company.reason, "financial_or_reit");
  assert.equal(arcc.company.status, "unresolved");
  assert.ok(lucid.flags?.some((flag) => flag.name === "distress" && flag.active));
  assert.ok(lucid.flags?.some((flag) => flag.name === "short_runway" && flag.active));
  assert.equal(streamex.grade?.grade, "B");
  assert.equal(streamex.coverage?.resolved, 7);
  assert.ok(streamex.flags?.some((flag) => flag.name === "degenerate_inputs" && flag.active));
  assert.equal(alphabet.market_scope?.reason, "multiple_share_classes");
  assert.equal((archerWarrant.market_snapshot as { status?: string })?.status, "unresolved");
});

test("frontend live revaluation agrees with the published Python result", async () => {
  const apple = await ticker("AAPL");
  const quote = apple.market_snapshot as { price?: number; price_timestamp?: string };
  const published = apple.live_implied_expectations as {
    market_equity: number;
    enterprise_value: number;
    metrics: Record<string, number>;
  };
  const result = revalueImpliedExpectations(
    apple.implied_expectations!.live_inputs,
    quote.price!,
    quote.price_timestamp!,
  );

  assert.equal(result.status, "resolved");
  assertClose(result.market_equity!, published.market_equity);
  assertClose(result.enterprise_value!, published.enterprise_value);
  for (const [name, value] of Object.entries(published.metrics)) {
    assertClose(result.metrics[name]!, value);
  }
  assert.equal(result.feeds_grade, false);
});

test("grade curve and peer cloud contain the focal company", async () => {
  const apple = await ticker("AAPL");
  const grade = apple.grade!;
  const peer = await read<{
    distribution_curve: { domain: [number, number] };
    tickers: Array<{ ticker: string }>;
  }>(`peer_groups/${grade.peer_group}.json`);
  const sector = await read<{
    display_rules: { quadrants: boolean; verdict: unknown; predictive_claim: boolean };
    peer_cloud: Array<{ ticker: string; financial_condition_percentile: number }>;
  }>(`sectors/${apple.company.sector}.json`);
  const [low, high] = peer.distribution_curve.domain;
  const marker = (grade.composite! - low) / (high - low);

  assert.ok(marker >= 0 && marker <= 1);
  assert.ok(peer.tickers.some((row) => row.ticker === "AAPL"));
  assert.equal(
    sector.peer_cloud.find((row) => row.ticker === "AAPL")?.financial_condition_percentile,
    grade.percentile,
  );
  assert.deepEqual(sector.display_rules, { continuous: true, predictive_claim: false, quadrants: false, verdict: null });
});

function assertClose(actual: number, expected: number): void {
  assert.ok(Math.abs(actual - expected) <= Math.max(1, Math.abs(expected)) * 1e-12, `${actual} != ${expected}`);
}
