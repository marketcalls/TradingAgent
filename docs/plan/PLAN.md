# OpenAlgo Trading Agent - Build Plan

A chat-first trading agent built on Agno, driven by the OpenAlgo REST API and Python SDK,
with the full 127-function OpenAlgo indicator library exposed to the model.
Architecturally a sibling of `equity-research-agent`: FastAPI + SSE backend, Agno agent,
React/Vite/Tailwind frontend, SQLite session store.

The difference that shapes every decision below: this agent **spends money**. The equity
agent could only be wrong; this one can be wrong *and* place a live order. The plan
therefore treats the order path as a safety-critical subsystem, not as one more tool.

---

## Part 0. Scope

**In scope**

- Every registered OpenAlgo v1 REST endpoint that a trader would drive from chat
  (57 method/path pairs; see Appendix B for the endpoint-to-tool map).
- All 127 callables on `openalgo.ta` (114 indicators + 13 utilities), reachable through a
  dispatcher rather than 127 separate tools.
- Order placement, modification, cancellation, GTT, options orders, basket/split orders -
  all behind an explicit human confirmation gate.
- Multi-session chat with history, streaming tokens, live tool timeline, cancellation.

**Out of scope for v1**

- WebSocket streaming quotes into the UI. The SDK's feed is thread-and-callback based and
  fights the SSE model. v1 polls `quotes`/`multiquotes` on demand. Noted as v2 work.
- Backtesting, the `portfolio/*` and `sip/*` routes (registered but undocumented).
- Telegram/WhatsApp *management* endpoints. Only the two send endpoints are exposed.
- Autonomous/unattended trading. The agent never places an order without a human click.

---

## Part 1. Verified ground truth

Everything in this part was measured against installed packages and source, not read off a
docs page. Where docs and source disagree, source wins and the disagreement is recorded.

### 1.1 Installed versions

| Package | Version | Location |
|---|---|---|
| `agno` | 2.8.7 | `Python314/Lib/site-packages/agno` |
| `litellm` | 1.79.1 | `Python314/Lib/site-packages/litellm` |
| `openalgo` (SDK + `ta`) | 2.0.3 | `openalgo/.venv/Lib/site-packages/openalgo` |
| `httpx` | 0.28.1 | SDK transport |

The Agno docs dump in `docs/reference/agno/` targets roughly 2.7.x. Section 1.5 lists the
2.8.7 deltas that matter.

### 1.2 The model path: LiteLLM to Baseten

Confirmed by reading `litellm/llms/baseten/chat.py` and
`litellm/litellm_core_utils/get_llm_provider_logic.py:504-510`:

- LiteLLM 1.79.1 ships a **first-class `baseten` provider**. It is not a generic
  OpenAI-compatible shim we have to configure by hand.
- A model id that is **not** an 8-character alphanumeric deployment code routes to the
  Baseten Model API at `https://inference.baseten.co/v1`. `deepseek-ai/DeepSeek-V4-Flash-0731`
  is not 8 alphanumerics, so it routes correctly.
- The key is read from `BASETEN_API_KEY` (`get_secret_str("BASETEN_API_KEY")`), the base URL
  can be overridden with `BASETEN_API_BASE`.
- `BasetenConfig.get_supported_openai_params` includes `tools` and `tool_choice`, so function
  calling is supported on this path. It also includes `stream_options`, `presence_penalty`,
  `frequency_penalty`, `response_format`, `seed` - the full set the user's OpenAI-client
  example passes.

Agno's `LiteLLM` class (`agno/models/litellm/chat.py`) accepts `id`, `api_key`, `api_base`,
`temperature` (default 0.7), `top_p` (default 1.0), `max_tokens`, `request_params`,
`extra_headers`, `extra_body`. So the configuration is:

```python
from agno.models.litellm import LiteLLM

model = LiteLLM(
    id="baseten/deepseek-ai/DeepSeek-V4-Flash-0731",
    api_key=settings.baseten_api_key,
    api_base="https://inference.baseten.co/v1",
    temperature=0.2,          # trading answers should be boring and repeatable
    top_p=1.0,
    max_tokens=4096,
    request_params={"stream_options": {"include_usage": True}},
)
```

Note the difference from `equity-research-agent`: that project had to pass
`temperature=None, top_p=None` because `gpt-5.6-luna` rejects them, and had to force
`reasoning_effort: "none"` to make tool calls work at all. **Neither hack applies here.**
Baseten's DeepSeek endpoint accepts both parameters, so we send real values.

Fallback if the `baseten/` prefix misbehaves - the generic OpenAI-compatible route, which
hits the identical URL:

```python
model = LiteLLM(
    id="openai/deepseek-ai/DeepSeek-V4-Flash-0731",
    api_key=settings.baseten_api_key,
    api_base="https://inference.baseten.co/v1",
)
```

**DeepSeek V4 Flash is a reasoning model, and that changes the configuration.** Measured
against the live endpoint: it spends completion tokens on hidden reasoning before emitting any
content, and exposes it as `message.reasoning_content` (and as `reasoning_content` deltas while
streaming). Consequences:

- **A small `max_tokens` returns an empty reply, not a truncated one.** At `max_tokens=16` all
  16 tokens went to reasoning, `content` came back `None`, and `finish_reason` was `"length"`.
  At 600 the same prompt returned `"OK"` after 52 reasoning tokens. `max_tokens=4096` is not
  generosity here, it is a correctness requirement. `validate_setup.py` asserts this trap so a
  future model swap cannot silently reintroduce it.
- **Reasoning dominates the token bill on short answers**: 65 total tokens for a two-character
  reply, 52 of them reasoning. Budget accordingly.
- **Time to first *content* token is ~1.7s** because reasoning streams first. The UI should show
  a thinking indicator during that window rather than an empty bubble; the `reasoning_content`
  deltas are available if we ever want to surface them.

Step 0 validation has been **run and passed** - see 1.6.

### 1.3 The OpenAlgo SDK surface, and its gaps

`openalgo.api` composes eight mixins: `OrderAPI, DataAPI, AccountAPI, FeedAPI, OptionsAPI,
TelegramAPI, WhatsAppAPI, UtilitiesAPI` - 53 public members. Constructor:

```python
api(api_key, host="http://127.0.0.1:5000", version="v1", timeout=120.0,
    ws_port=8765, ws_url=None, verbose=False, auto_reconnect=True)
```

Facts that drive the client layer in Part 3:

