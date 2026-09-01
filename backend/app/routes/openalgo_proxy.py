"""OpenAlgo proxy for the browser chart: REST candles and the live tick socket.

The chart runs in the browser and the OpenAlgo API key must not. Everything in this
module exists so the page can ask for candles and ticks while the credential stays in
this process.

Facts verified against a live OpenAlgo (zerodha session) on 127.0.0.1:5000 and its
WebSocket proxy on 127.0.0.1:8765, with openalgo 2.0.3 and websockets 16.0:

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

  - WebSocket handshake: {"action":"authenticate","api_key":...} is answered with
    {"type":"auth","status":"success","broker":...,"user_id":...,
    "supported_features":{...}}. The ack carries no credential, so it is relayed on
    to the browser: a client written against the OpenAlgo protocol waits for it
    before it subscribes.

  - The upstream accepts BOTH subscribe forms and this relay normalises away
    neither. The documented one is
    {"action":"subscribe","symbols":[{symbol,exchange}],"mode":"Depth","depth":5};
    the chart library sends
    {"action":"subscribe","symbol":..,"exchange":..,"mode":3,"depth_level":5}.
    server.subscribe_client falls back to data["symbol"]/data["exchange"] when
    "symbols" is absent, and normalize_mode accepts 1/2/3 or LTP/Quote/Depth in any
    case.

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
from pydantic import BaseModel, ConfigDict
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

    symbol: str
    exchange: str
    interval: str
    start_date: str
    end_date: str


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

    query: str
    exchange: str | None = None


class SymbolRequest(BaseModel):
    """One instrument's contract details.

    Attributes:
        symbol: OpenAlgo trading symbol.
        exchange: Exchange code.
    """

    model_config = ConfigDict(extra="ignore")

    symbol: str
    exchange: str


class QuotesRequest(BaseModel):
    """Snapshot quote for one instrument.

    Attributes:
        symbol: OpenAlgo trading symbol.
        exchange: Exchange code.
    """

    model_config = ConfigDict(extra="ignore")

    symbol: str
    exchange: str


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
    """Forward one request off the event loop.

    Args:
        endpoint: Endpoint name with no leading slash.
        payload: Request body, without the api key.

    Returns:
        The response built by _forward.
    """
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
        ws_path, the same-origin path the tick socket is served on, and
        host_reachable, whether OpenAlgo answered a ping just now.
    """
    ping = await asyncio.to_thread(client.ping)
    return {"ws_path": WS_PATH, "host_reachable": bool(ping.get("ok"))}


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

#: The server's default when a frame names no mode at all (server.subscribe_client).
_DEFAULT_MODE = "quote"

MAX_CLIENT_FRAME_BYTES = 64 * 1024
MAX_UPSTREAM_FRAME_BYTES = 8 * 1024 * 1024
#: Matches the server's own per-connection symbol budget (MAX_SYMBOLS_PER_WEBSOCKET).
MAX_TRACKED_SUBSCRIPTIONS = 1000

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
WS_CLOSE_UPSTREAM_GONE = 1011


def _strip_credentials(value: Any) -> Any:
    """Recursively drop credential-looking keys from a client frame.

    Args:
        value: Any decoded JSON value.

    Returns:
        The same structure with every credential key removed at every depth.
    """
    if isinstance(value, dict):
        return {k: _strip_credentials(v) for k, v in value.items()
                if str(k).lower() not in _WS_CREDENTIAL_KEYS}
    if isinstance(value, list):
        return [_strip_credentials(v) for v in value]
    return value


def _refusal(code: str, message: str, request_id: Any = None) -> dict[str, Any]:
    """Build a refusal in the shape the upstream uses for its own errors.

    Args:
        code: Short machine token, for example ORDER_STREAM_FORBIDDEN.
        message: Plain text explaining what was refused and why.
        request_id: Echoed back when the client supplied one.

    Returns:
        A frame ready to send to the browser.
    """
    frame: dict[str, Any] = {"type": "error", "status": "error", "code": code,
                             "message": message}
    if request_id is not None:
        frame["request_id"] = request_id
    return frame


