"""Step 0 validation: prove the two external dependencies work before writing any app code.

Part A checks the OpenAlgo REST connection (reachability, auth, account, quotes, history).
Part B checks the model path: LiteLLM -> Baseten -> DeepSeek V4 Flash, including the two
things the agent actually depends on - token streaming and tool calling.

Run:  python scripts/validate_setup.py
Exits non-zero if any required check fails. Secrets are never printed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def mask(value: str) -> str:
    if not value:
        return "(missing)"
    return f"{value[:4]}...{value[-4:]} (len {len(value)})"


def short(obj, limit: int = 220) -> str:
    text = json.dumps(obj, default=str, ensure_ascii=False) if not isinstance(obj, str) else obj
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


# ---------------------------------------------------------------------------
# Part A: OpenAlgo
# ---------------------------------------------------------------------------

def check_openalgo() -> None:
    print("\n=== Part A: OpenAlgo ===")
    api_key = os.getenv("OPENALGO_API_KEY", "")
    host = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
    timeout = float(os.getenv("OPENALGO_TIMEOUT", "15"))
    print(f"  host={host}  api_key={mask(api_key)}")

    if not api_key:
        record("openalgo api key present", FAIL, "OPENALGO_API_KEY is empty")
        return

    import httpx

    try:
        r = httpx.get(host, timeout=5.0, follow_redirects=False)
        record("server reachable", PASS, f"HTTP {r.status_code}")
    except Exception as exc:
        record("server reachable", FAIL, f"{type(exc).__name__}: {exc}")
        print("  OpenAlgo does not appear to be running. Start it and re-run.")
        return

    from openalgo import api

    client = api(api_key=api_key, host=host, timeout=timeout)

    # ping - authenticates the key without touching the broker.
    # NOTE: base_url is f"{host}/api/{version}/" and already ends in a slash, and
    # _make_request does a bare string concat. A leading slash on the endpoint gives
    # "/api/v1//ping", which the server answers with a 308 that httpx does not follow.
    # Every raw_post endpoint name must therefore be slash-free.
    try:
        out = client._make_request("ping", {"apikey": api_key})
        ok = isinstance(out, dict) and out.get("status") == "success"
        broker = (out or {}).get("data", {}).get("broker")
        record("ping (auth, raw_post path)", PASS if ok else FAIL,
               f"broker={broker}" if ok else short(out))
    except Exception as exc:
        record("ping (auth, raw_post path)", FAIL, f"{type(exc).__name__}: {exc}")

    # the same path the GTT tools will use - confirm an unwrapped endpoint responds
    try:
        out = client._make_request("gttorderbook", {"apikey": api_key})
        ok = isinstance(out, dict) and "status" in out
        record("raw_post reaches unwrapped endpoint (gttorderbook)",
               PASS if ok else FAIL, short(out, 140))
    except Exception as exc:
        record("raw_post reaches unwrapped endpoint (gttorderbook)", FAIL,
               f"{type(exc).__name__}: {exc}")

    # analyzer mode - decides whether orders would be simulated or real
    try:
        out = client.analyzerstatus()
        mode = (out or {}).get("data", {}).get("mode")
        record("analyzerstatus", PASS if out.get("status") == "success" else FAIL,
               f"mode={mode}")
    except Exception as exc:
        record("analyzerstatus", FAIL, f"{type(exc).__name__}: {exc}")

    for name, fn in [
        ("funds", lambda: client.funds()),
        ("orderbook", lambda: client.orderbook()),
        ("positionbook", lambda: client.positionbook()),
        ("holdings", lambda: client.holdings()),
    ]:
        try:
            out = fn()
            ok = isinstance(out, dict) and out.get("status") == "success"
            record(name, PASS if ok else FAIL, short(out))
        except Exception as exc:
            record(name, FAIL, f"{type(exc).__name__}: {exc}")

    # quotes - the check the user asked for
    for sym, exch in [("RELIANCE", "NSE"), ("SBIN", "NSE"), ("NIFTY", "NSE_INDEX")]:
        try:
            out = client.quotes(symbol=sym, exchange=exch)
            ok = isinstance(out, dict) and out.get("status") == "success"
            data = (out or {}).get("data", {})
            detail = (f"ltp={data.get('ltp')} open={data.get('open')} "
                      f"high={data.get('high')} low={data.get('low')} "
                      f"prev_close={data.get('prev_close')} vol={data.get('volume')}"
                      if ok else short(out))
            record(f"quotes {sym}@{exch}", PASS if ok else FAIL, detail)
        except Exception as exc:
            record(f"quotes {sym}@{exch}", FAIL, f"{type(exc).__name__}: {exc}")

    try:
        out = client.multiquotes(symbols=[
            {"symbol": "RELIANCE", "exchange": "NSE"},
            {"symbol": "TCS", "exchange": "NSE"},
            {"symbol": "INFY", "exchange": "NSE"},
        ])
        ok = isinstance(out, dict) and out.get("status") == "success"
        n = len(out.get("results", [])) if ok else 0
        record("multiquotes x3", PASS if ok else FAIL,
               f"{n} results" if ok else short(out))
    except Exception as exc:
        record("multiquotes x3", FAIL, f"{type(exc).__name__}: {exc}")

    try:
        out = client.depth(symbol="SBIN", exchange="NSE")
        ok = isinstance(out, dict) and out.get("status") == "success"
        d = (out or {}).get("data", {})
        record("depth SBIN", PASS if ok else FAIL,
               f"{len(d.get('bids', []))} bids / {len(d.get('asks', []))} asks" if ok else short(out))
    except Exception as exc:
        record("depth SBIN", FAIL, f"{type(exc).__name__}: {exc}")

    try:
        out = client.intervals()
        ok = isinstance(out, dict) and out.get("status") == "success"
        record("intervals", PASS if ok else FAIL, short(out.get("data") if ok else out))
    except Exception as exc:
        record("intervals", FAIL, f"{type(exc).__name__}: {exc}")

    # history returns a DataFrame on success and a dict on error - verify the type
    try:
        import datetime as dt
        end = dt.date.today()
        start = end - dt.timedelta(days=10)
        out = client.history(symbol="SBIN", exchange="NSE", interval="5m",
                             start_date=start.isoformat(), end_date=end.isoformat())
        import pandas as pd
        if isinstance(out, pd.DataFrame):
            record("history SBIN 5m", PASS,
                   f"{len(out)} bars, cols={list(out.columns)}, "
                   f"last={out.index[-1] if len(out) else 'n/a'}")
        else:
            record("history SBIN 5m", FAIL, short(out))
    except Exception as exc:
        record("history SBIN 5m", FAIL, f"{type(exc).__name__}: {exc}")

    # the indicator library runs on that frame - prove the whole path end to end
    try:
        from openalgo import ta
        import pandas as pd
        if isinstance(out, pd.DataFrame) and len(out) > 60:
            df = out.dropna().sort_index()
            rsi = ta.rsi(df["close"], 14)
            macd_line, signal, hist = ta.macd(df["close"])
            record("ta.rsi + ta.macd on live data", PASS,
                   f"rsi={round(float(rsi.iloc[-1]), 2)} macd={round(float(macd_line.iloc[-1]), 3)}")
        else:
            record("ta.rsi + ta.macd on live data", SKIP, "not enough bars")
    except Exception as exc:
        record("ta.rsi + ta.macd on live data", FAIL, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Part B: LiteLLM -> Baseten
# ---------------------------------------------------------------------------

QUOTE_TOOL = [{
    "type": "function",
    "function": {
        "name": "get_quote",
        "description": "Get the latest traded price for a stock on an Indian exchange.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "OpenAlgo symbol, e.g. RELIANCE"},
                "exchange": {"type": "string", "description": "NSE, BSE, NFO, MCX"},
            },
            "required": ["symbol", "exchange"],
        },
    },
}]


def check_litellm() -> None:
    print("\n=== Part B: LiteLLM -> Baseten ===")
    key = os.getenv("LITELLM_API_KEY", "")
    model = os.getenv("LITELLM_MODEL", "baseten/deepseek-ai/DeepSeek-V4-Flash-0731")
    api_base = os.getenv("LITELLM_API_BASE", "https://inference.baseten.co/v1")
    print(f"  model={model}  api_base={api_base}  key={mask(key)}")

    if not key:
        record("model api key present", FAIL, "LITELLM_API_KEY is empty")
        return

    import litellm
    litellm.suppress_debug_info = True
    litellm.drop_params = True
    # litellm exposes no __version__ attribute; read it from the installed distribution.
    from importlib.metadata import version as _dist_version
    print(f"  litellm={_dist_version('litellm')}")

    common = {"api_key": key, "api_base": api_base}

    # 1. plain completion.
    # This model is a REASONING model: it spends completion tokens on hidden reasoning
    # before emitting any content, and exposes it as message.reasoning_content. A small
    # max_tokens is consumed entirely by reasoning and returns content=None with
    # finish_reason="length" - an empty reply, not a truncated one. Keep max_tokens high.
    try:
        t0 = time.time()
        resp = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=600, temperature=0.0, **common,
        )
        msg = resp.choices[0].message
        text = (msg.content or "").strip()
        reasoning = getattr(msg, "reasoning_content", None)
        usage = getattr(resp, "usage", None)
        rtok = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
        record("completion", PASS if text else FAIL,
               f'"{text[:40]}" in {time.time() - t0:.2f}s, '
               f"total={getattr(usage, 'total_tokens', '?')} reasoning={rtok}")
        record("reasoning model confirmed", PASS if reasoning else SKIP,
               f"reasoning_content present ({len(reasoning)} chars)" if reasoning
               else "no reasoning_content on this response")
    except Exception as exc:
        record("completion", FAIL, f"{type(exc).__name__}: {str(exc)[:200]}")
        return

    # 1b. the low-max_tokens trap, asserted so a future model swap cannot reintroduce it
    try:
        resp = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=16, temperature=0.0, **common,
        )
        empty = not (resp.choices[0].message.content or "").strip()
        record("low max_tokens yields empty content (documented trap)",
               PASS if empty else SKIP,
               f"content empty={empty}, finish={resp.choices[0].finish_reason}")
    except Exception as exc:
        record("low max_tokens trap", SKIP, f"{type(exc).__name__}: {str(exc)[:120]}")

    # 2. streaming - the agent streams every reply. Ask for enough output that a
    # single-chunk response is not a plausible result.
    try:
        t0 = time.time()
        chunks, first_at, reasoning_chunks = 0, None, 0
        stream = litellm.completion(
            model=model,
            messages=[{"role": "user", "content":
                       "List the four OpenAlgo price types with a one-line description each."}],
            max_tokens=900, temperature=0.0, stream=True,
            stream_options={"include_usage": True}, **common,
        )
        text = ""
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "reasoning_content", None):
                reasoning_chunks += 1
            if delta.content:
                if first_at is None:
                    first_at = time.time() - t0
                chunks += 1
                text += delta.content
        record("streaming", PASS if chunks > 1 else FAIL,
               f"{chunks} content chunks ({reasoning_chunks} reasoning), "
               f"first content token {first_at:.2f}s, total {time.time() - t0:.2f}s, "
               f"{len(text)} chars")
    except Exception as exc:
        record("streaming", FAIL, f"{type(exc).__name__}: {str(exc)[:200]}")

    # 3. tool calling - the whole agent depends on this
    try:
        resp = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "What is the current price of RELIANCE on NSE?"}],
            tools=QUOTE_TOOL, tool_choice="auto",
            max_tokens=256, temperature=0.0, **common,
        )
        calls = resp.choices[0].message.tool_calls or []
        if calls:
            fn = calls[0].function
            args = json.loads(fn.arguments or "{}")
            ok = fn.name == "get_quote" and args.get("symbol") == "RELIANCE"
            record("tool calling", PASS if ok else FAIL, f"{fn.name}({args})")
        else:
            record("tool calling", FAIL,
                   f"no tool_calls; content={short(resp.choices[0].message.content or '')}")
    except Exception as exc:
        record("tool calling", FAIL, f"{type(exc).__name__}: {str(exc)[:200]}")

    # 4. tool calling while streaming - how the agent actually runs
    try:
        stream = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "Get me the price of SBIN on NSE."}],
            tools=QUOTE_TOOL, tool_choice="auto",
            max_tokens=256, temperature=0.0, stream=True, **common,
        )
        name_parts, arg_parts = [], []
        for chunk in stream:
            if not chunk.choices:
                continue
            for tc in (chunk.choices[0].delta.tool_calls or []):
                if tc.function.name:
                    name_parts.append(tc.function.name)
                if tc.function.arguments:
                    arg_parts.append(tc.function.arguments)
        name = "".join(name_parts)
        raw = "".join(arg_parts)
        ok = name == "get_quote" and "SBIN" in raw
        record("tool calling (streamed)", PASS if ok else FAIL, f"{name}({raw[:80]})")
    except Exception as exc:
        record("tool calling (streamed)", FAIL, f"{type(exc).__name__}: {str(exc)[:200]}")


# ---------------------------------------------------------------------------
# Part C: the full Agno path, including the confirmation gate
# ---------------------------------------------------------------------------

def check_agno() -> None:
    print("\n=== Part C: Agno agent (model + tools + confirmation gate) ===")
    key = os.getenv("LITELLM_API_KEY", "")
    model_id = os.getenv("LITELLM_MODEL", "baseten/deepseek-ai/DeepSeek-V4-Flash-0731")
    api_base = os.getenv("LITELLM_API_BASE", "https://inference.baseten.co/v1")
    if not key:
        record("agno agent", SKIP, "no LITELLM_API_KEY")
        return

    try:
        import agno
        from agno.agent import Agent
        from agno.db.sqlite import SqliteDb
        from agno.models.litellm import LiteLLM
        from agno.tools import tool
        print(f"  agno={agno.__version__}")
    except Exception as exc:
        record("agno import", FAIL, f"{type(exc).__name__}: {exc}")
        return

    calls: list[str] = []

    @tool()
    def get_ltp(symbol: str, exchange: str) -> str:
        """Get the last traded price of a symbol.

        Args:
            symbol (str): OpenAlgo symbol, for example RELIANCE.
            exchange (str): Exchange code, for example NSE.

        Returns:
            str: JSON with the last traded price.
        """
        calls.append(f"get_ltp({symbol},{exchange})")
        return json.dumps({"symbol": symbol, "exchange": exchange, "ltp": 1234.5})

    @tool(requires_confirmation=True)
    def place_test_order(symbol: str, quantity: int) -> str:
        """Place an order. Requires human confirmation.

        Args:
            symbol (str): OpenAlgo symbol.
            quantity (int): Number of shares.

        Returns:
            str: JSON with the order id.
        """
        calls.append(f"place_test_order({symbol},{quantity})")
        return json.dumps({"orderid": "TEST-1", "status": "success"})

    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    agent = Agent(
        model=LiteLLM(id=model_id, api_key=key, api_base=api_base,
                      temperature=0.2, max_tokens=4096),   # reasoning model, keep this high
        db=SqliteDb(db_file=str(data_dir / "validate.db")),
        tools=[get_ltp, place_test_order],
        instructions=["Use the tools. Be terse."],
        telemetry=False, store_events=False, markdown=False,
    )

    # the confirmation flag is the safety gate - assert it is really set
    record("confirmation flag set on place_test_order",
           PASS if getattr(place_test_order, "requires_confirmation", False) else FAIL,
           f"requires_confirmation={getattr(place_test_order, 'requires_confirmation', None)}")

    # 1. a normal tool-using turn
    try:
        run = agent.run("What is the LTP of RELIANCE on NSE?")
        used = any("get_ltp" in c for c in calls)
        record("agent tool call", PASS if used else FAIL,
               f"tools_called={calls or 'none'}; reply={short(run.content or '', 90)}")
    except Exception as exc:
        record("agent tool call", FAIL, f"{type(exc).__name__}: {str(exc)[:200]}")

    # 2. the confirmation gate must pause before executing
    try:
        calls.clear()
        run = agent.run("Place an order for 10 shares of RELIANCE.")
        paused = bool(getattr(run, "is_paused", False))
        reqs = list(getattr(run, "active_requirements", []) or [])
        detail = f"is_paused={paused}, requirements={len(reqs)}"
        if reqs:
            te = reqs[0].tool_execution
            detail += f", pending={te.tool_name}({te.tool_args})"
        not_executed = not any("place_test_order" in c for c in calls)
        record("confirmation gate pauses", PASS if (paused and reqs and not_executed) else FAIL,
               detail)

        # 3. resume after approving
        if paused and reqs:
            for req in reqs:
                if req.needs_confirmation:
                    req.confirm()
            run2 = agent.continue_run(run_id=run.run_id, session_id=run.session_id,
                                      requirements=run.requirements)
            executed = any("place_test_order" in c for c in calls)
            record("resume after approve executes tool", PASS if executed else FAIL,
                   f"tools_called={calls}; reply={short(run2.content or '', 90)}")
    except Exception as exc:
        record("confirmation gate pauses", FAIL, f"{type(exc).__name__}: {str(exc)[:200]}")


def main() -> int:
    print("OpenAlgo Trading Agent - Step 0 validation")
    print(f"python {sys.version.split()[0]}  root={ROOT}")
    check_openalgo()
    check_litellm()
    check_agno()

    print("\n=== Summary ===")
    n_pass = sum(1 for _, s, _ in results if s == PASS)
    n_fail = sum(1 for _, s, _ in results if s == FAIL)
    n_skip = sum(1 for _, s, _ in results if s == SKIP)
    for name, status, _ in results:
        if status == FAIL:
            print(f"  FAILED: {name}")
    print(f"  {n_pass} passed, {n_fail} failed, {n_skip} skipped")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
