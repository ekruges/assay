import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../functions/api/quote.js", import.meta.url));
const moduleUrl = `data:text/javascript;base64,${source.toString("base64")}`;
const { onRequestGet } = await import(moduleUrl);

let cachedResponse = null;
let fetchCount = 0;
let requestedUrl = null;
globalThis.caches = {
  default: {
    async match() {
      return cachedResponse ? cachedResponse.clone() : undefined;
    },
    async put(_key, response) {
      cachedResponse = response.clone();
    },
  },
};
globalThis.fetch = async (url) => {
  fetchCount += 1;
  requestedUrl = String(url);
  return new Response(
    JSON.stringify({
      "BRK.B": {
        latestTrade: { t: "2026-08-21T15:00:00Z", p: 600 },
        latestQuote: { bp: 599, ap: 601 },
        minuteBar: { c: 600 },
        prevDailyBar: { c: 590 },
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
};

const pending = [];
const context = {
  request: new Request("https://assay.example/api/quote?ticker=BRK-B"),
  env: {
    APCA_API_KEY_ID: "key",
    APCA_API_SECRET_KEY: "secret",
    ALPACA_LIVE_FEED: "iex",
  },
  waitUntil(promise) {
    pending.push(promise);
  },
};
const first = await onRequestGet(context);
const payload = await first.json();
await Promise.all(pending);

assert.equal(first.status, 200);
assert.equal(payload.ticker, "BRK-B");
assert.equal(payload.price, 600);
assert.equal(payload.previous_close, 590);
assert.equal(payload.quoted_spread, 2);
assert.equal(payload.feeds_grade, false);
assert.match(first.headers.get("cache-control"), /s-maxage=15/);
assert.match(requestedUrl, /symbols=BRK.B/);

const second = await onRequestGet({ ...context, waitUntil() {} });
assert.equal(second.status, 200);
assert.equal(fetchCount, 1);

const invalid = await onRequestGet({
  ...context,
  request: new Request("https://assay.example/api/quote?ticker=../../bad"),
});
assert.equal(invalid.status, 400);

const unconfigured = await onRequestGet({
  ...context,
  env: {},
  request: new Request("https://assay.example/api/quote?ticker=AAPL"),
});
assert.equal(unconfigured.status, 503);

cachedResponse = null;
globalThis.fetch = async () => new Response("not json", { status: 200 });
const malformed = await onRequestGet({
  ...context,
  request: new Request("https://assay.example/api/quote?ticker=AAPL"),
  waitUntil() {},
});
assert.equal(malformed.status, 502);
assert.deepEqual(await malformed.json(), { error: "market_data_upstream_error" });

globalThis.fetch = async () => {
  throw new Error("network unavailable");
};
const unavailable = await onRequestGet({
  ...context,
  request: new Request("https://assay.example/api/quote?ticker=MSFT"),
  waitUntil() {},
});
assert.equal(unavailable.status, 502);
assert.deepEqual(await unavailable.json(), {
  error: "market_data_upstream_error",
});

globalThis.fetch = async () => new Response("rate limited", { status: 429 });
const rateLimited = await onRequestGet({
  ...context,
  request: new Request("https://assay.example/api/quote?ticker=NVDA"),
  waitUntil() {},
});
assert.equal(rateLimited.status, 502);
assert.deepEqual(await rateLimited.json(), {
  error: "market_data_upstream_error",
  status: 429,
});

globalThis.fetch = async () =>
  new Response("{}", {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
const absent = await onRequestGet({
  ...context,
  request: new Request("https://assay.example/api/quote?ticker=TSLA"),
  waitUntil() {},
});
assert.equal(absent.status, 404);
assert.deepEqual(await absent.json(), {
  error: "snapshot_not_found",
  ticker: "TSLA",
});
