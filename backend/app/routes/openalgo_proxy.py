"""OpenAlgo proxy for the browser chart: REST candles and the live tick socket.

The chart runs in the browser and the OpenAlgo API key must not. Everything in this
module exists so the page can ask for candles and ticks while the credential stays in
this process.

Facts verified against a live OpenAlgo (zerodha session) on 127.0.0.1:5000 and its
WebSocket proxy on 127.0.0.1:8765, with openalgo 2.0.3, websockets 16.0 and
uvicorn 0.52.2, and against the server source in websocket_proxy/server.py:

  - /api/v1/history already returns EPOCH SECONDS. The body is
    {"status":"success","data":[{close,high,low,oi,open,timestamp,volume}, ...]},
    which is exactly the shape the chart library parses. The SDK's history() wrapper
    turns that into a DataFrame with a tz-aware DatetimeIndex and the epoch
    representation is gone, so the wrapper is not used here. Neither is
    normalize.envelope(): the chart parses OpenAlgo's own {status, data}, not the
    backend's {ok, source, data}.

  - The five REST endpoints therefore forward the upstream response BYTE FOR BYTE,
    with the upstream HTTP status code. A caller must read the body whatever the
    status says: an error is HTTP 400 carrying {"status":"error","message":...}, and
    `message` is sometimes a DICT rather than a string. A bad interval answers
    {"interval": ["Must be one of: 1s, 5s, 10s, ..."]}. Anything that str()'d that
    message would hand the browser a Python repr instead of JSON.

  - The SDK's own _handle_response rewrites every non-200 into
    {'status':'error','message': f'HTTP {code}: {body}'} and returns 200 upward, so
    the real JSON ends up nested inside a string. Posting through the SDK's pooled
    httpx.Client but reading the response here keeps the connection pool and loses
    none of that detail.

  - An endpoint name must not carry a leading slash. base_url is already
    slash-terminated and a bare concat produces "/api/v1//history", which the server
    answers with a 308 that httpx does not follow. Same trap as
    OpenAlgoClient.raw_post, guarded the same way.

  - Known limit: OpenAlgoClient.RateLimiter sleeps while holding its threading lock,
    so once the bucket is empty every caller, chart and agent alike, queues behind
    the sleeper. That limiter is shared with the order path and is deliberately left
    alone here. The proxy bounds its own share of the budget instead: at most
    MAX_CONCURRENT_FORWARDS upstream REST calls are in flight from the chart, so a
    page hammering history cannot fill the worker pool or the bucket on its own.

  - WebSocket handshake: {"action":"authenticate","api_key":...} is answered with
    {"type":"auth","status":"success","broker":...,"user_id":...,
    "supported_features":{...}}. The ack carries no credential, so it is relayed on
    to the browser: a client written against the OpenAlgo protocol waits for it
    before it subscribes.

  - The server's error frames carry NO "type" key. They are
    {"status":"error","code":...,"message":...}, plus request_id when the request
    named one (server.send_error). A handshake is refused with AUTHENTICATION_ERROR
    (the key is missing or wrong, and will not change between attempts),
    BROKER_ERROR (no broker configuration or adapter for that user) or
    BROKER_INIT_ERROR (the adapter failed to start). The socket stays OPEN after any
    of them until the server's unauthenticated grace period (15s, close code 4401)
    runs out, so the relay has to close it itself. A relay that waited only for a
    {"type":"auth"} frame sat through its own timeout and then blamed the network.

  - server.handle_client reads the action as data.get("action") or data.get("type"),
    so a frame carrying only "type" is a real command upstream. The screen here reads
    only "action" and refuses a frame without one, so a type-only frame never leaves
    this process.

  - The upstream accepts BOTH subscribe forms and this relay normalises away
    neither. The documented one is
    {"action":"subscribe","symbols":[{symbol,exchange}],"mode":"Depth","depth":5};
    the chart library sends
    {"action":"subscribe","symbol":..,"exchange":..,"mode":3,"depth_level":5}.
    server.subscribe_client falls back to data["symbol"]/data["exchange"] when
    "symbols" is absent, and normalize_mode accepts 1/2/3 or LTP/Quote/Depth in any
    case. It does NOT accept "3" as a string, a float, or a bool.

  - Subscribe reads mode and depth from the TOP LEVEL of the frame only; a "mode"
    inside a symbols[] entry is ignored (server.subscribe_client). Unsubscribe is the
    other way round: a per-entry mode wins, then the top-level mode, then Quote
    (server.unsubscribe_client). Replay identity here follows the same two rules,
    with the mode reduced to its canonical number, so an unsubscribe spelled "Depth"
    removes a subscription that was made as 3.

  - depth_level IS IGNORED BY THE SERVER. subscribe_client reads
    data.get("depth", 5) and nothing anywhere reads depth_level, so a library asking
    for depth_level:20 quietly gets 5 levels. The frame is still forwarded
    unchanged: silently rewriting a client's frame is worse than reporting the
    mismatch. A caller that wants 20 levels has to send "depth".

  - subscribe_orders is accepted upstream and streams the whole account's order
    updates: orderid, quantity, price, product, average_price, rejection_reason.
    This page is read-only market data, so the relay refuses that action, and
    refuses anything outside ALLOWED_ACTIONS, before a frame can reach the socket.

  - An unknown action does not close the upstream connection, it answers
    {"status":"error","code":"INVALID_ACTION","message":...}. Refusals raised here
    copy that shape, so a client needs no special case for a proxy-side rejection.

  - The upstream does not restore a dropped session's subscriptions; it removes that
    session's registry entries during cleanup. A proxy-side reconnect is invisible to
    the browser, whose own socket never moved, so active subscriptions are tracked
    here and replayed after every successful reconnect. Without that the chart goes
    quiet behind an open socket, with no error anywhere.

  - Out of hours a subscribe still produces one immediate market_data snapshot and
    then nothing. Measured 2026-09-01 evening IST: subscribe ack, one full depth
    frame, silence. A test that waits for a second tick waits forever.

  - CORSMiddleware does not apply to a WebSocket upgrade and uvicorn has no origin
    filter, so the Origin header is checked here, by exact match against
    settings.cors_origins, before accept(). A browser always sends Origin on an
    upgrade, so an absent header is refused as well. uvicorn's sansio websockets
    protocol answers a websocket.close sent before accept with an HTTP 403 on the
    upgrade itself: the 1008 code in the ASGI message never reaches the wire, and
    the browser sees a failed handshake, which is the right outcome. A wildcard
    entry is deliberately not honoured for the socket.

  - The websockets client used here arrives with uvicorn[standard], which requires
    websockets>=13.0. That floor is exactly the release that added
    websockets.asyncio.client, so nothing extra is pinned. Anyone who drops the
    [standard] extra from uvicorn has to add websockets>=13.0 to requirements.txt in
    its place.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from typing import Any

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from websockets.asyncio.client import ClientConnection, connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, WebSocketException

from ..config import get_settings
from ..openalgo.client import get_client

log = logging.getLogger(__name__)

settings = get_settings()
client = get_client()

#: Where the browser opens the tick socket. Served through GET /api/oa/config so the
#: frontend never hard-codes it.
WS_PATH = "/api/oa/ws"

router = APIRouter(prefix="/api/oa", tags=["openalgo-proxy"])

# --- REST proxy -------------------------------------------------------------

#: Body keys a client is never allowed to set. The key is injected server-side.
_CLIENT_CREDENTIAL_KEYS = frozenset({"apikey", "api_key", "api-key", "secret",
                                     "password", "auth"})

#: Upstream statuses outside this band mean something other than OpenAlgo answered.
_MIN_HTTP_STATUS, _MAX_HTTP_STATUS = 100, 599

#: Upstream REST calls the chart may have in flight at once. Each one occupies a
#: worker thread and a token from the shared bucket, and the agent's own calls
#: draw on both, so the chart is not allowed to take the whole pool.
MAX_CONCURRENT_FORWARDS = 4
_forward_slots = asyncio.Semaphore(MAX_CONCURRENT_FORWARDS)

#: Field bounds. The body is still buffered before validation, so these bound what
#: can be forwarded upstream rather than what can be posted.
MAX_SYMBOL_LEN = 64
MAX_QUERY_LEN = 128
MAX_EXCHANGE_LEN = 16
MAX_INTERVAL_LEN = 8
MAX_DATE_LEN = 10


class HistoryRequest(BaseModel):
    """Candle request.

    start_date and end_date are IST calendar days in YYYY-MM-DD form, which is the
    OpenAlgo server's own convention and not the chart's display timezone.

    Attributes:
        symbol: OpenAlgo trading symbol, for example RELIANCE.
        exchange: Exchange code, for example NSE.
        interval: Interval code, for example 5m. GET the intervals endpoint for the
            list this broker actually serves.
        start_date: First IST day to include, YYYY-MM-DD.
        end_date: Last IST day to include, YYYY-MM-DD.
    """

    model_config = ConfigDict(extra="ignore")

    symbol: str = Field(max_length=MAX_SYMBOL_LEN)
    exchange: str = Field(max_length=MAX_EXCHANGE_LEN)
    interval: str = Field(max_length=MAX_INTERVAL_LEN)
    start_date: str = Field(max_length=MAX_DATE_LEN)
    end_date: str = Field(max_length=MAX_DATE_LEN)


class IntervalsRequest(BaseModel):
    """Empty body. Present so an apikey posted by the client is dropped."""

    model_config = ConfigDict(extra="ignore")


class SearchRequest(BaseModel):
    """Instrument search.

    Attributes:
        query: Free text, matched against symbol and name.
        exchange: Optional exchange filter.
    """

    model_config = ConfigDict(extra="ignore")

    query: str = Field(max_length=MAX_QUERY_LEN)
    exchange: str | None = Field(default=None, max_length=MAX_EXCHANGE_LEN)


class SymbolRequest(BaseModel):
    """One instrument's contract details.

    Attributes:
        symbol: OpenAlgo trading symbol.
        exchange: Exchange code.
    """

    model_config = ConfigDict(extra="ignore")

    symbol: str = Field(max_length=MAX_SYMBOL_LEN)
    exchange: str = Field(max_length=MAX_EXCHANGE_LEN)


class QuotesRequest(BaseModel):
    """Snapshot quote for one instrument.

    Attributes:
        symbol: OpenAlgo trading symbol.
        exchange: Exchange code.
    """

    model_config = ConfigDict(extra="ignore")

    symbol: str = Field(max_length=MAX_SYMBOL_LEN)
    exchange: str = Field(max_length=MAX_EXCHANGE_LEN)


def _ascii(value: Any, limit: int = 200) -> str:
    """Render a value for the log: ASCII only and bounded.

    Args:
        value: Anything, typically text that arrived from a socket.
        limit: Maximum characters to keep.

    Returns:
        A printable ASCII string with other codepoints escaped.
    """
    text = str(value)[:limit]
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _redact(text: str) -> str:
    """Blank the configured api key wherever it appears in text.

    Args:
        text: Text that may echo a credential, such as an upstream error message.

    Returns:
        The text with every occurrence of the key replaced.
    """
    key = settings.openalgo_api_key
    if key and key in text:
        return text.replace(key, "[redacted]")
    return text


def _error(status: int, message: str, error_type: str) -> JSONResponse:
    """Build a proxy-side error in OpenAlgo's own error vocabulary.

    Args:
        status: HTTP status to answer with.
        message: Plain text a user can act on. Never a traceback.
        error_type: OpenAlgo's error_type token, for example connection_error.

    Returns:
        A JSON response the chart parses exactly like an upstream failure.
    """
    return JSONResponse(
        status_code=status,
        content={"status": "error", "message": message, "error_type": error_type},
    )


def _forward(endpoint: str, payload: dict[str, Any]) -> Response:
    """Post to OpenAlgo and wrap its own bytes, status and content type.

    Blocking: httpx.Client is synchronous. Always call this through
    asyncio.to_thread, never from the event loop.

    The API key is injected here and a client-supplied one is discarded first, so a
    browser cannot borrow the server's credential or smuggle in another.

    Args:
        endpoint: Endpoint name with no leading slash, for example "history".
        payload: Request body, without the api key.

    Returns:
        The upstream JSON body verbatim under the upstream status code, or a clean
        proxy-side error when the upstream could not be reached or did not answer
        with JSON.
    """
    name = endpoint.strip().lstrip("/")
    body = {k: v for k, v in payload.items() if k.lower() not in _CLIENT_CREDENTIAL_KEYS}
    body["apikey"] = settings.openalgo_api_key
    url = f"{client.raw.base_url}{name}"

    # The token bucket lives on OpenAlgoClient so chart traffic and agent traffic draw
    # on ONE budget. Reaching for the private attribute is deliberate: a second
    # limiter here would let each half of the process believe it had the whole
    # 50 req/s to itself.
    client._general.acquire()  # noqa: SLF001

    try:
        upstream = client.raw.client.post(
            url, json=body, headers=client.raw.headers, timeout=client.raw.timeout,
        )
    except httpx.TimeoutException:
        log.warning("openalgo proxy: %s timed out after %ss", name, client.raw.timeout)
        return _error(504, "OpenAlgo took too long to respond.", "timeout_error")
    except httpx.ConnectError:
        log.warning("openalgo proxy: cannot reach %s", settings.openalgo_host)
        return _error(502, "Cannot reach OpenAlgo. Check that it is running.",
                      "connection_error")
    except httpx.HTTPError as exc:
        log.warning("openalgo proxy: %s failed: %s", name, type(exc).__name__)
        return _error(502, "The request to OpenAlgo failed.", "http_error")

    content_type = (upstream.headers.get("content-type") or "").split(";")[0].strip()
    if content_type != "application/json":
        # A redirect page or an HTML error page. The body is not echoed: it is not
        # the caller's to read and it can be large.
        log.warning("openalgo proxy: %s answered %s with content-type %r",
                    name, upstream.status_code, content_type)
        return _error(502, "OpenAlgo did not answer with JSON.", "json_error")

    status = upstream.status_code
    if not _MIN_HTTP_STATUS <= status <= _MAX_HTTP_STATUS:
        status = 502

    # Byte-for-byte. Reparsing and re-serialising would round-trip every float and
    # reorder nothing usefully.
    return Response(content=upstream.content, status_code=status,
                    media_type="application/json")


async def _proxy(endpoint: str, payload: dict[str, Any]) -> Response:
    """Forward one request off the event loop, bounded to MAX_CONCURRENT_FORWARDS.

    Args:
        endpoint: Endpoint name with no leading slash.
        payload: Request body, without the api key.

    Returns:
        The response built by _forward.
    """
    async with _forward_slots:
        return await asyncio.to_thread(_forward, endpoint, payload)


# Every endpoint is registered twice. The second path lets a client written against
# OpenAlgo's own layout point its base URL at "/api/oa" and reach "/api/v1/history"
# from there unchanged. It is hidden from the schema so the two do not collide in the
# OpenAPI document.


@router.post("/history")
@router.post("/api/v1/history", include_in_schema=False)
async def history(req: HistoryRequest) -> Response:
    """Historical candles, passed through with timestamps in epoch seconds.

    Args:
        req: Symbol, exchange, interval and the IST date range.

    Returns:
        OpenAlgo's own {"status":"success","data":[{timestamp, open, high, low,
        close, volume, oi}, ...]} body, unaltered.
    """
    return await _proxy("history", req.model_dump())


@router.post("/intervals")
@router.post("/api/v1/intervals", include_in_schema=False)
async def intervals(req: IntervalsRequest | None = None) -> Response:
    """Interval codes this broker serves, grouped by unit.

    Args:
        req: Ignored. Present so a posted apikey is dropped rather than forwarded.

    Returns:
        OpenAlgo's {"status":"success","data":{seconds, minutes, hours, days, weeks,
        months}} body, unaltered.
    """
    return await _proxy("intervals", {})


@router.post("/search")
@router.post("/api/v1/search", include_in_schema=False)
async def search(req: SearchRequest) -> Response:
    """Instrument search.

    Args:
        req: Free-text query and an optional exchange filter.

    Returns:
        OpenAlgo's {"status":"success","data":[{symbol, exchange, name, lotsize,
        tick_size, token, ...}, ...]} body, unaltered.
    """
    payload: dict[str, Any] = {"query": req.query}
    if req.exchange:
        payload["exchange"] = req.exchange
    return await _proxy("search", payload)


@router.post("/symbol")
@router.post("/api/v1/symbol", include_in_schema=False)
async def symbol(req: SymbolRequest) -> Response:
    """Contract details for one instrument.

    Args:
        req: Symbol and exchange.

    Returns:
        OpenAlgo's {"status":"success","data":{...}} body, unaltered.
    """
    return await _proxy("symbol", req.model_dump())


@router.post("/quotes")
@router.post("/api/v1/quotes", include_in_schema=False)
async def quotes(req: QuotesRequest) -> Response:
    """Snapshot quote for one instrument.

    Args:
        req: Symbol and exchange.

    Returns:
        OpenAlgo's {"status":"success","data":{ltp, bid, ask, open, high, low,
        prev_close, volume, oi}} body, unaltered.
    """
    return await _proxy("quotes", req.model_dump())


@router.get("/config")
async def feed_config() -> dict[str, Any]:
    """What the frontend needs in order not to hard-code anything.

    Returns:
        ws_path, the same-origin path the tick socket is served on;
        host_reachable, whether OpenAlgo answered a ping just now; and
        relays_open with relays_max, how many tick sockets this process is carrying
        against its cap.
    """
    ping = await asyncio.to_thread(client.ping)
    return {"ws_path": WS_PATH, "host_reachable": bool(ping.get("ok")),
            "relays_open": active_relays(), "relays_max": MAX_RELAYS}


# --- WebSocket relay --------------------------------------------------------

#: The only actions a read-only chart page may send upstream.
#:
#: "pong" is here although the brief did not name it: the chart library answers an
#: inbound application-level ping with {"action":"pong"}, and a refused pong would
#: look to that client like a broken feed. It carries nothing and reaches nothing.
ALLOWED_ACTIONS = frozenset({"subscribe", "unsubscribe", "unsubscribe_all",
                             "ping", "pong"})

#: Refused with their own message, because refusing them is the point of this relay.
#: subscribe_orders streams the whole account's fills and rejections.
ORDER_STREAM_ACTIONS = frozenset({"subscribe_orders", "unsubscribe_orders"})

#: Stripped from every client frame at any depth. The relay authenticates upstream
#: itself; a client has no business naming a credential. "token" is deliberately
#: absent, because OpenAlgo uses it for instrument tokens.
_WS_CREDENTIAL_KEYS = frozenset({"api_key", "apikey", "api-key", "secret",
                                 "password", "auth"})

#: Actions whose instruments are tracked so they can be replayed after a reconnect.
_SUBSCRIBE_ACTIONS = frozenset({"subscribe"})
_UNSUBSCRIBE_ACTIONS = frozenset({"unsubscribe"})

#: The server's mode vocabulary (websocket_proxy/mode_utils.normalize_mode): the
#: numbers 1, 2, 3 or these labels in any case. Nothing else is accepted.
_MODE_BY_LABEL = {"ltp": 1, "quote": 2, "depth": 3}
_MODE_NUMBERS = frozenset({1, 2, 3})

#: The server's default when a frame names no mode at all: Quote, on both the
#: subscribe and the unsubscribe path.
_DEFAULT_MODE = 2

#: Frame fields a replay needs. Nothing else the client sent is kept.
_REPLAY_SETTINGS = ("mode", "depth", "depth_level")
#: A replayed setting is a number or a short string; a 60KB "depth" is not a setting.
_MAX_SETTING_LEN = 16
_MAX_NAME_LEN = 64

#: Upstream codes that mean the key itself was refused. The key does not change
#: between attempts, so reconnecting after one of these only repeats the refusal.
_FATAL_AUTH_CODES = frozenset({"AUTHENTICATION_ERROR"})

MAX_CLIENT_FRAME_BYTES = 64 * 1024
MAX_UPSTREAM_FRAME_BYTES = 8 * 1024 * 1024
#: How deep a client frame may nest. json.loads survives far deeper input than the
#: recursive credential strip did, and a frame that deep is never a subscribe.
MAX_FRAME_DEPTH = 32
#: Matches the server's own per-connection symbol budget (MAX_SYMBOLS_PER_WEBSOCKET).
MAX_TRACKED_SUBSCRIPTIONS = 1000

#: Tick sockets this process will carry at once. Each one is an authenticated
#: upstream session with its own keepalive, and one browser tab needs one.
MAX_RELAYS = 8
_relay_slots = asyncio.Semaphore(MAX_RELAYS)

UPSTREAM_OPEN_TIMEOUT_SEC = 10.0
UPSTREAM_AUTH_TIMEOUT_SEC = 10.0
#: How long a client frame waits while the relay is between upstream connections.
UPSTREAM_WAIT_SEC = 30.0
#: Control-frame keepalive, matching the server's own 20 second default.
UPSTREAM_PING_INTERVAL_SEC = 20.0
UPSTREAM_PING_TIMEOUT_SEC = 20.0

MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_DELAY_BASE_SEC = 0.5
RECONNECT_DELAY_MAX_SEC = 8.0

WS_CLOSE_NORMAL = 1000
WS_CLOSE_POLICY_VIOLATION = 1008
WS_CLOSE_UPSTREAM_GONE = 1011
WS_CLOSE_TRY_AGAIN_LATER = 1013


class UpstreamAuthRejected(Exception):
    """The upstream answered the handshake with a status "error" frame.

    Attributes:
        code: The upstream's error code, for example AUTHENTICATION_ERROR.
        message: The upstream's message with the api key redacted.
    """

    def __init__(self, code: str, message: str) -> None:
        """Record the upstream's verdict.

        Args:
            code: The upstream's error code.
            message: The upstream's message, already redacted.
        """
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def active_relays() -> int:
    """How many tick sockets this process is relaying right now.

    Returns:
        A count between 0 and MAX_RELAYS.
    """
    return MAX_RELAYS - _relay_slots._value  # noqa: SLF001


def _origin_allowed(origin: str | None) -> bool:
    """Whether a WebSocket upgrade's Origin may open the feed.

    Exact match, scheme and port included, against settings.cors_origins. An absent
    header is refused: a browser always sends one on an upgrade, so its absence
    means the caller is not the page this feed exists for.

    Args:
        origin: The Origin header, or None when the upgrade carried none.

    Returns:
        True when the origin is in the allowlist.
    """
    return origin is not None and origin in settings.cors_origins


def _strip_credentials(value: Any, _depth: int = 0) -> Any:
    """Recursively drop credential-looking keys from a client frame.

    Args:
        value: Any decoded JSON value.
        _depth: Current nesting level, used to enforce MAX_FRAME_DEPTH.

    Returns:
        The same structure with every credential key removed at every depth.

    Raises:
        ValueError: When the structure nests deeper than MAX_FRAME_DEPTH.
    """
    if _depth > MAX_FRAME_DEPTH:
        raise ValueError("frame nested too deeply")
    if isinstance(value, dict):
        return {k: _strip_credentials(v, _depth + 1) for k, v in value.items()
                if str(k).lower() not in _WS_CREDENTIAL_KEYS}
    if isinstance(value, list):
        return [_strip_credentials(v, _depth + 1) for v in value]
    return value


def _refusal(code: str, message: str, request_id: Any = None,
             **extra: Any) -> dict[str, Any]:
    """Build a refusal in the shape the upstream uses for its own errors.

    Args:
        code: Short machine token, for example ORDER_STREAM_FORBIDDEN.
        message: Plain text explaining what was refused and why.
        request_id: Echoed back when the client supplied one.
        **extra: Further fields to carry, for example the upstream's own code.

    Returns:
        A frame ready to send to the browser.
    """
    frame: dict[str, Any] = {"type": "error", "status": "error", "code": code,
                             "message": message}
    if request_id is not None:
        frame["request_id"] = request_id
    frame.update(extra)
    return frame


def _is_name(value: Any) -> bool:
    """Whether a value can be a symbol or exchange name.

    Args:
        value: A field read from a client frame.

    Returns:
        True for a non-empty string of at most _MAX_NAME_LEN characters.
    """
    return isinstance(value, str) and 0 < len(value) <= _MAX_NAME_LEN


def _is_setting(value: Any) -> bool:
    """Whether a value can be a mode or depth setting worth replaying.

    Args:
        value: A field read from a client frame.

    Returns:
        True for an int, a float, or a short string. Never a bool or a container.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and len(value) <= _MAX_SETTING_LEN


