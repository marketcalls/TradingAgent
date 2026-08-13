# Changelog

All notable changes to the OpenAlgo Trading Agent will be documented in this file.

Version is maintained in `backend/app/version.py`, the single source of truth, and served on
`GET /api/health`.

## [0.1.0] - 2026-08-13

First release. A chat-first trading agent for Indian markets, built on Agno with OpenAlgo as the
broker connection, where every order stops and asks for human approval before it reaches the
broker.

---

## Highlights

- **Human approval on every order** - the run pauses before the tool executes, the browser shows
  an approval card, and nothing is sent until the user clicks Approve. Verified end to end over
  HTTP, including that the resume happens in a different request from the pause.
- **All 127 indicators** from OpenAlgo's `ta` library behind 5 dispatcher tools, so the full
  library costs 5 schema slots instead of 127.
- **43 tools across 43 endpoints** - market data, symbols, account, options analytics, orders,
  GTT, indicators and system. 13 can move money and all 13 are gated.
- **Any LLM provider** through LiteLLM - Baseten, OpenAI, Anthropic, Gemini, Groq, OpenRouter, or
  a local model via Ollama with no API key at all.
- **Deterministic risk guard** that runs after approval and before the broker, which no amount of
  prompting can talk past.

---

## Features

### Trading

- Regular, smart, basket and split orders; modify, cancel, cancel-all and square-off-all.
- GTT orders: place, modify, cancel and list.
- Options: chain with Greeks, ATM/ITM/OTM symbol resolution, single and batch Greeks, synthetic
  future, single-leg and multi-leg orders.
- MARKET, LIMIT, SL and SL-M price types on every order-entry tool.

### Market data and analysis

- Quotes, bulk quotes, five-level depth, historical candles, supported intervals, holidays and
  session timings.
- Symbol search with alias resolution: "NIFTY 50" and "BANK NIFTY" resolve to the real symbols,
  and a wrong exchange is corrected automatically.
- 127 indicators with a multi-symbol screener and a batch mode that shares one candle fetch.

### Account

- Funds, order book, trade book, positions, holdings and pre-trade margin.

### Safety

- Four independent layers: instructions, tool scoping, human approval, and RiskGuard.
- With trading disabled the order tools are not registered at all, so they cannot be reached.
- RiskGuard: kill switch file, master switch, analyzer-mode enforcement, symbol denylist,
  exchange and product allowlists, quantity bounds, per-session order cap, duplicate suppression
  within 10 seconds, notional cap, and a fat-finger guard on limit prices more than 20 percent
  from the last traded price.
- Audit trail written twice per order, before the broker is touched and after, to a daily JSONL
  file and a SQLite table.
- Analyzer (simulated) mode is the default and live mode is refused while
  `REQUIRE_ANALYZER_MODE` is set.

### Frontend stack

Built on the current generation: React 19.2, TypeScript 7.0 (the native compiler), Vite 8.2 and
Tailwind CSS 4.3. Tailwind 4 is CSS-first, so `tailwind.config.js` and the PostCSS step are gone:
the theme lives in an `@theme` block in `src/index.css`, and `@tailwindcss/vite` replaces
`postcss` and `autoprefixer`. Two v4 behaviour changes are handled explicitly - the dark variant
is redefined to follow the app's class toggle rather than `prefers-color-scheme`, and the default
border colour is pinned to the project token instead of v4's `currentColor`.

### Interface

- Streaming chat over SSE with a live tool timeline.
- Approval card showing every argument alongside server-computed context - notional, LTP, current
  position and available funds - none of which come from the model.
- Per-model thinking control, shown only when the model actually supports it.
- Light and dark themes, session history, Indian digit grouping.

---

## Notable engineering notes

Several vendor behaviours turned out to be wrong when measured, and are worked around
automatically. Full detail in `docs/model-notes.md`.

- **LiteLLM's Ollama model table is stale**, so tool-capable models fell back to a broken
  JSON-emulation path. The agent asks Ollama for the real capabilities instead.
- **LiteLLM drops `tool_calls` from assistant messages on Ollama**, so the model never saw that
  it had called a tool and called it again forever. The agent restores the field.
- **OpenAI gpt-5.6 rejects `top_p`, accepts only `temperature=1`, and cannot combine tools with
  reasoning.** All three are detected and handled.
- **Baseten rejects `reasoning_effort` outright**, which failed every request when set.
- **openalgo's SDK 308-redirects on a leading slash** in `_make_request`, which the client layer
  guards against.
- **`vi` and `ulcerindex` are broken in openalgo 2.0.3** and return all-NaN; they are marked and
  refuse with an explanation.

---

## Known limitations

- The approval gate is enforced by the framework, but only fires when the model actually calls
  the order tool. Measured on gpt-5.6-luna, 5 of 6 order instructions reached the gate; the
  backend emits an explicit warning when a reply claims an order that was never placed.
- Small local models choose poorly among 43 tools, so a lean 15-tool profile is selected
  automatically for them.
- GTT cannot be rehearsed on builds where it returns 501 in analyzer mode; on the build tested
  here it is sandboxed and works.
- WebSocket streaming quotes are not wired into the UI; quotes are polled on demand.
- No authentication on the backend. It binds to localhost and any remote deployment needs auth
  added first.

---

## Requirements

Python 3.14, Node 18+, and a running OpenAlgo instance with a broker session.
Pinned: `agno` 2.8.7, `litellm` 1.79.1, `openalgo` 2.0.3.
