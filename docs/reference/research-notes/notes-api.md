# OpenAlgo v1 REST API — Agent-Tool Reference Note

Source of truth: `d:\AI Bootcamp 2026\Day08\openalgo\docs\api\**` plus
`docs\prompt\order-constants.md` and `docs\prompt\symbol-format.md`.
Cross-checked against `restx_api/__init__.py`, `restx_api/data_schemas.py`, `utils/constants.py`.

---

## 1. Base URLs, Auth, Transport

```text
REST:      http://127.0.0.1:5000/api/v1
WebSocket: ws://127.0.0.1:8765          (NOT under /api/v1)
```
Ngrok/custom-domain variants are the same paths on `https://<domain>/api/v1/...`.

- **POST endpoints**: API key goes in the JSON body as `apikey`.
- **GET endpoints** (`/ticker/<symbol>`, `/instruments`, `/chart`, `/telegram/*` GETs): API key as `apikey` query param.
- Telegram/WhatsApp management endpoints also accept `X-API-KEY` header.
- Telegram webhook authenticates with `X-Telegram-Bot-Api-Secret-Token` (NOT an OpenAlgo key).
- Never send broker credentials/tokens. The OpenAlgo key resolves the broker session server-side.
- Content type for POST: `application/json`.

**Registered surface: 57 method/path pairs** (a resource with both GET and POST counts twice).

---

## 2. COMPLETE ENDPOINT INVENTORY

Legend for "Response shape": `{status,data}` = standard envelope; anything else is flagged.

### 2.1 Order Management (14 endpoints — ALL POST)

| Method | Path | Purpose | Required params | Optional params (default) | Response shape |
|---|---|---|---|---|---|
| POST | `/placeorder` | Place a single order | `apikey`, `strategy`, `symbol`, `action`, `exchange`, `quantity` | `pricetype` (MARKET), `product` (MIS), `price` (0), `trigger_price` (0), `disclosed_quantity` (0) | **NON-STANDARD**: `{status, orderid, message?, mode?}` — no `data` key |
| POST | `/placesmartorder` | Reconcile symbol position to a target size | `apikey`, `strategy`, `exchange`, `symbol`, `action`, `quantity`, `position_size` | `product` (MIS), `pricetype` (MARKET), `price` (0), `trigger_price` (0), `disclosed_quantity` (0) | **NON-STANDARD**: `{status, orderid, message?, mode?}` |
| POST | `/optionsorder` | Place option order by ATM/ITMn/OTMn offset (auto strike resolution) | `apikey`, `strategy`, `underlying`, `exchange`, `offset`, `option_type`, `action`, `quantity` | `expiry_date` (derived when `underlying` embeds expiry), `pricetype` (MARKET), `product` (MIS), `splitsize` (0), `price` (0), `trigger_price` (0) | **NON-STANDARD**: `{status, orderid, symbol, exchange, offset, option_type, underlying, underlying_ltp, mode?}` |
| POST | `/optionsmultiorder` | Multi-leg options strategy (1–20 legs) | `apikey`, `strategy`, `underlying`, `exchange`, `legs[]` | `expiry_date` (common, overridable per leg) | **NON-STANDARD**: `{status, underlying, underlying_ltp, results[]}` |
| POST | `/basketorder` | Multiple independent orders in one call | `apikey`, `strategy`, `orders[]` | — | **NON-STANDARD**: `{status, results[]}` |
| POST | `/splitorder` | Split a large qty into chunks (max 100 child orders) | `apikey`, `strategy`, `symbol`, `exchange`, `action`, `quantity`, `splitsize` | `pricetype` (MARKET), `product` (MIS), `price` (0), `trigger_price` (0) | **NON-STANDARD**: `{status, split_size, total_quantity, results[]}` |
| POST | `/modifyorder` | Modify an open order (full replacement) | `apikey`, `orderid`, `strategy`, `symbol`, `action`, `exchange`, `pricetype`, `product`, `quantity`, `price`, `trigger_price`, `disclosed_quantity` — **all 12 mandatory** | — | **NON-STANDARD**: `{status, orderid, message?, mode?}` |
| POST | `/cancelorder` | Cancel one order | `apikey`, `orderid` | `strategy` | **NON-STANDARD**: `{status, orderid, message?, mode?}` |
| POST | `/cancelallorder` | Cancel every open + trigger-pending order | `apikey` | `strategy` | **NON-STANDARD**: `{status, message, canceled_orders[], failed_cancellations[], mode?}` |
| POST | `/closeposition` | Square off ALL open positions (market orders) | `apikey` | `strategy` | **NON-STANDARD**: `{status, message, mode?}` |
| POST | `/placegttorder` | Place GTT SINGLE / OCO trigger | `apikey`, `strategy`, `trigger_type`, `exchange`, `symbol`, `action`, `product`, `quantity`, `price` | `pricetype` (LIMIT), `triggerprice_sl` (0), `triggerprice_tg` (0), `stoploss` (null), `target` (null) — conditional, see §3 | **NON-STANDARD**: `{status, trigger_id, message?}` |
| POST | `/modifygttorder` | Replace an active GTT spec | same as placegttorder **plus** `trigger_id` | same as placegttorder | **NON-STANDARD**: `{status, trigger_id, message?}` |
| POST | `/cancelgttorder` | Cancel an active GTT trigger | `apikey`, `strategy`, `trigger_id` | — | **NON-STANDARD**: `{status, trigger_id, message?}` |
| POST | `/gttorderbook` | List **active-only** GTT triggers | `apikey` | — | `{status, data[]}` (standard) |

### 2.2 Order / Account Information (8 endpoints — ALL POST)

| Method | Path | Purpose | Required params | Optional params | Response shape |
|---|---|---|---|---|---|
| POST | `/orderstatus` | Status of one order | `apikey`, `orderid` | `strategy` | `{status, data:{orderid, symbol, exchange, action, quantity, price, trigger_price, pricetype, product, order_status, average_price, timestamp}}` |
| POST | `/openposition` | Net qty for symbol+exchange+product | `apikey`, `symbol`, `exchange`, `product` | `strategy` | **NON-STANDARD**: `{status, quantity}` (quantity is a **string**) |
| POST | `/funds` | Account funds | `apikey` | — | `{status, data:{availablecash, collateral, m2mrealized, m2munrealized, utiliseddebits}}` (all strings) |
| POST | `/margin` | Pre-trade margin for a basket (max 50 positions) | `apikey`, `positions[]` (each: `symbol`, `exchange`, `action`, `quantity`, `product`, `pricetype`; optional `price` 0) | — | `{status, data:{total_margin_required, span_margin, exposure_margin, margin_benefit}}` |
| POST | `/orderbook` | All orders for the day + stats | `apikey` | — | `{status, data:{orders[], statistics:{total_buy_orders,total_sell_orders,total_completed_orders,total_open_orders,total_rejected_orders}}}` |
| POST | `/tradebook` | Executed trades for the day | `apikey` | — | `{status, data:[{orderid,symbol,exchange,action,quantity,average_price,product,timestamp,trade_value}]}` |
| POST | `/positionbook` | All positions (incl. qty 0) | `apikey` | — | `{status, data:[{symbol,exchange,product,quantity,average_price,ltp,pnl}]}` (values are strings) |
| POST | `/holdings` | Delivery holdings + portfolio stats | `apikey` | — | `{status, data:{holdings[], statistics:{totalholdingvalue,totalinvvalue,totalprofitandloss,totalpnlpercentage}}}` |