def _canonical_mode(raw: Any) -> str | None:
    """Reduce a mode to the number the server would resolve it to.

    Mirrors websocket_proxy/mode_utils.normalize_mode exactly: the ints 1, 2, 3 and
    the labels LTP, Quote, Depth in any case. A string "3", a float, a bool or
    anything else is refused by the server with INVALID_MODE, so it has no identity.

    Args:
        raw: The mode as the client spelled it.

    Returns:
        "1", "2" or "3", or None when the server would refuse the value.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return str(raw) if raw in _MODE_NUMBERS else None
    if isinstance(raw, str):
        number = _MODE_BY_LABEL.get(raw.strip().lower())
        return str(number) if number is not None else None
    return None


def _instruments_of(frame: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the instruments a subscribe or unsubscribe frame names.

    Handles both wire forms: the documented "symbols" array and the single-symbol
    form the chart library sends. An entry whose symbol or exchange is not a short
    string is skipped, as the server skips it.

    Args:
        frame: A screened client frame.

    Returns:
        A list of instrument dicts, possibly empty. Array entries are returned as
        sent so a per-entry mode is still visible to _sub_key.
    """
    entries = frame.get("symbols")
    if isinstance(entries, list):
        return [e for e in entries if isinstance(e, dict)
                and _is_name(e.get("symbol")) and _is_name(e.get("exchange"))]
    if _is_name(frame.get("symbol")) and _is_name(frame.get("exchange")):
        return [{"symbol": frame["symbol"], "exchange": frame["exchange"]}]
    return []


