"""OpenAlgo proxy tests: REST passthrough, WebSocket relay, and the refusals.

Pure-logic checks run always. Stub checks mount ONLY the proxy router on a spare
port and point it at stand-ins this file runs itself: a WebSocket feed with a knob
for every failure the relay has to survive, and a REST upstream that can time out,
answer HTML, or fail. Live checks then talk to a real OpenAlgo, and are reported as
SKIP when nothing is listening.

The real API key is never read, printed or asserted on by this file. The stub
scenarios swap in a fake key of their own so they can assert that its VALUE never
reaches the browser, which a substring check for "api_key" could not prove.

Run:  python backend/tests/test_openalgo_proxy.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from websockets.asyncio.server import serve as ws_serve  # noqa: E402

from app.openalgo.client import get_client  # noqa: E402
from app.routes import openalgo_proxy  # noqa: E402
from app.routes.openalgo_proxy import (  # noqa: E402
    ALLOWED_ACTIONS,
    ChartFeedRelay,
    HistoryRequest,
    IntervalsRequest,
    MAX_FRAME_DEPTH,
    MAX_RELAYS,
    QuotesRequest,
    SearchRequest,
    SymbolRequest,
    WS_PATH,
    _strip_credentials,
    active_relays,
    router,
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str]] = []

SYMBOL, EXCHANGE, INTERVAL = "RELIANCE", "NSE", "5m"

#: The credential the stub scenarios run under. Its value must never reach a browser.
FAKE_KEY = "stub-key-not-a-real-credential-7f3a"

settings = openalgo_proxy.settings
if not settings.cors_origins:
    # The gate reads this list; an empty one would refuse every browser and make
    # every socket check below fail for a reason unrelated to the relay.
    settings.cors_origins = ["http://localhost:5173"]
ALLOWED_ORIGIN = settings.cors_origins[0]
FOREIGN_ORIGIN = "http://evil.example:9999"


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion.

    Args:
        name: What was being checked.
        condition: The result.
        detail: Optional evidence to print alongside.
    """
    status = PASS if condition else FAIL
    results.append((name, status))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    """Record one skipped assertion.

    Args:
        name: What would have been checked.
        detail: Why it was skipped.
    """
    results.append((name, SKIP))
    print(f"  [{SKIP}] {name}" + (f" - {detail}" if detail else ""))


# --- harness ----------------------------------------------------------------


