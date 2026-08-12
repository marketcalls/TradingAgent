# 002 - OpenAlgo client layer

**Date:** 2026-08-12
**Plan step:** Part 9, step 2
**Status:** complete, 43/43 checks passing

## What went in

| Path | Purpose |
|---|---|
| `backend/app/openalgo/constants.py` | Exchanges, products, price types, actions, GTT subsets, option offsets, symbology normalizers |
| `backend/app/openalgo/normalize.py` | One response envelope for every tool, NaN-safe JSON, size cap, candle-frame summarizer |
| `backend/app/openalgo/client.py` | Shared SDK client, rate limiting, `raw_post` for unwrapped endpoints, async offload |
| `backend/app/openalgo/frames.py` | Cleaned OHLCV frame cache with TTL and LRU bound |
| `backend/tests/test_client_layer.py` | 43 checks, pure-logic plus live |

## Design decisions

**One shared client, never on the event loop.** The SDK is fully synchronous - `httpx.Client`
plus OS threads for the feed. A single instance is reused because `httpx.Client` pools
connections and is thread-safe; `acall()` offloads to a worker thread so a broker round trip
never stalls FastAPI. Timeout is 15s, not the SDK's 120s default.

**`raw_post` strips leading slashes.** `BaseAPI.base_url` is `f"{host}/api/{version}/"` and
`_make_request` does a bare concat, so `"/ping"` builds `/api/v1//ping` and 308-redirects into
an opaque error string. The test asserts both `ping` and `/ping` succeed, so this cannot
regress. This matters because all four GTT tools have no SDK wrapper and depend on this path.

**Envelope collapses four real response shapes.** OpenAlgo is not uniform: `{status, data}` for
reads, flat `{status, orderid}` for order writes, `{status, results: [...]}` for batch writes,
and `{status, message}` for errors. Every tool now sees `{ok, source, data, error, mode}`.
The `mode` field is carried through so the UI can always tell live from simulated.

**Frames are cleaned once, at the boundary.** openalgo's `_backend` sma/rolling_sum are
`np.cumsum` based, so one NaN poisons everything after it. Every frame is numeric-coerced,
`dropna`'d on OHLC, sorted, and de-duplicated before it reaches an indicator. This is a private
60s TTL cache, deliberately not `Toolkit(cache_results=True)` - that one writes JSON to disk and
survives restarts, which is wrong for market data.

**Rate limiting is client-side.** Token buckets at 40 req/s general and 8 req/s for orders, under
OpenAlgo's documented 50/s and 10/s.

## Validation output

```
=== client (live) ===
  [PASS] ping via raw_post - broker=zerodha
  [PASS] raw_post strips leading slash - no 308
  [PASS] analyzer_mode - mode=analyze
  [PASS] quotes enveloped - ltp=1329
  [PASS] ltp helper - 1082.0
  [PASS] bad symbol returns ok=false, no raise
  [PASS] missing kwarg becomes an error dict - TypeError
  [PASS] raw_post reaches unwrapped endpoint - ok=True

=== frames (live) ===
  [PASS] frame fetched - 651 bars, cols=['close','high','low','oi','open','volume']
  [PASS] frame has no NaN in OHLC
  [PASS] second fetch hits cache
  [PASS] ta.rsi on cleaned frame is mostly finite - 637/651 finite

  43 passed, 0 failed, 0 skipped
```

Confirmed again from live data: `history` really does return six columns including `oi`.

## Notable behaviour confirmed

- A bad symbol returns `HTTP 400 ... not found for exchange` as an error **dict**, never an
  exception. The envelope turns it into `ok=false` so tools can raise `RetryAgentRun` with an
  actionable message instead of crashing.
- A missing required kwarg is one of the few things the SDK genuinely raises (`TypeError`);
  `call_enveloped` catches it and reports `kind="TypeError"`.

## Next

Iteration 3: `backend/app/safety/` - RiskGuard and AuditLog - with unit tests for every limit,
duplicate suppression, and the kill switch. The indicator registry is being built in parallel.
