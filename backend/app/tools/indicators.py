"""Indicator toolkit: 127 indicators behind 5 tools.

One tool per indicator would spend 127 schema slots and swamp the model's selection.
Instead the model picks an indicator by NAME - discoverable through list_indicators and
describe_indicator - and the dispatcher does the rest.

scan_symbols evaluates a restricted condition expression against named indicator
outputs. It never calls eval; the parser accepts a comparison or a crossover call and
nothing else.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd
from agno.exceptions import RetryAgentRun
from agno.tools import Toolkit

from ..indicators.compute import (
    IndicatorError, compute, required_bars, search_specs, spec_to_dict,
)
from ..indicators.registry import CATEGORIES, get_spec
from ..openalgo import constants as C
from ..openalgo.client import OpenAlgoClient, get_client
from ..openalgo.frames import FrameCache, get_frame_cache
from ..openalgo.normalize import ok, to_json

log = logging.getLogger(__name__)

MAX_SCAN_SYMBOLS = 15
LIST_DETAIL_LIMIT = 45
MAX_BATCH_INDICATORS = 8

_CONDITION_RE = re.compile(
    r"^\s*(?P<left>[a-zA-Z_][a-zA-Z0-9_]*)\s*(?P<op><=|>=|<|>|==)\s*(?P<right>-?\d+(\.\d+)?)\s*$"
)
_CROSS_RE = re.compile(
    r"^\s*(?P<fn>crossover|crossunder|cross)\s*\(\s*(?P<a>[a-zA-Z_][a-zA-Z0-9_]*)\s*,\s*"
    r"(?P<b>[a-zA-Z_][a-zA-Z0-9_]*)\s*\)\s*$"
)


class IndicatorTools(Toolkit):
    def __init__(self, client: OpenAlgoClient | None = None,
                 frames: FrameCache | None = None, **kwargs: Any) -> None:
        self.client = client or get_client()
        self.frames = frames or get_frame_cache()
        super().__init__(
            name="indicator_tools",
            tools=[
                self.list_indicators,
                self.describe_indicator,
                self.compute_indicator,
                self.compute_indicators_batch,
                self.scan_symbols,
            ],
            **kwargs,
        )

    def list_indicators(self, query: str = "", category: str = "") -> str:
        """List available technical indicators by name, keyword or category.

        There are 127 in total. Always confirm a name here before calling
        compute_indicator; do not guess from memory.

        Args:
            query (str): Optional keyword matched against name, description and
                category, for example "bollinger", "trend", "volume" or "momentum".
            category (str): Optional exact category filter. One of trend, momentum,
                volatility, volume, oscillators, statistical, hybrid, talib_extra,
                utility.

        Returns:
            str: JSON list of matching indicators with a one-line description each.
        """
        cat = (category or "").strip().lower()
        if cat and cat not in CATEGORIES:
            raise RetryAgentRun(
                f"Unknown category {category!r}. Valid categories: {', '.join(CATEGORIES)}."
            )
        specs = search_specs(query=query, category=cat)
        if not specs:
            raise RetryAgentRun(
                f"No indicator matches {query!r}. Try a broader keyword, or call "
                "list_indicators with no arguments to see every category."
            )

        # All 127 with descriptions overflows the 12,000-char tool budget and would be
        # truncated. Above the threshold, return names grouped by category instead; the
        # model can then narrow with a query or ask describe_indicator for detail.
        if len(specs) > LIST_DETAIL_LIMIT:
            grouped: dict[str, list[str]] = {}
            for spec in specs:
                grouped.setdefault(spec.category, []).append(spec.name)
            return to_json(ok({
                "total": len(specs),
                "detail_omitted": True,
                "hint": "Names only, because the full list with descriptions is too "
                        "large. Pass a query such as 'bollinger' or a category to get "
                        "descriptions, or call describe_indicator for one name.",
                "by_category": grouped,
            }, "list_indicators"))

        return to_json(ok({
            "total": len(specs),
            "categories": list(CATEGORIES),
            "indicators": [spec_to_dict(s) for s in specs],
        }, "list_indicators"))

    def describe_indicator(self, name: str) -> str:
        """Get the exact call signature of one indicator before computing it.

        Shows which price series it needs, every parameter with its default and allowed
        values, the names of its outputs in order, and how many bars of warm-up it needs.

        Args:
            name (str): Indicator method name, for example "macd", "supertrend" or "adx".

        Returns:
            str: JSON describing the indicator's inputs, parameters and outputs.
        """
        spec = get_spec((name or "").strip().lower())
        if spec is None:
            near = search_specs(query=(name or "").strip().lower())
            hint = (f" Did you mean: {', '.join(s.name for s in near[:5])}?" if near else "")
            raise RetryAgentRun(f"Unknown indicator {name!r}.{hint}")
        return to_json(ok(spec_to_dict(spec, verbose=True), "describe_indicator"))

    def compute_indicator(self, symbol: str, exchange: str, indicator: str,
                          interval: str = "5m", params: dict | None = None,
                          last_n: int = 10, lookback_bars: int = 0,
                          compare_symbol: str = "", compare_exchange: str = "") -> str:
        """Compute a technical indicator on live candle data.

        Fetches candles, cleans them, computes the indicator over the full series, and
        returns the most recent values plus a summary. The lookback is padded
        automatically for the indicator's warm-up.

        Args:
            symbol (str): OpenAlgo symbol, for example SBIN.
            exchange (str): Exchange code, for example NSE.
            indicator (str): Indicator name from list_indicators, for example "rsi".
            interval (str): Candle interval such as 5m, 15m, 1h or D. Defaults to 5m.
            params (dict): Optional indicator parameters, for example {"period": 21}.
                Call describe_indicator to see valid names and defaults.
            last_n (int): How many recent values to return per output. Defaults to 10.
            lookback_bars (int): Optional explicit bar count. Leave 0 to size it
                automatically from the indicator's warm-up.
            compare_symbol (str): Second instrument, required only for correlation and beta.
            compare_exchange (str): Exchange of the comparison instrument.

        Returns:
            str: JSON with the recent values per output, plus latest, min, max and
                direction for each.
        """
        name = (indicator or "").strip().lower()
        spec = get_spec(name)
        if spec is None:
            near = search_specs(query=name)
            hint = (f" Did you mean: {', '.join(s.name for s in near[:5])}?" if near else "")
            raise RetryAgentRun(f"Unknown indicator {indicator!r}.{hint}")

        sym, ex = C.normalize_symbol(symbol), C.normalize_exchange(exchange)
        params = params or {}
        bars = int(lookback_bars) if lookback_bars else required_bars(spec, params, int(last_n or 10))

        frame = self.frames.get_frame(sym, ex, interval, lookback_bars=bars)
        if not frame["ok"]:
            raise RetryAgentRun(
                f"Cannot compute {name} for {sym}: {frame['error']}. "
                "Check the symbol and exchange, or call get_supported_intervals."
            )

        second: pd.Series | None = None
        if spec.needs_second_series:
            if not compare_symbol:
                raise RetryAgentRun(
                    f"{name} compares two instruments. Supply compare_symbol and "
                    "compare_exchange, for example compare_symbol='NIFTY', "
                    "compare_exchange='NSE_INDEX'."
                )
            other = self.frames.get_frame(
                C.normalize_symbol(compare_symbol),
                C.normalize_exchange(compare_exchange or exchange),
                interval, lookback_bars=bars)
            if not other["ok"]:
                raise RetryAgentRun(f"Comparison series failed: {other['error']}")
            second = other["frame"]["close"]

        try:
            result = compute(name, frame["frame"], params, second_series=second,
                             last_n=int(last_n or 10))
        except IndicatorError as exc:
            raise RetryAgentRun(str(exc)) from None

        return to_json(ok({**result, "symbol": sym, "exchange": ex,
                           "interval": interval}, "compute_indicator"))

    def compute_indicators_batch(self, symbol: str, exchange: str, indicators: list,
                                 interval: str = "5m", last_n: int = 5) -> str:
        """Compute several indicators on one instrument in a single pass.

        Shares one candle fetch across all of them, so prefer this over repeated
        compute_indicator calls when answering "show me RSI, MACD and ADX".

        Args:
            symbol (str): OpenAlgo symbol.
            exchange (str): Exchange code.
            indicators (list): Indicator names, or dicts with 'name' and optional
                'params'. Example: ["rsi", {"name": "macd"}, {"name": "sma",
                "params": {"period": 50}}]. Maximum 8.
            interval (str): Candle interval. Defaults to 5m.
            last_n (int): Recent values per output. Defaults to 5.

        Returns:
            str: JSON with one result per indicator, and errors reported per indicator
                rather than failing the whole call.
        """
        if not indicators:
            raise RetryAgentRun("compute_indicators_batch needs at least one indicator.")
        if len(indicators) > MAX_BATCH_INDICATORS:
            raise RetryAgentRun(
                f"Too many indicators ({len(indicators)}); the limit is "
                f"{MAX_BATCH_INDICATORS}. Split the request."
            )

        requests: list[tuple[str, dict]] = []
        for item in indicators:
            if isinstance(item, str):
                requests.append((item.strip().lower(), {}))
            elif isinstance(item, dict) and item.get("name"):
                requests.append((str(item["name"]).strip().lower(),
                                 item.get("params") or {}))
        if not requests:
            raise RetryAgentRun(
                'Could not read the indicator list. Use ["rsi", {"name": "sma", '
                '"params": {"period": 50}}].'
            )

        sym, ex = C.normalize_symbol(symbol), C.normalize_exchange(exchange)
        bars = 60
        for name, params in requests:
            spec = get_spec(name)
            if spec:
                bars = max(bars, required_bars(spec, params, int(last_n or 5)))

        frame = self.frames.get_frame(sym, ex, interval, lookback_bars=bars)
        if not frame["ok"]:
            raise RetryAgentRun(f"Cannot fetch candles for {sym}: {frame['error']}")

        results, failures = [], []
        for name, params in requests:
            try:
                results.append(compute(name, frame["frame"], params,
                                       last_n=int(last_n or 5)))
            except IndicatorError as exc:
                failures.append({"indicator": name, "error": str(exc)})

        if not results and failures:
            raise RetryAgentRun("; ".join(f["error"] for f in failures))

        return to_json(ok({
            "symbol": sym, "exchange": ex, "interval": interval,
            "bars_used": int(len(frame["frame"])),
            "results": results,
            "failed": failures or None,
        }, "compute_indicators_batch"))

    def scan_symbols(self, symbols: list, exchange: str, indicator: str,
                     condition: str, interval: str = "5m",
                     params: dict | None = None) -> str:
        """Screen several instruments for an indicator condition.

        Args:
            symbols (list): Symbols to scan, for example ["SBIN", "RELIANCE", "INFY"].
                Maximum 15.
            exchange (str): Exchange for all of them, for example NSE.
            indicator (str): Indicator name, for example "rsi" or "macd".
            condition (str): A comparison against one of the indicator's outputs, such as
                "rsi < 30" or "adx > 25", or a cross between two outputs, such as
                "crossover(macd_line, signal_line)". Output names come from
                describe_indicator.
            interval (str): Candle interval. Defaults to 5m.
            params (dict): Optional indicator parameters.

        Returns:
            str: JSON listing which symbols matched, with the values that decided it.
        """
        if not symbols:
            raise RetryAgentRun("scan_symbols needs at least one symbol.")
        if len(symbols) > MAX_SCAN_SYMBOLS:
            raise RetryAgentRun(
                f"Too many symbols ({len(symbols)}); the limit is {MAX_SCAN_SYMBOLS}."
            )

        name = (indicator or "").strip().lower()
        spec = get_spec(name)
        if spec is None:
            raise RetryAgentRun(f"Unknown indicator {indicator!r}.")

        parsed = _parse_condition(condition)
        if parsed is None:
            raise RetryAgentRun(
                f"Cannot read the condition {condition!r}. Use a comparison such as "
                '"rsi < 30", or a cross such as "crossover(macd_line, signal_line)". '
                f"Valid output names for {name}: {', '.join(spec.outputs)}."
            )

        ex = C.normalize_exchange(exchange)
        params = params or {}
        bars = required_bars(spec, params, 5)

        matched, checked, failures = [], [], []
        for raw_symbol in symbols[:MAX_SCAN_SYMBOLS]:
            sym = C.normalize_symbol(str(raw_symbol))
            frame = self.frames.get_frame(sym, ex, interval, lookback_bars=bars)
            if not frame["ok"]:
                failures.append({"symbol": sym, "error": frame["error"][:120]})
                continue
            try:
                result = compute(name, frame["frame"], params, last_n=3)
            except IndicatorError as exc:
                failures.append({"symbol": sym, "error": str(exc)[:120]})
                continue

            hit, detail = _evaluate(parsed, result)
            entry = {"symbol": sym, **detail}
            checked.append(entry)
            if hit:
                matched.append(entry)

        if not checked and failures:
            raise RetryAgentRun(
                "No symbol could be scanned. First error: " + failures[0]["error"]
            )

        return to_json(ok({
            "indicator": name, "condition": condition, "exchange": ex,
            "interval": interval,
            "scanned": len(checked), "matched_count": len(matched),
            "matches": matched, "all_results": checked,
            "failed": failures or None,
        }, "scan_symbols"))


def _parse_condition(condition: str) -> dict | None:
    """Parse the two supported condition forms. Never uses eval."""
    text = (condition or "").strip()
    m = _CONDITION_RE.match(text)
    if m:
        return {"kind": "compare", "left": m.group("left"),
                "op": m.group("op"), "right": float(m.group("right"))}
    m = _CROSS_RE.match(text)
    if m:
        return {"kind": "cross", "fn": m.group("fn"),
                "a": m.group("a"), "b": m.group("b")}
    return None


def _pick(result: dict, label: str) -> list | None:
    """Resolve an output name, tolerating the indicator's own name as an alias."""
    values = result.get("values") or {}
    if label in values:
        return values[label]
    if label == result.get("indicator") and len(values) == 1:
        return next(iter(values.values()))
    return None