def free_port() -> int:
    """Pick a port nothing is listening on.

    Returns:
        A port number on the loopback interface.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class AppServer:
    """A uvicorn instance serving one ASGI app on a background thread."""

    def __init__(self, app: FastAPI, port: int, name: str) -> None:
        """Build the server without starting it.

        Args:
            app: The application to serve.
            port: Loopback port to bind.
            name: Thread name, for the log.
        """
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.ws_base = f"ws://127.0.0.1:{port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True, name=name)

    def start(self, timeout: float = 20.0) -> bool:
        """Start serving and wait until the socket is accepting.

        Args:
            timeout: Seconds to wait for startup.

        Returns:
            True once uvicorn reports it has started.
        """
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server.started:
                return True
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        """Ask uvicorn to exit and wait for the thread."""
        self._server.should_exit = True
        self._thread.join(timeout=15)


def proxy_server(port: int) -> AppServer:
    """The proxy router alone, mounted on a throwaway app.

    Args:
        port: Loopback port to bind.

    Returns:
        An unstarted server.
    """
    app = FastAPI(title="openalgo proxy under test")
    app.include_router(router)
    return AppServer(app, port, "proxy-under-test")


class StubRest:
    """Stands in for OpenAlgo's REST API, with a failure mode per magic symbol.

    SLOW sleeps past any short timeout, HTML answers a login page, FAIL503 answers
    a JSON 503, BUSY sleeps briefly and counts how many calls overlap. Anything
    else is answered with one candle and the endpoint name.
    """

    def __init__(self, port: int) -> None:
        """Build the stub app.

        Args:
            port: Loopback port to bind.
        """
        self.received: list[tuple[str, dict[str, Any]]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        app = FastAPI(title="openalgo rest stub")

        @app.post("/api/v1/{endpoint}")
        async def handle(endpoint: str, request: Request) -> Any:
            body = await request.json()
            self.received.append((endpoint, body))
            marker = str(body.get("symbol") or body.get("query") or "")
            if marker == "SLOW":
                await asyncio.sleep(3.0)
            elif marker == "HTML":
                return HTMLResponse("<html><body>login</body></html>")
            elif marker == "FAIL503":
                return JSONResponse({"status": "error",
                                     "message": "stub upstream is down"},
                                    status_code=503)
            elif marker == "BUSY":
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                try:
                    await asyncio.sleep(0.4)
                finally:
                    self.in_flight -= 1
            return JSONResponse({
                "status": "success", "endpoint": endpoint,
                "data": [{"timestamp": 1756700000, "open": 1.0, "high": 2.0,
                          "low": 0.5, "close": 1.5, "volume": 10, "oi": 0}],
            })

        self.server = AppServer(app, port, "rest-stub")


class StubFeed:
    """Stands in for the OpenAlgo WebSocket proxy, on the test's own event loop.

    With every knob off it authenticates anything, acks subscribes and
    unsubscribes, and answers ping with pong. Error frames copy the real server's
    shape exactly: {"status":"error","code":...,"message":...} with no "type".
    """

    def __init__(self, port: int, *, drop_after_first_subscribe: bool = False,
                 refuse_after_session: int | None = None,
                 reject_auth: str | None = None, reject_from_session: int = 0,
                 echo_key_in_rejection: bool = False,
                 delay_auth_on_session: int | None = None,
                 delay_auth_sec: float = 4.0) -> None:
        """Configure the stub.

        Args:
            port: Loopback port to listen on.
            drop_after_first_subscribe: Close the first session with 1011 right
                after acking its first subscribe, to force a reconnect.
            refuse_after_session: Sessions with an index above this are closed at
                once, before any handshake, so every reconnect attempt fails.
            reject_auth: An upstream error code to answer authenticate with. The
                socket stays open afterwards, exactly as the real server's does.
            reject_from_session: The first session index the rejection applies to;
                earlier sessions authenticate normally.
            echo_key_in_rejection: Put the received api_key in the rejection
                message, to prove the relay redacts it on the way to the browser.
            delay_auth_on_session: Index of the session whose auth ack is withheld
                for delay_auth_sec, or until the relay closes the connection.
            delay_auth_sec: How long that ack is withheld.
        """
        self.port = port
        self.drop_after_first_subscribe = drop_after_first_subscribe
        self.refuse_after_session = refuse_after_session
        self.reject_auth = reject_auth
        self.reject_from_session = reject_from_session
        self.echo_key_in_rejection = echo_key_in_rejection
        self.delay_auth_on_session = delay_auth_on_session
        self.delay_auth_sec = delay_auth_sec

        self.sessions: list[list[dict[str, Any]]] = []
        self.close_codes: list[int | None] = []
        self.closed: list[asyncio.Event] = []
        self.auth_pending = asyncio.Event()
        self.closed_during_auth = False
        self._dropped = False

    def serve(self) -> Any:
        """The server context manager.

        Returns:
            An async context manager that listens while it is entered.
        """
        return ws_serve(self._handle, "127.0.0.1", self.port)

    async def _handle(self, conn: Any) -> None:
        """One upstream session.

        Args:
            conn: The server-side connection.
        """
        index = len(self.sessions)
        received: list[dict[str, Any]] = []
        self.sessions.append(received)
        self.close_codes.append(None)
        self.closed.append(asyncio.Event())
        try:
            if self.refuse_after_session is not None and index > self.refuse_after_session:
                await conn.close(code=1011, reason="stub refuses")
                return
            async for raw in conn:
                message = json.loads(raw)
                received.append(message)
                action = message.get("action")
                if action == "authenticate":
                    if self.delay_auth_on_session == index:
                        self.auth_pending.set()
                        try:
                            await asyncio.wait_for(conn.wait_closed(),
                                                   timeout=self.delay_auth_sec)
                        except (TimeoutError, asyncio.TimeoutError):
                            pass
                        else:
                            self.closed_during_auth = True
                            return
                    if self.reject_auth and index >= self.reject_from_session:
                        text = "stub refuses this key"
                        if self.echo_key_in_rejection:
                            text += " " + str(message.get("api_key"))
                        await conn.send(json.dumps({"status": "error",
                                                    "code": self.reject_auth,
                                                    "message": text}))
                        continue
                    await conn.send(json.dumps({"type": "auth", "status": "success",
                                                "broker": "stub"}))
                elif action == "subscribe":
                    await conn.send(json.dumps({
                        "type": "subscribe", "status": "success",
                        "subscriptions": [{"symbol": message.get("symbol"),
                                           "exchange": message.get("exchange"),
                                           "status": "success"}]}))
                    if (self.drop_after_first_subscribe and index == 0
                            and not self._dropped):
                        self._dropped = True
                        await conn.close(code=1011, reason="stub drop")
                        return
                elif action == "unsubscribe":
                    await conn.send(json.dumps({"type": "unsubscribe",
                                                "status": "success"}))
                elif action == "ping":
                    await conn.send(json.dumps({"type": "pong"}))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.close_codes[index] = conn.close_code
            self.closed[index].set()


def browser_connect(url: str, **kwargs: Any) -> Any:
    """Connect the way the charts page does: with the Vite origin.

    Args:
        url: The proxy's WebSocket URL.
        **kwargs: Extra arguments for websockets.connect.

    Returns:
        The connect context manager.
    """
    kwargs.setdefault("origin", ALLOWED_ORIGIN)
    kwargs.setdefault("open_timeout", 10)
    return websockets.connect(url, **kwargs)


def with_stub_feed(name: str, scenario: Callable[..., Any], stub_factory: Callable[[int], StubFeed],
                   server: AppServer, **overrides: Any) -> None:
    """Run one scenario with the relay pointed at a stub feed.

    The relay's settings and any named module constants are swapped for the
    duration and restored afterwards, whatever happens.

    Args:
        name: Scenario name for the failure line.
        scenario: async callable taking (browser_url, stub).
        stub_factory: Builds the StubFeed for a given port.
        server: The proxy under test.
        **overrides: Module constants to patch on openalgo_proxy, by name.
    """
    port = free_port()
    stub = stub_factory(port)
    saved_url, saved_key = settings.openalgo_ws_url, settings.openalgo_api_key
    saved = {key: getattr(openalgo_proxy, key) for key in overrides}
    settings.openalgo_ws_url = f"ws://127.0.0.1:{port}"
    settings.openalgo_api_key = FAKE_KEY
    for key, value in overrides.items():
        setattr(openalgo_proxy, key, value)

    async def runner() -> None:
        async with stub.serve():
            await scenario(f"{server.ws_base}{WS_PATH}", stub)

    try:
        asyncio.run(runner())
    except Exception as exc:  # noqa: BLE001
        check(f"{name} ran without raising", False, f"{type(exc).__name__}: {exc}")
    finally:
        settings.openalgo_ws_url = saved_url
        settings.openalgo_api_key = saved_key
        for key, value in saved.items():
            setattr(openalgo_proxy, key, value)


def openalgo_reachable() -> bool:
    """Whether the OpenAlgo REST host answers a ping.

    Returns:
        True when OpenAlgo is up.
    """
    try:
        return bool(get_client().ping().get("ok"))
    except Exception:  # noqa: BLE001
        return False


def date_range(days: int = 10) -> tuple[str, str]:
    """An IST calendar range wide enough to contain a trading session.

    Args:
        days: How many calendar days back to start.

    Returns:
        (start_date, end_date) as YYYY-MM-DD.
    """
    today = dt.date.today()
    return (today - dt.timedelta(days=days)).isoformat(), today.isoformat()


async def _collect(ws: Any, seconds: float, limit: int = 40) -> list[dict[str, Any]]:
    """Read frames until the budget runs out or the socket closes.

    Args:
        ws: An open client connection.
        seconds: How long to keep reading.
        limit: Stop after this many frames.

    Returns:
        The decoded frames, in arrival order.
    """
    frames: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + seconds
    while len(frames) < limit:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except (TimeoutError, asyncio.TimeoutError):
            break
        except websockets.ConnectionClosed:
            break
        try:
            frames.append(json.loads(raw))
        except ValueError:
            continue
    return frames


async def _first(ws: Any, timeout: float = 3.0) -> dict[str, Any] | None:
    """The next decoded frame, or None when nothing arrives in time.

    Args:
        ws: An open client connection.
        timeout: Seconds to wait.

    Returns:
        The frame, or None on timeout or close.
    """
    frames = await _collect(ws, timeout, limit=1)
    return frames[0] if frames else None


async def _wait_until(predicate: Callable[[], bool], seconds: float) -> bool:
    """Poll a condition.

    Args:
        predicate: Evaluated every 50ms.
        seconds: How long to keep polling.

    Returns:
        True as soon as the predicate holds, False when time runs out.
    """
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


def _no_key_leak(frames: list[dict[str, Any]]) -> bool:
    """Whether the fake key's value is absent from every frame.

    Args:
        frames: Frames the browser received.

    Returns:
        True when the value appears nowhere.
    """
    return all(FAKE_KEY not in json.dumps(f) for f in frames)


# --- pure logic -------------------------------------------------------------


def test_request_models() -> None:
    """A client-supplied credential must never survive into the forwarded body."""
    print("\n=== request models ===")
    req = HistoryRequest(symbol="RELIANCE", exchange="NSE", interval="5m",
                         start_date="2026-08-25", end_date="2026-09-01",
                         apikey="client-supplied-key")
    dumped = req.model_dump()
    check("history model drops an apikey the client sent",
          "apikey" not in dumped and "api_key" not in dumped, str(sorted(dumped)))

    quote = QuotesRequest(symbol="RELIANCE", exchange="NSE", api_key="nope")
    check("quotes model drops an api_key the client sent",
          "api_key" not in quote.model_dump())

    search = SearchRequest(query="REL", exchange="NSE", api_key="nope", apikey="nope")
    check("search model drops both credential spellings",
          not ({"api_key", "apikey"} & set(search.model_dump())))
    sym = SymbolRequest(symbol="RELIANCE", exchange="NSE", secret="nope", auth="nope")
    check("symbol model drops secret and auth", not ({"secret", "auth"} & set(sym.model_dump())))
    empty = IntervalsRequest(apikey="nope", api_key="nope")
    check("intervals model drops everything", empty.model_dump() == {})

    def rejects(model: Any, **fields: Any) -> bool:
        try:
            model(**fields)
        except ValidationError:
            return True
        return False

    base = {"symbol": "RELIANCE", "exchange": "NSE", "interval": "5m",
            "start_date": "2026-08-25", "end_date": "2026-09-01"}
    check("history refuses a 65 character symbol",
          rejects(HistoryRequest, **{**base, "symbol": "X" * 65}))
    check("history accepts a 64 character symbol",
          not rejects(HistoryRequest, **{**base, "symbol": "X" * 64}))
    check("history refuses a 17 character exchange",
          rejects(HistoryRequest, **{**base, "exchange": "X" * 17}))
    check("history refuses a 9 character interval",
          rejects(HistoryRequest, **{**base, "interval": "X" * 9}))
    check("history refuses an 11 character date",
          rejects(HistoryRequest, **{**base, "start_date": "2026-08-25T"}))
    check("search refuses a 129 character query", rejects(SearchRequest, query="q" * 129))
    check("search refuses a 17 character exchange",
          rejects(SearchRequest, query="q", exchange="X" * 17))
    check("quotes refuses a 65 character symbol",
          rejects(QuotesRequest, symbol="X" * 65, exchange="NSE"))
    check("symbol refuses a 17 character exchange",
          rejects(SymbolRequest, symbol="X", exchange="X" * 17))


def test_strip_credentials() -> None:
    """Credential keys go at every depth; instrument tokens stay; depth is bounded."""
    print("\n=== credential stripping ===")
    frame = {
        "action": "subscribe",
        "api_key": "leaked",
        "apikey": "leaked",
        "symbols": [{"symbol": "SBIN", "exchange": "NSE", "api_key": "leaked",
                     "token": "3045"}],
        "mode": 3,
    }
    cleaned = _strip_credentials(frame)
    text = json.dumps(cleaned)
    check("top level api_key removed", "api_key" not in cleaned)
    check("top level apikey removed", "apikey" not in cleaned)
    check("nested api_key removed", "leaked" not in text, text)
    check("instrument token survives", cleaned["symbols"][0]["token"] == "3045")
    check("mode and action survive",
          cleaned["mode"] == 3 and cleaned["action"] == "subscribe")

    def nested(depth: int) -> Any:
        value: Any = "leaf"
        for _ in range(depth):
            value = [value]
        return value

    check(f"a structure {MAX_FRAME_DEPTH} deep is accepted",
          _strip_credentials(nested(MAX_FRAME_DEPTH)) is not None)
    for depth in (MAX_FRAME_DEPTH + 8, 3000):
        try:
            _strip_credentials({"action": "subscribe", "junk": nested(depth)})
            outcome = "accepted"
        except ValueError:
            outcome = "ValueError"
        except RecursionError:
            outcome = "RecursionError"
        check(f"a structure {depth} deep is refused with ValueError, not RecursionError",
              outcome == "ValueError", outcome)


def test_screening() -> None:
    """The allowlist, the order-stream refusal, and the shapes of a bad frame."""
    print("\n=== frame screening ===")
    relay = ChartFeedRelay(None)  # type: ignore[arg-type]

    forward, refusal = relay._screen({"action": "subscribe", "symbol": "RELIANCE",
                                      "exchange": "NSE", "mode": 3,
                                      "depth_level": 5})
    check("subscribe is allowed", refusal is None and forward is not None)
    check("subscribe is forwarded unchanged (single-symbol form kept)",
          forward == {"action": "subscribe", "symbol": "RELIANCE",
                      "exchange": "NSE", "mode": 3, "depth_level": 5},
          json.dumps(forward))

    forward, refusal = relay._screen({"action": "subscribe_orders"})
    check("subscribe_orders is refused",
          forward is None and refusal is not None
          and refusal["code"] == "ORDER_STREAM_FORBIDDEN")

    forward, refusal = relay._screen({"action": "SUBSCRIBE_ORDERS"})
    check("subscribe_orders is refused whatever the case",
          forward is None and refusal is not None
          and refusal["code"] == "ORDER_STREAM_FORBIDDEN")

    forward, refusal = relay._screen({"action": "unsubscribe_orders"})
    check("unsubscribe_orders is refused too", forward is None and refusal is not None)

    for action in ("get_broker_info", "get_supported_brokers", "authenticate",
                   "placeorder", ""):
        forward, refusal = relay._screen({"action": action})
        check(f"action {action or '(missing)'} is refused",
              forward is None and refusal is not None
              and refusal["code"] == "ACTION_NOT_ALLOWED")

    for action in sorted(ALLOWED_ACTIONS):
        forward, refusal = relay._screen({"action": action})
        check(f"action {action} is allowed", refusal is None and forward is not None)

    forward, refusal = relay._screen({"action": "subscribe", "symbol": "X",
                                      "exchange": "NSE", "request_id": "req-1",
                                      "api_key": "leaked"})
    check("api_key is stripped from a subscribe on the way upstream",
          "api_key" not in (forward or {}))
    check("request_id survives on the forwarded frame",
          (forward or {}).get("request_id") == "req-1")

    # The upstream honours "type" as a fallback for "action"; the relay must not.
    forward, refusal = relay._screen({"type": "subscribe_orders"})
    check("a type-keyed frame with no action is refused",
          forward is None and refusal is not None
          and refusal["code"] == "ACTION_NOT_ALLOWED")
    forward, refusal = relay._screen({"type": "subscribe", "symbol": "X",
                                      "exchange": "NSE"})
    check("a type-keyed subscribe with no action is refused too",
          forward is None and refusal is not None)

    for bad in (5, 0, ["subscribe"], {"x": "subscribe"}, None, True, 3.5):
        forward, refusal = relay._screen({"action": bad})
        check(f"a non-string action {json.dumps(bad)} is refused",
              forward is None and refusal is not None
              and refusal["code"] == "ACTION_NOT_ALLOWED")

    dup = json.loads('{"action": "ping", "action": "subscribe_orders"}')
    forward, refusal = relay._screen(dup)
    check("with duplicate keys the last action wins and is screened",
          forward is None and refusal is not None
          and refusal["code"] == "ORDER_STREAM_FORBIDDEN")
    dup = json.loads('{"action": "subscribe_orders", "action": "ping"}')
    forward, refusal = relay._screen(dup)
    check("with duplicate keys the forwarded frame carries one action only",
          refusal is None and forward == {"action": "ping"}, json.dumps(forward))

    deep: Any = "leaf"
    for _ in range(MAX_FRAME_DEPTH + 8):
        deep = [deep]
    forward, refusal = relay._screen({"action": "subscribe", "symbol": "X",
                                      "exchange": "NSE", "junk": deep})
    check("a frame nested past the depth bound is refused as INVALID_JSON",
          forward is None and refusal is not None and refusal["code"] == "INVALID_JSON",
          json.dumps(refusal))


def test_replay_set() -> None:
    """Tracked subscriptions, in both wire forms, and what removes them."""
    print("\n=== subscription tracking ===")
    relay = ChartFeedRelay(None)  # type: ignore[arg-type]

    relay._screen({"action": "subscribe", "symbol": "RELIANCE", "exchange": "NSE",
                   "mode": 3, "depth_level": 5, "request_id": "req-1"})
    relay._screen({"action": "subscribe", "symbols": [{"symbol": "SBIN",
                                                       "exchange": "NSE"},
                                                      {"symbol": "TCS",
                                                       "exchange": "NSE"}],
                   "mode": "LTP"})
    check("three instruments tracked from two frames", len(relay._subs) == 3,
          str(sorted(relay._subs)))

    stored = relay._subs[("NSE", "RELIANCE", "3")]
    check("replay frame keeps mode and depth_level verbatim",
          stored["mode"] == 3 and stored["depth_level"] == 5, json.dumps(stored))
    check("replay frame drops request_id", "request_id" not in stored)

    bulk = relay._subs[("NSE", "SBIN", "1")]
    check("a bulk subscribe is split into single-instrument replay frames",
          bulk == {"action": "subscribe", "symbol": "SBIN", "exchange": "NSE",
                   "mode": "LTP"},
          json.dumps(bulk))

    relay._screen({"action": "unsubscribe", "symbols": [{"symbol": "SBIN",
                                                         "exchange": "NSE",
                                                         "mode": "LTP"}]})
    check("unsubscribe drops just that instrument", len(relay._subs) == 2,
          str(sorted(relay._subs)))

    relay._screen({"action": "unsubscribe_all"})
    check("unsubscribe_all clears the replay set", not relay._subs)

    # Mode identity is the canonical number, however either side spelled it.
    relay._screen({"action": "subscribe", "symbol": "RELIANCE", "exchange": "NSE",
                   "mode": 3})
    relay._screen({"action": "unsubscribe", "symbol": "RELIANCE", "exchange": "NSE",
                   "mode": "Depth"})
    check("subscribe as 3, unsubscribe as Depth: the instrument is forgotten",
          not relay._subs, str(sorted(relay._subs)))
    relay._screen({"action": "subscribe", "symbol": "RELIANCE", "exchange": "NSE",
                   "mode": "ltp"})
    relay._screen({"action": "unsubscribe", "symbol": "RELIANCE", "exchange": "NSE",
                   "mode": 1})
    check("subscribe as ltp, unsubscribe as 1: the instrument is forgotten",
          not relay._subs, str(sorted(relay._subs)))
    relay._screen({"action": "subscribe", "symbol": "RELIANCE", "exchange": "NSE"})
    check("no mode means Quote, the server's default, keyed as 2",
          ("NSE", "RELIANCE", "2") in relay._subs, str(sorted(relay._subs)))
    relay._screen({"action": "unsubscribe", "symbol": "RELIANCE", "exchange": "NSE",
                   "mode": "QUOTE"})
    check("unsubscribe as QUOTE removes the default-mode subscription",
          not relay._subs)

    # The server reads a subscribe's mode from the top level only.
    relay._screen({"action": "subscribe",
                   "symbols": [{"symbol": "SBIN", "exchange": "NSE", "mode": "LTP"}],
                   "mode": "Depth"})
    check("a per-entry mode on subscribe is ignored, as the server ignores it",
          ("NSE", "SBIN", "3") in relay._subs and ("NSE", "SBIN", "1") not in relay._subs,
          str(sorted(relay._subs)))
    relay._screen({"action": "unsubscribe",
                   "symbols": [{"symbol": "SBIN", "exchange": "NSE", "mode": 3}]})
    check("a per-entry mode on unsubscribe wins, as it does on the server",
          not relay._subs)

    for mode in ("3", 3.0, True, "full"):
        relay._screen({"action": "subscribe", "symbol": "X", "exchange": "NSE",
                       "mode": mode})
    check("a mode the server would refuse is not tracked at all", not relay._subs,
          str(sorted(relay._subs)))

    # Only the fields a replay needs are kept.
    relay._screen({"action": "subscribe", "symbol": "TCS", "exchange": "NSE",
                   "mode": 2, "depth": 20, "junk": "x" * 1000, "nested": {"a": 1},
                   "request_id": 7})
    kept = relay._subs[("NSE", "TCS", "2")]
    check("a replay frame stores only action, symbol, exchange, mode and depth",
          set(kept) == {"action", "symbol", "exchange", "mode", "depth"}
          and kept["depth"] == 20, json.dumps(kept))
    relay._screen({"action": "subscribe", "symbol": "INFY", "exchange": "NSE",
                   "mode": 2, "depth": "x" * 500})
    check("an oversized depth value is not stored",
          "depth" not in relay._subs[("NSE", "INFY", "2")])
    relay._screen({"action": "unsubscribe_all"})

    # The cap refuses, rather than forwarding what could never be replayed.
    saved_cap = openalgo_proxy.MAX_TRACKED_SUBSCRIPTIONS
    openalgo_proxy.MAX_TRACKED_SUBSCRIPTIONS = 3
    try:
        for name in ("A", "B", "C"):
            relay._screen({"action": "subscribe", "symbol": name, "exchange": "NSE"})
        forward, refusal = relay._screen({"action": "subscribe", "symbol": "D",
                                          "exchange": "NSE", "request_id": "r4"})
        check("a subscribe past the cap is refused, not forwarded",
              forward is None and refusal is not None
              and refusal["code"] == "SUBSCRIPTION_LIMIT"
              and refusal.get("request_id") == "r4", json.dumps(refusal))
        forward, refusal = relay._screen({"action": "subscribe", "symbol": "A",
                                          "exchange": "NSE"})
        check("re-subscribing a tracked instrument still goes through at the cap",
              refusal is None and forward is not None)
        forward, refusal = relay._screen({"action": "subscribe",
                                          "symbols": [{"symbol": "A", "exchange": "NSE"},
                                                      {"symbol": "D", "exchange": "NSE"}]})
        check("a bulk frame that does not fit is refused whole",
              forward is None and refusal is not None and len(relay._subs) == 3)
        relay._screen({"action": "unsubscribe", "symbol": "A", "exchange": "NSE"})
        forward, refusal = relay._screen({"action": "subscribe", "symbol": "D",
                                          "exchange": "NSE"})
        check("after an unsubscribe there is room again",
              refusal is None and ("NSE", "D", "2") in relay._subs)
    finally:
        openalgo_proxy.MAX_TRACKED_SUBSCRIPTIONS = saved_cap


class GatedConn:
    """A fake upstream whose first send blocks until released."""

    def __init__(self) -> None:
        """Start with nothing sent and the gate closed."""
        self.sent: list[dict[str, Any]] = []
        self.gate = asyncio.Event()
        self.first_send_started = asyncio.Event()

    async def send(self, text: str) -> None:
        """Record a frame, blocking on the first one.

        Args:
            text: The serialised frame.
        """
        self.sent.append(json.loads(text))
        if len(self.sent) == 1:
            self.first_send_started.set()
            await self.gate.wait()


async def _replay_lock_scenario() -> None:
    """An unsubscribe arriving mid-replay must land after the replay, not inside it."""
    relay = ChartFeedRelay(None)  # type: ignore[arg-type]
    relay._screen({"action": "subscribe", "symbol": "RELIANCE", "exchange": "NSE",
                   "mode": 3})
    relay._screen({"action": "subscribe", "symbol": "SBIN", "exchange": "NSE",
                   "mode": 3})
    conn = GatedConn()
    relay._upstream = conn  # type: ignore[assignment]
    relay._ready.set()

    replay = asyncio.create_task(relay._replay())
    await asyncio.wait_for(conn.first_send_started.wait(), timeout=5)
    unsub = asyncio.create_task(relay._handle_client_frame(
        {"action": "unsubscribe", "symbol": "RELIANCE", "exchange": "NSE", "mode": 3}))
    await asyncio.sleep(0.1)
    check("a client frame waits while a replay is in progress",
          not unsub.done() and len(conn.sent) == 1, f"{len(conn.sent)} sent")
    conn.gate.set()
    await asyncio.wait_for(asyncio.gather(replay, unsub), timeout=5)
    order = [(f["action"], f["symbol"]) for f in conn.sent]
    check("the whole replay lands before the unsubscribe",
          order == [("subscribe", "RELIANCE"), ("subscribe", "SBIN"),
                    ("unsubscribe", "RELIANCE")], str(order))
    check("the replay set no longer holds the unsubscribed instrument",
          ("NSE", "RELIANCE", "3") not in relay._subs)


def test_replay_lock() -> None:
    """The replay-versus-unsubscribe race, on fakes."""
    print("\n=== replay lock ===")
    try:
        asyncio.run(_replay_lock_scenario())
    except Exception as exc:  # noqa: BLE001
        check("the replay lock scenario ran without raising", False,
              f"{type(exc).__name__}: {exc}")


# --- stub feed scenarios (no broker needed) -----------------------------------


async def _reconnect_scenario(browser_url: str, stub: StubFeed) -> None:
    """Drop the upstream mid-session and prove the relay heals it invisibly.

    The browser's own socket never moves, so a proxy-side reconnect that did not
    replay subscriptions would leave the chart open and permanently silent. That is
    the failure this scenario exists to catch.

    Args:
        browser_url: The proxy's own WebSocket URL.
        stub: The stub feed, configured to drop its first session once.
    """
    async with browser_connect(browser_url) as ws:
        first_auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        check("the stub handshake is relayed to the browser",
              first_auth.get("type") == "auth", json.dumps(first_auth))

        await ws.send(json.dumps({"action": "subscribe", "symbol": SYMBOL,
                                  "exchange": EXCHANGE, "mode": 3,
                                  "depth_level": 5}))
        frames = await _collect(ws, seconds=4.0)
        kinds = [f.get("type") for f in frames]
        check("the browser is told the upstream is reconnecting",
              any(f.get("type") == "proxy"
                  and f.get("status") == "reconnecting" for f in frames),
              str(kinds))
        check("a second auth ack reaches the browser after the reconnect",
              sum(1 for f in frames if f.get("type") == "auth") >= 1, str(kinds))
        check("the browser socket survived the upstream drop",
              ws.state is websockets.protocol.State.OPEN, str(ws.state))
        check("no frame the browser received carried the fake key's value",
              _no_key_leak([first_auth, *frames]))

    check("the stub saw two upstream sessions", len(stub.sessions) == 2,
          f"{len(stub.sessions)} session(s)")
    if len(stub.sessions) == 2:
        replayed = [m for m in stub.sessions[1] if m.get("action") == "subscribe"]
        check("the second session re-authenticated",
              any(m.get("action") == "authenticate" for m in stub.sessions[1]))
        # Only the subscribe frames are printed. The authenticate frame carries a
        # credential field and never belongs in test output, stub or not.
        check("the subscription was replayed on the new upstream session",
              bool(replayed), json.dumps(replayed))
        if replayed:
            check("the replayed frame kept mode and depth_level",
                  replayed[0].get("mode") == 3
                  and replayed[0].get("depth_level") == 5,
                  json.dumps(replayed[0]))
            check("the replay carried no api_key",
                  "api_key" not in replayed[0] and "apikey" not in replayed[0])


async def _origin_gate_scenario(browser_url: str, stub: StubFeed) -> None:
    """A foreign or absent Origin is refused at the handshake; the page's is not.

    Args:
        browser_url: The proxy's own WebSocket URL.
        stub: A plain stub feed.
    """

    async def attempt(**kwargs: Any) -> tuple[str, int | None]:
        try:
            async with websockets.connect(browser_url, open_timeout=10, **kwargs) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)
                return "connected", None
        except websockets.exceptions.InvalidHandshake as exc:
            response = getattr(exc, "response", None)
            return type(exc).__name__, getattr(response, "status_code", None)

    outcome, status = await attempt(origin=FOREIGN_ORIGIN)
    check("a foreign Origin is refused at the handshake with 403",
          outcome != "connected" and status == 403, f"{outcome} {status}")
    outcome, status = await attempt()
    check("an absent Origin is refused at the handshake with 403",
          outcome != "connected" and status == 403, f"{outcome} {status}")
    check("the refused upgrades never reached the upstream", len(stub.sessions) == 0,
          f"{len(stub.sessions)} session(s)")
    outcome, _ = await attempt(origin=ALLOWED_ORIGIN)
    check(f"the allowed origin {ALLOWED_ORIGIN} connects and gets the auth ack",
          outcome == "connected", outcome)


async def _auth_rejected_at_open_scenario(browser_url: str, stub: StubFeed) -> None:
    """The first handshake is refused upstream: a precise frame, no key, then close.

    Args:
        browser_url: The proxy's own WebSocket URL.
        stub: A stub answering authenticate with AUTHENTICATION_ERROR that echoes
            the key it was given.
    """
    async with browser_connect(browser_url) as ws:
        frame = await _first(ws, timeout=8)
        check("an upstream auth rejection reaches the browser as UPSTREAM_AUTH_REJECTED",
              frame is not None and frame.get("code") == "UPSTREAM_AUTH_REJECTED",
              json.dumps(frame))
        check("the frame carries the upstream's own code",
              (frame or {}).get("upstream_code") == "AUTHENTICATION_ERROR")
        check("the upstream's message is forwarded with the key redacted",
              frame is not None and "[redacted]" in json.dumps(frame)
              and FAKE_KEY not in json.dumps(frame), json.dumps(frame))
        try:
            await asyncio.wait_for(ws.recv(), timeout=5)
            closed = None
        except websockets.ConnectionClosed as exc:
            closed = exc.rcvd.code if exc.rcvd else None
        check("the browser socket is then closed with 1011", closed == 1011, str(closed))
    check("the relay closed the refused upstream session itself",
          await _wait_until(lambda: stub.close_codes and stub.close_codes[0] is not None, 5),
          str(stub.close_codes))


def _auth_rejected_on_reconnect_scenario(code: str) -> Callable[..., Any]:
    """Build the reconnect-path rejection scenario for one upstream code.

    Args:
        code: The upstream error code the stub answers with from its second session.

    Returns:
        The scenario coroutine function.
    """

    async def scenario(browser_url: str, stub: StubFeed) -> None:
        async with browser_connect(browser_url) as ws:
            await asyncio.wait_for(ws.recv(), timeout=10)
            await ws.send(json.dumps({"action": "subscribe", "symbol": SYMBOL,
                                      "exchange": EXCHANGE, "mode": 3}))
            frames = await _collect(ws, seconds=6.0)
        rejections = [f for f in frames if f.get("code") == "UPSTREAM_AUTH_REJECTED"]
        unavailable = [f for f in frames if f.get("code") == "UPSTREAM_UNAVAILABLE"]
        attempts = len(stub.sessions) - 1
        check(f"{code} on reconnect is reported to the browser as UPSTREAM_AUTH_REJECTED",
              bool(rejections) and rejections[0].get("upstream_code") == code,
              json.dumps(rejections[:1]))
        check(f"{code}: the misleading UPSTREAM_UNAVAILABLE is not sent on top",
              not unavailable)
        if code == "AUTHENTICATION_ERROR":
            check("AUTHENTICATION_ERROR is not retried", attempts == 1,
                  f"{attempts} reconnect attempt(s)")
        else:
            check(f"{code} is retried up to MAX_RECONNECT_ATTEMPTS",
                  attempts == openalgo_proxy.MAX_RECONNECT_ATTEMPTS,
                  f"{attempts} reconnect attempt(s)")
        check(f"{code}: no frame carried the fake key's value", _no_key_leak(frames))
        check(f"{code}: every refused upstream session was closed by the relay",
              await _wait_until(lambda: all(c is not None for c in stub.close_codes), 5),
              str(stub.close_codes))

    return scenario


async def _steady_disconnect_scenario(browser_url: str, stub: StubFeed) -> None:
    """The browser leaves in steady state: upstream closed, both pumps gone.

    Args:
        browser_url: The proxy's own WebSocket URL.
        stub: A plain stub feed.
    """
    async with browser_connect(browser_url) as ws:
        await asyncio.wait_for(ws.recv(), timeout=10)
        await ws.send(json.dumps({"action": "subscribe", "symbol": SYMBOL,
                                  "exchange": EXCHANGE, "mode": 3}))
        await _first(ws)
    closed = await _wait_until(lambda: stub.closed and stub.closed[0].is_set(), 5)
    check("when the browser leaves, the upstream session receives a close frame",
          closed and stub.close_codes[0] == 1000, str(stub.close_codes))
    check("both pump tasks ended and the relay slot was released",
          await _wait_until(lambda: active_relays() == 0, 5), f"{active_relays()} open")


async def _midreconnect_disconnect_scenario(browser_url: str, stub: StubFeed) -> None:
    """The browser leaves while a reconnect is mid-handshake: no leaked session.

    Args:
        browser_url: The proxy's own WebSocket URL.
        stub: A stub that drops its first session and withholds the second's ack.
    """
    async with browser_connect(browser_url) as ws:
        await asyncio.wait_for(ws.recv(), timeout=10)
        await ws.send(json.dumps({"action": "subscribe", "symbol": SYMBOL,
                                  "exchange": EXCHANGE, "mode": 3}))
        pending = await _wait_until(stub.auth_pending.is_set, 8)
        check("the relay reached the second session's handshake", pending)
        # Leave now, while _authenticate is waiting on a reply that will not come.
    check("the half-authenticated upstream session was closed, not leaked",
          await _wait_until(lambda: stub.closed_during_auth, 6),
          f"closed_during_auth={stub.closed_during_auth} codes={stub.close_codes}")
    check("it received a proper close frame",
          len(stub.close_codes) >= 2 and stub.close_codes[1] == 1000, str(stub.close_codes))
    check("the relay slot was released after the mid-reconnect disconnect",
          await _wait_until(lambda: active_relays() == 0, 5), f"{active_relays()} open")


async def _exhaustion_scenario(browser_url: str, stub: StubFeed) -> None:
    """The upstream stays down: bounded attempts, UPSTREAM_UNAVAILABLE, relay closed.

    Args:
        browser_url: The proxy's own WebSocket URL.
        stub: A stub that drops its first session and refuses every later one.
    """
    async with browser_connect(browser_url) as ws:
        await asyncio.wait_for(ws.recv(), timeout=10)
        await ws.send(json.dumps({"action": "subscribe", "symbol": SYMBOL,
                                  "exchange": EXCHANGE, "mode": 3}))
        frames = await _collect(ws, seconds=8.0)
        state = ws.state
    kinds = [f.get("code") or f.get("type") for f in frames]
    check("the browser was told the relay was reconnecting",
          any(f.get("status") == "reconnecting" for f in frames), str(kinds))
    check("UPSTREAM_UNAVAILABLE is sent when every attempt fails",
          any(f.get("code") == "UPSTREAM_UNAVAILABLE" for f in frames), str(kinds))
    check("the browser socket is closed afterwards",
          state is websockets.protocol.State.CLOSED, str(state))
    attempts = len(stub.sessions) - 1
    check("MAX_RECONNECT_ATTEMPTS is honoured",
          attempts == openalgo_proxy.MAX_RECONNECT_ATTEMPTS,
          f"{attempts} attempt(s), limit {openalgo_proxy.MAX_RECONNECT_ATTEMPTS}")
    check("the relay slot was released after giving up",
          await _wait_until(lambda: active_relays() == 0, 5), f"{active_relays()} open")


async def _frame_edge_cases_scenario(browser_url: str, stub: StubFeed) -> None:
    """Every malformed frame is refused and the relay survives each one.

    Args:
        browser_url: The proxy's own WebSocket URL.
        stub: A plain stub feed.
    """
    seen: list[dict[str, Any]] = []

    async def send_expect(ws: Any, payload: Any, code: str | None, label: str) -> None:
        await ws.send(payload)
        frame = await _first(ws, timeout=3)
        if frame is not None:
            seen.append(frame)
        got = (frame or {}).get("code") or (frame or {}).get("type")
        check(label, got == code, f"got {got}")

    async with browser_connect(browser_url) as ws:
        seen.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=10)))

        big = json.dumps({"action": "ping", "pad": "x" * (64 * 1024)})
        await send_expect(ws, big, "FRAME_TOO_LARGE", "an oversized frame is refused")
        await send_expect(ws, b"\xff\xfe{not json", "INVALID_JSON",
                          "a binary frame that is not JSON is refused")
        await send_expect(ws, b'{"action": "ping"}', "pong",
                          "a binary frame carrying JSON is decoded and forwarded")
        # On Python 3.14 json.loads parses 3000 nested arrays without raising, so a
        # top-level array of that depth is refused for not being an object. Wrapped
        # in an object it reaches the credential strip, and the depth bound is the
        # only thing between it and a RecursionError that would end the pump.
        await ws.send("[" * 3000 + "]" * 3000)
        frame = await _first(ws, timeout=3)
        if frame is not None:
            seen.append(frame)
        check("3000 nested arrays are refused, not crashed",
              (frame or {}).get("code") in ("INVALID_JSON", "INVALID_FRAME"),
              json.dumps(frame))
        for depth in (200, 3000):
            deep = '{"action": "subscribe", "symbol": "X", "exchange": "NSE", "junk": ' \
                   + "[" * depth + "]" * depth + "}"
            await send_expect(ws, deep, "INVALID_JSON",
                              f"{depth} nested arrays inside an object are refused as "
                              "INVALID_JSON")
        await send_expect(ws, json.dumps({"type": "subscribe_orders"}), "ACTION_NOT_ALLOWED",
                          "a type-keyed order stream request is refused")
        await send_expect(ws, json.dumps({"type": "subscribe", "symbol": SYMBOL,
                                          "exchange": EXCHANGE}), "ACTION_NOT_ALLOWED",
                          "a type-keyed subscribe with no action is refused")
        await send_expect(ws, json.dumps({"action": 5}), "ACTION_NOT_ALLOWED",
                          "a numeric action is refused")
        await send_expect(ws, json.dumps({"action": ["subscribe"]}), "ACTION_NOT_ALLOWED",
                          "an array action is refused")
        await send_expect(ws, json.dumps(["subscribe"]), "INVALID_FRAME",
                          "a top-level JSON array is refused")
        await send_expect(ws, '{"action": "ping", "action": "subscribe_orders"}',
                          "ORDER_STREAM_FORBIDDEN",
                          "duplicate keys: the last action is the one screened")
        await send_expect(ws, '{"action": "subscribe_orders", "action": "ping"}', "pong",
                          "duplicate keys: the surviving action is the one forwarded")
        await ws.send(json.dumps({"action": "pong"}))
        quiet = await _collect(ws, seconds=0.5)
        seen.extend(quiet)
        check("pong is forwarded and produces no answer", not quiet, str(quiet))
        await send_expect(ws, json.dumps({"action": "ping"}), "pong",
                          "the relay still answers after every bad frame")

    upstream = stub.sessions[0] if stub.sessions else []
    check("nothing that was refused reached the upstream",
          not any(m.get("action") == "subscribe_orders" or m.get("type") in
                  ("subscribe_orders", "subscribe") or m.get("action") == "subscribe"
                  for m in upstream),
          str([m.get("action") for m in upstream]))
    check("the upstream saw the pong", any(m.get("action") == "pong" for m in upstream))
    check("no frame the browser received carried the fake key's value", _no_key_leak(seen))


async def _relay_cap_scenario(browser_url: str, stub: StubFeed) -> None:
    """The ninth socket is refused with RELAY_LIMIT and 1013; a freed slot reopens.

    Args:
        browser_url: The proxy's own WebSocket URL.
        stub: A plain stub feed.
    """
    check("no relay is open before the cap scenario",
          await _wait_until(lambda: active_relays() == 0, 5), f"{active_relays()} open")
    held: list[Any] = []
    try:
        for _ in range(MAX_RELAYS):
            ws = await browser_connect(browser_url)
            await asyncio.wait_for(ws.recv(), timeout=10)
            held.append(ws)
        check(f"{MAX_RELAYS} relays are open", active_relays() == MAX_RELAYS,
              f"{active_relays()} open")

        async with browser_connect(browser_url) as extra:
            frame = await _first(extra, timeout=5)
            check("the next socket is refused with RELAY_LIMIT",
                  frame is not None and frame.get("code") == "RELAY_LIMIT", json.dumps(frame))
            try:
                await asyncio.wait_for(extra.recv(), timeout=5)
                closed = None
            except websockets.ConnectionClosed as exc:
                closed = exc.rcvd.code if exc.rcvd else None
            check("and closed with 1013 (try again later)", closed == 1013, str(closed))
        check("the refused socket opened no upstream session",
              len(stub.sessions) == MAX_RELAYS, f"{len(stub.sessions)} session(s)")

        await held.pop().close()
        freed = await _wait_until(lambda: active_relays() == MAX_RELAYS - 1, 5)
        check("closing one socket frees its slot", freed, f"{active_relays()} open")
        async with browser_connect(browser_url) as again:
            ack = await _first(again, timeout=5)
            check("a new socket then connects", ack is not None and ack.get("type") == "auth",
                  json.dumps(ack))
    finally:
        for ws in held:
            await ws.close()
    check("all slots are released at the end",
          await _wait_until(lambda: active_relays() == 0, 5), f"{active_relays()} open")


def test_stub_feed(server: AppServer) -> None:
    """Every relay scenario that needs no broker."""
    fast = {"RECONNECT_DELAY_BASE_SEC": 0.05, "RECONNECT_DELAY_MAX_SEC": 0.1}

    print("\n=== upstream drop, reconnect and replay ===")
    with_stub_feed("reconnect", _reconnect_scenario,
                   lambda port: StubFeed(port, drop_after_first_subscribe=True),
                   server, RECONNECT_DELAY_BASE_SEC=0.1)

    print("\n=== origin gate ===")
    with_stub_feed("origin gate", _origin_gate_scenario, StubFeed, server)

    print("\n=== upstream auth rejection ===")
    with_stub_feed("auth rejected at open", _auth_rejected_at_open_scenario,
                   lambda port: StubFeed(port, reject_auth="AUTHENTICATION_ERROR",
                                         echo_key_in_rejection=True), server)
    for code in ("AUTHENTICATION_ERROR", "BROKER_ERROR", "BROKER_INIT_ERROR"):
        with_stub_feed(f"auth rejected on reconnect ({code})",
                       _auth_rejected_on_reconnect_scenario(code),
                       lambda port, code=code: StubFeed(
                           port, drop_after_first_subscribe=True, reject_auth=code,
                           reject_from_session=1),
                       server, MAX_RECONNECT_ATTEMPTS=2, **fast)

    print("\n=== browser disconnect ===")
    with_stub_feed("steady-state disconnect", _steady_disconnect_scenario, StubFeed, server)
    with_stub_feed("mid-reconnect disconnect", _midreconnect_disconnect_scenario,
                   lambda port: StubFeed(port, drop_after_first_subscribe=True,
                                         delay_auth_on_session=1, delay_auth_sec=4.0),
                   server, **fast)

    print("\n=== reconnect exhaustion ===")
    with_stub_feed("reconnect exhaustion", _exhaustion_scenario,
                   lambda port: StubFeed(port, drop_after_first_subscribe=True,
                                         refuse_after_session=0),
                   server, MAX_RECONNECT_ATTEMPTS=3, **fast)

    print("\n=== frame edge cases ===")
    with_stub_feed("frame edge cases", _frame_edge_cases_scenario, StubFeed, server)

    print("\n=== relay cap ===")
    with_stub_feed("relay cap", _relay_cap_scenario, StubFeed, server)


# --- stub REST --------------------------------------------------------------


def test_rest_stub(server: AppServer) -> None:
    """The REST proxy against a stand-in upstream: every failure has a clean answer."""
    print("\n=== REST proxy (stub upstream) ===")
    stub = StubRest(free_port())
    if not stub.server.start():
        check("the REST stub started", False, "uvicorn did not come up")
        return

    raw = get_client().raw
    saved_base, saved_timeout = raw.base_url, raw.timeout
    saved_key = settings.openalgo_api_key
    raw.base_url = f"{stub.server.base}/api/v1/"
    settings.openalgo_api_key = FAKE_KEY
    start, end = date_range()
    history = {"symbol": SYMBOL, "exchange": EXCHANGE, "interval": INTERVAL,
               "start_date": start, "end_date": end}
    try:
        with httpx.Client(base_url=server.base, timeout=30.0) as http:
            res = http.post("/api/oa/history", json={**history, "apikey": "client-key"})
            body = res.json()
            check("history is passed through from the stub with its status",
                  res.status_code == 200 and body.get("endpoint") == "history"
                  and body.get("status") == "success", f"HTTP {res.status_code}")
            check("the stub's body arrives byte for byte",
                  json.loads(res.content) == body and res.headers.get("content-type", "")
                  .startswith("application/json"))

            posted = [
                ("search", "/api/oa/search", {"query": SYMBOL, "exchange": EXCHANGE,
                                              "api_key": "client-key"}),
                ("symbol", "/api/oa/symbol", {"symbol": SYMBOL, "exchange": EXCHANGE,
                                              "apikey": "client-key"}),
                ("quotes", "/api/oa/quotes", {"symbol": SYMBOL, "exchange": EXCHANGE,
                                              "api-key": "client-key"}),
                ("intervals", "/api/oa/intervals", {"apikey": "client-key"}),
            ]
            for endpoint, path, payload in posted:
                res = http.post(path, json=payload)
                check(f"POST {path} is proxied to {endpoint}",
                      res.status_code == 200 and res.json().get("endpoint") == endpoint,
                      f"HTTP {res.status_code}")
            for endpoint, body_seen in stub.received:
                check(f"{endpoint}: the stub received the server key and not the client's",
                      body_seen.get("apikey") == FAKE_KEY
                      and "client-key" not in json.dumps(body_seen)
                      and not ({"api_key", "api-key"} & set(body_seen)),
                      str(sorted(body_seen)))

            for endpoint, path, payload in posted:
                alias = path.replace("/api/oa/", "/api/oa/api/v1/")
                res = http.post(alias, json=payload)
                check(f"the alias {alias} reaches {endpoint}",
                      res.status_code == 200 and res.json().get("endpoint") == endpoint,
                      f"HTTP {res.status_code}")

            res = http.post("/api/oa/quotes", json={"symbol": "FAIL503", "exchange": EXCHANGE})
            check("an upstream 5xx is passed through with its status and body",
                  res.status_code == 503
                  and res.json().get("message") == "stub upstream is down",
                  f"HTTP {res.status_code}")

            res = http.post("/api/oa/quotes", json={"symbol": "HTML", "exchange": EXCHANGE})
            check("a non-JSON upstream body becomes a 502 json_error",
                  res.status_code == 502 and res.json().get("error_type") == "json_error"
                  and "login" not in res.text, f"HTTP {res.status_code} {res.text[:80]}")

            raw.timeout = 1.0
            res = http.post("/api/oa/quotes", json={"symbol": "SLOW", "exchange": EXCHANGE})
            raw.timeout = saved_timeout
            check("an upstream timeout becomes a 504 timeout_error",
                  res.status_code == 504 and res.json().get("error_type") == "timeout_error",
                  f"HTTP {res.status_code}")

            raw.base_url = f"http://127.0.0.1:{free_port()}/api/v1/"
            res = http.post("/api/oa/quotes", json={"symbol": SYMBOL, "exchange": EXCHANGE})
            raw.base_url = f"{stub.server.base}/api/v1/"
            check("a refused connection becomes a 502 connection_error",
                  res.status_code == 502
                  and res.json().get("error_type") == "connection_error",
                  f"HTTP {res.status_code}")
            check("proxy-side errors carry no traceback",
                  "Traceback" not in res.text and "File \"" not in res.text)

            before = len(stub.received)
            oversized = [
                ("symbol", {**history, "symbol": "X" * 65}),
                ("exchange", {**history, "exchange": "X" * 17}),
                ("interval", {**history, "interval": "X" * 9}),
                ("start_date", {**history, "start_date": "X" * 11}),
            ]
            for field, payload in oversized:
                res = http.post("/api/oa/history", json=payload)
                check(f"an oversized {field} answers 422", res.status_code == 422,
                      f"HTTP {res.status_code}")
            res = http.post("/api/oa/search", json={"query": "q" * 129})
            check("an oversized query answers 422", res.status_code == 422)
            check("nothing oversized was forwarded", len(stub.received) == before)

            with ThreadPoolExecutor(max_workers=8) as pool:
                calls = [pool.submit(http.post, "/api/oa/quotes",
                                     json={"symbol": "BUSY", "exchange": EXCHANGE})
                         for _ in range(8)]
                statuses = [c.result().status_code for c in calls]
            check("eight concurrent chart calls all succeed",
                  statuses == [200] * 8, str(statuses))
            check("at most MAX_CONCURRENT_FORWARDS upstream calls were in flight",
                  1 <= stub.max_in_flight <= openalgo_proxy.MAX_CONCURRENT_FORWARDS,
                  f"peak {stub.max_in_flight}")
    finally:
        raw.base_url = saved_base
        raw.timeout = saved_timeout
        settings.openalgo_api_key = saved_key
        stub.server.stop()


# --- live REST --------------------------------------------------------------


def test_rest_live(server: AppServer) -> None:
    """Real candles and quotes through the proxy, with the envelope intact."""
    print("\n=== REST proxy (live) ===")
    start, end = date_range()

    with httpx.Client(base_url=server.base, timeout=30.0) as http:
        res = http.get("/api/oa/config")
        cfg = res.json()
        check("GET /api/oa/config answers 200", res.status_code == 200)
        check("config reports the ws path", cfg.get("ws_path") == WS_PATH,
              json.dumps(cfg))
        check("config reports host_reachable true",
              cfg.get("host_reachable") is True, json.dumps(cfg))
        check("config reports the relay cap", cfg.get("relays_max") == MAX_RELAYS,
              json.dumps(cfg))

        # A wrong apikey in the body proves the client's is ignored, not merged.
        res = http.post("/api/oa/history", json={
            "symbol": SYMBOL, "exchange": EXCHANGE, "interval": INTERVAL,
            "start_date": start, "end_date": end,
            "apikey": "a-client-supplied-key-that-must-be-ignored",
        })
        check("POST /api/oa/history answers 200", res.status_code == 200,
              f"HTTP {res.status_code}")
        check("history content type is JSON",
              res.headers.get("content-type", "").startswith("application/json"),
              res.headers.get("content-type", ""))
        body = res.json()
        check("history keeps OpenAlgo's own envelope",
              body.get("status") == "success" and isinstance(body.get("data"), list),
              str(sorted(body))[:80])
        rows = body.get("data") or []
        check("history returned candles", len(rows) > 0, f"{len(rows)} bars")

        if rows:
            row = rows[0]
            wanted = {"timestamp", "open", "high", "low", "close", "volume", "oi"}
            check("candle carries every field the chart reads",
                  wanted <= set(row), json.dumps(row))
            stamp = row["timestamp"]
            check("timestamp is an integer", isinstance(stamp, int), repr(stamp))
            # 1e9 is 2001 and 4e9 is 2096. A millisecond stamp lands far above this.
            check("timestamp is EPOCH SECONDS, not milliseconds",
                  isinstance(stamp, int) and 1_000_000_000 < stamp < 4_000_000_000,
                  f"{stamp} -> {dt.datetime.fromtimestamp(stamp, dt.UTC)} UTC")
            stamps = [r["timestamp"] for r in rows]
            check("candles are in ascending time order",
                  stamps == sorted(stamps))
            print(f"        first bar: {json.dumps(rows[0])}")
            print(f"        last bar:  {json.dumps(rows[-1])}")

        res = http.post("/api/oa/api/v1/history", json={
            "symbol": SYMBOL, "exchange": EXCHANGE, "interval": INTERVAL,
            "start_date": start, "end_date": end,
        })
        check("the /api/v1 alias path serves the same candles",
              res.status_code == 200 and res.json().get("status") == "success",
              f"HTTP {res.status_code}")

        res = http.post("/api/oa/intervals", json={})
        body = res.json()
        check("POST /api/oa/intervals answers 200 with an envelope",
              res.status_code == 200 and body.get("status") == "success")
        check("intervals carries the minute codes",
              INTERVAL in (body.get("data") or {}).get("minutes", []),
              json.dumps(body.get("data")))

        res = http.post("/api/oa/search", json={"query": SYMBOL,
                                                "exchange": EXCHANGE})
        body = res.json()
        hits = body.get("data") or []
        check("POST /api/oa/search answers 200 with matches",
              res.status_code == 200 and body.get("status") == "success"
              and len(hits) > 0, f"{len(hits)} hits")
        if hits:
            print(f"        first hit: {json.dumps(hits[0])}")

        res = http.post("/api/oa/symbol", json={"symbol": SYMBOL,
                                                "exchange": EXCHANGE})
        body = res.json()
        check("POST /api/oa/symbol answers 200 with contract details",
              res.status_code == 200 and body.get("status") == "success"
              and (body.get("data") or {}).get("symbol") == SYMBOL,
              json.dumps(body.get("data")))

        res = http.post("/api/oa/quotes", json={"symbol": SYMBOL,
                                                "exchange": EXCHANGE})
        body = res.json()
        quote = body.get("data") or {}
        check("POST /api/oa/quotes answers 200 with an ltp",
              res.status_code == 200 and body.get("status") == "success"
              and isinstance(quote.get("ltp"), (int, float)),
              json.dumps(quote))

        res = http.post("/api/oa/history", json={
            "symbol": "NOSUCHSYMBOL", "exchange": EXCHANGE, "interval": INTERVAL,
            "start_date": start, "end_date": end})
        body = res.json()
        check("a bad symbol forwards OpenAlgo's own 400",
              res.status_code == 400 and body.get("status") == "error",
              f"HTTP {res.status_code} {json.dumps(body)[:90]}")
        check("the error carries no traceback",
              "Traceback" not in res.text and "File \"" not in res.text)

        res = http.post("/api/oa/history", json={
            "symbol": SYMBOL, "exchange": EXCHANGE, "interval": "nope",
            "start_date": start, "end_date": end})
        body = res.json()
        # This is the case a str()-based envelope would have destroyed.
        check("a validation error keeps its message as a JSON object",
              res.status_code == 400 and isinstance(body.get("message"), dict),
              json.dumps(body)[:140])

        res = http.post("/api/oa/history", json={"symbol": SYMBOL})
        check("a malformed request answers 422, not 500", res.status_code == 422,
              f"HTTP {res.status_code}")


# --- live WebSocket ---------------------------------------------------------


async def _ws_relay(url: str) -> None:
    """The happy path: handshake, subscribe, and whatever the market sends."""
    async with browser_connect(url, open_timeout=15) as ws:
        auth = await asyncio.wait_for(ws.recv(), timeout=15)
        ack = json.loads(auth)
        check("the browser gets the auth ack without sending a credential",
              ack.get("type") == "auth" and ack.get("status") == "success",
              json.dumps(ack)[:120])
        check("the auth ack leaks no api key",
              "api_key" not in auth and "apikey" not in auth)
        print(f"        auth ack: {auth[:160]}")

        # The single-symbol form with depth_level, exactly what the chart library
        # sends, plus an api_key the relay must strip.
        await ws.send(json.dumps({"action": "subscribe", "symbol": SYMBOL,
                                  "exchange": EXCHANGE, "mode": 3,
                                  "depth_level": 5,
                                  "api_key": "a-client-key-that-must-be-stripped"}))
        frames = await _collect(ws, seconds=10.0)
        kinds = [f.get("type") for f in frames]
        subs = [f for f in frames if f.get("type") == "subscribe"]
        ticks = [f for f in frames if f.get("type") == "market_data"]

        check("the subscribe is acked through the proxy",
              bool(subs) and subs[0].get("status") in ("success", "partial"),
              json.dumps(subs[0])[:200] if subs else str(kinds))
        if ticks:
            data = ticks[0].get("data") or {}
            check("a market_data tick arrived", True,
                  f"ltp={data.get('ltp')} mode={ticks[0].get('mode')}")
            print(f"        tick: {json.dumps(ticks[0])[:220]}")
        else:
            skip("a market_data tick arrived",
                 "no tick inside 10s; the ack above is the assertion instead")

        await ws.send(json.dumps({"action": "ping"}))
        frames = await _collect(ws, seconds=5.0)
        check("ping is relayed and answered with a pong",
              any(f.get("type") == "pong" for f in frames),
              str([f.get("type") for f in frames]))

        await ws.send(json.dumps({"action": "unsubscribe", "symbol": SYMBOL,
                                  "exchange": EXCHANGE, "mode": 3}))
        frames = await _collect(ws, seconds=5.0)
        check("unsubscribe is relayed and acked",
              any(f.get("type") == "unsubscribe" for f in frames),
              str([f.get("type") for f in frames]))


async def _ws_refusals(url: str) -> None:
    """The order stream and everything else outside the allowlist."""
    async with browser_connect(url, open_timeout=15) as ws:
        await asyncio.wait_for(ws.recv(), timeout=15)  # auth ack

        await ws.send(json.dumps({"action": "subscribe_orders"}))
        frames = await _collect(ws, seconds=4.0)
        refusals = [f for f in frames
                    if f.get("code") == "ORDER_STREAM_FORBIDDEN"]
        check("subscribe_orders is refused by the proxy", bool(refusals),
              json.dumps(refusals[0])[:200] if refusals else str(frames))
        check("no order stream ack came back from upstream",
              not any(f.get("type") in ("subscribe_orders", "order_update")
                      for f in frames),
              str([f.get("type") for f in frames]))
        print(f"        refusal: {json.dumps(refusals[0]) if refusals else '(none)'}")

        await ws.send(json.dumps({"action": "get_broker_info"}))
        frames = await _collect(ws, seconds=4.0)
        check("an action outside the allowlist is refused",
              any(f.get("code") == "ACTION_NOT_ALLOWED" for f in frames),
              json.dumps(frames)[:200])

        await ws.send("not json at all")
        frames = await _collect(ws, seconds=4.0)
        check("a non-JSON frame is refused rather than crashing the relay",
              any(f.get("code") == "INVALID_JSON" for f in frames),
              json.dumps(frames)[:160])

        # The socket must still work after three refusals.
        await ws.send(json.dumps({"action": "subscribe", "symbol": SYMBOL,
                                  "exchange": EXCHANGE, "mode": 1}))
        frames = await _collect(ws, seconds=6.0)
        check("the relay still serves market data after refusing three frames",
              any(f.get("type") in ("subscribe", "market_data") for f in frames),
              str([f.get("type") for f in frames]))


def test_ws_live(server: AppServer) -> None:
    """Drive the relay end to end over a real socket."""
    print("\n=== WebSocket relay (live) ===")
    url = f"{server.ws_base}{WS_PATH}"
    try:
        asyncio.run(_ws_relay(url))
        asyncio.run(_ws_refusals(url))
    except Exception as exc:  # noqa: BLE001
        check("websocket relay ran without raising", False,
              f"{type(exc).__name__}: {exc}")


# --- main -------------------------------------------------------------------


def main() -> int:
    """Run the suite.

    Returns:
        1 when anything failed, 0 otherwise.
    """
    print("OpenAlgo proxy tests")
    test_request_models()
    test_strip_credentials()
    test_screening()
    test_replay_set()
    test_replay_lock()

    server = proxy_server(free_port())
    if not server.start():
        check("the test server started", False, "uvicorn did not come up")
    else:
        print(f"\n  proxy under test on {server.base}")
        try:
            # Needs no broker: both upstreams are stubs this file runs itself.
            test_stub_feed(server)
            test_rest_stub(server)
            if openalgo_reachable():
                test_rest_live(server)
                test_ws_live(server)
            else:
                print("\n=== live ===")
                skip("REST proxy (live)",
                     "OpenAlgo is not answering on the configured host")
                skip("WebSocket relay (live)", "OpenAlgo is not answering")
        finally:
            server.stop()

    n_pass = sum(1 for _, s in results if s == PASS)
    n_fail = sum(1 for _, s in results if s == FAIL)
    n_skip = sum(1 for _, s in results if s == SKIP)
    print("\n=== Summary ===")
    for name, status in results:
        if status == FAIL:
            print(f"  FAILED: {name}")
    print(f"  {n_pass} passed, {n_fail} failed, {n_skip} skipped")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
