# 006 - Agent construction and the SSE backend

**Date:** 2026-08-12
**Plan steps:** Part 9, steps 7 and 8 (backend half)
**Status:** complete - the agent answers real questions with real data

## What went in

| Path | Purpose |
|---|---|
| `backend/app/agent.py` | Model wiring, instructions, dynamic tool scoping |
| `backend/app/main.py` | FastAPI, SSE contract, the confirmation loop, sessions |

## Tool scoping works as a safety layer

`tools` is a **callable factory**, resolved by Agno at the start of every run:

```
read-only session:      26 tools  (market 6, symbols 4, account 6, indicators 5, system 5)
trading enabled:        37 tools  (+ orders 9, gtt 2)
```

A session that has not enabled trading has no order tools **in its schema at all**, so no
amount of prompting can reach one. That is layer 2 of the four safety layers, and unlike the
instructions it cannot be talked around.

## The SSE contract

`confirm` is the only addition to the sibling app's vocabulary, and it is terminal for its
request. Three Agno behaviours the implementation is written against:

- `RunPausedEvent` is yielded **unconditionally** in streaming mode and the stream **ends**
  there with no trailing `RunCompleted`. The generator returns immediately after emitting
  `confirm`; waiting for a terminal event would hang the browser forever.
- `continue_run`'s `stream_events` defaults to **False**, not None, so the resume leg passes it
  explicitly or the tool timeline goes blank mid-trade.
- `ToolCallCompleted` with `tool_call_error=True` is followed by a separate `ToolCallError`, so
  the second is suppressed against a set of seen ids.

The confirmation card's `preview` is computed **server-side** - LTP, notional, current position
in that symbol, available cash, analyzer mode. The model does not produce these numbers, which
is the point: the card must show something the model cannot get wrong.

## End-to-end validation

Server started on 8088, live against broker zerodha in analyze mode.

```
GET /api/health
{"ok":true,"model":"baseten/deepseek-ai/DeepSeek-V4-Flash-0731","missing_keys":[],
 "openalgo_connected":true,"broker":"zerodha","trading_enabled":false}

GET /api/mode
{"mode":"analyze","orders_are_real":false,"require_analyzer_mode":true,"kill_switch":false}
```

Chat turn: *"What is the current price of RELIANCE on NSE, and what is its 14-period RSI on the
15 minute chart?"*

```
START run=4ac7af68-28c
  TOOL START get_quote({"symbol": "RELIANCE", "exchange": "NSE"})
  TOOL START compute_indicator({"symbol": "RELIANCE", "indicator": "rsi", "interval": "15m"})
  TOOL END   get_quote ok=True
  TOOL END   compute_indicator ok=True
DONE reason=stop
```

The reply came back as markdown tables with LTP 1,329.00, volume 79,01,012 in Indian digit
grouping, and a ten-row RSI series ending at 60.81 with the direction called as falling.

**Both tools were dispatched in parallel in a single model turn.** That is the behaviour Risk 1
in the plan was worried about, and it holds at 26 tools. The 37-tool case still needs measuring
once the order toolkits land.

## Instructions

Seven blocks, deliberately short, because instructions are safety layer 1 and carry no
enforcement weight: symbology, constants, data discipline (quote before you size, confirm the
interval, never guess an indicator name), order discipline (state every field and the LTP before
proposing; never re-propose a rejected order unchanged), mode awareness, presentation, and the
advice boundary.

## Next

Iteration 7: the order toolkits and frontend agents are finishing. Then the confirmation loop
gets its end-to-end test - propose, pause, approve, resume - and the frontend is wired to the
live backend.