### 2.3 Market Data (6 endpoints)

| Method | Path | Purpose | Required params | Optional params (default) | Response shape |
|---|---|---|---|---|---|
| POST | `/quotes` | Single-symbol quote | `apikey`, `symbol`, `exchange` | — | `{status, data:{open,high,low,ltp,ask,bid,prev_close,volume}}` |
| POST | `/multiquotes` | Batch quotes | `apikey`, `symbols[]` (each `{symbol, exchange}`) | — | **NON-STANDARD**: `{status, results:[{symbol,exchange,data{...},error?}]}` — `results`, not `data`. Per-item `data` adds `oi`. |
| POST | `/depth` | Level-2 depth (top 5) | `apikey`, `symbol`, `exchange` | — | `{status, data:{open,high,low,ltp,ltq,prev_close,volume,oi,totalbuyqty,totalsellqty,asks[5],bids[5]}}` |
| POST | `/history` | Historical OHLCV candles | `apikey`, `symbol`, `exchange`, `interval`, `start_date` (YYYY-MM-DD), `end_date` (YYYY-MM-DD) | `source` (`"api"` default; `"db"` = DuckDB/Historify) — validated OneOf `["api","db"]` | `{status, data:[{timestamp,open,high,low,close,volume}]}`. **timestamps are epoch numbers** despite the doc's IST-formatted sample; convert client-side. OI included by default for F&O. |
| POST | `/intervals` | Broker-supported intervals | `apikey` | — | `{status, data:{months[],weeks[],days[],hours[],minutes[],seconds[]}}` |
| GET | `/ticker/<string:symbol>` | Ticker-compatible history (broker API only) | query: `apikey`, `from` (YYYY-MM-DD), `to` (YYYY-MM-DD); path `symbol` as `EXCHANGE:SYMBOL` e.g. `NSE:RELIANCE` | `interval` (D), `format` (`json` \| `txt`, default json) | `json`: `{status, data:[{timestamp(epoch int),open,high,low,close,volume}]}`. **`format=txt` returns PLAIN TEXT CSV rows** — daily `Ticker,Date,Open,High,Low,Close,Volume`; intraday adds time after date. Path must contain exactly one colon (a missing colon silently falls back to `NSE:RELIANCE` — do not rely on it). |

### 2.4 Symbol Services (4 endpoints)

| Method | Path | Purpose | Required params | Optional params (default) | Response shape |
|---|---|---|---|---|---|
| POST | `/symbol` | Symbol metadata + broker mapping | `apikey`, `symbol`, `exchange` | — | `{status, data:{id,name,symbol,brsymbol,exchange,brexchange,instrumenttype,expiry,strike,lotsize,tick_size,freeze_qty,token}}` |
| POST | `/search` | Fuzzy symbol search | `apikey`, `query`, `exchange` | — | `{status, message, data[]}` (adds `message` = "Found N matching symbols") |
| POST | `/expiry` | Expiry dates for an underlying | `apikey`, `symbol`, `exchange`, `instrumenttype` | — | `{status, message, data:["DD-MMM-YY", ...]}` ascending |
| GET | `/instruments` | Full instrument master dump | query: `apikey` | `exchange` (all exchanges), `format` (`json` \| `csv`, default json) | `json`: `{status, message, data[]}`. **`format=csv` returns `Content-Type: text/csv`**, filename `instruments_<exchange>.csv` / `instruments_all.csv`. Errors: 400 bad param, 401 key missing, 403 key invalid, 500 DB failure. |

### 2.5 Options Analytics (5 endpoints — ALL POST)

| Method | Path | Purpose | Required params | Optional params (default) | Response shape |
|---|---|---|---|---|---|
| POST | `/optionsymbol` | Resolve offset → concrete option symbol | `apikey`, `underlying`, `exchange` (NSE_INDEX/BSE_INDEX), `expiry_date` (DDMMMYY), `offset`, `option_type` | — | **NON-STANDARD (flat)**: `{status, symbol, exchange, lotsize, tick_size, freeze_qty, underlying_ltp}` |
| POST | `/optionchain` | Full chain + optional IV/Greeks | `apikey`, `underlying`, `exchange`, `expiry_date` (DDMMMYY) | `strike_count` (all strikes; 1–100), `with_greeks` (false), `interest_rate` (0; 0–100) | **NON-STANDARD (flat)**: `{status, underlying, underlying_ltp, underlying_prev_close, expiry_date, expiry_ts, server_ts, atm_strike, quotes_included, greeks_included, forward_price, chain[]}`; each `chain[i]` = `{strike, ce\|null, pe\|null}` with `label` = ATM/ITMn/OTMn |
| POST | `/syntheticfuture` | Put-call-parity forward | `apikey`, `underlying`, `exchange` (NSE_INDEX/BSE_INDEX), `expiry_date` | — | **NON-STANDARD (flat)**: `{status, underlying, underlying_ltp, expiry, atm_strike, synthetic_future_price}` |
| POST | `/optiongreeks` | Greeks + IV for one option | `apikey`, `symbol`, `exchange` (NFO/BFO/CDS/MCX/CRYPTO) | `interest_rate` (exchange default), `underlying_symbol` (derived), `underlying_exchange` (NSE_INDEX), `forward_price`, `expiry_time` ("HH:MM") | **NON-STANDARD (flat)**: `{status, symbol, exchange, underlying, strike, option_type, expiry_date, days_to_expiry, spot_price, option_price, interest_rate, implied_volatility, greeks:{delta,gamma,theta,vega,rho}}` |
| POST | `/multioptiongreeks` | Greeks for 1–50 options | `apikey`, `symbols[]` (each `{symbol, exchange}`, optional `underlying_symbol`, `underlying_exchange`) | `interest_rate` (0–100), `expiry_time` ("HH:MM") | `{status, data[], summary:{total,success,failed}}` — batch is "success" even with per-item failures; **inspect every item**. |

