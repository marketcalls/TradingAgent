"""Order toolkit tests: the confirmation gate, RiskGuard wiring, audit rows, live calls.

The first section is the safety test the plan calls for. A typo in
requires_confirmation_tools only WARNS in agno 2.8.7, so nothing except an explicit
assertion stands between a misspelled entry and an unconfirmed live order. Every gated
tool is therefore listed by name here rather than derived from the toolkit, so that a
renamed method fails loudly instead of quietly leaving the list empty.

The stub sections need neither a broker nor a network. The live section runs only when
OpenAlgo is reachable AND the analyzer is in "analyze" mode, so every order placed is
simulated. If the server is in live mode the order tests are skipped loudly.

TRADING_ENABLED is false in .env, so the shipped RiskGuard refuses everything. The live
section builds its own Settings with trading_enabled=True, exactly as test_safety.py
does, rather than editing .env.

Run:  python backend/tests/test_tools_orders.py
"""

from __future__ import annotations

import inspect
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import Settings, setup_logging  # noqa: E402

setup_logging("ERROR")

from agno.exceptions import RetryAgentRun  # noqa: E402

from app.openalgo.client import get_client  # noqa: E402
from app.safety.audit import AuditLog  # noqa: E402
from app.safety.risk import RiskGuard  # noqa: E402
from app.tools.gtt import GttTools  # noqa: E402
from app.tools.options import OptionsTools  # noqa: E402
from app.tools.orders import OrderTools  # noqa: E402

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
    "order_tools": ["place_order", "place_smart_order", "place_basket_order",
                    "place_split_order", "modify_order", "cancel_order",
                    "cancel_all_orders", "close_all_positions", "get_order_status"],
    "gtt_tools": ["place_gtt_order", "manage_gtt_order"],
    "options_tools": ["get_option_chain", "resolve_option_symbol", "get_option_greeks",
                      "get_synthetic_future", "place_options_order",
                      "place_options_multi_order"],
}

# Spelled out on purpose. Deriving this from the toolkit would make the test pass
# against an empty requires_confirmation_tools list.
MUST_BE_GATED = [
    ("order_tools", "place_order"),
    ("order_tools", "place_smart_order"),
    ("order_tools", "place_basket_order"),
    ("order_tools", "place_split_order"),
    ("order_tools", "modify_order"),
    ("order_tools", "cancel_order"),
    ("order_tools", "cancel_all_orders"),
    ("order_tools", "close_all_positions"),
    ("gtt_tools", "place_gtt_order"),
    ("gtt_tools", "manage_gtt_order"),
    ("options_tools", "place_options_order"),
    ("options_tools", "place_options_multi_order"),
]

MUST_NOT_BE_GATED = [
    ("order_tools", "get_order_status"),
    ("options_tools", "get_synthetic_future"),
    ("options_tools", "get_option_chain"),
    ("options_tools", "resolve_option_symbol"),
    ("options_tools", "get_option_greeks"),
]


# --------------------------------------------------------------------------
# stubs
# --------------------------------------------------------------------------

class StubClient:
    """Stands in for OpenAlgoClient and records every broker call it receives."""

    def __init__(self, mode: str = "analyze", ltp: float | None = 100.0) -> None:
        self._mode = mode
        self._ltp = ltp
        self.calls: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []

    # -- the surface the toolkits use
    def analyzer_mode(self) -> str:
        return self._mode

    def ltp(self, symbol: str, exchange: str) -> float | None:
        return self._ltp

    def call_enveloped(self, method: str, source: str | None = None,
                       _order: bool = False, **kwargs) -> dict:
        self.calls.append((method, kwargs))
        if method == "expiry":
            return {"ok": True, "source": "expiry", "data": ["25-AUG-26"]}
        if method == "optionsymbol":
            return {"ok": True, "source": "optionsymbol",
                    "data": {"symbol": "NIFTY25AUG2624500CE", "exchange": "NFO",
                             "lotsize": 65}}
        return {"ok": True, "source": method,
                "data": {"orderid": "STUB-1"}, "mode": self._mode}

    def raw_post(self, endpoint: str, payload: dict | None = None,
                 _order: bool = False) -> dict:
        self.posts.append((endpoint, payload or {}))
        return {"ok": True, "source": endpoint,
                "data": {"trigger_id": "STUB-GTT-1"}, "mode": self._mode}

    # -- helpers for assertions
    @property
    def order_calls(self) -> list[str]:
        writes = ("placeorder", "placesmartorder", "basketorder", "splitorder",
                  "modifyorder", "cancelorder", "cancelallorder", "closeposition",
                  "optionsorder", "optionsmultiorder")
        return [m for m, _ in self.calls if m in writes] + \
               [e for e, _ in self.posts if "gtt" in e]


