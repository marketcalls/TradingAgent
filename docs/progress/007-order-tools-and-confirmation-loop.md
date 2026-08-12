# 007 - Order toolkits and the confirmation loop

**Date:** 2026-08-12
**Plan steps:** Part 9, steps 9 and 10
**Status:** complete - 24/24 confirmation loop, 114/114 order tools, 323 checks overall

## What went in

| Path | Purpose |
|---|---|
| `backend/app/tools/orders.py` | OrderTools - 9 tools, 8 gated, plus a shared `MutatingToolkit` base |
| `backend/app/tools/gtt.py` | GttTools - 2 tools, both gated, all four endpoints via `raw_post` |
| `backend/app/tools/options.py` | OptionsTools - 6 tools, 2 gated |
| `backend/tests/test_tools_orders.py` | 114 checks |
| `backend/tests/test_confirm_loop.py` | 24 checks, full HTTP round trip |

`MutatingToolkit` means all three order paths run the same sequence: audit attempt, risk check,
broker call, audit result. There is one implementation of that order, not three.

## The confirmation loop, proven end to end

The test drives the real FastAPI app with the real model and the real broker, and refuses to run
unless OpenAlgo reports analyzer mode, so every order is simulated.

```
=== propose an order ===
  events: start start:get_analyzer_status start:get_quote end:... confirm
  [PASS] proposing an order emits confirm
  [PASS] confirm is the terminal event
  [PASS] place_order did NOT execute before approval - 0 executions
  [PASS] requirement carries the order arguments -
         {"symbol": "SBIN", "action": "BUY", "exchange": "NSE", "quantity": 1, "product": "CNC"}
  [PASS] preview computed server-side carries ltp -
         ltp=1082.0 notional=1082.0 cash=10000000.0 mode=analyze

=== reject the order ===
  [PASS] a rejected order never reaches the broker - 0 successful executions
  [PASS] the model acknowledges the rejection

=== propose again, then approve ===
  events: start start:place_order end:place_order(ok) done:stop
  [PASS] approving executes the order tool - 1 executions
  [PASS] the order tool executes exactly once
  [PASS] the broker returned an order id -
         {"data":{"orderid":"26081213161452"},"mode":"analyze"}

=== audit trail ===
  [PASS] audit recorded decisions - ['attempt', 'decision', 'result']
  [PASS] audit recorded one approval and one rejection
  [PASS] audit captured an order id - ["26081213161452"]

=== stale confirmation ===
  [PASS] re-confirming a resolved run is refused - HTTP 400

  24 passed, 0 failed, 0 skipped
```

The resume happens in a **different HTTP request** from the pause, which is the real deployment
shape.

## The bug that took the longest: paused runs were being cancelled

The first run of this test failed with the confirm endpoint returning
`400 this run has no pending confirmations`. Rehydrating the same run in an isolated script
worked fine, which made it look like a test problem. It was not.

Instrumenting the endpoint showed the truth: **`status=CANCELLED, reqs=0`**.

`_pump` emitted `confirm` and then `return`ed. Returning early makes Starlette close the
generator, which throws `GeneratorExit` into Agno's generator; its cleanup marks the run
cancelled and **overwrites the paused state in the database**. By the time the confirm request
arrived, the paused run no longer existed.

The isolated script passed because it used `break`, leaving the generator suspended rather than
closed, and read the database before garbage collection ever ran the cleanup.

The fix is to stop abandoning the generator. Agno yields nothing after `RunPaused`, so
`continue` lets the loop end naturally and the run stays paused. The disconnect check now also
skips `cancel_run` once paused - a run waiting for a human is not a stuck run.

## Two documented behaviours that are wrong on this build

1. **GTT is not 501 in analyzer mode.** `placegttorder` returned
   `{"trigger_id": "GTT-260812-...", "mode": "analyze"}`. GTT is sandboxed like everything else
   and can be rehearsed, contrary to the plan and the API docs. The 501 handler stays, tested
   against a synthetic response, since other builds may differ. Plan corrected.

2. **The sandbox rejects MIS after 15:15 IST** - "MIS orders cannot be placed after square-off
   time". The confirmation test originally used MIS and was only green in the morning; it now
   uses CNC. Plan corrected.

Also recorded: `optionchain(with_greeks=True)` returns IV and Greeks **flat on each leg**, not
nested under `greeks` as `optiongreeks` does, and the chain is compacted to 13 fields per leg
because the raw response blows the 12,000-char budget past about 7 strikes.
`syntheticfuture` requires `expiry_date`, so an expiry is auto-resolved and reformatted
`25-AUG-26` to `25AUG26`.

## A model-behaviour fix

The model was inconsistent about actually calling the order tool - sometimes it described the
order in prose and stopped. The cause was my own instruction, which said to "explain clearly
what will happen while they decide". That reads as "do not call the tool". Replaced with an
explicit rule: when the user gives a complete order instruction, call the tool, because the
interface is what shows the approval card and nothing reaches the broker until they approve.

## Test suite

| Suite | Result |
|---|---|
| `test_client_layer` | 44 passed |
| `test_safety` | 48 passed |
| `test_registry` | 21 passed |
| `test_tools_readonly` | 32 passed |
| `test_indicators` | 64 passed |
| `test_tools_orders` | 114 passed |
| `test_confirm_loop` | 24 passed |
| **Total** | **347 passed, 0 failed** |

One regression was caught and fixed on the way: `test_client_layer` asserted a truncated result
was under 200 characters, which stopped being true when `to_json` started wrapping overflow in a
well-formed envelope. The assertion now checks the real intent - bounded and parseable.

## Next

Iteration 8: the frontend. The React app is being built against this SSE contract; once it
lands, the ConfirmCard gets wired to the live `confirm` and `/api/chat/confirm` endpoints and
the whole thing runs in a browser.
