# 008 - Frontend, and the whole app running in a browser

**Date:** 2026-08-12
**Plan steps:** Part 9, steps 8, 11 and 12
**Status:** complete - the app runs end to end in Chrome

## What went in

```
frontend/
  package.json  vite.config.ts  tsconfig.json  tsconfig.node.json
  tailwind.config.js  postcss.config.js  index.html
  src/main.tsx  index.css  App.tsx
  src/lib/       sse.ts  api.ts  format.ts
  src/components/  Sidebar  ModeBanner  Message  ToolTimeline
                   ConfirmCard  DataTable  Composer
```

React 18.3, Vite 5.4, TypeScript 5.6, Tailwind 3.4, react-markdown 9 with remark-gfm - the same
stack as the equity-research sibling. `tsc --noEmit` clean; `vite build` produces 1836 modules,
15.36 kB CSS and 362.26 kB JS (112.85 kB gzipped) in about 2 seconds.

## Browser validation

Backend on 8088, Vite on 5173, live against broker zerodha in analyze mode.

**A research turn.** Clicking the suggestion "Quote RELIANCE and compute a 14 period RSI on daily
bars" streamed a reply with a collapsed "Behind the scenes, 3 steps" timeline, a price table
showing LTP as ₹1,329.00 and volume as 79,01,012 in Indian digit grouping, and a ten-row daily
RSI table ending at 54.26 with the direction called as falling. The model noticed the broker
exposes `D` and said so before computing.

**An order turn.** "Buy 5 shares of TATAMOTORS on NSE at market price, CNC product." produced the
confirmation card:

```
Simulated order (analyze mode)                             place_order
  Symbol TMCV | Action BUY | Exchange NSE | Quantity 5
  Product CNC | Price type MARKET

  Checked by the server
  Mode             analyze
  Notional         Rs 2,285.25
  LTP              Rs 457.05
  Current position 0
  Available funds  Rs 99,99,954.04

  [Approve] [Reject]        Note sent back to the model if you reject
```

The composer was blocked while the card was pending - "approve or reject the pending action to
continue" - because sending a new message would abandon the server-side paused run.

Approving froze the card to "Decision submitted", resumed the run in a second request, executed
`place_order`, and returned **Order ID 26081293028254** in analyze mode. The model closed by
stating the order was simulated, did not reach the real broker, and that it would not switch to
live mode unless explicitly asked.

## The symbol resolution worth recording

The model turned "TATAMOTORS" into **TMCV**, which looked like a hallucination. It was not.
`TATAMOTORS` no longer resolves on NSE after the demerger; a live quote for it fails. The current
symbols are `TMCV` (TATA MOTORS) and `TMPV` (TATA MOTORS PASS VEH), and the LTP of ₹457.05 on the
card matched the live TMCV quote exactly.

The model reached that by calling `search_symbols` instead of constructing a symbol from memory,
which is precisely what the instructions ask for. This is the single best evidence so far that
the symbology guidance is working, and it is a case where a plausible-looking symbol from memory
would have failed the order outright.

## Contract fix found by building the UI

The ConfirmCard decides whether to paint itself red from `preview.mode`. The backend was sending
`analyzer_mode` and `available_cash`, so the card silently fell back to the **polled** banner
value, which can be up to 15 seconds stale. For the one decision that separates a simulated order
from a real one, a stale fallback is not acceptable.

The backend now emits `mode` and `available_funds` under the exact keys the card reads, keeping
the OpenAlgo-native spellings as aliases. The confirmation test asserts both **by name**, so this
cannot regress silently.

Other decisions the frontend resolved: per-requirement mode beats the banner; multiple
requirements in one `confirm` get a per-requirement toggle plus a single submit so the
`decisions` map is always complete; the card is marked resolved before the POST as a
double-submit guard; and `set_analyzer_mode(analyze=false)` requires typing GO LIVE.

## Build order status

All 12 steps in the plan's Part 9 are done.

| Step | State |
|---|---|
| 0 Model validation | done, 26 checks |
| 1 Scaffolding | done |
| 2 Client layer | done, 44 checks |
| 3 Audit and RiskGuard | done, 48 checks |
| 4 Read-only toolkits | done, 32 checks |
| 5 Indicator registry | done, 21 checks |
| 6 Indicator dispatcher | done, 64 checks |
| 7 SSE backend | done |
| 8 Frontend shell | done |
| 9 Order toolkits | done, 114 checks |
| 10 Confirm loop | done, 25 checks |
| 11 Mode and hardening | done |
| 12 Docs | README pending |

**374 automated checks passing**, plus the browser run above.

## Next

A README covering setup and the run commands, then the loop closes.