def make_settings(tmp: Path, **over) -> Settings:
    s = Settings.load()
    s.trading_enabled = True
    s.require_analyzer_mode = True
    s.max_order_value = 100_000.0
    s.max_order_quantity = 1000
    s.max_orders_per_session = 25
    s.max_price_deviation_pct = 20.0
    s.duplicate_order_window_sec = 0      # tests repeat identical orders on purpose
    s.allowed_exchanges = ["NSE", "NFO", "BSE", "BFO", "MCX"]
    s.allowed_products = ["MIS", "CNC", "NRML"]
    s.symbol_denylist = ["BANNEDCO"]
    s.kill_switch_file = tmp / "KILL"
    s.db_path = tmp / "test.db"
    s.audit_dir = tmp / "audit"
    s.audit_dir.mkdir(parents=True, exist_ok=True)
    for k, v in over.items():
        setattr(s, k, v)
    return s


def stub_kits(tmp: Path, client: StubClient, session: str = "test-session", **over):
    # session ids keep the audit assertions isolated: every stub toolkit in this file
    # shares one SQLite file, so rows are read back by session rather than by table.
    st = make_settings(tmp, **over)
    guard = RiskGuard(st, client)
    audit = AuditLog(st)
    common = dict(client=client, settings=st, guard=guard, audit=audit,
                  session_id=session)
    return OrderTools(**common), GttTools(**common), OptionsTools(**common), audit


def call(kit, name, **kw):
    return json.loads(kit.functions[name].entrypoint(**kw))


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_registration() -> list:
    print("\n=== toolkit registration ===")
    kits = [OrderTools(), GttTools(), OptionsTools()]
    total = 0
    for kit in kits:
        names = sorted(f.name for f in kit.functions.values())
        total += len(names)
        expected = sorted(EXPECTED[kit.name])
        check(f"{kit.name} registers {len(expected)} tools", names == expected,
              str(names) if names != expected else f"{len(names)}")
    check("17 order-path tools total", total == 17, str(total))
    return kits


def test_confirmation_gate(kits: list) -> None:
    print("\n=== confirmation gate (the safety test) ===")
    by_name = {k.name: k for k in kits}
    for kit_name, tool in MUST_BE_GATED:
        fn = by_name[kit_name].functions.get(tool)
        check(f"{tool} requires confirmation",
              fn is not None and fn.requires_confirmation is True,
              f"requires_confirmation={getattr(fn, 'requires_confirmation', 'MISSING')}")

    for kit_name, tool in MUST_NOT_BE_GATED:
        fn = by_name[kit_name].functions.get(tool)
        check(f"{tool} is not gated",
              fn is not None and not getattr(fn, "requires_confirmation", False),
              f"requires_confirmation={getattr(fn, 'requires_confirmation', 'MISSING')}")

    gated = {f.name for k in kits for f in k.functions.values()
             if getattr(f, "requires_confirmation", False)}
    check("exactly the 12 expected tools are gated",
          gated == {t for _, t in MUST_BE_GATED}, str(sorted(gated)))


