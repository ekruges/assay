const SYMBOL_PATTERN = /^[A-Z0-9.-]{1,15}$/;
const ALLOWED_FEEDS = new Set(["iex", "sip", "delayed_sip"]);

export async function onRequestGet(context) {
  const requestUrl = new URL(context.request.url);
  const ticker = (requestUrl.searchParams.get("ticker") || "").trim().toUpperCase();
  if (!SYMBOL_PATTERN.test(ticker)) {
    return jsonResponse({ error: "invalid_ticker" }, 400);
  }
  if (!context.env.APCA_API_KEY_ID || !context.env.APCA_API_SECRET_KEY) {
    return jsonResponse({ error: "market_data_not_configured" }, 503);
  }

  const feed = ALLOWED_FEEDS.has(context.env.ALPACA_LIVE_FEED)
    ? context.env.ALPACA_LIVE_FEED
    : "iex";
  const cacheKey = new Request(
    `${requestUrl.origin}/api/quote?ticker=${encodeURIComponent(ticker)}`,
  );
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  if (cached) return cached;

  const providerSymbol = ticker.replaceAll("-", ".");
  const upstreamUrl = new URL("https://data.alpaca.markets/v2/stocks/snapshots");
  upstreamUrl.searchParams.set("symbols", providerSymbol);
  upstreamUrl.searchParams.set("feed", feed);
  let upstream;
  try {
    upstream = await fetch(upstreamUrl, {
      headers: {
        Accept: "application/json",
        "APCA-API-KEY-ID": context.env.APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": context.env.APCA_API_SECRET_KEY,
      },
    });
  } catch {
    return jsonResponse({ error: "market_data_upstream_error" }, 502);
  }
  if (!upstream.ok) {
    return jsonResponse(
      { error: "market_data_upstream_error", status: upstream.status },
      502,
    );
  }
  let payload;
  try {
    payload = await upstream.json();
  } catch {
    return jsonResponse({ error: "market_data_upstream_error" }, 502);
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return jsonResponse({ error: "market_data_upstream_error" }, 502);
  }
  const snapshot = payload[providerSymbol];
  if (!snapshot) {
    return jsonResponse({ error: "snapshot_not_found", ticker }, 404);
  }

  const trade = snapshot.latestTrade || {};
  const quote = snapshot.latestQuote || {};
  const minute = snapshot.minuteBar || {};
  const previous = snapshot.prevDailyBar || {};
  const price = positiveNumber(trade.p) || positiveNumber(minute.c);
  const previousClose = positiveNumber(previous.c);
  const bid = positiveNumber(quote.bp);
  const ask = positiveNumber(quote.ap);
  const midpoint = bid && ask ? (bid + ask) / 2 : null;
  const response = jsonResponse(
    {
      ticker,
      status: price ? "resolved" : "unresolved",
      feed,
      price,
      price_timestamp: trade.t || minute.t || null,
      previous_close: previousClose,
      day_change: price && previousClose ? price - previousClose : null,
      day_change_percent:
        price && previousClose ? (price - previousClose) / previousClose : null,
      bid,
      ask,
      quoted_spread: bid && ask && ask >= bid ? ask - bid : null,
      quoted_spread_percent:
        bid && ask && ask >= bid ? (ask - bid) / midpoint : null,
      feeds_grade: false,
      cache_seconds: 15,
    },
    200,
    { "Cache-Control": "public, max-age=0, s-maxage=15" },
  );
  context.waitUntil(cache.put(cacheKey, response.clone()));
  return response;
}

function jsonResponse(value, status, extraHeaders = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...extraHeaders,
    },
  });
}

function positiveNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}