### 2.6 Market Calendar (2 endpoints — POST)

| Method | Path | Purpose | Required params | Optional params (default) | Response shape |
|---|---|---|---|---|---|
| POST | `/market/holidays` | Holidays for a year | `apikey` | `year` (current year; must be 2020–2050) | **NON-STANDARD**: `{status, year, timezone, data:[{date, description, holiday_type, closed_exchanges[], open_exchanges[{exchange,start_time,end_time}]}]}` — times in **epoch ms** |
| POST | `/market/timings` | Session times for a date | `apikey`, `date` (YYYY-MM-DD, 2020-01-01..2050-12-31) | — | `{status, data:[{exchange, start_time, end_time}]}` — epoch ms; **empty array = weekend / full holiday** (this is the substitute for a "checkholiday" endpoint, which does NOT exist) |

### 2.7 Analyzer / Sandbox (3 endpoints — POST)

| Method | Path | Purpose | Required params | Optional | Response shape |
|---|---|---|---|---|---|
| POST | `/analyzer` | Read analyzer mode state | `apikey` | — | `{status, data:{analyze_mode(bool), mode("analyze"\|"live"), total_logs}}` |
| POST | `/analyzer/toggle` | Switch analyzer mode | `apikey`, `mode` (**boolean**: true=analyze, false=live) | — | `{status, data:{analyze_mode, message, mode, total_logs}}` |
| POST | `/pnl/symbols` | Sandbox P&L by symbol | `apikey` | — | **NON-STANDARD**: `{status, data[], total_pnl, total_unrealized_pnl, total_today_realized_pnl, total_pnl_today, mode}`. **HTTP 400 if called in live mode**; 403 on bad key. |

### 2.8 Chart Preferences (2 endpoints)

| Method | Path | Purpose | Required params | Optional | Response shape |
|---|---|---|---|---|---|
| GET | `/chart` | Read per-API-key chart prefs | query `apikey` | — | `{status, data:{...all stored keys}}` |
| POST | `/chart` | Update chart prefs | `apikey` + **at least one** preference key | arbitrary keys | `{status, ...}`. Limits: ≤50 keys per request, key name ≤50 chars, each JSON-serialized value ≤1 MiB. Invalid key → 403. |

### 2.9 Utility (1 endpoint)

| Method | Path | Purpose | Required params | Response shape |
|---|---|---|---|---|
| POST | `/ping` | Verify key resolves to a live broker session | `apikey` | `{status, data:{message:"pong", broker:"<brokername>"}}`. **Not** an anonymous health check — invalid key or dead broker session → **403**. |

### 2.10 Messaging (12 endpoints: Telegram 11 + WhatsApp 1)

| Method | Path | Purpose | Auth | Notes / response |
|---|---|---|---|---|
| GET | `/telegram/config` | Read config (bot token masked) | API key | `{status, data}` |
| POST | `/telegram/config` | Update accepted config fields | API key | `{status, ...}` |
| POST | `/telegram/start` | Start polling/webhook mode | API key | `{status, ...}` |
| POST | `/telegram/stop` | Stop bot service | API key | `{status, ...}` |
| POST | `/telegram/webhook` | Telegram update receiver | `X-Telegram-Bot-Api-Secret-Token` | **NON-STANDARD**: validated updates get an **empty HTTP 200 body**. Missing header → 401, wrong header → 403. `process_webhook_update` is **not implemented** — updates are acknowledged, not dispatched. |
| GET | `/telegram/users` | List linked users (filterable) | API key | `{status, data[]}` |
| POST | `/telegram/broadcast` | Broadcast to linked users | API key | **Currently reports zero deliveries** (validates only). Own limit: 5/minute. |
| POST | `/telegram/notify` | Send to one linked user | API key | Body: `apikey`, `username` (must already be linked), `message`, optional `wait_for_delivery` (false). HTTP 200 = **queued**, not delivered. |
| GET | `/telegram/stats` | Command statistics (1–365 days) | API key | `{status, data}` |
| GET | `/telegram/preferences` | Read prefs for a `telegram_id` | API key | `{status, data}` |
| POST | `/telegram/preferences` | Update prefs for a `telegram_id` | API key | `{status, ...}` |
| POST | `/whatsapp/notify` | Send text/image/document | API key | See below |

`/whatsapp/notify` body: `apikey` (mandatory) + **exactly one** recipient form — `self` (bool) \| `username` \| `phone` (E.164 digits) \| `phones[]` (max 5, extras dropped). Content: `message` (≤4096 chars; optional if an attachment is given), `image_path`, `document_path`, `caption`, `filename`, `wait_for_delivery` (false).
- Async response (**NON-STANDARD**): `{status, message, queued}`.
- `wait_for_delivery:true`: `{status, message, data:{sent[], failed[], skipped}}`.
- Attachments are **server-local paths** restricted to `WHATSAPP_ATTACHMENT_ROOTS` (default `<openalgo>/db/attachments/`); outside → `400 image_path is not allowed`.
- Rate limits: `/notify` 30/min; inbound bot commands 10/sec/user.
- Everything else WhatsApp (pair, unpair, start/stop, config, users, broadcast, stats, preferences) is **admin session-cookie only** at `/whatsapp/...`, deliberately NOT exposed via API key.

Telegram rate limits: `TELEGRAM_RATE_LIMIT` default `30 per minute`; broadcast `5 per minute`.

### 2.11 WebSocket (not under /api/v1) — see §8

4 documented streams: LTP, Quote, Depth, Order Updates.

### 2.12 Registered-but-undocumented namespaces (found in `restx_api/__init__.py`, absent from `docs/api`)

Not part of the "57" and not documented — treat as out of scope unless the engineer opts in:
`GET /portfolio/benchmarks`, `POST /portfolio/backtest`, `POST /portfolio/tearsheet`, `POST /portfolio/holdings`, `GET /sip/frequencies`, `POST /sip/backtest`.

---

## 3. ORDER-MUTATING ENDPOINTS — exact params, validation, destructiveness

**Destructiveness legend**
- 🔴 **DESTRUCTIVE / IRREVERSIBLE** — sends real money orders or unwinds state that cannot be restored by an inverse call. Require explicit human confirmation.
- 🟠 **MUTATING, RECOVERABLE** — changes broker state but has an inverse (cancel a placed order, re-place a cancelled GTT). Still confirm.
- 🟢 **READ-ONLY**.