def test_schemas(kits: list) -> None:
    print("\n=== schema generation ===")
    # description and parameters are populated LAZILY by process_entrypoint(), which the
    # Agent calls when it assembles its tool list. Straight after construction they are
    # empty, so trigger the same processing here.
    for kit in kits:
        for fn in kit.functions.values():
            fn.process_entrypoint()

    problems: list[str] = []
    described = 0
    for kit in kits:
        for fn in kit.functions.values():
            if fn.description:
                described += 1
            else:
                problems.append(f"{fn.name}: no description")
            props = (fn.parameters or {}).get("properties", {})
            for pname in inspect.signature(fn.entrypoint).parameters:
                if pname in ("self", "agent", "team", "run_context"):
                    continue
                if pname not in props:
                    problems.append(f"{fn.name}: arg {pname} missing from schema")
                elif "type" not in props[pname] and "anyOf" not in props[pname]:
                    problems.append(f"{fn.name}: arg {pname} has no type")
                elif not props[pname].get("description"):
                    problems.append(f"{fn.name}: arg {pname} has no description")
    check("every tool has a description", described == 17, f"{described}/17")
    check("every argument reached the schema typed and described", not problems,
          "; ".join(problems[:4]) if problems else "clean")

    order = next(k for k in kits if k.name == "order_tools")
    props = (order.functions["place_order"].parameters or {}).get("properties", {})
    check("place_order schema carries the six core fields",
          all(p in props for p in ("symbol", "action", "exchange", "quantity",
                                   "product", "price_type")), str(sorted(props)))
    required = (order.functions["place_order"].parameters or {}).get("required", [])
    check("place_order marks the unavoidable fields required",
          set(required) >= {"symbol", "action", "exchange", "quantity"}, str(required))


def test_risk_refusals(tmp: Path) -> None:
    print("\n=== RiskGuard refusals reach the broker never ===")
    client = StubClient()
    orders, gtt, options, _ = stub_kits(tmp, client)

    # an index exchange is never tradable
    r = call(orders, "place_order", symbol="NIFTY", exchange="NSE_INDEX",
             action="BUY", quantity=1)
    check("index exchange refused", r["ok"] is False and r["kind"] == "index_not_tradable",
          str(r.get("error"))[:70])
    check("refused order never reached the broker", client.order_calls == [],
          str(client.order_calls))

    # over the quantity cap
    r = call(orders, "place_order", symbol="SBIN", exchange="NSE", action="BUY",
             quantity=5000)
    check("quantity over the cap refused",
          r["ok"] is False and r["kind"] == "quantity_cap", str(r.get("error"))[:70])
    check("still no broker call", client.order_calls == [], str(client.order_calls))

    # over the notional cap: 2000 x 100 = 200,000 against a 100,000 limit
    client2 = StubClient(ltp=100.0)
    orders2, _, _, _ = stub_kits(tmp, client2, max_order_quantity=100_000)
    r = call(orders2, "place_order", symbol="SBIN", exchange="NSE", action="BUY",
             quantity=2000)
    check("notional cap refused", r["ok"] is False and r["kind"] == "notional_cap",
          str(r.get("error"))[:70])
    check("notional refusal never reached the broker", client2.order_calls == [])

    # the master switch
    client3 = StubClient()
    orders3, gtt3, options3, _ = stub_kits(tmp, client3, trading_enabled=False)
    r = call(orders3, "place_order", symbol="SBIN", exchange="NSE", action="BUY",
             quantity=1)
    check("TRADING_ENABLED=false refuses",
          r["ok"] is False and r["kind"] == "trading_disabled", str(r.get("kind")))
    r = call(orders3, "close_all_positions")
    check("close_all_positions refused by the destructive gate",
          r["ok"] is False and r["kind"] == "trading_disabled", str(r.get("kind")))
    r = call(orders3, "cancel_all_orders")
    check("cancel_all_orders refused by the destructive gate",
          r["ok"] is False and r["kind"] == "trading_disabled", str(r.get("kind")))
    r = call(gtt3, "place_gtt_order", symbol="SBIN", exchange="NSE", action="BUY",
             quantity=1, product="CNC", price_type="MARKET", triggerprice_sl=90)
    check("place_gtt_order refused", r["ok"] is False and r["kind"] == "trading_disabled",
          str(r.get("kind")))
    r = call(gtt3, "manage_gtt_order", operation="cancel", trigger_id="GTT-1")
    check("GTT cancel refused by the destructive gate",
          r["ok"] is False and r["kind"] == "trading_disabled", str(r.get("kind")))
    r = call(options3, "place_options_order", underlying="NIFTY", offset="ATM",
             option_type="CE", action="BUY", quantity=65)
    check("place_options_order refused",
          r["ok"] is False and r["kind"] == "trading_disabled", str(r.get("kind")))
    r = call(options3, "place_options_multi_order", underlying="NIFTY", legs=[
        {"offset": "ATM", "option_type": "CE", "action": "BUY", "quantity": 65}])
    check("place_options_multi_order refused",
          r["ok"] is False and r["kind"] == "trading_disabled", str(r.get("kind")))
    check("nothing at all reached the broker with trading disabled",
          client3.order_calls == [], str(client3.order_calls))

    # live mode while REQUIRE_ANALYZER_MODE is on
    client4 = StubClient(mode="live")
    orders4, _, _, _ = stub_kits(tmp, client4)
    r = call(orders4, "place_order", symbol="SBIN", exchange="NSE", action="BUY",
             quantity=1)
    check("live mode blocked while REQUIRE_ANALYZER_MODE",
          r["ok"] is False and r["kind"] == "live_mode_blocked", str(r.get("kind")))
    check("live-mode refusal never reached the broker", client4.order_calls == [])


