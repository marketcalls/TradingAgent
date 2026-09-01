"""Chart toolkit: the analyst sees, analyses and acts on the chart in front of the user.

Runtime facts about agno 2.8.7, all verified by executing the installed build rather
than read off a doc page. Each one shaped a decision here.

  1. Setting output_schema on the Agent DISABLES token streaming
     (agent/_response.py:1067-1071: "Response model set, model response is not
     streamed"). Drawing instructions therefore cannot be a structured agent response,
     or every reply would arrive as one silent block. They travel as a CustomEvent
     yielded from inside the tool instead, which leaves streaming untouched.

  2. A tool may be an async generator. FunctionCall._wrap_callable re-wraps the
     pydantic-validated callable in an outer `async def ... yield` shim precisely so
     inspect.isasyncgenfunction still answers True after validation
     (tools/function.py:586-600), and models/base.py drains it, forwarding each yielded
     item. So `yield event` then `yield "text"` works, and the event reaches the UI
     BEFORE the model has written a word: the markup lands first, the prose follows.

  3. A yielded CustomEvent bubbles up only when it is an instance of agno's CustomEvent
     class, because the dispatch test is
     `isinstance(item, tuple(get_args(RunOutputEvent)))` (models/base.py:2186). A
     subclass passes. agno also stamps tool_call_id onto it and fills agent_id,
     session_id and run_id in agent/_response.py:1389.

  4. THE ONE SURPRISE, and it contradicts the "geometry never reaches the model"
     assumption: agno does `function_call_output += str(item)` for every yielded
     CustomEvent (models/base.py:2202, 2749 and 2887). The dataclass repr of an
     envelope with thirty vertices would therefore be pasted into the tool result the
     model reads, and would blow the 12,000 character cap in normalize.to_json.
     ChartCommandEvent.__str__ returns an empty string to close that leak. __repr__ is
     left alone, so logs and debuggers still show everything.

  5. CustomEvent.to_dict() is asdict-based, so only real dataclass FIELDS survive
     serialisation to the browser. `commands` is declared as a field for that reason;
     an attribute set in __init__ alone would be dropped on the wire.

  6. run_context is injected by name, not by type, and is stripped from the generated
     JSON schema (tools/function.py:306 and 939). Mutating run_context.session_state
     inside a tool mutates the caller's dict in place, verified by identity: that is
     what lets a pattern drawn in one turn still be there for project_targets in the
     next.

  7. Toolkit.register splits on inspect.iscoroutinefunction (tools/toolkit.py:196), and
     an async GENERATOR answers False to that while a plain `async def` answers True.
     So the eleven drawing tools land in Toolkit.functions and the three analysis tools
     land in Toolkit.async_functions. get_async_functions() merges both and is what the
     agent reads in async mode, so all fourteen are present under agent.arun, which is
     the only path the backend uses. THIS TOOLKIT REQUIRES arun: under the synchronous
     agent.run, get_functions() returns the eleven and models/base.py would str() an
     un-drained async generator rather than execute it.

Everything the model can see is short. Geometry rides the event; the tool result is one
sentence, so a twelve-vertex envelope costs the model roughly a hundred characters.
Blocking work (candle fetch, indicator maths, pivot detection) goes through
asyncio.to_thread, because agno calls a tool entrypoint on the event loop directly and
never offloads it to a worker (tools/function.py:1320-1332); a blocking call there would
stall every other SSE stream in the process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from itertools import permutations
from pathlib import Path
from typing import Any

from agno.exceptions import RetryAgentRun
from agno.run.agent import CustomEvent
from agno.run.base import RunContext
from agno.tools import Toolkit

from ..charts.contract import (
    Anchor,
    ChartContext,
    Envelope,
    Level,
    Marker,
    Trendline,
    Zone,
    clear_command,
    draw_command,
)
from ..indicators.compute import IndicatorError, compute
from ..openalgo import constants as C
from ..openalgo.client import OpenAlgoClient, get_client
from ..openalgo.frames import FrameCache, get_frame_cache
from ..openalgo.normalize import err, ok, to_json

# The sibling module that owns pivot detection. Guarded so this toolkit still imports
# (and its schemas still validate) while geometry.py is being written; every tool that
# needs it says so plainly instead of raising ImportError at agent build time.
try:  # pragma: no cover - the except arm exists only during the build-out
    from ..charts import geometry as G
except ImportError:  # pragma: no cover
    G = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

CATALOGUE_PATH = Path(__file__).resolve().parent.parent / "charts" / "indicator_catalogue.json"

#: session_state keys. "chart" is written by the browser every turn, "chart_patterns"
#: by this toolkit, and it has to outlive the turn: project_targets works on geometry
#: that draw_envelope computed in an earlier one.
STATE_CHART = "chart"
STATE_PATTERNS = "chart_patterns"

#: Group ids. The chart replaces a whole group on every draw, so a second envelope
#: overwrites the first rather than stacking, and clear_annotations("envelope") removes
#: exactly one layer. Must not contain "#": the hit-test parser splits ids on it.
GROUP_ENVELOPE = "envelope"

# Major swings only, per the shape the channel is meant to describe.
CHANNEL_PIVOTS = 5
GROUP_TRENDLINE = "trendline"
GROUP_LEVELS = "levels"
GROUP_ZONE = "zone"
GROUP_TARGETS = "targets"
ALL_GROUPS = (GROUP_ENVELOPE, GROUP_TRENDLINE, GROUP_LEVELS, GROUP_ZONE, GROUP_TARGETS)

DEFAULT_LOOKBACK = 300
MAX_LOOKBACK = 2000
MAX_LEVELS = 8
SNAP_TOLERANCE_PCT = 2.0

# How close a pivot sits to a candidate line before it counts as a touch, as a
# percentage of the line's price there. The geometry default, named here because
# both rails of a channel must be fitted on the same terms.
TREND_TOLERANCE_PCT = 0.5

NO_CHART = (
    "No chart is open, so there is nothing to read or draw on. Tell the user to open a "
    "chart first, and do not guess a symbol."
)
NO_GEOMETRY = (
    "The chart geometry module is not available in this build, so pivots cannot be "
    "detected. Report this as a backend problem rather than inventing levels."
)

#: Spoken forms that do not match a registry id. Everything else is resolved from the
#: generated catalogue, so this list stays short by design.
_ID_ALIASES = {
    "bollinger bands": "bollinger",
    "bollinger band": "bollinger",
    "bb": "bollinger",
    "psar": "parabolic-sar",
    "parabolic sar": "parabolic-sar",
    "sar": "parabolic-sar",
    "moving average": "sma",
    "simple moving average": "sma",
    "exponential moving average": "ema",
    "super trend": "supertrend",
    "st": "supertrend",
    "stoch": "stochastic",
    "ichimoku cloud": "ichimoku",
    "relative strength index": "rsi",
    "average true range": "atr",
    "on balance volume": "obv",
}


@dataclass
class ChartCommandEvent(CustomEvent):
    """One or more chart commands, on their way to the browser mid-turn.

    Yielded from a tool rather than returned, so the chart updates before the model
    starts narrating. See fact 4 in the module docstring for why __str__ is empty.

    Attributes:
        commands: Command dicts matching frontend/src/lib/charts/types.ts ChartCommand.
    """

    commands: list[dict] | None = None

    def __str__(self) -> str:
        """Contribute nothing to the tool result the model reads.

        agno concatenates str() of every yielded event into the tool output. The
        default dataclass repr would put the full geometry into the model's context,
        which is the exact cost this design exists to avoid.
        """
        return ""


# --- catalogue --------------------------------------------------------------


@lru_cache(maxsize=1)
def load_catalogue() -> dict[str, Any]:
    """Read the generated chart indicator catalogue.

    Returns:
        dict: The parsed catalogue, or an empty skeleton if the file is missing.
    """
    try:
        with CATALOGUE_PATH.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:  # noqa: BLE001
        log.warning("indicator catalogue unreadable at %s: %s", CATALOGUE_PATH, exc)
        return {"count": 0, "indicators": {}, "chart_types": []}


def resolve_indicator_id(raw: str) -> str | None:
    """Map whatever the user said onto a chart registry id.

    Args:
        raw: A spoken or typed indicator name.

    Returns:
        The registry id, or None if nothing matches.
    """
    catalogue = load_catalogue().get("indicators", {})
    text = (raw or "").strip().lower()
    if not text:
        return None
    if text in catalogue:
        return text
    if text in _ID_ALIASES and _ID_ALIASES[text] in catalogue:
        return _ID_ALIASES[text]

    hyphened = text.replace(" ", "-").replace("_", "-")
    if hyphened in catalogue:
        return hyphened
    if hyphened in _ID_ALIASES and _ID_ALIASES[hyphened] in catalogue:
        return _ID_ALIASES[hyphened]

    tight = text.replace(" ", "").replace("-", "").replace("_", "")
    for key, entry in catalogue.items():
        if key.replace("-", "") == tight:
            return key
        if str(entry.get("name", "")).lower().replace(" ", "").replace("-", "") == tight:
            return key
    return None


def _numeric_inputs(entry: dict) -> list[dict]:
    return [i for i in entry.get("inputs", []) if i.get("type") == "number"]


def _in_bounds(spec: dict, value: float) -> bool:
    low, high = spec.get("min"), spec.get("max")
    if low is not None and value < low:
        return False
    if high is not None and value > high:
        return False
    return True


def assign_positional(entry: dict, values: list[float]) -> tuple[dict[str, float], str]:
    """Land bare numbers, as spoken, on named indicator settings.

    "supertrend 3,10" is genuinely ambiguous: the declared bounds are ATR Period 1..200
    and Multiplier 0.1..20, so 3 and 10 are legal in either slot. The rule is to take
    the assignment closest to the indicator's own declared defaults, measured as the
    summed relative distance. For supertrend that reads 3,10 as multiplier 3 and ATR
    period 10, which is the convention every charting package uses, and it generalises:
    "macd 12,26,9" and "bollinger 20,2" both come out in declaration order because that
    is already the nearest-to-defaults assignment.

    Args:
        entry: One catalogue entry.
        values: The bare numbers, in the order the user said them.

    Returns:
        A (settings, reading) pair. `reading` is a plain sentence naming the assignment
        so the user can correct it in four words.
    """
    slots = _numeric_inputs(entry)[: len(values)]
    if not slots or not values:
        return {}, ""

    best: tuple[float, tuple[float, ...]] | None = None
    for candidate in permutations(values, len(slots)):
        if not all(_in_bounds(spec, v) for spec, v in zip(slots, candidate)):
            continue
        score = 0.0
        for spec, value in zip(slots, candidate):
            default = float(spec.get("default", 0) or 0)
            score += abs(value - default) / max(abs(default), 1.0)
        if best is None or score < best[0] - 1e-12:
            best = (score, candidate)

    if best is None:
        # Nothing fits the declared ranges. Fall back to declaration order and let the
        # bounds check downstream report which number is out of range.
        best = (0.0, tuple(values[: len(slots)]))

    settings = {spec["key"]: _tidy(value) for spec, value in zip(slots, best[1])}
    reading = ", ".join(
        f"{spec['label']} {_tidy(value)}" for spec, value in zip(slots, best[1])
    )
    return settings, reading


def _tidy(value: float) -> Any:
    """Return an int when the number is whole, so settings read as people say them."""
    as_float = float(value)
    return int(as_float) if as_float.is_integer() else round(as_float, 6)


def _coerce_values(settings: dict | None) -> list[float]:
    """Pull bare positional numbers out of the settings argument."""
    if not settings:
        return []
    raw = settings.get("values")
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p for p in raw.replace(";", ",").replace(" ", ",").split(",") if p]
    elif isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = [raw]
    out: list[float] = []
    for part in parts:
        try:
            out.append(float(part))
        except (TypeError, ValueError):
            continue
    return out


# --- state ------------------------------------------------------------------


def read_context(run_context: RunContext | None) -> ChartContext | None:
    """Read the chart the user is looking at out of session state.

    Args:
        run_context: The run context agno injects into every tool.

    Returns:
        A validated ChartContext, or None when no chart is open.
    """
    state = getattr(run_context, "session_state", None) or {}
    raw = state.get(STATE_CHART)
    if not raw:
        return None
    if isinstance(raw, ChartContext):
        return raw if raw.symbol else None
    if not isinstance(raw, dict):
        return None
    try:
        ctx = ChartContext.model_validate(raw)
    except Exception:  # noqa: BLE001
        log.warning("unreadable chart context in session state", exc_info=True)
        return None
    return ctx if ctx.symbol else None


def _patterns(run_context: RunContext | None) -> dict[str, Any]:
    """The live patterns dict inside session state, created on first use."""
    state = getattr(run_context, "session_state", None)
    if state is None:
        return {}
    store = state.get(STATE_PATTERNS)
    if not isinstance(store, dict):
        store = {}
        state[STATE_PATTERNS] = store
    return store


# --- geometry plumbing ------------------------------------------------------


def _pivot_dict(pivot: Any) -> dict[str, Any]:
    return {
        "index": int(getattr(pivot, "index", 0)),
        "time": float(getattr(pivot, "time", 0.0)),
        "price": float(getattr(pivot, "price", 0.0)),
    }


def _to_pivot(raw: Any) -> Any:
    """Rebuild a geometry.Pivot from the JSON-safe form kept in session state."""
    if not isinstance(raw, dict):
        return raw
    return G.Pivot(index=int(raw["index"]), time=float(raw["time"]),
                   price=float(raw["price"]))


def _anchors(pivots: list) -> list[Anchor]:
    return [Anchor(time=float(p.time), price=float(p.price)) for p in pivots]


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def _round(value: Any, places: int = 2) -> Any:
    out = _finite(value)
    return None if out is None else round(out, places)


def _store_envelope(run_context: RunContext | None, group: str, ctx: ChartContext,
                    env: dict) -> dict[str, Any]:
    """Persist an envelope so a later turn can still project targets off it."""
    record = {
        "kind": "envelope",
        "group": group,
        "symbol": ctx.symbol,
        "exchange": ctx.exchange,
        "interval": ctx.interval,
        "highs": [_pivot_dict(p) for p in env.get("highs", [])],
        "lows": [_pivot_dict(p) for p in env.get("lows", [])],
        "slope_high": _finite(env.get("slope_high")),
        "slope_low": _finite(env.get("slope_low")),
        "direction": env.get("direction"),
        "width": _finite(env.get("width")),
        "first_time": _finite(env.get("first_time")),
        "last_time": _finite(env.get("last_time")),
    }
    _patterns(run_context)[group] = record
    return record


def _restore_envelope(record: dict) -> dict[str, Any]:
    """Turn a stored record back into the dict shape geometry.measured_move expects."""
    return {
        "highs": [_to_pivot(p) for p in record.get("highs", [])],
        "lows": [_to_pivot(p) for p in record.get("lows", [])],
        "slope_high": record.get("slope_high"),
        "slope_low": record.get("slope_low"),
        "direction": record.get("direction"),
        "width": record.get("width"),
        "first_time": record.get("first_time"),
        "last_time": record.get("last_time"),
    }


def _line_at(fit: dict[str, Any], t0: float, t1: float) -> tuple[Anchor, Anchor]:
    """Evaluate a fitted line at two times.

    Args:
        fit: A result from :func:`geometry.fit_trendline`.
        t0: Left edge, UTC seconds.
        t1: Right edge, UTC seconds.

    Returns:
        The two anchors the line passes through at those times.
    """
    slope = float(fit.get("slope") or 0.0)
    intercept = float(fit.get("intercept") or 0.0)
    return (Anchor(time=t0, price=slope * t0 + intercept),
            Anchor(time=t1, price=slope * t1 + intercept))


def _channel_rails(upper_fit: dict[str, Any], lower_fit: dict[str, Any],
                   highs: list, lows: list, t0: float, t1: float,
                   df: Any) -> tuple[tuple[Anchor, Anchor], tuple[Anchor, Anchor]]:
    """Two sane rails for a channel, falling back to parallel when a fit runs away.

    Each side is fitted only to its own pivots, so projecting it across the union
    of both sides extrapolates it beyond any evidence. When one side's pivots are
    bunched into part of the window that extrapolation is violent: a lower rail
    fitted to a late cluster of rising lows was drawn at 1198 on a chart whose
    lowest bar was 1267, and the two rails crossed.

    So the independent fits are used only if they survive two checks: the rails
    must not cross inside the span, and neither may leave the data's own range by
    more than the height of that range. Anything else falls back to a parallel
    channel built on the better supported slope, which cannot cross by
    construction and always brackets the bars.

    Args:
        upper_fit: Fit over the swing highs.
        lower_fit: Fit over the swing lows.
        highs: The swing high pivots.
        lows: The swing low pivots.
        t0: Left edge, UTC seconds.
        t1: Right edge, UTC seconds.
        df: The candle frame, for the data's real high and low.

    Returns:
        The upper rail's two anchors and the lower rail's two anchors.
    """
    upper = _line_at(upper_fit, t0, t1)
    lower = _line_at(lower_fit, t0, t1)

    low = float(df["low"].min())
    high = float(df["high"].max())
    span = max(high - low, 1e-9)
    floor, ceiling = low - span, high + span

    crosses = upper[0].price <= lower[0].price or upper[1].price <= lower[1].price
    runaway = any(not (floor <= a.price <= ceiling) for a in (*upper, *lower))
    if not crosses and not runaway:
        return upper, lower

    # Parallel fallback. The slope comes from whichever side more pivots agreed
    # on, because that is the side whose trend is actually evidenced.
    if int(upper_fit.get("touches") or 0) >= int(lower_fit.get("touches") or 0):
        slope = float(upper_fit.get("slope") or 0.0)
    else:
        slope = float(lower_fit.get("slope") or 0.0)

    # Offset each rail so it sits on the most extreme pivot of its own side: the
    # channel then touches the bars it claims to touch.
    top = max(p.price - slope * p.time for p in highs)
    bottom = min(p.price - slope * p.time for p in lows)
    return (
        (Anchor(time=t0, price=slope * t0 + top), Anchor(time=t1, price=slope * t1 + top)),
        (Anchor(time=t0, price=slope * t0 + bottom), Anchor(time=t1, price=slope * t1 + bottom)),
    )


def _channel_shape(upper: tuple[Anchor, Anchor], lower: tuple[Anchor, Anchor]) -> str:
    """Name a channel by what its two rails do relative to each other.

    Args:
        upper: The upper rail's left and right anchors.
        lower: The lower rail's left and right anchors.

    Returns:
        One of "rising", "falling", "contracting", "broadening" or "sideways".
    """
    left = upper[0].price - lower[0].price
    right = upper[1].price - lower[1].price
    if left > 0 and right / left < 0.7:
        return "contracting"
    if left > 0 and right / left > 1.4:
        return "broadening"
    drift = ((upper[1].price + lower[1].price) - (upper[0].price + lower[0].price)) / 2.0
    span = max(abs(left), abs(right), 1e-9)
    if drift > span * 0.5:
        return "rising"
    if drift < -span * 0.5:
        return "falling"
    return "sideways"


def _tone_for(direction: Any) -> str:
    text = str(direction or "").lower()
    if "down" in text or "bear" in text or "desc" in text:
        return "bearish"
    if "up" in text or "bull" in text or "asc" in text:
        return "bullish"
    return "neutral"


def _extract_targets(measured: dict) -> list[tuple[str, float]]:
    """Name the two measured-move targets, skipping the ones that are not prices.

    geometry.measured_move returns width, direction, time, target_time, breakout,
    breakdown, upside_target and downside_target. Only the last four are prices;
    target_time in particular is UTC seconds and would draw a level at year 59,000 if
    it were treated as one.
    """
    if not isinstance(measured, dict):
        return []
    width = _finite(measured.get("width")) or 0.0
    if width <= 0:
        return []
    out: list[tuple[str, float]] = []
    for key, name in (("upside_target", "upside target"),
                      ("downside_target", "downside target")):
        price = _finite(measured.get(key))
        if price is not None and price > 0:
            out.append((name, price))
    return out


class ChartTools(Toolkit):
    """See, analyse and act on the chart the user is looking at.

    Nothing here writes an order or changes broker state, so no tool is confirmation
    gated: pausing a read-only page for approval would strand the markup half drawn.
    """

    def __init__(self, client: OpenAlgoClient | None = None,
                 frames: FrameCache | None = None, **kwargs: Any) -> None:
        # Set before super().__init__: the parent inspects the bound methods handed to
        # tools=[...], and they close over these attributes.
        self.client = client or get_client()
        self.frames = frames or get_frame_cache()
        super().__init__(
            name="chart_tools",
            tools=[
                self.set_chart_symbol,
                self.set_chart_interval,
                self.set_chart_type,
                self.add_chart_indicator,
                self.remove_chart_indicator,
                self.clear_annotations,
                self.draw_envelope,
                self.draw_trendline,
                self.draw_levels,
                self.draw_zone,
                self.project_targets,
                self.analyse_trend,
                self.analyse_momentum,
                self.describe_chart,
            ],
            **kwargs,
        )

    # --- shared helpers -----------------------------------------------------

    async def _frame(self, ctx: ChartContext, lookback: int):
        """Fetch the candle frame behind the chart, off the event loop.

        Returns:
            The frames.get_frame envelope: {"ok": True, "frame": DataFrame} or
            {"ok": False, "error": str}.
        """
        bars = max(60, min(int(lookback or DEFAULT_LOOKBACK), MAX_LOOKBACK))
        return await asyncio.to_thread(
            self.frames.get_frame,
            C.normalize_symbol(ctx.symbol), C.normalize_exchange(ctx.exchange),
            ctx.interval, lookback_bars=bars,
        )

    def _window(self, ctx: ChartContext) -> tuple[float | None, float | None]:
        """The viewport, when the browser reported one. Otherwise the loaded range."""
        start = ctx.visible_from if ctx.visible_from is not None else ctx.first_time
        end = ctx.visible_to if ctx.visible_to is not None else ctx.last_time
        return start, end

    async def _supported_intervals(self) -> tuple[list[str], str | None]:
        """Ask the broker which intervals exist. Never a hard-coded list.

        Returns:
            A (intervals, error) pair; `error` is set when the call itself failed.
        """
        res = await asyncio.to_thread(self.client.call_enveloped, "intervals")
        if not res.get("ok"):
            return [], str(res.get("error") or "intervals lookup failed")
        data = res.get("data") or {}
        found: list[str] = []
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list):
                    found.extend(str(v) for v in value)
        elif isinstance(data, list):
            found = [str(v) for v in data]
        return found, None

    # --- chart control ------------------------------------------------------

    async def set_chart_symbol(self, symbol: str, exchange: str,
                               run_context: RunContext | None = None):
        """Load a different instrument into the chart the user is looking at.

        Args:
            symbol (str): OpenAlgo symbol. Equity is the bare base symbol (RELIANCE).
                Indices carry no spaces and no "50": NIFTY, BANKNIFTY, SENSEX.
            exchange (str): Exchange code such as NSE, BSE, NFO, MCX, NSE_INDEX.

        Returns:
            str: One line naming what the chart now shows.
        """
        sym = C.normalize_symbol(symbol)
        ex = C.normalize_exchange(exchange)
        if not sym or not ex:
            raise RetryAgentRun(
                "set_chart_symbol needs both a symbol and an exchange, for example "
                "symbol='RELIANCE', exchange='NSE'."
            )
        alias = C.resolve_index_alias(sym)
        note = ""
        if alias:
            note = f" ({sym} is not an OpenAlgo symbol, used {alias})"
            sym = alias
        if sym in C.INDEX_EXCHANGE and ex != C.INDEX_EXCHANGE[sym]:
            ex = C.INDEX_EXCHANGE[sym]
            note += f" ({sym} quotes on {ex})"

        yield ChartCommandEvent(commands=[{"op": "set_symbol", "symbol": sym,
                                          "exchange": ex}])
        yield f"Chart switched to {sym} on {ex}.{note}"

    async def set_chart_interval(self, interval: str,
                                 run_context: RunContext | None = None):
        """Change the candle interval of the chart.

        The valid set is broker-specific and is checked against a live intervals call,
        never against a remembered list.

        Args:
            interval (str): Candle interval such as 1m, 5m, 15m, 1h, D, W or M.

        Returns:
            str: One line naming the new interval.
        """
        wanted = (interval or "").strip()
        if not wanted:
            raise RetryAgentRun("set_chart_interval needs an interval, for example '15m'.")

        supported, failure = await self._supported_intervals()
        if failure:
            # A broker or network failure, which the model cannot fix by retrying.
            yield to_json(err(f"cannot verify intervals: {failure}", "set_chart_interval"))
            return

        match = next((s for s in supported if s.lower() == wanted.lower()), None)
        if match is None:
            raise RetryAgentRun(
                f"This broker does not offer a {wanted!r} interval. It supports: "
                f"{', '.join(supported) or 'nothing reported'}. Pick one of those."
            )

        yield ChartCommandEvent(commands=[{"op": "set_interval", "interval": match}])
        yield f"Chart interval set to {match}."

    async def set_chart_type(self, chart_type: str,
                             run_context: RunContext | None = None):
        """Change how the bars are drawn.

        Args:
            chart_type (str): One of candlestick, hollow-candle, volume-candle, bar,
                high-low, line, line-markers, step, area, hlc-area, baseline, column,
                histogram, point-figure or kagi. Heikin-Ashi and Renko are bar
                transforms in this engine, not chart types, and cannot be set here.

        Returns:
            str: One line naming the new chart type.
        """
        wanted = (chart_type or "").strip().lower().replace(" ", "-").replace("_", "-")
        known = [str(t) for t in load_catalogue().get("chart_types", [])]
        if wanted in ("heikin-ashi", "heikinashi", "ha", "renko"):
            raise RetryAgentRun(
                f"{chart_type} is a bar transform in this chart engine, not a chart "
                f"type, so it cannot be set here. The available types are: "
                f"{', '.join(known)}."
            )
        if known and wanted not in known:
            raise RetryAgentRun(
                f"Unknown chart type {chart_type!r}. Available types: {', '.join(known)}."
            )

        yield ChartCommandEvent(commands=[{"op": "set_chart_type", "chartType": wanted}])
        yield f"Chart type set to {wanted}."

    async def add_chart_indicator(self, indicator_id: str, settings: dict | None = None,
                                  run_context: RunContext | None = None):
        """Add an indicator to the chart, with the settings the user asked for.

        There are 102 indicators in the chart's own registry, each with named settings
        and declared bounds. Pass named settings when you know them. When the user gave
        bare numbers ("supertrend 3,10"), pass them positionally as
        {"values": [3, 10]}: they are landed on the indicator's numeric settings by
        closeness to its declared defaults, and the reply states which reading was used
        so the user can correct it in a few words.

        Args:
            indicator_id (str): Indicator name or registry id, for example "supertrend",
                "rsi", "bollinger", "macd" or "ichimoku".
            settings (dict): Optional. Either named settings such as
                {"period": 10, "multiplier": 3}, or bare numbers as spoken, such as
                {"values": [3, 10]}. Leave empty for the indicator's own defaults.

        Returns:
            str: One line naming the indicator and every setting applied.
        """
        catalogue = load_catalogue().get("indicators", {})
        if not catalogue:
            yield to_json(err("indicator catalogue is missing from this build",
                              "add_chart_indicator"))
            return

        resolved = resolve_indicator_id(indicator_id)
        if resolved is None:
            text = (indicator_id or "").strip().lower()
            near = [k for k in catalogue if text and (text[:3] in k or k[:3] in text)][:6]
            hint = f" Closest ids: {', '.join(near)}." if near else ""
            raise RetryAgentRun(
                f"The chart has no indicator called {indicator_id!r}.{hint} "
                f"There are {len(catalogue)} available, including supertrend, rsi, "
                "macd, bollinger, ema, sma, vwap, adx, atr and ichimoku."
            )

        entry = catalogue[resolved]
        by_key = {i["key"]: i for i in entry.get("inputs", [])}
        applied: dict[str, Any] = {}
        reading = ""

        positional = _coerce_values(settings)
        if positional:
            applied, reading = assign_positional(entry, positional)

        for key, value in (settings or {}).items():
            if key == "values":
                continue
            spec = by_key.get(key)
            if spec is None:
                raise RetryAgentRun(
                    f"{entry['name']} has no setting called {key!r}. Its settings are: "
                    + ", ".join(f"{i['key']} ({i['label']})" for i in entry.get("inputs", []))
                    + "."
                )
            if spec.get("type") == "number":
                number = _finite(value)
                if number is None:
                    raise RetryAgentRun(
                        f"{entry['name']}.{key} must be a number, got {value!r}."
                    )
                if not _in_bounds(spec, number):
                    raise RetryAgentRun(
                        f"{entry['name']}.{key} ({spec['label']}) must be between "
                        f"{spec.get('min')} and {spec.get('max')}, got {number}."
                    )
                applied[key] = _tidy(number)
            else:
                applied[key] = value

        described = ", ".join(
            f"{by_key.get(k, {}).get('label', k)} {v}" for k, v in applied.items()
        ) or "its defaults"

        yield ChartCommandEvent(commands=[{
            "op": "add_indicator", "indicatorId": resolved,
            "settings": applied or {},
        }])

        sentence = f"Added {entry['name']} to the chart with {described}."
        if positional and reading:
            spoken = ",".join(str(_tidy(v)) for v in positional)
            sentence += (
                f" Read {spoken} as {reading}; say which setting you meant if that is "
                "the wrong way round."
            )
        yield sentence

    async def remove_chart_indicator(self, indicator_id: str,
                                     run_context: RunContext | None = None):
        """Remove an indicator from the chart.

        Args:
            indicator_id (str): Indicator name or registry id currently on the chart,
                for example "supertrend". Call describe_chart to see what is on it.

        Returns:
            str: One line confirming the removal.
        """
        resolved = resolve_indicator_id(indicator_id) or (indicator_id or "").strip().lower()
        if not resolved:
            raise RetryAgentRun("remove_chart_indicator needs an indicator id.")

        ctx = read_context(run_context)
        instance = None
        if ctx is not None:
            for item in ctx.indicators:
                if item.indicator_id == resolved:
                    instance = item.instance_id
                    break
            if instance is None and ctx.indicators:
                names = ", ".join(i.indicator_id for i in ctx.indicators)
                yield f"{resolved} is not on the chart. What is on it: {names}."
                return

        command: dict[str, Any] = {"op": "remove_indicator", "indicatorId": resolved}
        if instance:
            command["instanceId"] = instance
        yield ChartCommandEvent(commands=[command])
        yield f"Removed {resolved} from the chart."

    async def clear_annotations(self, group: str = "",
                                run_context: RunContext | None = None):
        """Remove the analyst's markup from the chart.

        Never touches a drawing the user placed by hand.

        Args:
            group (str): Which layer to remove: envelope, trendline, levels, zone or
                targets. Leave empty to remove every layer the analyst drew.

        Returns:
            str: One line saying what was removed.
        """
        wanted = (group or "").strip().lower()
        if wanted and wanted not in ALL_GROUPS:
            raise RetryAgentRun(
                f"Unknown annotation group {group!r}. Valid groups: "
                f"{', '.join(ALL_GROUPS)}, or leave it empty to clear all of them."
            )

        store = _patterns(run_context)
        if wanted:
            store.pop(wanted, None)
        else:
            store.clear()

        yield ChartCommandEvent(commands=[clear_command(wanted or None)])
        yield (f"Cleared the {wanted} markup." if wanted
               else "Cleared all analyst markup from the chart.")

    # --- markup -------------------------------------------------------------

    async def draw_envelope(self, from_price: float = 0.0, to_price: float = 0.0,
                            lookback: int = DEFAULT_LOOKBACK, label: str = "",
                            run_context: RunContext | None = None):
        """Draw a swing envelope: a closed band through the real pivot highs and lows.

        This is the primary markup tool. Prices the user names are loose, so they are
        snapped onto actual swing pivots before anything is drawn; the model never
        writes a number that lands on the canvas.

        Args:
            from_price (float): Roughly where the move started, as the user said it.
                Pass 0 to let the detector pick the start of the window.
            to_price (float): Roughly where the move ended. Pass 0 to run to the last
                bar. A to_price below from_price makes the envelope descending.
            lookback (int): How many bars back to search for pivots. Defaults to 300.
            label (str): Optional caption drawn with the envelope.

        Returns:
            str: One line naming the anchors the envelope was actually built on.
        """
        ctx = read_context(run_context)
        if ctx is None:
            yield NO_CHART
            return
        if G is None:
            yield NO_GEOMETRY
            return

        res = await self._frame(ctx, lookback)
        if not res["ok"]:
            yield to_json(err(f"no candles for {ctx.symbol}: {res['error']}",
                              "draw_envelope"))
            return
        df = res["frame"]

        start_pivot = None
        end_pivot = None
        if _finite(from_price):
            start_pivot = await asyncio.to_thread(
                G.snap_to_pivot, df, float(from_price), "any", SNAP_TOLERANCE_PCT)
        if _finite(to_price):
            end_pivot = await asyncio.to_thread(
                G.snap_to_pivot, df, float(to_price), "any", SNAP_TOLERANCE_PCT)

        missing = [name for name, price, hit in
                   (("from_price", from_price, start_pivot), ("to_price", to_price, end_pivot))
                   if _finite(price) and hit is None]
        if missing:
            low, high = float(df["low"].min()), float(df["high"].max())
            raise RetryAgentRun(
                f"No swing pivot within {SNAP_TOLERANCE_PCT}% of the {', '.join(missing)} "
                f"you gave. Over the last {len(df)} bars {ctx.symbol} ranged {low:.2f} "
                f"to {high:.2f}. Give a price inside that range, or omit it and let the "
                "detector choose."
            )

        # A price the user named pins that end of the window; an end they did not name
        # falls back to the viewport, because "draw the swing" means the swing they can
        # see, not every bar the fetch happened to return.
        ctx_start, ctx_end = self._window(ctx)
        window_start = start_pivot.time if start_pivot is not None else ctx_start
        window_end = end_pivot.time if end_pivot is not None else ctx_end
        if window_start is not None and window_end is not None and window_start > window_end:
            window_start, window_end = window_end, window_start

        # CHANNEL_PIVOTS keeps the fit on major swings. A channel describes the
        # move; it does not trace it. Nine pivots a side produced a line fitted to
        # noise, and before that a polyline that kinked back on itself.
        env = await asyncio.to_thread(
            G.envelope, df, 3, 3, CHANNEL_PIVOTS, window_start, window_end)
        highs = list(env.get("highs") or [])
        lows = list(env.get("lows") or [])
        if len(highs) < 2 or len(lows) < 2:
            yield (f"Not enough swing structure in the last {len(df)} bars of "
                   f"{ctx.symbol} {ctx.interval} to fit a channel: it needs at least "
                   "two swing highs and two swing lows. Try a longer lookback.")
            return

        # Two straight lines, each fitted to its own side, so they are free to
        # converge or diverge. That is what makes a triangle or a wedge expressible;
        # forcing the second rail parallel would flatten every one of them into a
        # constant-width band that describes nothing.
        upper_fit = await asyncio.to_thread(G.fit_trendline, highs, TREND_TOLERANCE_PCT)
        lower_fit = await asyncio.to_thread(G.fit_trendline, lows, TREND_TOLERANCE_PCT)
        if upper_fit.get("from") is None or lower_fit.get("from") is None:
            yield (f"Could not fit a channel through the swings on {ctx.symbol} "
                   f"{ctx.interval}. Try a longer lookback.")
            return

        # Both rails are evaluated at the SAME two times. Fitted separately they
        # each span only their own pivots, and two lines starting and stopping at
        # different bars read as two unrelated trendlines rather than a channel.
        edges = [p.time for p in highs] + [p.time for p in lows]
        t0, t1 = min(edges), max(edges)
        upper, lower = _channel_rails(upper_fit, lower_fit, highs, lows, t0, t1, df)

        tone = _tone_for(env.get("direction"))
        caption = label or None
        shapes = [
            Trendline(from_=upper[0], to=upper[1], extend_right=True,
                      label=caption, tone=tone),
            Trendline(from_=lower[0], to=lower[1], extend_right=True, tone=tone),
        ]

        # The channel's height is measured at the right-hand edge, where a trader
        # reads it, not as an average across a band that may be widening.
        width = abs(upper[1].price - lower[1].price)
        record = _store_envelope(run_context, GROUP_ENVELOPE, ctx, env)
        record["width"] = width
        yield ChartCommandEvent(commands=[draw_command(GROUP_ENVELOPE, shapes)])

        shape_word = _channel_shape(upper, lower)
        yield (
            f"Drew a {shape_word} channel on {ctx.symbol} {ctx.interval}. Upper rail "
            f"{upper[0].price:.2f} to {upper[1].price:.2f} across "
            f"{upper_fit['touches']} swing highs, lower rail {lower[0].price:.2f} to "
            f"{lower[1].price:.2f} across {lower_fit['touches']} swing lows. Width at "
            f"the right edge {_round(width)}."
        )

    async def draw_trendline(self, from_price: float = 0.0, to_price: float = 0.0,
                             extend: bool = True, label: str = "",
                             lookback: int = DEFAULT_LOOKBACK,
                             run_context: RunContext | None = None):
        """Draw one straight trendline through real pivots.

        Both prices are snapped onto pivots and the line is then fitted across every
        pivot of that kind between them, so its slope reflects the touches rather than
        two cherry-picked ends.

        Args:
            from_price (float): Roughly where the line should start. Pass 0 to start at
                the first pivot in the window.
            to_price (float): Roughly where it should end. Pass 0 to run to the last one.
            extend (bool): Project the line to the right of the last anchor. Defaults
                to True.
            label (str): Optional caption.
            lookback (int): How many bars back to search for pivots. Defaults to 300.

        Returns:
            str: One line naming the anchors, the touch count and the fit quality.
        """
        ctx = read_context(run_context)
        if ctx is None:
            yield NO_CHART
            return
        if G is None:
            yield NO_GEOMETRY
            return

        res = await self._frame(ctx, lookback)
        if not res["ok"]:
            yield to_json(err(f"no candles for {ctx.symbol}: {res['error']}",
                              "draw_trendline"))
            return
        df = res["frame"]

        start_price = _finite(from_price)
        end_price = _finite(to_price)
        descending = (start_price is not None and end_price is not None
                      and end_price < start_price)
        kind = "high" if descending else "low"

        start_pivot = None
        end_pivot = None
        if start_price:
            start_pivot = await asyncio.to_thread(
                G.snap_to_pivot, df, start_price, kind, SNAP_TOLERANCE_PCT)
        if end_price:
            end_pivot = await asyncio.to_thread(
                G.snap_to_pivot, df, end_price, kind, SNAP_TOLERANCE_PCT)

        ctx_start, ctx_end = self._window(ctx)
        window_start = start_pivot.time if start_pivot is not None else ctx_start
        window_end = end_pivot.time if end_pivot is not None else ctx_end
        if window_start is not None and window_end is not None and window_start > window_end:
            window_start, window_end = window_end, window_start

        pivot_highs, pivot_lows = await asyncio.to_thread(
            G.swing_pivots, df, 3, 3, 0.0, window_start, window_end)
        pivots = pivot_highs if kind == "high" else pivot_lows
        if len(pivots) < 2:
            yield (f"Fewer than two swing {kind}s in that range on {ctx.symbol} "
                   f"{ctx.interval}, so there is nothing to draw a line through. Widen "
                   "the lookback or the price range.")
            return

        fit = await asyncio.to_thread(G.fit_trendline, pivots, 0.5)
        a, b = fit.get("from"), fit.get("to")
        if a is None or b is None:
            a, b = pivots[0], pivots[-1]

        tone = "bearish" if b.price < a.price else "bullish"
        shape = Trendline(
            **{"from": Anchor(time=float(a.time), price=float(a.price))},
            to=Anchor(time=float(b.time), price=float(b.price)),
            extendRight=bool(extend), label=(label or None), tone=tone,
        )
        _patterns(run_context)[GROUP_TRENDLINE] = {
            "kind": "trendline", "group": GROUP_TRENDLINE,
            "symbol": ctx.symbol, "exchange": ctx.exchange, "interval": ctx.interval,
            "from": _pivot_dict(a), "to": _pivot_dict(b),
            "slope": _finite(fit.get("slope")), "intercept": _finite(fit.get("intercept")),
            "touches": fit.get("touches"), "r2": _finite(fit.get("r2")),
        }

        yield ChartCommandEvent(commands=[draw_command(GROUP_TRENDLINE, [shape])])
        yield (
            f"Drew a {tone} trendline on {ctx.symbol} {ctx.interval} from "
            f"{a.price:.2f} to {b.price:.2f}, {fit.get('touches', 2)} touches, "
            f"r2 {_round(fit.get('r2'), 3)}"
            + (", extended right." if extend else ".")
        )

    async def draw_levels(self, count: int = 4, lookback: int = DEFAULT_LOOKBACK,
                          run_context: RunContext | None = None):
        """Draw the strongest horizontal support and resistance levels.

        Levels come from clustered pivot prices, ranked by how many times price
        actually turned there.

        Args:
            count (int): How many levels to draw, at most 8. Defaults to 4.
            lookback (int): How many bars back to search. Defaults to 300.

        Returns:
            str: One line listing the levels drawn and their touch counts.
        """
        ctx = read_context(run_context)
        if ctx is None:
            yield NO_CHART
            return
        if G is None:
            yield NO_GEOMETRY
            return

        wanted = max(1, min(int(count or 4), MAX_LEVELS))
        res = await self._frame(ctx, lookback)
        if not res["ok"]:
            yield to_json(err(f"no candles for {ctx.symbol}: {res['error']}",
                              "draw_levels"))
            return
        df = res["frame"]

        start, end = self._window(ctx)
        found = await asyncio.to_thread(G.support_resistance, df, 0, 2, start, end)
        if not found:
            yield (f"No level on {ctx.symbol} {ctx.interval} was touched twice in the "
                   f"last {len(df)} bars, so there is nothing worth drawing.")
            return

        # support_resistance already returns strongest first: touch count, then most
        # recently touched. Re-ranking here would only undo that.
        chosen = [r for r in found if _finite(r.get("price")) is not None][:wanted]
        last_price = ctx.last_price if ctx.last_price is not None else float(df["close"].iloc[-1])
        # UTC seconds, via the one conversion the whole system agrees on. OpenAlgo hands
        # daily bars back timezone-naive and intraday bars tz-aware Asia/Kolkata, so
        # calling Timestamp.timestamp() here would put daily levels five and a half
        # hours off.
        anchor_time = float(G.to_utc_seconds(df.index)[-1])

        shapes: list[Level] = []
        described: list[str] = []
        for row in chosen:
            price = float(_finite(row.get("price")))
            touches = int(row.get("touches") or 0)
            role = str(row.get("kind") or ("resistance" if price > last_price else "support"))
            tone = "bearish" if price > last_price else "bullish"
            shapes.append(Level(price=price, time=_finite(row.get("first_time")) or anchor_time,
                                ray=False, label=f"{role} {price:.2f}", tone=tone))
            described.append(f"{price:.2f} ({role}, {touches} touches)")

        _patterns(run_context)[GROUP_LEVELS] = {
            "kind": "levels", "group": GROUP_LEVELS,
            "symbol": ctx.symbol, "exchange": ctx.exchange, "interval": ctx.interval,
            "levels": [{"price": _round(r.get("price")), "touches": r.get("touches"),
                        "kind": r.get("kind")} for r in chosen],
        }

        yield ChartCommandEvent(commands=[draw_command(GROUP_LEVELS, shapes)])
        yield (f"Drew {len(shapes)} levels on {ctx.symbol} {ctx.interval}: "
               + ", ".join(described) + ".")

    async def draw_zone(self, from_price: float, to_price: float, label: str = "",
                        run_context: RunContext | None = None):
        """Shade a price band across the chart, for a supply or demand area.

        Args:
            from_price (float): One edge of the band.
            to_price (float): The other edge. Order does not matter.
            label (str): Optional caption, for example "supply" or "demand".

        Returns:
            str: One line naming the band drawn.
        """
        ctx = read_context(run_context)
        if ctx is None:
            yield NO_CHART
            return

        low = _finite(min(from_price, to_price))
        high = _finite(max(from_price, to_price))
        if low is None or high is None or high <= low:
            raise RetryAgentRun(
                "draw_zone needs two different prices, for example from_price=1420 "
                "and to_price=1465."
            )

        start, end = self._window(ctx)
        if start is None or end is None:
            start = ctx.first_time
            end = ctx.last_time
        if start is None or end is None:
            yield ("The chart context carries no time range, so a zone cannot be "
                   "placed. Ask the user to reload the chart.")
            return

        last_price = ctx.last_price
        tone = "neutral"
        if last_price is not None:
            tone = "bearish" if low > last_price else "bullish" if high < last_price else "neutral"

        shape = Zone(
            **{"from": Anchor(time=float(start), price=high)},
            to=Anchor(time=float(end), price=low),
            label=(label or None), tone=tone,
        )
        _patterns(run_context)[GROUP_ZONE] = {
            "kind": "zone", "group": GROUP_ZONE,
            "symbol": ctx.symbol, "exchange": ctx.exchange, "interval": ctx.interval,
            "low": low, "high": high, "label": label or None,
        }

        yield ChartCommandEvent(commands=[draw_command(GROUP_ZONE, [shape])])
        yield (f"Shaded a {tone} zone on {ctx.symbol} {ctx.interval} between "
               f"{low:.2f} and {high:.2f}"
               + (f", labelled {label}." if label else "."))

    async def project_targets(self, group: str = GROUP_ENVELOPE,
                              run_context: RunContext | None = None):
        """Project measured-move targets off a pattern already on the chart.

        Reads the geometry stored when the pattern was drawn, so this still works in a
        later turn.

        Args:
            group (str): Which pattern to measure. Only "envelope" carries the geometry
                a measured move needs. Defaults to "envelope".

        Returns:
            str: One line naming the projected targets.
        """
        ctx = read_context(run_context)
        if ctx is None:
            yield NO_CHART
            return
        if G is None:
            yield NO_GEOMETRY
            return

        wanted = (group or GROUP_ENVELOPE).strip().lower()
        store = _patterns(run_context)
        record = store.get(wanted)
        if record is None:
            drawn = ", ".join(sorted(store)) or "nothing"
            raise RetryAgentRun(
                f"No {wanted} has been drawn in this session, so there is no geometry "
                f"to measure. Currently stored: {drawn}. Call draw_envelope first."
            )
        if record.get("kind") != "envelope":
            raise RetryAgentRun(
                f"A measured move needs an envelope, and {wanted} is a "
                f"{record.get('kind')}. Call draw_envelope first."
            )

        env = _restore_envelope(record)
        measured = await asyncio.to_thread(G.measured_move, env)
        targets = _extract_targets(measured)
        if not targets:
            yield ("That envelope has no measurable height, so a measured move would "
                   "project nothing. Draw the envelope over a wider swing first.")
            return

        last_price = ctx.last_price
        # The right edge of the pattern, plus its own duration: where the target would
        # be reached if the move repeats the pattern's pace. It is a seat for the
        # label, not a forecast of the date.
        anchor_time = (_finite(measured.get("time"))
                       or _finite(record.get("last_time"))
                       or _finite(ctx.last_time) or 0.0)
        seat_time = _finite(measured.get("target_time")) or anchor_time

        shapes: list[Any] = []
        described: list[str] = []
        for name, price in targets:
            tone = "neutral"
            if last_price is not None:
                tone = "bullish" if price > last_price else "bearish"
            shapes.append(Level(price=price, time=anchor_time, ray=True,
                                label=f"{name} {price:.2f}", tone=tone))
            described.append(f"{name} {price:.2f}")

        # A marker, not a callout: a callout needs a seat placed clear of price, and
        # there is no honest way to choose one without knowing the pane's pixel scale.
        direction = str(measured.get("direction") or record.get("direction") or "")
        headline = targets[0] if "down" not in direction.lower() else targets[-1]
        shapes.append(Marker(
            at=Anchor(time=seat_time, price=headline[1]),
            text=f"measured move {headline[1]:.2f}",
            tone=_tone_for(direction),
        ))

        store[GROUP_TARGETS] = {
            "kind": "targets", "group": GROUP_TARGETS, "from_group": wanted,
            "symbol": ctx.symbol, "exchange": ctx.exchange, "interval": ctx.interval,
            "width": _round(measured.get("width")),
            "breakout": _round(measured.get("breakout")),
            "breakdown": _round(measured.get("breakdown")),
            "targets": [{"name": n, "price": _round(p)} for n, p in targets],
        }

        yield ChartCommandEvent(commands=[draw_command(GROUP_TARGETS, shapes)])
        yield (
            f"Projected the {_round(measured.get('width'))} point height of the "
            f"{wanted} off both boundaries on {ctx.symbol} {ctx.interval}: "
            + ", ".join(described)
            + f". Break of {_round(measured.get('breakout'))} or "
              f"{_round(measured.get('breakdown'))} is what triggers them."
        )

    # --- analysis -----------------------------------------------------------

    async def analyse_trend(self, lookback: int = DEFAULT_LOOKBACK,
                            run_context: RunContext | None = None) -> str:
        """Read the trend structure off the chart, with numbers. Draws nothing.

        Reports direction, the slope of each envelope boundary, and whether the swing
        highs and lows are actually making higher highs and higher lows.

        Args:
            lookback (int): How many bars back to analyse. Defaults to 300.

        Returns:
            str: JSON with direction, slopes, the pivot sequence and a structure verdict.
        """
        ctx = read_context(run_context)
        if ctx is None:
            return NO_CHART
        if G is None:
            return NO_GEOMETRY

        res = await self._frame(ctx, lookback)
        if not res["ok"]:
            return to_json(err(f"no candles for {ctx.symbol}: {res['error']}",
                               "analyse_trend"))
        df = res["frame"]

        start, end = self._window(ctx)
        env = await asyncio.to_thread(G.envelope, df, 3, 3, 9, start, end)
        highs = list(env.get("highs") or [])
        lows = list(env.get("lows") or [])

        higher_highs = sum(1 for a, b in zip(highs, highs[1:]) if b.price > a.price)
        lower_highs = sum(1 for a, b in zip(highs, highs[1:]) if b.price < a.price)
        higher_lows = sum(1 for a, b in zip(lows, lows[1:]) if b.price > a.price)
        lower_lows = sum(1 for a, b in zip(lows, lows[1:]) if b.price < a.price)

        if lower_highs > higher_highs and lower_lows > higher_lows:
            structure = "lower highs and lower lows, a downtrend"
        elif higher_highs > lower_highs and higher_lows > lower_lows:
            structure = "higher highs and higher lows, an uptrend"
        elif lower_highs > higher_highs and higher_lows > lower_lows:
            structure = "lower highs into higher lows, a contracting range"
        elif higher_highs > lower_highs and lower_lows > higher_lows:
            structure = "higher highs and lower lows, an expanding range"
        else:
            structure = "no clean sequence of highs and lows"

        close = df["close"]
        first, last = float(close.iloc[0]), float(close.iloc[-1])
        change_pct = round((last - first) / first * 100, 2) if first else None

        return to_json(ok({
            "symbol": ctx.symbol, "exchange": ctx.exchange, "interval": ctx.interval,
            "bars_analysed": int(len(df)),
            "direction": env.get("direction"),
            "structure": structure,
            "slope_high_per_bar": _round(env.get("slope_high"), 4),
            "slope_low_per_bar": _round(env.get("slope_low"), 4),
            "envelope_width": _round(env.get("width")),
            "swing_highs": [_round(p.price) for p in highs],
            "swing_lows": [_round(p.price) for p in lows],
            "higher_highs": higher_highs, "lower_highs": lower_highs,
            "higher_lows": higher_lows, "lower_lows": lower_lows,
            "period_high": _round(df["high"].max()),
            "period_low": _round(df["low"].min()),
            "last_close": _round(last),
            "period_change_pct": change_pct,
        }, "analyse_trend"))

    async def analyse_momentum(self, lookback: int = DEFAULT_LOOKBACK,
                               run_context: RunContext | None = None) -> str:
        """Read RSI, MACD and ADX off the chart's own instrument. Draws nothing.

        Args:
            lookback (int): How many bars back to compute over. Defaults to 300, which
                clears the warm-up of all three.

        Returns:
            str: JSON with the latest value and direction of each indicator, plus a
                one-line verdict.
        """
        ctx = read_context(run_context)
        if ctx is None:
            return NO_CHART

        res = await self._frame(ctx, max(int(lookback or DEFAULT_LOOKBACK), 300))
        if not res["ok"]:
            return to_json(err(f"no candles for {ctx.symbol}: {res['error']}",
                               "analyse_momentum"))
        df = res["frame"]

        readings: dict[str, Any] = {}
        failures: list[str] = []
        for name in ("rsi", "macd", "adx"):
            try:
                result = await asyncio.to_thread(compute, name, df, {}, None, 3)
            except IndicatorError as exc:
                failures.append(f"{name}: {exc}")
                continue
            summary = result.get("summary") or {}
            readings[name] = {
                "outputs": result["outputs"],
                # From the summary, not values[-1]: the tail can still hold a null
                # where the indicator has not warmed up, and a null reads as "no
                # signal" rather than "not enough bars".
                "latest": {k: _round((summary.get(k) or {}).get("latest"))
                           for k in result["values"]},
                "direction": {k: (summary.get(k) or {}).get("direction")
                              for k in result["values"]},
            }

        if not readings:
            return to_json(err("; ".join(failures) or "no indicator could be computed",
                               "analyse_momentum"))

        verdict = _momentum_verdict(readings)
        return to_json(ok({
            "symbol": ctx.symbol, "exchange": ctx.exchange, "interval": ctx.interval,
            "bars_used": int(len(df)),
            "readings": readings,
            "verdict": verdict,
            "failed": failures or None,
        }, "analyse_momentum"))

    async def describe_chart(self, run_context: RunContext | None = None) -> str:
        """Report what is on the chart right now. Draws nothing, fetches nothing.

        Use this before answering "what am I looking at", before removing an indicator,
        and before assuming a symbol or timeframe.

        Returns:
            str: JSON with the instrument, timeframe, loaded and visible ranges, every
                indicator on the chart with its settings, and the analyst markup drawn
                so far in this session.
        """
        ctx = read_context(run_context)
        if ctx is None:
            return NO_CHART

        store = _patterns(run_context)
        return to_json(ok({
            "symbol": ctx.symbol, "exchange": ctx.exchange, "interval": ctx.interval,
            "chart_type": ctx.chart_type,
            "theme": ctx.theme,
            "bars_loaded": ctx.bar_count,
            "loaded_from": ctx.first_time, "loaded_to": ctx.last_time,
            "visible_from": ctx.visible_from, "visible_to": ctx.visible_to,
            "last_price": ctx.last_price,
            "indicators": [
                {"indicator_id": i.indicator_id, "name": i.name,
                 "pane": i.pane_index, "settings": i.settings}
                for i in ctx.indicators
            ],
            "analyst_markup": sorted(store),
            "summary": ctx.describe(),
        }, "describe_chart"))


def _momentum_verdict(readings: dict[str, Any]) -> str:
    """Turn three indicator readings into one plain sentence."""
    parts: list[str] = []
    rsi = (readings.get("rsi") or {}).get("latest", {})
    rsi_value = next((v for v in rsi.values() if v is not None), None)
    if rsi_value is not None:
        if rsi_value >= 70:
            parts.append(f"RSI {rsi_value} is overbought")
        elif rsi_value <= 30:
            parts.append(f"RSI {rsi_value} is oversold")
        else:
            parts.append(f"RSI {rsi_value} is neutral")

    macd = (readings.get("macd") or {}).get("latest", {})
    line = macd.get("macd_line", macd.get("macd"))
    signal = macd.get("signal_line", macd.get("signal"))
    if line is not None and signal is not None:
        parts.append("MACD is above its signal" if line > signal
                     else "MACD is below its signal")

    adx = (readings.get("adx") or {}).get("latest", {})
    adx_value = adx.get("adx")
    if adx_value is not None:
        parts.append(f"ADX {adx_value} shows a "
                     + ("strong" if adx_value >= 25 else "weak") + " trend")

    return "; ".join(parts) or "no reading was conclusive"
