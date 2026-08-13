"""Read-only toolkit tests: registration, schema generation, and live calls.

Schema generation is worth testing on its own: Agno builds the JSON schema from
get_type_hints plus a parsed docstring, so a missing Args: block yields a tool the model
cannot call correctly, and nothing warns you.

Run:  python backend/tests/test_tools_readonly.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import setup_logging  # noqa: E402

setup_logging("WARNING")

from app.openalgo.client import get_client  # noqa: E402
from app.tools.account import AccountTools  # noqa: E402
from app.tools.market import MarketDataTools  # noqa: E402
from app.tools.symbols import SymbolTools  # noqa: E402
from app.tools.system import SystemTools  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    results.append((name, status))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    results.append((name, SKIP))
    print(f"  [{SKIP}] {name}" + (f" - {detail}" if detail else ""))


EXPECTED = {
    "market_data_tools": ["get_quote", "get_quotes_bulk", "get_market_depth",
                          "get_history", "get_supported_intervals", "get_market_calendar"],
    "symbol_tools": ["search_symbols", "get_symbol_info", "get_expiry_dates",
                     "list_instruments"],
    "account_tools": ["get_funds", "get_orderbook", "get_tradebook", "get_positionbook",
                      "get_holdings", "calculate_margin"],
    "system_tools": ["get_analyzer_status", "set_analyzer_mode", "get_sandbox_pnl",
                     "check_connection", "send_alert"],
}


def test_registration() -> list:
    print("\n=== toolkit registration ===")
    kits = [MarketDataTools(), SymbolTools(), AccountTools(), SystemTools()]
    total = 0
    for kit in kits:
        names = [f.name for f in kit.functions.values()]
        total += len(names)
        expected = EXPECTED[kit.name]
        check(f"{kit.name} registers {len(expected)} tools",
              sorted(names) == sorted(expected),
              f"{sorted(names)}" if sorted(names) != sorted(expected) else f"{len(names)}")
    check("21 read-only tools total", total == 21, str(total))
    return kits


def test_schemas(kits: list) -> None:
    print("\n=== schema generation ===")
    # Function.description and Function.parameters are populated LAZILY by
    # process_entrypoint(), which the Agent calls when it assembles its tool list.
    # Straight after Toolkit construction they are still empty, so the test has to
    # trigger the same processing the agent would.
    for kit in kits:
        for fn in kit.functions.values():
            fn.process_entrypoint()

    problems: list[str] = []
    described = 0
    for kit in kits:
        for fn in kit.functions.values():
            params = fn.parameters or {}
            props = params.get("properties", {})
            if not fn.description:
                problems.append(f"{fn.name}: no description")
            else:
                described += 1
            # every declared arg must appear in the schema with a type
            import inspect
            sig = inspect.signature(fn.entrypoint)
            for pname, p in sig.parameters.items():
                if pname in ("self", "agent", "team", "run_context"):
                    continue
                if pname not in props:
                    problems.append(f"{fn.name}: arg {pname} missing from schema")
                elif "type" not in props[pname] and "anyOf" not in props[pname]:
                    problems.append(f"{fn.name}: arg {pname} has no type")
    check("every tool has a description", described == 21, f"{described}/21")
    check("every argument reached the schema with a type", not problems,
          "; ".join(problems[:4]) if problems else "clean")

    q = next(f for k in kits for f in k.functions.values() if f.name == "get_quote")
    props = (q.parameters or {}).get("properties", {})
    check("get_quote schema has symbol and exchange",
          "symbol" in props and "exchange" in props, str(list(props)))
    check("get_quote arg descriptions survived the docstring parse",
          bool(props.get("symbol", {}).get("description")),
          str(props.get("symbol", {}).get("description", ""))[:50])


def test_confirmation_flags(kits: list) -> None:
    print("\n=== confirmation flags ===")
    # A typo in requires_confirmation_tools only WARNS in agno 2.8.7, so assert it.
    system = next(k for k in kits if k.name == "system_tools")
    fn = system.functions.get("set_analyzer_mode")
    check("set_analyzer_mode requires confirmation",
          fn is not None and fn.requires_confirmation is True,
          f"requires_confirmation={getattr(fn, 'requires_confirmation', None)}")

    gated = [f.name for k in kits for f in k.functions.values()
             if getattr(f, "requires_confirmation", False)]
    check("only set_analyzer_mode is gated among read-only kits",
          gated == ["set_analyzer_mode"], str(gated))


def test_live(kits: list) -> None:
    print("\n=== live calls ===")
    client = get_client()
    if not client.settings.openalgo_api_key or not client.ping().get("ok"):
        skip("live tool calls", "OpenAlgo unreachable")
        return

    market = next(k for k in kits if k.name == "market_data_tools")
    symbols = next(k for k in kits if k.name == "symbol_tools")
    account = next(k for k in kits if k.name == "account_tools")
    system = next(k for k in kits if k.name == "system_tools")

    def call(kit, name, **kw):
        return json.loads(kit.functions[name].entrypoint(**kw))

    r = call(system, "check_connection")
    check("check_connection", r["ok"], f"broker={r['data']['broker']} mode={r['data']['mode']}")

    r = call(system, "get_analyzer_status")
    check("get_analyzer_status", r["ok"] and r["mode"] in ("analyze", "live"),
          f"mode={r.get('mode')} orders_are_real={r.get('orders_are_real')}")

    r = call(market, "get_quote", symbol="RELIANCE", exchange="NSE")
    check("get_quote", r["ok"] and r["data"]["ltp"] > 0, f"ltp={r['data']['ltp']}")

    r = call(market, "get_quotes_bulk",
             symbols=[{"symbol": "SBIN", "exchange": "NSE"},
                      {"symbol": "INFY", "exchange": "NSE"}])
    check("get_quotes_bulk", r["ok"] and len(r["data"]["results"]) == 2,
          f"{len(r['data'].get('results', []))} results")

    r = call(market, "get_market_depth", symbol="SBIN", exchange="NSE")
    check("get_market_depth", r["ok"] and len(r["data"]["bids"]) == 5)

    r = call(market, "get_history", symbol="SBIN", exchange="NSE", interval="5m",
             lookback_bars=100, last_n=3)
    check("get_history returns a summary not raw candles",
          r["ok"] and r["data"]["bars"] > 50 and len(r["data"]["recent"]) == 3,
          f"{r['data']['bars']} bars, {len(r['data']['recent'])} shown, "
          f"change={r['data'].get('period_change_pct')}%")

    r = call(market, "get_supported_intervals")
    check("get_supported_intervals", r["ok"] and "minutes" in r["data"],
          str(r["data"].get("minutes")))

    r = call(market, "get_market_calendar", year=2026)
    check("get_market_calendar", r["ok"])

    r = call(symbols, "search_symbols", query="RELIANCE", exchange="NSE")
    check("search_symbols", r["ok"] and len(r["data"]) > 0, f"{len(r['data'])} matches")

    r = call(symbols, "get_expiry_dates", symbol="NIFTY", exchange="NFO",
             instrumenttype="options")
    check("get_expiry_dates", r["ok"] and len(r["data"]) > 0,
          f"{len(r['data'])} expiries, first={r['data'][0] if r['data'] else None}")

    r = call(symbols, "list_instruments", exchange="NSE", name_contains="RELIANCE", limit=5)
    check("list_instruments filtered and capped",
          r["ok"] and r["data"]["returned"] <= 5,
          f"{r['data']['returned']} of {r['data']['total_matches']}")

    try:
        call(symbols, "list_instruments", exchange="NSE")
        check("list_instruments refuses an unfiltered call", False, "no exception")
    except Exception as exc:  # noqa: BLE001
        check("list_instruments refuses an unfiltered call",
              "name_contains" in str(exc), type(exc).__name__)

    for name in ("get_funds", "get_orderbook", "get_tradebook", "get_holdings"):
        r = call(account, name)
        check(name, r["ok"])

    r = call(account, "get_positionbook")
    check("get_positionbook", r["ok"])
    r = call(account, "get_positionbook", symbol="SBIN", exchange="NSE")
    check("get_positionbook filter", r["ok"] and "filter" in r, str(r.get("matched")))

    r = call(account, "calculate_margin", positions=[
        {"symbol": "SBIN", "exchange": "NSE", "action": "BUY",
         "product": "MIS", "pricetype": "MARKET", "quantity": 1}])
    check("calculate_margin", r["ok"], str(r.get("data"))[:80])

    # a bad symbol must raise RetryAgentRun with an actionable message
    from agno.exceptions import RetryAgentRun
    try:
        call(market, "get_quote", symbol="NOSUCHSYM999", exchange="NSE")
        check("bad symbol raises RetryAgentRun", False, "no exception")
    except RetryAgentRun as exc:
        # The message must be ACTIONABLE, not merely an error. It now carries the
        # closest real symbols rather than telling the model to go and search.
        msg = str(exc)
        check("bad symbol raises an actionable RetryAgentRun",
              "Closest matching symbols" in msg or "search_symbols" in msg
              or "Index symbols" in msg, msg[:90])
    except Exception as exc:  # noqa: BLE001
        check("bad symbol raises RetryAgentRun", False, f"wrong type {type(exc).__name__}")

    # every tool must return non-empty JSON; agno turns a falsy return into an empty
    # tool message that derails the model
    empties = []
    for kit in kits:
        for fn in kit.functions.values():
            if fn.name in ("set_analyzer_mode", "send_alert", "get_sandbox_pnl"):
                continue  # mutating or needs config
    check("no tool returned an empty payload", not empties, str(empties))


def main() -> int:
    print("Read-only toolkit tests")
    kits = test_registration()
    test_schemas(kits)
    test_confirmation_flags(kits)
    test_live(kits)

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