def test_allowed_path(tmp: Path) -> None:
    print("\n=== allowed orders reach the broker with the right payload ===")
    client = StubClient()
    orders, gtt, options, _ = stub_kits(tmp, client)

    r = call(orders, "place_order", symbol="sbin", exchange="nse", action="buy",
             quantity=1, product="MIS", price_type="MARKET")
    check("place_order accepted", r["ok"] is True, str(r.get("data")))
    method, kw = client.calls[-1]
    check("place_order used the placeorder endpoint", method == "placeorder", method)
    check("place_order sends price_type at the top level, not pricetype",
          "price_type" in kw and "pricetype" not in kw, str(sorted(kw)))
    check("place_order stamps the strategy name",
          kw.get("strategy") == orders.settings.default_strategy_name,
          str(kw.get("strategy")))
    check("place_order normalizes symbol and exchange",
          kw["symbol"] == "SBIN" and kw["exchange"] == "NSE" and kw["action"] == "BUY")
    check("place_order reports the analyzer mode it ran under",
          r.get("analyzer_mode_at_send") == "analyze", str(r.get("analyzer_mode_at_send")))

    r = call(orders, "place_basket_order", orders=[
        {"symbol": "SBIN", "exchange": "NSE", "action": "BUY", "quantity": 1,
         "price_type": "LIMIT", "price": 100, "product": "MIS"}])
    check("place_basket_order accepted", r["ok"] is True)
    _, kw = client.calls[-1]
    leg = kw["orders"][0]
    check("basket leg uses pricetype with no underscore",
          "pricetype" in leg and "price_type" not in leg, str(sorted(leg)))
    check("basket leg kept the LIMIT price type", leg["pricetype"] == "LIMIT",
          leg["pricetype"])

    r = call(orders, "place_split_order", symbol="SBIN", exchange="NSE", action="SELL",
             quantity=100, splitsize=25)
    check("place_split_order accepted", r["ok"] is True)
    _, kw = client.calls[-1]
    check("split order sends price_type at the top level and a splitsize",
          "price_type" in kw and kw.get("splitsize") == "25", str(sorted(kw)))
    check("split order reports the expected child count",
          r.get("expected_children") == 4, str(r.get("expected_children")))

    r = call(orders, "place_smart_order", symbol="SBIN", exchange="NSE", action="BUY",
             quantity=10, position_size=10)
    check("place_smart_order accepted", r["ok"] is True)
    check("smart order surfaces the worst-case quantity",
          r.get("worst_case_quantity") == 20, str(r.get("worst_case_quantity")))

    r = call(orders, "modify_order", order_id="X1", symbol="SBIN", exchange="NSE",
             action="BUY", quantity=1, price=101.0, product="MIS", price_type="LIMIT")
    check("modify_order accepted", r["ok"] is True)
    _, kw = client.calls[-1]
    check("modify_order sends order_id in and reads orderid out",
          kw.get("order_id") == "X1", str(kw.get("order_id")))

    r = call(orders, "cancel_order", order_id="X1")
    check("cancel_order accepted", r["ok"] is True)
    r = call(orders, "cancel_all_orders")
    check("cancel_all_orders accepted", r["ok"] is True)
    r = call(orders, "close_all_positions")
    check("close_all_positions accepted", r["ok"] is True)

    r = call(gtt, "place_gtt_order", symbol="SBIN", exchange="NSE", action="BUY",
             quantity=1, product="CNC", price_type="LIMIT", price=95,
             triggerprice_sl=90)
    check("place_gtt_order accepted", r["ok"] is True, str(r.get("data")))
    endpoint, payload = client.posts[-1]
    check("GTT posts to placegttorder with no leading slash",
          endpoint == "placegttorder", endpoint)
    check("GTT payload uses pricetype and omits last_price",
          "pricetype" in payload and "last_price" not in payload, str(sorted(payload)))

    r = call(gtt, "manage_gtt_order", operation="list")
    check("manage_gtt_order list works", r["ok"] is True, client.posts[-1][0])
    r = call(gtt, "manage_gtt_order", operation="cancel", trigger_id="GTT-1")
    check("manage_gtt_order cancel works",
          r["ok"] is True and client.posts[-1][0] == "cancelgttorder",
          client.posts[-1][0])

    r = call(options, "place_options_order", underlying="NIFTY", offset="ATM",
             option_type="CE", action="BUY", quantity=65)
    check("place_options_order accepted", r["ok"] is True)
    check("options order resolved the concrete symbol first",
          r.get("resolved_symbol") == "NIFTY25AUG2624500CE", str(r.get("resolved_symbol")))
    method, kw = client.calls[-1]
    check("options order used optionsorder with price_type at the top level",
          method == "optionsorder" and "price_type" in kw, method)

    r = call(options, "place_options_multi_order", underlying="NIFTY", legs=[
        {"offset": "ATM", "option_type": "CE", "action": "SELL", "quantity": 65},
        {"offset": "OTM5", "option_type": "CE", "action": "BUY", "quantity": 65,
         "pricetype": "LIMIT", "price": 10}])
    check("place_options_multi_order accepted", r["ok"] is True)
    method, kw = client.calls[-1]
    check("multi order sends the required strategy",
          method == "optionsmultiorder" and kw.get("strategy"), str(kw.get("strategy")))
    check("multi order legs use pricetype and carry no annotations",
          all("price_type" not in leg and "resolved_symbol" not in leg
              for leg in kw["legs"]), str(kw["legs"]))