| Endpoint | Risk | Why |
|---|---|---|
| `/placeorder` | 🔴 | Real order at the exchange; a filled MARKET order cannot be undone |
| `/placesmartorder` | 🔴 | Computes and fires a delta order; can DOUBLE a position (see truth table) |
| `/optionsorder` | 🔴 | Real F&O order; strike auto-resolved from live LTP so the exact instrument is not known until execution |
| `/optionsmultiorder` | 🔴 | Up to 20 real legs; **partial execution possible** — a failed leg does not roll back earlier legs |
| `/basketorder` | 🔴 | N real orders, partial success possible |
| `/splitorder` | 🔴 | Up to 100 real child orders |
| `/modifyorder` | 🔴 | Full replacement; a modified LIMIT can fill instantly |
| `/cancelorder` | 🟠 | Removes a resting order; irreversible for that orderid but re-placeable |
| `/cancelallorder` | 🔴 | **Bulk** — kills every open + trigger-pending order (incl. stop-losses) at once |
| `/closeposition` | 🔴 | **Most destructive** — squares off ALL positions across ALL exchanges with MARKET orders; docs explicitly say "no confirmation prompt" |
| `/placegttorder` | 🟠 | Creates a broker-side trigger that will later fire a real order |
| `/modifygttorder` | 🟠 | Full replacement of an active trigger (omitted fields are NOT preserved) |
| `/cancelgttorder` | 🟠 | Removes protection (an OCO cancel drops the stoploss too) |
| `/gttorderbook` | 🟢 | Read-only |

### 3.1 `/placeorder`
```
apikey*, strategy*, symbol*, action*, exchange*, quantity*
pricetype=MARKET, product=MIS, price=0, trigger_price=0, disclosed_quantity=0
```
Rules:
- `action` ∈ {BUY, SELL} (lowercase normalized by order schemas).
- `exchange` ∈ `VALID_EXCHANGES`; broker capability metadata narrows the usable subset.
- MARKET → price/trigger_price not needed. LIMIT → `price` required. SL → `price` **and** `trigger_price` required. SL-M → `trigger_price` required.
- `quantity` positive numeric. **Fractional only when `exchange == "CRYPTO"`**; every other exchange rejects fractions at schema validation.
- Rate limit: `ORDER_RATE_LIMIT`.

### 3.2 `/placesmartorder`
```
apikey*, strategy*, exchange*, symbol*, action*, quantity*, position_size*
product=MIS, pricetype=MARKET, price=0, trigger_price=0, disclosed_quantity=0
```
Rules:
- `position_size` = **absolute target** position: positive = long, negative = short, 0 = flat.
- If current position already equals the target, no order is placed (`message: "No action needed"`).
- Reconciliation truth table (from docs):

| action | quantity | position_size | current open pos | OpenAlgo does |
|---|---|---|---|---|
| BUY | 100 | 0 | 0 | no open pos → BUY +100 |
| BUY | 100 | 100 | -100 | **BUY 200** (crosses through flat) |
| BUY | 100 | 100 | 100 | no action |
| BUY | 100 | 200 | 100 | BUY 100 |
| SELL | 100 | 0 | 0 | no open pos → SELL 100 |
| SELL | 100 | -100 | +100 | **SELL 200** |
| SELL | 100 | -100 | -100 | no action |
| SELL | 100 | -200 | -100 | SELL 100 |

- The "×2" rows are the confirmation trap — the agent must surface the computed delta, not the `quantity` field.
- Rate limit: `SMART_ORDER_RATE_LIMIT` (independent of `ORDER_RATE_LIMIT`).

### 3.3 `/optionsorder`
```
apikey*, strategy*, underlying*, exchange*, offset*, option_type*, action*, quantity*
expiry_date (DDMMMYY, derived if underlying embeds expiry), pricetype=MARKET,
product=MIS, splitsize=0, price=0, trigger_price=0
```
Rules:
- `exchange` ∈ {NSE_INDEX, BSE_INDEX, NFO, BFO}. NSE_INDEX → order routed to **NFO**; BSE_INDEX → **BFO**.
- `offset` validated against `ATM` \| `ITM1`..`ITM50` \| `OTM1`..`OTM50` (exact spelling: no space, no zero padding).
- `option_type` ∈ {CE, PE}.
- `product` here documented as MIS or NRML.
- `quantity` positive **integer** (options quantities are never fractional).
- `splitsize` 0 = no split; splitting caps at 100 child orders.
- `expiry_date` format `DDMMMYY` uppercase, e.g. `25AUG26`, `29SEP26`.
- ATM is resolved from the synthetic-futures price or spot at request time — **the resolved strike is non-deterministic ahead of the call**. Show `underlying_ltp` + resolved `symbol` in any confirmation.

### 3.4 `/optionsmultiorder`
```
apikey*, strategy*, underlying*, exchange*(NSE_INDEX|BSE_INDEX), legs*[]
expiry_date (common default for all legs)
leg: offset*, option_type*, action*, quantity*
     expiry_date (falls back to common), pricetype=MARKET, product=MIS, splitsize=0
```
Rules: 1–20 legs (broker may be stricter). **BUY legs always execute before SELL legs** for margin efficiency. A failed leg does not stop subsequent legs. One `underlying_ltp` is used for every leg's ATM calc.

### 3.5 `/basketorder`
```
apikey*, strategy*, orders*[]
order: symbol*, exchange*, action*, quantity*, pricetype=MARKET, product=MIS, price=0, trigger_price=0
```
Rules: BUY orders processed before SELL. Live path = concurrent batches of 10 with a 1-second delay between batches. Partial success possible (`status:"success"` if at least one order succeeded — **the envelope status alone is not proof all legs worked**). Fractional qty only for CRYPTO.

### 3.6 `/splitorder`
```
apikey*, strategy*, symbol*, exchange*, action*, quantity*, splitsize*
pricetype=MARKET, product=MIS, price=0, trigger_price=0
```
Rules: max 100 child orders; last order carries the remainder (`quantity % splitsize`); if `splitsize > quantity` a single order is placed. `splitsize` must be a **positive integer**. Fractional total qty only for CRYPTO. Live children are sequential with a delay derived from `ORDER_RATE_LIMIT`. Freeze quantities come from `data/qtyfreeze.csv` — never hard-code.

### 3.7 `/modifyorder`
All 12 fields are **schema-required**, even ones the broker ignores:
```
apikey*, orderid*, strategy*, symbol*, action*, exchange*, pricetype*, product*,
quantity*, price*, trigger_price*, disclosed_quantity*
```
Rules: only open/pending orders are modifiable. Cannot change `product`, `symbol`, or `action` (broker rejects). `pricetype` change is broker-dependent. Send `0` for unused `trigger_price` / `disclosed_quantity`. F&O quantity must be a valid lot multiple. Errors: order not found / not modifiable / invalid price (circuit limits) / invalid quantity.