| Fact | Consequence |
|---|---|
| The client is **fully synchronous** (`httpx.Client` + OS threads for the feed). | Every call must go through `asyncio.to_thread`, or the FastAPI event loop stalls for the length of a broker round trip. |
| Errors are **returned as dicts, never raised** (except `Strategy.strategyorder`, an `optionsmultiorder` leg `KeyError`, and missing-kwarg `TypeError`). | Tools must inspect `status` on every response; a truthy return is not a success. |
| `history` and `instruments` return a **DataFrame on success but a dict on error**. | Type-check before `.tail()`, or the tool throws on the error path. |
| `price_type` at the top level, but **`pricetype`** inside basket/margin/leg dicts. | The tool schema must use the right spelling per call site. One typo here silently drops to a default. |
| Request `order_id`, response `orderid`. | Normalize both directions in the client wrapper. |
| `strategy` defaults vary: `"Python"` / required (`optionsmultiorder`) / `None` and deprecated (`optionsymbol`). | Always pass our own `DEFAULT_STRATEGY_NAME` so orders are attributable in the OpenAlgo order book. |
| Default `timeout=120.0`. | Far too long for a chat UI. We set 15s (30s for `instruments`). |
| `ws://` is hard-coded even against an https host. | Only matters for v2 streaming. |

**Endpoints the server exposes but the SDK does not wrap** (13): all four GTT methods
(`placegttorder`, `modifygttorder`, `cancelgttorder`, `gttorderbook`), `multioptiongreeks`,
`ping`, `chart` (GET/POST), `pnl/symbols`, `ticker`, plus the undocumented `portfolio/*` and
`sip/*`. These are reachable through the SDK's own transport:

```python
client._make_request("/placegttorder", payload)   # payload includes apikey
```

We wrap that in a documented `raw_post()` helper rather than scattering underscore calls
through the toolkits, and we cover it with a smoke test since it is a private method.

**The leading-slash trap, found while validating.** `BaseAPI.__init__` sets
`self.base_url = f"{host}/api/{version}/"` - already ending in a slash - and `_make_request`
does a bare `self.base_url + endpoint`. Passing `"/ping"` therefore builds
`http://127.0.0.1:5000/api/v1//ping`, which the server answers with a **308 redirect that the
SDK's `httpx.Client` does not follow** (`follow_redirects` defaults to False). The call returns
`{"status": "error", "message": "HTTP 308: <!doctype html>..."}` rather than failing loudly.
Verified fix: **endpoint names passed to `raw_post` must not have a leading slash.**
`raw_post` will strip one defensively and assert in debug builds.

### 1.4 The indicator library: 127, not "80+"

`from openalgo import ta` gives a **singleton instance** of `TechnicalAnalysis`, not a module.
`len([m for m in dir(ta) if not m.startswith('_')]) == 127` on the installed 2.0.3 build:
114 indicators + 13 utilities. The introduction doc says "over 80"; the flow reference says
116. Both are wrong. Appendix A lists all 127 by category.

Behaviours a wrapper must respect (all measured on 400-600 synthetic bars):

1. **A single NaN in the input poisons everything downstream.** `_backend`'s `sma`/`rolling_sum`
   are `np.cumsum`-based. One NaN at bar 50 of 300 leaves `ta.sma` with 263/300 NaN. Broker
   history has gaps. **`dropna().sort_index()` before every call, no exceptions.**
2. **Period arguments must be Python `int`.** `validate_period` does `isinstance(period, int)`,
   so `14.0` and `np.int64(14)` both raise `TypeError`. JSON tool arguments decode numbers to
   `float`. **Cast with `int()` in the dispatcher** or roughly half the indicator calls fail.
3. **`ta.vi` and `ta.ulcerindex` return all-NaN in this build** (same cumsum bug). Return an
   explicit "not available in this build" error, not a wall of nulls. `ta.median_bands`, which
   the repo's own flow reference also lists as broken, actually works.
4. **`ta.rvi` is Relative *Vigor* Index**, not Relative Volatility Index:
   `ta.rvi(open, high, low, close, period=10) -> (rvi, signal)`. The volatility RVI is imported
   as `VolatilityRVI` and never exposed. The volatility doc's `ta.rvi(df['close'])` raises.
5. **`ta.psar` returns a single Series**, not the 2-tuple the hybrid doc shows.
   **`ta.adx` returns `(+DI, -DI, adx)`** - ADX is third, not first.
   **`ta.aroon` default period is 25**, not 14.
   **`ta.stochastic` hides `smooth_k` between `k_period` and `d_period`.**
6. **The 13 utilities always return raw `np.ndarray`**, never a Series - the pandas index is
   lost. The utility doc's `result.iloc[-1]` examples would crash.
7. **Three methods change return arity based on a flag**: `obv_smoothed`
   (`ma_type='SMA + Bollinger Bands'` -> 3-tuple), `variance` (`return_components=True` ->
   5-tuple), `ulcerindex` (`return_signal=True` -> 2-tuple). Branch on the argument, not on
   the type annotation.
8. **Type annotations are unreliable** - 34 methods have none, and several annotated
   `-> np.ndarray` return a Series. Build the schema from `inspect.signature`, and the return
   shape from a hand-maintained map.
9. `align_arrays` checks **length only, never index equality**. Two same-length series from
   different frames produce silently wrong output. Always slice from one DataFrame.
10. `ta` is a module-level singleton with mutable `_*` attributes. Read-only concurrent use is
    fine; never mutate it from a tool.
11. `ta.ultimate_oscillator` and `ta.uo_oscillator` are numerically identical duplicates.
    Expose one, alias the other.

### 1.5 Agno 2.8.7: HITL and streaming

The confirmation gate is the reason this project uses Agno rather than a hand-rolled loop.

```python
run = agent.run("Buy 10 RELIANCE")
run.is_paused                 # True; run.status == RunStatus.paused
for req in run.active_requirements:      # list[RunRequirement], unresolved only
    req.tool_execution.tool_name         # "place_order"
    req.tool_execution.tool_args         # {"symbol": "RELIANCE", "action": "BUY", ...}
    req.confirm()                        # or req.reject("note the model will see")
    req.to_dict()                        # JSON-safe, ship to browser and back
run = agent.continue_run(run_id=..., session_id=..., requirements=run.requirements)
```

Six verified behaviours the backend must be written against:

1. **`db` is mandatory** for HITL. Without it a paused run cannot be resumed.
2. **`session_id` must accompany `run_id`** on resume, or 2.8.7 raises
   `ValueError("Session ID is required to continue a run from a run_id.")`.
3. **`RunPausedEvent` is yielded unconditionally in streaming mode**, even with
   `stream_events=False` - it is not behind the guard that wraps `RunStarted`/`RunCompleted`.
4. **The stream terminates at the pause.** No `RunCompleted` follows. An SSE generator that
   waits for a terminal event after `RunPaused` hangs the browser forever.
5. **`continue_run`'s `stream_events` defaults to `False`** (not `None`). Pass it explicitly or
   the resume leg emits no tool events and the UI timeline goes blank mid-trade.
6. **`continue_run(updated_tools=...)` is deprecated in 2.8.7.** Many doc pages still show it.
   Write against `active_requirements` / `req.confirm()` / `requirements=`.

Two further traps:

- `requires_confirmation_tools=[...]` on a Toolkit only **warns** on a typo - `include_tools`
  and `exclude_tools` raise. A misspelled entry silently disables the safety gate. Part 5.2
  mandates a test that asserts the flag is actually set on every mutating tool.