def test_validation(tmp: Path) -> None:
    print("\n=== model-fixable errors raise RetryAgentRun ===")
    client = StubClient()
    orders, gtt, options, _ = stub_kits(tmp, client)

    cases = [
        ("bad action", orders, "place_order",
         dict(symbol="SBIN", exchange="NSE", action="HOLD", quantity=1), "BUY or SELL"),
        ("bad price type", orders, "place_order",
         dict(symbol="SBIN", exchange="NSE", action="BUY", quantity=1,
              price_type="STOP"), "SL-M"),
        ("LIMIT without a price", orders, "place_order",
         dict(symbol="SBIN", exchange="NSE", action="BUY", quantity=1,
              price_type="LIMIT"), "positive price"),
        ("split over 100 children", orders, "place_split_order",
         dict(symbol="SBIN", exchange="NSE", action="BUY", quantity=1000, splitsize=1),
         "child orders"),
        ("GTT with MIS", gtt, "place_gtt_order",
         dict(symbol="SBIN", exchange="NSE", action="BUY", quantity=1, product="MIS",
              triggerprice_sl=90, price=95), "intraday"),
        ("GTT SINGLE with both triggers", gtt, "place_gtt_order",
         dict(symbol="SBIN", exchange="NSE", action="BUY", quantity=1, product="CNC",
              price=95, triggerprice_sl=90, triggerprice_tg=110), "exactly one"),
        ("GTT OCO with sl above tg", gtt, "place_gtt_order",
         dict(symbol="SBIN", exchange="NSE", action="BUY", quantity=1, product="CNC",
              trigger_type="OCO", triggerprice_sl=110, stoploss=109,
              triggerprice_tg=100, target=101), "below"),
        ("GTT with SL-M", gtt, "place_gtt_order",
         dict(symbol="SBIN", exchange="NSE", action="BUY", quantity=1, product="CNC",
              price_type="SL-M", triggerprice_sl=90), "LIMIT or MARKET"),
        ("GTT cancel without a trigger id", gtt, "manage_gtt_order",
         dict(operation="cancel"), "trigger_id"),
        ("unknown GTT operation", gtt, "manage_gtt_order",
         dict(operation="delete"), "operation must be"),
        ("bad offset", options, "place_options_order",
         dict(underlying="NIFTY", offset="ATM1", option_type="CE", action="BUY",
              quantity=65), "ATM"),
        ("bad option type", options, "place_options_order",
         dict(underlying="NIFTY", offset="ATM", option_type="CALL", action="BUY",
              quantity=65), "CE or PE"),
        ("too many option legs", options, "place_options_multi_order",
         dict(underlying="NIFTY", legs=[{"offset": "ATM", "option_type": "CE",
                                         "action": "BUY", "quantity": 65}] * 21),
         "at most 20"),
        ("empty order id", orders, "get_order_status", dict(order_id=""), "order_id"),
    ]
    for label, kit, tool, kwargs, needle in cases:
        try:
            call(kit, tool, **kwargs)
            check(f"{label} raises RetryAgentRun", False, "no exception")
        except RetryAgentRun as exc:
            check(f"{label} raises RetryAgentRun", needle.lower() in str(exc).lower(),
                  str(exc)[:70])
        except Exception as exc:  # noqa: BLE001
            check(f"{label} raises RetryAgentRun", False,
                  f"{type(exc).__name__}: {exc}")
    check("no validation failure reached the broker", client.order_calls == [],
          str(client.order_calls))


