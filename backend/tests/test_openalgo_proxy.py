"""OpenAlgo proxy tests: REST passthrough, WebSocket relay, and the refusals.

Pure-logic checks run always. Live checks mount ONLY the proxy router on a spare port
and talk to a real OpenAlgo, so they are reported as SKIP when nothing is listening.

The API key is never read, printed or asserted on by this file. The whole point of the
proxy is that a client never sees it, so the tests behave like a client.

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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from websockets.asyncio.server import serve as ws_serve  # noqa: E402

from app.openalgo.client import get_client  # noqa: E402
from app.routes import openalgo_proxy  # noqa: E402
from app.routes.openalgo_proxy import (  # noqa: E402
    ALLOWED_ACTIONS,
    ChartFeedRelay,
    HistoryRequest,
    QuotesRequest,
    WS_PATH,
    _strip_credentials,
    router,
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str]] = []

SYMBOL, EXCHANGE, INTERVAL = "RELIANCE", "NSE", "5m"


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


class ProxyServer:
    """A uvicorn instance mounting only the proxy router, on a background thread."""

    def __init__(self, port: int) -> None:
        """Build the server without starting it.

        Args:
            port: Loopback port to bind.
        """
        app = FastAPI(title="openalgo proxy under test")
        app.include_router(router)
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.ws_base = f"ws://127.0.0.1:{port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True,
                                        name="proxy-under-test")

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


# --- pure logic -------------------------------------------------------------


def test_request_models() -> None:
    """A client-supplied apikey must never survive into the forwarded body."""
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


def test_strip_credentials() -> None:
    """Credential keys go at every depth; instrument tokens stay."""
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


def test_screening() -> None:
    """The allowlist, the order-stream refusal, and the replay set."""
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

    bulk = relay._subs[("NSE", "SBIN", "ltp")]
    check("a bulk subscribe is split into single-instrument replay frames",
          bulk["symbols"] == [{"symbol": "SBIN", "exchange": "NSE"}],
          json.dumps(bulk))

    relay._screen({"action": "unsubscribe", "symbols": [{"symbol": "SBIN",
                                                         "exchange": "NSE",
                                                         "mode": "LTP"}]})
    check("unsubscribe drops just that instrument", len(relay._subs) == 2,
          str(sorted(relay._subs)))

    relay._screen({"action": "unsubscribe_all"})
    check("unsubscribe_all clears the replay set", not relay._subs)


# --- reconnect and replay (stub upstream, no broker needed) ------------------


async def _reconnect_scenario(browser_url: str, stub_port: int) -> None:
    """Drop the upstream mid-session and prove the relay heals it invisibly.

    The browser's own socket never moves, so a proxy-side reconnect that did not
    replay subscriptions would leave the chart open and permanently silent. That is
    the failure this scenario exists to catch.

    Args:
        browser_url: The proxy's own WebSocket URL.
        stub_port: Port the stub upstream should listen on.
    """
    sessions: list[list[dict[str, Any]]] = []
    drop_pending = {"armed": True}

    async def upstream(conn: Any) -> None:
        """Stand in for the OpenAlgo feed, dropping the first session once."""
        received: list[dict[str, Any]] = []
        sessions.append(received)
        first = len(sessions) == 1
        try:
            async for raw in conn:
                message = json.loads(raw)
                received.append(message)
                action = message.get("action")
                if action == "authenticate":
                    await conn.send(json.dumps({"type": "auth", "status": "success",
                                                "broker": "stub"}))
                elif action == "subscribe":
                    await conn.send(json.dumps({
                        "type": "subscribe", "status": "success",
                        "subscriptions": [{"symbol": message.get("symbol"),
                                           "exchange": message.get("exchange"),
                                           "status": "success"}]}))
                    if first and drop_pending["armed"]:
                        drop_pending["armed"] = False
                        await conn.close(code=1011, reason="stub drop")
                        return
        except websockets.ConnectionClosed:
            pass

    async with ws_serve(upstream, "127.0.0.1", stub_port):
        async with websockets.connect(browser_url, open_timeout=10) as ws:
            first_auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            check("the stub handshake is relayed to the browser",
                  first_auth.get("type") == "auth", json.dumps(first_auth))

            await ws.send(json.dumps({"action": "subscribe", "symbol": SYMBOL,
                                      "exchange": EXCHANGE, "mode": 3,
                                      "depth_level": 5}))
            frames = await _collect(ws, seconds=8.0)
            kinds = [f.get("type") for f in frames]
            check("the browser is told the upstream is reconnecting",
                  any(f.get("type") == "proxy"
                      and f.get("status") == "reconnecting" for f in frames),
                  str(kinds))
            check("a second auth ack reaches the browser after the reconnect",
                  sum(1 for f in frames if f.get("type") == "auth") >= 1, str(kinds))
            check("the browser socket survived the upstream drop",
                  ws.state is websockets.protocol.State.OPEN, str(ws.state))

    check("the stub saw two upstream sessions", len(sessions) == 2,
          f"{len(sessions)} session(s)")
    if len(sessions) == 2:
        replayed = [m for m in sessions[1] if m.get("action") == "subscribe"]
        check("the second session re-authenticated",
              any(m.get("action") == "authenticate" for m in sessions[1]))
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


def test_reconnect(server: ProxyServer) -> None:
    """Point the relay at a stub upstream and force one drop."""
    print("\n=== upstream drop, reconnect and replay ===")
    stub_port = free_port()
    saved_url = openalgo_proxy.settings.openalgo_ws_url
    saved_key = openalgo_proxy.settings.openalgo_api_key
    openalgo_proxy.settings.openalgo_ws_url = f"ws://127.0.0.1:{stub_port}"
    openalgo_proxy.settings.openalgo_api_key = "stub-key-not-a-real-credential"
    try:
        asyncio.run(_reconnect_scenario(f"{server.ws_base}{WS_PATH}", stub_port))
    except Exception as exc:  # noqa: BLE001
        check("the reconnect scenario ran without raising", False,
              f"{type(exc).__name__}: {exc}")
    finally:
        openalgo_proxy.settings.openalgo_ws_url = saved_url
        openalgo_proxy.settings.openalgo_api_key = saved_key


# --- live REST --------------------------------------------------------------


def test_rest_live(server: ProxyServer) -> None:
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


async def _collect(ws: Any, seconds: float, limit: int = 40) -> list[dict[str, Any]]:
    """Read frames until the budget runs out.

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


async def _ws_relay(url: str) -> None:
    """The happy path: handshake, subscribe, and whatever the market sends."""
    async with websockets.connect(url, open_timeout=15) as ws:
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
    async with websockets.connect(url, open_timeout=15) as ws:
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


def test_ws_live(server: ProxyServer) -> None:
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

    server = ProxyServer(free_port())
    if not server.start():
        check("the test server started", False, "uvicorn did not come up")
    else:
        print(f"\n  proxy under test on {server.base}")
        try:
            # Needs no broker: the upstream is a stub this file runs itself.
            test_reconnect(server)
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