def _mode_key(frame: dict[str, Any], entry: dict[str, Any], action: str) -> str | None:
    """Resolve the mode the server applies to one instrument.

    The precedence differs by action and this mirrors the server, not the
    documentation: subscribe_client reads the top-level mode only, while
    unsubscribe_client lets a per-entry mode win. Both default to Quote.

    Args:
        frame: The whole client frame.
        entry: One instrument from that frame.
        action: The lowercased action, subscribe or unsubscribe.

    Returns:
        The canonical mode number as a string, or None when the server would
        refuse the value.
    """
    if action in _UNSUBSCRIBE_ACTIONS and "mode" in entry:
        raw = entry["mode"]
    else:
        raw = frame.get("mode", _DEFAULT_MODE)
    return _canonical_mode(raw)


def _sub_key(frame: dict[str, Any], entry: dict[str, Any],
             action: str = "subscribe") -> tuple[str, str, str] | None:
    """Canonical identity of one subscription.

    Args:
        frame: The whole client frame.
        entry: One instrument from that frame.
        action: The lowercased action the frame carries.

    Returns:
        (exchange, symbol, mode), uppercased where the server is case-insensitive
        and with the mode as its canonical number, or None when the server would
        refuse the mode and so never hold the subscription.
    """
    mode = _mode_key(frame, entry, action)
    if mode is None:
        return None
    return (str(entry.get("exchange", "")).upper(),
            str(entry.get("symbol", "")).upper(),
            mode)