def _instruments_of(frame: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the instruments a subscribe or unsubscribe frame names.

    Handles both wire forms: the documented "symbols" array and the single-symbol
    form the chart library sends.

    Args:
        frame: A screened client frame.

    Returns:
        A list of instrument dicts, possibly empty.
    """
    entries = frame.get("symbols")
    if isinstance(entries, list):
        return [e for e in entries if isinstance(e, dict)]
    if frame.get("symbol") and frame.get("exchange"):
        return [{"symbol": frame["symbol"], "exchange": frame["exchange"]}]
    return []


def _mode_key(frame: dict[str, Any], entry: dict[str, Any]) -> str:
    """Resolve the mode that applies to one instrument.

    An entry's own mode wins, then the top-level mode, then the server's default.
    Mirrors server.subscribe_client so a tracked key matches what was really
    subscribed.

    Args:
        frame: The whole client frame.
        entry: One instrument from that frame.

    Returns:
        A lowercased mode token, for example "3" or "depth".
    """
    mode = entry.get("mode", frame.get("mode", _DEFAULT_MODE))
    return str(mode).strip().lower()


def _sub_key(frame: dict[str, Any], entry: dict[str, Any]) -> tuple[str, str, str]:
    """Canonical identity of one subscription.

    Args:
        frame: The whole client frame.
        entry: One instrument from that frame.

    Returns:
        (exchange, symbol, mode), uppercased where the server is case-insensitive.
    """
    return (str(entry.get("exchange", "")).upper(),
            str(entry.get("symbol", "")).upper(),
            _mode_key(frame, entry))


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
        self._sub_overflow_logged = False

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
            await self._send_browser(_refusal(
                "UPSTREAM_UNAVAILABLE",
                "The OpenAlgo market data feed did not accept a connection."))
            await self._close_browser(WS_CLOSE_UPSTREAM_GONE, "upstream unavailable")
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
                                task.get_name(), type(exc).__name__, exc)
        finally:
            self._closing = True
            await self._close_upstream()
            await self._close_browser(WS_CLOSE_NORMAL, "feed closed")

    async def _open_upstream(self) -> bool:
        """Connect upstream and complete the authentication handshake.

        Nothing is relayed to the browser until the auth ack arrives. The ack itself
        is relayed, because it carries no credential and a client written against the
        OpenAlgo protocol waits for it before subscribing.

        Returns:
            True when an authenticated connection is in place.
        """
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

        ack = await self._authenticate(conn)
        if ack is None:
            await self._quiet_close(conn)
            return False

        self._upstream = conn
        self._ready.set()
        await self._send_browser_text(ack)
        return True

    async def _authenticate(self, conn: ClientConnection) -> str | None:
        """Send the handshake and wait for the ack.

        The key is never logged, at any level.

        Args:
            conn: A freshly opened upstream connection.

        Returns:
            The raw auth ack to relay on, or None when authentication failed.
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
            if not isinstance(message, dict) or message.get("type") != "auth":
                # Nothing is relayed before the ack, so anything else is dropped.
                continue
            if str(message.get("status", "")).lower() != "success":
                log.error("openalgo feed: upstream rejected the api key: %s",
                          message.get("message"))
                return None
            log.info("openalgo feed: authenticated upstream, broker=%s",
                     message.get("broker"))
            return text

    async def _reopen_upstream(self) -> bool:
        """Reconnect upstream on a bounded exponential backoff and replay subs.

        Returns:
            True when a reconnect succeeded within MAX_RECONNECT_ATTEMPTS.
        """
        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            delay = min(RECONNECT_DELAY_MAX_SEC,
                        RECONNECT_DELAY_BASE_SEC * (2 ** (attempt - 1)))
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return False
            if self._closing:
                return False
            log.info("openalgo feed: reconnect attempt %s of %s",
                     attempt, MAX_RECONNECT_ATTEMPTS)
            if await self._open_upstream():
                await self._replay()
                return True
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

            forward, refusal = self._screen(frame)
            if refusal is not None:
                await self._send_browser(refusal)
                continue
            if forward is None:
                continue
            if not await self._forward_upstream(forward):
                return

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
        silently corrected here.

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
                f"Action {action or '(missing)'} is not allowed on the chart feed. "
                f"Allowed: {', '.join(sorted(ALLOWED_ACTIONS))}.", request_id)

        forward = _strip_credentials(frame)
        if action in _SUBSCRIBE_ACTIONS:
            self._remember(forward)
        elif action in _UNSUBSCRIBE_ACTIONS:
            self._forget(forward)
        elif action == "unsubscribe_all":
            self._subs.clear()
        return forward, None

    # -- subscription tracking -------------------------------------------

    def _remember(self, frame: dict[str, Any]) -> None:
        """Record a subscribe frame so it can be replayed after a reconnect.

        Each instrument is stored as its own single-instrument frame, keeping mode,
        depth and depth_level exactly as the client sent them. request_id is dropped
        so a replayed ack cannot be mistaken for an answer to the original request.

        Args:
            frame: A screened subscribe frame.
        """
        entries = _instruments_of(frame)
        if not entries:
            return
        bulk = isinstance(frame.get("symbols"), list)
        for entry in entries:
            if len(self._subs) >= MAX_TRACKED_SUBSCRIPTIONS:
                if not self._sub_overflow_logged:
                    log.warning("openalgo feed: tracking %s subscriptions, further "
                                "ones will not be replayed after a reconnect",
                                MAX_TRACKED_SUBSCRIPTIONS)
                    self._sub_overflow_logged = True
                return
            single = {k: v for k, v in frame.items()
                      if k != "request_id" and not (bulk and k == "symbols")}
            if bulk:
                single["symbols"] = [entry]
            self._subs[_sub_key(frame, entry)] = single

    def _forget(self, frame: dict[str, Any]) -> None:
        """Drop the instruments an unsubscribe frame names from the replay set.

        Args:
            frame: A screened unsubscribe frame.
        """
        for entry in _instruments_of(frame):
            self._subs.pop(_sub_key(frame, entry), None)

    async def _replay(self) -> None:
        """Resend every tracked subscription on a fresh upstream connection."""
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


@router.websocket("/ws")
async def chart_feed(websocket: WebSocket) -> None:
    """Read-only market data socket for the chart.

    The browser connects with no credential. This endpoint authenticates upstream
    with the real OpenAlgo key, relays subscribe and unsubscribe traffic in both wire
    forms, and refuses anything outside ALLOWED_ACTIONS, the account order stream
    above all.

    Args:
        websocket: The incoming browser connection.
    """
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