def test_audit(tmp: Path) -> None:
    print("\n=== audit rows ===")
    client = StubClient()
    orders, _, _, audit = stub_kits(tmp, client, session="audit-allowed")

    call(orders, "place_order", symbol="SBIN", exchange="NSE", action="BUY", quantity=1)
    rows = audit.recent(limit=10, session_id="audit-allowed")
    phases = [r["phase"] for r in rows]
    check("attempt and result rows both written",
          sorted(phases) == ["attempt", "result"], str(phases))
    result_row = next(r for r in rows if r["phase"] == "result")
    check("result row records the analyzer mode",
          result_row["analyzer_mode"] == "analyze", str(result_row["analyzer_mode"]))
    check("result row records the risk verdict",
          result_row["risk_verdict"] == "allowed", str(result_row["risk_verdict"]))
    check("result row captured the order id",
          "STUB-1" in (result_row["order_ids"] or ""), str(result_row["order_ids"]))
    check("attempt row precedes the broker call and names the tool",
          all(r["tool"] == "place_order" for r in rows))

    # a refusal must still leave a trail
    client2 = StubClient()
    orders2, _, _, audit2 = stub_kits(tmp, client2, session="audit-refused",
                                      trading_enabled=False)
    call(orders2, "place_order", symbol="SBIN", exchange="NSE", action="BUY", quantity=1)
    rows2 = audit2.recent(limit=10, session_id="audit-refused")
    refused = [r for r in rows2 if r["risk_verdict"] == "refused"]
    check("a refusal is audited too", len(refused) == 1,
          str(refused[0]["risk_reason"])[:60] if refused else "none")
    check("the refused attempt was recorded before the check ran",
          sorted(r["phase"] for r in rows2) == ["attempt", "result"],
          str([r["phase"] for r in rows2]))

    # auditing must never take a trade down
    client3 = StubClient()
    orders3, _, _, audit3 = stub_kits(tmp, client3, session="audit-broken")
    audit3.db_path = Path("Z:/definitely/not/a/path/x.db")
    audit3.audit_dir = Path("Z:/definitely/not/a/path")
    try:
        r = call(orders3, "place_order", symbol="INFY", exchange="NSE", action="BUY",
                 quantity=1)
        check("a broken audit sink does not block the order", r["ok"] is True)
    except Exception as exc:  # noqa: BLE001
        check("a broken audit sink does not block the order", False,
              f"{type(exc).__name__}: {exc}")


