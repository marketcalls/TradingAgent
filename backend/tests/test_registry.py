"""Registry conformance test: does the hand-coded table still match openalgo 2.0.3?

Plain-python runnable, no pytest dependency, same PASS/FAIL style as
scripts/validate_setup.py.

    python backend/tests/test_registry.py

Four checks:
  1. validate_registry() is empty - all 127 names present, none extra.
  2. Every spec's params keys and series_args are real signature parameters.
  3. Every non-broken, non-utility spec can actually be CALLED on a synthetic 400-bar
     OHLCV frame, and the returned arity matches len(spec.outputs). correlation and
     beta are called with a second synthetic price series.
  4. The two indicators marked broken really do return all-NaN. If a future SDK fixes
     them this check fails, which is the point - the bug is documented by test.

Exits non-zero if any check fails.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from openalgo import ta  # noqa: E402

from app.indicators.registry import (  # noqa: E402
    CATEGORIES,
    REGISTRY,
    IndicatorSpec,
    list_specs,
    validate_registry,
)

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []

BARS = 400
REQUIRED_PERIOD = 14  # specs whose period argument has no default get this


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def synthetic_ohlcv(bars: int = BARS, seed: int = 7) -> pd.DataFrame:
    """A clean, gap-free OHLCV frame. No NaN: one NaN would poison every cumsum."""
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0.0, 1.0, bars))
    close = np.maximum(close, 5.0)
    spread = np.abs(rng.normal(0.0, 0.6, bars)) + 0.2
    high = close + spread
    low = close - spread
    open_ = np.concatenate([[close[0]], close[:-1]])
    open_ = np.clip(open_, low, high)
    volume = rng.integers(50_000, 500_000, bars).astype(float)
    index = pd.date_range("2025-01-02 09:15", periods=bars, freq="5min")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def build_args(spec: IndicatorSpec, df: pd.DataFrame) -> tuple[list, dict]:
    """Map inputs to frame columns; supply only the params that have no default."""
    series = [df[column] for column in spec.inputs]
    kwargs = {p.name: REQUIRED_PERIOD for p in spec.params.values() if p.required}
    return series, kwargs


def arity(result) -> int:
    return len(result) if isinstance(result, tuple) else 1


def all_nan(result) -> bool:
    parts = result if isinstance(result, tuple) else (result,)
    return all(bool(np.isnan(np.asarray(p, dtype=float)).all()) for p in parts)


# ---------------------------------------------------------------------------
# 1. registry vs dir(ta)
# ---------------------------------------------------------------------------

def check_coverage() -> None:
    print("\n=== 1. Registry covers the installed SDK ===")
    problems = validate_registry()
    record("validate_registry() is empty", PASS if not problems else FAIL,
           f"{len(REGISTRY)} specs" if not problems else "; ".join(problems[:6]))

    live = len([m for m in dir(ta) if not m.startswith("_")])
    record("127 public callables on ta", PASS if live == 127 else FAIL, f"found {live}")

    counts = {c: len(list_specs(category=c)) for c in CATEGORIES}
    expected = {"trend": 20, "momentum": 9, "volatility": 15, "volume": 17,
                "oscillators": 19, "statistical": 9, "hybrid": 7, "talib_extra": 18,
                "utility": 13}
    record("category counts match the plan", PASS if counts == expected else FAIL,
           " ".join(f"{k}={v}" for k, v in counts.items()))

    broken = sorted(s.name for s in REGISTRY.values() if s.status == "broken")
    record("vi and ulcerindex marked broken", PASS if broken == ["ulcerindex", "vi"] else FAIL,
           str(broken))
    uo = REGISTRY["uo_oscillator"]
    ok = uo.status == "alias" and uo.alias_of == "ultimate_oscillator"
    record("uo_oscillator marked alias", PASS if ok else FAIL,
           f"status={uo.status} alias_of={uo.alias_of}")


# ---------------------------------------------------------------------------
# 2. params vs the live signature
# ---------------------------------------------------------------------------

def check_params() -> None:
    print("\n=== 2. params and series_args are real signature parameters ===")
    bad_params: list[str] = []
    bad_series: list[str] = []
    bad_defaults: list[str] = []
    bad_enums: list[str] = []

    for name, spec in REGISTRY.items():
        sig = inspect.signature(getattr(ta, name)).parameters
        real = set(sig)
        extra = set(spec.params) - real
        if extra:
            bad_params.append(f"{name}: {sorted(extra)}")
        missing_series = set(spec.series_args) - real
        if missing_series:
            bad_series.append(f"{name}: {sorted(missing_series)}")
        if len(spec.inputs) > len(spec.series_args):
            bad_series.append(f"{name}: {len(spec.inputs)} inputs but {len(spec.series_args)} series_args")
        # every parameter is either a declared series argument or a declared param
        unclassified = real - set(spec.params) - set(spec.series_args)
        if unclassified:
            bad_params.append(f"{name}: unclassified {sorted(unclassified)}")
        for pname, pspec in spec.params.items():
            live_default = sig[pname].default
            if live_default is inspect.Parameter.empty:
                if not pspec.required:
                    bad_defaults.append(f"{name}.{pname}: required in SDK, optional in registry")
            elif pspec.required or pspec.default != live_default:
                bad_defaults.append(f"{name}.{pname}: registry={pspec.default!r} sdk={live_default!r}")
            if pspec.enum is not None and live_default not in pspec.enum:
                bad_enums.append(f"{name}.{pname}: default {live_default!r} not in {pspec.enum}")

    record("params keys are a subset of the signature",
           PASS if not bad_params else FAIL, "; ".join(bad_params[:5]))
    record("series_args exist in the signature",
           PASS if not bad_series else FAIL, "; ".join(bad_series[:5]))
    record("param defaults match the signature",
           PASS if not bad_defaults else FAIL, "; ".join(bad_defaults[:5]))
    record("enum values contain the SDK default",
           PASS if not bad_enums else FAIL, "; ".join(bad_enums[:5]))


# ---------------------------------------------------------------------------
# 3. every spec actually runs and returns the declared number of outputs
# ---------------------------------------------------------------------------

def check_calls() -> None:
    print(f"\n=== 3. Callable on a synthetic {BARS}-bar frame with declared arity ===")
    df = synthetic_ohlcv()
    errors: list[str] = []
    arity_errors: list[str] = []
    length_errors: list[str] = []
    called = 0

    for name, spec in REGISTRY.items():
        if spec.status == "broken" or spec.category == "utility" or spec.needs_second_series:
            continue
        series, kwargs = build_args(spec, df)
        try:
            out = getattr(ta, name)(*series, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the failure detail is the point
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        called += 1
        got = arity(out)
        if got != len(spec.outputs):
            arity_errors.append(f"{name}: returned {got}, spec declares {len(spec.outputs)}")
        parts = out if isinstance(out, tuple) else (out,)
        for part in parts:
            if len(np.asarray(part)) != len(df):
                length_errors.append(f"{name}: output length {len(np.asarray(part))} != {len(df)}")
                break

    record(f"{called} indicators executed without error",
           PASS if not errors else FAIL, "; ".join(errors[:5]))
    record("returned arity matches spec.outputs",
           PASS if not arity_errors else FAIL, "; ".join(arity_errors[:5]))
    record("outputs are the same length as the input",
           PASS if not length_errors else FAIL, "; ".join(length_errors[:5]))

    # correlation and beta need a second, independent price series
    market = synthetic_ohlcv(seed=99)["close"]
    for name in ("correlation", "beta"):
        spec = REGISTRY[name]
        try:
            out = getattr(ta, name)(df["close"], market)
            ok = arity(out) == len(spec.outputs)
            finite = int(np.isfinite(np.asarray(out, dtype=float)).sum())
            record(f"{name} with a second series", PASS if ok and finite else FAIL,
                   f"arity={arity(out)} finite={finite}")
        except Exception as exc:  # noqa: BLE001
            record(f"{name} with a second series", FAIL, f"{type(exc).__name__}: {exc}")

    # the conditional-arity trio: the alternate shapes must match conditional_outputs
    conditional_checks = [
        ("obv_smoothed", (df["close"], df["volume"]), {"ma_type": "SMA + Bollinger Bands"},
         "ma_type=SMA + Bollinger Bands"),
        ("variance", (df["close"],), {"return_components": True}, "return_components=True"),
    ]
    for name, series, kwargs, key in conditional_checks:
        spec = REGISTRY[name]
        expected = spec.conditional_outputs[key]
        try:
            out = getattr(ta, name)(*series, **kwargs)
            ok = arity(out) == len(expected)
            record(f"{name} conditional arity [{key}]", PASS if ok else FAIL,
                   f"returned {arity(out)}, spec declares {len(expected)}")
        except Exception as exc:  # noqa: BLE001
            record(f"{name} conditional arity [{key}]", FAIL, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 4. the broken pair really is broken
# ---------------------------------------------------------------------------

def check_broken() -> None:
    print("\n=== 4. The two broken indicators still return all-NaN ===")
    df = synthetic_ohlcv()
    cases = [
        ("vi", (df["high"], df["low"], df["close"]), {}),
        ("ulcerindex", (df["close"],), {}),
        ("ulcerindex (return_signal=True)", (df["close"],), {"return_signal": True}),
    ]
    for label, series, kwargs in cases:
        name = label.split(" ")[0]
        try:
            out = getattr(ta, name)(*series, **kwargs)
            nan = all_nan(out)
            record(f"{label} returns all-NaN", PASS if nan else FAIL,
                   "still broken in this build" if nan
                   else "FIXED upstream - drop status='broken' from the registry")
        except Exception as exc:  # noqa: BLE001
            record(f"{label} returns all-NaN", FAIL, f"{type(exc).__name__}: {exc}")


def main() -> int:
    print("OpenAlgo Trading Agent - indicator registry test")
    print(f"python {sys.version.split()[0]}  backend={BACKEND}")
    check_coverage()
    check_params()
    check_calls()
    check_broken()

    print("\n=== Summary ===")
    n_pass = sum(1 for _, s, _ in results if s == PASS)
    n_fail = sum(1 for _, s, _ in results if s == FAIL)
    for name, status, _ in results:
        if status == FAIL:
            print(f"  FAILED: {name}")
    print(f"  {n_pass} passed, {n_fail} failed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
