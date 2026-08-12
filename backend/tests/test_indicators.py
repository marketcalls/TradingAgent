"""Indicator dispatcher and toolkit tests.

The dispatcher checks run on synthetic frames so they need no network. The toolkit
checks hit live candles when OpenAlgo is reachable.

Run:  python backend/tests/test_indicators.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.config import setup_logging  # noqa: E402

setup_logging("WARNING")

from agno.exceptions import RetryAgentRun  # noqa: E402

from app.indicators.compute import (  # noqa: E402
    IndicatorError, compute, required_bars, search_specs, spec_to_dict,
)
from app.indicators.descriptions import DESCRIPTIONS  # noqa: E402
from app.indicators.registry import REGISTRY, get_spec  # noqa: E402
from app.openalgo.client import get_client  # noqa: E402
from app.tools.indicators import IndicatorTools, _parse_condition  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    results.append((name, status))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    results.append((name, SKIP))
    print(f"  [{SKIP}] {name}" + (f" - {detail}" if detail else ""))


def synthetic(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0.1, 1.5, n)
    low = close - rng.uniform(0.1, 1.5, n)
    open_ = close + rng.normal(0, 0.4, n)
    vol = rng.uniform(1e5, 5e5, n)
    idx = pd.date_range("2026-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


def test_descriptions() -> None:
    print("\n=== descriptions ===")
    missing = [n for n in REGISTRY if n not in DESCRIPTIONS]
    check("every one of the 127 indicators has a description", not missing,
          f"missing: {missing[:6]}" if missing else f"{len(DESCRIPTIONS)} descriptions")
    extra = [n for n in DESCRIPTIONS if n not in REGISTRY]
    check("no description without a registry entry", not extra, str(extra[:5]))
    # the gap the registry agent flagged: searching by intent must work
    check("search finds bbands by the word bollinger",
          any(s.name == "bbands" for s in search_specs("bollinger")))
    check("search finds mfi by the phrase money flow",
          any(s.name == "mfi" for s in search_specs("money flow")))
    check("search finds adx by trend strength",
          any(s.name == "adx" for s in search_specs("trend strength")))
    check("category filter works",
          all(s.category == "volume" for s in search_specs(category="volume")))


def test_compute() -> None:
    print("\n=== compute dispatcher ===")
    df = synthetic()

    r = compute("rsi", df, {"period": 14}, last_n=5)
    check("rsi computes", r["outputs"] == ["rsi"] and len(r["values"]["rsi"]) == 5,
          f"latest={r['summary']['rsi']['latest']}")
    check("rsi latest is in range 0-100",
          0 <= r["summary"]["rsi"]["latest"] <= 100)
    check("result carries a description", bool(r["description"]))

    r = compute("macd", df, last_n=3)
    check("macd returns three named outputs",
          r["outputs"] == ["macd_line", "signal_line", "histogram"], str(r["outputs"]))

    r = compute("adx", df, last_n=3)
    check("adx puts the adx line third",
          r["outputs"] == ["plus_di", "minus_di", "adx"], str(r["outputs"]))

    r = compute("supertrend", df, {"period": 10, "multiplier": 3.0}, last_n=3)
    check("supertrend returns value and direction",
          r["outputs"] == ["supertrend", "direction"], str(r["outputs"]))

    r = compute("psar", df, last_n=3)
    check("psar returns a single series", len(r["outputs"]) == 1, str(r["outputs"]))

    # the int coercion that JSON tool arguments make necessary
    r = compute("sma", df, {"period": 20.0}, last_n=2)
    check("float period 20.0 is coerced to int", r["params_used"]["period"] == 20,
          str(r["params_used"]["period"]))
    try:
        compute("sma", df, {"period": 20.5})
        check("non-integer period rejected", False, "no error")
    except IndicatorError as exc:
        check("non-integer period rejected", "whole number" in str(exc), str(exc)[:50])
    try:
        compute("sma", df, {"period": -5})
        check("negative period rejected", False, "no error")
    except IndicatorError as exc:
        check("negative period rejected", "positive" in str(exc))

    # enums
    try:
        compute("po", df, {"ma_type": "WMA"})
        check("invalid enum rejected", False, "no error")
    except IndicatorError as exc:
        check("invalid enum rejected", "must be one of" in str(exc), str(exc)[:60])
    r = compute("po", df, {"ma_type": "EMA"}, last_n=2)
    check("valid enum accepted", r["params_used"]["ma_type"] == "EMA")

    # unknown parameter
    try:
        compute("rsi", df, {"windows": 14})
        check("unknown parameter rejected", False, "no error")
    except IndicatorError as exc:
        check("unknown parameter rejected", "no parameter" in str(exc), str(exc)[:50])

    # broken indicators must explain themselves
    for broken in ("vi", "ulcerindex"):
        try:
            compute(broken, df)
            check(f"{broken} refused with an explanation", False, "no error")
        except IndicatorError as exc:
            check(f"{broken} refused with an explanation",
                  "not usable" in str(exc), str(exc)[:55])

    # alias resolves to the real implementation
    r = compute("uo_oscillator", df, last_n=2)
    check("alias resolves to ultimate_oscillator",
          r["indicator"] == "ultimate_oscillator", r["indicator"])

    # conditional arity
    r = compute("obv_smoothed", df, {"ma_type": "SMA + Bollinger Bands"}, last_n=2)
    check("obv_smoothed conditional arity gives three outputs",
          len(r["outputs"]) == 3, str(r["outputs"]))
    r = compute("obv_smoothed", df, {"ma_type": "EMA"}, last_n=2)
    check("obv_smoothed default arity gives one output", len(r["outputs"]) == 1)
    r = compute("variance", df, {"return_components": True}, last_n=2)
    check("variance with components gives five outputs", len(r["outputs"]) == 5,
          str(r["outputs"]))

    # two-series indicators
    try:
        compute("correlation", df, {"period": 20})
        check("correlation without a second series is refused", False, "no error")
    except IndicatorError as exc:
        check("correlation without a second series is refused",
              "second symbol" in str(exc), str(exc)[:50])
    other = synthetic(400)["close"] * 1.1
    r = compute("correlation", df, {"period": 20}, second_series=other, last_n=3)
    check("correlation with a second series computes",
          r["summary"]["correlation"]["finite_count"] > 0,
          f"latest={r['summary']['correlation']['latest']}")

    # multi-input traps
    r = compute("bop", df, last_n=2)
    check("bop uses full OHLC", r["summary"]["bop"]["finite_count"] > 0)
    r = compute("emv", df, last_n=2)
    check("emv uses high, low and volume", r["summary"]["emv"]["finite_count"] > 0)
    r = compute("rvol", df, last_n=2)
    check("rvol uses volume only", r["summary"]["rvol"]["finite_count"] > 0)
    r = compute("frama", df, last_n=2)
    check("frama uses high and low", r["summary"]["frama"]["finite_count"] > 0)

    # unknown name
    try:
        compute("bollinger", df)
        check("unknown indicator rejected", False, "no error")
    except IndicatorError as exc:
        check("unknown indicator rejected", "Unknown indicator" in str(exc))

    # NaN must serialize to null, never bare NaN
    r = compute("beta", synthetic(120), {"period": 60},
                second_series=synthetic(120)["close"], last_n=5)
    text = json.dumps(r)
    check("NaN serializes as null and stays valid JSON",
          "NaN" not in text and json.loads(text) is not None)

    # warm-up padding
    check("required_bars pads for beta's 253-bar warm-up",
          required_bars(get_spec("beta"), {}, 10) > 250,
          str(required_bars(get_spec("beta"), {}, 10)))
    check("required_bars is modest for rsi",
          required_bars(get_spec("rsi"), {}, 10) < 100,
          str(required_bars(get_spec("rsi"), {}, 10)))
    check("required_bars grows with a larger period",
          required_bars(get_spec("sma"), {"period": 200}, 10)
          > required_bars(get_spec("sma"), {"period": 20}, 10))

    # spec_to_dict
    d = spec_to_dict(get_spec("macd"), verbose=True)
    check("verbose spec exposes parameters and outputs",
          "parameters" in d and d["outputs"] == ["macd_line", "signal_line", "histogram"])


def test_condition_parser() -> None:
    print("\n=== condition parser ===")
    check("parses a less-than comparison",
          _parse_condition("rsi < 30") == {"kind": "compare", "left": "rsi",
                                           "op": "<", "right": 30.0})
    check("parses a greater-or-equal comparison",
          (_parse_condition("adx >= 25.5") or {}).get("right") == 25.5)
    check("parses a crossover call",
          (_parse_condition("crossover(macd_line, signal_line)") or {}).get("fn")
          == "crossover")
    check("parses crossunder", (_parse_condition("crossunder(a, b)") or {}).get("fn")
          == "crossunder")
    # anything resembling code must be refused, since there is no eval anywhere
    for bad in ["__import__('os').system('ls')", "rsi < 30 and 1==1", "eval(x)",
                "rsi<", "", "os.remove('/')", "rsi + 1 < 30"]:
        check(f"rejects {bad[:26]!r}", _parse_condition(bad) is None)


def test_toolkit_live() -> None:
    print("\n=== toolkit (live) ===")
    kit = IndicatorTools()
    names = sorted(f.name for f in kit.functions.values())
    check("5 indicator tools registered",
          names == ["compute_indicator", "compute_indicators_batch",
                    "describe_indicator", "list_indicators", "scan_symbols"], str(names))

    for fn in kit.functions.values():
        fn.process_entrypoint()
    check("all indicator tools have descriptions",
          all(f.description for f in kit.functions.values()))

    def call(tool, **kw):
        # parameter is 'tool', not 'name': describe_indicator takes a 'name' argument
        return json.loads(kit.functions[tool].entrypoint(**kw))

    r = call("list_indicators")
    check("list_indicators returns all 127", r["data"]["total"] == 127,
          str(r["data"]["total"]))
    r = call("list_indicators", query="bollinger")
    check("list_indicators finds bollinger by keyword",
          any(i["name"] == "bbands" for i in r["data"]["indicators"]),
          f"{r['data']['total']} matches")
    r = call("list_indicators", category="hybrid")
    check("list_indicators filters by category", r["data"]["total"] == 7,
          str(r["data"]["total"]))

    r = call("describe_indicator", name="macd")
    check("describe_indicator gives parameters and outputs",
          r["data"]["outputs"] == ["macd_line", "signal_line", "histogram"]
          and "fast_period" in r["data"]["parameters"])

    try:
        call("describe_indicator", name="bollinger")
        check("unknown name suggests alternatives", False, "no error")
    except RetryAgentRun as exc:
        check("unknown name suggests alternatives", "Did you mean" in str(exc),
              str(exc)[:60])

    client = get_client()
    if not client.settings.openalgo_api_key or not client.ping().get("ok"):
        skip("live indicator computation", "OpenAlgo unreachable")
        return

    r = call("compute_indicator", symbol="SBIN", exchange="NSE", indicator="rsi",
             interval="5m", params={"period": 14}, last_n=3)
    latest = r["data"]["summary"]["rsi"]["latest"]
    check("compute_indicator rsi on live data",
          r["ok"] and latest is not None and 0 <= latest <= 100,
          f"rsi={latest} over {r['data']['bars_used']} bars")

    r = call("compute_indicator", symbol="SBIN", exchange="NSE", indicator="supertrend",
             interval="15m", last_n=2)
    check("compute_indicator supertrend on live data",
          r["ok"] and r["data"]["summary"]["supertrend"]["latest"] is not None,
          f"st={r['data']['summary']['supertrend']['latest']} "
          f"dir={r['data']['summary']['direction']['latest']}")

    # a JSON float period must survive, since tool args decode as floats
    r = call("compute_indicator", symbol="SBIN", exchange="NSE", indicator="sma",
             interval="5m", params={"period": 50.0}, last_n=2)
    check("float period from JSON args works live",
          r["ok"] and r["data"]["params_used"]["period"] == 50)

    r = call("compute_indicators_batch", symbol="SBIN", exchange="NSE",
             indicators=["rsi", {"name": "macd"},
                         {"name": "sma", "params": {"period": 50}}, "adx"],
             interval="5m", last_n=2)
    check("batch computes four indicators on one fetch",
          r["ok"] and len(r["data"]["results"]) == 4,
          f"{len(r['data']['results'])} results, {r['data']['bars_used']} bars")

    r = call("compute_indicators_batch", symbol="SBIN", exchange="NSE",
             indicators=["rsi", "vi"], interval="5m", last_n=2)
    check("batch reports a broken indicator without failing the call",
          r["ok"] and len(r["data"]["results"]) == 1 and r["data"]["failed"],
          str(r["data"]["failed"][0]["indicator"]))

    r = call("scan_symbols", symbols=["SBIN", "RELIANCE", "INFY", "TCS"],
             exchange="NSE", indicator="rsi", condition="rsi < 70", interval="15m")
    check("scan_symbols screens a watchlist",
          r["ok"] and r["data"]["scanned"] >= 3,
          f"{r['data']['matched_count']} of {r['data']['scanned']} matched")

    r = call("scan_symbols", symbols=["SBIN", "RELIANCE"], exchange="NSE",
             indicator="macd", condition="crossover(macd_line, signal_line)",
             interval="15m")
    check("scan_symbols handles a crossover condition",
          r["ok"] and r["data"]["scanned"] == 2,
          f"{r['data']['matched_count']} matched")

    try:
        call("scan_symbols", symbols=["SBIN"], exchange="NSE", indicator="rsi",
             condition="__import__('os')")
        check("scan_symbols refuses a code-like condition", False, "no error")
    except RetryAgentRun as exc:
        check("scan_symbols refuses a code-like condition",
              "Cannot read the condition" in str(exc), str(exc)[:50])

    # correlation needs the second symbol, and should work when given one
    r = call("compute_indicator", symbol="SBIN", exchange="NSE", indicator="correlation",
             interval="D", params={"period": 20}, compare_symbol="NIFTY",
             compare_exchange="NSE_INDEX", last_n=3)
    check("correlation against an index works",
          r["ok"] and r["data"]["summary"]["correlation"]["latest"] is not None,
          f"corr={r['data']['summary']['correlation']['latest']}")


def main() -> int:
    print("Indicator dispatcher and toolkit tests")
    test_descriptions()
    test_compute()
    test_condition_parser()
    test_toolkit_live()

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