### 3.8 `/cancelorder`
```
apikey*, orderid*    strategy (optional)
```
Only open/pending orders. Already-cancelled → returns success. In-transit orders may not be cancellable.

### 3.9 `/cancelallorder`
```
apikey*    strategy (optional)
```
Cancels all open limit orders, pending SL/SL-M triggers, and AMO orders where supported. Returns `status:"success"` even when some cancels failed — **always read `failed_cancellations[]`**. Docs flag this as a bulk operation to use with caution.

### 3.10 `/closeposition`
```
apikey*    strategy (optional)
```
Mechanics: fetch position book → for each non-zero position, long → SELL, short → BUY → **all closing orders are MARKET** → same product type as the original position. Covers NSE/BSE (MIS, CNC) and NFO/BFO/CDS/BCD/MCX (MIS, NRML). CNC positions with intraday qty are also closed. Explicitly documented as "a destructive operation … no confirmation prompt … affects all positions across all exchanges." **Highest-risk tool in the surface.**

### 3.11 `/placegttorder` and `/modifygttorder`
```
apikey*, strategy*, [trigger_id* for modify], trigger_type*, exchange*, symbol*,
action*, product*, quantity*, price*
pricetype=LIMIT, triggerprice_sl=0, triggerprice_tg=0, stoploss=null, target=null
```
Validation rules (identical for place & modify):
- `trigger_type` ∈ {`SINGLE`, `OCO`} — **uppercase in requests**; the GTT orderbook reports them lowercase as `"single"` / `"two-leg"`.
- `exchange` restricted to NSE, BSE, NFO, BFO, CDS, BCD, MCX.
- `product` ∈ {`CNC`, `NRML`} only. **`MIS` is rejected**: "GTT supports only CNC (delivery) or NRML (overnight F&O); MIS is intraday-only."
- `pricetype` ∈ {`LIMIT`, `MARKET`} only (no SL / SL-M).
- **SINGLE**: exactly one of `triggerprice_sl` / `triggerprice_tg` > 0, the other `0`. `triggerprice_sl` = trigger **below** LTP; `triggerprice_tg` = trigger **above** LTP. `price` is the child limit; send `0` for MARKET. `stoploss` / `target` are ignored.
- **OCO**: all four of `triggerprice_sl`, `stoploss`, `triggerprice_tg`, `target` required and > 0, with **`triggerprice_sl < triggerprice_tg`**. `price` is ignored (each leg uses its own limit). Both legs share `action`, `quantity`, `product`.
- `quantity` > 0; integer for equity/F&O, fractional only for crypto.
- Empty strings `""` for `stoploss`/`target`/`triggerprice_sl`/`triggerprice_tg` are coerced to `null`/`0`.
- `last_price` is fetched **server-side** — never send it.
- MARKET child orders are auto-converted to Market-Price-Protected LIMIT for brokers whose GTT API only accepts LIMIT.
- **Modify is a full replacement, not a patch** — omitted fields are lost. Cannot switch SINGLE ↔ OCO, cannot change `symbol`/`exchange`/`action`. Only active GTTs are modifiable.
- Modify is blocked in **Semi-Auto mode** → 403 "Modify GTT order is not allowed in Semi-Auto mode".
- **All GTT endpoints return 501 when analyzer mode is ON**: "Sandbox GTT support not yet implemented". Also 501 "GTT orders are not supported for broker 'X' yet" when the broker ships no `gtt_api` module. 502 if broker quotes fail (`Failed to fetch last_price from broker quotes`).

### 3.12 `/cancelgttorder`
```
apikey*, strategy*, trigger_id*
```
Only active GTTs. Cancelling an OCO removes **both legs atomically** — no per-leg cancel. Idempotency is broker-defined: cancelling an already-cancelled trigger may return success OR "Trigger not found". Missing `trigger_id` → 400.

---

## 4. VALID CONSTANTS (exact spelling)

### 4.1 Exchanges (`VALID_EXCHANGES`, 14 values)
`NSE`, `NFO`, `CDS`, `BSE`, `BFO`, `BCD`, `MCX`, `NCDEX`, `NCO`, `NSE_INDEX`, `BSE_INDEX`, `MCX_INDEX`, `GLOBAL_INDEX`, `CRYPTO`

| Code | Meaning | Tradable? | Broker limitation |
|---|---|---|---|
| `NSE` | NSE equity | Yes | — |
| `BSE` | BSE equity | Yes | — |
| `NFO` | NSE futures & options | Yes | — |
| `BFO` | BSE futures & options | Yes | — |
| `CDS` | NSE currency | Yes | — |
| `BCD` | BSE currency | Yes | — |
| `MCX` | MCX commodity | Yes | — |
| `NCDEX` | NCDEX commodity | Yes | — |
| `NCO` | NSE Commodities (futures + options) | Yes | **Zerodha only** |
| `NSE_INDEX` | NSE indices | **QUOTE-ONLY** | — |
| `BSE_INDEX` | BSE indices | **QUOTE-ONLY** | — |
| `MCX_INDEX` | MCX commodity sectoral indices | **QUOTE-ONLY** | Sourced from Zerodha |
| `GLOBAL_INDEX` | Global indices | **QUOTE-ONLY** | **Zerodha only** |
| `CRYPTO` | Crypto derivatives | Yes | **Delta Exchange only** |

Derived sets in `utils/constants.py`: `FNO_EXCHANGES = {NFO, BFO, MCX, CDS, BCD, NCDEX, NCO} ∪ CRYPTO_EXCHANGES`; `CRYPTO_EXCHANGES = {CRYPTO}`; `CRYPTO_BROKERS = {"deltaexchange"}`.

### 4.2 Product types (`VALID_PRODUCT_TYPES`)
`CNC` (equity delivery), `NRML` (F&O overnight), `MIS` (intraday square-off).
GTT endpoints accept only `CNC` and `NRML`.

### 4.3 Price types (`VALID_PRICE_TYPES`)
`MARKET`, `LIMIT`, `SL` (stop-loss limit), `SL-M` (stop-loss market). Note the hyphen in `SL-M`.
GTT accepts only `LIMIT` and `MARKET`.

### 4.4 Actions (`VALID_ACTIONS`)
`BUY`, `SELL`. Lowercase input is normalized by the order schemas.

### 4.5 Intervals (history / ticker)
Common set: `1m`, `3m`, `5m`, `10m`, `15m`, `30m`, `1h`, `D`, `W`, `M` (also `1s`-style second intervals where a broker supports them).
`/history` doc lists `1m, 3m, 5m, 10m, 15m, 30m, 1h, D`. Typical broker response from `/intervals`:
`{months:[], weeks:[], days:["D"], hours:["1h"], minutes:["1m","3m","5m","10m","15m","30m"], seconds:[]}`.
**Always call `/intervals` first — availability is broker-specific.**