- `Toolkit(cache_results=True)` is an **on-disk JSON cache that survives restarts**, despite
  the docstring implying memory. Never enable it on quotes, positions, or orders.

### 1.6 Step 0 results - the whole path is proven

`scripts/validate_setup.py` runs 26 checks against the live stack. All 26 pass.
Last run: OpenAlgo on `127.0.0.1:5000`, broker **zerodha**, analyzer mode **analyze**.

| Area | Result |
|---|---|
| Server reachable, `ping` authenticated | PASS - broker=zerodha |
| `raw_post` to an unwrapped endpoint (`gttorderbook`) | PASS - returns `{"data": [], "mode": "analyze"}` |
| `analyzerstatus`, `funds`, `orderbook`, `positionbook`, `holdings` | PASS - sandbox funds 10,000,000 |
| `quotes` RELIANCE / SBIN / NIFTY | PASS - RELIANCE ltp 1329, SBIN ltp 1082, NIFTY 24435.95 |
| `multiquotes` (3 symbols), `depth` (5x5 book) | PASS |
| `intervals` | PASS - `1m 3m 5m 10m 15m 30m 60m 1h D`; **no weekly or monthly on this broker** |
| `history` SBIN 5m, 10 days | PASS - 576 bars |
| `ta.rsi` + `ta.macd` on that live frame | PASS - rsi 56.18, macd 0.911 |
| LiteLLM -> Baseten completion | PASS - 1.71s |
| Streaming | PASS - 18 content chunks after 73 reasoning chunks, first content token 1.67s |
| Tool calling, non-streamed and streamed | PASS - correct name and arguments both ways |
| Agno agent calls a tool and answers | PASS |
| Agno confirmation gate pauses before execution | PASS - `is_paused=True`, tool not executed |
| Approve then `continue_run` executes exactly once | PASS |

Two schema facts worth recording, both differing from the docs:

- `history` returns **six** columns - `close, high, low, oi, open, volume`. The SDK doc shows
  five; `oi` is present and is zero for cash equities.
- The `intervals` response is broker-specific. This broker exposes `60m` but no `W`/`M`, so any
  weekly or monthly request must be rejected with a clear message rather than passed through.

---

## Part 2. Architecture

### 2.1 Component map

```
Browser (React)
   |  POST /api/chat/stream          SSE: start token tool_start tool_end confirm done error
   |  POST /api/chat/confirm         SSE: same event vocabulary, resumed run
   v
FastAPI (backend/app/main.py)
   |
   +-- Agno Agent  (LiteLLM -> Baseten -> DeepSeek V4 Flash)
   |     +-- SqliteDb            sessions, runs, paused-run state
   |     +-- 8 Toolkits          ~40 tools
   |
   +-- RiskGuard                 deterministic, LLM-independent order checks
   +-- AuditLog                  every mutating call, JSONL + SQLite
   |
   v
OpenAlgoClient (backend/app/openalgo/client.py)
   |  one shared openalgo.api instance, every call via asyncio.to_thread
   v
OpenAlgo server  http://127.0.0.1:5000/api/v1  ->  broker
```

### 2.2 Directory layout

```
TradingAgent/
  .env                        real keys (gitignored)
  .env.example                template
  README.md
  docs/
    plan/PLAN.md              this document
    reference/                copied source docs (see docs/reference/README.md)
  data/
    trading.db                Agno sessions + audit table
    audit/orders-YYYY-MM-DD.jsonl
  backend/
    requirements.txt
    app/
      __init__.py
      config.py               settings, logging, risk limits
      agent.py                Agent construction + instructions
      openalgo/
        __init__.py
        client.py             shared client, to_thread wrapper, raw_post
        normalize.py          response envelope normalization, error contract
        constants.py          exchanges, products, price types, intervals, offsets
        frames.py             OHLCV frame cache
      indicators/
        __init__.py
        registry.py           the 127-entry IndicatorSpec table
        compute.py            dispatcher: fetch, clean, call, serialize
      safety/
        __init__.py
        risk.py               RiskGuard
        audit.py              AuditLog
      tools/
        __init__.py
        market.py             MarketDataTools
        symbols.py            SymbolTools
        account.py            AccountTools
        orders.py             OrderTools           (confirmation-gated)
        gtt.py                GttTools             (confirmation-gated)
        options.py            OptionsTools         (partly gated)
        indicators.py         IndicatorTools
        system.py             SystemTools          (analyzer, ping, alerts)
      main.py                 FastAPI, SSE, sessions, confirm endpoint
  frontend/
    package.json  vite.config.ts  tsconfig.json  tailwind.config.js  postcss.config.js
    index.html
    src/
      main.tsx  index.css
      App.tsx
      lib/sse.ts  lib/api.ts  lib/format.ts
      components/
        Sidebar.tsx  Message.tsx  ToolTimeline.tsx  ConfirmCard.tsx
        ModeBanner.tsx  Composer.tsx  DataTable.tsx
```

The reference app collapsed everything into one 444-line `App.tsx`. This one does not,
because `ConfirmCard` is safety-critical UI and must be independently reviewable and testable.

### 2.3 Request flow for a trade

1. User: "Buy 50 SBIN at market, MIS."
2. Agent calls `get_quote` (read-only, executes immediately) to price-check.
3. Agent calls `place_order(...)`. The tool is `requires_confirmation=True`, so Agno pauses
   **before** the function body runs.
4. Backend receives `RunPaused`, emits `confirm` with `[r.to_dict() for r in active_requirements]`,
   and **closes the SSE stream**.
5. UI renders a `ConfirmCard` showing tool name, every argument, the notional value, and the
   current mode (LIVE or ANALYZE). Approve / Reject / Reject-with-note.
6. `POST /api/chat/confirm` rehydrates via `agent.get_run_output(run_id, session_id=...)`,
   resolves each requirement, and calls `acontinue_run(..., stream=True, stream_events=True)`.
7. The tool body now runs. **Before touching the broker it calls `RiskGuard.check()`** - a
   deterministic check the model cannot talk its way past. A violation returns a refusal string
   to the model; no order is sent.
8. On success the order id, the full request, and the decision trail are appended to `AuditLog`.
9. Remaining tokens stream to the same UI thread; `done` closes it.

---

## Part 3. The OpenAlgo client layer

### 3.1 One shared client, always off the event loop

```python
# backend/app/openalgo/client.py
class OpenAlgoClient:
    def __init__(self, settings):
        self._api = api(api_key=settings.openalgo_api_key,
                        host=settings.openalgo_host,
                        version=settings.openalgo_api_version,
                        timeout=settings.openalgo_timeout)   # 15s, not the 120s default

    async def call(self, method: str, **kwargs):
        fn = getattr(self._api, method)
        return await asyncio.to_thread(fn, **kwargs)
```

One instance for the process. `httpx.Client` pools connections and is thread-safe, so this is
both correct and faster than a client per call. Toolkit methods stay synchronous - Agno runs
sync tools in a worker thread already - and call a small sync facade; only `main.py` and any
`async def` tool use `call()`. The rule is: **no toolkit method ever blocks the event loop.**

