# OpenAlgo Python SDK — Exact Surface Reference

Ground truth: `d:\AI Bootcamp 2026\Day08\openalgo\.venv\Lib\site-packages\openalgo\`
Installed version: **2.0.3** (`openalgo-2.0.3.dist-info`, `openalgo.__version__ == "2.0.3"`)
HTTP client: **httpx 0.28.1** (sync `httpx.Client`). WebSocket: `websocket-client` (`websocket.WebSocketApp`).
Also installed in the same venv: `openalgoui 2.0.1.9` (editable, the server app — unrelated to the SDK).

---

## 1. How the unified `api` class is composed

`openalgo/__init__.py:28`

```python
class api(OrderAPI, DataAPI, AccountAPI, FeedAPI, OptionsAPI,
          TelegramAPI, WhatsAppAPI, UtilitiesAPI):
```

Runtime MRO (verified):
`api -> OrderAPI -> DataAPI -> AccountAPI -> FeedAPI -> OptionsAPI -> TelegramAPI -> WhatsAppAPI -> UtilitiesAPI -> BaseAPI -> object`

Every mixin subclasses `BaseAPI` and each one **redefines identical private helpers** `_make_request` / `_handle_response`. Because of the MRO, `api._make_request` and `api._handle_response` always resolve to **`OrderAPI`'s copies** (verified: `api._handle_response.__qualname__ == 'OrderAPI._handle_response'`).

`Strategy` (webhook client) is a **separate, standalone class**, NOT part of `api`.
`ta` is a separate indicator object (`openalgo.ta`, 127 public indicator functions, Rust-backed) — not on `api`.

### Constructor (exact)

```python
api(api_key,
    host="http://127.0.0.1:5000",
    version="v1",
    timeout=120.0,
    ws_port=8765,
    ws_url=None,
    verbose=False,
    auto_reconnect=True)
```

> **Positional order gotcha:** it is `(api_key, host, version, timeout, ws_port, ws_url, ...)` — `timeout` comes **before** `ws_port`/`ws_url`. Always pass these by keyword.

What it does:
- `BaseAPI.__init__(api_key, host, version, timeout)` sets:
  - `self.base_url = f"{host}/api/{version}/"`
  - `self.headers = {'Content-Type': 'application/json'}`
  - `self.timeout = timeout` (default **120.0 seconds**)
  - `self.client = httpx.Client(timeout=timeout, limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=120.0))` — one pooled client reused for every REST call.
- `verbose`: `int(verbose) if verbose is not False else 0` (0 = silent/errors, 1 = basic, 2 = full debug — **prints to stdout via `print()`**, no logging module).
- `ws_url`: if not given, derived as `ws://{host-without-scheme-without-port-without-path}:{ws_port}` (default `ws://127.0.0.1:8765`). Note: it **always builds `ws://`, never `wss://`**, even when `host` is `https://`.
- Initializes all FeedAPI state inline (mirrors `FeedAPI.__init__`): `ws`, `connected=False`, `authenticated=False`, `ws_thread`, `message_queue`, `lock` (`threading.Lock`), `ltp_data/quotes_data/depth_data = {}`, all 5 callbacks = `None`, `order_data = []`, `_orders_subscribed = False`, `auto_reconnect`, `_shutting_down`, `_reconnect_thread`, `_reconnect_lock`, `_active_subs = {1:{},2:{},3:{}}`.

Lifecycle: `close()` (closes the pooled httpx client), plus `__enter__`/`__exit__` from `BaseAPI` — **`__exit__` does NOT close the WebSocket**; call `disconnect()` yourself.

### Public surface: 53 members on one instance

| Domain | Methods |
|---|---|
| Orders (`orders.py`) | `placeorder`, `placesmartorder`, `basketorder`, `splitorder`, `modifyorder`, `cancelorder`, `cancelallorder`, `closeposition`, `orderstatus`, `openposition` |
| Account (`account.py`) | `funds`, `orderbook`, `tradebook`, `positionbook`, `holdings`, `analyzerstatus`, `analyzertoggle`, `margin` |
| Data (`data.py`) | `quotes`, `multiquotes`, `depth`, `symbol`, `search`, `history`, `intervals`, `interval`, `expiry`, `instruments`, `syntheticfuture` |
| Options (`options.py`) | `optiongreeks`, `optionsorder`, `optionsymbol`, `optionsmultiorder`, `optionchain` |
| Utilities (`utilities.py`) | `holidays`, `timings` |
| Notifications | `telegram`, `whatsapp` |
| WebSocket (`feed.py`) | `connect`, `disconnect`, `subscribe_ltp`, `unsubscribe_ltp`, `subscribe_quote`, `unsubscribe_quote`, `subscribe_depth`, `unsubscribe_depth`, `subscribe_orders`, `unsubscribe_orders`, `get_ltp`, `get_quotes`, `get_depth`, `get_orders` |
| Lifecycle | `close` |