### 4.6 GTT trigger types
Request: `SINGLE`, `OCO`. GTT orderbook reports: `"single"`, `"two-leg"`. GTT orderbook `status` is always `"active"` (non-active triggers are filtered at the broker mapper).

### 4.7 Option offsets
`ATM`, `ITM1` … `ITM50`, `OTM1` … `OTM50` (51 + 50 + 50 = 101 valid strings; no space, no zero-padding).

| Option type | ITM direction | OTM direction |
|---|---|---|
| `CE` (call) | Lower strikes | Higher strikes |
| `PE` (put) | Higher strikes | Lower strikes |

Option chain `label` uses the same vocabulary: `ATM`, `ITM1`, `ITM2`…, `OTM1`, `OTM2`…

### 4.8 `option_type`
`CE`, `PE`.

### 4.9 `instrumenttype` values
- `/expiry` request parameter: lowercase words **`"options"`** or **`"futures"`**.
- Symbol master / `/symbol` / `/search` / `/instruments` response field: `EQ`, `FUT`, `CE`, `PE` (empty string `""` for plain NSE equity in some rows). Crypto perpetuals use `PERPFUT`.
- Do not mix the two vocabularies — the request enum and the response enum are different.

### 4.10 Order status values
`complete`, `open`, `pending` (trigger pending), `rejected`, `cancelled`. WebSocket order updates use lowercase and add `trigger pending` and broker extras such as `expired`.

### 4.11 Holiday types
`TRADING_HOLIDAY`, `SETTLEMENT_HOLIDAY`, `SPECIAL_SESSION`.

### 4.12 Mode values
`live`, `analyze` (appears as `mode` in order responses and WS order updates).

---

## 5. SYMBOL FORMAT RULES

OpenAlgo normalizes every broker's symbology to one format. Always uppercase.

| Instrument | Format | Examples |
|---|---|---|
| **Equity** | `[BaseSymbol]` | `INFY`, `TATAMOTORS`, `SBIN`, `RELIANCE` |
| **Futures** | `[Base][DDMMMYY]FUT` | `BANKNIFTY24APR24FUT`, `SENSEX24APR24FUT`, `USDINR10MAY24FUT`, `CRUDEOILM20MAY24FUT`, `726GS203325APR24FUT`, `NIFTY25AUG26FUT` |
| **Options** | `[Base][DDMMMYY][Strike][CE\|PE]` | `NIFTY28MAR2420800CE`, `VEDL25APR24292.5CE` (decimal strikes allowed), `USDINR19APR2482CE`, `CRUDEOIL17APR246750CE`, `726GS203225APR2497PE` |
| **Indices** | bare index name on `*_INDEX` exchange | `NIFTY`, `BANKNIFTY`, `SENSEX`, `MCXBULLDEX`, `US30` |
| **NCO futures** | `[Underlying][Expiry]FUT` | `ALUMINI26MAYFUT` |
| **NCO options** | `[Underlying][Expiry][Strike][CE\|PE]` | `COPPER26MAY1195CE` |
| **Crypto perps** | base + `USDT` quote suffix | e.g. `BTCUSDT` (instrumenttype `PERPFUT`) |

Expiry token inside a symbol is `DDMMMYY` (`25AUG26`); the `/expiry` endpoint and `/symbol` **response** use `DD-MMM-YY` (`25-AUG-26`); the `expiry_date` **request** parameter for options endpoints uses `DDMMMYY` (`25AUG26`). Three different renderings — do not interchange.

### 5.1 Quote-only (non-tradable) exchanges
`NSE_INDEX`, `BSE_INDEX`, `MCX_INDEX`, `GLOBAL_INDEX`.
Valid for `quotes`, `ltp`, `history`, `depth`, and WebSocket subscriptions only — **never for order placement**. Tradable index derivatives live on the regular derivative exchange (e.g. `MCXBULLDEX27MAY26FUT` on `MCX`, `NIFTY25AUG26FUT` on `NFO`).
Note: `NSE_INDEX` / `BSE_INDEX` ARE valid as the `exchange` parameter of `/optionsorder`, `/optionsmultiorder`, `/optionsymbol`, `/optionchain`, `/syntheticfuture` — there they identify the **underlying**, and the actual order is routed to NFO/BFO.

### 5.2 Representative index symbols
- **NSE_INDEX**: `NIFTY`, `NIFTYNXT50`, `FINNIFTY`, `BANKNIFTY`, `MIDCPNIFTY`, `INDIAVIX`, `NIFTY100/200/500`, `NIFTYIT`, `NIFTYBANK`-family sectorals, `NIFTYAUTO`, `NIFTYPHARMA`, `NIFTYMETAL`, `NIFTYFMCG`, … (~57 listed)
- **BSE_INDEX**: `SENSEX`, `BANKEX`, `SENSEX50`, `BSE100`, `BSE200`, `BSE500`, `BSEAUTO`, `BSEMETAL`, `BSEIPO`, `BSEOIL&GAS`, … (~39 listed)
- **MCX_INDEX**: `MCXAGRI`, `MCXBULLDEX`, `MCXCOMDEX`, `MCXCOMPDEX`, `MCXCOPRDEX`, `MCXCRUDEX`, `MCXENERGY`, `MCXGOLDEX`, `MCXMETAL`, `MCXMETLDEX`, `MCXSILVDEX` (11)
- **GLOBAL_INDEX**: `AUS200`, `FRANCE40`, `GERMANY40`, `GIFTNIFTY`, `HANGSENG`, `JAPAN225`, `SHANGHAICHINA`, `UK100`, `US100`, `US10YRYIELD`, `US30`, `US500`, `USCOMPOSITE` (13). `GIFTNIFTY` really comes from NSE IFSC (`NSEIX`) but is bucketed here.
- **NCO underlyings** (26): `ALUMINIUMFUTURES`, `ALUMINIUMMINIFUTURES`, `BRENTCRUDEOIL`, `BRENTCRUDEOILMINI`, `COPPER`, `CRUDEDEGUMSOYBEANOIL`, `ELECTRICITYFUTURES`, `GOLD`, `GOLD10GM`, `GOLD1GM`, `GOLDGUINEA8GM`, `GOLDMINI`, `LEADFUTURES`, `LEADMINIFUTURES`, `NATURALGASHENRYHUB`, `NATURALGASMINI`, `NICKELFUTURES`, `PLATTSDATEDBRENTASSESS`, `SILVER`, `SILVERMICRO`, `SILVERMINI`, `WTICRUDEOIL`, `WTICRUDEOILMINI`, `XAUGOLD`, `ZINCFUTURES`, `ZINCMINIFUTURES`