### 3.2 `raw_post` for the 13 unwrapped endpoints

```python
def raw_post(self, endpoint: str, payload: dict) -> dict:
    """Reach a REST endpoint the SDK does not wrap (GTT, multioptiongreeks, ping, pnl)."""
    return self._api._make_request(endpoint, payload)
```

Covered by a smoke test against `/ping`, because `_make_request` is private API and could move
between SDK releases. If it does, the fallback is a direct `httpx.post` with the same payload
shape - documented in the module docstring.

### 3.3 Response normalization and the error contract

OpenAlgo is not uniform. Most resources return `{status, data}`, but order writes return a flat
`{status, orderid}`, batch writes return `{status, results: [...]}`, `/instruments?format=csv`
and `/ticker?format=txt` return non-JSON, and the Telegram webhook returns an empty 200.

`normalize.py` gives every tool one shape:

```python
{"ok": bool, "data": <payload>, "error": <str|None>, "source": "<endpoint>"}
```

Rules, mirroring what the equity agent learned the hard way:

- Agno never truncates tool results and calls `str()` on the return value, so every tool
  returns a **JSON string** through `_trim()` with a hard cap (12,000 chars).
- A falsy return becomes an **empty tool message** and confuses the model. Every path returns
  a non-empty string; the empty case is `{"ok": false, "error": "no_data"}`.
- NaN is not valid JSON. All serialization goes through a NaN-to-null converter.
- Recoverable, model-fixable errors (bad symbol, wrong exchange, unsupported interval) raise
  `RetryAgentRun` with an actionable message - "SBIN not found on NFO; try exchange='NSE', or
  call search_symbols first". Unrecoverable errors return `ok: false` and stop.

### 3.4 The frame cache

Indicator work re-reads the same candles repeatedly. `frames.py` keys on
`(symbol, exchange, interval, start_date, end_date, source)` with a 60-second TTL and a
32-frame LRU bound. It stores the **cleaned** frame (`dropna().sort_index()`) so the NaN rule
from 1.4 is enforced once, at the boundary, rather than in 5 dispatchers.

This is a private cache, not `Toolkit(cache_results=True)` - that one writes JSON to disk and
survives restarts, which is exactly wrong for market data.

---

## Part 4. Tool design

### 4.1 The count problem

A naive one-tool-per-capability mapping gives 57 endpoint tools + 127 indicator tools = 184.
Agno imposes no limit, but its own guidance is to expose only what the task needs, and the
practical ceiling for reliable selection is roughly 15-25 tools. The reference agent runs 10.

Two consolidations bring 184 down to **40**:

- **Indicators collapse 127 into 5 dispatcher tools** backed by a registry (4.3). The model
  picks an indicator by *name*, not by tool, which is the natural fit - `list_indicators` and
  `describe_indicator` make the catalog discoverable without burning 127 schema slots.
- **Endpoints consolidate where the operations are variants of one action**: holidays+timings
  become `get_market_calendar`; the three GTT mutations become `manage_gtt_order(operation=...)`;
  telegram+whatsapp become `send_alert(channel=...)`.

Order tools are deliberately **not** consolidated. `place_order`, `modify_order` and
`cancel_order` stay separate with explicit schemas, because a dispatcher with an `operation`
argument is exactly the shape where a model picks the wrong branch, and here that means the
wrong trade.

**Contingency (decided at Step 0, not later):** if the Step 1.2 validation shows DeepSeek V4
Flash mis-selecting among 40 tools, apply dynamic scoping - `tools` accepts a callable resolved
per run:

```python
def get_tools(run_context: RunContext) -> list:
    base = [MarketDataTools(), SymbolTools(), AccountTools(), IndicatorTools(), SystemTools()]
    if (run_context.session_state or {}).get("trading_enabled"):
        base += [OrderTools(), GttTools(), OptionsTools()]
    return base

Agent(tools=get_tools, cache_callables=False, ...)
```

This drops the default surface to 22 tools and doubles as a second safety layer: a session that
never enabled trading has no order tools in its schema at all. The escalation after that is a
two-member Team (research agent + execution agent), which we avoid unless forced - it adds a
routing hop to a latency-sensitive path.

### 4.2 Toolkit inventory

Forty tools across eight toolkits. `C` marks a `requires_confirmation=True` tool.

**MarketDataTools (6)**

| Tool | Backing call | Notes |
|---|---|---|
| `get_quote` | `quotes` | ltp, ohlc, bid/ask, volume |
| `get_quotes_bulk` | `multiquotes` | list of `{symbol, exchange}`; one round trip |
| `get_market_depth` | `depth` | 5-level book, total buy/sell qty |
| `get_history` | `history` | returns a **summary** (bar count, range, first/last, OHLC stats) plus the last N bars, never the whole frame |
| `get_supported_intervals` | `intervals` | broker-dependent; call before assuming `5m` exists |
| `get_market_calendar` | `holidays` + `timings` | one tool, `year` or `date` |

**SymbolTools (4)**: `search_symbols` (`search`), `get_symbol_info` (`symbol`),
`get_expiry_dates` (`expiry`), `list_instruments` (`instruments`, mandatory exchange +
name/type filter, capped output - the raw master is thousands of rows).

**AccountTools (6)**: `get_funds`, `get_orderbook`, `get_tradebook`, `get_positionbook`,
`get_holdings`, `calculate_margin` (`margin`; note `pricetype` spelling inside the positions
list). `openposition` is folded into `get_positionbook` with an optional symbol filter.

**OrderTools (9)**

| Tool | Backing call | Gate |
|---|---|---|
| `place_order` | `placeorder` | C |
| `place_smart_order` | `placesmartorder` | C - can *double* a position if `position_size` is misread; the confirm card shows current position beside the target |
| `place_basket_order` | `basketorder` | C - card lists every leg |
| `place_split_order` | `splitorder` | C - card shows slice count and total |
| `modify_order` | `modifyorder` | C - **full replacement, not a patch**; card shows before/after |
| `cancel_order` | `cancelorder` | C |
| `cancel_all_orders` | `cancelallorder` | C |
| `close_all_positions` | `closeposition` | C - highest risk in the API; squares off everything at MARKET with no broker-side prompt |
| `get_order_status` | `orderstatus` | read-only |

**GttTools (2)**: `place_gtt_order` (C) and `manage_gtt_order(operation="modify"|"cancel"|"list")`
(C for modify/cancel, not for list). All four go through `raw_post` - the SDK does not wrap GTT.
GTT accepts only `CNC`/`NRML` products and `LIMIT`/`MARKET` price types, and returns **501 in
analyzer mode**, so there is no dry-run path; the tool says so explicitly rather than surfacing
a raw 501.

**OptionsTools (6)**: `get_option_chain` (`optionchain`, with `with_greeks=True` -
free Greeks, no extra broker calls, and unlike `multioptiongreeks` it is not capped at 50
symbols), `resolve_option_symbol` (`optionsymbol`), `get_option_greeks` (`optiongreeks` and
`multioptiongreeks` via `raw_post`), `get_synthetic_future` (`syntheticfuture`),
`place_options_order` (C), `place_options_multi_order` (C - the card renders every leg of the
spread with its resolved strike).