def test_gtt_unavailable(tmp: Path) -> None:
    print("\n=== GTT 501 handling ===")

    class GttUnsupportedClient(StubClient):
        def raw_post(self, endpoint, payload=None, _order=False):
            self.posts.append((endpoint, payload or {}))
            return {"ok": False, "source": endpoint,
                    "error": "HTTP 501: {\"status\":\"error\",\"message\":"
                             "\"Sandbox GTT support not yet implemented\"}",
                    "kind": "http_error"}

    client = GttUnsupportedClient()
    _, gtt, _, _ = stub_kits(tmp, client)

    r = call(gtt, "place_gtt_order", symbol="SBIN", exchange="NSE", action="BUY",
             quantity=1, product="CNC", price_type="LIMIT", price=95,
             triggerprice_sl=90)
    check("a 501 becomes a plain explanation, not a raw status line",
          r["ok"] is False and r["kind"] == "gtt_unavailable"
          and not str(r["error"]).lower().startswith("http 501"),
          str(r.get("error"))[:80])
    check("the explanation says GTT cannot be rehearsed",
          "rehearsed" in str(r["error"]).lower(), str(r["error"])[:60])
    check("the raw server message is still available for a human",
          "501" in str(r.get("server_message")), str(r.get("server_message"))[:40])

    r = call(gtt, "manage_gtt_order", operation="list")
    check("a 501 on the GTT book is explained too",
          r["ok"] is False and r["kind"] == "gtt_unavailable", str(r.get("kind")))


def test_never_empty(tmp: Path) -> None:
    print("\n=== every tool returns a non-empty JSON string ===")
    client = StubClient()
    orders, gtt, options, _ = stub_kits(tmp, client)
    payloads = [
        orders.functions["place_order"].entrypoint(
            symbol="SBIN", exchange="NSE", action="BUY", quantity=1),
        orders.functions["cancel_all_orders"].entrypoint(),
        gtt.functions["manage_gtt_order"].entrypoint(operation="list"),
        options.functions["place_options_order"].entrypoint(
            underlying="NIFTY", offset="ATM", option_type="CE", action="BUY",
            quantity=65),
    ]
    bad = [p for p in payloads if not isinstance(p, str) or not p.strip()]
    check("no tool returned a falsy payload", not bad, str(bad))
    check("every payload parses as JSON",
          all(isinstance(json.loads(p), dict) for p in payloads))


