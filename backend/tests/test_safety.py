"""Safety layer tests: every RiskGuard limit, and the audit trail.

These run entirely against a stub client so they need neither a broker nor a network,
and they must pass before any order tool is considered done.

Run:  python backend/tests/test_safety.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import Settings  # noqa: E402
from app.safety.audit import AuditLog, _extract_order_ids  # noqa: E402
from app.safety.risk import RiskGuard  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    results.append((name, status))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


class StubClient:
    """Stands in for OpenAlgoClient. No network."""

    def __init__(self, mode: str = "analyze", ltp: float | None = 100.0) -> None:
        self._mode = mode
        self._ltp = ltp

    def analyzer_mode(self) -> str:
        return self._mode

    def ltp(self, symbol: str, exchange: str) -> float | None:
        return self._ltp


def make_settings(tmp: Path, **over) -> Settings:
    s = Settings.load()
    s.trading_enabled = True
    s.require_analyzer_mode = True
    s.max_order_value = 100_000.0
    s.max_order_quantity = 1000
    s.max_orders_per_session = 25
    s.max_price_deviation_pct = 20.0
    s.duplicate_order_window_sec = 10
    s.allowed_exchanges = ["NSE", "NFO", "BSE", "MCX"]
    s.allowed_products = ["MIS", "CNC", "NRML"]
    s.symbol_denylist = ["BANNEDCO"]
    s.kill_switch_file = tmp / "KILL"
    s.db_path = tmp / "test.db"
    s.audit_dir = tmp / "audit"
    s.audit_dir.mkdir(parents=True, exist_ok=True)
    for k, v in over.items():
        setattr(s, k, v)
    return s


BASE = dict(symbol="SBIN", exchange="NSE", action="BUY", quantity=10,
            product="MIS", price_type="MARKET", price=0)


def test_risk(tmp: Path) -> None:
    print("\n=== RiskGuard ===")
    st = make_settings(tmp)

    g = RiskGuard(st, StubClient())
    check("a normal order passes", g.check_order(**BASE).allowed)

    # kill switch
    g = RiskGuard(st, StubClient())
    st.kill_switch_file.write_text("stop")
    v = g.check_order(**BASE)
    check("kill switch blocks", not v.allowed and v.code == "kill_switch", v.code)
    st.kill_switch_file.unlink()
    check("removing kill switch resumes", RiskGuard(st, StubClient()).check_order(**BASE).allowed)

    # master switch
    g = RiskGuard(make_settings(tmp, trading_enabled=False), StubClient())
    v = g.check_order(**BASE)
    check("TRADING_ENABLED=false blocks", not v.allowed and v.code == "trading_disabled", v.code)

    # analyzer mode
    g = RiskGuard(make_settings(tmp), StubClient(mode="live"))
    v = g.check_order(**BASE)
    check("live mode blocked while REQUIRE_ANALYZER_MODE", not v.allowed
          and v.code == "live_mode_blocked", v.code)
    g = RiskGuard(make_settings(tmp, require_analyzer_mode=False), StubClient(mode="live"))
    check("live mode allowed once the flag is off", g.check_order(**BASE).allowed)

    # exchanges
    g = RiskGuard(make_settings(tmp), StubClient())
    v = g.check_order(**{**BASE, "symbol": "NIFTY", "exchange": "NSE_INDEX"})
    check("index exchange refused", not v.allowed and v.code == "index_not_tradable", v.code)
    g = RiskGuard(make_settings(tmp), StubClient())
    v = g.check_order(**{**BASE, "exchange": "CDS"})
    check("exchange outside allowlist refused", not v.allowed
          and v.code == "exchange_not_allowed", v.code)

    # product
    g = RiskGuard(make_settings(tmp), StubClient())
    v = g.check_order(**{**BASE, "product": "BO"})
    check("product outside allowlist refused", not v.allowed
          and v.code == "product_not_allowed", v.code)

    # symbol
    g = RiskGuard(make_settings(tmp), StubClient())
    v = g.check_order(**{**BASE, "symbol": "BANNEDCO"})
    check("denylisted symbol refused", not v.allowed and v.code == "symbol_denied", v.code)
    g = RiskGuard(make_settings(tmp), StubClient())
    v = g.check_order(**{**BASE, "symbol": ""})
    check("empty symbol refused", not v.allowed and v.code == "missing_symbol", v.code)

    # quantity
    for qty, code in [(0, "bad_quantity"), (-5, "bad_quantity"),
                      ("abc", "bad_quantity"), (5000, "quantity_cap")]:
        g = RiskGuard(make_settings(tmp), StubClient())
        v = g.check_order(**{**BASE, "quantity": qty})
        check(f"quantity {qty!r} refused", not v.allowed and v.code == code, v.code)

    # notional cap: 2000 x 100 = 200,000 > 100,000
    g = RiskGuard(make_settings(tmp, max_order_quantity=100_000), StubClient(ltp=100.0))
    v = g.check_order(**{**BASE, "quantity": 2000})
    check("notional cap refused", not v.allowed and v.code == "notional_cap", v.code)

    # fat finger: ltp 100, limit 150 = 50 percent away
    g = RiskGuard(make_settings(tmp), StubClient(ltp=100.0))
    v = g.check_order(**{**BASE, "price_type": "LIMIT", "price": 150, "quantity": 1})
    check("fat-finger limit price refused", not v.allowed
          and v.code == "price_deviation", v.code)
    g = RiskGuard(make_settings(tmp), StubClient(ltp=100.0))
    check("limit price within guard passes",
          g.check_order(**{**BASE, "price_type": "LIMIT", "price": 105, "quantity": 1}).allowed)
    g = RiskGuard(make_settings(tmp), StubClient(ltp=100.0))
    v = g.check_order(**{**BASE, "price_type": "LIMIT", "price": 0, "quantity": 1})
    check("LIMIT with zero price refused", not v.allowed and v.code == "bad_price", v.code)

    # no quote available must not crash the guard
    g = RiskGuard(make_settings(tmp), StubClient(ltp=None))
    check("missing quote does not block a market order", g.check_order(**BASE).allowed)

    # duplicates
    g = RiskGuard(make_settings(tmp), StubClient())
    check("first order accepted", g.check_order(**BASE).allowed)
    v = g.check_order(**BASE)
    check("identical repeat refused", not v.allowed and v.code == "duplicate", v.code)
    check("different quantity is not a duplicate",
          g.check_order(**{**BASE, "quantity": 11}).allowed)
    g2 = RiskGuard(make_settings(tmp, duplicate_order_window_sec=0), StubClient())
    g2.check_order(**BASE)
    check("duplicate window of 0 disables the check", g2.check_order(**BASE).allowed)

    # session cap
    g = RiskGuard(make_settings(tmp, max_orders_per_session=3,
                                duplicate_order_window_sec=0), StubClient())
    for i in range(3):
        g.check_order(**{**BASE, "quantity": 10 + i}, session_id="s1")
    v = g.check_order(**{**BASE, "quantity": 99}, session_id="s1")
    check("session cap refused after 3", not v.allowed and v.code == "session_cap", v.code)
    check("a different session is unaffected",
          g.check_order(**{**BASE, "quantity": 99}, session_id="s2").allowed)
    check("session counter", g.session_order_count("s1") == 3, str(g.session_order_count("s1")))

    # action
    g = RiskGuard(make_settings(tmp), StubClient())
    v = g.check_order(**{**BASE, "action": "HOLD"})
    check("invalid action refused", not v.allowed and v.code == "bad_action", v.code)

    # baskets
    g = RiskGuard(make_settings(tmp), StubClient(ltp=100.0))
    legs = [{"symbol": "SBIN", "exchange": "NSE", "action": "BUY", "quantity": 10,
             "product": "MIS", "pricetype": "MARKET"},
            {"symbol": "INFY", "exchange": "NSE", "action": "SELL", "quantity": 10,
             "product": "MIS", "pricetype": "MARKET"}]
    check("valid basket passes", g.check_basket(legs).allowed)
    g = RiskGuard(make_settings(tmp), StubClient(ltp=100.0))
    v = g.check_basket([])
    check("empty basket refused", not v.allowed and v.code == "empty_basket", v.code)
    g = RiskGuard(make_settings(tmp), StubClient(ltp=100.0))
    bad_legs = legs + [{"symbol": "NIFTY", "exchange": "NSE_INDEX", "action": "BUY",
                        "quantity": 10, "product": "MIS", "pricetype": "MARKET"}]
    v = g.check_basket(bad_legs)
    check("basket with an index leg refused", not v.allowed
          and v.code == "index_not_tradable", v.code)
    g = RiskGuard(make_settings(tmp), StubClient(ltp=100.0))
    v = g.check_basket([{"symbol": "SBIN", "exchange": "NSE", "action": "BUY",
                         "quantity": 600, "product": "MIS", "pricetype": "MARKET"},
                        {"symbol": "INFY", "exchange": "NSE", "action": "BUY",
                         "quantity": 600, "product": "MIS", "pricetype": "MARKET"}])
    check("basket combined notional cap refused", not v.allowed
          and v.code == "notional_cap", v.code)

    # destructive operations
    g = RiskGuard(make_settings(tmp), StubClient())
    check("close_all passes in analyze mode", g.check_destructive("close_all_positions").allowed)
    g = RiskGuard(make_settings(tmp), StubClient(mode="live"))
    v = g.check_destructive("close_all_positions")
    check("close_all blocked in live mode", not v.allowed
          and v.code == "live_mode_blocked", v.code)
    st2 = make_settings(tmp)
    st2.kill_switch_file.write_text("stop")
    g = RiskGuard(st2, StubClient())
    v = g.check_destructive("cancel_all_orders")
    check("kill switch blocks destructive ops", not v.allowed and v.code == "kill_switch")
    st2.kill_switch_file.unlink()

    # the refusal message must be usable by the model
    g = RiskGuard(make_settings(tmp), StubClient())
    v = g.check_order(**{**BASE, "symbol": "BANNEDCO"})
    check("refusal message names the code and reason",
          "symbol_denied" in v.as_message() and "DENYLIST" in v.as_message().upper(),
          v.as_message()[:70])


def test_audit(tmp: Path) -> None:
    print("\n=== AuditLog ===")
    st = make_settings(tmp)
    audit = AuditLog(st)

    aid = audit.record("attempt", "place_order", {"symbol": "SBIN", "quantity": 10},
                       session_id="s1", run_id="r1", analyzer_mode="analyze",
                       risk_verdict="allowed")
    check("attempt recorded", bool(aid))

    audit.record("result", "place_order", {"symbol": "SBIN", "quantity": 10},
                 session_id="s1", run_id="r1", analyzer_mode="analyze",
                 risk_verdict="allowed", ok=True,
                 response={"ok": True, "data": {"orderid": "250408000989443"}})

    rows = audit.recent(limit=10)
    check("two rows written", len(rows) == 2, f"{len(rows)} rows")
    check("phases recorded", {r["phase"] for r in rows} == {"attempt", "result"})
    check("order id extracted", "250408000989443" in (rows[0]["order_ids"] or ""),
          rows[0]["order_ids"])

    jsonl = list((tmp / "audit").glob("orders-*.jsonl"))
    check("jsonl written", len(jsonl) == 1 and jsonl[0].read_text().count("\n") == 2)

    check("session filter", len(audit.recent(session_id="s1")) == 2)
    check("session filter excludes others", len(audit.recent(session_id="nope")) == 0)

    check("order ids from a basket response",
          _extract_order_ids({"data": {"results": [{"orderid": "1"}, {"orderid": "2"}]}})
          == ["1", "2"])
    check("order ids from a flat response",
          _extract_order_ids({"data": {"orderid": "9"}}) == ["9"])
    check("order ids from a GTT response",
          _extract_order_ids({"data": {"trigger_id": "T1"}}) == ["T1"])
    check("no order ids in a plain dict", _extract_order_ids({"data": {}}) == [])

    # auditing must never take a trade down
    broken = AuditLog(st)
    broken.db_path = Path("Z:/definitely/not/a/path/x.db")
    broken.audit_dir = Path("Z:/definitely/not/a/path")
    try:
        broken.record("attempt", "place_order", {"x": 1})
        check("audit failure is swallowed", True)
    except Exception as exc:  # noqa: BLE001
        check("audit failure is swallowed", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    print("Safety layer tests")
    # ignore_cleanup_errors: on Windows sqlite keeps a handle on test.db past the last
    # connection close, so rmtree raises WinError 32 after every check has already passed.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        test_risk(tmp)
        test_audit(tmp)

    n_pass = sum(1 for _, s in results if s == PASS)
    n_fail = sum(1 for _, s in results if s == FAIL)
    print("\n=== Summary ===")
    for name, status in results:
        if status == FAIL:
            print(f"  FAILED: {name}")
    print(f"  {n_pass} passed, {n_fail} failed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