**IndicatorTools (5)**: see 4.3.

**SystemTools (5)**: `get_analyzer_status`, `set_analyzer_mode` (C, and see 5.4),
`get_sandbox_pnl` (`pnl/symbols` via `raw_post`), `ping`, `send_alert(channel=...)`.

### 4.3 The indicator dispatcher

**`registry.py`** holds one `IndicatorSpec` per callable:

```python
@dataclass(frozen=True)
class IndicatorSpec:
    name: str                       # "macd"
    category: str                   # "momentum"
    inputs: tuple[str, ...]         # ("close",) | ("high","low","close") | ("volume",)
    outputs: tuple[str, ...]        # ("macd_line", "signal_line", "histogram")
    params: dict[str, ParamSpec]    # from inspect.signature at import time
    status: str = "ok"              # "ok" | "broken" | "alias"
    note: str = ""
```

`inputs` and `outputs` are **hand-maintained** - they are not derivable at runtime, because
annotations are missing or wrong on 34 methods (1.4 item 8). `params` is read from
`inspect.signature(getattr(ta, name))` at import, so a future SDK release that changes a
default is caught immediately: a startup assertion compares the registry's 127 names against
`dir(ta)` and fails loudly on drift.

`ta.vi` and `ta.ulcerindex` are registered with `status="broken"` and a note; calling them
returns a clear error instead of an all-null array. `ta.uo_oscillator` is `status="alias"` of
`ta.ultimate_oscillator`.

**Five tools:**

1. `list_indicators(category=None, query=None)` - names and one-line descriptions. Cheap
   discovery so the model never guesses a name.
2. `describe_indicator(name)` - required series, every parameter with default and valid enum
   values, output names in order, warm-up length, caveats.
3. `compute_indicator(symbol, exchange, interval, indicator, params={}, start_date=None,
   end_date=None, lookback_bars=300, last_n=10)` - the workhorse.
4. `compute_indicators_batch(symbol, exchange, interval, indicators=[...])` - several
   indicators on **one** cached frame. Prevents five sequential history fetches for
   "show me RSI, MACD, ADX, ATR and Supertrend on SBIN".
5. `scan_symbols(symbols=[...], exchange, interval, indicator, params, condition)` - the
   screener. `condition` is a restricted expression over the indicator's named outputs and
   `close` (`"rsi < 30"`, `"crossover(macd_line, signal_line)"`), parsed with a whitelist
   evaluator - never `eval`. This is where the 13 utility functions earn their place.

`compute.py` executes a fixed pipeline for every call:

```
resolve spec -> reject unknown/broken name
  -> derive date range from lookback_bars, honouring the indicator's warm-up
     (beta needs 253 bars, lrslope 101, crsi 100, pvi_with_signal 255)
  -> fetch frame via frames.py  (already dropna().sort_index())
  -> validate required series exist and are non-empty
  -> coerce every int-typed param with int(); validate string enums against the spec
  -> call getattr(ta, name)(*series, **params)
  -> normalize output: tuple or single, branch on conditional-arity flags
  -> NaN -> null; return last_n values per output plus latest, min, max, and slope sign
```

The warm-up rule matters: asking for `beta` over 100 bars raises inside the library. The
dispatcher pads the request automatically and tells the model what it did.

### 4.4 Result budget

Every tool result passes through `_trim(payload, limit=12_000)`. `get_history` and
`list_instruments` are additionally shaped before trimming - a summary object plus a bounded
tail - because a 437-row candle frame is both useless to the model and expensive. The pattern
is: **compute over the full series server-side, return conclusions plus a small sample.**

### 4.5 Tool errors

- `RetryAgentRun` for anything the model can fix: unknown symbol, wrong exchange for the
  instrument type, unsupported interval, missing expiry.
- Plain `{"ok": false, "error": ...}` for broker/network failures.
- `StopAgentRun` only from `RiskGuard` hard stops (kill switch engaged).
- Never let a raw traceback reach the model - it wastes context and teaches nothing.

---

## Part 5. Safety and risk control

### 5.1 Four independent layers

| Layer | Enforced by | Defeated by |
|---|---|---|
| 1. Instructions | The prompt | Any model mistake. Assume it fails. |
| 2. Tool scoping | `tools` callable factory, `trading_enabled` session state | Nothing the model does - the schema simply is not there |
| 3. Human confirmation | Agno `requires_confirmation` | A user who clicks Approve without reading |
| 4. RiskGuard | Deterministic Python, after confirmation, before the broker | Only a config change |

Layers 2 and 4 are the ones that actually hold. Layer 3 is what makes the agent usable; layer 1
is a nicety. The plan never relies on a layer above the one below it.

### 5.2 The confirmation gate, end to end

Backend:

```python
# streaming leg
elif name == "RunPaused":
    yield sse("confirm", {
        "run_id": ev.run_id,
        "session_id": ev.session_id,
        "requirements": [r.to_dict() for r in ev.active_requirements],
    })
    return                      # terminal for this request - the stream does NOT continue

# POST /api/chat/confirm  {run_id, session_id, decisions: {req_id: bool}, notes: {req_id: str}}
run = agent.get_run_output(run_id=run_id, session_id=session_id)
for req in run.active_requirements:
    if req.needs_confirmation:
        req.confirm() if decisions.get(req.id) else req.reject(notes.get(req.id))
async for ev in agent.acontinue_run(
    run_id=run_id, session_id=session_id,
    requirements=run.requirements,
    stream=True, stream_events=True,        # defaults to False here - must be explicit
):
    ...same event switch...
```

Tests that must exist before the order tools are considered done:

1. Every tool in `OrderTools`, `GttTools`, and the two options order tools asserts
   `fn.requires_confirmation is True`. A typo in `requires_confirmation_tools` only warns
   (1.5), so this test is the only thing standing between a typo and an unconfirmed live order.
2. A rejected order produces no broker call and the model sees the rejection note.
3. A confirmed order executes exactly once across a re-`continue_run` (Agno guards on
   `result is None`, but we verify it).
4. A paused run resumed from a *different* HTTP request works - this is the real deployment
   shape.

### 5.3 RiskGuard

Runs inside every mutating tool, after confirmation, before the broker. Config from `.env`:

```
TRADING_ENABLED         master switch; false -> order tools are not even registered
MAX_ORDER_VALUE         notional cap per order, priced at LTP for MARKET orders
MAX_ORDER_QUANTITY      per-order quantity cap
MAX_ORDERS_PER_SESSION  runaway-loop backstop
ALLOWED_EXCHANGES       default NSE,NFO,BSE,BFO,MCX,CDS
ALLOWED_PRODUCTS        default MIS,CNC,NRML
SYMBOL_DENYLIST         optional
REQUIRE_ANALYZER_MODE   true -> refuse every order while the app is in live mode
```

Also enforced in code, not config:

