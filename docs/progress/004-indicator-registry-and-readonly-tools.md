# 004 - Indicator registry and the read-only toolkits

**Date:** 2026-08-12
**Plan steps:** Part 9, steps 4 and 5
**Status:** complete - registry 21/21, toolkits 32/32

## What went in

| Path | Purpose |
|---|---|
| `backend/app/indicators/registry.py` | All 127 `ta` callables as typed specs |
| `backend/tests/test_registry.py` | 21 checks, including execution of every indicator |
| `backend/app/tools/market.py` | MarketDataTools - 6 tools |
| `backend/app/tools/symbols.py` | SymbolTools - 4 tools |
| `backend/app/tools/account.py` | AccountTools - 6 tools |
| `backend/app/tools/system.py` | SystemTools - 5 tools |
| `backend/tests/test_tools_readonly.py` | 32 checks: registration, schemas, confirmation flags, live calls |

## The indicator registry

127 specs: trend 20, momentum 9, volatility 15, volume 17, oscillators 19, statistical 9,
hybrid 7, TA-Lib extras 18, utilities 13. Built by a dedicated agent that verified every entry
by **executing** the installed library on synthetic 400-bar frames rather than trusting docs.

Three fields beyond the plan's sketch turned out to be necessary:

- **`series_args`** - the real signature parameter names, because they do not match OHLCV column
  names: `sma(data)`, `hv(close)`, `bop(open_prices)`. `inputs[i]` maps to `series_args[i]`.
- **`needs_second_series`** - for `correlation(data1, data2)` and `beta(asset, market)`, which
  cannot be driven from a single frame.
- **`conditional_outputs`** - keyed `"param=value"`, for the three methods that change return
  arity based on an argument: `obv_smoothed`, `variance`, `ulcerindex`.

`validate_registry()` compares the registry against `dir(ta)` at import and raises on drift, so
an SDK bump that adds or renames an indicator fails loudly instead of silently.

**Warm-up was measured for all 127 by execution, and every value in the research note was
confirmed exactly - zero discrepancies.** Two utility corrections: `ta.roc` warm-up is `length`
not `length-1`, and `ta.valuewhen` is data-dependent rather than fixed.

The two known-broken indicators (`vi`, `ulcerindex`) are still all-NaN in openalgo 2.0.3 and are
marked `status="broken"`; the test asserts they remain broken so a future fix is noticed.

## The read-only toolkits

21 tools, none confirmation-gated except `set_analyzer_mode`, none cached.

`get_history` returns a **summary** - bar count, period high and low, percent change, and the
last n candles - not the raw frame. A 432-bar series is both useless to the model and expensive.
`list_instruments` refuses an unfiltered call outright, because the NSE master is thousands of
rows.

Errors the model can fix raise `RetryAgentRun` with an actionable message. A bad symbol produces
"Quote failed for NOSUCHSYM999 on NSE ... call search_symbols to find the exact symbol" rather
than a traceback.

## Validation output

```
=== live calls ===
  [PASS] check_connection - broker=zerodha mode=analyze
  [PASS] get_quote - ltp=1329
  [PASS] get_history returns a summary not raw candles - 432 bars, 3 shown, change=3.61%
  [PASS] get_supported_intervals - ['1m','3m','5m','10m','15m','30m','60m']
  [PASS] search_symbols - 7 matches
  [PASS] get_expiry_dates - 18 expiries, first=18-AUG-26
  [PASS] list_instruments refuses an unfiltered call - RetryAgentRun
  [PASS] calculate_margin - total_margin_required 216.4
  [PASS] bad symbol raises RetryAgentRun

  32 passed, 0 failed, 0 skipped
```

Registry: `21 passed, 0 failed` - including "110 indicators executed without error" and
"returned arity matches spec.outputs".

## Finding: Agno builds tool schemas lazily

The schema checks failed on the first run with `0/21` descriptions and empty parameter lists.
The cause is not a bug in the toolkits: `Function.description` and `Function.parameters` are
populated by `process_entrypoint()`, which the **Agent** calls when it assembles its tool list.
Straight after `Toolkit.__init__` they are still empty.

This matters beyond the test. Anything that inspects a toolkit before an Agent is constructed -
a schema dump, a docs generator, a lint pass - sees nothing and may wrongly conclude the tools
are undocumented. The test now calls `process_entrypoint()` explicitly, mirroring the agent, and
then asserts all 21 tools have descriptions and every argument reached the schema with a type
and a description parsed from the docstring.

## Next

Iteration 5: `indicators/compute.py` and the IndicatorTools dispatcher - the five tools that
expose all 127 indicators without spending 127 schema slots. The registry agent noted that
`list_specs(query=...)` currently matches only names and caveats, so a `description` field gets
added there for discoverability.
