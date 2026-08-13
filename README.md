# OpenAlgo Trading Agent

Trade Indian markets by typing what you want. Ask for a quote, an option chain, an indicator or
a screen, and tell it to place the order.

**Every order stops and asks you to approve it before anything reaches your broker.**

Built on [Agno](https://docs.agno.com) with [OpenAlgo](https://docs.openalgo.in) as the broker
connection.

```
You:   Buy 5 shares of TATAMOTORS on NSE at market, CNC.

Agent: [approval card]
       Simulated order (analyze mode)                         place_order
       Symbol TMCV | Action BUY | Exchange NSE | Quantity 5 | Product CNC

       Checked by the server
       Mode analyze | Notional Rs 2,285.25 | LTP Rs 457.05
       Current position 0 | Available funds Rs 99,99,954.04

       [Approve]  [Reject]
```

Nothing is sent until you click Approve. The numbers under "Checked by the server" are fetched by
the backend, not written by the AI, so they are worth trusting even if the reply above them is
wrong.

## Things you can ask it

**Prices and charts**

- What is RELIANCE trading at?
- Show me the market depth for SBIN
- NIFTY and BANKNIFTY quotes side by side
- How has INFY moved over the last 10 days on the 15 minute chart?

**Indicators** - all 127 from OpenAlgo's library

- 14 period RSI for SBIN on the 15 minute chart
- Show me RSI, MACD, ADX and Supertrend for RELIANCE together
- Which of SBIN, TCS, INFY, WIPRO have RSI below 30?
- Where is MACD crossing over on my watchlist?
- Correlation between SBIN and NIFTY over 20 days

**Options**

- Current month NIFTY straddle symbols
- NIFTY option chain for the nearest expiry with Greeks
- What is the ATM call for BANKNIFTY this expiry?
- Margin required for a NIFTY iron condor

**Your account**

- What are my funds?
- Show my open positions and P&L
- What did I trade today?
- My holdings with current value

**Orders** - each one asks you to approve first

- Buy 10 shares of RELIANCE at market, CNC
- Sell 50 SBIN at limit 1085, MIS
- Buy 1 lot of NIFTY 24300 CE
- Modify order 250408001002736 to limit 16.50
- Cancel all open orders
- Square off all positions

It knows OpenAlgo's symbol format, so "NIFTY 50" and "BANK NIFTY" resolve to the right
instruments, and it looks option symbols up rather than guessing them.

## Safety

**Analyzer mode is the default.** OpenAlgo simulates orders until you deliberately switch it off,
and `REQUIRE_ANALYZER_MODE=true` refuses live orders even then. You have to mean it.

Four independent layers, because only the lower two really hold:

| Layer | What it does | What defeats it |
|---|---|---|
| 1. Instructions | Tells the AI the rules | Any AI mistake. Assume it fails |
| 2. Tool scoping | With trading off, order tools do not exist for the AI at all | Nothing the AI does |
| 3. Your approval | Every order pauses for a click | Approving without reading |
| 4. RiskGuard | Deterministic checks after approval, before the broker | Only a config change |

RiskGuard refuses, in order: the kill switch, `TRADING_ENABLED`, live mode, denylisted symbols,
exchanges outside your allowlist, index symbols (which cannot be traded), products outside your
allowlist, bad quantities, your per-session order cap, **duplicate orders within 10 seconds**,
your notional cap, and a **fat-finger guard on any limit price more than 20 percent from the last
traded price**.

Every order is written to an audit trail twice, before the broker is touched and after, in
`data/audit/orders-YYYY-MM-DD.jsonl` and a SQLite table. That is the record of what happened and
why.

**Stop everything instantly**, without restarting anything:

```bash
touch data/KILL
```

Every order tool refuses while that file exists.

## Setup

You need Python 3.14, Node 18+, and **OpenAlgo already running with your broker logged in**.

```bash
git clone https://github.com/marketcalls/TradingAgent
cd TradingAgent

cp .env.example .env

pip install -r backend/requirements.txt
cd frontend && npm install && cd ..
```

Two keys go in `.env`:

| Key | Where it comes from |
|---|---|
| `OPENALGO_API_KEY` | The OpenAlgo web app, after logging in with your broker |
| `LITELLM_API_KEY` | Your AI provider. Leave empty to run a local model with Ollama |

Everything else has a working default. `.env` is gitignored, so your keys stay local.

## Run

```bash
# terminal 1 - backend
cd backend
python -m uvicorn app.main:app --port 8088

# terminal 2 - frontend
cd frontend
npm run dev
```

Open **http://localhost:5173**.

Use `localhost`, not `127.0.0.1`: Vite binds the hostname only, so `http://127.0.0.1:5173` is
refused even while the server is up.

### Before you place anything

1. The banner at the top should read **ANALYZE**, meaning orders are simulated.
2. Ask it "what are my funds?" - if that works, the broker connection is good.
3. Order tools are **off by default**. To use them, set `TRADING_ENABLED=true` in `.env` and
   restart the backend. Keep `REQUIRE_ANALYZER_MODE=true` until you genuinely intend to trade
   real money.

## If something is not working

| What you see | What it means |
|---|---|
| "I do not have an order-placement tool" | `TRADING_ENABLED=false`. Set it true and restart the backend |
| Every answer fails | OpenAlgo is not running, or the API key is wrong. Run `curl http://127.0.0.1:8088/api/health` and look for `openalgo_connected: true` |
| The page will not load | You used `127.0.0.1:5173`. Use `localhost:5173` |
| A change to `.env` did nothing | The backend reads `.env` once at startup. Restart it |
| "No order was placed" warning under a reply | The AI described an order without actually submitting it. Ask again |
| Symbol not found | Ask it to search, for example "find the symbol for Tata Motors". Names change; `TATAMOTORS` became `TMCV` after the demerger |
| It refuses your order | Read the reason. RiskGuard names the exact limit it hit |

## The stack

### Backend

| Piece | Version | What it does |
|---|---|---|
| [Agno](https://docs.agno.com) | 2.8.7 | Agent loop, tool calling, the approval gate, session storage |
| [LiteLLM](https://docs.litellm.ai) | 1.79.1 | Model routing to any provider |
| [OpenAlgo SDK](https://docs.openalgo.in) | 2.0.3 | Broker connection, REST on `127.0.0.1:5000` |
| FastAPI + Uvicorn | 0.115+ / 0.32+ | HTTP API and SSE streaming |
| OpenAlgo `ta` | bundled | 127 indicators, Rust-backed |
| pandas / numpy | 2.2+ / 1.26+ | Candle handling for indicators |
| SQLite | stdlib | Sessions, paused orders awaiting approval, audit trail |
| Python | 3.14 | |

### Frontend

| Piece | Version | Notes |
|---|---|---|
| React | 19.2 | |
| TypeScript | 7.0 | The native compiler |
| Vite | 8.2 | |
| Tailwind CSS | 4.3 | CSS-first: the theme lives in `@theme` in `src/index.css`, and there is no `tailwind.config.js` or PostCSS step |
| react-markdown + remark-gfm | 10.1 / 4.0 | GFM tables, which most answers use |
| lucide-react | 1.31 | Icons for UI chrome |

### Models

Any LiteLLM provider: Baseten, OpenAI, Anthropic, Gemini, Groq, OpenRouter, or a local model
through Ollama with no API key. See [docs/model-notes.md](docs/model-notes.md).

**43 tools** across 8 toolkits, covering 43 of OpenAlgo's endpoints. 13 of them can move money,
and all 13 require your approval.

## Validate

```bash
python scripts/validate_setup.py            # broker, model and approval gate
python backend/tests/test_client_layer.py
python backend/tests/test_safety.py         # every RiskGuard limit
python backend/tests/test_registry.py       # all 127 indicators
python backend/tests/test_tools_readonly.py
python backend/tests/test_indicators.py
python backend/tests/test_tools_orders.py
python backend/tests/test_confirm_loop.py   # full approval round trip over HTTP
python backend/tests/test_hitl_models.py    # the approval gate on every model
```

The order tests refuse to run unless OpenAlgo reports analyzer mode, so they never place a real
order.

## Layout

```
backend/app/
  version.py          the single source of truth for the version
  config.py           settings and logging
  agent.py            model wiring, instructions, per-run tool scoping
  main.py             FastAPI, SSE streaming, the approval loop, sessions
  openalgo/           broker client, response handling, symbols, candle cache
  indicators/         127-indicator registry and dispatcher
  safety/             RiskGuard and the audit trail
  tools/              8 toolkits, 43 tools
frontend/src/
  components/         Sidebar, ModeBanner, Message, ToolTimeline,
                      ConfirmCard, DataTable, Composer, ThinkingSelector
docs/
  CHANGELOG.md        what changed, per version
  model-notes.md      choosing a model, and the provider bugs worked around
  plan/PLAN.md        the design document
  progress/           what was built and validated, per iteration
```

## Documentation

- **[docs/model-notes.md](docs/model-notes.md)** - choosing a model, thinking control, and the
  provider bugs the agent works around automatically.
- **[docs/CHANGELOG.md](docs/CHANGELOG.md)** - what changed in each version.
- **[docs/plan/PLAN.md](docs/plan/PLAN.md)** - the design document.
- **[docs/progress/](docs/progress/)** - each build iteration with its real validation output.

## Version

Maintained in `backend/app/version.py` as the single source of truth, and served on
`GET /api/health`.

## Not investment advice

This software carries out your instructions and shows you data. It does not give investment
advice and will not tell you what to buy or sell. Trading carries risk of loss. You are
responsible for every order you approve.

## Licence

MIT - see [LICENSE](LICENSE).