---

## 2. Every public method — exact signature + payload mapping

Notation: everything after `*` is **keyword-only**. `-> endpoint` is the REST path appended to `base_url` (all POST unless noted).

### 2.1 Orders (`openalgo/orders.py`)

```python
placeorder(*, strategy="Python", symbol, action, exchange,
           price_type="MARKET", product="MIS", quantity=1, **kwargs)      -> "placeorder"
```
Payload: `apikey, strategy, symbol, action, exchange, pricetype(=price_type), product, quantity(str())`.
kwargs: each non-None value is `str()`-cast then added. Documented kwargs: `price`, `trigger_price`, `disclosed_quantity`, `target`, `stoploss`, `trailing_sl`.

```python
placesmartorder(*, strategy="Python", symbol, action, exchange,
                price_type="MARKET", product="MIS", quantity=1,
                position_size, **kwargs)                                   -> "placesmartorder"
```
`position_size` is keyword-only, **required**, and declared *after* defaulted params. Payload adds `position_size` (str()). kwargs `str()`-cast.

```python
basketorder(*, strategy="Python", orders, **kwargs)                        -> "basketorder"
```
`orders` = list of dicts. Each dict's int/float values are `str()`-cast (bools too, since `isinstance(True, int)`), strings untouched. Per-order keys: `symbol, exchange, action, quantity, pricetype, product` + optional `price, trigger_price, disclosed_quantity`.
Response shape: `{"results": [{"orderid", "status", "symbol"}, ...], "status": ...}`.
kwargs: `str()`-cast.

```python
splitorder(*, strategy="Python", symbol, action, exchange, quantity,
           splitsize, price_type="MARKET", product="MIS", **kwargs)        -> "splitorder"
```
Payload: `... quantity(str), splitsize(str), pricetype, product`. kwargs `str()`-cast.
Response: `{"results":[{"order_num","orderid","quantity","status"}...], "split_size", "status", "total_quantity"}`.

```python
orderstatus(*, order_id, strategy="Python", **kwargs)                      -> "orderstatus"
```
Payload key is **`orderid`**, not `order_id`. Response `data` contains `orderid`, `order_status`, `pricetype`, etc.

```python
openposition(*, strategy="Python", symbol, exchange, product, **kwargs)    -> "openposition"
```
Response: `{"quantity": <int>, "status": "success"}` (quantity at top level, no `data` wrapper).

```python
modifyorder(*, order_id, strategy="Python", symbol, action, exchange,
            price_type="LIMIT", product, quantity, price,
            disclosed_quantity="0", trigger_price="0", **kwargs)           -> "modifyorder"
```
Note `price_type` defaults to **"LIMIT"** here (vs "MARKET" in placeorder). `product`, `quantity`, `price` are required-keyword despite appearing after defaulted params. Payload key `orderid`.

```python
cancelorder(*, order_id, strategy="Python", **kwargs)                      -> "cancelorder"
closeposition(*, strategy="Python", **kwargs)                              -> "closeposition"
cancelallorder(*, strategy="Python", **kwargs)                             -> "cancelallorder"
```
`closeposition` / `cancelallorder` take **only** `strategy` — they are destructive account-wide/strategy-wide operations. Guard these when exposing as agent tools.

### 2.2 Account (`openalgo/account.py`)

```python
funds(**kwargs)          -> "funds"
orderbook(**kwargs)      -> "orderbook"
tradebook(**kwargs)      -> "tradebook"
positionbook(**kwargs)   -> "positionbook"
holdings(**kwargs)       -> "holdings"
analyzerstatus(**kwargs) -> "analyzer"            # NOTE: method name != endpoint
analyzertoggle(mode, **kwargs) -> "analyzer/toggle"
margin(*, positions: List[Dict[str, Union[str,int,float]]], **kwargs) -> Dict[str, Any]  -> "margin"
```
- These five books take **no positional/keyword args at all** except `**kwargs` (all values passed through raw, **not** `str()`-cast).
- `analyzertoggle(mode)` — `mode` is **positional-or-keyword** (the only order/account mutator that is not keyword-only). `mode` is a bool: `True` = analyze/simulated, `False` = live.
- `margin(positions=[...])`: client-side validation happens **before** any HTTP call and returns error dicts with `error_type: 'validation_error'` for: not a list, empty list, > 50 positions. Each position dict: `symbol, exchange, action, product, pricetype, quantity` required; `price`/`trigger_price` optional and defaulted to `"0"`. Only `quantity`, `price`, `trigger_price` are `str()`-cast.