- **Duplicate suppression**: an identical `(symbol, exchange, action, quantity, product)` within
  10 seconds is refused. Models retry; brokers do not deduplicate.
- **Quote sanity**: for a LIMIT order more than 20 percent away from LTP, refuse and explain.
  Fat-finger prices are the most common real-world loss here.
- **Kill switch**: a `data/KILL` file, checked on every mutating call. Its presence raises
  `StopAgentRun`. This exists so a human can stop the agent without killing the process or
  waiting for a chat turn to finish.

A refusal returns a plain-language string to the model, which then explains it to the user.
The order is never sent.

### 5.4 Analyzer mode is the default

OpenAlgo's analyzer (sandbox) mode simulates order responses. The agent:

- Calls `analyzerstatus` at startup and on every session open, and surfaces the result in a
  persistent UI banner: **ANALYZE** (green) or **LIVE** (red).
- Ships with `REQUIRE_ANALYZER_MODE=true`. Flipping to live is a deliberate act.
- `set_analyzer_mode(live=True)` is confirmation-gated **and** requires the user to type
  `GO LIVE` into the confirm card. Approving a checkbox is too easy for the one action that
  converts every subsequent simulated order into a real one.
- Every order response records the mode it was placed under, so the audit trail is unambiguous.

**Correction, measured during build (iteration 7):** the documented "GTT returns 501 in analyzer
mode" is **not true on this OpenAlgo build**. `placegttorder` in analyzer mode returned
`{"trigger_id": "GTT-260812-...", "mode": "analyze"}` - GTT is sandboxed like every other order
type, so it can be rehearsed. The 501 handler is retained and tested against a synthetic
response, because other builds may still behave as documented.

**Second correction:** the sandbox **rejects MIS orders after 15:15 IST** with "MIS orders
cannot be placed after square-off time". Any test or example that must run at an arbitrary hour
uses CNC.

### 5.5 Audit log

Every mutating tool call appends one JSON line to `data/audit/orders-YYYY-MM-DD.jsonl` and one
row to a SQLite table: timestamp, session_id, run_id, tool, arguments, confirmation decision
and note, RiskGuard verdict, analyzer mode, broker response, order id. Written **before and
after** the broker call, so a crash mid-flight is still visible. This is the record that
answers "why did it place that", and it is the first thing to build after the client layer.

---

## Part 6. Backend API and the SSE contract

### 6.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | model id, missing keys, OpenAlgo reachability, analyzer mode |
| POST | `/api/chat/stream` | `{message, session_id?}` -> SSE |
| POST | `/api/chat/confirm` | `{run_id, session_id, decisions, notes?}` -> SSE (resumed run) |
| POST | `/api/chat/{run_id}/cancel` | `agent.cancel_run` |
| GET | `/api/sessions` | sidebar list |
| GET | `/api/sessions/{id}` | replay messages |
| DELETE | `/api/sessions/{id}` | delete |
| GET | `/api/mode` | analyzer status, polled by the banner |

### 6.2 Event vocabulary

Same transport convention as the reference app - `text/event-stream`, no `event:` lines, a
`type` discriminator inside the JSON, consumed with `fetch` + `getReader` rather than
`EventSource` (which cannot POST). Headers: `Cache-Control: no-cache`,
`Connection: keep-alive`, `X-Accel-Buffering: no`.

| `type` | Source event | Payload |
|---|---|---|
| `start` | RunStarted | `run_id`, `session_id` |
| `token` | RunContent | `delta` |
| `tool_start` | ToolCallStarted | `id`, `name`, `args` |
| `tool_end` | ToolCallCompleted / ToolCallError | `id`, `name`, `ok`, `result` (4k cap), `duration` |
| **`confirm`** | **RunPaused** | **`run_id`, `session_id`, `requirements[]` (each `RunRequirement.to_dict()`), plus a server-computed `preview` per requirement: notional value, current position, mode** |
| `done` | RunCompleted / RunCancelled / fallthrough | `reason`: `stop` \| `cancelled` \| `incomplete` |
| `error` | RunError or an exception | `message`, `kind` |

`confirm` is the only addition to the reference contract, and it is terminal for its request.

Two inherited quirks that must be re-implemented: `ToolCallCompleted` fires with
`tool_call_error=True` and is then followed by a separate `ToolCallError`, so the second is
suppressed by tracking seen ids; and a cancelled run still emits a trailing `RunCompleted`, so
`RunCancelled` is treated as terminal.

### 6.3 Sessions

Agno does not name sessions, so the sidebar would show "New chat" for every thread. As in the
reference app, the first user message titles the session via `db.rename_session`. Session data
is double-encoded JSON in Agno's SQLite schema, so the title reader decodes twice defensively.

`session_state` carries `trading_enabled` and `analyzer_mode` for the tool-scoping factory.

---

## Part 7. Frontend

### 7.1 Stack

Matching the reference app so the two projects stay familiar: React 18.3, Vite 5.4,
TypeScript 5.6, Tailwind 3.4, react-markdown 9 + remark-gfm 4, lucide-react. No component
library, no react-query - the state here is a single stream and a session list.

### 7.2 Components

| Component | Renders |
|---|---|
| `App` | layout, session state, stream orchestration |
| `Sidebar` | session list, new chat, delete |
| `ModeBanner` | ANALYZE / LIVE, always visible, red when live |
| `Message` | markdown with GFM tables - most trading answers are tabular |
| `ToolTimeline` | one row per tool call: name, args, status, duration, collapsible result |
| `ConfirmCard` | the confirmation gate (7.3) |
| `DataTable` | orderbook / positions / holdings / option chain, sortable |
| `Composer` | input, send, stop |

### 7.3 ConfirmCard

The single most important piece of UI in the project. It must make an unreviewed approval
harder than a reviewed one.

- Header states the action in plain words: "Place a LIVE order" or "Simulated order (analyze mode)".
- Every argument is shown as a labelled row. No collapsed JSON blob.
- Server-computed context beside the arguments: notional value, current position in that
  symbol, available funds. The model does not produce these; the backend does.
- Live mode paints the card red and requires the Approve button to be pressed after a
  400ms delay, defeating reflex clicking.
- Reject offers a one-line note; the note is fed back to the model, which usually proposes a
  corrected order.
- Multi-leg orders (basket, split, options multi-leg) render every leg with its resolved symbol.
- The card is disabled and marked resolved once submitted; a stale card from a reloaded page
  cannot double-submit.

### 7.4 Rendering rules

Numbers are formatted Indian-style (lakh/crore grouping) with the exchange's tick precision.
P&L is signed and coloured. Timestamps render in Asia/Kolkata. No icons or emoji anywhere in
code, logs, or model instructions.

---

## Part 8. Agent configuration

