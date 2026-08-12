# 001 - Foundation and Step 0 validation

**Date:** 2026-08-12
**Commit:** initial import
**Status:** complete

## What went in

| Path | Purpose |
|---|---|
| `docs/plan/PLAN.md` | The full build plan, 1029 lines, 11 parts plus 3 appendices |
| `docs/reference/research-notes/` | Five verification notes produced by reading and executing the installed packages |
| `scripts/validate_setup.py` | Step 0 harness - 26 live checks across OpenAlgo, LiteLLM/Baseten, and Agno |
| `backend/app/config.py` | Settings loader and ASCII logging setup |
| `backend/requirements.txt` | Pinned dependency set |
| `.env.example` | Env template covering OpenAlgo, model, risk limits, and app settings |
| `LICENSE` | MIT |

## Validation run

All 26 checks pass against the live stack. Broker **zerodha**, analyzer mode **analyze**.

| Area | Result |
|---|---|
| Server reachable, `ping` authenticated | PASS |
| `raw_post` to unwrapped endpoint (`gttorderbook`) | PASS |
| `funds`, `orderbook`, `positionbook`, `holdings` | PASS |
| `quotes` RELIANCE 1329 / SBIN 1082 / NIFTY 24435.95 | PASS |
| `multiquotes` x3, `depth` 5x5 | PASS |
| `history` SBIN 5m - 576 bars | PASS |
| `ta.rsi` 56.18, `ta.macd` 0.911 on live data | PASS |
| LiteLLM to Baseten completion, streaming | PASS |
| Tool calling, streamed and non-streamed | PASS |
| Agno confirmation gate pauses before execution | PASS |
| Approve then `continue_run` executes exactly once | PASS |

Reproduce with `python scripts/validate_setup.py`.

## Findings that changed the design

1. **DeepSeek V4 Flash is a reasoning model.** It spends completion tokens on hidden reasoning
   before emitting content. At `max_tokens=16` the entire budget went to reasoning and
   `content` came back `None` with `finish_reason="length"` - an empty reply, not a truncated
   one. `max_tokens=4096` is therefore a correctness requirement. The trap is asserted in the
   validation script so a future model swap cannot silently reintroduce it.

2. **The SDK has a leading-slash trap.** `BaseAPI.base_url` is `f"{host}/api/{version}/"` and
   already ends in a slash, while `_make_request` does a bare string concat. Passing `"/ping"`
   builds `/api/v1//ping`, which returns a 308 that the SDK's httpx client does not follow, and
   surfaces as a confusing `HTTP 308` error string. Endpoint names for `raw_post` must be
   slash-free. This affects all four GTT tools, which have no SDK wrapper.

3. **Two schema differences from the docs.** `history` returns six columns including `oi`, not
   the documented five. The `intervals` response is broker-specific: this broker exposes `60m`
   but has no weekly or monthly, so those requests must be rejected rather than passed through.

## Repository decisions

- `.env` is gitignored and was verified absent from the index; staged content was scanned for
  the live key values before the first push.
- The 14 MB of third-party documentation copied into `docs/reference/` (Agno's docs, OpenAlgo's
  API and prompt docs, the reference app) is **gitignored**. Those belong to their upstream
  projects and are not redistributed from this repository. They remain on disk for offline work.
  Only the research notes written for this project are committed.

## Next

Iteration 2: the OpenAlgo client layer - `constants.py`, `normalize.py`, `client.py`,
`frames.py` - with a unit test per module and a live smoke test.
