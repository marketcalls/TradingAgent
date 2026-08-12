# 005 - Indicator dispatcher: 127 indicators behind 5 tools

**Date:** 2026-08-12
**Plan step:** Part 9, step 6
**Status:** complete, 64/64 checks passing

## What went in

| Path | Purpose |
|---|---|
| `backend/app/indicators/descriptions.py` | One-line description for each of the 127 callables |
| `backend/app/indicators/compute.py` | The dispatcher: coerce, validate, call, serialize |
| `backend/app/tools/indicators.py` | IndicatorTools - 5 tools |
| `backend/tests/test_indicators.py` | 64 checks, synthetic and live |

## The five tools

`list_indicators`, `describe_indicator`, `compute_indicator`, `compute_indicators_batch`,
`scan_symbols`. The model picks an indicator by **name**, so 127 capabilities cost 5 schema
slots instead of 127.

`descriptions.py` closes the gap the registry agent flagged: search now matches intent, not just
method names. "bollinger" finds `bbands`, "money flow" finds `mfi`, "trend strength" finds `adx`.

## What the dispatcher enforces

Every rule was measured, not assumed:

- **Int coercion.** JSON tool arguments decode numbers to float, and the library's
  `validate_period` does `isinstance(period, int)` - so `{"period": 20.0}` would raise
  `TypeError` on roughly half the library. The dispatcher casts, and rejects `20.5` with a
  readable message rather than a traceback.
- **Warm-up padding.** `required_bars` uses the measured warm-up as a floor and scales with the
  requested period. Asking for the last 10 values of `beta` fetches 292 bars, not 10; `rsi` only
  needs 60. Without this, "show me beta" returns ten nulls.
- **Conditional arity.** `obv_smoothed` with Bollinger Bands returns 3 outputs, `variance` with
  components returns 5. Resolved from the registry, then cross-checked against what actually
  came back - reality wins over the registry so columns are never mislabelled.
- **Broken indicators explain themselves.** `vi` and `ulcerindex` return a message naming the
  library bug and suggesting an alternative, instead of a wall of nulls.
- **Aliases resolve.** `uo_oscillator` computes as `ultimate_oscillator`.
- **Two-series indicators** ask for the second symbol by name: correlation and beta prompt for
  `compare_symbol` rather than failing obscurely.

`scan_symbols` accepts a restricted condition - a comparison (`"rsi < 30"`) or a cross
(`"crossover(macd_line, signal_line)"`) - parsed by regex. **There is no `eval` anywhere.** The
test feeds it `__import__('os').system('ls')`, `eval(x)`, `os.remove('/')` and
`rsi < 30 and 1==1`; all are refused.

## Two real bugs found and fixed

1. **`list_indicators` overflowed the tool budget.** All 127 with descriptions exceeds the
   12,000-char cap. Above 45 matches the tool now returns names grouped by category with a hint
   to narrow, instead of being silently cut.

2. **`to_json` produced invalid JSON when truncating.** It appended a marker mid-string, so a
   truncated result could not be parsed - by the test or by anything else consuming it. It now
   emits a well-formed object carrying `truncated`, `dropped_chars`, a instruction to narrow the
   request, and the partial text. A tool that trips this is a design problem in that tool, but
   the guard no longer makes things worse.

## Validation output

```
=== compute dispatcher ===
  [PASS] adx puts the adx line third - ['plus_di', 'minus_di', 'adx']
  [PASS] psar returns a single series - ['psar']
  [PASS] float period 20.0 is coerced to int - 20
  [PASS] vi refused with an explanation
  [PASS] variance with components gives five outputs
  [PASS] required_bars pads for beta's 253-bar warm-up - 292

=== condition parser ===
  [PASS] rejects "__import__('os').system('l"
  [PASS] rejects 'rsi < 30 and 1==1'

=== toolkit (live) ===
  [PASS] compute_indicator rsi on live data - rsi=56.176534 over 432 bars
  [PASS] compute_indicator supertrend on live data - st=1071.391239 dir=-1.0
  [PASS] batch computes four indicators on one fetch - 4 results, 432 bars
  [PASS] scan_symbols screens a watchlist - 4 of 4 matched
  [PASS] scan_symbols handles a crossover condition - 1 matched
  [PASS] correlation against an index works - corr=0.713175

  64 passed, 0 failed, 0 skipped
```

## Next

Iteration 6: the order toolkits and the React frontend are being built in parallel by two
agents. After those land, `agent.py` and `main.py` wire everything into the SSE backend with the
confirmation loop.
