"""The indicator dispatcher: one code path for all 127 ta callables.

Every rule here was measured against openalgo 2.0.3, not read off a doc page:

  - Period arguments must be Python int. validate_period does isinstance(period, int),
    so 14.0 and np.int64(14) both raise TypeError. JSON tool arguments decode numbers to
    float, so roughly half of all indicator calls would fail without _coerce_params.

  - A single NaN in the input poisons everything downstream, because the backend's
    sma/rolling_sum are cumsum based. Frames arrive pre-cleaned from frames.py; this
    module additionally refuses to run if any required series is still short or empty.

  - Warm-up varies wildly: beta needs 253 bars before its first finite value, lrslope
    101, crsi 100, pvi_with_signal 255. required_bars() pads the fetch so a caller
    asking for "the last 10 values of beta" actually gets numbers instead of nulls.

  - Three methods change return arity based on an argument. spec.conditional_outputs
    maps "param=value" to the alternate output names, and _resolve_outputs applies it.

  - vi and ulcerindex return all-NaN in this build. They are refused with an explanation
    rather than returning a wall of nulls.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import pandas as pd
from openalgo import ta

from .descriptions import describe
from .registry import REGISTRY, IndicatorSpec, get_spec

log = logging.getLogger(__name__)

# Extra bars fetched on top of the measured warm-up, so the caller gets a usable tail.
WARMUP_MARGIN = 30


class IndicatorError(Exception):
    """Raised for anything the caller can fix by changing the request."""


def _coerce_params(spec: IndicatorSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Cast JSON-decoded arguments into what the library will actually accept."""
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if key not in spec.params:
            raise IndicatorError(
                f"{spec.name} has no parameter {key!r}. Valid parameters: "
                f"{', '.join(spec.params) or 'none'}."
            )
        pspec = spec.params[key]
        if value is None:
            continue

        if pspec.kind == "int":
            try:
                as_float = float(value)
            except (TypeError, ValueError):
                raise IndicatorError(
                    f"{spec.name}.{key} must be a whole number, got {value!r}."
                ) from None
            if not as_float.is_integer():
                raise IndicatorError(
                    f"{spec.name}.{key} must be a whole number, got {value!r}."
                )
            coerced: Any = int(as_float)
            if coerced <= 0:
                raise IndicatorError(f"{spec.name}.{key} must be positive, got {coerced}.")
        elif pspec.kind == "float":
            try:
                coerced = float(value)
            except (TypeError, ValueError):
                raise IndicatorError(
                    f"{spec.name}.{key} must be a number, got {value!r}."
                ) from None
        elif pspec.kind == "bool":
            coerced = bool(value) if not isinstance(value, str) else \
                value.strip().lower() in ("1", "true", "yes")
        else:
            coerced = str(value)
            if pspec.enum and coerced not in pspec.enum:
                raise IndicatorError(
                    f"{spec.name}.{key} must be one of {list(pspec.enum)}, got {coerced!r}."
                )
        out[key] = coerced
    return out


def _resolve_outputs(spec: IndicatorSpec, params: dict[str, Any]) -> tuple[str, ...]:
    """Apply conditional_outputs when a flag changes the return arity."""
    if not spec.conditional_outputs:
        return spec.outputs
    for condition, outputs in spec.conditional_outputs.items():
        key, _, raw = condition.partition("=")
        if key not in params:
            continue
        actual = params[key]
        if isinstance(actual, bool):
            if str(actual) == raw:
                return outputs
        elif str(actual) == raw:
            return outputs
    return spec.outputs


def required_bars(spec: IndicatorSpec, params: dict[str, Any] | None = None,
                  want: int = 10) -> int:
    """How many bars to fetch so `want` finite values come back.

    Uses the measured warm-up as a floor and scales it when the caller overrides a
    period, because warm-up tracks the period arguments.
    """
    warmup = int(spec.warmup or 0)
    for key, value in (params or {}).items():
        pspec = spec.params.get(key)
        if pspec and pspec.kind == "int" and isinstance(value, int):
            default = pspec.default if isinstance(pspec.default, int) else None
            if default and value > default:
                warmup += (value - default)
            elif default is None:
                warmup = max(warmup, value)
    return max(60, warmup + want + WARMUP_MARGIN)


def _series_for(spec: IndicatorSpec, df: pd.DataFrame) -> list[pd.Series]:
    """Map the spec's declared inputs onto real DataFrame columns."""
    series: list[pd.Series] = []
    for column in spec.inputs:
        if column not in df.columns:
            raise IndicatorError(
                f"{spec.name} needs the {column!r} series but the candle data only has "
                f"{list(df.columns)}."
            )
        col = df[column]
        if col.isna().all():
            raise IndicatorError(f"{spec.name}: the {column!r} series is entirely empty.")
        series.append(col)
    return series


def _to_list(values: Any) -> list[Any]:
    """NaN and infinity become None so the result is valid JSON."""
    if isinstance(values, pd.Series):
        raw = values.tolist()
    elif isinstance(values, np.ndarray):
        raw = values.tolist()
    else:
        raw = list(values)
    out = []
    for v in raw:
        if isinstance(v, (bool, np.bool_)):
            out.append(bool(v))
        elif v is None:
            out.append(None)
        else:
            try:
                f = float(v)
                out.append(None if (math.isnan(f) or math.isinf(f)) else round(f, 6))
            except (TypeError, ValueError):
                out.append(None)
    return out