### 5.3 Symbol master schema (per-instrument metadata)
`id`, `symbol` (OpenAlgo), `brsymbol` (broker), `name`, `exchange` (OpenAlgo), `brexchange` (broker), `token`, `expiry`, `strike`, `lotsize`, `instrumenttype`, `tick_size`; `/symbol` and `/search` also return `freeze_qty`.
Representative lot sizes at time of writing: NIFTY 65, BANKNIFTY 30, SENSEX 20, equity 1. **Read them from `/symbol` — never hard-code.**

---

## 6. RATE LIMITS AND STATUS CODES

### 6.1 Rate limits (Flask-Limiter, moving window, keyed by remote IP, in-memory store)

| Env var | Default | Applies to |
|---|---|---|
| `ORDER_RATE_LIMIT` | `10 per second` | `/placeorder`, `/modifyorder`, `/cancelorder`, `/optionsorder`, `/optionsmultiorder`, `/placegttorder`, `/modifygttorder`, `/cancelgttorder` |
| `SMART_ORDER_RATE_LIMIT` | `10 per second` | `/placesmartorder` only (independent counter from ORDER_RATE_LIMIT) |
| `API_RATE_LIMIT` | `50 per second` | Everything else — market data, account, information, search/symbol |
| `WEBHOOK_RATE_LIMIT` | `100 per minute` | `/strategy/webhook/<id>`, `/chartink/webhook/<id>` (not v1 API) |
| `STRATEGY_RATE_LIMIT` | `200 per minute` | Strategy/ChartInk management routes (not v1 API) |
| `LOGIN_RATE_LIMIT_MIN` / `_HOUR` | `5 per minute` / `25 per hour` | UI login |
| `TELEGRAM_RATE_LIMIT` | `30 per minute` | Most Telegram resources; `/telegram/broadcast` is separately `5 per minute` |
| WhatsApp | `30 / minute` (`/notify`), `10 / second` per linked user (inbound bot commands) | — |

Values support compound syntax (`10 per second;40 per minute`) — parse them as strings, don't assume a single limit. `/splitorder` derives its inter-child delay from `ORDER_RATE_LIMIT`; `/basketorder` live path uses concurrent batches of 10 with a 1s gap.

### 6.2 Status-code semantics

| Code | Meaning |
|---|---|
| 200 | Handled successfully. **Not proof of full success** for batch endpoints — check per-item `status`. |
| 400 | Invalid JSON, schema validation failure, unsupported mode, invalid request state. Also: `/pnl/symbols` called in live mode; `trigger_id is required`; `image_path is not allowed`. |
| 401 | Missing authentication on endpoints that use 401 — `/instruments` with no `apikey`; Telegram webhook with missing secret header. |
| 403 | **Invalid API key**, or operation blocked by mode/policy. Includes `/ping` with a revoked/missing broker session, `/chart` bad key, GTT modify in Semi-Auto mode, Telegram webhook with wrong secret. |
| 404 | Broker module, symbol, order, or linked messaging user not found. |
| 429 | Rate limit exceeded → body `{"status":"error","message":"Rate limit exceeded. Please try again later."}`. **No `Retry-After` header** — implement exponential backoff client-side. |
| 500 | Unhandled internal or broker error; also `/instruments` DB query failure. |
| 501 | (GTT-specific) broker has no `gtt_api` module, or GTT called while analyzer mode is ON. |
| 502 | (GTT-specific) `Failed to fetch last_price from broker quotes`. |

---

## 7. ANALYZER (SANDBOX) MODE — why it matters for a trading agent

`POST /analyzer` reads the mode; `POST /analyzer/toggle` with `{"mode": true|false}` switches it.

| Behaviour | Live mode | Analyzer mode |
|---|---|---|
| Orders sent to broker | Yes | **No** |
| Real money at risk | Yes | **No** |
| Order IDs | Real broker IDs | Simulated IDs |
| Response shape | Same | Same, plus `"mode": "analyze"` |
| Capital | Broker funds | Sandbox capital, ₹1 crore default |
| Positions | Broker position book | Separate sandbox DB (`/pnl/symbols` readable) |
| Auto square-off | Broker | Follows exchange timings in the sandbox engine |
| GTT endpoints | Work | **501 "Sandbox GTT support not yet implemented"** |
| Order-update WebSocket | Broker feed | Sandbox engine emits identical messages |

Agent-design implications:
1. **Call `/analyzer` before every order-mutating tool call** and include the returned `mode` in the confirmation prompt. "You are in LIVE mode" vs "You are in ANALYZE mode" completely changes the risk of a confirmation.
2. Every order response carries `mode` — assert it matches what was expected; a silent flip to `live` is the worst failure mode.
3. Analyzer mode is an **application-wide** setting for this single-user deployment, not per-API-key. Another process/tab toggling it affects the agent. Re-read it, do not cache.
4. `/analyzer/toggle` with `mode:false` is itself a **safety-critical mutation** — gate it behind confirmation exactly like an order.
5. `/pnl/symbols` is analyzer-only (HTTP 400 in live mode) and is the natural "did my simulated strategy work" read.
6. Because GTT is not sandboxed, GTT tools **cannot be dry-run** — they are always live or always 501. No safe rehearsal path exists.

---

## 8. WEBSOCKET PROTOCOL

```text
ws://127.0.0.1:8765     (or ws://<host>:8765 / wss://<domain> in production)
```
Not mounted under `/api/v1`. Separate process/port.

### 8.1 Handshake
Every session must begin with:
```json
{"action": "authenticate", "api_key": "YOUR_OPENALGO_API_KEY"}
```
On failure the connection is closed or an error is returned. On reconnect the client must re-authenticate and re-subscribe.

### 8.2 Actions (9)
`authenticate`, `subscribe`, `unsubscribe`, `unsubscribe_all`, `subscribe_orders`, `unsubscribe_orders`, `get_broker_info`, `get_supported_brokers`, `ping`

### 8.3 Market-data modes (3)
`subscribe` / `unsubscribe` take `mode` ∈ `ltp` \| `quote` \| `depth` and an `instruments` array of `{exchange, symbol}`:
```json
{"action":"subscribe","mode":"ltp","instruments":[{"exchange":"NSE","symbol":"RELIANCE"}]}
```

| Mode | Push message `type` | Data payload |
|---|---|---|
| `ltp` | `"ltp"` | `exchange, symbol, ltp, timestamp` (epoch ms). Lowest latency; every tick. |
| `quote` | `"quote"` | `exchange, symbol, ltp, open, high, low, close (prev close), volume, timestamp` |
| `depth` | `"depth"` | `exchange, symbol, ltp, ltq, open, high, low, close, volume, totalbuyqty, totalsellqty, bids[5]{price,quantity}, asks[5]{price,quantity}, timestamp`. Highest bandwidth. |