def _evaluate(parsed: dict, result: dict) -> tuple[bool, dict]:
    if parsed["kind"] == "compare":
        series = _pick(result, parsed["left"])
        if series is None:
            return False, {"error": f"no output named {parsed['left']!r}",
                           "outputs": result.get("outputs")}
        latest = next((v for v in reversed(series) if isinstance(v, (int, float))
                       and not isinstance(v, bool)), None)
        if latest is None:
            return False, {"error": "no finite value"}
        op, right = parsed["op"], parsed["right"]
        hit = {"<": latest < right, "<=": latest <= right, ">": latest > right,
               ">=": latest >= right, "==": latest == right}[op]
        return hit, {parsed["left"]: latest, "condition_met": hit}

    a = _pick(result, parsed["a"])
    b = _pick(result, parsed["b"])
    if a is None or b is None:
        return False, {"error": f"need outputs {parsed['a']} and {parsed['b']}",
                       "outputs": result.get("outputs")}
    pairs = [(x, y) for x, y in zip(a, b)
             if isinstance(x, (int, float)) and isinstance(y, (int, float))]
    if len(pairs) < 2:
        return False, {"error": "not enough finite values to detect a cross"}
    (prev_a, prev_b), (last_a, last_b) = pairs[-2], pairs[-1]
    up = prev_a <= prev_b and last_a > last_b
    down = prev_a >= prev_b and last_a < last_b
    hit = {"crossover": up, "crossunder": down, "cross": up or down}[parsed["fn"]]
    return hit, {parsed["a"]: last_a, parsed["b"]: last_b, "condition_met": hit}