def _replay_frame(frame: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Build the minimal single-instrument frame a replay sends.

    Only the fields the server reads are kept: the action, the instrument, and the
    top-level mode, depth and depth_level as the client spelled them. request_id is
    dropped so a replayed ack cannot be mistaken for an answer to the original
    request, and everything else the client sent is not stored at all.

    Args:
        frame: The screened subscribe frame.
        entry: One instrument from that frame.

    Returns:
        A frame in the single-symbol wire form.
    """
    single: dict[str, Any] = {"action": "subscribe", "symbol": entry["symbol"],
                              "exchange": entry["exchange"]}
    for name in _REPLAY_SETTINGS:
        value = frame.get(name)
        if value is not None and _is_setting(value):
            single[name] = value
    return single


class ChartFeedRelay:
    """One browser socket, one authenticated upstream socket, frames both ways.

    The browser sends no credential. This relay performs the OpenAlgo handshake with
    the real key, screens every client frame against ALLOWED_ACTIONS, and relays
    everything the upstream sends back. It reconnects upstream on a bounded backoff
    and replays the subscriptions it is holding, because the browser's own socket
    never noticed the drop and would otherwise sit open and silent.
    """

    def __init__(self, browser: WebSocket) -> None:
        """Bind the relay to an accepted browser socket.

        Args:
            browser: The already accepted client WebSocket.
        """
        self._browser = browser
        self._upstream: ClientConnection | None = None
        self._ready = asyncio.Event()
        self._closing = False
        self._subs: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
        # Held across a replay and across every client frame's screen-and-forward,
        # so an unsubscribe cannot land between two replayed subscribes and leave
        # the fresh upstream holding something the client removed.
        self._subs_lock = asyncio.Lock()
        self._sub_overflow_logged = False
        #: The upstream code of the last handshake refusal, or None.
        self._auth_rejected: str | None = None

    # -- lifecycle --------------------------------------------------------

    async def run(self) -> None:
        """Open the upstream, relay until either side goes away, then clean up."""
        if not settings.openalgo_api_key:
            await self._send_browser(_refusal(
                "OPENALGO_KEY_MISSING",
                "The backend has no OPENALGO_API_KEY configured, so the market data "
                "feed cannot be opened."))
            await self._close_browser(WS_CLOSE_UPSTREAM_GONE, "api key not configured")
            return

        if not await self._open_upstream():
            if self._auth_rejected is None:
                await self._send_browser(_refusal(
                    "UPSTREAM_UNAVAILABLE",
                    "The OpenAlgo market data feed did not accept a connection."))
                await self._close_browser(WS_CLOSE_UPSTREAM_GONE, "upstream unavailable")
            else:
                await self._close_browser(WS_CLOSE_UPSTREAM_GONE,
                                          "upstream rejected authentication")
            return

        tasks = [
            asyncio.create_task(self._pump_browser(), name="oa-ws-browser"),
            asyncio.create_task(self._pump_upstream(), name="oa-ws-upstream"),
        ]
        try:
            done, pending = await asyncio.wait(tasks,
                                               return_when=asyncio.FIRST_COMPLETED)
            self._closing = True
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    log.warning("openalgo feed: %s ended with %s: %s",
                                task.get_name(), type(exc).__name__, _ascii(exc))
        finally:
            self._closing = True
            # If run() itself was cancelled, asyncio.wait left both pumps running.
            # Nothing may outlive the relay: a pump that survived would hold the
            # upstream connection and its keepalive open with no browser behind it.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._close_upstream()
            await self._close_browser(WS_CLOSE_NORMAL, "feed closed")

    async def _open_upstream(self) -> bool:
        """Connect upstream and complete the authentication handshake.

        Nothing is relayed to the browser until the auth ack arrives. The ack itself
        is relayed, because it carries no credential and a client written against the
        OpenAlgo protocol waits for it before subscribing.

        The connection is closed on every way out except success, cancellation
        included: a browser that leaves mid-handshake must not leave an
        authenticated upstream session and its keepalive behind.

        Returns:
            True when an authenticated connection is in place. On False,
            _auth_rejected says whether the upstream refused the handshake.
        """
        self._auth_rejected = None
        try:
            conn = await ws_connect(
                settings.openalgo_ws_url,
                open_timeout=UPSTREAM_OPEN_TIMEOUT_SEC,
                ping_interval=UPSTREAM_PING_INTERVAL_SEC,
                ping_timeout=UPSTREAM_PING_TIMEOUT_SEC,
                max_size=MAX_UPSTREAM_FRAME_BYTES,
            )
        except (OSError, WebSocketException, InvalidHandshake, TimeoutError,
                asyncio.TimeoutError) as exc:
            log.warning("openalgo feed: cannot open %s: %s",
                        settings.openalgo_ws_url, type(exc).__name__)
            return False

        try:
            ack = await self._authenticate(conn)
        except UpstreamAuthRejected as exc:
            await self._quiet_close(conn)
            self._auth_rejected = exc.code
            log.error("openalgo feed: upstream rejected authentication: %s: %s",
                      _ascii(exc.code, 64), _ascii(exc.message))
            await self._send_browser(_refusal(
                "UPSTREAM_AUTH_REJECTED",
                f"OpenAlgo refused the feed's authentication ({exc.code}): "
                f"{exc.message}",
                upstream_code=exc.code, upstream_message=exc.message))
            return False
        except BaseException:
            # CancelledError above all. The task is going away; the socket must too.
            await self._quiet_close(conn)
            raise

        if ack is None:
            await self._quiet_close(conn)
            return False

        self._upstream = conn
        self._ready.set()
        await self._send_browser_text(ack)
        return True

    async def _authenticate(self, conn: ClientConnection) -> str | None:
        """Send the handshake and wait for the ack.

        The key is never logged, at any level, and an upstream message that echoes
        it is redacted before it goes anywhere.

        Args:
            conn: A freshly opened upstream connection.

        Returns:
            The raw auth ack to relay on, or None when no ack arrived in time or the
            connection dropped.

        Raises:
            UpstreamAuthRejected: The upstream answered with a status "error" frame,
                or an auth frame whose status is not success.
        """
        try:
            await conn.send(json.dumps({"action": "authenticate",
                                        "api_key": settings.openalgo_api_key}))
        except (ConnectionClosed, WebSocketException):
            return None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + UPSTREAM_AUTH_TIMEOUT_SEC
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.warning("openalgo feed: no auth ack within %ss",
                            UPSTREAM_AUTH_TIMEOUT_SEC)
                return None
            try:
                raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
            except (TimeoutError, asyncio.TimeoutError, ConnectionClosed,
                    WebSocketException):
                log.warning("openalgo feed: upstream closed during authentication")
                return None

            text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            try:
                message = json.loads(text)
            except (ValueError, RecursionError):
                continue
            if not isinstance(message, dict):
                continue

            status = str(message.get("status", "")).lower()
            is_auth = message.get("type") == "auth"
            if status == "error" or (is_auth and status != "success"):
                # The server's error frames have no "type"; see the module docstring.
                code = _ascii(message.get("code") or "UPSTREAM_ERROR", 64)
                detail = _redact(_ascii(message.get("message") or "", 300))
                raise UpstreamAuthRejected(code, detail)
            if not is_auth:
                # Nothing is relayed before the ack, so anything else is dropped.
                continue
            log.info("openalgo feed: authenticated upstream, broker=%s",
                     _ascii(message.get("broker"), 64))
            return text

    async def _reopen_upstream(self) -> bool:
        """Reconnect upstream on a bounded exponential backoff and replay subs.

        Cancellation propagates straight out of the backoff sleep: a relay that is
        being torn down must not keep reconnecting.

        Returns:
            True when a reconnect succeeded within MAX_RECONNECT_ATTEMPTS.
        """
        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            delay = min(RECONNECT_DELAY_MAX_SEC,
                        RECONNECT_DELAY_BASE_SEC * (2 ** (attempt - 1)))
            await asyncio.sleep(delay)
            if self._closing:
                return False
            log.info("openalgo feed: reconnect attempt %s of %s",
                     attempt, MAX_RECONNECT_ATTEMPTS)
            if await self._open_upstream():
                await self._replay()
                return True
            if self._auth_rejected in _FATAL_AUTH_CODES:
                log.error("openalgo feed: not retrying after %s, the key will not "
                          "change between attempts", self._auth_rejected)
                return False
        log.error("openalgo feed: giving up after %s reconnect attempts",
                  MAX_RECONNECT_ATTEMPTS)
        return False

    # -- pumps ------------------------------------------------------------

    async def _pump_browser(self) -> None:
        """Screen client frames and forward the allowed ones upstream."""
        while not self._closing:
            try:
                message = await self._browser.receive()
            except (WebSocketDisconnect, RuntimeError):
                return
            if message.get("type") == "websocket.disconnect":
                return

            raw = message.get("text")
            if raw is None:
                payload = message.get("bytes")
                raw = payload.decode("utf-8", "replace") if payload else ""
            if not raw:
                continue
            if len(raw) > MAX_CLIENT_FRAME_BYTES:
                await self._send_browser(_refusal(
                    "FRAME_TOO_LARGE",
                    f"A frame may not exceed {MAX_CLIENT_FRAME_BYTES} bytes."))
                continue

            try:
                frame = json.loads(raw)
            except (ValueError, RecursionError):
                await self._send_browser(_refusal(
                    "INVALID_JSON", "A frame must be a JSON object."))
                continue
            if not isinstance(frame, dict):
                await self._send_browser(_refusal(
                    "INVALID_FRAME", "A frame must be a JSON object."))
                continue

            if not await self._handle_client_frame(frame):
                return

    async def _handle_client_frame(self, frame: dict[str, Any]) -> bool:
        """Screen one decoded frame and forward it, serialised against a replay.

        Args:
            frame: A decoded client frame, not yet screened.

        Returns:
            False when the relay should stop, True otherwise.
        """
        async with self._subs_lock:
            forward, refusal = self._screen(frame)
            if refusal is not None:
                await self._send_browser(refusal)
                return True
            if forward is None:
                return True
            return await self._forward_upstream(forward)

    async def _pump_upstream(self) -> None:
        """Relay every upstream frame to the browser, reconnecting on a drop."""
        while not self._closing:
            conn = self._upstream
            if conn is None:
                return
            try:
                async for raw in conn:
                    text = raw if isinstance(raw, str) else raw.decode("utf-8",
                                                                       "replace")
                    await self._browser.send_text(text)
            except (ConnectionClosed, WebSocketException):
                pass
            except (WebSocketDisconnect, RuntimeError):
                return  # the browser went away mid-send

            if self._closing:
                return

            self._ready.clear()
            log.info("openalgo feed: upstream connection closed, reconnecting")
            await self._send_browser({"type": "proxy", "status": "reconnecting",
                                      "message": "Reconnecting to the OpenAlgo feed."})
            if not await self._reopen_upstream():
                if self._auth_rejected is None:
                    # After a rejection the browser already holds the precise
                    # reason; "could not reconnect" would only muddy it.
                    await self._send_browser(_refusal(
                        "UPSTREAM_UNAVAILABLE",
                        "Lost the OpenAlgo market data feed and could not reconnect."))
                return

    async def _forward_upstream(self, frame: dict[str, Any]) -> bool:
        """Send one screened frame upstream, waiting out a reconnect if needed.

        Args:
            frame: The screened, credential-stripped frame.

        Returns:
            False when the relay should stop, True otherwise. A send that fails
            because the upstream just dropped returns True: the upstream pump owns
            reconnecting, and the frame is replayed from the tracked set if it was a
            subscribe.
        """
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=UPSTREAM_WAIT_SEC)
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("openalgo feed: no upstream after %ss, dropping the client",
                        UPSTREAM_WAIT_SEC)
            return False
        conn = self._upstream
        if conn is None:
            return False
        try:
            await conn.send(json.dumps(frame))
        except (ConnectionClosed, WebSocketException):
            log.debug("openalgo feed: upstream send failed, reconnect pending")
        return True

    # -- screening --------------------------------------------------------

    def _screen(self, frame: dict[str, Any]
                ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Decide whether a client frame may go upstream, and clean it if so.

        The action is compared lowercased so no case variant of subscribe_orders
        slips past, but the frame's own spelling is forwarded untouched: the upstream
        matches actions exactly, so a mis-cased action fails there rather than being
        silently corrected here. Only "action" is read: the upstream would also
        honour "type", so a frame with no action is refused rather than forwarded.

        Args:
            frame: A decoded client frame.

        Returns:
            (frame to forward, refusal to send back). Exactly one is not None.
        """
        request_id = frame.get("request_id")
        action = str(frame.get("action") or "").strip().lower()

        if action in ORDER_STREAM_ACTIONS:
            log.warning("openalgo feed: refused %s from a chart client", action)
            return None, _refusal(
                "ORDER_STREAM_FORBIDDEN",
                "The order stream is not available on the chart feed. This "
                "connection carries read-only market data.", request_id)

        if action not in ALLOWED_ACTIONS:
            return None, _refusal(
                "ACTION_NOT_ALLOWED",
                f"Action {_ascii(action, 64) or '(missing)'} is not allowed on the "
                f"chart feed. Allowed: {', '.join(sorted(ALLOWED_ACTIONS))}.",
                request_id)

        try:
            forward = _strip_credentials(frame)
        except (ValueError, RecursionError):
            return None, _refusal(
                "INVALID_JSON",
                f"A frame may not nest more than {MAX_FRAME_DEPTH} levels deep.",
                request_id)

        if action in _SUBSCRIBE_ACTIONS:
            if not self._remember(forward):
                return None, _refusal(
                    "SUBSCRIPTION_LIMIT",
                    f"This connection already tracks {MAX_TRACKED_SUBSCRIPTIONS} "
                    "subscriptions. Unsubscribe before adding more.", request_id)
        elif action in _UNSUBSCRIBE_ACTIONS:
            self._forget(forward)
        elif action == "unsubscribe_all":
            self._subs.clear()
        return forward, None

    # -- subscription tracking -------------------------------------------

    def _remember(self, frame: dict[str, Any]) -> bool:
        """Record a subscribe frame so it can be replayed after a reconnect.

        Each instrument is stored as its own minimal single-instrument frame (see
        _replay_frame). A frame that would push the tracked set past
        MAX_TRACKED_SUBSCRIPTIONS is not recorded at all, and the caller refuses it
        rather than forwarding something that could never be replayed.

        Args:
            frame: A screened subscribe frame.

        Returns:
            True when every instrument in the frame is tracked, or the frame names
            none. False when the frame does not fit.
        """
        records: dict[tuple[str, str, str], dict[str, Any]] = {}
        for entry in _instruments_of(frame):
            key = _sub_key(frame, entry, "subscribe")
            if key is None:
                continue  # the server will refuse the mode; nothing to replay
            records[key] = _replay_frame(frame, entry)
        if not records:
            return True
        new = sum(1 for key in records if key not in self._subs)
        if len(self._subs) + new > MAX_TRACKED_SUBSCRIPTIONS:
            if not self._sub_overflow_logged:
                log.warning("openalgo feed: %s subscriptions tracked, refusing "
                            "further ones", MAX_TRACKED_SUBSCRIPTIONS)
                self._sub_overflow_logged = True
            return False
        self._subs.update(records)
        return True

    def _forget(self, frame: dict[str, Any]) -> None:
        """Drop the instruments an unsubscribe frame names from the replay set.

        Args:
            frame: A screened unsubscribe frame.
        """
        for entry in _instruments_of(frame):
            key = _sub_key(frame, entry, "unsubscribe")
            if key is not None:
                self._subs.pop(key, None)

    async def _replay(self) -> None:
        """Resend every tracked subscription on a fresh upstream connection.

        Runs under the subscription lock, so no client frame is screened or
        forwarded while the set is being replayed.
        """
        async with self._subs_lock:
            conn = self._upstream
            if conn is None or not self._subs:
                return
            log.info("openalgo feed: replaying %s subscription(s)", len(self._subs))
            for frame in list(self._subs.values()):
                try:
                    await conn.send(json.dumps(frame))
                except (ConnectionClosed, WebSocketException):
                    log.warning("openalgo feed: replay interrupted by another drop")
                    return

    # -- socket helpers ---------------------------------------------------

    async def _send_browser(self, frame: dict[str, Any]) -> None:
        """Send one JSON frame to the browser, ignoring a closed socket.

        Args:
            frame: The frame to serialise and send.
        """
        await self._send_browser_text(json.dumps(frame))

    async def _send_browser_text(self, text: str) -> None:
        """Send raw text to the browser, ignoring a closed socket.

        Args:
            text: An already serialised frame.
        """
        try:
            await self._browser.send_text(text)
        except (WebSocketDisconnect, RuntimeError, OSError):
            log.debug("openalgo feed: browser socket is gone")

    async def _close_browser(self, code: int, reason: str) -> None:
        """Close the browser socket, tolerating one that is already closed.

        Args:
            code: WebSocket close code.
            reason: Short ASCII reason.
        """
        try:
            await self._browser.close(code=code, reason=reason)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass

    async def _close_upstream(self) -> None:
        """Close the upstream connection if one is open."""
        conn, self._upstream = self._upstream, None
        self._ready.clear()
        if conn is not None:
            await self._quiet_close(conn)

    @staticmethod
    async def _quiet_close(conn: ClientConnection) -> None:
        """Close an upstream connection without raising.

        Args:
            conn: The connection to close.
        """
        try:
            await conn.close()
        except (ConnectionClosed, WebSocketException, OSError):
            pass


async def _refuse_after_accept(websocket: WebSocket, frame: dict[str, Any],
                               code: int, reason: str) -> None:
    """Accept a socket only to tell it why it is being closed.

    Args:
        websocket: The incoming browser connection, not yet accepted.
        frame: The refusal to send.
        code: Close code.
        reason: Short ASCII close reason.
    """
    try:
        await websocket.accept()
        await websocket.send_text(json.dumps(frame))
        await websocket.close(code=code, reason=reason)
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass


@router.websocket("/ws")
async def chart_feed(websocket: WebSocket) -> None:
    """Read-only market data socket for the chart.

    The browser connects with no credential. This endpoint checks the upgrade's
    Origin against settings.cors_origins, refuses to carry more than MAX_RELAYS
    sockets at once, then authenticates upstream with the real OpenAlgo key, relays
    subscribe and unsubscribe traffic in both wire forms, and refuses anything
    outside ALLOWED_ACTIONS, the account order stream above all.

    Args:
        websocket: The incoming browser connection.
    """
    origin = websocket.headers.get("origin")
    if not _origin_allowed(origin):
        log.info("openalgo feed: refused a socket from origin %s, not in CORS_ORIGINS",
                 _ascii(origin) if origin is not None else "(absent)")
        # Before accept(); uvicorn turns this into an HTTP 403 on the upgrade.
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="origin not allowed")
        return

    if _relay_slots.locked():
        log.warning("openalgo feed: refused a socket, %s relays already open", MAX_RELAYS)
        await _refuse_after_accept(websocket, _refusal(
            "RELAY_LIMIT",
            f"This backend is already carrying {MAX_RELAYS} market data sockets. "
            "Close another chart and try again."),
            WS_CLOSE_TRY_AGAIN_LATER, "too many feeds")
        return

    # A slot is free and nothing awaits between the check and this acquire, so it
    # cannot block.
    await _relay_slots.acquire()
    try:
        await websocket.accept()
        try:
            await ChartFeedRelay(websocket).run()
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            # Never let a traceback reach the socket. The detail belongs in the log.
            log.exception("openalgo feed: relay failed")
            try:
                await websocket.close(code=WS_CLOSE_UPSTREAM_GONE, reason="relay failed")
            except (WebSocketDisconnect, RuntimeError, OSError):
                pass
    finally:
        _relay_slots.release()