Response shapes: `funds` → `data.{availablecash, collateral, m2mrealized, m2munrealized, utiliseddebits}`; `orderbook` → `data.{orders[], statistics{...}}`; `tradebook` → `data[]` (flat list); `positionbook` → `data[]`; `holdings` → `data.{holdings[], statistics{...}}`; `margin` → `data.{total_margin_required, span_margin, exposure_margin}`.

### 2.3 Data (`openalgo/data.py`)

```python
quotes(*, symbol, exchange, **kwargs)                                      -> "quotes"
multiquotes(*, symbols, **kwargs)                                          -> "multiquotes"
depth(*, symbol, exchange, **kwargs)                                       -> "depth"
symbol(*, symbol, exchange, **kwargs)                                      -> "symbol"
search(*, query, exchange=None, **kwargs)                                  -> "search"
history(*, symbol, exchange, interval, start_date, end_date,
        source="api", **kwargs)                                            -> "history"   # returns DataFrame
intervals(**kwargs)                                                        -> "intervals"
interval()                                                                 # legacy alias, calls intervals(); takes NO args
expiry(*, symbol, exchange, instrumenttype, **kwargs)                      -> "expiry"
instruments(*, exchange=None)                                              -> GET "instruments"  # returns DataFrame, NO **kwargs
syntheticfuture(*, underlying, exchange, expiry_date, **kwargs)            -> "syntheticfuture"
```
- All data-module kwargs are passed through **raw** (no `str()` cast).
- `multiquotes(symbols=[{"symbol":..,"exchange":..}, ...])`.
- `search`: `exchange` omitted from payload entirely if falsy.
- `instruments` is the **only GET** endpoint and the only method with no `**kwargs`.

### 2.4 Options (`openalgo/options.py`)

```python
optiongreeks(*, symbol, exchange, interest_rate=None, forward_price=None,
             underlying_symbol=None, underlying_exchange=None,
             expiry_time=None, **kwargs)                                   -> "optiongreeks"
```
Optional params only added to payload when not `None`. kwargs raw (no cast).

```python
optionsorder(*, strategy="Python", underlying, exchange, strike_int=None,
             offset, option_type, action, quantity, expiry_date=None,
             price_type="MARKET", product="MIS", **kwargs)                 -> "optionsorder"
```
`strike_int` emits `DeprecationWarning` when supplied. Payload uses `pricetype`, `quantity` is `str()`-cast, kwargs **`str()`-cast**.

```python
optionsymbol(*, strategy=None, underlying, exchange, strike_int=None,
             offset, option_type, expiry_date=None, **kwargs)              -> "optionsymbol"
```
`strategy` **defaults to `None` here and is DEPRECATED** (emits `DeprecationWarning` if passed) — opposite of every other method where `strategy="Python"`. kwargs `str()`-cast.

```python
optionsmultiorder(*, strategy, underlying, exchange, legs,
                  expiry_date=None, strike_int=None, **kwargs)             -> "optionsmultiorder"
```
`strategy` is **required with no default** here. `legs` (1–20): each leg must have `offset, option_type, action, quantity`; optional `expiry_date, pricetype, product, price, trigger_price, disclosed_quantity`.
**Leg processing raises raw Python exceptions**, it does not return error dicts: `leg["offset"]` → `KeyError`, `int(leg["quantity"])` → `ValueError`/`TypeError`. `quantity`→`int`, `price`/`trigger_price`→`float`, `disclosed_quantity`→`int`. kwargs raw.