```python
Agent(
    id="openalgo-trading-agent",
    name="Trading Agent",
    model=LiteLLM(...),                      # Part 1.2
    db=SqliteDb(db_file=str(DB_PATH)),       # required for HITL
    tools=get_tools,                         # callable factory, Part 4.1
    cache_callables=False,
    description="You are a trading assistant operating an OpenAlgo brokerage connection.",
    instructions=INSTRUCTIONS,
    markdown=True,
    add_datetime_to_context=True,
    timezone_identifier="Asia/Kolkata",
    add_history_to_context=True,
    num_history_runs=5,
    store_history_messages=False,
    store_tool_messages=True,
    tool_call_limit=20,
    max_tool_calls_from_history=6,
    telemetry=False,
    store_events=False,
)
```

`tool_call_limit=20` is higher than the reference agent's 14 because a single trading question
legitimately chains more calls (search symbol, get expiry, resolve option symbol, quote, margin,
place). It is still a bound - a model in a retry loop stops.

`max_tokens=4096` is load-bearing, not a default. This model reasons before it answers, and a
low cap produces an **empty** reply rather than a short one (1.2). Never lower it to save cost.

Instruction blocks (kept short; instructions are layer 1 and do not carry safety weight):

1. **Symbology.** Equity is the bare base symbol. Futures are `[BASE][DDMMMYY]FUT`. Options are
   `[BASE][DDMMMYY][STRIKE][CE|PE]`. Indices live on `NSE_INDEX`/`BSE_INDEX`/`MCX_INDEX`/
   `GLOBAL_INDEX` and are **quote-only, never tradable**. When unsure, call `search_symbols`
   rather than constructing a symbol.
2. **Constants.** Exchanges, products (`MIS`/`CNC`/`NRML`), price types
   (`MARKET`/`LIMIT`/`SL`/`SL-M`), actions. Never invent a value.
3. **Order discipline.** State symbol, exchange, action, quantity, product, price type and the
   current LTP before proposing any order. Never place an order the user did not ask for.
   Never chain orders without a fresh instruction. If a confirmation is rejected, do not re-propose
   the identical order - address the stated reason.
4. **Data discipline.** Quote before you size. Check `get_supported_intervals` before assuming
   an interval. Indicator names come from `list_indicators`, never from memory.
5. **Analyzer awareness.** Always say which mode an order was or would be placed in.
6. **Presentation.** Markdown tables for anything with more than two fields. Money with its
   currency. Never present a simulated fill as a real one.
7. **Boundary.** Execution and analysis, not investment advice. Do not tell the user what to buy.

---

## Part 9. Build order

| # | Step | Deliverable | Gate before moving on |
|---|---|---|---|
| 0 | Model + broker validation | `scripts/validate_setup.py` | **DONE - 26/26 passing (1.6).** Quotes, depth, history and indicators live; streaming, tool calling, and the pause/approve/resume cycle all verified |
| 1 | Scaffolding | `.env`, config, logging, requirements | `python -m app.config` prints resolved settings, no keys leaked |
| 2 | Client layer | `openalgo/client.py`, `normalize.py`, `constants.py` | `ping`, `funds`, `quotes` return normalized dicts; `raw_post` reaches `/ping` |
| 3 | Audit + RiskGuard | `safety/` | Unit tests for every limit, duplicate suppression, kill switch |
| 4 | Read-only toolkits | market, symbols, account, system | Agent answers "what is my P&L and RELIANCE's LTP" end to end |
| 5 | Indicator registry | `indicators/registry.py` | Startup assertion: 127 names match `dir(ta)`; broken pair flagged |
| 6 | Indicator dispatcher | `indicators/compute.py` + toolkit | RSI, MACD, Supertrend, ADX, VWAP, beta computed correctly on real data; int-coercion and warm-up padding tested |
| 7 | SSE backend | `main.py` minus confirm | Streaming, tool timeline, cancel, sessions all work |
| 8 | Frontend shell | App, Sidebar, Message, ToolTimeline, Composer | Full read-only chat usable in the browser |
| 9 | Order toolkits | orders, gtt, options writes - all gated | Confirmation-flag test passes; every order runs in analyzer mode only |
| 10 | Confirm loop | `/api/chat/confirm` + `ConfirmCard` | Approve, reject, reject-with-note, cross-request resume, double-submit guard |
| 11 | Mode + hardening | ModeBanner, GO LIVE flow, DataTable, formatting | Live mode reachable only through the deliberate path |
| 12 | Docs | README, runbook | A new user can go from clone to first simulated order |

Steps 0-8 produce a genuinely useful read-only trading assistant. Nothing can place an order
until step 9, and nothing can place a *live* order until step 11.

---

## Part 10. Conventions

- No icons or emoji in code, comments, logs, or agent instructions.
- ASCII-only logging, plain formatter, stdout reconfigured to UTF-8 with `errors="replace"`
  (the model emits currency signs; cp1252 consoles raise on them). Agno's rich handler is
  replaced before any Agent is constructed.
- Module docstrings record hard-won runtime facts, in the style of the reference app - the
  `price_type`/`pricetype` split, the int-coercion rule, the `RunPaused` terminality. These are
  the notes that stop a future edit from silently reintroducing a bug.
- Type hints and Google-style docstrings on every tool method - Agno builds the JSON schema
  from `get_type_hints` plus a parsed docstring, so a missing `Args:` block produces an
  unusable tool.
- Secrets only in `.env`. `.env` is gitignored; `.env.example` is committed.

---

## Part 11. Risks and open questions

| # | Risk | Mitigation | Status |
|---|---|---|---|
| 1 | DeepSeek V4 Flash tool-selection quality across 40 tools | Correct selection verified at 2 tools (1.6); the 40-tool case is still unproven. Re-measure at Step 4 with the real read-only set; dynamic scoping contingency in 4.1 | **Partly open - re-measure at Step 4** |
| 2 | Baseten streaming + tool-call deltas through LiteLLM + Agno | **Closed.** All three layers verified end to end, including streamed tool-call argument assembly and the paused-run resume (1.6) | Closed |
| 2b | Reasoning tokens make a low `max_tokens` return empty content | `max_tokens=4096`; the trap is asserted in `validate_setup.py` | Mitigated |
| 2c | Reasoning adds ~1.7s before the first content token | Thinking indicator in the UI during that window | Accepted |
| 3 | `client._make_request` is private; GTT depends on it | Verified working against `ping` and `gttorderbook` (1.6); leading-slash rule documented in 1.3; `httpx` fallback documented | Mitigated |
| 4 | `requires_confirmation_tools` typos only warn | Mandatory assertion test (5.2) | Mitigated |
| 5 | GTT cannot be rehearsed (501 in analyzer mode) | Stated in the tool result and the UI | Accepted |
| 6 | `ta.vi` and `ta.ulcerindex` broken in openalgo 2.0.3 | Registered as `broken`, explicit error | Accepted |
| 7 | Indicator registry drifts on an SDK upgrade | Startup assertion against `dir(ta)` | Mitigated |
| 8 | Broker rate limits (10 orders/sec, 50 API/sec) | Client-side token bucket; batch via `multiquotes`/`basketorder` | Planned |
| 9 | A user approves without reading | Server-computed context, red live card, click delay | Partially mitigated - unavoidable residue |

