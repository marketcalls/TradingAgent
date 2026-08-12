"""Client layer tests: constants, normalize, client, frames.

Pure-logic checks run always. Live checks run only when OpenAlgo is reachable, and are
reported as SKIP otherwise so this file is useful without a running broker session.

Run:  python backend/tests/test_client_layer.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.openalgo import constants as C  # noqa: E402
from app.openalgo.client import get_client  # noqa: E402
from app.openalgo.frames import get_frame_cache  # noqa: E402
from app.openalgo.normalize import (  # noqa: E402
    envelope, err, frame_summary, is_success, ok, to_json,
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    results.append((name, status))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    results.append((name, SKIP))
    print(f"  [{SKIP}] {name}" + (f" - {detail}" if detail else ""))


def test_constants() -> None:
    print("\n=== constants ===")
    check("index exchanges are not tradable",
          all(x not in C.TRADABLE_EXCHANGES for x in C.INDEX_EXCHANGES))
    check("is_index_exchange", C.is_index_exchange("nse_index") and not C.is_index_exchange("NSE"))
    check("price type SLM normalizes to SL-M", C.normalize_price_type("slm") == "SL-M")
    check("price type SL_M normalizes to SL-M", C.normalize_price_type("SL_M") == "SL-M")
    check("offset ATM valid", C.is_valid_offset("ATM"))
    check("offset OTM50 valid", C.is_valid_offset("otm50"))
    check("offset OTM51 rejected", not C.is_valid_offset("OTM51"))
    check("offset OTM0 rejected", not C.is_valid_offset("OTM0"))
    check("offset garbage rejected", not C.is_valid_offset("NEAR"))
    check("GTT products narrower than regular",
          set(C.GTT_PRODUCTS) < set(C.PRODUCTS))


def test_normalize() -> None:
    print("\n=== normalize ===")
    check("to_json never empty", to_json("") == '{"ok":false,"error":"no_data"}')

    # Oversized payloads are wrapped in a well-formed envelope rather than cut
    # mid-string, so the result stays bounded AND parseable.
    capped = to_json("x" * 50_000, limit=100)
    check("to_json caps size", len(capped) < 1000, f"{len(capped)} chars")
    parsed_cap = json.loads(capped)
    check("a truncated result is still valid JSON",
          parsed_cap["truncated"] is True and parsed_cap["dropped_chars"] == 49_900
          and len(parsed_cap["partial_text"]) == 100,
          f"dropped={parsed_cap['dropped_chars']}")

    nan_payload = {"a": float("nan"), "b": [1.0, float("inf")], "c": {"d": float("nan")}}
    text = to_json(nan_payload)
    parsed = json.loads(text)  # would raise if NaN leaked through
    check("NaN becomes null and stays valid JSON",
          parsed["a"] is None and parsed["b"][1] is None and parsed["c"]["d"] is None)

    check("is_success", is_success({"status": "success"}) and not is_success({"status": "error"}))
    check("ok shape", ok({"x": 1}, "src")["ok"] is True)
    check("err shape", err("boom", "src")["ok"] is False)

    # the four real OpenAlgo response shapes
    e1 = envelope({"status": "success", "data": {"ltp": 100}}, "quotes")
    check("envelope {status,data}", e1["ok"] and e1["data"]["ltp"] == 100)

    e2 = envelope({"status": "success", "orderid": "123"}, "placeorder")
    check("envelope flat orderid", e2["ok"] and e2["data"]["orderid"] == "123")

    e3 = envelope({"status": "success", "results": [{"orderid": "1"}, {"orderid": "2"}]},
                  "basketorder")
    check("envelope results list", e3["ok"] and len(e3["data"]["results"]) == 2)

    e4 = envelope({"status": "error", "message": "bad symbol"}, "quotes")
    check("envelope error", not e4["ok"] and e4["error"] == "bad symbol")

    e5 = envelope({"status": "success", "data": [], "mode": "analyze"}, "positionbook")
    check("envelope carries mode", e5.get("mode") == "analyze")

    check("envelope None", not envelope(None, "x")["ok"])


def test_frame_summary() -> None:
    print("\n=== frame_summary ===")
    import numpy as np
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=50, freq="5min")
    df = pd.DataFrame({
        "open": np.linspace(100, 110, 50), "high": np.linspace(101, 111, 50),
        "low": np.linspace(99, 109, 50), "close": np.linspace(100, 110, 50),
        "volume": np.arange(50) * 100.0,
    }, index=idx)
    s = frame_summary(df, last_n=3)
    check("summary bar count", s["bars"] == 50)
    check("summary tail size", len(s["recent"]) == 3)
    check("summary change pct", s["period_change_pct"] == 10.0, f"{s['period_change_pct']}")
    check("summary of empty frame", frame_summary(df.iloc[0:0])["bars"] == 0)


def test_client_live() -> None:
    print("\n=== client (live) ===")
    client = get_client()
    if not client.settings.openalgo_api_key:
        skip("live client checks", "no OPENALGO_API_KEY")
        return

    res = client.ping()
    if not res.get("ok"):
        skip("live client checks", f"OpenAlgo unreachable: {res.get('error')}")
        return
    check("ping via raw_post", res["ok"], f"broker={res.get('data', {}).get('broker')}")

    # The trap this layer exists to prevent: a leading slash 308-redirects.
    slashed = client.raw_post("/ping")
    check("raw_post strips leading slash", slashed.get("ok") is True,
          "no 308" if slashed.get("ok") else str(slashed.get("error"))[:60])

    mode = client.analyzer_mode()
    check("analyzer_mode", mode in ("analyze", "live"), f"mode={mode}")

    q = client.call_enveloped("quotes", symbol="RELIANCE", exchange="NSE")
    check("quotes enveloped", q["ok"] and q["data"].get("ltp") is not None,
          f"ltp={q.get('data', {}).get('ltp')}")

    ltp = client.ltp("SBIN", "NSE")
    check("ltp helper", isinstance(ltp, float) and ltp > 0, f"{ltp}")

    bad = client.call_enveloped("quotes", symbol="NOSUCHSYMBOL123", exchange="NSE")
    check("bad symbol returns ok=false, no raise", bad["ok"] is False,
          str(bad.get("error"))[:60])

    missing = client.call_enveloped("quotes", symbol="SBIN")  # exchange omitted
    check("missing kwarg becomes an error dict", missing["ok"] is False,
          str(missing.get("kind")))

    unknown = client.raw_post("gttorderbook")
    check("raw_post reaches unwrapped endpoint", "ok" in unknown,
          f"ok={unknown.get('ok')}")


def test_frames_live() -> None:
    print("\n=== frames (live) ===")
    client = get_client()
    if not client.settings.openalgo_api_key or not client.ping().get("ok"):
        skip("live frame checks", "OpenAlgo unreachable")
        return

    cache = get_frame_cache()
    cache.clear()

    start, end = cache.date_range_for("5m", 300)
    check("date_range_for produces a range", start < end, f"{start}..{end}")

    r1 = cache.get_frame("SBIN", "NSE", "5m", lookback_bars=300)
    if not r1.get("ok"):
        skip("frame fetch", str(r1.get("error"))[:80])
        return
    df = r1["frame"]
    check("frame fetched", len(df) > 50, f"{len(df)} bars, cols={list(df.columns)}")
    check("frame is sorted", df.index.is_monotonic_increasing)
    check("frame has no NaN in OHLC",
          not df[[c for c in ("open", "high", "low", "close") if c in df.columns]]
          .isna().any().any())
    check("frame index unique", not df.index.duplicated().any())
    check("first fetch not cached", r1["cached"] is False)

    r2 = cache.get_frame("SBIN", "NSE", "5m", lookback_bars=300)
    check("second fetch hits cache", r2["cached"] is True)

    bad = cache.get_frame("NOSUCHSYM", "NSE", "5m", lookback_bars=50)
    check("bad symbol frame returns ok=false", bad["ok"] is False,
          str(bad.get("error"))[:60])

    # The whole point of cleaning: indicators must not see NaN.
    from openalgo import ta
    rsi = ta.rsi(df["close"], 14)
    finite = int(rsi.notna().sum())
    check("ta.rsi on cleaned frame is mostly finite", finite > len(df) - 20,
          f"{finite}/{len(df)} finite")


def main() -> int:
    print("Client layer tests")
    test_constants()
    test_normalize()
    test_frame_summary()
    test_client_live()
    test_frames_live()

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