```python
optionchain(*, underlying, exchange, expiry_date=None,
            strike_count=None, **kwargs)                                   -> "optionchain"
```
`strike_count` cast to `int`. kwargs raw — so **`with_greeks=True` and `interest_rate=0` work via `**kwargs`** even though they are not named params (the server's `OptionChainSchema` accepts both). Server-side `expiry_date` is `required=True` even though the SDK signature makes it optional.

### 2.5 Utilities / notifications

```python
holidays(year=None)                                                        -> "market/holidays"
timings(date=None)                                                         -> "market/timings"
```
Both take **positional-or-keyword** args and have **no `**kwargs`**. `timings` defaults `date` to `datetime.now().strftime("%Y-%m-%d")` — i.e. server-local *client* date. `holidays` omits `year` from payload if `None`.

```python
telegram(*, username, message, priority=5, **kwargs)                       -> "telegram/notify"
```
`username` is the **OpenAlgo login username**, not the Telegram handle. kwargs raw.

```python
whatsapp(message=None, *, to=None, username=None, image=None, document=None,
         caption=None, filename=None, wait_for_delivery=True, **kwargs)    -> "whatsapp/notify"
```
Only method with a **positional first argument** (`message`). Recipient resolution is if/elif: `username` → `payload["username"]`; `to` list/tuple → `payload["phones"]`; `to` non-empty str → `payload["phone"]`; else `payload["self"] = True`. `image`→`image_path`, `document`→`document_path`. kwargs merged only if key not already present.

### 2.6 `Strategy` (separate class, `openalgo/strategy.py`)

```python
Strategy(host_url: str, webhook_id: str)
  .webhook_url -> str  (property, cached: f"{host}/strategy/webhook/{webhook_id}")
  .strategyorder(symbol: str, action: str, position_size: Optional[int] = None) -> dict
  .close(); __enter__/__exit__
```
**This is the ONLY method in the whole SDK that raises instead of returning an error dict** — it calls `response.raise_for_status()` and re-raises `httpx.HTTPError` after `print()`ing. Args are positional-or-keyword.

---

## 3. CRITICAL naming inconsistencies (complete list)

1. **`price_type` (SDK kwarg) → `pricetype` (wire/payload)**. Affects `placeorder`, `placesmartorder`, `splitorder`, `modifyorder`, `optionsorder`.
2. **`pricetype` (no underscore) inside nested dicts**: `basketorder(orders=[{... "pricetype": ...}])`, `margin(positions=[{... "pricetype": ...}])`, `optionsmultiorder(legs=[{... "pricetype": ...}])`. So the same concept is `price_type` at the top level and `pricetype` one level down. Easy silent bug: passing `price_type` inside a basket/margin/leg dict is forwarded verbatim and rejected/ignored server-side.
3. **`order_id` (SDK kwarg) → `orderid` (payload AND every response)**. Affects `orderstatus`, `modifyorder`, `cancelorder`. Responses (`orderbook`, `tradebook`, `orderstatus`, `basketorder.results`, `splitorder.results`, `optionsorder`, WS `order_update`) all use **`orderid`**. Never `order_id`.
4. **`strategy` default is not uniform**:
   - `"Python"` — placeorder, placesmartorder, basketorder, splitorder, modifyorder, cancelorder, cancelallorder, closeposition, orderstatus, openposition, optionsorder
   - **required, no default** — `optionsmultiorder`
   - **`None` and DEPRECATED** — `optionsymbol`
   - **absent entirely** — funds, orderbook, tradebook, positionbook, holdings, margin, analyzer*, all data methods, options greeks/chain, holidays, timings, telegram, whatsapp
5. **`analyzerstatus()` posts to endpoint `analyzer`** — method name and endpoint differ.
6. **`interval()` vs `intervals()`** — near-identical names; `interval()` is a legacy zero-arg alias.
7. **`symbol` is both a method and a parameter name** — `client.symbol(symbol="X", exchange="NSE")`. Shadowing hazard when generating tool wrappers.
8. **kwargs coercion is inconsistent per module**:
   - `orders.py` (all methods) and `options.py` (`optionsorder`, `optionsymbol`) cast every kwarg with `str()`.
   - `data.py`, `account.py`, `utilities.py`, `telegram.py`, `whatsapp.py`, `options.py` (`optiongreeks`, `optionsmultiorder`, `optionchain`) pass kwargs through raw.
   So `placeorder(..., some_flag=True)` sends the string `"True"`, but `optionchain(..., with_greeks=True)` sends JSON `true`.
9. **All `**kwargs` silently DROP `None` values** (`if value is not None`). You cannot null out a field via kwargs.
10. **`quantity` is always `str()`-cast** in order methods (wire type is a string), but `optionsmultiorder` legs cast it to `int`. `margin` casts `quantity/price/trigger_price` to str.
11. **Keyword-only vs positional inconsistency**: `analyzertoggle(mode)`, `holidays(year)`, `timings(date)`, `whatsapp(message)`, `Strategy.strategyorder(symbol, action, position_size)` accept positionals; everything else after `*` is keyword-only.
12. **`instruments(*, exchange=None)` has no `**kwargs`** while every sibling data method does.
13. **`modifyorder` default `price_type="LIMIT"`** vs `"MARKET"` everywhere else.
14. **WhatsApp arg renames**: `image` → `image_path`, `document` → `document_path`, `to` → `phone`/`phones`.
15. **`WhatsAppAPI._handle_response` is dead code on the unified `api` class.** It contains richer error handling (parses the JSON body of a non-200 to surface the real message), but the MRO resolves `_handle_response` to `OrderAPI`'s. Verified at runtime. A `whatsapp()` failure on `api` therefore yields the generic `HTTP {code}: {text}` message. It only takes effect if you instantiate `WhatsAppAPI` directly.
16. **`ws://` is hard-coded** — an `https://` host still produces an insecure `ws://` URL unless you pass `ws_url` explicitly.

---

## 4. Return types

**Everything returns a `dict` except two methods** — and those two return `pandas.DataFrame` *on success only*, `dict` on failure. Always `isinstance(result, pd.DataFrame)` before treating it as a frame.

### `history(...)` → `pd.DataFrame`
- Built from `result['data']` (a list of bar dicts).
- `df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")` — epoch seconds.
- If `interval not in ['D', 'W', 'M']`: `.dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')` → **tz-aware IST index for intraday**; daily/weekly/monthly stay **tz-naive**. (Note: intervals like `Q`, `Y`, `2W`, `3M` fall into the "intraday" branch and get wrongly tz-converted.)
- **Index**: `timestamp` (DatetimeIndex, named `timestamp`), sorted ascending, duplicates dropped (`keep='first'`).
- **Columns**: `close, high, low, open, volume` (alphabetical, as returned by the server; documented example shows exactly `[437 rows x 5 columns]`). For F&O exchanges the server now always includes open interest, so an **`oi`** column can also appear.
- Empty frame → error dict `{'status':'error','message':'No data available for the specified period','error_type':'no_data'}`.
- Processing exception → `{'status':'error','message':'Failed to process historical data: ...','error_type':'processing_error','raw_data': <original list>}`.

### `instruments(exchange=None)` → `pd.DataFrame`
- **Default `RangeIndex`** (no index set; `pd.concat(..., ignore_index=True)` when downloading all).
- **Columns**: `symbol, brsymbol, name, exchange, token, expiry, strike, lotsize, instrumenttype, tick_size`.
- With `exchange=None` it performs **9 sequential GET requests** (`NSE, BSE, NFO, BFO, MCX, CDS, BCD, NSE_INDEX, BSE_INDEX`), **silently swallowing per-exchange failures** (`except Exception: pass`), then concatenates. This can take minutes and return hundreds of thousands of rows — do not expose it unbounded to an agent.
- All-exchanges failure → `{'status':'error','message':'Failed to fetch instruments from any exchange','error_type':'no_data'}`.

### Feed accessors → `dict` (see §6)
### `connect`, `subscribe_*`, `unsubscribe_*` → `bool`

---

## 5. Error handling

**No exceptions are raised for network/HTTP/API errors — error dicts are returned.** (Exceptions: `Strategy.strategyorder` re-raises `httpx.HTTPError`; `optionsmultiorder` leg parsing raises `KeyError`/`ValueError`; and any missing required keyword-only arg raises `TypeError` before any request is made.)

### `_make_request(endpoint, payload)` (OrderAPI's copy is the live one)
```python
url = self.base_url + endpoint
response = self.client.post(url, json=payload, headers=self.headers, timeout=self.timeout)
return self._handle_response(response)
```
Catches, in order:
| Exception | Returned dict |
|---|---|
| `httpx.TimeoutException` | `{'status':'error','message':'Request timed out. The server took too long to respond.','error_type':'timeout_error'}` |
| `httpx.ConnectError` | `{'status':'error','message':'Failed to connect to the server. Please check if the server is running.','error_type':'connection_error'}` |
| `httpx.HTTPError` | `{'status':'error','message':f'HTTP error occurred: {e}','error_type':'http_error'}` |
| `Exception` | `{'status':'error','message':f'An unexpected error occurred: {e}','error_type':'unknown_error'}` |

### `_handle_response(response)`
| Condition | Returned dict |
|---|---|
| `status_code != 200` | `{'status':'error','message':f'HTTP {code}: {response.text}','code': <int>,'error_type':'http_error'}` |
| 200 + JSON `status == 'error'` | `{'status':'error','message': data.get('message','Unknown error'),'code': 200,'error_type':'api_error'}` — **the rest of the server's response body is discarded** |
| 200 + valid JSON otherwise | the parsed JSON **verbatim** (no wrapping) |
| non-JSON body | `{'status':'error','message':'Invalid JSON response from server','raw_response': response.text,'error_type':'json_error'}` |
| other | `{'status':'error','message': str(e),'error_type':'unknown_error'}` |

**Canonical error dict:** `{'status': 'error', 'message': <str>, 'error_type': <one of timeout_error|connection_error|http_error|api_error|json_error|validation_error|no_data|processing_error|unknown_error>}` plus optional `code` (int) / `raw_response` / `raw_data`.

**Detection rule for wrappers:** `isinstance(r, dict) and r.get('status') == 'error'`. Note that `code` is only present on `http_error`/`api_error`, and `api_error` always reports `code: 200`.

**Timeouts:** `timeout=120.0` seconds by default, passed both to `httpx.Client(timeout=...)` **and** per-request. Connection pool: 20 keepalive / 50 max / 120s keepalive expiry. For an agent tool, 120s is far too long — construct `api(..., timeout=15.0)` or similar.

---

## 6. WebSocket surface (`openalgo/feed.py`)

Transport: `websocket-client`'s `WebSocketApp`, run via `run_forever(ping_interval=20, ping_timeout=10)` in a **daemon `threading.Thread`** (`self.ws_thread`).

```python
connect() -> bool          # clears _shutting_down, then _do_connect()
disconnect() -> None       # sets _shutting_down=True, ws.close(), waits <=2s, clears state
```
`connect()` **blocks up to ~10s**: waits up to 5s for `self.connected`, then up to 5s for `self.authenticated`. Returns `self.authenticated`. Auth message: `{"action":"authenticate","api_key": <key>}`.

```python
subscribe_ltp(instruments: List[Dict[str, Any]], on_data_received: Optional[Callable] = None) -> bool
subscribe_quote(instruments: List[Dict[str, Any]], on_data_received: Optional[Callable] = None) -> bool
subscribe_depth(instruments: List[Dict[str, Any]], on_data_received: Optional[Callable] = None) -> bool
unsubscribe_ltp(instruments)   -> bool
unsubscribe_quote(instruments) -> bool
unsubscribe_depth(instruments) -> bool
subscribe_orders(on_order_update: Optional[Callable] = None) -> bool
unsubscribe_orders() -> bool
```
- Instrument dicts: `{"exchange": "NSE", "symbol": "RELIANCE"}`; `exchange_token` is accepted and used as the symbol if `symbol` is absent. Invalid entries are logged and skipped; if nothing valid remains → `False`.
- All args are **positional-or-keyword** (no `*`).
- Returns `False` immediately if `not self.connected` or `not self.authenticated`.
- One **bulk** frame per call: `{"action":"subscribe","symbols":[...],"mode":<1|2|3>,"depth":5}` (modes: **1 = LTP, 2 = Quote, 3 = Depth**). Unsubscribe frame carries per-entry `mode`.
- Callbacks are stored on the instance (`ltp_callback`, `quote_callback`, `depth_callback`, `order_callback`) — passing a new one **replaces** the previous. There is exactly **one callback per mode for the whole client**, not per symbol. `quotes_callback` exists as an initialized attribute but is never used.
- `subscribe_orders` is account-level (no symbols): sends `{"action":"subscribe_orders"}`, sets `_orders_subscribed = True`.

### Polling snapshot accessors (all thread-safe under `self.lock`)
```python
get_ltp(exchange: str = None, symbol: str = None) -> Dict[str, Any]
    {"ltp":   {EXCH: {SYM: {"timestamp":, "ltp":}}}}
get_quotes(exchange: str = None, symbol: str = None) -> Dict[str, Any]
    {"quote": {EXCH: {SYM: {"timestamp","open","high","low","close","ltp","volume",
                            "last_trade_quantity","avg_trade_price","change","change_percent"}}}}
get_depth(exchange: str = None, symbol: str = None) -> Dict[str, Any]
    {"depth": {EXCH: {SYM: {"timestamp","ltp",
                            "buyBook":  {"1": {"price","qty","orders"}, ... "5": ...},
                            "sellBook": {"1": {...}, ... "5": ...}}}}}
get_orders() -> {"orders": [ <order_update message>, ... ]}   # most-recent-first, capped at 500
```
Filters are plain equality; `symbol` alone (without `exchange`) does still work. Internal store keys are `"EXCHANGE:SYMBOL"` — note `symbol_key.split(":")` takes `parts[1]`, so a symbol containing `:` would be truncated. Snapshots return **whatever last arrived**; there is no freshness guarantee and no blocking wait.

### Callback payloads
LTP/Quote/Depth callbacks get a cleaned dict:
`{"type":"market_data","symbol":...,"exchange":...,"mode":1|2|3,"data":{...}}` (LTP data: `ltp`, `timestamp`, optional `ltt`).
`order_callback` gets the raw server message: `{"type":"order_update","orderid","symbol","exchange","action","quantity","pricetype","product","order_status","filled_quantity","pending_quantity","average_price","rejection_reason","broker"}`.
**Callbacks execute on the WebSocket reader thread**, wrapped in try/except that only logs — an exception in your callback is swallowed. Never do blocking work there; never touch an asyncio loop directly (use `loop.call_soon_threadsafe` / `asyncio.run_coroutine_threadsafe`).

### auto_reconnect (default `True`)
On `on_close`, if `auto_reconnect and not _shutting_down`, `_schedule_reconnect()` starts a daemon thread `openalgo-ws-reconnect` running `_reconnect_loop()`: backoff schedule `[1, 2, 5, 10, 30, 60]` seconds (capped at 60), sleeping in 0.2s slices so `disconnect()` stays responsive; on success calls `_replay_subscriptions()`, which re-sends one bulk subscribe per mode from `_active_subs` and re-sends `subscribe_orders` if `_orders_subscribed`. Callback references survive reconnects. `disconnect()` sets `_shutting_down = True` and suppresses it.

---

## 7. Sync-only — implications for async FastAPI

**The entire client is synchronous and blocking.** `BaseAPI` uses `httpx.Client` (not `AsyncClient`); there are no `async def` methods and no awaitables anywhere in the package. `feed.py` uses `websocket-client` + OS threads, not `asyncio`/`websockets`.

Consequences for the FastAPI wrapper:
- **Every REST call must be offloaded**: `await asyncio.to_thread(client.placeorder, symbol=..., ...)` (or `loop.run_in_executor`). Calling directly from an `async def` route blocks the event loop for up to `timeout` seconds (default **120s**).
- `functools.partial` or a lambda is needed for keyword-only args with `run_in_executor`; `asyncio.to_thread` forwards kwargs natively.
- `httpx.Client` is thread-safe and connection-pooled, so **one shared `api` instance across threads is correct and preferred** — do not build a client per request (that defeats the pooling `BaseAPI` was explicitly written to provide) and remember to call `close()` on app shutdown.
- Lower the timeout at construction (`api(api_key=..., timeout=15.0)`); the thread pool has a bounded size and 120s hangs will exhaust it.
- `instruments()` (especially with no `exchange`) and `history()` over long ranges are the heavy calls — give them their own executor or cache them.
- WebSocket: `connect()` blocks ~10s → offload it too. Feed callbacks run on a non-asyncio thread; bridge with `asyncio.run_coroutine_threadsafe(coro, loop)` or push into a `janus`/thread-safe queue.
- Alternatively skip `feed.py` entirely and poll `get_ltp()/get_quotes()/get_depth()` — these are cheap in-memory, lock-guarded reads (still technically blocking on a `threading.Lock`, but for microseconds).

---

## 8. SDK vs REST server: gap analysis

The OpenAlgo server (`d:\AI Bootcamp 2026\Day08\openalgo\restx_api\__init__.py`) registers **49 namespaces**. The installed SDK 2.0.3 covers 33 of them. Endpoints below exist server-side with **no SDK method**:

| Endpoint | Server schema / file | Payload (POST JSON) |
|---|---|---|
| `/placegttorder` | `PlaceGTTOrderSchema` | `apikey, strategy, trigger_type ('SINGLE'\|'OCO'), exchange, symbol, action, product ('NRML'\|'CNC' only — MIS rejected), quantity, pricetype (default 'LIMIT'), price, triggerprice_sl, triggerprice_tg, stoploss, target, expires_at`. SINGLE: exactly one of `triggerprice_sl`/`triggerprice_tg`. OCO: all four required and `triggerprice_sl < triggerprice_tg`. `last_price` is server-fetched — do not send. |
| `/modifygttorder` | `ModifyGTTOrderSchema` | Same as place **plus required `trigger_id`**; full replacement semantics. |
| `/cancelgttorder` | `CancelGTTOrderSchema` | `apikey, strategy, trigger_id` |
| `/gttorderbook` | `GTTOrderBookSchema` | `apikey` only |
| `/multioptiongreeks` | `MultiOptionGreeksSchema` | `apikey, symbols[1..50] of {symbol, exchange, underlying_symbol?, underlying_exchange?}, interest_rate?, expiry_time?` |
| `/ping` | `PingSchema` | `apikey` only (health/auth check) |
| `/chart` | `ChartSchema` | `apikey` + arbitrary unknown keys (`unknown = INCLUDE`) — chart preferences |
| `/pnl` | `PnlSymbolsSchema` | `apikey` only (per-symbol net P&L) |
| `/ticker` | `TickerSchema` | `apikey, symbol` (combined `EXCHANGE:SYMBOL`), `interval` (1m,5m,15m,30m,1h,4h,D,W,M), `from` (note: `data_key="from"`, Python attr `from_`), `to`, `adjusted?`, `sort?` (asc/desc) |
| `/portfolio` | `portfolio.py` | not exposed by the SDK |
| `/sip` | `sip.py` | not exposed by the SDK |

**Answering the specific questions:** `placegttorder`/`modifygttorder`/`cancelgttorder`/`gttorderbook`, `multioptiongreeks`, `ping`, `chart`, `pnl`, `ticker` → **NOT in SDK 2.0.3**. `multiquotes` → **present**. `optionchain` → present, and `with_greeks` (+ `interest_rate`) works **only through `**kwargs`**, not as a named parameter.

**In the SDK but only reachable server-side under a different name:** `analyzerstatus()` → `/analyzer`, `analyzertoggle()` → `/analyzer/toggle`, `telegram()` → `/telegram/notify`, `whatsapp()` → `/whatsapp/notify`, `holidays()` → `/market/holidays`, `timings()` → `/market/timings`.

**Docs drift:** `docs/prompt/openalgo python sdk.md` documents `with_greeks=True` on `optionchain` and references `multioptiongreeks`, and its option-chain response includes `underlying_prev_close`, `expiry_ts`, `server_ts`, `quotes_included`, `greeks_included`, `forward_price`, `bid_qty`, `ask_qty` — fields **not in the installed SDK docstring**. The docs describe a newer server than SDK 2.0.3's typed surface. The docs also list bot commands (`/pnl`, `/orderbook`, …) which are Telegram/WhatsApp chat commands, not SDK methods.

**Escape hatch:** all missing endpoints are plain POST + `apikey`, so they can be reached without a new SDK release via the private helper:
```python
client._make_request("gttorderbook", {"apikey": client.api_key})
client._make_request("multioptiongreeks", {"apikey": client.api_key, "symbols": [...]})
```
(`_make_request` prepends `base_url` and applies the same error handling.)

---

## 9. Wrapper checklist / top gotchas

1. Sync-only → `asyncio.to_thread(...)` for every call; share one `api` instance; drop `timeout` to ~15s.
2. Errors are **dicts, not exceptions** — check `r.get('status') == 'error'`; but missing required kwargs raise `TypeError`, `optionsmultiorder` legs raise `KeyError`/`ValueError`, and `Strategy.strategyorder` raises `httpx.HTTPError`.
3. `history` / `instruments` return **DataFrame on success, dict on error** — always `isinstance` check, and serialize the frame (`df.reset_index().to_dict('records')`) before handing it to an LLM.
4. `price_type` at top level vs `pricetype` inside basket/margin/leg dicts; `order_id` in, `orderid` out.
5. Nearly everything is **keyword-only**; the exceptions (`analyzertoggle`, `holidays`, `timings`, `whatsapp`, feed methods) are not.
6. kwargs are `str()`-cast in `orders.py`/two `options.py` methods but raw elsewhere; `None` kwargs are silently dropped.
7. `closeposition`, `cancelallorder`, `analyzertoggle(False)` (switch to LIVE) and `instruments()` (9 full downloads) are the dangerous/expensive tools — gate them.
8. `optionchain(with_greeks=True)` works only because of `**kwargs`; server requires `expiry_date` even though the SDK signature says optional.
9. WhatsApp's better error handling is dead code on `api` (MRO shadowing).
10. Constructor positional order is `(api_key, host, version, timeout, ws_port, ws_url, verbose, auto_reconnect)` — always keyword.
11. `ws://` is hard-coded; pass `ws_url="wss://..."` for TLS deployments.
12. Feed callbacks fire on a daemon thread and swallow exceptions; one callback per mode, globally.