**Assumptions, stated so they can be corrected:**

- OpenAlgo runs locally at `http://127.0.0.1:5000` with a broker already authenticated.
- Indian markets, single broker, single user, local deployment. No auth layer on the FastAPI
  app - it binds to localhost. Any remote deployment needs auth added first, and that is a
  precondition, not an enhancement.
- Python 3.14, matching the environment agno 2.8.7 and litellm 1.79.1 are installed into.

---

## Appendix A. The 127 `ta` callables

Names are the exact method names on the `ta` singleton. Multi-output indicators show their
tuple element names in return order.

**Trend (20)** - `sma` `ema` `wma` `dema` `tema` `hma` `vwma` `alma` `kama` `zlema` `t3`
`frama`(high,low) `trima` `mcginley` `vidya` `alligator`->(jaw,teeth,lips)
`ma_envelopes`->(upper,middle,lower) `supertrend`->(supertrend,direction)
`ichimoku`->(conversion,base,span_a,span_b,lagging) `ckstop`->(stop_long,stop_short)

**Momentum (9)** - `rsi` `macd`->(macd_line,signal_line,histogram)
`stochastic`->(k,d) [args: k_period,smooth_k,d_period] `cci` `williams_r` `bop`(OHLC)
`elderray`->(bull,bear) `fisher`->(fisher,trigger) `crsi`

**Volatility (15)** - `atr` `bbands`->(upper,middle,lower) `keltner`->(upper,middle,lower)
`donchian`->(upper,middle,lower) `chaikin` `natr` `ultimate_oscillator` `true_range`
`massindex` `bbpercent` `bbwidth` `chandelier_exit`->(long_exit,short_exit) `hv`
`ulcerindex` **[broken]** `starc`->(upper,middle,lower)

**Volume (17)** - `obv` `obv_smoothed` `vwap` `mfi` `adl` `cmf` `emv`(high,low,volume)
`force_index` `nvi` `nvi_with_ema`->(nvi,ema) `pvi` `pvi_with_signal`->(pvi,signal)
`volosc`(volume) `vroc`(volume) `kvo`->(kvo,trigger) `pvt` `rvol`(volume)

**Oscillators (19)** - `cmo` `trix` `uo_oscillator` [alias of ultimate_oscillator]
`awesome_oscillator` `accelerator_oscillator` `ppo`->(ppo,signal,histogram) `po` `dpo`
`aroon_oscillator` `stochrsi`->(k,d) `rvi`(OHLC)->(rvi,signal) `cho` `chop`
`kst`->(kst,signal) `tsi`->(tsi,signal) `vi`->(vi_plus,vi_minus) **[broken]** `stc`
`gator_oscillator`->(upper,lower) `coppock`

**Statistical (9)** - `linreg` `lrslope` `correlation`(two series) `beta`(asset,market)
`variance` `tsf` `median` `median_bands`->(median,upper,lower,median_ema) `mode`

**Hybrid (7)** - `adx`->(plus_di,minus_di,adx) `aroon`->(up,down) [period=25]
`pivot_points`->(pivot,r1,s1,r2,s2,r3,s3) `psar` [single series] `dmi`->(plus_di,minus_di)
`fractals`->(up,down) `rwi`->(high,low)

**TA-Lib extras (18)** - undocumented in the category docs, present and working: `mom` `rocp`
`rocr` `rocr100` `midpoint` `apo` `medprice` `typprice` `wclprice` `midprice` `avgprice`
`plus_dm` `minus_dm` `dx` `adxr` `stochf`->(fastk,fastd) `linregangle` `linregintercept`

**Utilities (13)** - all return raw `np.ndarray`: `crossover` `crossunder` `cross` `highest`
`lowest` `change` `roc` `stdev` `exrem` `flip` `valuewhen` `rising` `falling`

Doc-name to method-name mapping for the non-obvious ones is in
`docs/reference/research-notes/notes-indicators.md` section 8.4.

## Appendix B. Endpoint to tool map

| Endpoint | Tool | Endpoint | Tool |
|---|---|---|---|
| `/placeorder` | `place_order` | `/quotes` | `get_quote` |
| `/placesmartorder` | `place_smart_order` | `/multiquotes` | `get_quotes_bulk` |
| `/optionsorder` | `place_options_order` | `/depth` | `get_market_depth` |
| `/optionsmultiorder` | `place_options_multi_order` | `/history` | `get_history` |
| `/basketorder` | `place_basket_order` | `/intervals` | `get_supported_intervals` |
| `/splitorder` | `place_split_order` | `/ticker` | (covered by `get_history`) |
| `/modifyorder` | `modify_order` | `/symbol` | `get_symbol_info` |
| `/cancelorder` | `cancel_order` | `/search` | `search_symbols` |
| `/cancelallorder` | `cancel_all_orders` | `/expiry` | `get_expiry_dates` |
| `/closeposition` | `close_all_positions` | `/instruments` | `list_instruments` |
| `/placegttorder` | `place_gtt_order` | `/optionsymbol` | `resolve_option_symbol` |
| `/modifygttorder` | `manage_gtt_order` | `/optionchain` | `get_option_chain` |
| `/cancelgttorder` | `manage_gtt_order` | `/syntheticfuture` | `get_synthetic_future` |
| `/gttorderbook` | `manage_gtt_order` | `/optiongreeks` | `get_option_greeks` |
| `/orderstatus` | `get_order_status` | `/multioptiongreeks` | `get_option_greeks` |
| `/openposition` | `get_positionbook` | `/market/holidays` | `get_market_calendar` |
| `/funds` | `get_funds` | `/market/timings` | `get_market_calendar` |
| `/margin` | `calculate_margin` | `/analyzer` | `get_analyzer_status` |
| `/orderbook` | `get_orderbook` | `/analyzer/toggle` | `set_analyzer_mode` |
| `/tradebook` | `get_tradebook` | `/pnl/symbols` | `get_sandbox_pnl` |
| `/positionbook` | `get_positionbook` | `/ping` | `ping` |
| `/holdings` | `get_holdings` | `/telegram/notify`, `/whatsapp/notify` | `send_alert` |

Not exposed in v1: `/chart` (UI preference storage), the nine Telegram management routes, and
the undocumented `/portfolio/*` and `/sip/*` routes.

## Appendix C. Reference material

Everything this plan was built from is copied into `docs/reference/`:

| Path | Contents |
|---|---|
| `openalgo-api/` | All 53 OpenAlgo v1 REST endpoint docs, README, rate limiting |
| `openalgo-prompt/` | Symbol format, order constants, Python SDK guide, all 9 indicator docs |
| `agno/` | The Agno documentation tree (examples omitted) |
| `reference-app/` | `equity-research-agent`'s PLAN.md and README.md |
| `research-notes/` | The five verification notes produced for this plan: `notes-indicators.md`, `notes-api.md`, `notes-sdk.md`, `notes-agno.md`, `notes-reference-app.md` |

The research notes are the authoritative detail behind Part 1. Where this plan summarizes, they
carry the measurement.