### 8.4 Order-update stream (account-level, no symbols/modes)
```json
{"action":"subscribe_orders","request_id":"orders-1"}
{"action":"unsubscribe_orders"}
```
Ack: `{"type":"subscribe_orders","status":"success","message":"...","request_id":"..."}`.
Push message `type: "order_update"` with fields: `user_id, mode(live|analyze), broker, orderid, symbol (OpenAlgo format), exchange, action(BUY|SELL), quantity, price, trigger_price, pricetype(MARKET|LIMIT|SL|SL-M), product(CNC|NRML|MIS), order_status (lowercase: open | trigger pending | complete | rejected | cancelled | expired), filled_quantity, pending_quantity, average_price, rejection_reason`.
Covers **every order origin** — OpenAlgo API, the broker's own app/website, engine square-offs.
Sources: native broker push (Zerodha, Dhan, Fyers, Upstox, AliceBlue, Definedge, IndMoney, Angel One, Nubra, Arrow, IIFL Capital, Kotak); REST polling fallback for brokers without push (e.g. Groww, `ORDER_POLL_INTERVAL` default 5s); HTTPS postbacks at `/postback/<broker>`.
**Deduplicate on `orderid` + `order_status` + `filled_quantity`** — a broker WS plus a postback can deliver the same transition twice. Global kill switch: `ORDER_UPDATES_ENABLED=FALSE`.

### 8.5 Keepalive and connection limits
- Server sends `ping` every 30s; client must reply `pong` or be disconnected (also `WS_PING_INTERVAL`=20, `WS_PING_TIMEOUT`=20 in env).
- `WS_MAX_QUEUE` = 1024 per-client send queue.
- Upstream: `MAX_SYMBOLS_PER_WEBSOCKET` = 1000, `MAX_WEBSOCKET_CONNECTIONS` = 3 → ~3000 unique symbols.
- **No application-level cap on downstream client connections** (bounded only by file descriptors).
- The broker feed is pooled per `{broker}_{user_id}` and subscriptions are refcounted — a second client subscribing to the same symbol consumes no extra upstream slot.

---

## 9. RESPONSES THAT ARE **NOT** THE STANDARD `{status, data}` ENVELOPE

Critical for writing tool output parsers — do not assume `response["data"]`.

**A. Flat top-level fields instead of `data` (order writes):**
- `/placeorder`, `/placesmartorder`, `/modifyorder`, `/cancelorder` → `{status, orderid, message?, mode?}`
- `/optionsorder` → `{status, orderid, symbol, exchange, offset, option_type, underlying, underlying_ltp, mode?}`
- `/placegttorder`, `/modifygttorder`, `/cancelgttorder` → `{status, trigger_id, message?}`
- `/openposition` → `{status, quantity}` (string!)
- `/closeposition`, `/cancelallorder` → `{status, message, ...}`

**B. `results` array instead of `data`:**
- `/multiquotes` → `{status, results[]}`
- `/basketorder` → `{status, results[]}`
- `/splitorder` → `{status, split_size, total_quantity, results[]}`
- `/optionsmultiorder` → `{status, underlying, underlying_ltp, results[]}`

**C. Flat analytics payloads:**
- `/optionsymbol` → `{status, symbol, exchange, lotsize, tick_size, freeze_qty, underlying_ltp}`
- `/optionchain` → `{status, underlying, underlying_ltp, underlying_prev_close, expiry_date, expiry_ts, server_ts, atm_strike, quotes_included, greeks_included, forward_price, chain[]}`
- `/syntheticfuture` → `{status, underlying, underlying_ltp, expiry, atm_strike, synthetic_future_price}`
- `/optiongreeks` → flat + nested `greeks{}`
- `/multioptiongreeks` → `{status, data[], summary{}}` (has `data`, plus an extra `summary`)

**D. Extra sibling keys alongside `data`:**
- `/search`, `/expiry`, `/instruments` → `{status, message, data}`
- `/market/holidays` → `{status, year, timezone, data[]}`
- `/pnl/symbols` → `{status, data[], total_pnl, total_unrealized_pnl, total_today_realized_pnl, total_pnl_today, mode}`

**E. Non-JSON / empty bodies:**
- `GET /instruments?format=csv` → `text/csv` file download (`instruments_<exchange>.csv`)
- `GET /ticker/<symbol>?format=txt` → plain-text comma-separated rows
- `POST /telegram/webhook` → **empty HTTP 200 body** on a validated update

**F. Async/queued semantics (200 ≠ delivered):**
- `/telegram/notify`, `/telegram/broadcast` (currently reports 0 deliveries), `/whatsapp/notify` → `{status, message, queued}`; only `wait_for_delivery:true` returns a per-recipient `data{sent,failed,skipped}` report.

---

## 10. AGENT-DESIGN CHECKLIST (derived)

1. **Confirmation gate** on the 13 order-mutating endpoints listed in §3, with `/closeposition`, `/cancelallorder`, `/placesmartorder`, `/optionsmultiorder`, `/basketorder`, `/splitorder` treated as the highest tier (bulk / multi-order / position-crossing).
2. **Pre-flight `/analyzer`** on every mutating call; echo `mode` back into the confirmation text; assert the returned `mode` on the response.
3. **Never trust the envelope `status` alone** for `/basketorder`, `/splitorder`, `/optionsmultiorder`, `/cancelallorder`, `/multioptiongreeks`, `/multiquotes` — walk the per-item results.
4. **Resolve before ordering**: `/symbol` (lotsize, freeze_qty, tick_size), `/expiry`, `/optionsymbol` — so the confirmation shows the exact instrument and a lot-aligned quantity.
5. **Block quote-only exchanges** (`NSE_INDEX`, `BSE_INDEX`, `MCX_INDEX`, `GLOBAL_INDEX`) as an order `exchange`, while still allowing them as the options-underlying `exchange`.
6. **Enforce fraction rule** client-side: fractional quantity only when `exchange == "CRYPTO"`.
7. **Enforce GTT rules** client-side: no `MIS`, no `SL`/`SL-M`, SINGLE = exactly one trigger, OCO = all four with `triggerprice_sl < triggerprice_tg`; and refuse GTT calls entirely while analyzer mode is ON (they 501).
8. **Rate-limit budgets**: 10/s for order writes, 10/s separately for smart orders, 50/s general; exponential backoff on 429 (no `Retry-After` header is sent).
9. **Modify semantics**: both `/modifyorder` and `/modifygttorder` are full replacements — the agent must reconstruct the complete order spec, never send a diff.
