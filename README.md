# OpenAlgo Trading Agent

A chat-first trading agent for Indian markets. Ask it for quotes, positions, option chains or
any of 127 technical indicators, and instruct it to place orders. Every order stops and asks for
your approval before anything reaches the broker.

Built on [Agno](https://docs.agno.com) with [OpenAlgo](https://docs.openalgo.in) as the broker
gateway, a FastAPI + SSE backend, and a React frontend.

```
You:   Buy 5 shares of TATAMOTORS on NSE at market, CNC.

Agent: [confirmation card]
       Simulated order (analyze mode)                         place_order
       Symbol TMCV | Action BUY | Exchange NSE | Quantity 5 | Product CNC

       Checked by the server
       Mode analyze | Notional Rs 2,285.25 | LTP Rs 457.05
       Current position 0 | Available funds Rs 99,99,954.04

       [Approve]  [Reject]
```

Nothing is sent until you click Approve.

## What it can do

- **Market data** - quotes, bulk quotes, five-level depth, historical candles, intervals,
  holidays and session timings.
- **Symbols** - free-text search, contract details with lot and tick size, expiry lists, and a
  filtered instrument master.
- **Account** - funds, order book, trade book, positions, holdings, and pre-trade margin.
- **Indicators** - all 127 callables from OpenAlgo's `ta` library behind five tools: list,
  describe, compute, batch-compute, and a multi-symbol screener.
- **Options** - option chain with Greeks, ATM/ITM/OTM symbol resolution, single and batch
  Greeks, synthetic future, and multi-leg orders.
- **Orders** - market, limit, SL and SL-M; smart, basket and split orders; modify and cancel;
  cancel-all and square-off-all; GTT.

43 tools in total. 13 of them mutate something, and all 13 are confirmation-gated.

## Safety

Four independent layers, because only the lower two actually hold:

| Layer | Enforced by | Defeated by |
|---|---|---|
| 1. Instructions | The prompt | Any model mistake. Assume it fails. |
| 2. Tool scoping | Tools resolved per run from session state | Nothing the model does - the schema is not there |
| 3. Human confirmation | Agno `requires_confirmation` | A user who clicks Approve without reading |
| 4. RiskGuard | Deterministic Python, after approval, before the broker | Only a config change |

RiskGuard checks, in order: kill switch file, `TRADING_ENABLED`, analyzer mode, symbol denylist,
exchange allowlist (index feeds are refused as untradable), product allowlist, quantity bounds,
per-session order cap, duplicate suppression within 10 seconds, notional cap, and a fat-finger
guard that refuses a limit price more than 20 percent from the last traded price.

Every mutating call is written to an audit trail twice - before the broker is touched and after -
in `data/audit/orders-YYYY-MM-DD.jsonl` and a SQLite table.

**Analyzer mode is the default.** OpenAlgo simulates orders until you deliberately turn it off,
and `REQUIRE_ANALYZER_MODE=true` refuses live orders regardless.

## Setup

Requires Python 3.14, Node 18+, and a running OpenAlgo instance with a broker session.

```bash
git clone https://github.com/marketcalls/TradingAgent
cd TradingAgent

cp .env.example .env      # then fill in the two keys below

pip install -r backend/requirements.txt
cd frontend && npm install && cd ..
```

Two keys are required in `.env`:

| Key | Where it comes from |
|---|---|
| `OPENALGO_API_KEY` | The OpenAlgo web app, after logging in with your broker |
| `LITELLM_API_KEY` | Your model provider. Leave empty for a local Ollama model |

Everything else has a working default. `.env` is gitignored.

## Run

```bash
# terminal 1
cd backend && python -m uvicorn app.main:app --port 8088

# terminal 2
cd frontend && npm run dev
```

Open http://localhost:5173.

To let the agent place orders at all, set `TRADING_ENABLED=true`. Keep
`REQUIRE_ANALYZER_MODE=true` until you genuinely intend to trade real money.

To stop everything immediately without restarting, create the kill switch file:

```bash
touch data/KILL
```

Every mutating tool refuses while it exists.

## Validate

```bash
python scripts/validate_setup.py            # 26 checks: broker, model, confirmation gate
python backend/tests/test_client_layer.py   # 44
python backend/tests/test_safety.py         # 48
python backend/tests/test_registry.py       # 21
python backend/tests/test_tools_readonly.py # 32
python backend/tests/test_indicators.py     # 64
python backend/tests/test_tools_orders.py   # 114
python backend/tests/test_confirm_loop.py   # 25, full HTTP round trip
```

374 checks. The order tests refuse to run unless OpenAlgo reports analyzer mode, so they never
place a real order.

## Model

Everything goes through LiteLLM, so any provider works - cloud or local. There is **one key**
and one optional base URL; the prefix on `LITELLM_MODEL` decides the provider.

```bash
# Baseten (default)
LITELLM_MODEL=baseten/deepseek-ai/DeepSeek-V4-Flash-0731
LITELLM_API_KEY=<baseten key>

# OpenAI
LITELLM_MODEL=openai/gpt-5.2
LITELLM_API_KEY=sk-<openai key>

# Local, via Ollama - no key at all
LITELLM_MODEL=ollama_chat/llama3.1
LITELLM_API_KEY=
```

Anthropic, Gemini, Groq, OpenRouter and LM Studio follow the same shape. `.env.example` lists
them.

### The agent needs tool calling

Every answer comes from a tool, so a model that cannot call tools is useless here.

**Running locally with Ollama:** use the `ollama_chat/` prefix and a tool-capable model
(`ollama show <model>` lists `tools` under capabilities). Two LiteLLM bugs sit on that path,
both corrected automatically in `backend/app/model_providers.py`:

1. **LiteLLM's model table is stale.** It reports `supports_function_calling=False` for most
   Ollama tags and falls back to a JSON-emulation path that either crashes on the turn after a
   tool result or returns an empty reply. The agent asks Ollama for the model's real
   capabilities and registers the truth.

2. **LiteLLM drops `tool_calls` from assistant messages.** `OllamaChatConfig.transform_request`
   converts them, then builds the outgoing message copying only role, thinking, content and
   images. Ollama therefore sees a tool result for a call it has no record of, so the model
   calls the tool again - forever, with an empty final answer. The agent restores them.

   The same exchange against Ollama's native `/api/chat` answered correctly and called nothing,
   which is how this was isolated: the model was never the problem.

### Tool profile

`TOOL_PROFILE` is `full` (43 tools) or `lean` (15). Empty picks automatically: **lean for local
models, full otherwise**.

Small models cannot choose reliably among 43 tools. With `gemma4:e4b` (8B) on the full set, a
simple quote request produced 16 tool calls against the wrong symbol and an answer about market
depth; asked for funds, it claimed it had no such function while `get_funds` was in scope.

On the lean profile, with both LiteLLM bugs corrected, the same model answers each of those in
**a single tool call** with a correct markdown table. Lean also shortens the instruction set and
caps `tool_call_limit` at 6, so a confused model stops instead of looping.

Set `TOOL_PROFILE=full` to override, at the cost of reliability on a small model.

### The reasoning-model trap

The default Baseten model is a **reasoning model**: it spends completion tokens on hidden
reasoning before emitting any content, so a small `max_tokens` returns an **empty** reply rather
than a short one. `LITELLM_MAX_TOKENS=4096` is a correctness requirement, not a cost dial.
Time to first visible token is around 1.7 seconds.

## Layout

```
backend/app/
  config.py           settings and ASCII logging
  agent.py            model wiring, instructions, per-run tool scoping
  main.py             FastAPI, SSE, the confirmation loop, sessions
  openalgo/           client, response envelope, constants, frame cache
  indicators/         127-entry registry, descriptions, dispatcher
  safety/             RiskGuard, AuditLog
  tools/              8 toolkits, 43 tools
frontend/src/
  lib/                sse, api, Indian number formatting
  components/         Sidebar, ModeBanner, Message, ToolTimeline,
                      ConfirmCard, DataTable, Composer
docs/
  plan/PLAN.md        the build plan
  progress/           what was built and validated, per iteration
  reference/          research notes behind the plan
```

## Documentation

`docs/plan/PLAN.md` is the design document. `docs/progress/` records each iteration with real
validation output. `docs/reference/research-notes/` holds the notes behind the plan, written by
executing the installed libraries rather than reading their docs - which is how several
documented behaviours turned out to be wrong.

## Not investment advice

This software executes instructions and presents data. It does not give investment advice.
Trading carries risk of loss. You are responsible for every order you approve.

## Licence

MIT - see [LICENSE](LICENSE).