def _stats(values: list[Any]) -> dict[str, Any]:
    finite = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not finite:
        return {"latest": None, "finite_count": 0}
    latest = finite[-1]
    out: dict[str, Any] = {
        "latest": latest,
        "finite_count": len(finite),
        "min": min(finite),
        "max": max(finite),
    }
    if len(finite) >= 2:
        prev = finite[-2]
        out["previous"] = prev
        out["direction"] = "rising" if latest > prev else ("falling" if latest < prev else "flat")
    return out


def compute(name: str, df: pd.DataFrame, params: dict[str, Any] | None = None,
            second_series: pd.Series | None = None, last_n: int = 10) -> dict[str, Any]:
    """Run one indicator over a cleaned frame and return a JSON-safe result."""
    spec = get_spec(name)
    if spec is None:
        raise IndicatorError(
            f"Unknown indicator {name!r}. Call list_indicators to see the 127 available "
            "names."
        )
    if spec.status == "broken":
        raise IndicatorError(
            f"{spec.name} is not usable in openalgo 2.0.3: it returns only nulls because "
            f"of a cumulative-sum bug in the library. {spec.note or ''} "
            "Pick a different indicator."
        )

    target = spec
    if spec.status == "alias" and spec.alias_of:
        target = get_spec(spec.alias_of) or spec

    clean_params = _coerce_params(target, params or {})

    if df is None or len(df) == 0:
        raise IndicatorError(f"{target.name}: no candle data supplied.")

    series = _series_for(target, df)

    if target.needs_second_series:
        if second_series is None:
            raise IndicatorError(
                f"{target.name} compares two instruments and needs a second symbol. "
                "Supply compare_symbol and compare_exchange."
            )
        aligned = pd.Series(second_series).reindex(df.index).ffill().bfill()
        if aligned.isna().all():
            raise IndicatorError(
                f"{target.name}: the comparison series does not overlap the main series."
            )
        series = [series[0], aligned]

    needed = max((v for k, v in clean_params.items()
                  if target.params.get(k) and target.params[k].kind == "int"), default=0)
    if needed and len(df) <= needed:
        raise IndicatorError(
            f"{target.name}: period {needed} needs more than {len(df)} bars. "
            "Increase lookback_bars or use a shorter period."
        )

    try:
        raw = getattr(ta, target.name)(*series, **clean_params)
    except TypeError as exc:
        raise IndicatorError(f"{target.name}: bad arguments - {exc}") from None
    except ValueError as exc:
        raise IndicatorError(f"{target.name}: {exc}") from None

    outputs = _resolve_outputs(target, clean_params)
    parts = list(raw) if isinstance(raw, tuple) else [raw]
    if len(parts) != len(outputs):
        # Trust reality over the registry rather than mislabelling columns.
        outputs = tuple(f"output_{i + 1}" for i in range(len(parts)))

    tail = max(int(last_n or 10), 1)
    values: dict[str, Any] = {}
    stats: dict[str, Any] = {}
    for label, part in zip(outputs, parts):
        full = _to_list(part)
        values[label] = full[-tail:]
        stats[label] = _stats(full)

    timestamps = [str(t) for t in df.index[-tail:]]

    return {
        "indicator": target.name,
        "category": target.category,
        "description": describe(target.name),
        "params_used": {**{k: v.default for k, v in target.params.items()}, **clean_params},
        "bars_used": int(len(df)),
        "outputs": list(outputs),
        "timestamps": timestamps,
        "values": values,
        "summary": stats,
        "note": target.note or None,
    }


def spec_to_dict(spec: IndicatorSpec, verbose: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": spec.name,
        "category": spec.category,
        "description": describe(spec.name),
    }
    if spec.status != "ok":
        out["status"] = spec.status
        if spec.alias_of:
            out["alias_of"] = spec.alias_of
    if not verbose:
        return out
    out.update({
        "requires_series": list(spec.inputs) or ["derived series"],
        "outputs": list(spec.outputs),
        "warmup_bars": spec.warmup,
        "parameters": {
            key: {"type": p.kind, "default": p.default,
                  **({"allowed": list(p.enum)} if p.enum else {})}
            for key, p in spec.params.items()
        },
    })
    if spec.needs_second_series:
        out["needs_second_symbol"] = True
    if spec.conditional_outputs:
        out["conditional_outputs"] = {k: list(v) for k, v in spec.conditional_outputs.items()}
    if spec.note:
        out["note"] = spec.note
    return out


def search_specs(query: str = "", category: str = "") -> list[IndicatorSpec]:
    """Match on name, description and category, so intent-based queries work."""
    q = (query or "").strip().lower()
    cat = (category or "").strip().lower()
    found = []
    for spec in REGISTRY.values():
        if cat and spec.category.lower() != cat:
            continue
        if q:
            haystack = f"{spec.name} {spec.category} {describe(spec.name)} {spec.note}".lower()
            if q not in haystack:
                continue
        found.append(spec)
    return sorted(found, key=lambda s: (s.category, s.name))