def test_live(tmp: Path) -> None:
    print("\n=== live calls ===")
    client = get_client()
    if not client.settings.openalgo_api_key or not client.ping().get("ok"):
        skip("live order tests", "OpenAlgo unreachable")
        return

    mode = client.analyzer_mode()
    if mode != "analyze":
        skip("live order tests", f"OpenAlgo is in {mode.upper()} MODE - refusing to "
                                 "place real orders from a test")
        print("  ***  analyzer mode is not 'analyze'. All order tests skipped.  ***")
        return

    st = make_settings(tmp, trading_enabled=True, require_analyzer_mode=True)
    guard = RiskGuard(st, client)
    audit = AuditLog(st)
    common = dict(client=client, settings=st, guard=guard, audit=audit,
                  session_id="live-test")
    orders = OrderTools(**common)
    options = OptionsTools(**common)

    # -- options reads
    r = call(options, "get_option_chain", underlying="NIFTY", exchange="NSE_INDEX",
             strike_count=3, with_greeks=True)
    chain = r.get("data", {}).get("chain", []) if r.get("ok") else []
    atm = next((row for row in chain if (row.get("ce") or {}).get("label") == "ATM"), None)
    check("get_option_chain returns strikes", r.get("ok") and len(chain) >= 3,
          f"{len(chain)} strikes, atm={r.get('data', {}).get('atm_strike')}")
    check("chain carries delta and implied volatility",
          atm is not None and atm["ce"].get("delta") is not None
          and atm["ce"].get("implied_volatility") is not None,
          f"delta={atm['ce'].get('delta')} iv={atm['ce'].get('implied_volatility')}"
          if atm else "no ATM row")

    r = call(options, "resolve_option_symbol", underlying="NIFTY", offset="ATM",
             option_type="CE", exchange="NSE_INDEX")
    resolved = (r.get("data") or {}).get("symbol", "")
    check("resolve_option_symbol returns a concrete NIFTY CE",
          r.get("ok") and resolved.startswith("NIFTY") and resolved.endswith("CE"),
          f"{resolved} lot={r.get('data', {}).get('lotsize')}")

    if resolved:
        r = call(options, "get_option_greeks", symbol=resolved, exchange="NFO")
        greeks = (r.get("data") or {}).get("greeks") or {}
        check("get_option_greeks single", r.get("ok") and "delta" in greeks,
              f"delta={greeks.get('delta')} iv={(r.get('data') or {}).get('implied_volatility')}")
        r = call(options, "get_option_greeks",
                 symbols=[{"symbol": resolved, "exchange": "NFO"}])
        check("get_option_greeks batch via multioptiongreeks",
              r.get("ok") and r["data"]["returned"] == 1,
              f"returned={r.get('data', {}).get('returned')}")

    r = call(options, "get_synthetic_future", underlying="NIFTY", exchange="NSE_INDEX")
    check("get_synthetic_future", r.get("ok")
          and (r.get("data") or {}).get("synthetic_future_price"),
          str((r.get("data") or {}).get("synthetic_future_price")))

    # -- a simulated order, start to finish
    # CNC rather than MIS: the sandbox rejects MIS after the 15:15 IST square-off time,
    # which would make this test pass only in the morning.
    print("  (analyzer mode is 'analyze' - the orders below are simulated)")
    r = call(orders, "place_order", symbol="SBIN", exchange="NSE", action="BUY",
             quantity=1, product="CNC", price_type="MARKET")
    order_id = str((r.get("data") or {}).get("orderid") or "")
    check("place_order returns an order id", bool(r.get("ok")) and bool(order_id),
          f"orderid={order_id} mode={r.get('mode')} err={r.get('error')}")
    check("the order response names the mode it ran under",
          r.get("analyzer_mode_at_send") == "analyze",
          str(r.get("analyzer_mode_at_send")))

    status = ""
    if order_id:
        r = call(orders, "get_order_status", order_id=order_id)
        status = str((r.get("data") or {}).get("order_status") or "")
        check("get_order_status finds the order", r.get("ok") and bool(status),
              f"status={status}")

        r = call(orders, "cancel_order", order_id=order_id)
        # A market order that already filled cannot be cancelled. Either answer is
        # acceptable as long as the tool reports it cleanly rather than throwing.
        check("cancel_order answers cleanly", isinstance(r.get("ok"), bool),
              str(r.get("error") or r.get("data"))[:70])

    if status == "complete":
        # Leave the sandbox as it was found. close_all_positions would square off
        # everything in the sandbox, so the exact opposite order is used instead.
        r = call(orders, "place_order", symbol="SBIN", exchange="NSE", action="SELL",
                 quantity=1, product="CNC", price_type="MARKET")
        check("the simulated position was flattened again", bool(r.get("ok")),
              str((r.get("data") or {}).get("orderid") or r.get("error"))[:60])

    try:
        call(orders, "get_order_status", order_id="NOSUCHORDER-999")
        check("an unknown order id raises RetryAgentRun", False, "no exception")
    except RetryAgentRun as exc:
        check("an unknown order id raises RetryAgentRun",
              "get_orderbook" in str(exc), str(exc)[:60])

    # audit trail for the live leg
    rows = audit.recent(limit=20, session_id="live-test")
    check("live orders were audited",
          len([x for x in rows if x["phase"] == "attempt"]) >= 1,
          f"{len(rows)} rows")


def main() -> int:
    print("Order toolkit tests")
    kits = test_registration()
    test_confirmation_gate(kits)
    test_schemas(kits)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        test_risk_refusals(tmp)
        test_allowed_path(tmp)
        test_validation(tmp)
        test_audit(tmp)
        test_gtt_unavailable(tmp)
        test_never_empty(tmp)
        test_live(tmp)

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
